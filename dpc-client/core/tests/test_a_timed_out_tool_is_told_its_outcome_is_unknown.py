"""A tool the loop stopped waiting for is told to the model as "outcome unknown".

The loop abandons a running tool on timeout (the pool's worker runs on), so the
call may still complete and its effect may already have happened. Until
2026-11-10 the model read that inside `[TOOL_FAILED: ...]` with "Do NOT report
success", and retried what had in fact been done. Now the result says the
outcome is unknown, that the call may still complete, and to check the effect
before retrying; a call that failed for any other reason keeps the failed
envelope. For `run_shell` the loop's clock also outlasts the approval wait and
the command's own deadline, so the shell's known-outcome answer comes first.
"""

from __future__ import annotations

import asyncio
import copy
import threading

from dpc_client_core.dpc_agent import loop as loop_mod
from dpc_client_core.dpc_agent.loop import (
    _classify_tool_error,
    _execute_with_timeout,
    run_llm_loop,
    timeout_result_text,
)
from dpc_client_core.dpc_agent.tools import shell as shell_tool

SCHEMAS = [{"type": "function", "function": {"name": "write_file", "parameters": {}}}]


class _Provider:
    """One tool round, then an answer; keeps every request's messages."""

    def __init__(self):
        self.requests = []
        self._llm_manager = None

    async def chat(self, messages, tools=None, reasoning_effort=None, **kwargs):
        self.requests.append(copy.deepcopy(messages))
        usage = {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 0}
        if len(self.requests) > 1:
            return {"role": "assistant", "content": "final answer"}, usage
        return {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "c1", "function": {"name": "write_file",
                                                     "arguments": '{"path": "a.txt"}'}}],
        }, usage


class _Tools:
    """A registry whose one tool blocks until released, then 'completes'."""

    _ctx = None

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.release = threading.Event()
        self.completed = threading.Event()
        self.calls = 0

    def schemas(self, core_only=False, include_restricted=False):
        return SCHEMAS

    def get_timeout(self, name):
        return 1

    def execute(self, fn_name, args, ctx=None):
        self.calls += 1
        if self.behaviour == "hang":
            self.release.wait(30)
            self.completed.set()
            return "wrote a.txt"
        if self.behaviour == "raise":
            raise RuntimeError("disk on fire")
        if self.behaviour == "warn":
            return "⚠️ could not write a.txt"
        raise AssertionError(self.behaviour)


def _run(tmp_path, monkeypatch, tools):
    monkeypatch.setattr(loop_mod, "load_agent_config", lambda _name: {"compaction_enabled": False})
    provider = _Provider()
    text, _usage, trace = asyncio.run(run_llm_loop(
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}],
        tools=tools, llm=provider, agent_root=tmp_path, emit_progress=lambda *a, **k: None,
        conversation_id="conv-timeout",
    ))
    assert text == "final answer"
    return provider, trace


def _tool_messages(messages):
    return [m for m in messages if m.get("role") == "tool" and m.get("tool_call_id") == "c1"]


def test_a_timed_out_call_reaches_the_model_as_outcome_unknown(tmp_path, monkeypatch):
    tools = _Tools("hang")
    try:
        provider, _trace = _run(tmp_path, monkeypatch, tools)
    finally:
        tools.release.set()
    (msg,) = _tool_messages(provider.requests[1])
    content = msg["content"]
    assert content.startswith("⚠️ TOOL_TIMEOUT (write_file): exceeded 1s limit.")
    assert "outcome is UNKNOWN" in content
    assert "may still complete" in content and "already have taken effect" in content
    assert "Check the effect first" in content
    # not the plain failed envelope, which reads as "nothing happened"
    assert "[TOOL_FAILED" not in content
    assert "The tool call above FAILED" not in content


