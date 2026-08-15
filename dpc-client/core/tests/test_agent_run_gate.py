"""One run at a time per agent, whichever door it comes through.

The damage this prevents is not abstract: the agent publishes usage, trace and
cap info on itself, and the manager reads them *after* the run returns. A second
run finishing in that window writes its thinking and tool calls into the first
one's history. Both doors — a message from any surface, and a queued task — take
the same gate.
"""

import asyncio
import types

import pytest

from dpc_client_core.managers import agent_manager as am
from dpc_client_core.managers.agent_manager import get_run_gate


@pytest.fixture(autouse=True)
def _clear_gates():
    am._RUN_GATES.clear()
    yield
    am._RUN_GATES.clear()


def test_the_gate_belongs_to_the_agent_not_to_the_object_holding_it():
    """Two managers or two agent objects for one agent must not get two gates —
    that is what disqualified putting the lock on the instance."""
    assert get_run_gate("agent_001") is get_run_gate("agent_001")
    assert get_run_gate("agent_001") is not get_run_gate("agent_002")
    assert get_run_gate(None) is get_run_gate(None)


class FakeManager:
    """A manager reduced to the gate and the door, so the door can be observed."""

    def __init__(self, agent_id):
        self._run_gate = get_run_gate(agent_id)
        self.entries = []
        self.overlapped = False
        self._inside = 0

    process_message = am.DpcAgentManager.process_message

    async def _process_message_guarded(self, tag):
        self._inside += 1
        if self._inside > 1:
            self.overlapped = True
        self.entries.append(tag)
        await asyncio.sleep(0.02)
        self._inside -= 1
        return tag


@pytest.mark.asyncio
async def test_two_messages_to_one_agent_do_not_run_at_once():
    manager = FakeManager("agent_001")

    results = await asyncio.gather(
        manager.process_message("first"),
        manager.process_message("second"),
    )

    assert results == ["first", "second"]
    assert manager.overlapped is False


@pytest.mark.asyncio
async def test_two_different_agents_still_run_at_once():
    """The gate is per agent. Serialising the whole fleet would be a different,
    much more expensive decision than the one taken."""
    slow = FakeManager("agent_001")
    other = FakeManager("agent_002")

    order = []

    async def run(manager, tag):
        await manager.process_message(tag)
        order.append(tag)

    await asyncio.gather(run(slow, "a"), run(other, "b"))

    assert set(order) == {"a", "b"}
    assert slow.overlapped is False and other.overlapped is False


@pytest.mark.asyncio
async def test_a_queued_task_waits_for_a_live_chat_and_the_reverse():
    """The second door. A check_back replies into the conversation it came from,
    so a task landing mid-chat is the corruption, not a hypothetical."""
    from dpc_client_core.dpc_agent.agent import DpcAgent

    manager = FakeManager("agent_001")

    agent = DpcAgent.__new__(DpcAgent)
    agent._run_gate = get_run_gate("agent_001")

    # Both doors run the same body, so an overlap is visible whichever pair of
    # entrants produced it.
    inside = {"n": 0, "overlapped": False}

    async def body(tag):
        inside["n"] += 1
        if inside["n"] > 1:
            inside["overlapped"] = True
        await asyncio.sleep(0.02)
        inside["n"] -= 1
        return tag

    manager._process_message_guarded = body
    agent._execute_task_guarded = lambda task: body("task done")

    results = await asyncio.gather(
        DpcAgent._execute_task(agent, types.SimpleNamespace(task_type="chat", data={}, id="t1")),
        manager.process_message("chat"),
    )

    assert results == ["task done", "chat"]
    assert inside["overlapped"] is False


def test_the_second_agent_built_for_a_provider_gets_the_same_gate(monkeypatch, tmp_path):
    """The sibling instance is the whole reason the gate is not on the agent —
    if this wiring is dropped, the two objects over one agent_root diverge again."""
    captured = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(am, "DpcAgent", FakeAgent)

    manager = am.DpcAgentManager.__new__(am.DpcAgentManager)
    manager.agent_id = "agent_001"
    manager._run_gate = get_run_gate("agent_001")
    manager.config = {}
    manager.agent_root = tmp_path
    manager.firewall = None
    manager.service = types.SimpleNamespace(llm_manager=object())
    manager._agent = None
    manager._agents = {}

    manager._get_or_create_agent_for_provider("deepseek_flash")

    assert captured["run_gate"] is manager._run_gate


@pytest.mark.asyncio
async def test_the_gate_is_released_when_a_run_raises():
    manager = FakeManager("agent_001")

    async def boom(tag):
        raise RuntimeError("run failed")

    manager._process_message_guarded = boom
    with pytest.raises(RuntimeError):
        await manager.process_message("first")

    assert not manager._run_gate.locked()
