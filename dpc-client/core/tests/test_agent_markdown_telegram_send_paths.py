"""Every door that carries agent or CC text to Telegram renders its Markdown.

The converter is tested in test_agent_markdown_reaches_telegram_rendered.py;
here each send path is driven with a mocked bot and checked for
parse_mode="HTML", rendered tags, and a plain-text resend when Telegram
refuses the markup.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest, NetworkError

from dpc_client_core.dpc_agent.events import AgentEvent, EventType
from dpc_client_core.managers.agent_telegram_bridge import AgentTelegramBridge

CHAT = "424242"
AGENT_MD = "## Done\n**3** files, see `a<b>.py`"
AGENT_HTML = "<b>Done</b>\n<b>3</b> files, see <code>a&lt;b&gt;.py</code>"
AGENT_PLAIN = "Done\n3 files, see a<b>.py"


def _bridge():
    bridge = AgentTelegramBridge(bot_token="t", allowed_chat_ids=[CHAT])
    bridge._enabled = True
    bridge._bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
    return bridge


def _sent(bot):
    return [(c.kwargs["text"], c.kwargs["parse_mode"]) for c in bot.send_message.await_args_list]


# --- the agent's own bot ----------------------------------------------------


def _update(text="hi"):
    u = MagicMock()
    u.effective_chat.id = CHAT
    u.effective_user.first_name = "Mike"
    u.message.text = text
    u.message.reply_text = AsyncMock()
    return u


def _ctx():
    return SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))


@pytest.mark.asyncio
async def test_the_agents_reply_to_a_telegram_message_is_rendered():
    bridge = _bridge()
    bridge._message_handler = AsyncMock(return_value=AGENT_MD)
    update = _update()
    await bridge._handle_message(update, _ctx())
    update.message.reply_text.assert_awaited_once_with(AGENT_HTML, parse_mode="HTML")


@pytest.mark.asyncio
async def test_a_reply_telegram_cannot_parse_arrives_as_plain_text():
    bridge = _bridge()
    bridge._message_handler = AsyncMock(return_value=AGENT_MD)
    update = _update()
    update.message.reply_text = AsyncMock(side_effect=[BadRequest("Can't parse entities"), None])
    await bridge._handle_message(update, _ctx())
    calls = update.message.reply_text.await_args_list
    assert calls[1].args == (AGENT_PLAIN,)
    assert calls[1].kwargs == {"parse_mode": None}


@pytest.mark.asyncio
async def test_a_message_the_agent_sends_on_its_own_is_rendered():
    """send_user_message emits AGENT_MESSAGE; the bridge formats and sends it."""
    bridge = _bridge()
    bridge._check_rate_limit = lambda _t: True
    event = AgentEvent(type=EventType.AGENT_MESSAGE, data={"message": AGENT_MD, "priority": "high"})
    assert await bridge.handle_event(event) is True
    (text, mode), = _sent(bridge._bot)
    assert mode == "HTML"
    assert text.startswith("🟠 <b>Message from Agent</b> (high)")
    assert AGENT_HTML in text
    assert "**" not in text and "\\" not in text


@pytest.mark.asyncio
async def test_data_values_in_an_event_show_as_written():
    bridge = _bridge()
    bridge._check_rate_limit = lambda _t: True
    event = AgentEvent(
        type=EventType.TASK_FAILED,
        data={"task_id": "t_1", "error": "bad *glob* in my_file_name"},
    )
    await bridge.handle_event(event)
    (text, mode), = _sent(bridge._bot)
    assert mode == "HTML"
    assert "<code>t_1</code>" in text
    assert "bad *glob* in my_file_name" in text


@pytest.mark.asyncio
async def test_the_morning_brief_is_rendered():
    bridge = _bridge()
    bridge._check_rate_limit = lambda _t: True
    bridge.event_filter.add(EventType.SLEEP_STATE_CHANGED.value)
    event = AgentEvent(type=EventType.SLEEP_STATE_CHANGED, data={
        "status": "awake", "result": "completed", "agent_id": "agent_001", "sessions_analyzed": 2,
        "morning_brief": {"summary": "We **shipped** it", "last_session": {"what_was_done": ["fixed `x`"]}},
    })
    await bridge.handle_event(event)
    (text, mode), = _sent(bridge._bot)
    assert mode == "HTML"
    assert "<b>agent_001 woke up</b>" in text
    assert "We <b>shipped</b> it" in text
    assert "• fixed <code>x</code>" in text


@pytest.mark.asyncio
async def test_send_markdown_falls_back_to_plain_on_a_parse_error():
    bridge = _bridge()
    bridge._bot.send_message = AsyncMock(side_effect=[BadRequest("Can't parse entities"), SimpleNamespace(message_id=2)])
    await bridge.send_markdown(CHAT, AGENT_MD)
    assert _sent(bridge._bot) == [(AGENT_HTML, "HTML"), (AGENT_PLAIN, None)]


@pytest.mark.asyncio
async def test_send_markdown_does_not_resend_on_an_outage():
    bridge = _bridge()
    bridge._bot.send_message = AsyncMock(side_effect=NetworkError("down"))
    with pytest.raises(NetworkError):
        await bridge.send_markdown(CHAT, AGENT_MD)
    assert bridge._bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_a_chain_response_is_rendered():
    bridge = _bridge()
    await bridge.send_chain_response(AGENT_MD)
    assert _sent(bridge._bot) == [(AGENT_HTML, "HTML")]


@pytest.mark.asyncio
async def test_a_scheduled_task_result_is_rendered():
    from dpc_client_core.managers.agent_manager import DpcAgentManager

    manager = DpcAgentManager.__new__(DpcAgentManager)
    manager._telegram_bridge = _bridge()
    await manager._deliver_telegram_result(CHAT, AGENT_MD)
    assert _sent(manager._telegram_bridge._bot) == [(AGENT_HTML, "HTML")]


# --- CC's reply relayed to the Telegram chat that asked ----------------------


@pytest.mark.asyncio
async def test_ccs_reply_relayed_to_telegram_is_rendered():
    from dpc_client_core.service import CoreService

    bridge = _bridge()
    monitor = SimpleNamespace(message_history=[], save_history=lambda: None,
                              get_last_msg_index=lambda: 0)
    monitor.add_message = lambda **kw: monitor.message_history.append(kw)
    monitor.message_history.append({"role": "user", "content": "@CC?", "sender_name": "Mike (Telegram)"})
    manager = SimpleNamespace(_agent_monitors={"agent_001": monitor}, _telegram_bridge=bridge,
                              get_session_state=lambda _c: {})
    provider = SimpleNamespace(get_manager=lambda _c: manager, _managers={"agent_001": manager})

    service = CoreService.__new__(CoreService)
    service.llm_manager = SimpleNamespace(providers={"dpc_agent": provider})
    service.local_api = SimpleNamespace(broadcast_event=AsyncMock())
    service.telegram_manager = object()
    service._cc_ark_chain_depths = {}
    service.get_cc_display_name = lambda: "CC"
    service._get_agent_display_name = lambda _a=None: "Ark"

    result = await service.send_cc_agent_response("agent_001", "**Yes**, merged")
    assert result["status"] == "success"
    assert _sent(bridge._bot) == [("<b>CC:</b> <b>Yes</b>, merged", "HTML")]


# --- the main bot, for a chat linked to an agent ------------------------------


@pytest.mark.asyncio
async def test_the_agents_reply_through_the_main_bot_is_html():
    from dpc_client_core.coordinators.telegram_coordinator import TelegramBridge

    agent_manager = SimpleNamespace(process_message=AsyncMock(return_value=AGENT_MD), _agent_monitors={})
    provider = SimpleNamespace(_ensure_manager=AsyncMock(), get_manager=lambda _a: agent_manager)
    service = SimpleNamespace(
        llm_manager=SimpleNamespace(providers={"dpc_agent": provider}),
        conversation_monitors={"agent-agent_001": SimpleNamespace(on_message=AsyncMock())},
        local_api=SimpleNamespace(broadcast_event=AsyncMock()),
        settings=MagicMock(),
        get_conversation_history=AsyncMock(return_value={"messages": []}),
        reset_cc_agent_chain=lambda _c: None,
    )
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
    update.message.chat_id = CHAT
    update.message.text = "hi"
    update.message.message_id = 7
    update.message.from_user.full_name = "Mike"
    update.message.date = datetime(2026, 9, 25, 12, 0, 0)
    await coordinator.handle_text_message(update, None)
    telegram.send_message.assert_awaited_once_with(CHAT, AGENT_HTML)


@pytest.mark.asyncio
async def test_the_main_bots_sender_resends_refused_html_as_plain_text():
    from dpc_client_core.managers.telegram_manager import TelegramBotManager

    bot = SimpleNamespace(send_message=AsyncMock(side_effect=[BadRequest("Can't parse entities"), None]))
    manager = TelegramBotManager.__new__(TelegramBotManager)
    manager.application = SimpleNamespace(bot=bot)
    await manager._send_text_message({"chat_id": CHAT, "text": AGENT_HTML, "parse_mode": "HTML"})
    assert _sent(bot) == [(AGENT_HTML, "HTML"), (AGENT_PLAIN, None)]


@pytest.mark.asyncio
async def test_the_main_bots_sender_splits_html_with_balanced_tags():
    from dpc_client_core.managers.telegram_manager import TelegramBotManager
    from dpc_client_core.telegram_format import _balanced, markdown_to_telegram_html

    bot = SimpleNamespace(send_message=AsyncMock())
    manager = TelegramBotManager.__new__(TelegramBotManager)
    manager.application = SimpleNamespace(bot=bot)
    html = markdown_to_telegram_html("```py\n" + "x = 1\n" * 2000 + "```")
    await manager._send_text_message({"chat_id": CHAT, "text": html, "parse_mode": "HTML"})
    texts = [t for t, _ in _sent(bot)]
    assert len(texts) > 1
    assert all(len(t) <= 4096 and _balanced(t) for t in texts)
