"""A question sent in an agent chat is confirmed with the id it was stored under.

The UI draws the question at once under an id it mints itself, and the backend
stores it as a record with an id of its own before the agent runs.
`user_message_confirmed` carried the record's index and not its id, so the
bubble kept the minted one: a reload of the newest page kept the bubble in
front of the record it stood for (the question drawn twice, both #1) and the
older-page cursor became an id the backend had never seen (dpc-client.log,
2026-10-30). The confirmation now names the record, on every path that stores
the person's message before the answer: a question to the agent, an @CC-only
question, and an image pasted into the chat.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.service import CoreService
from tests.test_a_turn_that_ends_without_an_answer_is_saved_with_why import (
    CID,
    _FakeAgent,
    _manager,
    _monitor,
)
from tests.test_an_image_pasted_into_an_agent_chat_is_kept_in_its_history import (
    GROUP,
    _history,
    _paste,
    _world,
)


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


def _confirmations(broadcast_mock):
    return [c.args[1] for c in broadcast_mock.call_args_list if c.args[0] == "user_message_confirmed"]


def _on_disk(tmp_path):
    return json.loads((tmp_path / CID / "history.json").read_text(encoding="utf-8"))["messages"]


async def _ask(tmp_path, prompt):
    monitor = _monitor(tmp_path)
    svc = _service_over(_manager(_FakeAgent("An answer.", {}), monitor), monitor)
    await svc._execute_agent_query("cmd-1", prompt, CID, include_context=False,
                                   instruction_set_name="general", agent_llm_provider=None)
    return svc


@pytest.mark.asyncio
async def test_a_question_to_the_agent_is_confirmed_with_the_stored_records_id(tmp_path):
    svc = await _ask(tmp_path, "hello")

    (confirmed,) = _confirmations(svc.local_api.broadcast_event)
    question = _on_disk(tmp_path)[0]
    assert question["role"] == "user" and question["content"] == "hello"
    assert confirmed["message_id"] == question["id"]
    assert confirmed["msg_index"] == question["msg_index"] == 1
    assert confirmed["conversation_id"] == CID and confirmed["command_id"] == "cmd-1"


@pytest.mark.asyncio
async def test_an_at_cc_only_question_is_confirmed_with_the_stored_records_id(tmp_path):
    svc = await _ask(tmp_path, "@CC are you there?")

    (confirmed,) = _confirmations(svc.local_api.broadcast_event)
    (question,) = _on_disk(tmp_path)
    assert question["content"] == "@CC are you there?"
    assert confirmed["message_id"] == question["id"]
    assert confirmed["msg_index"] == question["msg_index"]
    assert svc.local_api.send_response_to_all.call_args.kwargs["payload"]["provider"] == "cc_pending"


@pytest.mark.asyncio
async def test_a_pasted_image_is_stored_once_and_confirmed_with_that_records_id(
    tmp_path, signing_identity, monkeypatch,
):
    temp = tmp_path / "tmp"
    temp.mkdir()
    w = _world(tmp_path, monkeypatch, save=True, temp_dir=temp)

    await _paste(w)

    names = [n for n, _ in w.events]
    (confirmed,) = [p for n, p in w.events if n == "user_message_confirmed"]
    (question,) = [m for m in _history(w) if m["role"] == "user"]
    assert question["attachments"][0]["type"] == "image"
    assert confirmed["message_id"] == question["id"]
    assert confirmed["msg_index"] == question["msg_index"]
    # Before the answer, as a typed question is confirmed before the agent runs.
    assert names.index("user_message_confirmed") < names.index("ai_response_with_image")


@pytest.mark.asyncio
async def test_an_image_pasted_into_a_group_is_not_confirmed_from_the_agents_side(
    tmp_path, signing_identity, monkeypatch,
):
    temp = tmp_path / "tmp"
    temp.mkdir()
    w = _world(tmp_path, monkeypatch, save=True, temp_dir=temp)

    await _paste(w, conversation_id=GROUP)

    assert not [n for n, _ in w.events if n == "user_message_confirmed"]
