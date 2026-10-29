"""The pause that gathers Telegram messages sent in a row is the agent's own.

It was a module constant of 3 s, shorter than the gaps a person leaves typing
one sentence in pieces: the first piece closed the burst and reached the agent
alone, and the rest came as a second request. The pause is now set per agent in
Edit Telegram Configuration (`burst_quiet_seconds` on the command,
`telegram_burst_quiet_seconds` on the agent's row), 20 s when the row has no
value, 0 for no pause, at most 300. The ceiling of a burst is three pauses.

Nothing here touches the real ~/.dpc: `Path.home()` is a temporary directory.
The timing of the pause itself is in
test_a_burst_of_telegram_messages_is_one_agent_run.py.
"""

import asyncio
import json
import pathlib
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.agent_service import AgentService
from dpc_client_core.dpc_agent.utils import AgentRegistry
from dpc_client_core.managers import agent_telegram_bridge
from dpc_client_core.managers.agent_manager import DpcAgentManager
from dpc_client_core.managers.agent_telegram_bridge import AgentTelegramBridge
from dpc_client_core.service import CoreService
from dpc_client_core.telegram_service import TelegramService

AGENT = "agent_burst_pause_0001"
CHAT = "41783586"


@pytest.fixture(autouse=True)
def _telegram_bursts_start_at_once():
    """Overrides the suite's fixture of this name, which zeroes the default
    pause: here the default is the thing under test."""


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    AgentRegistry().register_agent(AGENT, "Burst test agent")
    return tmp_path


def _row():
    return AgentRegistry().get_agent(AGENT)


def _link(**kw):
    return AgentRegistry().link_agent_to_telegram(
        AGENT, bot_token="123:abc", chat_ids=[CHAT], **kw)


# --- the registry ------------------------------------------------------------


def test_a_pause_set_on_the_link_is_stored_on_the_agents_row(home):
    _link(burst_quiet_seconds=45)

    assert _row()["telegram_burst_quiet_seconds"] == 45
    stored = json.loads((home / ".dpc" / "agents" / "_registry.json").read_text("utf-8"))
    assert stored["agents"][AGENT]["telegram_burst_quiet_seconds"] == 45


def test_no_pause_is_accepted_and_kept_as_zero_not_as_the_default(home):
    _link(burst_quiet_seconds=0)

    assert _row()["telegram_burst_quiet_seconds"] == 0


@pytest.mark.parametrize("edge", [0, 300, 0.5, 299.5])
def test_the_ends_of_the_range_are_allowed(home, edge):
    _link(burst_quiet_seconds=edge)

    assert _row()["telegram_burst_quiet_seconds"] == edge


@pytest.mark.parametrize("bad", [301, 300.5, -1, -0.1, "abc", "45", True, [], float("nan")])
def test_a_pause_outside_zero_to_three_hundred_or_not_a_number_is_refused(home, bad):
    with pytest.raises(ValueError, match="burst_quiet_seconds"):
        _link(burst_quiet_seconds=bad)

    assert "telegram_burst_quiet_seconds" not in _row()  # nothing was written


def test_unlinking_clears_the_pause_as_it_clears_the_cooldown(home):
    _link(burst_quiet_seconds=45)

    AgentRegistry().unlink_agent_from_telegram(AGENT)

    row = _row()
    assert row["telegram_burst_quiet_seconds"] is None
    assert row["telegram_cooldown_seconds"] is None


def test_the_agents_list_the_screen_reads_carries_the_pause(home):
    _link(burst_quiet_seconds=45)
    service = AgentService.__new__(AgentService)

    result = asyncio.run(service.list_agents())

    (row,) = [a for a in result["agents"] if a["agent_id"] == AGENT]
    assert row["telegram_burst_quiet_seconds"] == 45


# --- the command: service -> telegram service -> registry --------------------


def test_the_core_service_command_hands_the_pause_to_the_telegram_service():
    telegram = SimpleNamespace(link_agent_telegram=AsyncMock(return_value={"status": "success"}))
    core = SimpleNamespace(telegram_service=telegram)

    asyncio.run(CoreService.link_agent_telegram(
        core, AGENT, "123:abc", [CHAT], burst_quiet_seconds=45))

    args = telegram.link_agent_telegram.await_args.args
    assert args[0] == AGENT and args[-1] == 45


class _OldBridge:
    async def stop(self):
        pass


