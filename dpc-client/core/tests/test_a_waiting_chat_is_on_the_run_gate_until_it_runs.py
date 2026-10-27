"""A chat waiting for an agent is listed at the run gate, and the row says why.

ADR-046 point 8: the agent's row says which chat its turn is in and which chats
wait for it. A bare asyncio.Lock does not show its queue, so a chat waiting
behind another chat looked like a chat nobody answered. The `RunGate` keeps an
entry per waiter from before the acquire to after it, in arrival order, and
removes it in a `finally` — a waiter that is cancelled or times out is gone too.
"""

import asyncio
import types

import pytest

from dpc_client_core.dpc_agent import run_gate as rg
from dpc_client_core.dpc_agent.run_gate import RunGate, agent_turn_snapshot, get_run_gate, hold


@pytest.fixture(autouse=True)
def _clean_gates():
    rg._RUN_GATES.clear()
    rg.set_turn_listener(None)
    yield
    rg._RUN_GATES.clear()
    rg.set_turn_listener(None)


async def _until(predicate, tries=200):
    for _ in range(tries):
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return False


def _service_named(names):
    return types.SimpleNamespace(_conversation_display_name=lambda cid: names.get(cid, cid))


class TestTheGateKeepsItsWaiters:
    @pytest.mark.asyncio
    async def test_waiters_are_listed_in_arrival_order_and_run_in_it(self):
        gate = get_run_gate("agent_ark")
        release = asyncio.Event()
        ran = []

        async def run(cid):
            async with hold(gate, {"conversation_id": cid, "conversation_title": cid.upper()}):
                ran.append(cid)
                if cid == "c1":
                    await release.wait()

        tasks = [asyncio.create_task(run("c1"))]
        await _until(lambda: gate.turn is not None)
        for cid in ("c2", "c3", "c4"):
            tasks.append(asyncio.create_task(run(cid)))
            await asyncio.sleep(0)
        await _until(lambda: len(gate.waiters) == 3)

        snap = agent_turn_snapshot("agent_ark")
        assert snap["turn"]["conversation_id"] == "c1"
        assert [w["conversation_id"] for w in snap["waiting"]] == ["c2", "c3", "c4"]
        assert all(w["wait_reason"] == "other_chat" for w in snap["waiting"])
        assert snap["waiting"][0]["held_by"] == {"conversation_id": "c1", "wait_reason": None}
        assert snap["waiting"][0]["conversation_title"] == "C2"
        assert snap["waiting"][0]["waiting_since"]

        release.set()
        await asyncio.gather(*tasks)
        assert ran == ["c1", "c2", "c3", "c4"]
        assert gate.waiters == []
        assert agent_turn_snapshot("agent_ark")["turn"] is None

    @pytest.mark.asyncio
    async def test_a_cancelled_waiter_leaves_the_list(self):
        gate = get_run_gate("agent_ark")
        release = asyncio.Event()

        async def holder():
            async with hold(gate, {"conversation_id": "c1"}):
                await release.wait()

        async def waiter():
            async with hold(gate, {"conversation_id": "c2"}):
                pass

        h = asyncio.create_task(holder())
        await _until(lambda: gate.turn is not None)
        w = asyncio.create_task(waiter())
        await _until(lambda: len(gate.waiters) == 1)

        w.cancel()
        with pytest.raises(asyncio.CancelledError):
            await w
        assert gate.waiters == []
        assert agent_turn_snapshot("agent_ark")["waiting"] == []

        release.set()
        await h
        assert not gate.locked()

    @pytest.mark.asyncio
    async def test_a_waiter_that_times_out_leaves_the_list(self):
        gate = get_run_gate("agent_ark")
        release = asyncio.Event()

        async def holder():
            async with hold(gate, {"conversation_id": "c1"}):
                await release.wait()

        async def waiter():
            async with hold(gate, {"conversation_id": "c2"}):
                pass

        h = asyncio.create_task(holder())
        await _until(lambda: gate.turn is not None)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(waiter(), timeout=0.05)
        assert gate.waiters == []

        release.set()
        await h

    @pytest.mark.asyncio
    async def test_the_gate_is_free_and_idle_after_a_run_that_raised(self):
        gate = get_run_gate("agent_ark")
        with pytest.raises(RuntimeError):
            async with hold(gate, {"conversation_id": "c1"}):
                raise RuntimeError("run failed")
        assert not gate.locked()
        assert gate.turn is None

    @pytest.mark.asyncio
    async def test_a_plain_lock_still_serialises_and_registers_nothing(self):
        lock = asyncio.Lock()
        async with hold(lock, {"conversation_id": "c1"}):
            assert lock.locked()
        assert not lock.locked()


