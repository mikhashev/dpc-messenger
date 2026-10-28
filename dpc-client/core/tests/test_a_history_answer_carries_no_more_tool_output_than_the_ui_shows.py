"""A history answer stays one message the local API can carry.

On 2026-10-28 a group history on the Linux node went past 10 MiB in one local
API message. Measured the same day on the Windows node: tool_calls were 97% of
the largest group answer (19.1 of 19.6 MiB), one record alone 8.9 MiB, while
the UI's tool row shows 4000 characters of an output and names the rest
(ui/src/lib/utils/toolDetail.ts). The backend now sends what that row shows.
"""

import json
import logging
from pathlib import Path

import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from dpc_client_core import local_api
from dpc_client_core import service as service_module
from dpc_client_core.service import CoreService
from dpc_client_core.tool_detail import EXPANDED_MAX, clip_detail, clip_tool_calls

# A Cyrillic letter: six bytes once json.dumps escapes it, as the local API does.
WIDE = chr(0x436)

UI_TOOL_DETAIL =Path(__file__).resolve().parents[2] / "ui" / "src" / "lib" / "utils" / "toolDetail.ts"


class FakeGroupManager:
    def __init__(self, conv_dir):
        self._conv_dir = conv_dir

    def _get_conversation_dir(self, conversation_id):
        return self._conv_dir


def make_service():
    svc = CoreService.__new__(CoreService)
    svc.conversation_monitors = {}
    svc.llm_manager = None
    return svc


