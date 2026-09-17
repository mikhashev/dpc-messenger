"""A guard-stopped task's totals hold every call it made, the finalising one too.

ADR-041 D3 reads a task's cost as the sum of its ledger rows. When a guard
stops the loop it asks the model for one last answer without tools, and
`chat()` writes that call's row like any other — so the task's own totals have
to carry it, or the two series disagree by exactly one call on every task that
ends this way.
"""

import asyncio

import pytest

from dpc_client_core.dpc_agent.loop import run_llm_loop

CALL_USAGE = {
    "prompt_tokens": 100,
    "completion_tokens": 20,
    "total_tokens": 120,
    "cost": 0.004,
    "reasoning_tokens": 7,
}


class _Llm:
    """Reports the same usage on every call, and keeps asking for a tool so the
    loop takes rounds until a guard stops it. The call the guard stop makes is
    the one that arrives with `tools=None`."""

    def __init__(self):
        self.usages = []

    async def chat(self, messages, **kwargs):
        usage = dict(CALL_USAGE)
        self.usages.append(usage)
        if kwargs.get("tools") is None:
            return {"content": "final answer", "tool_calls": []}, usage
        return {
            "content": "",
            "tool_calls": [{
                "id": f"call-{len(self.usages)}",
                "function": {"name": "noop", "arguments": "{}"},
            }],
        }, usage


class _Tools:
    _ctx = None

    def schemas(self, core_only=False, include_restricted=False):
        return [{"name": "noop"}]

    def get_timeout(self, name):
        return 5

    def execute(self, name, args, ctx=None):
        return "ok"


def _run_until_the_round_guard_stops(tmp_path, monkeypatch, max_rounds=2):
    monkeypatch.setattr(
        "dpc_client_core.dpc_agent.loop.load_agent_config", lambda _name: {}
    )
    llm = _Llm()
    answer, usage, _trace = asyncio.run(run_llm_loop(
        messages=[{"role": "user", "content": "hi"}],
        tools=_Tools(),
        llm=llm,
        agent_root=tmp_path,
        emit_progress=lambda *a, **k: None,
        task_id="task-guarded",
        max_rounds=max_rounds,
    ))
    return answer, usage, llm.usages


def test_the_task_total_is_the_sum_of_every_call_the_guard_stop_included(tmp_path, monkeypatch):
    answer, usage, calls = _run_until_the_round_guard_stops(tmp_path, monkeypatch)

    # Two rounds, then the guard stops and the finalising call writes the answer.
    assert len(calls) == 3
    assert answer == "final answer"
    assert usage["cost"] == pytest.approx(sum(c["cost"] for c in calls))
    assert usage["prompt_tokens"] == sum(c["prompt_tokens"] for c in calls)
    assert usage["completion_tokens"] == sum(c["completion_tokens"] for c in calls)
    assert usage["total_tokens"] == sum(c["total_tokens"] for c in calls)
    assert usage["reasoning_tokens"] == sum(c["reasoning_tokens"] for c in calls)


def test_the_finalising_call_is_counted_without_being_called_a_round(tmp_path, monkeypatch):
    """It spends tokens like a round and is not one: `rounds` is the number a
    reader compares against the limit the guard stopped on."""
    _answer, usage, calls = _run_until_the_round_guard_stops(tmp_path, monkeypatch)

    assert usage["rounds"] == 2
    assert len(calls) == 3
    assert usage["first_prompt_tokens"] == CALL_USAGE["prompt_tokens"]
    assert usage["last_prompt_tokens"] == CALL_USAGE["prompt_tokens"]


def test_a_task_that_ends_on_its_own_counts_its_calls_once_each(tmp_path, monkeypatch):
    """The guard-stop path is the change; the ordinary end must not move."""
    monkeypatch.setattr(
        "dpc_client_core.dpc_agent.loop.load_agent_config", lambda _name: {}
    )

    class _AnswersAtOnce(_Llm):
        async def chat(self, messages, **kwargs):
            usage = dict(CALL_USAGE)
            self.usages.append(usage)
            return {"content": "done", "tool_calls": []}, usage

    llm = _AnswersAtOnce()
    answer, usage, _trace = asyncio.run(run_llm_loop(
        messages=[{"role": "user", "content": "hi"}],
        tools=_Tools(),
        llm=llm,
        agent_root=tmp_path,
        emit_progress=lambda *a, **k: None,
        task_id="task-plain",
    ))

    assert answer == "done"
    assert len(llm.usages) == 1
    assert usage["rounds"] == 1
    assert usage["cost"] == pytest.approx(CALL_USAGE["cost"])
    assert usage["total_tokens"] == CALL_USAGE["total_tokens"]
