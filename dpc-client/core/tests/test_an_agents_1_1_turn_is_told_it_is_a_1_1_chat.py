"""An agent's 1:1 turn is told it is a 1:1 chat, with whom and through which door
(AN-AGENT-IS-TOLD-THE-CHAT-TYPE-ONLY-IN-A-GROUP-SO-IN-ITS-1-1-CHAT-IT-HAS-TO-GUESS,
follow-up 13).

Only the group doors handed the agent a chat_context, so turn_context's task block in
the agent's own chat with its person read {id, type: chat}. The agent manager now gives
a turn from a 1:1 door (the UI agent chat, Telegram, Discord) a block of the same shape:
chat_type "1:1", the chat's name, participants, the door and the person (this node's
owner, with his node, for the UI door and the owner's Telegram). A group turn keeps the
block its door built; a Telegram group chat is not called 1:1. The block holds nothing
that changes between turns, so it reads the same on every turn of one chat.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from dpc_client_core.dpc_agent import context
from dpc_client_core.managers import agent_manager as am

AGENT = "agent_forge_0001"
NODE = "dpc-node-" + "c" * 32


class _CapturingAgent:
    def __init__(self):
        self.calls = []
        self._last_usage, self._last_trace = {}, {}

    async def process(self, **kwargs):
        self.calls.append(kwargs)
        self._last_usage, self._last_trace = {}, {}
        return "an answer"


def _manager(tmp_path, conversation_id=AGENT):
    from dpc_client_core.conversation_monitor import ConversationMonitor
    monitor = ConversationMonitor(
        conversation_id=conversation_id,
        participants=[{"node_id": NODE, "name": "User", "context": "local"}],
        llm_manager=None)
    monitor._get_history_path = lambda: tmp_path / conversation_id / "history.json"
    manager = am.DpcAgentManager.__new__(am.DpcAgentManager)
    manager.agent_id = AGENT
    manager.firewall = MagicMock()
    manager.config = {}
    manager._agent = _CapturingAgent()
    manager._agents = {}
    manager._agent_monitors = {conversation_id: monitor}
    manager._memory_indexes_initialized = True
    manager._agent_display_name = "Forge"
    manager._interrupt_events = {}
    manager._kill_events = {}
    manager._daily_tokens_used = 0
    manager._daily_tokens_date = ""
    manager.service = SimpleNamespace(
        p2p_manager=SimpleNamespace(node_id=NODE, get_display_name=lambda: "Mike"),
        local_api=SimpleNamespace(broadcast_event=AsyncMock()),
        _conversation_display_name=lambda cid: "Forge (1:1)" if cid == AGENT else cid,
    )
    manager.ensure_started = AsyncMock(return_value=manager)
    manager._get_or_create_agent_monitor = lambda conv: monitor
    manager.get_session_state = lambda conv: {}
    manager._resolve_reasoning_effort = lambda conv, effort: (None, "none")
    manager._resolve_context_window = lambda: None
    return manager


@pytest.fixture(autouse=True)
def _signed(signing_identity):
    yield


@pytest.mark.asyncio
async def test_a_ui_turn_carries_a_1_1_block_naming_the_owner_and_his_node(tmp_path):
    manager = _manager(tmp_path)
    await manager._process_message_guarded("hello", AGENT, message_source="ui")
    block = manager._agent.calls[-1]["chat_context"]
    assert block == {
        "chat_type": "1:1",
        "chat_name": "Forge (1:1)",
        "chat_id": AGENT,
        "description": "",
        "participants": ["Mike (User)", "Forge [agent on this node]"],
        "door": "ui",
        "person": {"name": "Mike", "node_id": NODE, "is_owner": True},
    }


@pytest.mark.asyncio
async def test_the_block_is_the_same_on_every_turn_of_one_chat(tmp_path):
    manager = _manager(tmp_path)
    await manager._process_message_guarded("one", AGENT, message_source="ui")
    await manager._process_message_guarded("two", AGENT, message_source="ui")
    first, second = (c["chat_context"] for c in manager._agent.calls)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


@pytest.mark.asyncio
async def test_a_group_turn_keeps_the_block_its_door_built(tmp_path):
    manager = _manager(tmp_path)
    group_block = {"chat_type": "group", "chat_name": "Room", "chat_id": "group-x",
                   "description": "", "participants": ["Mike (User)"]}
    await manager._process_message_guarded("hi", AGENT, message_source="group",
                                           chat_context=dict(group_block))
    assert manager._agent.calls[-1]["chat_context"] == group_block


@pytest.mark.asyncio
async def test_a_group_turn_without_a_block_is_not_called_1_1(tmp_path):
    manager = _manager(tmp_path)
    await manager._process_message_guarded("hi", AGENT, message_source="group")
    assert manager._agent.calls[-1]["chat_context"] is None


def test_the_owners_telegram_names_the_owner_and_the_door():
    block = am.one_to_one_chat_context(
        AGENT, "telegram_owner", "mike_tg", "Forge", owner_name="Mike",
        owner_node_id=NODE, chat_name="Forge (1:1)", telegram_chat_id="111")
    assert block["door"] == "telegram"
    assert block["person"] == {"name": "Mike", "node_id": NODE, "is_owner": True,
                               "telegram_chat_id": "111"}
    assert block["participants"][0] == "Mike (User)"


def test_somebody_elses_telegram_names_the_sender_and_no_node():
    block = am.one_to_one_chat_context(
        "telegram-222", "telegram", "Anna", "Forge", owner_name="Mike",
        owner_node_id=NODE, telegram_chat_id="222")
    assert block["chat_type"] == "1:1"
    assert block["person"] == {"name": "Anna", "is_owner": False, "telegram_chat_id": "222"}
    assert block["participants"] == ["Anna (Telegram)", "Forge [agent on this node]"]
    assert block["chat_name"] == "telegram-222"


def test_a_telegram_group_chat_is_not_called_1_1():
    assert am.one_to_one_chat_context(
        "telegram--100", "telegram", "Anna", "Forge", owner_name="Mike",
        owner_node_id=NODE, telegram_chat_id="-1009999") is None


def test_discord_names_the_sender_and_the_door():
    block = am.one_to_one_chat_context(
        "discord-user-5", "discord", "bob (Discord)", "Forge", owner_name="Mike",
        owner_node_id=NODE)
    assert block["door"] == "discord"
    assert block["person"] == {"name": "bob (Discord)", "is_owner": False}
    assert block["participants"][0] == "bob (Discord)"


@pytest.mark.parametrize("source", ["external_agent", "scheduler", None])
def test_other_doors_get_no_1_1_block(source):
    assert am.one_to_one_chat_context(AGENT, source, "x", "Forge", owner_name="Mike",
                                      owner_node_id=NODE) is None


def test_the_runtime_section_of_a_1_1_turn_carries_chat_type_and_participants(tmp_path):
    block = am.one_to_one_chat_context(AGENT, "ui", "User", "Forge", owner_name="Mike",
                                       owner_node_id=NODE, chat_name="Forge (1:1)")
    text = context._build_runtime_section(
        tmp_path, {"id": "chat-1", "type": "chat", "chat_context": block})
    task = json.loads(text[text.index("{"):text.rindex("}") + 1])["task"]
    assert task["chat_type"] == "1:1"
    assert task["participants"] == ["Mike (User)", "Forge [agent on this node]"]
    assert task["door"] == "ui"
    assert task["person"]["node_id"] == NODE