def test_a_late_completion_adds_no_second_result_for_the_call(tmp_path, monkeypatch):
    tools = _Tools("hang")
    try:
        provider, trace = _run(tmp_path, monkeypatch, tools)
        assert not tools.completed.is_set()  # the worker is still inside the call
    finally:
        tools.release.set()
    assert tools.completed.wait(10)  # now it completes, after the model was told
    # the answer round saw exactly one result for the call, and it is the timeout
    assert len(provider.requests) == 2
    (msg,) = _tool_messages(provider.requests[1])
    assert "TOOL_TIMEOUT" in msg["content"] and "wrote a.txt" not in msg["content"]
    assert [c["tool"] for c in trace["tool_calls"]] == ["write_file"]
    assert tools.calls == 1  # and nothing re-ran it


def test_a_timeout_still_counts_as_an_error_for_the_trace_and_the_counters(tmp_path, monkeypatch):
    # A timeout is a call that did not answer in time; the readers of `is_error`
    # (the trace, the status icon, skill reflection's recent-errors count) keep
    # counting it. Only the words the model reads changed.
    tools = _Tools("hang")
    try:
        _provider, trace = _run(tmp_path, monkeypatch, tools)
    finally:
        tools.release.set()
    assert trace["tool_calls"][0]["is_error"] is True
    assert _classify_tool_error(timeout_result_text("write_file", 5)) == "timeout"


def test_the_result_names_the_tool_and_the_limit():
    text = timeout_result_text("run_shell", 390)
    assert text.startswith("⚠️ TOOL_TIMEOUT (run_shell): exceeded 390s limit.")


def test_a_call_that_raised_keeps_the_failed_envelope(tmp_path, monkeypatch):
    provider, _trace = _run(tmp_path, monkeypatch, _Tools("raise"))
    (msg,) = _tool_messages(provider.requests[1])
    assert msg["content"].startswith("[TOOL_FAILED: write_file]\n")
    assert "TOOL_ERROR" in msg["content"]
    assert "The tool call above FAILED" in msg["content"]
    assert "UNKNOWN" not in msg["content"]


def test_a_tool_that_answered_with_a_warning_keeps_the_failed_envelope(tmp_path, monkeypatch):
    provider, _trace = _run(tmp_path, monkeypatch, _Tools("warn"))
    (msg,) = _tool_messages(provider.requests[1])
    assert msg["content"].startswith("[TOOL_FAILED: write_file]\n")
    assert "UNKNOWN" not in msg["content"]


def test_the_timeout_result_of_the_executor_is_marked_and_the_others_are_not(tmp_path):
    tools = _Tools("hang")
    call = {"id": "c1", "function": {"name": "write_file", "arguments": "{}"}}
    try:
        timed_out = asyncio.run(_execute_with_timeout(tools, call, tmp_path, timeout_sec=1))
    finally:
        tools.release.set()
    assert timed_out["timed_out"] is True and timed_out["is_error"] is True
    failed = asyncio.run(_execute_with_timeout(_Tools("raise"), call, tmp_path, timeout_sec=5))
    assert failed["is_error"] is True and not failed.get("timed_out")


# --- run_shell: the loop's clock outlasts a person's wait plus the command ---------

def _run_shell_entry():
    return next(e for e in shell_tool.get_tools() if e.name == "run_shell")


def test_the_loops_timeout_for_run_shell_covers_the_approval_and_the_longest_command():
    entry = _run_shell_entry()
    longest = shell_tool.MAX_COMMAND_TIMEOUT_SECONDS
    assert longest == 300  # the schema's "5-300" and the clamp in _execute_shell_command
    assert entry.timeout_sec > shell_tool.APPROVAL_TTL_SECONDS + longest
    assert entry.timeout_sec == shell_tool.RUN_SHELL_TOOL_TIMEOUT_SECONDS


def test_the_clamp_and_the_loops_ceiling_share_one_number():
    # the command's clamp reads the constant the loop's clock is built on, so
    # one cannot be raised without the other
    with open(shell_tool.__file__, encoding="utf-8") as f:
        source = f.read()
    assert "min(max(timeout, 5), 300)" not in source
    assert "min(max(timeout, 5), MAX_COMMAND_TIMEOUT_SECONDS)" in source
