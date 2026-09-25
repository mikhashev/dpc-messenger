"""A CC <-> agent ping-pong stops after five rounds.

`send_cc_agent_response` injects CC's reply into an agent chat and, when the
reply mentions the agent, re-invokes the agent. The agent's answer can mention
CC, CC answers again, and so on; the chain depth is the only brake. It used to
be reset to zero inside `send_cc_agent_response` itself, so the check always
saw 0 and the loop never stopped. Only a human message may start a new chain.

The service is a bare `CoreService` with doubles for the agent manager, its
monitor and the local API; `_invoke_agent_in_agent_chat` is replaced by a
recorder, so the count is of scheduled agent invocations.
"""

import asyncio

import pytest

from dpc_client_core.service import CoreService


class _Monitor:
    def __init__(self):
        self.message_history = []

    def add_message(self, role, content, timestamp=None, **kwargs):
        self.message_history.append({"role": role, "content": content, **kwargs})

    def save_history(self):
        pass

    def get_last_msg_index(self):
        return len(self.message_history) - 1


class _Manager:
    def __init__(self, conversation_id):
        self._agent_monitors = {conversation_id: _Monitor()}

    def _get_or_create_agent_monitor(self, conversation_id):
        return self._agent_monitors.setdefault(conversation_id, _Monitor())

    def get_session_state(self, conversation_id):
        return {}


class _Provider:
    def __init__(self):
        self.managers = {}

    async def _ensure_manager(self, agent_id):
        self.get_manager(agent_id)

    def get_manager(self, conversation_id):
        return self.managers.setdefault(conversation_id, _Manager(conversation_id))


class _LocalApi:
    async def broadcast_event(self, *args, **kwargs):
        pass

    async def send_response_to_all(self, **kwargs):
        pass


def _service():
    service = CoreService.__new__(CoreService)
    provider = _Provider()
    service.llm_manager = type("L", (), {"providers": {"dpc_agent": provider}})()
    service.local_api = _LocalApi()
    service.telegram_manager = None
    service._cc_ark_chain_depths = {}
    service.get_cc_display_name = lambda: "CC"
    service._get_agent_display_name = lambda agent_id=None: "Ark"
    service.invoked = []

    def _record(conversation_id, cc_text, manager):
        service.invoked.append(conversation_id)
        # The agent answers, so CC's next line is not a duplicate of the last.
        manager._agent_monitors[conversation_id].add_message("assistant", "@CC ok")

        async def _noop():
            return None

        return _noop()

    service._invoke_agent_in_agent_chat = _record
    return service


async def _cc_says(service, conversation_id, rounds):
    for i in range(rounds):
        result = await service.send_cc_agent_response(conversation_id, f"@Ark round {i}")
        assert result["status"] == "success"
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_the_agent_is_invoked_five_times_and_not_the_sixth():
    service = _service()
    await _cc_says(service, "agent_001", 8)
    assert service.invoked == ["agent_001"] * 5


@pytest.mark.asyncio
async def test_one_conversation_spent_its_chain_the_other_still_has_its_own():
    service = _service()
    await _cc_says(service, "agent_001", 6)
    await _cc_says(service, "agent_002", 1)
    assert service.invoked.count("agent_001") == 5
    assert service.invoked.count("agent_002") == 1


@pytest.mark.asyncio
async def test_a_human_message_gives_the_conversation_a_new_chain():
    service = _service()
    await _cc_says(service, "agent_001", 6)
    assert service.invoked.count("agent_001") == 5

    # The human path resets the depth first; the rest of the query is cut
    # short here, since only the reset is under test.
    def _stop():
        raise RuntimeError("stop after the reset")

    service.get_cc_display_name = _stop
    await service._execute_agent_query(
        command_id="c1", prompt="hello", conversation_id="agent_001",
        include_context=False, instruction_set_name=None, agent_llm_provider=None,
    )
    service.get_cc_display_name = lambda: "CC"

    await _cc_says(service, "agent_001", 1)
    assert service.invoked.count("agent_001") == 6


