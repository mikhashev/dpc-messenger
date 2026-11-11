"""A `run_shell` call a rule stopped before the command ran counts as an error and says why.

Mike's call, 2026-11-11 (ADR-046 amendment): the guardrail block and the cwd refusals
start with texts the tests and the person read, so the loop learns them
(`SHELL_GATE_REFUSALS`). The texts come from the real `run_shell` paths.
"""
import types

import pytest

from dpc_client_core.dpc_agent import loop as agent_loop
from dpc_client_core.dpc_agent.tools.registry import ToolContext
from dpc_client_core.dpc_agent.tools.shell import run_shell


def _ctx(root):
    ctx = ToolContext(agent_root=root)
    ctx.firewall = None
    return ctx


def _classified(tmp_path, text):
    tools = types.SimpleNamespace(execute=lambda name, args, ctx=None: text)
    tc = {"id": "call-1", "function": {"name": "run_shell", "arguments": "{}"}}
    return agent_loop._execute_single_tool(tools, tc, tmp_path)


def _blocked(tmp_path):
    return "rm -rf /", run_shell(_ctx(tmp_path), "rm -rf /")


def _cwd_outside(tmp_path, tmp_path_factory):
    other = tmp_path_factory.mktemp("elsewhere")
    return "echo hi", str(other), run_shell(_ctx(tmp_path), "echo hi", cwd=str(other))


def test_a_guardrail_block_is_an_error_naming_the_command_the_rule_and_that_it_did_not_run(tmp_path):
    command, text = _blocked(tmp_path)
    assert text.startswith("⛔ Command blocked by safety guardrails")
    assert agent_loop.is_a_refusal_result("run_shell", text)
    assert _classified(tmp_path, text)["is_error"] is True
    assert f"`{command}`" in text
    assert "Not run" in text
    assert "not an approval" in text


def test_a_cwd_outside_the_sandbox_is_an_error_naming_the_cwd_the_boundary_and_the_command(tmp_path, tmp_path_factory):
    command, cwd, text = _cwd_outside(tmp_path, tmp_path_factory)
    assert agent_loop.is_a_refusal_result("run_shell", text)
    assert _classified(tmp_path, text)["is_error"] is True
    assert f"cwd '{cwd}'" in text
    assert "outside allowed sandbox" in text
    assert "agent sandbox" in text
    assert f"Not run: `{command}`" in text


def test_a_cwd_that_is_no_directory_is_an_error_that_says_the_command_did_not_run(tmp_path):
    missing = str(tmp_path / "nope")
    text = run_shell(_ctx(tmp_path), "echo hi", cwd=missing)
    assert agent_loop.is_a_refusal_result("run_shell", text)
    assert f"cwd '{missing}'" in text
    assert "not a valid directory" in text
    assert "Not run: `echo hi`" in text


def test_a_command_that_ran_and_failed_is_not_mistaken_for_a_refusal(tmp_path):
    assert not agent_loop.is_a_refusal_result("run_shell", "Error: command timed out after 5s - x.")
    assert not agent_loop.is_a_refusal_result("run_shell", "hello\n[exit code: 1]")


def test_only_the_shell_tool_reads_these_prefixes(tmp_path):
    _, text = _blocked(tmp_path)
    assert not agent_loop.is_a_refusal_result("read_file", text)


def test_a_human_rejection_says_the_command_did_not_run(tmp_path, monkeypatch):
    import asyncio
    from dpc_client_core import service as svc_mod
    from dpc_client_core.dpc_agent.tools import shell as shell_mod

    entry = {"command": "rm important.txt", "agent_id": "a"}
    monkeypatch.setitem(shell_mod._pending_approvals, "req-1", entry)
    holder = types.SimpleNamespace(
        _SHELL_DECISION_PLACES=svc_mod.CoreService._SHELL_DECISION_PLACES,
        p2p_manager=None,
        announce_shell_approval_closed=lambda **kw: asyncio.sleep(0),
    )
    resolved_via = next(iter(holder._SHELL_DECISION_PLACES))
    fn = svc_mod.CoreService.resolve_shell_approval
    asyncio.run(fn(holder, "req-1", False, resolved_via))
    text = entry["result"]
    assert text.startswith("Command rejected by")
    assert text.endswith("Not run: `rm important.txt`")
    assert agent_loop.is_a_refusal_result("run_shell", text)
    assert "Not run: `{command}`" in open(shell_mod.__file__, encoding="utf-8").read().split("Command manually rejected by")[1][:80]
