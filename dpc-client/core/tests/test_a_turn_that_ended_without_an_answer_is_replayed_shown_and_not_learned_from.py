"""A turn that ended without an answer, on the turns after it (Mike's call, 2026-10-27).

Its record (`turn_end` set) is replayed as the agent's own turn, so the prompt
never holds two user turns in a row; a `failed` one goes back as its first line
only, capped, so a provider's error body never enters the prompt. The rendering
is a function of the record alone: the same history renders the same bytes on
every later turn. Sleep reads past it. The UI gets the mark on the live
response, read off the record the reload will read.
"""

from __future__ import annotations

import json
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.dpc_agent.context import (
    FAILED_TURN_REPLAY_CHARS,
    build_llm_messages,
    history_body,
)
from dpc_client_core.dpc_agent.memory import Memory
from dpc_client_core.dpc_agent.sleep_pipeline import _format_archive_messages
from dpc_client_core.service import CoreService
from tests.test_a_turn_that_ends_without_an_answer_is_saved_with_why import (
    CID,
    _FakeAgent,
    _manager,
    _monitor,
)

READER = {"agent_id": CID, "display_name": "Ark", "node_id": "dpc-node-me"}
ERROR_BODY = (
    "⚠️ LLM error: Error code: 502 - upstream failed " + "x" * 400 + "\n"
    "<html><body>Bad gateway</body></html>\nTraceback (most recent call last): ..."
)


def _rec(idx, role, sender, content, **extra):
    return {"id": f"m{idx}", "role": role, "content": content, "msg_index": idx,
            "timestamp": f"2026-10-27T10:00:0{idx}Z", "sender_name": sender, **extra}


HISTORY = [
    _rec(1, "user", "Mike", "first question"),
    _rec(2, "assistant", "Ark", ERROR_BODY, turn_end="failed"),
    _rec(3, "user", "Mike", "second question"),
    _rec(4, "assistant", "Ark", "⚠️ Stopped by user after 2 rounds.", turn_end="stopped"),
    _rec(5, "user", "Mike", "third question"),
    _rec(6, "assistant", "Ark", "No answer this time: Linus did not take the request (busy).",
         turn_end="refused"),
]


@pytest.fixture(autouse=True)
def _no_embedding_download(monkeypatch):
    class _FakeProvider:
        model_name = "fake-embedding-model"
        dimensions = 384

    monkeypatch.setattr("dpc_client_core.dpc_agent.memory.get_embedding_provider",
                        lambda *a, **k: _FakeProvider())


def _turns(agent_root, history):
    messages, _ = build_llm_messages(
        agent_root=agent_root, memory=Memory(agent_root),
        task={"id": CID, "type": "chat", "text": "fourth question"},
        conversation_history=history, reader_identity=READER,
    )
    return [m for m in messages if m["role"] != "system"]


def test_a_marked_record_is_the_assistants_turn_and_no_two_user_turns_stand_together(tmp_path):
    turns = _turns(tmp_path / CID, HISTORY)

    roles = [t["role"] for t in turns]
    assert roles == ["user", "assistant"] * 3 + ["user"]
    assert all(a != b or a != "user" for a, b in zip(roles, roles[1:]))
    assert turns[3]["content"].endswith("⚠️ Stopped by user after 2 rounds.")
    assert turns[5]["content"].endswith("did not take the request (busy).")


def test_a_failed_turn_goes_back_as_its_first_line_capped(tmp_path):
    failed = _turns(tmp_path / CID, HISTORY)[1]["content"]

    assert failed == "[#2 | 10:00:02 | Ark] " + ERROR_BODY.splitlines()[0][:FAILED_TURN_REPLAY_CHARS]
    assert "<html>" not in failed and "Traceback" not in failed
    assert history_body(HISTORY[1]) == ERROR_BODY.splitlines()[0][:200]
    assert history_body(HISTORY[3]) == HISTORY[3]["content"]
    assert history_body(_rec(7, "assistant", "Ark", "an answer\nover two lines")) \
        == "an answer\nover two lines"


def test_the_history_renders_the_same_bytes_on_every_later_turn(tmp_path):
    first = _turns(tmp_path / CID, HISTORY)
    later = _turns(tmp_path / CID, HISTORY + [_rec(7, "user", "Mike", "fourth question")])

    assert [t["content"] for t in later[:6]] == [t["content"] for t in first[:6]]


def test_sleep_reads_past_a_marked_record():
    text = _format_archive_messages({"messages": HISTORY + [
        _rec(7, "assistant", "Ark", "A real answer."),
    ]})

    assert text.splitlines() == [
        "[Mike]: first question", "[Mike]: second question", "[Mike]: third question",
        "[Ark]: A real answer.",
    ]


# --- the live response carries the record's mark -------------------------------------


@pytest.fixture(autouse=True)
def _always_persist(monkeypatch):
    monkeypatch.setattr(ConversationMonitor, "persist_history", property(lambda self: True))


def _service_over(manager, monitor):
    manager.process_message = manager._process_message_guarded
    manager._agent_monitors = {CID: monitor}
    manager._get_or_create_agent_for_provider = lambda alias: manager._agent
    provider = SimpleNamespace(_ensure_manager=AsyncMock(), get_manager=lambda aid: manager)
    svc = CoreService.__new__(CoreService)
    svc.llm_manager = SimpleNamespace(providers={"dpc_agent": provider})
    svc.p2p_manager = SimpleNamespace(node_id="dpc-node-me", get_display_name=lambda: "Mike")
    svc.local_api = SimpleNamespace(send_response_to_all=AsyncMock(), broadcast_event=AsyncMock())
    svc.reset_cc_agent_chain = lambda cid: None
    svc.get_cc_display_name = lambda: "CC"
    svc._get_agent_display_name = lambda cid: "Ark"
    svc._check_agent_cc_mention = AsyncMock()
    return svc


def _payload(svc):
    call = svc.local_api.send_response_to_all.call_args
    assert call.kwargs["status"] == "OK", call
    return call.kwargs["payload"]


@pytest.mark.asyncio
@pytest.mark.parametrize("text, trace, expected", [
    ("⚠️ Stopped by user after 1 rounds.", {"turn_end": "stopped"}, "stopped"),
    ("⚠️ LLM error: boom", {"turn_end": "failed"}, "failed"),
    ("An answer.", {}, None),
], ids=["stopped", "failed", "answer"])
async def test_the_live_response_carries_the_records_mark(tmp_path, text, trace, expected):
    monitor = _monitor(tmp_path)
    svc = _service_over(_manager(_FakeAgent(text, trace), monitor), monitor)

    await svc._execute_agent_query("cmd-1", "hello", CID, include_context=False,
                                   instruction_set_name="general", agent_llm_provider=None)

    payload = _payload(svc)
    on_disk = json.loads((tmp_path / CID / "history.json").read_text(encoding="utf-8"))["messages"]
    assert payload["content"] == text
    assert payload["user_msg_index"] == on_disk[0]["msg_index"] == 1
    assert payload["assistant_msg_index"] == on_disk[1]["msg_index"] == 2
    if expected is None:
        assert "turn_end" not in payload
    else:
        assert payload["turn_end"] == on_disk[1]["turn_end"] == expected