# --- a human writing from the agent's Telegram bot also starts a new chain ---

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from dpc_client_core.managers.agent_telegram_bridge import AgentTelegramBridge

_TG_CHAT = "424242"


def _telegram_bridge(service, conversation_id):
    bridge = AgentTelegramBridge(
        bot_token="t", allowed_chat_ids=[_TG_CHAT],
        agent_id=conversation_id, unified_conversation=False,
    )
    manager = service.llm_manager.providers["dpc_agent"].get_manager(conversation_id)
    manager.service = service
    bridge.set_message_handler(AsyncMock(return_value="agent reply"), manager)
    # Unified off keeps the handler from touching history; the conversation is
    # then telegram-<chat>, so the chain under test lives there.
    return bridge


def _tg_update(text):
    u = MagicMock()
    u.effective_chat.id = _TG_CHAT
    u.effective_user.first_name = "Mike"
    u.message.text = text
    u.message.reply_text = AsyncMock()
    return u


def _tg_context():
    ctx = SimpleNamespace(bot=SimpleNamespace())
    ctx.bot.send_chat_action = AsyncMock()
    return ctx


@pytest.mark.asyncio
async def test_a_human_message_from_telegram_gives_the_conversation_a_new_chain():
    conversation_id = f"telegram-{_TG_CHAT}"
    service = _service()
    await _cc_says(service, conversation_id, 6)
    assert service.invoked.count(conversation_id) == 5

    bridge = _telegram_bridge(service, conversation_id)
    await bridge._handle_message(_tg_update("hello from the phone"), _tg_context())
    bridge._message_handler.assert_awaited()

    await _cc_says(service, conversation_id, 1)
    assert service.invoked.count(conversation_id) == 6


@pytest.mark.asyncio
async def test_a_cc_only_message_from_telegram_is_human_too():
    """`@CC` alone skips the agent, but a person still wrote it."""
    conversation_id = f"telegram-{_TG_CHAT}"
    service = _service()
    service.p2p_manager = SimpleNamespace(node_id="dpc-node-test")
    service._check_agent_cc_mention = AsyncMock()
    await _cc_says(service, conversation_id, 6)

    bridge = _telegram_bridge(service, conversation_id)
    await bridge._handle_message(_tg_update("@CC look at this"), _tg_context())
    bridge._message_handler.assert_not_awaited()

    await _cc_says(service, conversation_id, 1)
    assert service.invoked.count(conversation_id) == 6


@pytest.mark.asyncio
async def test_a_human_message_through_the_main_telegram_bot_gives_a_new_chain():
    """The main bot routes a chat linked to an agent into that agent's conversation."""
    from datetime import datetime

    from dpc_client_core.coordinators.telegram_coordinator import TelegramBridge

    conversation_id = "agent_001"
    service = _service()
    await _cc_says(service, conversation_id, 6)
    assert service.invoked.count(conversation_id) == 5

    manager = service.llm_manager.providers["dpc_agent"].get_manager(conversation_id)
    manager.process_message = AsyncMock(return_value="")
    service.conversation_monitors = {"agent-agent_001": SimpleNamespace(on_message=AsyncMock())}
    service.settings = MagicMock()
    service.get_conversation_history = AsyncMock(return_value={"messages": []})

    telegram = MagicMock()
    telegram.is_allowed.return_value = True
    telegram.bridge_to_p2p = False
    telegram.send_message = AsyncMock()
    coordinator = TelegramBridge.__new__(TelegramBridge)
    coordinator.service = service
    coordinator.telegram = telegram
    coordinator._get_or_create_conversation_id = lambda chat_id, agent_id=None: "agent-agent_001"
    coordinator._load_agent_context = lambda agent_id: {}

    update = MagicMock()
    update.message.chat_id = _TG_CHAT
    update.message.text = "hello from the phone"
    update.message.message_id = 7
    update.message.from_user.full_name = "Mike"
    update.message.date = datetime(2026, 9, 25, 12, 0, 0)
    await coordinator.handle_text_message(update, None)
    manager.process_message.assert_awaited()

    await _cc_says(service, conversation_id, 1)
    assert service.invoked.count(conversation_id) == 6
