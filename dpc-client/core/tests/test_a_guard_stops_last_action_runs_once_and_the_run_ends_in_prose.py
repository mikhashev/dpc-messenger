"""When a guard has stopped the loop and the finalising call answers with a
tool call, that call is run once through the loop's own dispatch, and the run
then ends with an answer asked for under `tool_choice="none"`.

Live, 2026-10-25: Ark was asked to save a write-up; ResearchLimitGuard stopped
him, the finalising call answered with the write_file call, and the loop threw
it away. One round only: calls in the retry's answer are not run, so the
research the guard stopped cannot resume. No registry flag says which tools
only read (`is_code_tool` marks two), so every call of that answer runs.
"""

from __future__ import annotations

import asyncio
import json

from dpc_client_core.dpc_agent.guards import ResearchLimitGuard
from dpc_client_core.dpc_agent.hooks import HookRegistry
from dpc_client_core.dpc_agent.loop import _finalize_after_guard_stop, run_llm_loop

TOOLS = [{"type": "function", "function": {"name": "write_file", "parameters": {}}}]
WRITE = {"id": "w1", "type": "function",
         "function": {"name": "write_file", "arguments": '{"path": "knowledge/a.md", "content": "x"}'}}
PROBE = {"id": "p1", "type": "function", "function": {"name": "search_files", "arguments": "{}"}}


class _Scripted:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    async def chat(self, messages, tools=None, **kwargs):
        self.calls.append({"messages": list(messages), "tools": tools,
                           "tool_choice": kwargs.get("tool_choice")})
        return self.answers.pop(0), {"prompt_tokens": 1, "completion_tokens": 1}


def _hooks():
    hooks = HookRegistry()
    guard = ResearchLimitGuard()
    hooks.register(guard)
    hooks.last_triggered = guard
    return hooks


def _finalize(llm, dispatch):
    return asyncio.run(_finalize_after_guard_stop(
        _hooks(), [{"role": "user", "content": "save it"}], llm, None, "c", {}, {},
        fallback_reason="stopped", tools=TOOLS, execute_tool_calls=dispatch,
    ))


def test_the_finalising_calls_run_once_and_the_retry_sees_their_results():
    ran = []

    async def dispatch(target, calls, reasoning):
        ran.append([tc["function"]["name"] for tc in calls])
        for tc in calls:
            target.append({"role": "tool", "tool_call_id": tc["id"], "content": "written"})
        return True

    llm = _Scripted(
        {"role": "assistant", "content": "", "tool_calls": [WRITE]},
        {"role": "assistant", "content": "Saved to knowledge/a.md."},
    )
    text, _, _ = _finalize(llm, dispatch)
    assert text == "Saved to knowledge/a.md."
    assert ran == [["write_file"]]
    assert [c["tool_choice"] for c in llm.calls] == [None, "none"]
    assert llm.calls[1]["tools"] == TOOLS
    retry = llm.calls[1]["messages"]
    assert retry[-2] == {"role": "assistant", "content": "", "tool_calls": [WRITE]}
    assert retry[-1] == {"role": "tool", "tool_call_id": "w1", "content": "written"}


def test_calls_in_the_retrys_answer_are_not_run():
    ran = []

    async def dispatch(target, calls, reasoning):
        ran.append([tc["function"]["name"] for tc in calls])
        return True

    llm = _Scripted(
        {"role": "assistant", "content": "", "tool_calls": [WRITE]},
        {"role": "assistant", "content": "One more look first.", "tool_calls": [PROBE]},
    )
    text, _, trace = _finalize(llm, dispatch)
    assert ran == [["write_file"]], "a second round of calls was run"
    assert len(llm.calls) == 2
    assert text.startswith("⚠️ Not done") and "`search_files`" in text
    assert text.endswith("One more look first.")
    assert trace["unexecuted_tool_calls"][0]["tools"] == ["search_files"]


def test_a_stop_press_during_the_round_ends_the_run_without_the_retry():
    async def dispatch(target, calls, reasoning):
        return False

    llm = _Scripted({"role": "assistant", "content": "", "tool_calls": [WRITE]})
    text, _, trace = _finalize(llm, dispatch)
    assert text.startswith("⚠️ Stopped by user") and trace["stopped_by_user"]
    assert len(llm.calls) == 1


# --- through the loop: the registry runs it, the ledger records it ------------

class _Tools:
    _ctx = None

    def __init__(self):
        self.executed = []

    def schemas(self, core_only=False, include_restricted=False):
        return TOOLS

    def get_timeout(self, name):
        return 5

    def execute(self, name, args, ctx=None):
        self.executed.append((name, args))
        return "ok" if name == "write_file" else "found"


class _Llm:
    """Researches until the round guard stops it, then answers the stop with the
    write the user asked for, and — asked for no calls — with `second`."""

    def __init__(self, second):
        self.second = second
        self.tool_choices = []

    async def chat(self, messages, **kwargs):
        usage = {"prompt_tokens": 1, "completion_tokens": 1}
        self.tool_choices.append(kwargs.get("tool_choice"))
        if kwargs.get("tool_choice") == "none":
            return self.second, usage
        if str(messages[-1].get("content", "")).startswith("[ROUND_LIMIT]"):
            return {"content": "", "tool_calls": [WRITE]}, usage
        n = len(self.tool_choices)
        return {"content": "", "tool_calls": [dict(PROBE, id=f"p{n}")]}, usage


def _run(tmp_path, monkeypatch, second):
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})
    tools, llm = _Tools(), _Llm(second)
    text, usage, trace = asyncio.run(run_llm_loop(
        messages=[{"role": "user", "content": "save it"}], tools=tools, llm=llm,
        agent_root=tmp_path, emit_progress=lambda *a, **k: None, task_id="t", max_rounds=2,
    ))
    return text, usage, trace, tools, llm


def _ledger(tmp_path):
    lines = (tmp_path / "logs" / "tools.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def test_the_loop_runs_the_write_once_and_ends_with_prose(tmp_path, monkeypatch):
    text, usage, trace, tools, llm = _run(
        tmp_path, monkeypatch, {"content": "Saved to knowledge/a.md."})
    assert text == "Saved to knowledge/a.md."
    assert [n for n, _ in tools.executed] == ["search_files", "search_files", "write_file"]
    assert tools.executed[-1][1] == {"path": "knowledge/a.md", "content": "x"}
    assert llm.tool_choices[-1] == "none" and len(llm.tool_choices) == 4
    assert usage["rounds"] == 2
    writes = [r for r in _ledger(tmp_path) if r["tool"] == "write_file" and r.get("phase") == "outcome"]
    assert len(writes) == 1 and writes[0]["is_error"] is False
    assert [c["tool"] for c in trace["accumulated_tool_calls"]][-1] == "write_file"


def test_the_loop_does_not_resume_when_the_retry_asks_for_more(tmp_path, monkeypatch):
    text, _, _, tools, llm = _run(
        tmp_path, monkeypatch, {"content": "", "tool_calls": [dict(PROBE, id="late")]})
    assert [n for n, _ in tools.executed] == ["search_files", "search_files", "write_file"]
    assert len(llm.tool_choices) == 4
    assert text.startswith("⚠️ Not done") and "`search_files`" in text
    # The call that was not run is kept, its arguments with it.
    kept_at = text.split("kept at `", 1)[1].split("`", 1)[0]
    kept = (tmp_path / kept_at).read_text(encoding="utf-8")
    assert "--- tool calls ---" in kept and '"id": "late"' in kept
