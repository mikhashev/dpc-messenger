"""Stop in a group lands on the agent that runs there, whoever the UI named.

2026-09-23: Johnny ran in a group, the live block's Stop named the first agent
in the list (Ark), Ark's manager had no loop, and seven presses all answered
"no_active_loop".
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from dpc_client_core.managers.agent_manager import DpcAgentManager
from dpc_client_core.service import CoreService

GROUP = "group-b88b65076b85"


def _manager(agent_id: str, running_in: tuple[str, ...] = ()) -> DpcAgentManager:
    m = DpcAgentManager.__new__(DpcAgentManager)
    m.agent_id = agent_id
    m._interrupt_events = {cid: asyncio.Event() for cid in running_in}
    return m


def _service(*managers: DpcAgentManager) -> CoreService:
    provider = SimpleNamespace(
        _managers={m.agent_id: m for m in managers},
        _manager=None,
        get_manager=lambda aid: (_ for _ in ()).throw(AssertionError("must not create managers")),
    )
    svc = CoreService.__new__(CoreService)
    svc.llm_manager = SimpleNamespace(providers={"dpc_agent": provider})
    return svc


@pytest.mark.asyncio
async def test_a_stop_naming_an_idle_agent_stops_the_one_running_in_the_group():
    ark = _manager("agent_001")
    johnny = _manager("agent_johnny_f309700d", running_in=(GROUP,))

    res = await _service(ark, johnny).interrupt_agent(agent_id="agent_001", conversation_id=GROUP)

    assert res["status"] == "stopped"
    assert res["agent_id"] == "agent_johnny_f309700d"
    assert johnny._interrupt_events[GROUP].is_set()


@pytest.mark.asyncio
async def test_a_stop_with_no_agent_named_stops_the_one_running_in_the_group():
    ark = _manager("agent_001")
    johnny = _manager("agent_johnny_f309700d", running_in=(GROUP,))

    res = await _service(ark, johnny).interrupt_agent(agent_id="", conversation_id=GROUP)

    assert res == {"status": "stopped", "agent_id": "agent_johnny_f309700d",
                   "agent_ids": ["agent_johnny_f309700d"]}
    assert johnny._interrupt_events[GROUP].is_set()


@pytest.mark.asyncio
async def test_a_named_agent_that_runs_here_is_the_only_one_stopped():
    ark = _manager("agent_001", running_in=(GROUP,))
    johnny = _manager("agent_johnny_f309700d", running_in=(GROUP,))

    res = await _service(ark, johnny).interrupt_agent(agent_id="agent_001", conversation_id=GROUP)

    assert res["agent_id"] == "agent_001"
    assert ark._interrupt_events[GROUP].is_set()
    assert not johnny._interrupt_events[GROUP].is_set()


@pytest.mark.asyncio
async def test_a_loop_in_another_conversation_is_not_stopped():
    johnny = _manager("agent_johnny_f309700d", running_in=("group-other",))

    res = await _service(_manager("agent_001"), johnny).interrupt_agent(
        agent_id="agent_001", conversation_id=GROUP)

    assert res == {"status": "no_active_loop"}
    assert not johnny._interrupt_events["group-other"].is_set()


@pytest.mark.asyncio
async def test_no_agent_running_answers_no_active_loop():
    res = await _service(_manager("agent_001"), _manager("agent_johnny_f309700d")).interrupt_agent(
        agent_id="", conversation_id=GROUP)

    assert res == {"status": "no_active_loop"}