class TestBothDoorsRegisterTheirChat:
    @pytest.mark.asyncio
    async def test_door_one_registers_the_message_s_conversation(self):
        from dpc_client_core.managers import agent_manager as am

        release = asyncio.Event()
        manager = types.SimpleNamespace(
            _run_gate=am.get_run_gate("agent_ark"),
            service=_service_named({"c1": "Mike (1:1)", "group-x": "DPC project"}),
        )

        async def body(**kwargs):
            await release.wait()
            return kwargs["conversation_id"]

        manager._process_message_guarded = body
        first = asyncio.create_task(am.DpcAgentManager.process_message(
            manager, message="hi", conversation_id="c1"))
        await _until(lambda: manager._run_gate.turn is not None)
        second = asyncio.create_task(am.DpcAgentManager.process_message(
            manager, message="hi", conversation_id="group-x",
            chat_context={"chat_name": "DPC room"}))
        await _until(lambda: len(manager._run_gate.waiters) == 1)

        snap = agent_turn_snapshot("agent_ark")
        assert snap["turn"]["conversation_id"] == "c1"
        assert snap["turn"]["conversation_title"] == "Mike (1:1)"
        assert snap["waiting"][0]["conversation_id"] == "group-x"
        assert snap["waiting"][0]["conversation_title"] == "DPC room", "the caller's chat_name first"

        release.set()
        assert await asyncio.gather(first, second) == ["c1", "group-x"]

    @pytest.mark.asyncio
    async def test_door_two_registers_the_chat_the_task_answers_in(self):
        from dpc_client_core.dpc_agent.agent import DpcAgent

        release = asyncio.Event()
        gate = get_run_gate("agent_ark")
        agent = DpcAgent.__new__(DpcAgent)
        agent._run_gate = gate
        agent._service = _service_named({"group-x": "DPC project"})

        async def body(task):
            await release.wait()
            return task.id

        agent._execute_task_guarded = body

        async def holder():
            async with hold(gate, {"conversation_id": "c1"}):
                await release.wait()

        h = asyncio.create_task(holder())
        await _until(lambda: gate.turn is not None)
        replying = asyncio.create_task(DpcAgent._execute_task(
            agent, types.SimpleNamespace(data={"_reply_conversation_id": "group-x"}, id="t1")))
        bare = asyncio.create_task(DpcAgent._execute_task(
            agent, types.SimpleNamespace(data={}, id="t2")))
        await _until(lambda: len(gate.waiters) == 2)

        waiting = agent_turn_snapshot("agent_ark")["waiting"]
        assert [w["conversation_id"] for w in waiting] == ["group-x", "t2"]
        assert waiting[0]["conversation_title"] == "DPC project"

        release.set()
        await asyncio.gather(h, replying, bare)


class TestTheRoomLockGivesRoomBusy:
    def _service(self):
        from dpc_client_core.service import CoreService

        service = CoreService.__new__(CoreService)
        service._group_agent_locks = {}
        service.peer_metadata = {}
        service.group_manager = types.SimpleNamespace(
            get_group=lambda gid: types.SimpleNamespace(name="DPC project"))
        service._get_default_agent_id = lambda: "agent_ark"
        return service

    @pytest.mark.asyncio
    async def test_a_second_agent_in_the_room_waits_room_busy_held_by_the_first(self):
        service = self._service()
        release = asyncio.Event()
        seen = []
        rg.set_turn_listener(seen.append)

        async def invoke(group_id, text, sender_name, agent_id, trigger_message_id, mention_depth=0):
            async with hold(get_run_gate(agent_id), {"conversation_id": group_id}):
                await release.wait()

        service._invoke_agent_in_group = invoke
        group = "group-b88b"
        ark = asyncio.create_task(service._invoke_agent_in_group_serialized(
            group, "@Ark", "Mike", "agent_ark"))
        await _until(lambda: get_run_gate("agent_ark").turn is not None)
        johnny = asyncio.create_task(service._invoke_agent_in_group_serialized(
            group, "@Johnny", "Mike", "agent_johnny"))
        await _until(lambda: agent_turn_snapshot("agent_johnny")["waiting"])

        item = agent_turn_snapshot("agent_johnny")["waiting"][0]
        assert item["conversation_id"] == group
        assert item["conversation_title"] == "DPC project"
        assert item["wait_reason"] == "room_busy"
        assert item["held_by"] == {"conversation_id": group, "wait_reason": None,
                                   "agent_id": "agent_ark"}

        # One level down: the holder's turn waits for an approval, and the
        # waiting agent's row is told without asking.
        seen.clear()
        get_run_gate("agent_ark").update_turn(wait_reason="approval")
        mine = [s for s in seen if s["agent_id"] == "agent_johnny"]
        assert mine and mine[-1]["waiting"][0]["held_by"]["wait_reason"] == "approval"

        release.set()
        await asyncio.gather(ark, johnny)
        assert agent_turn_snapshot("agent_johnny")["waiting"] == []


