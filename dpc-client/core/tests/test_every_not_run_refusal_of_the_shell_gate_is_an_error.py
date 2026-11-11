"""Every refusal of `run_shell`'s approval gate counts as an error in the loop.

ADR-046 (amendment 2026-11-11) says a refusal counts as an error. Three of the gate's
refusals did not start with the warning mark (nobody could be asked, the approval timed
out, a person said no) and stayed `is_error: False`. Their texts are read elsewhere, so
the loop learned them (`SHELL_GATE_REFUSALS`) instead of the texts changing. The
texts here come from the real code paths, so a reworded refusal fails this test.
"""
import asyncio
import types

import pytest

from dpc_client_core.dpc_agent import loop as agent_loop
from dpc_client_core.dpc_agent.tools import shell
from dpc_client_core.service import CoreService


@pytest.fixture(autouse=True)
def _no_leftover_requests():
    shell._pending_approvals.clear()
    yield
    shell._pending_approvals.clear()


class _Service:
    def __init__(self, has_clients):
        self.local_api = types.SimpleNamespace(has_clients=has_clients)

    async def announce_shell_approval_request(self, **kwargs):
        pass

    async def announce_shell_approval_closed(self, **kwargs):
        pass


def _ctx(tmp_path, has_clients):
    return types.SimpleNamespace(agent_root=tmp_path, dpc_service=_Service(has_clients),
                                 reply_telegram_chat_id="", _event_loop=None, _agent=None)


def _ask(tmp_path, has_clients):
    return shell._request_approval(_ctx(tmp_path, has_clients), "rm -rf x", reason="tier1", cwd="", timeout=10)


def _a_person_said_no():
    service = CoreService.__new__(CoreService)
    service.p2p_manager = types.SimpleNamespace(get_display_name=lambda: "Mike")

    async def _closed(**kwargs):
        pass

    service.announce_shell_approval_closed = _closed
    entry = {"command": "rm -rf x", "agent_id": "a", "agent_name": "a", "event": None}
    shell._pending_approvals["r1"] = entry
    asyncio.run(service.resolve_shell_approval("r1", approved=False, resolved_via="desktop"))
    return entry["result"]


def _the_fallback(tmp_path):
    """A rejection that set no text: the real path, answered by writing the decision only."""
    import threading
    out = {}
    t = threading.Thread(target=lambda: out.update(
        r=shell._request_approval(_ctx(tmp_path, True), "rm -rf y", reason="tier1", cwd="", timeout=10)))
    t.start()
    for _ in range(500):
        if shell._pending_approvals:
            break
        threading.Event().wait(0.01)
    (entry,) = shell._pending_approvals.values()
    entry["decision"] = "rejected"
    entry["event"].set()
    t.join(5)
    return out["r"]


def _classified(tmp_path, text, tool="run_shell"):
    tools = types.SimpleNamespace(execute=lambda name, args, ctx=None: text)
    tc = {"id": "call-1", "function": {"name": tool, "arguments": "{}"}}
    return agent_loop._execute_single_tool(tools, tc, tmp_path)


def _the_gates_refusals(tmp_path, monkeypatch):
    monkeypatch.setattr(shell, "APPROVAL_TTL_SECONDS", 0.05)
    return {
        "nobody could be asked": _ask(tmp_path, has_clients=False),
        "the approval timed out": _ask(tmp_path, has_clients=True),
        "a person said no": _a_person_said_no(),
        "the fallback when no text was set": _the_fallback(tmp_path),
    }


def test_the_gate_refusals_carry_no_warning_mark_and_the_loop_still_counts_them(tmp_path, monkeypatch):
    texts = _the_gates_refusals(tmp_path, monkeypatch)
    assert len(texts) == 4
    for what, text in texts.items():
        assert agent_loop.is_a_refusal_result("run_shell", text), what
        assert _classified(tmp_path, text)["is_error"] is True, what


def test_the_unmarked_texts_are_unchanged(tmp_path, monkeypatch):
    """The texts the tests and the person read stay as they were."""
    texts = _the_gates_refusals(tmp_path, monkeypatch)
    assert texts["nobody could be asked"].startswith("\U0001f6ab Approval required and nobody could be asked")
    assert "Not run" in texts["nobody could be asked"]
    assert texts["the approval timed out"].startswith("⏳ Command approval timed out")
    assert "Not run" in texts["the approval timed out"]
    assert texts["a person said no"] == "Command rejected by Mike on the desktop. Not run: `rm -rf x`"
    assert texts["the fallback when no text was set"] == "Command manually rejected by User. Not run: `rm -rf y`"


def test_only_the_shell_tool_gets_the_wider_reading(tmp_path):
    text = "Command rejected by Mike on the desktop. Not run: `rm -rf x`"
    assert agent_loop.is_a_refusal_result("run_shell", text)
    assert not agent_loop.is_a_refusal_result("read_file", text)
    assert _classified(tmp_path, text, tool="read_file")["is_error"] is False


def test_an_ordinary_result_is_not_an_error_and_the_mark_still_is(tmp_path):
    assert _classified(tmp_path, "hello\n")["is_error"] is False
    assert agent_loop.is_a_refusal_result("run_shell", "⚠️ Not run: x. `y`")
