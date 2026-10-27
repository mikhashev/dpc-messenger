"""Stop pressed in a chat that waits at a run gate takes its message out of line.

Until 2026-10-27 the Stop reached `AgentManager.interrupt`, which found no loop
for a chat that was still waiting, answered "no_active_loop", and the message
ran later although the person had asked to stop it. Now a Stop with no loop to
stop takes the chat's waiting entries off every gate, the agent's
(`other_chat`) and the room's (`room_busy`); each waiter's `hold` raises
`StoppedWhileWaiting`, its run never starts, and the answer is "stopped".
A 1:1 chat gets a `stopped` record, the way the quota closes the turn it never
ran; a group gets nothing posted.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.dpc_agent import run_gate as rg
from dpc_client_core.dpc_agent.run_gate import (
    RunGate, StoppedWhileWaiting, agent_turn_snapshot, get_run_gate, hold, stop_waiting,
)
from dpc_client_core.managers import agent_manager as am
from dpc_client_core.service import CoreService

CID = "agent_001"
GROUP = "group-b88b65076b85"


@pytest.fixture(autouse=True)
def _clean_gates():
    rg._RUN_GATES.clear()
    rg.set_turn_listener(None)
    yield
    rg._RUN_GATES.clear()
    rg.set_turn_listener(None)


async def _until(predicate, tries=400):
    for _ in range(tries):
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return False


async def _held(gate, cid="c-other", **extra):
    """A task holding `gate` until the returned event is set."""
    release = asyncio.Event()

    async def run():
        async with hold(gate, {"conversation_id": cid, **extra}):
            await release.wait()

    task = asyncio.create_task(run())
    assert await _until(lambda: gate.locked())
    return task, release


# --- the gate ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stopped_waiter_leaves_the_line_and_its_body_never_runs():
    gate = get_run_gate("agent_ark")
    snaps = []
    rg.set_turn_listener(snaps.append)
    holder, release = await _held(gate)
    ran = []

    async def waiter(cid):
        async with hold(gate, {"conversation_id": cid}, stoppable=True):
            ran.append(cid)

    stopped_one = asyncio.create_task(waiter("c-stop"))
    later = asyncio.create_task(waiter("c-later"))
    assert await _until(lambda: len(gate.waiters) == 2)

    assert stop_waiting("c-stop") == ["agent_ark"]
    with pytest.raises(StoppedWhileWaiting):
        await asyncio.wait_for(stopped_one, 2)

    assert [w["conversation_id"] for w in gate.waiters] == ["c-later"]
    assert [w["conversation_id"] for w in snaps[-1]["waiting"]] == ["c-later"]
    assert snaps[-1]["turn"]["conversation_id"] == "c-other"

    release.set()
    await asyncio.wait_for(asyncio.gather(holder, later), 2)
    assert ran == ["c-later"]
    assert not gate.locked() and gate.waiters == []
    assert agent_turn_snapshot("agent_ark")["waiting"] == []


@pytest.mark.asyncio
async def test_a_queued_task_and_another_chat_are_not_stopped():
    gate = get_run_gate("agent_ark")
    holder, release = await _held(gate)
    ran = []

    async def waiter(cid, stoppable):
        async with hold(gate, {"conversation_id": cid}, stoppable=stoppable):
            ran.append(cid)

    task_door = asyncio.create_task(waiter("c-stop", False))
    other_chat = asyncio.create_task(waiter("c-other-chat", True))
    assert await _until(lambda: len(gate.waiters) == 2)

    assert stop_waiting("c-stop") == []
    assert len(gate.waiters) == 2

    release.set()
    await asyncio.wait_for(asyncio.gather(holder, task_door, other_chat), 2)
    assert ran == ["c-stop", "c-other-chat"]


@pytest.mark.asyncio
async def test_a_stop_landing_after_the_lock_was_handed_over_still_stops_and_frees_it():
    gate = get_run_gate("agent_ark")
    await gate._lock.acquire()
    ran = []

    async def waiter():
        async with hold(gate, {"conversation_id": "c-stop"}, stoppable=True):
            ran.append(1)

    w = asyncio.create_task(waiter())
    assert await _until(lambda: len(gate.waiters) == 1)

    # One step: the lock passes to the waiter, which has not resumed yet.
    gate._lock.release()
    assert stop_waiting("c-stop") == ["agent_ark"]

    with pytest.raises(StoppedWhileWaiting):
        await asyncio.wait_for(w, 2)
    assert ran == [] and not gate.locked() and gate.waiters == []
    async with hold(gate, {"conversation_id": "c-next"}):
        assert gate.turn["conversation_id"] == "c-next"


@pytest.mark.asyncio
async def test_a_cancel_from_elsewhere_beside_the_stop_is_still_a_cancel():
    gate = get_run_gate("agent_ark")
    holder, release = await _held(gate)

    async def waiter():
        async with hold(gate, {"conversation_id": "c-stop"}, stoppable=True):
            pass

    w = asyncio.create_task(waiter())
    assert await _until(lambda: len(gate.waiters) == 1)
    stop_waiting("c-stop")
    w.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(w, 2)
    assert gate.waiters == []
    release.set()
    await holder
    assert not gate.locked()


@pytest.mark.asyncio
async def test_a_waiter_at_the_room_is_stopped_and_named_by_its_agent():
    room = RunGate(room=GROUP)
    get_run_gate("agent_warren")
    holder, release = await _held(room, GROUP, agent_id="agent_johnny")

    async def waiter():
        async with hold(room, {"conversation_id": GROUP, "agent_id": "agent_warren"},
                        stoppable=True):
            pass

    w = asyncio.create_task(waiter())
    assert await _until(lambda: len(room.waiters) == 1)
    assert agent_turn_snapshot("agent_warren")["waiting"][0]["wait_reason"] == "room_busy"

    assert stop_waiting(GROUP) == ["agent_warren"]
    with pytest.raises(StoppedWhileWaiting):
        await asyncio.wait_for(w, 2)
    assert agent_turn_snapshot("agent_warren")["waiting"] == []
    release.set()
    await holder


# --- the agent's door -----------------------------------------------------------------


def _monitor(root):
    m = ConversationMonitor(
        conversation_id=CID,
        participants=[{"node_id": "dpc-node-me", "name": "User", "context": "local"},
                      {"node_id": CID, "name": "Ark", "context": "agent"}],
        llm_manager=None,
    )
    m._get_history_path = lambda: root / CID / "history.json"
    return m


def _manager(monitor, gate):
    manager = am.DpcAgentManager.__new__(am.DpcAgentManager)
    manager.agent_id = CID
    manager._run_gate = gate
    manager._agent_display_name = "Ark"
    manager.service = SimpleNamespace(p2p_manager=SimpleNamespace(node_id="dpc-node-me"))
    manager._get_or_create_agent_monitor = lambda conv: monitor
    manager._process_message_guarded_calls = 0

    async def guarded(*args, **kwargs):
        manager._process_message_guarded_calls += 1
        return "ran"

    # The stopped branch binds the call's arguments to the real signature.
    guarded.__signature__ = inspect.signature(
        am.DpcAgentManager._process_message_guarded.__get__(manager))
    manager._process_message_guarded = guarded
    return manager


def _on_disk(tmp_path):
    return json.loads((tmp_path / CID / "history.json").read_text(encoding="utf-8"))["messages"]


@pytest.mark.asyncio
@pytest.mark.parametrize("skip_history", [True, False], ids=["caller-saved", "manager-saves"])
async def test_a_stopped_message_in_a_1to1_chat_is_closed_with_a_stopped_record(
    tmp_path, signing_identity, skip_history,
):
    monitor = _monitor(tmp_path)
    if skip_history:
        monitor.add_message(role="user", content="hello", sender_node_id="dpc-node-me",
                            sender_name="Mike")
    gate = get_run_gate(CID)
    manager = _manager(monitor, gate)
    holder, release = await _held(gate, GROUP)

    w = asyncio.create_task(manager.process_message(
        "hello", CID, sender_name="Mike", _skip_history=skip_history))
    assert await _until(lambda: len(gate.waiters) == 1)
    assert stop_waiting(CID) == [CID]

    text = await asyncio.wait_for(w, 2)
    assert text == "⚠️ Stopped by user while waiting for its turn."
    assert manager._process_message_guarded_calls == 0
    user, rec = _on_disk(tmp_path)
    assert user["role"] == "user" and user["content"] == "hello" and user["sender_name"] == "Mike"
    assert rec["role"] == "assistant" and rec["content"] == text
    assert rec["turn_end"] == "stopped" and rec["sender_name"] == "Ark"
    assert "thinking" not in rec and "tool_calls" not in rec
    release.set()
    await holder


@pytest.mark.asyncio
async def test_a_stopped_message_in_a_group_saves_and_posts_nothing(tmp_path):
    monitor = MagicMock()
    gate = get_run_gate(CID)
    manager = _manager(monitor, gate)
    holder, release = await _held(gate, "agent_001")

    w = asyncio.create_task(manager.process_message("[Mike]: hi", GROUP, _skip_history=True))
    assert await _until(lambda: len(gate.waiters) == 1)
    stop_waiting(GROUP)

    assert await asyncio.wait_for(w, 2) == ""
    assert manager._process_message_guarded_calls == 0
    assert not monitor.add_message.called and not monitor.save_history.called
    release.set()
    await holder


# --- the Stop command and the room's door ------------------------------------------------


def _idle_manager(agent_id):
    m = am.DpcAgentManager.__new__(am.DpcAgentManager)
    m.agent_id = agent_id
    m._interrupt_events = {}
    return m


def _service(*agent_ids):
    provider = SimpleNamespace(_managers={a: _idle_manager(a) for a in agent_ids}, _manager=None,
                               get_manager=lambda aid: None)
    svc = CoreService.__new__(CoreService)
    svc.llm_manager = SimpleNamespace(providers={"dpc_agent": provider})
    svc._group_agent_locks = {}
    svc._conversation_display_name = lambda cid: cid
    return svc


@pytest.mark.asyncio
@pytest.mark.parametrize("force", [False, True], ids=["stop", "kill"])
async def test_the_stop_command_answers_stopped_for_a_chat_that_waits_for_its_agent(force):
    gate = get_run_gate(CID)
    holder, release = await _held(gate, GROUP)

    async def waiter():
        async with hold(gate, {"conversation_id": CID}, stoppable=True):
            pass

    w = asyncio.create_task(waiter())
    assert await _until(lambda: len(gate.waiters) == 1)

    res = await _service(CID).interrupt_agent(agent_id=CID, conversation_id=CID, force=force)

    assert res == {"status": "stopped", "agent_id": CID, "agent_ids": [CID]}
    with pytest.raises(StoppedWhileWaiting):
        await asyncio.wait_for(w, 2)
    assert await _service(CID).interrupt_agent(agent_id=CID, conversation_id=CID, force=force) \
        == {"status": "no_active_loop"}
    release.set()
    await holder


@pytest.mark.asyncio
async def test_a_stop_in_a_group_lets_go_of_an_agent_waiting_for_the_room():
    svc = _service("agent_johnny", "agent_warren")
    svc._get_default_agent_id = lambda: "agent_johnny"
    svc._invoke_agent_in_group = AsyncMock()
    room = svc._group_agent_locks[GROUP] = RunGate(room=GROUP)
    holder, release = await _held(room, GROUP, agent_id="agent_johnny")

    w = asyncio.create_task(svc._invoke_agent_in_group_serialized(
        GROUP, "hi", "Mike", agent_id="agent_warren"))
    assert await _until(lambda: len(room.waiters) == 1)

    res = await svc.interrupt_agent(agent_id="agent_warren", conversation_id=GROUP)

    assert res == {"status": "stopped", "agent_id": "agent_warren", "agent_ids": ["agent_warren"]}
    assert await asyncio.wait_for(w, 2) is None
    assert not svc._invoke_agent_in_group.called
    assert agent_turn_snapshot("agent_warren")["waiting"] == []
    release.set()
    await holder
    assert not room.locked()