class TestTheEventIsAFullSnapshot:
    @pytest.mark.asyncio
    async def test_every_change_sends_the_whole_row_and_idle_is_turn_null(self):
        seen = []
        rg.set_turn_listener(seen.append)
        gate = get_run_gate("agent_ark")
        release = asyncio.Event()

        async def run(cid):
            async with hold(gate, {"conversation_id": cid, "conversation_title": cid}):
                await release.wait()

        a = asyncio.create_task(run("c1"))
        await _until(lambda: gate.turn is not None)
        b = asyncio.create_task(run("c2"))
        await _until(lambda: len(gate.waiters) == 1)
        release.set()
        await asyncio.gather(a, b)

        assert seen, "no snapshot was sent"
        for snap in seen:
            assert set(snap) == {"agent_id", "turn", "waiting", "ts"}
            assert snap["agent_id"] == "agent_ark"
            if snap["turn"] is not None:
                assert set(snap["turn"]) == {
                    "conversation_id", "conversation_title", "started_at", "round",
                    "provider_alias", "tool", "wait_reason"}
        assert any(s["waiting"] and s["waiting"][0]["conversation_id"] == "c2" for s in seen)
        assert seen[-1]["turn"] is None and seen[-1]["waiting"] == []

    @pytest.mark.asyncio
    async def test_the_service_broadcasts_it_as_agent_turn_updated(self):
        from dpc_client_core.agent_service import AgentService

        events = []

        class Api:
            async def broadcast_event(self, name, payload):
                events.append((name, payload))

        AgentService(llm_manager=None, local_api=Api(), firewall=None, peer_metadata={})
        async with hold(get_run_gate("agent_ark"), {"conversation_id": "c1"}):
            pass
        await asyncio.sleep(0.01)

        assert events and all(name == "agent_turn_updated" for name, _ in events)
        assert events[-1][1]["turn"] is None


class TestListAgentsCarriesTheTurn:
    @pytest.mark.asyncio
    async def test_each_row_has_turn_and_waiting(self, monkeypatch):
        from dpc_client_core.agent_service import AgentService
        from dpc_client_core.dpc_agent import utils

        class Registry:
            def list_agents(self):
                return [{"agent_id": "agent_ark", "name": "Ark"},
                        {"agent_id": "agent_idle", "name": "Idle"}]

        monkeypatch.setattr(utils, "AgentRegistry", Registry)
        monkeypatch.setattr(utils, "load_agent_config", lambda aid: {})
        service = AgentService(llm_manager=None, local_api=None, firewall=None, peer_metadata={})

        async with hold(get_run_gate("agent_ark"), {"conversation_id": "c1",
                                                    "conversation_title": "Mike (1:1)"}):
            result = await service.list_agents()

        rows = {r["agent_id"]: r for r in result["agents"]}
        assert rows["agent_ark"]["turn"]["conversation_id"] == "c1"
        assert rows["agent_ark"]["turn"]["conversation_title"] == "Mike (1:1)"
        assert rows["agent_ark"]["waiting"] == []
        assert rows["agent_idle"]["turn"] is None
        assert rows["agent_idle"]["waiting"] == []


def test_a_bare_gate_belongs_to_nobody_and_reports_nothing():
    gate = RunGate()
    assert gate.agent_id is None and gate.room is None
    snap = agent_turn_snapshot("agent_nobody")
    assert (snap["turn"], snap["waiting"]) == (None, [])
