"""Turning a Telegram bot off must not cost its configuration.

The panel used to offer linked or unlinked and nothing between, and unlinking
clears the token, the chat ids, the event filter and the rate limits — so
pausing a bot for an hour meant typing the whole screen back in afterwards.
The flag the bridge is gated on already existed; what was missing was a verb
that writes only that flag, and a live bridge that hears about it.
"""

from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent.utils import AgentRegistry
from dpc_client_core.telegram_service import TelegramService

AGENT = "agent_ark_0001"

LINKED = {
    "agent_id": AGENT,
    "telegram_enabled": True,
    "telegram_bot_token": "123:abc",
    "telegram_allowed_chat_ids": ["41783586", "429727247"],
    "telegram_event_filter": ["task_started", "task_completed"],
    "telegram_max_events_per_minute": 20,
    "telegram_cooldown_seconds": 3.0,
    "telegram_transcription_enabled": True,
    "telegram_unified_conversation": True,
    "telegram_linked_at": "2026-09-06T17:42:24.492992+00:00",
}


def _registry(agent):
    reg = AgentRegistry.__new__(AgentRegistry)
    reg.written = {}

    def _update(agent_id, updates):
        reg.written = dict(updates)
        agent.update(updates)
        return agent

    reg.get_agent = lambda agent_id: agent if agent_id == AGENT else None
    reg.update_agent = _update
    return reg


def test_disabling_writes_the_flag_and_nothing_else():
    agent = dict(LINKED)
    reg = _registry(agent)

    reg.set_agent_telegram_enabled(AGENT, False)

    assert reg.written == {"telegram_enabled": False}
    for field, value in LINKED.items():
        if field == "telegram_enabled":
            continue
        assert agent[field] == value, f"{field} was touched by a pause"


def test_enabling_writes_the_flag_and_nothing_else():
    agent = dict(LINKED, telegram_enabled=False)
    reg = _registry(agent)

    reg.set_agent_telegram_enabled(AGENT, True)

    assert reg.written == {"telegram_enabled": True}
    assert agent["telegram_bot_token"] == "123:abc"


def test_enabling_what_was_never_configured_is_refused():
    """Otherwise the bridge starts and logs «enabled but missing bot_token»."""
    agent = {"agent_id": AGENT, "telegram_enabled": False}
    reg = _registry(agent)

    with pytest.raises(ValueError):
        reg.set_agent_telegram_enabled(AGENT, True)

    assert reg.written == {}


def test_an_unknown_agent_is_not_invented():
    reg = _registry(dict(LINKED))

    assert reg.set_agent_telegram_enabled("agent_nobody", False) is None
    assert reg.written == {}


def test_unlink_still_clears_everything():
    """The destructive verb stays destructive; this entry did not soften it."""
    agent = dict(LINKED)
    reg = _registry(agent)

    reg.unlink_agent_from_telegram(AGENT)

    assert agent["telegram_enabled"] is False
    assert agent["telegram_bot_token"] is None
    assert agent["telegram_allowed_chat_ids"] is None
    assert agent["telegram_linked_at"] is None


# --- the running bridge -----------------------------------------------------


def _service(agent, bridge):
    svc = TelegramService.__new__(TelegramService)
    manager = SimpleNamespace(_telegram_bridge=bridge)
    svc.restarted = []
    provider = SimpleNamespace(_managers={AGENT: manager})
    svc.llm_manager = SimpleNamespace(providers={"dpc_agent": provider})
    svc._manager = manager

    async def _restart(agent_id):
        svc.restarted.append(agent_id)

    svc._restart_agent_telegram_bridge = _restart
    return svc


class _Bridge:
    def __init__(self):
        self.stopped = False

    async def stop(self):
        self.stopped = True


@pytest.mark.asyncio
async def test_disabling_stops_the_bridge_that_is_already_running(monkeypatch):
    """The flag gates the build, so a live bridge keeps answering without this."""
    agent = dict(LINKED)
    reg = _registry(agent)
    monkeypatch.setattr(
        "dpc_client_core.dpc_agent.utils.AgentRegistry", lambda *a, **kw: reg
    )
    bridge = _Bridge()
    svc = _service(agent, bridge)

    result = await svc.set_agent_telegram_enabled(AGENT, False)

    assert result["status"] == "success"
    assert result["telegram_enabled"] is False
    assert bridge.stopped is True
    assert svc._manager._telegram_bridge is None
    assert svc.restarted == []


@pytest.mark.asyncio
async def test_enabling_starts_the_bridge_again(monkeypatch):
    agent = dict(LINKED, telegram_enabled=False)
    reg = _registry(agent)
    monkeypatch.setattr(
        "dpc_client_core.dpc_agent.utils.AgentRegistry", lambda *a, **kw: reg
    )
    svc = _service(agent, None)

    result = await svc.set_agent_telegram_enabled(AGENT, True)

    assert result["status"] == "success"
    assert result["telegram_enabled"] is True
    assert svc.restarted == [AGENT]


@pytest.mark.asyncio
async def test_editing_a_paused_link_does_not_resume_it(monkeypatch):
    """One button saves the edit; only Enable changes the on/off state."""
    agent = dict(LINKED, telegram_enabled=False)
    reg = _registry(agent)
    monkeypatch.setattr(
        "dpc_client_core.dpc_agent.utils.AgentRegistry", lambda *a, **kw: reg
    )
    bridge = _Bridge()
    svc = _service(agent, bridge)

    result = await svc.link_agent_telegram(
        agent_id=AGENT, bot_token="123:abc", chat_ids=["41783586"]
    )

    assert result["status"] == "success"
    assert result["telegram_enabled"] is False
    assert agent["telegram_enabled"] is False
    assert agent["telegram_allowed_chat_ids"] == ["41783586"]
    assert svc.restarted == []
    assert bridge.stopped is True


@pytest.mark.asyncio
async def test_a_first_link_does_enable(monkeypatch):
    agent = {"agent_id": AGENT, "telegram_enabled": False}
    reg = _registry(agent)
    monkeypatch.setattr(
        "dpc_client_core.dpc_agent.utils.AgentRegistry", lambda *a, **kw: reg
    )
    svc = _service(agent, None)

    result = await svc.link_agent_telegram(
        agent_id=AGENT, bot_token="123:abc", chat_ids=["41783586"]
    )

    assert result["telegram_enabled"] is True
    assert agent["telegram_enabled"] is True
    assert svc.restarted == [AGENT]


@pytest.mark.asyncio
async def test_the_refusal_reaches_the_caller_rather_than_raising(monkeypatch):
    agent = {"agent_id": AGENT, "telegram_enabled": False}
    reg = _registry(agent)
    monkeypatch.setattr(
        "dpc_client_core.dpc_agent.utils.AgentRegistry", lambda *a, **kw: reg
    )
    svc = _service(agent, None)

    result = await svc.set_agent_telegram_enabled(AGENT, True)

    assert result["status"] == "error"
    assert "link it first" in result["message"]
    assert svc.restarted == []
