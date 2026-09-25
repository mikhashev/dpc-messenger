"""`send_user_message` must not report "sent" when nothing can deliver it.

GROUP-TELEGRAM-BINDING-MISSING: the tool returned "Message sent to user via
Telegram" always, whether or not a channel was bound, so the agent believed it
had reached the human while nothing arrived. The group-chat path now refuses
with a reason. The 1:1 path still emits `agent_message` and reports success
even when no Telegram bridge and no other listener is attached to the emitter.

Both tests use the real emitter with no listener, the state of an agent whose
Telegram bridge was never started.
"""

from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent import events
from dpc_client_core.dpc_agent.events import AgentEventEmitter
from dpc_client_core.dpc_agent.tools.messaging import send_user_message


@pytest.fixture
def no_listener(monkeypatch):
    emitter = AgentEventEmitter(persist_events=False)
    monkeypatch.setattr(events, "_emitter", emitter)
    assert emitter._listeners == []
    return emitter


def _ctx(conversation_id):
    return SimpleNamespace(
        agent_root=SimpleNamespace(name="agent_001"),
        current_task_id=conversation_id,
    )


@pytest.mark.xfail(
    strict=True,
    reason="GROUP-TELEGRAM-BINDING-MISSING: 1:1 without a bridge reports sent",
)
@pytest.mark.asyncio
async def test_a_one_to_one_turn_with_no_bridge_is_not_reported_sent(no_listener):
    out = await send_user_message(_ctx("agent_001"), "hello")

    assert "Message sent" not in out, f"reported success with nobody listening: {out!r}"
    assert "Not sent" in out


@pytest.mark.asyncio
async def test_a_group_turn_with_no_bridge_is_refused_and_emits_nothing(no_listener):
    out = await send_user_message(_ctx("group-b88b65076b85"), "hello group")

    assert out.startswith("Not sent")
    assert "in the conversation" in out
    assert no_listener._event_log == [], "the group turn still emitted an event"
