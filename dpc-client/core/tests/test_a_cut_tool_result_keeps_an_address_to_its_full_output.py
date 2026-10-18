"""A cut tool result keeps an address to its full output (ADR-045 decision 2).

Until 2026-10-19 the compaction-off cut replaced an old tool result with its
first 200 characters and nothing else. The full output reached history.json only
after the turn, while the cut happens inside it, so the model could not get the
data back and did not know what it had lost. Now the loop writes each output at
the call and the cut leaves a placeholder naming the call, its size and that file.
Acceptance fixed by Ark before the code: a result over 20,000 chars, a forced cut,
the address in the next request, the same bytes back through read_file, the
placeholder unchanged by a second cut, the same bytes on identical input, and the
file on disk before anything writes history.json.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import time

from dpc_client_core.dpc_agent import loop as loop_mod
from dpc_client_core.dpc_agent.context import (
    OFF_ROUND_BOUND, CompactionState, apply_compaction, compact_tool_history, compact_tool_history_llm,
    cut_placeholder,
)
from dpc_client_core.dpc_agent.loop import TOOL_OUTPUT_DIR, run_llm_loop
from dpc_client_core.dpc_agent.tools.core import read_file
from dpc_client_core.dpc_agent.tools.registry import ToolContext

BIG = 25_000
SCHEMAS = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]


def _output(n: int) -> str:
    # Lines, CRLF and non-ASCII, so "the same bytes" is a claim about more than one alphabet.
    line = f"round {n} caf\u00e9 \u4e2d\u6587 line\r\n"
    return (line * (BIG // len(line) + 1))[:BIG]


def _tool(n: int) -> str:
    return "read_file" if n % 2 else "run_shell"


class _Provider:
    """Tool rounds until `rounds`, then an answer. Each request records which
    stored outputs exist on disk at that moment, and whether history.json does."""

    def __init__(self, agent_root, rounds):
        self.agent_root = agent_root
        self.rounds = rounds
        self.requests = []
        self._llm_manager = None

    async def chat(self, messages, tools=None, reasoning_effort=None, **kwargs):
        store = self.agent_root / TOOL_OUTPUT_DIR
        self.requests.append({
            "messages": copy.deepcopy(messages),
            "stored": sorted(p.name for p in store.rglob("*.txt")) if store.exists() else [],
            "history_json": any(self.agent_root.rglob("history.json")),
        })
        usage = {"prompt_tokens": sum(len(json.dumps(m)) for m in messages) // 4,
                 "completion_tokens": 10, "total_tokens": 0}
        n = len(self.requests)
        if n > self.rounds:
            return {"role": "assistant", "content": "final answer"}, usage
        args = {"path": f"notes/{n}.md"} if _tool(n) == "read_file" else {"command": f"cat log{n}"}
        return {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": f"c{n}", "function": {"name": _tool(n), "arguments": args}}],
        }, usage


class _Tools:
    _ctx = None

    def schemas(self, core_only=False, include_restricted=False):
        return SCHEMAS

    def get_timeout(self, name):
        return 10


async def _fake_execute(tools, tc, logs_dir, timeout, task_id, **kwargs):
    n = int(tc["id"][1:])
    return {"result": _output(n), "is_error": False, "fn_name": tc["function"]["name"],
            "tool_call_id": tc["id"], "args_for_log": tc["function"]["arguments"],
            "duration_ms": 1}


def _run(tmp_path, monkeypatch, rounds=OFF_ROUND_BOUND + 2, window=10_000_000):
    # A large window, so the cut is the off path's round bound and no guard stops first.
    monkeypatch.setattr(loop_mod, "load_agent_config", lambda _name: {
        "compaction_enabled": False, "context_window": window,
    })
    monkeypatch.setattr(loop_mod, "_execute_with_timeout", _fake_execute)
    provider = _Provider(tmp_path, rounds)
    text, _usage, _trace = asyncio.run(run_llm_loop(
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}],
        tools=_Tools(), llm=provider, agent_root=tmp_path, emit_progress=lambda *a, **k: None,
        conversation_id="conv-address",
    ))
    assert text == "final answer"
    return provider


def _tool_msg(messages, call_id):
    return next(m for m in messages if m.get("role") == "tool" and m.get("tool_call_id") == call_id)


def _first_cut_request(provider):
    for i, req in enumerate(provider.requests[1:], start=1):
        if _tool_msg(req["messages"], "c1").get("_compacted"):
            return i
    raise AssertionError("no request carried a cut result")


# --- 1, 2, 3, 5: the loop, end to end on a fake provider --------------------------

def test_a_forced_cut_leaves_the_address_in_the_next_request(tmp_path, monkeypatch):
    provider = _run(tmp_path, monkeypatch)
    first = _first_cut_request(provider)
    placeholder = _tool_msg(provider.requests[first]["messages"], "c1")["content"]
    assert placeholder.startswith("[Cut from context: read_file(path=\"notes/1.md\")")
    assert f"returned {BIG:,} chars" in placeholder
    address = _tool_msg(provider.requests[first]["messages"], "c1")["_output_at"]
    assert address.startswith(TOOL_OUTPUT_DIR + "/") and address.endswith("/r1-0.txt")
    assert f"Full output: {address}" in placeholder
    assert "Re-running the same call also returns the data" in placeholder
    run_shell_cut = _tool_msg(provider.requests[first]["messages"], "c2")["content"]
    assert "do not re-run the call" in run_shell_cut


def test_the_address_gives_back_the_same_bytes_through_read_file(tmp_path, monkeypatch):
    provider = _run(tmp_path, monkeypatch)
    msgs = provider.requests[_first_cut_request(provider)]["messages"]
    for n in (1, 2):
        address = _tool_msg(msgs, f"c{n}")["_output_at"]
        # The agent's own tool, by the relative path the placeholder names.
        assert read_file(ToolContext(agent_root=tmp_path), address) == _output(n)
        assert (tmp_path / address).read_bytes() == _output(n).encode("utf-8")


def test_the_placeholder_survives_a_second_cut(tmp_path, monkeypatch):
    provider = _run(tmp_path, monkeypatch)
    first = _first_cut_request(provider)
    once = _tool_msg(provider.requests[first]["messages"], "c1")
    assert f"Full output: {once['_output_at']}" in once["content"]
    # Every later request of the loop carries it unchanged ...
    for req in provider.requests[first + 1:]:
        assert _tool_msg(req["messages"], "c1") == once
    # ... and so does a deeper cut over the same list.
    again = compact_tool_history(provider.requests[-1]["messages"], keep_recent=1)
    assert _tool_msg(again, "c1") == once


def test_the_output_is_on_disk_before_the_next_request_and_before_history_json(tmp_path, monkeypatch):
    provider = _run(tmp_path, monkeypatch, rounds=3, window=10_000_000)
    for n, req in enumerate(provider.requests[1:], start=1):
        assert f"r{n}-0.txt" in req["stored"], (n, req["stored"])
        assert req["history_json"] is False


# --- 4: determinism ---------------------------------------------------------------

def _history(rounds=8):
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
    for n in range(1, rounds + 1):
        msgs.append({"role": "assistant", "content": "x" * 300, "tool_calls": [
            {"id": f"c{n}", "function": {"name": _tool(n), "arguments": json.dumps({"path": f"{n}.md"})}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{n}", "content": _output(n)[:15000],
                     "_output_chars": BIG, "_output_at": f"{TOOL_OUTPUT_DIR}/T/r{n}-0.txt"})
    return msgs


def test_identical_input_gives_byte_identical_messages():
    a = compact_tool_history(copy.deepcopy(_history()), keep_recent=2)
    b = compact_tool_history(copy.deepcopy(_history()), keep_recent=2)
    assert json.dumps(a, ensure_ascii=False) == json.dumps(b, ensure_ascii=False)
    cut = [m for m in a if m.get("role") == "tool" and m.get("_compacted")]
    assert len(cut) == 6 and all(m["content"].startswith("[Cut from context:") for m in cut)


def test_shortened_assistant_text_says_so():
    out = compact_tool_history(_history(), keep_recent=2)
    first = next(m for m in out if m.get("role") == "assistant")
    assert first["content"].endswith("[shortened by a context cut from 300 chars]")


def test_a_small_result_is_kept_verbatim_and_marked():
    # Under 2,000 chars a placeholder saves little and costs a re-read.
    small = "s" * 1500
    msgs = _history()
    msgs[3] = {**msgs[3], "content": small, "_output_chars": len(small)}
    out = compact_tool_history(msgs, keep_recent=2)
    assert out[3]["content"] == small and out[3]["_compacted"] is True


def test_without_an_address_the_placeholder_says_there_is_no_copy():
    text = cut_placeholder("run_shell", {"command": "ls"}, 30000, None, head="abc")
    assert text == ('[Cut from context: run_shell(command="ls") returned 30,000 chars; '
                    'no stored copy. First 3 chars follow.]\nabc')


def test_long_arguments_are_bounded():
    text = cut_placeholder("write_file", {"path": "a.md", "content": "y" * 5000}, 10, "s/a.txt")
    assert len(text) < 500 and "(5002 chars)" in text


# --- the summariser path and the log -------------------------------------------------

def test_a_summary_carries_the_address_too():
    class _LM:
        async def query(self, prompt, provider_alias=None, **kwargs):
            return "SUMMARY"

    out = asyncio.run(compact_tool_history_llm(_history(), _LM(), "p", keep_recent=2))
    tool = _tool_msg(out, "c1")
    assert tool["content"] == f"SUMMARY\n[Full output: {TOOL_OUTPUT_DIR}/T/r1-0.txt (read_file reads it).]"


def test_the_cut_log_line_names_the_evicted_calls(caplog):
    state = CompactionState({"compaction_enabled": False, "context_window": 1000})
    with caplog.at_level(logging.INFO, logger="dpc_client_core.dpc_agent.context"):
        asyncio.run(apply_compaction(_history(), state=state, last_prompt_tokens=999,
                                     llm_manager=None, round_idx=9))
    line = next(r.getMessage() for r in caplog.records if "context cut" in r.getMessage())
    assert f"read_file:{BIG}@{TOOL_OUTPUT_DIR}/T/r1-0.txt" in line
    assert f"run_shell:{BIG}@{TOOL_OUTPUT_DIR}/T/r2-0.txt" in line


# --- retention -------------------------------------------------------------------------

def test_turn_directories_older_than_the_retention_are_removed(tmp_path):
    store = tmp_path / TOOL_OUTPUT_DIR
    old, new = store / "old", store / "new"
    for d in (old, new):
        d.mkdir(parents=True)
        (d / "r1-0.txt").write_text("x")
    stale = time.time() - (loop_mod.TOOL_OUTPUT_KEEP_DAYS + 1) * 86400
    os.utime(old, (stale, stale))
    loop_mod._prune_tool_outputs(tmp_path)
    assert not old.exists() and new.exists()
