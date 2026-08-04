"""An agent speaking in a group must not reach a private Telegram chat.

`send_user_message` emitted an event carrying only `agent_id`, and the bridge
forwards by agent — so a turn taken in a group chat was delivered to a DM with
nothing authorising it. Group chats have no Telegram binding; that mechanism
does not exist yet, so the only honest behaviour is to refuse and say so.
"""

from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent.tools.messaging import send_user_message
from dpc_client_core.managers.agent_telegram_bridge import AgentTelegramBridge
from dpc_client_core.dpc_agent.events import AgentEvent, EventType


def _ctx(conversation_id):
    return SimpleNamespace(
        agent_root=SimpleNamespace(name="agent_001"),
        current_task_id=conversation_id,
    )


@pytest.mark.asyncio
async def test_a_group_turn_is_refused_and_says_why(monkeypatch):
    emitted = []
    monkeypatch.setattr(
        "dpc_client_core.dpc_agent.tools.messaging.emit_agent_message",
        lambda **kw: emitted.append(kw),
    )

    out = await send_user_message(_ctx("group-b88b65076b85"), "привет группе")

    assert emitted == [], "the group turn still emitted a Telegram-bound event"
    assert "group chat" in out
    # The agent must learn what to do instead, or it will simply try again.
    assert "in the conversation" in out


@pytest.mark.asyncio
async def test_a_one_to_one_turn_still_goes_through(monkeypatch):
    emitted = []

    async def _emit(**kw):
        emitted.append(kw)

    monkeypatch.setattr(
        "dpc_client_core.dpc_agent.tools.messaging.emit_agent_message", _emit
    )

    out = await send_user_message(_ctx("agent_001"), "привет")

    assert len(emitted) == 1
    assert emitted[0]["conversation_id"] == "agent_001", (
        "the event must name its conversation, or the bridge cannot tell them apart"
    )
    assert "Message sent" in out


@pytest.mark.asyncio
async def test_the_bridge_refuses_a_group_event_on_its_own():
    """Defence in depth: events this tool does not own take the same route."""
    bridge = AgentTelegramBridge.__new__(AgentTelegramBridge)
    bridge._enabled = True
    bridge._bot = object()
    bridge.event_filter = {EventType.AGENT_MESSAGE.value}
    bridge._check_rate_limit = lambda _t: True
    bridge.allowed_chat_ids = ["429727247"]

    sent = []
    bridge._send_message = lambda cid, msg: sent.append((cid, msg))

    event = AgentEvent(
        type=EventType.AGENT_MESSAGE,
        data={"message": "из группы", "conversation_id": "group-b88b65076b85"},
    )

    assert await AgentTelegramBridge.handle_event(bridge, event) is False
    assert sent == [], "group content reached a private chat"