def heavy_history(records=60, calls=8, output_chars=200_000):
    """Records shaped like a group's agent answers, each call with a huge output."""
    messages = []
    for i in range(records):
        messages.append({
            "id": f"m{i}", "role": "assistant", "content": f"answer {i}",
            "tool_calls": [
                {"tool": "browse_page", "input": json.dumps({"url": f"https://example.org/{j}"}),
                 "output": WIDE * output_chars, "is_error": False, "duration_ms": 10,
                 "round": j // 4, "round_text": WIDE * 3000}
                for j in range(calls)
            ],
        })
    return messages


# --- the clip itself -------------------------------------------------------

def test_a_short_output_is_untouched():
    assert clip_detail("ok") == "ok"
    exact = "x" * EXPANDED_MAX
    assert clip_detail(exact) is exact


def test_a_long_output_keeps_what_the_row_shows_and_names_the_rest():
    out = clip_detail("y" * (EXPANDED_MAX + 120))
    assert out == "y" * EXPANDED_MAX + "\n… 120 more characters (full text in the agent log)"


def test_the_cut_never_splits_a_surrogate_pair():
    # 3999 ASCII then an emoji: units 4000 and 4001 are its two halves.
    text = "a" * (EXPANDED_MAX - 1) + "\U0001F600" + "b" * 50
    out = clip_detail(text)
    head = out.split("\n… ")[0]
    assert head == "a" * (EXPANDED_MAX - 1)
    # Counted in UTF-16 units, as JavaScript's length counts them.
    assert "52 more characters" in out
    out.encode("utf-8")  # no lone surrogate left behind


def test_round_text_stays_on_the_first_call_of_each_round_only():
    calls = [
        {"tool": "a", "round": 1, "round_text": "why 1", "output": ""},
        {"tool": "b", "round": 1, "round_text": "why 1", "output": ""},
        {"tool": "c", "round": 2, "round_text": "why 2", "output": ""},
        {"tool": "d", "round_text": "why 0", "output": ""},
    ]
    clipped = clip_tool_calls(calls)
    assert [c.get("round_text") for c in clipped] == ["why 1", None, "why 2", "why 0"]
    # The caller's list — the record, the live trace — is not changed.
    assert calls[1]["round_text"] == "why 1"


def test_not_a_list_passes_as_is():
    assert clip_tool_calls(None) is None
    assert clip_tool_calls("x") == "x"


def test_the_ui_cuts_at_the_same_number_with_the_same_notice():
    source = UI_TOOL_DETAIL.read_text(encoding="utf-8")
    assert f"export const EXPANDED_MAX = {EXPANDED_MAX};" in source
    assert "more characters (full text in the agent log)" in source
    assert "CLIPPED_TAIL" in source


# --- the answer ------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_group_answer_with_megabytes_of_tool_output_stays_under_the_bound(tmp_path):
    conv_dir = tmp_path / "group-abc-heavy"
    conv_dir.mkdir()
    # 15 records x 8 calls of 20 000 Cyrillic characters (6 bytes each as JSON): 14 MB as JSON.
    messages = heavy_history(records=15, output_chars=20_000)
    (conv_dir / "history.json").write_text(json.dumps({"messages": messages}), encoding="utf-8")
    on_disk = (conv_dir / "history.json").read_bytes()
    full = len(json.dumps({"payload": {"messages": messages}}))
    assert full > local_api.MAX_INCOMING_MESSAGE_BYTES  # the shape that broke

    svc = make_service()
    svc.group_manager = FakeGroupManager(conv_dir)
    result = await svc.get_conversation_history("group-abc")

    frame = json.dumps({"id": "x", "command": "get_conversation_history", "status": "OK", "payload": result})
    assert len(frame) < local_api.OUTGOING_MESSAGE_WARN_BYTES
    assert result["message_count"] == len(messages)
    first = result["messages"][0]
    assert first["content"] == "answer 0"
    assert len(first["tool_calls"]) == 8
    assert first["tool_calls"][0]["output"].startswith(WIDE * EXPANDED_MAX + "\n… ")
    assert first["tool_calls"][0]["input"] == messages[0]["tool_calls"][0]["input"]
    assert (conv_dir / "history.json").read_bytes() == on_disk


@pytest.mark.asyncio
async def test_an_agent_answer_is_clipped_the_same_way(tmp_path, monkeypatch):
    conv_dir = tmp_path / "conversations" / "agent_x"
    conv_dir.mkdir(parents=True)
    messages = heavy_history(records=20, output_chars=20_000)
    (conv_dir / "history.json").write_text(json.dumps({"messages": messages}), encoding="utf-8")
    monkeypatch.setattr(service_module, "DPC_HOME_DIR", tmp_path)
    svc = make_service()
    svc._resolve_agent_token_limit = lambda _id: 0

    result = await svc.get_conversation_history("agent_x")

    assert result["message_count"] == 20
    output = result["messages"][0]["tool_calls"][0]["output"]
    assert len(output) < EXPANDED_MAX + 100


# --- the local API ---------------------------------------------------------

def test_an_oversized_message_to_the_ui_is_named_in_the_log(caplog):
    with caplog.at_level(logging.WARNING, logger=local_api.logger.name):
        local_api._warn_if_oversized("get_conversation_history", "x" * (local_api.OUTGOING_MESSAGE_WARN_BYTES + 1))
        local_api._warn_if_oversized("get_status", "x" * 100)
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 1
    assert "get_conversation_history" in lines[0]
    assert str(local_api.OUTGOING_MESSAGE_WARN_BYTES + 1) in lines[0]


def test_a_1009_close_is_a_warning_not_silence(caplog):
    closed = ConnectionClosedError(None, Close(1009, "frame exceeds limit of 10485760 bytes"))
    with caplog.at_level(logging.DEBUG, logger=local_api.logger.name):
        local_api._log_closed(closed)
        local_api._log_closed(ConnectionClosedError(Close(1000, ""), Close(1000, ""), True))
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "1009" in warnings[0].getMessage()
    assert "frame exceeds limit" in warnings[0].getMessage()


def test_the_server_reads_at_most_the_named_limit():
    source = Path(local_api.__file__).read_text(encoding="utf-8")
    assert "max_size=MAX_INCOMING_MESSAGE_BYTES" in source
    assert local_api.OUTGOING_MESSAGE_WARN_BYTES < local_api.MAX_INCOMING_MESSAGE_BYTES