def _telegram_service_with_a_live_agent():
    """TelegramService over a manager that runs the real `_start_telegram_bridge`."""
    svc = TelegramService.__new__(TelegramService)
    svc._telegram_link_locks = {}
    manager = SimpleNamespace(
        agent_id=AGENT,
        service=SimpleNamespace(settings=None, telegram_manager=None),
        process_message=AsyncMock(),
        _telegram_bridge=_OldBridge(),
    )
    manager._start_telegram_bridge = MethodType(DpcAgentManager._start_telegram_bridge, manager)
    svc.llm_manager = SimpleNamespace(
        providers={"dpc_agent": SimpleNamespace(_managers={AGENT: manager})})
    return svc, manager


def test_a_command_refusal_reaches_the_caller_as_an_error_and_writes_nothing(home):
    svc, _ = _telegram_service_with_a_live_agent()

    result = asyncio.run(svc.link_agent_telegram(
        AGENT, "123:abc", [CHAT], burst_quiet_seconds=301))

    assert result["status"] == "error"
    assert "burst_quiet_seconds" in result["message"]
    assert not _row().get("telegram_bot_token")


def test_update_configuration_builds_the_bridge_with_the_new_pause_at_once(home, monkeypatch):
    monkeypatch.setattr(AgentTelegramBridge, "start", AsyncMock(return_value=True))
    svc, manager = _telegram_service_with_a_live_agent()

    first = asyncio.run(svc.link_agent_telegram(
        AGENT, "123:abc", [CHAT], burst_quiet_seconds=45))
    assert first["status"] == "success"
    assert _row()["telegram_burst_quiet_seconds"] == 45
    assert manager._telegram_bridge._burst_quiet_seconds == 45
    assert manager._telegram_bridge._burst_ceiling_seconds == 135

    # The same button again with another value: no restart of the service.
    second = asyncio.run(svc.link_agent_telegram(
        AGENT, "123:abc", [CHAT], burst_quiet_seconds=0))
    assert second["status"] == "success"
    assert manager._telegram_bridge._burst_quiet_seconds == 0
    assert manager._telegram_bridge._burst_ceiling_seconds == 0


def test_a_link_with_no_value_builds_a_bridge_with_the_default_pause(home, monkeypatch):
    monkeypatch.setattr(AgentTelegramBridge, "start", AsyncMock(return_value=True))
    svc, manager = _telegram_service_with_a_live_agent()

    asyncio.run(svc.link_agent_telegram(AGENT, "123:abc", [CHAT]))

    assert _row()["telegram_burst_quiet_seconds"] is None
    assert manager._telegram_bridge._burst_quiet_seconds == 20
    assert manager._telegram_bridge._burst_ceiling_seconds == 60


def test_an_agent_whose_row_never_had_the_field_gets_the_default_pause(home, monkeypatch):
    """Existing agents: no field on the row, and no migration writes one."""
    monkeypatch.setattr(AgentTelegramBridge, "start", AsyncMock(return_value=True))
    _link()  # then take the field off the row, as an agent linked before it
    registry = AgentRegistry()
    del registry._registry["agents"][AGENT]["telegram_burst_quiet_seconds"]
    registry._save_registry()
    _, manager = _telegram_service_with_a_live_agent()

    asyncio.run(manager._start_telegram_bridge())

    assert "telegram_burst_quiet_seconds" not in _row()
    assert manager._telegram_bridge._burst_quiet_seconds == 20


# --- the bridge --------------------------------------------------------------


def test_the_default_pause_is_twenty_seconds_and_the_ceiling_three_pauses():
    assert agent_telegram_bridge.BURST_QUIET_SECONDS == 20.0
    bridge = AgentTelegramBridge(bot_token="t", allowed_chat_ids=[CHAT])
    assert bridge._burst_quiet_seconds == 20.0
    assert bridge._burst_ceiling_seconds == 60.0


def test_a_bridge_keeps_its_own_pause_whatever_the_module_default_is(monkeypatch):
    monkeypatch.setattr(agent_telegram_bridge, "BURST_QUIET_SECONDS", 7.0)
    bridge = AgentTelegramBridge(bot_token="t", allowed_chat_ids=[CHAT], burst_quiet_seconds=45)
    assert bridge._burst_quiet_seconds == 45
    assert bridge._burst_ceiling_seconds == 135
