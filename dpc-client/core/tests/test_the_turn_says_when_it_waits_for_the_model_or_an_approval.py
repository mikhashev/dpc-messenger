"""An agent's turn reads `model_call` or `approval` while it waits, and None after.

ADR-046 point 8: "busy in another chat" is half an answer when that chat is
itself stuck on an approval nobody has seen. The turn's `wait_reason` is set
around the adapter's model call and around the two approval waits (run_shell's
tier-1 gate, schedule_task's), and cleared in a `finally` — also when the call
raises or the approval times out. The approvals wait on an executor thread, so
their update reaches the event loop through `call_soon_threadsafe`.
"""

import asyncio
import threading
import time
import types

import pytest

from dpc_client_core.dpc_agent import run_gate as rg
from dpc_client_core.dpc_agent.run_gate import get_run_gate, hold


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


def _adapter(chat_impl):
    from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter

    adapter = DpcLlmAdapter.__new__(DpcLlmAdapter)
    adapter._chat = chat_impl
    adapter._write_usage_row = lambda *a, **k: None
    adapter._get_agent_provider_alias = lambda: "deepseek_flash"
    return adapter


class TestTheModelCall:
    @pytest.mark.asyncio
    async def test_the_turn_reads_model_call_during_the_call_and_none_after(self):
        gate = get_run_gate("agent_ark")
        during = {}

        async def chat_impl(messages, **kwargs):
            during.update(gate.turn)
            return {"content": "ok"}, {}

        async with hold(gate, {"conversation_id": "c1"}):
            await _adapter(chat_impl).chat([{"role": "user", "content": "hi"}])
            after = dict(gate.turn)

        assert during["wait_reason"] == "model_call"
        assert during["provider_alias"] == "deepseek_flash"
        assert after["wait_reason"] is None
        assert after["provider_alias"] == "deepseek_flash", "the alias stays the turn's"

    @pytest.mark.asyncio
    async def test_a_call_that_raises_clears_the_wait(self):
        gate = get_run_gate("agent_ark")

        async def chat_impl(messages, **kwargs):
            raise RuntimeError("provider down")

        async with hold(gate, {"conversation_id": "c1"}):
            with pytest.raises(RuntimeError):
                await _adapter(chat_impl).chat([{"role": "user", "content": "hi"}])
            assert gate.turn["wait_reason"] is None

    @pytest.mark.asyncio
    async def test_a_call_outside_any_turn_touches_nothing(self):
        gate = get_run_gate("agent_ark")

        async def chat_impl(messages, **kwargs):
            return {"content": "ok"}, {}

        await _adapter(chat_impl).chat([{"role": "user", "content": "hi"}])
        assert gate.turn is None

    @pytest.mark.asyncio
    async def test_a_task_that_outlives_its_turn_cannot_write_into_the_next(self):
        gate = get_run_gate("agent_ark")
        go = asyncio.Event()

        async def late():
            await go.wait()
            rg.note_turn(round=99)

        async with hold(gate, {"conversation_id": "c1"}):
            straggler = asyncio.create_task(late())
        async with hold(gate, {"conversation_id": "c2"}):
            go.set()
            await straggler
            assert gate.turn["round"] is None


class TestTheLoopReportsRoundAndTool:
    @pytest.mark.asyncio
    async def test_round_and_tool_land_on_the_turn(self, tmp_path):
        from dpc_client_core.dpc_agent import loop

        gate = get_run_gate("agent_ark")
        during = {}

        def fake_single(origin, *args, **kwargs):
            during.update(gate.turn)
            return {"tool_call_id": "t1", "fn_name": "read_file", "result": "x",
                    "is_error": False, "args_for_log": {}}

        tc = {"id": "t1", "function": {"name": "read_file", "arguments": "{}"}}
        logs = tmp_path / "agent_ark" / "logs"
        logs.mkdir(parents=True)
        orig = loop._execute_single_tool_for
        loop._execute_single_tool_for = fake_single
        try:
            async with hold(gate, {"conversation_id": "c1"}):
                rg.note_turn(round=3)
                await loop._execute_with_timeout(None, tc, logs, 5, round_number=3)
                after = dict(gate.turn)
        finally:
            loop._execute_single_tool_for = orig

        assert during["tool"] == "read_file" and during["round"] == 3
        assert after["tool"] is None and after["round"] == 3


class _Api:
    has_clients = True

    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


class TestTheShellApproval:
    @pytest.mark.asyncio
    async def test_the_turn_reads_approval_while_the_person_decides(self, tmp_path):
        from dpc_client_core.dpc_agent.tools import shell

        gate = get_run_gate("agent_ark")

        async def announce(**kwargs):
            return None

        service = types.SimpleNamespace(
            local_api=_Api(),
            announce_shell_approval_request=announce,
            announce_shell_approval_closed=announce,
        )
        out = {}

        async with hold(gate, {"conversation_id": "c1"}):
            ctx = types.SimpleNamespace(
                agent_root=tmp_path / "agent_ark",
                _agent=types.SimpleNamespace(_run_gate=gate, display_name="Ark",
                                             _firewall_profile=None),
                dpc_service=service,
                _event_loop=asyncio.get_running_loop(),
                reply_telegram_chat_id="",
                conversation_id="c1",
                conversation_title="Mike (1:1)",
            )
            worker = threading.Thread(target=lambda: out.update(
                result=shell._request_approval(ctx, "rm -rf ./x", "Requires approval", "", 10)))
            worker.start()
            assert await _until(lambda: gate.turn["wait_reason"] == "approval")
            for rid, entry in list(shell._pending_approvals.items()):
                entry["decision"] = "rejected"
                entry["result"] = "rejected in the test"
                entry["event"].set()
            await asyncio.to_thread(worker.join, 5)
            assert await _until(lambda: gate.turn["wait_reason"] is None)

        assert out["result"] == "rejected in the test"


class TestTheScheduleApproval:
    @pytest.mark.asyncio
    async def test_the_turn_reads_approval_until_the_answer(self, tmp_path):
        from dpc_client_core.dpc_agent.tools import core as tools_core

        gate = get_run_gate("agent_ark")

        async def announce(**kwargs):
            return None

        service = types.SimpleNamespace(
            local_api=_Api(),
            announce_schedule_approval_request=announce,
            announce_schedule_approval_closed=announce,
        )
        out = {}

        async with hold(gate, {"conversation_id": "group-x"}):
            ctx = types.SimpleNamespace(
                agent_root=tmp_path / "agent_ark",
                _agent=types.SimpleNamespace(_run_gate=gate, display_name="Ark"),
                dpc_service=service,
                _event_loop=asyncio.get_running_loop(),
                current_task_id="task-1",
                conversation_id="group-x",
                conversation_title="DPC project",
            )
            worker = threading.Thread(target=lambda: out.update(
                result=tools_core._await_schedule_approval(
                    ctx, task_type="check_back", when="in 60s", about="look again")))
            worker.start()
            assert await _until(lambda: gate.turn["wait_reason"] == "approval")
            for rid in list(tools_core._pending_schedule_approvals):
                tools_core.resolve_schedule_approval(rid, True)
            await asyncio.to_thread(worker.join, 5)
            assert await _until(lambda: gate.turn["wait_reason"] is None)

        assert out["result"] == (True, "")

    @pytest.mark.asyncio
    async def test_an_approval_from_another_chat_does_not_mark_this_turn(self, tmp_path):
        gate = get_run_gate("agent_ark")
        async with hold(gate, {"conversation_id": "c1"}):
            def from_thread():
                with rg.turn_waits_for("approval", gate=gate, conversation_id="c2"):
                    time.sleep(0.05)

            await asyncio.to_thread(from_thread)
            await asyncio.sleep(0.01)
            assert gate.turn["wait_reason"] is None
