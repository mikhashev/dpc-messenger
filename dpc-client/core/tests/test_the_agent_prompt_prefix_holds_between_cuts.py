"""The agent loop rewrites its prompt prefix only when a cut fires, and rarely.

A local engine (llama-server, Strata) reuses its cache up to the first message
that changed. Until 2026-10-18 the compaction-off path re-truncated the history
on every round past the eighth, so the boundary slid forward one round per
round and every request re-read the tail; the guard-stop finalising call also
dropped the tools and the reasoning effort, which some templates render at the
head of the prompt. Covered here with compaction on and off, on a fake provider:
the change lives in the loop, so it holds for every provider behind it.
"""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from dpc_client_core.dpc_agent import context as ctx_mod
from dpc_client_core.dpc_agent import loop as loop_mod
from dpc_client_core.dpc_agent.context import (
    CUT_MIN_BATCH, EMERGENCY_KEEP, OFF_ROUND_BOUND, UNDER_PRESSURE,
    CompactionState, apply_compaction, compact_tool_history, uncut_rounds,
)
from dpc_client_core.dpc_agent.guards import ResearchLimitGuard
from dpc_client_core.dpc_agent.hooks import HookRegistry
from dpc_client_core.dpc_agent.loop import _finalize_after_guard_stop, run_llm_loop
from dpc_client_core.dpc_agent.prompt_divergence import (
    PromptDivergenceTracker, first_divergence, message_fingerprint,
)

SCHEMAS = [{"type": "function", "function": {"name": "probe", "parameters": {}}}]
RESULT_CHARS = 3000


# --- the instrument ------------------------------------------------------------

def _fps(messages):
    return [message_fingerprint(m) for m in messages]


class TestTheDivergenceIndex:
    def test_equal_lists_have_none(self):
        msgs = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
        assert first_divergence(_fps(msgs), _fps(copy.deepcopy(msgs))) is None

    def test_a_change_at_i_reports_i(self):
        msgs = [{"role": "user", "content": str(i)} for i in range(6)]
        changed = copy.deepcopy(msgs)
        changed[3]["content"] = "other"
        assert first_divergence(_fps(msgs), _fps(changed)) == 3

    def test_an_append_reports_the_previous_length(self):
        msgs = [{"role": "user", "content": str(i)} for i in range(4)]
        longer = msgs + [{"role": "tool", "content": "x", "tool_call_id": "t"}]
        assert first_divergence(_fps(msgs), _fps(longer)) == 4

    def test_dropped_notes_count_as_a_change(self):
        msg = {"role": "assistant", "content": "", "tool_calls": [{"id": "t"}], "thinking": "n"}
        bare = {k: v for k, v in msg.items() if k != "thinking"}
        assert message_fingerprint(msg) != message_fingerprint(bare)

    def test_the_tracker_flags_tools_and_effort_and_ignores_appends(self):
        tr = PromptDivergenceTracker()
        msgs = [{"role": "user", "content": "q"}]
        first = tr.observe("c", msgs, SCHEMAS, "high", 1)
        assert first["first_request"] and not first["prefix_rewritten"]
        grown = msgs + [{"role": "assistant", "content": "a"}]
        seen = tr.observe("c", grown, SCHEMAS, "high", 2)
        assert seen["first_diff_msg"] == 1 and not seen["prefix_rewritten"]
        seen = tr.observe("c", grown, None, "high", 3)
        assert seen["tools_changed"] and not seen["effort_changed"] and seen["prefix_rewritten"]
        seen = tr.observe("c", grown, None, None, 4)
        assert seen["effort_changed"] and not seen["tools_changed"]
        assert seen["first_diff_msg"] is None

    def test_conversations_are_kept_apart_and_bounded(self):
        tr = PromptDivergenceTracker(max_conversations=2)
        for conv in ("a", "b", "c"):
            tr.observe(conv, [{"role": "user", "content": conv}], None, None)
        assert tr.observe("a", [{"role": "user", "content": "a"}], None, None)["first_request"]

    def test_agents_in_one_group_are_kept_apart(self):
        """Three agents answer in one group conversation: each one's first round
        is a first observation, and each one's next round is compared with its
        own last request, not with the agent that spoke before it."""
        tr = PromptDivergenceTracker()
        prompts = {a: [{"role": "system", "content": f"I am {a}"}, {"role": "user", "content": "q"}]
                   for a in ("ark", "johnny", "warren")}
        for agent, msgs in prompts.items():
            seen = tr.observe("group-1", msgs, SCHEMAS, None, 1, agent_id=agent)
            assert seen["first_request"] and seen["first_diff_msg"] is None
        for agent, msgs in prompts.items():
            grown = msgs + [{"role": "assistant", "content": "a"}]
            seen = tr.observe("group-1", grown, SCHEMAS, None, 2, agent_id=agent)
            assert seen["first_diff_msg"] == len(msgs) and not seen["prefix_rewritten"]

    def test_a_first_observation_is_logged_as_first(self, caplog):
        tr = PromptDivergenceTracker()
        with caplog.at_level("INFO", logger="dpc_client_core.dpc_agent.prompt_divergence"):
            tr.observe("c", [{"role": "user", "content": "q"}], None, None, 1, agent_id="ark")
            tr.observe("c", [{"role": "user", "content": "q"}] * 2, None, None, 2, agent_id="ark")
        lines = [r.getMessage() for r in caplog.records if "prompt divergence" in r.getMessage()]
        assert "conv=c round=1 first_diff_msg=first/1 " in lines[0]
        assert "conv=c round=2 first_diff_msg=1/2 " in lines[1]


# --- apply_compaction alone ----------------------------------------------------

def _round(r, size=RESULT_CHARS):
    return [
        {"role": "assistant", "content": f"step {r}", "tool_calls": [{"id": f"t{r}"}],
         "thinking": f"notes {r}"},
        {"role": "tool", "tool_call_id": f"t{r}", "content": f"{r}:" + "X" * size},
    ]


class _Summariser:
    def __init__(self):
        self.calls = 0

    async def query(self, prompt, provider_alias=None, **kwargs):
        self.calls += 1
        return f"SUMMARY {self.calls}"


def _drive(state, manager, ratios):
    """One round appended per call, the loop keeping the output; returns the
    rounds whose output rewrote anything already sent."""
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    rewrites = []

    async def run():
        nonlocal messages
        for r, ratio in enumerate(ratios, start=1):
            before = _fps(messages)
            messages = await apply_compaction(
                messages, state=state, last_prompt_tokens=int(ratio * state.window),
                llm_manager=manager, round_idx=r,
            )
            if first_divergence(before, _fps(messages)) is not None:
                rewrites.append(r)
            messages = messages + _round(r)

    asyncio.run(run())
    return rewrites, messages


@pytest.mark.parametrize("enabled", [False, True])
class TestTheCutIsBatched:
    def _state(self, enabled):
        return CompactionState({"compaction_enabled": enabled, "context_window": 100000})

    def _manager(self, enabled):
        return _Summariser() if enabled else None

    def test_no_pressure_cuts_on_the_round_bound_only_when_off(self, enabled):
        rewrites, _ = _drive(self._state(enabled), self._manager(enabled), [0.1] * 30)
        assert rewrites == ([OFF_ROUND_BOUND + 1] if not enabled else [])

    def test_a_release_the_tail_cannot_reach_cuts_once_per_batch(self, enabled):
        """Ark's case: usage stays between release and the pressure line however
        much is cut, so the hysteresis never clears. The old rule rewrote the
        prefix every round from there on; this one waits for a full batch."""
        rewrites, _ = _drive(self._state(enabled), self._manager(enabled), [0.82] * 30)
        assert rewrites, "pressure never cut anything"
        gaps = [b - a for a, b in zip(rewrites, rewrites[1:])]
        assert all(g >= CUT_MIN_BATCH for g in gaps), rewrites
        assert len(rewrites) <= 30 // CUT_MIN_BATCH + 1

    def test_above_the_pressure_line_it_cuts_deeper_and_still_batches(self, enabled):
        rewrites, messages = _drive(self._state(enabled), self._manager(enabled),
                                    [UNDER_PRESSURE + 0.05] * 30)
        gaps = [b - a for a, b in zip(rewrites, rewrites[1:])]
        assert rewrites and all(g >= 6 - EMERGENCY_KEEP + 1 for g in gaps), rewrites

    def test_a_cut_reapplied_changes_nothing(self, enabled):
        state = self._state(enabled)
        _, messages = _drive(state, self._manager(enabled), [0.82] * 20)
        again = asyncio.run(apply_compaction(
            messages[:-2], state=state, last_prompt_tokens=int(0.82 * state.window),
            llm_manager=self._manager(enabled), round_idx=21,
        ))
        assert _fps(again) == _fps(messages[:-2])


@pytest.mark.parametrize("threshold", [0.3, 0.5])
def test_off_ignores_the_summarisers_threshold(threshold):
    """Johnny (0.5) and Warren (0.3) run with compaction off; their threshold is
    the summariser's and must not cut the deterministic path. Off cuts at the
    pressure line or the round bound, nothing else."""
    state = CompactionState({"compaction_enabled": False, "compaction_threshold": threshold,
                             "context_window": 100000})
    rewrites, messages = _drive(state, None, [threshold + 0.1] * 20)
    assert rewrites == [] and uncut_rounds(messages) == 20
    assert state.compacting is False
    # At the pressure line it cuts, down to the emergency tail.
    out = asyncio.run(apply_compaction(
        messages, state=state, last_prompt_tokens=int(UNDER_PRESSURE * state.window),
        llm_manager=None, round_idx=21,
    ))
    assert uncut_rounds(out) == EMERGENCY_KEEP


def test_on_still_cuts_at_its_threshold():
    state = CompactionState({"compaction_enabled": True, "compaction_threshold": 0.5,
                             "context_window": 100000})
    manager = _Summariser()
    rewrites, _ = _drive(state, manager, [0.6] * 20)
    assert rewrites, "the summariser never ran above its own threshold"
    assert manager.calls > 0 and state.compacting is True


def test_the_deterministic_cut_is_idempotent():
    msgs = [m for r in range(12) for m in _round(r)]
    once = compact_tool_history(msgs, keep_recent=6)
    twice = compact_tool_history(once, keep_recent=6)
    assert _fps(once) == _fps(twice)
    assert uncut_rounds(once) == 6


# --- the loop, end to end on a fake provider ------------------------------------

class _Provider:
    """Tool rounds until `rounds`, then a final answer. Prompt tokens are the
    characters sent / 4, so the window fills the way a real run's does."""

    def __init__(self, rounds, summariser):
        self.rounds = rounds
        self.requests = []
        self._llm_manager = summariser

    async def chat(self, messages, tools=None, reasoning_effort=None, **kwargs):
        self.requests.append({
            "fps": _fps(messages), "tools": tools, "effort": reasoning_effort,
            "messages": copy.deepcopy(messages),
        })
        usage = {"prompt_tokens": sum(len(json.dumps(m)) for m in messages) // 4,
                 "completion_tokens": 10, "total_tokens": 0}
        n = len(self.requests)
        if n > self.rounds:
            return {"role": "assistant", "content": "final answer"}, usage
        return {
            "role": "assistant", "content": f"looking at {n}",
            "tool_calls": [{"id": f"c{n}", "function": {"name": "probe", "arguments": {"n": n}}}],
        }, usage


class _Tools:
    _ctx = None

    def schemas(self, core_only=False, include_restricted=False):
        return SCHEMAS

    def get_timeout(self, name):
        return 10


async def _fake_execute(tools, tc, logs_dir, timeout, task_id, **kwargs):
    n = tc["function"]["arguments"]["n"]
    return {"result": f"{n}:" + "R" * RESULT_CHARS, "is_error": False, "fn_name": "probe",
            "tool_call_id": tc["id"], "args_for_log": {"n": n}, "duration_ms": 1}


def _run_loop(tmp_path, monkeypatch, *, enabled, window, rounds=30, max_rounds=200):
    monkeypatch.setattr(loop_mod, "load_agent_config", lambda _name: {
        "compaction_enabled": enabled, "context_window": window,
    })
    monkeypatch.setattr(loop_mod, "_execute_with_timeout", _fake_execute)
    cut_rounds = []
    real_apply = loop_mod.apply_compaction

    async def spy(messages, **kwargs):
        out = await real_apply(messages, **kwargs)
        if out is not messages:
            cut_rounds.append(kwargs["round_idx"])
        return out

    monkeypatch.setattr(loop_mod, "apply_compaction", spy)
    provider = _Provider(rounds, _Summariser() if enabled else None)
    text, _usage, trace = asyncio.run(run_llm_loop(
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}],
        tools=_Tools(), llm=provider, agent_root=tmp_path, emit_progress=lambda *a, **k: None,
        reasoning_effort="high", conversation_id=f"conv-{enabled}-{window}-{max_rounds}",
        max_rounds=max_rounds,
    ))
    return text, trace, provider, cut_rounds


def _moved_rounds(provider):
    moved = []
    for r in range(1, len(provider.requests)):
        prev, cur = provider.requests[r - 1]["fps"], provider.requests[r]["fps"]
        i = first_divergence(prev, cur)
        if i is not None and i < len(prev):
            moved.append(r + 1)
    return moved


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("window", [12000, 10_000_000])
def test_over_thirty_rounds_the_prefix_moves_only_when_a_cut_fires(tmp_path, monkeypatch, enabled, window):
    text, trace, provider, cut_rounds = _run_loop(tmp_path, monkeypatch, enabled=enabled, window=window)
    assert text == "final answer"
    moved = _moved_rounds(provider)
    assert set(moved) <= set(cut_rounds), (moved, cut_rounds)
    assert len(cut_rounds) <= 30 // CUT_MIN_BATCH, cut_rounds
    if window == 12000:
        assert cut_rounds, "a 12K window filled by 30 rounds must be cut"
    # The loop's own counter says the same as the provider saw.
    assert trace["prompt_divergence"]["rewrite_rounds"] == moved
    assert trace["prompt_divergence"]["requests"] == len(provider.requests)
    # Every request carried the same tools and effort.
    assert all(r["tools"] == SCHEMAS and r["effort"] == "high" for r in provider.requests)


@pytest.mark.parametrize("enabled", [False, True])
def test_the_guard_stop_final_call_sends_what_the_loop_sent(tmp_path, monkeypatch, enabled):
    text, trace, provider, _ = _run_loop(
        tmp_path, monkeypatch, enabled=enabled, window=10_000_000, rounds=3, max_rounds=3,
    )
    loop_calls, final = provider.requests[:-1], provider.requests[-1]
    assert len(loop_calls) == 3
    assert final["tools"] == loop_calls[0]["tools"] == SCHEMAS
    assert final["effort"] == loop_calls[0]["effort"] == "high"
    # The finalising request only appends to the last loop request.
    assert first_divergence(loop_calls[-1]["fps"], final["fps"]) >= len(loop_calls[-1]["fps"])
    assert trace["prompt_divergence"]["prefix_rewrites"] == 0


# --- the finalising call, alone ------------------------------------------------

def _stopped_by_research():
    hooks = HookRegistry()
    guard = ResearchLimitGuard()
    hooks.register(guard)
    hooks.last_triggered = guard
    return hooks


class _Scripted:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    async def chat(self, messages, tools=None, reasoning_effort=None, **kwargs):
        call = {"tools": tools, "effort": reasoning_effort}
        if "tool_choice" in kwargs:
            call["tool_choice"] = kwargs["tool_choice"]
        self.calls.append(call)
        return self.answers.pop(0), {"prompt_tokens": 1, "completion_tokens": 1}


def _finalize(llm, **kwargs):
    return asyncio.run(_finalize_after_guard_stop(
        _stopped_by_research(), [{"role": "user", "content": "q"}], llm, None, "conv-f", {}, {},
        fallback_reason="stopped", task_id="t", **kwargs,
    ))


def test_the_final_call_carries_the_loop_tools_and_effort():
    llm = _Scripted({"role": "assistant", "content": "answer"})
    text, _, _ = _finalize(llm, tools=SCHEMAS, reasoning_effort="high")
    assert text == "answer"
    assert llm.calls == [{"tools": SCHEMAS, "effort": "high"}]


def test_a_tool_call_answer_asks_again_with_tool_choice_none():
    llm = _Scripted(
        {"role": "assistant", "content": "", "tool_calls": [{"id": "x", "function": {"name": "probe"}}]},
        {"role": "assistant", "content": "plain answer"},
    )
    text, usage, _ = _finalize(llm, tools=SCHEMAS, reasoning_effort="high")
    assert text == "plain answer"
    assert [c["tools"] for c in llm.calls] == [SCHEMAS, SCHEMAS]
    assert [c.get("tool_choice") for c in llm.calls] == [None, "none"]
    assert all(c["effort"] == "high" for c in llm.calls)


def test_a_text_written_tool_call_also_falls_back():
    class _Parsing(_Scripted):
        def _parse_tool_calls(self, content):
            return [{"function": {"name": "probe"}}] if "```tool_call" in content else []

    llm = _Parsing(
        {"role": "assistant", "content": "```tool_call\n{\"name\": \"probe\"}\n```"},
        {"role": "assistant", "content": "plain answer"},
    )
    text, _, _ = _finalize(llm, tools=SCHEMAS)
    assert text == "plain answer" and len(llm.calls) == 2


def test_without_tools_the_final_call_is_made_once():
    llm = _Scripted({"role": "assistant", "content": "answer"})
    text, _, _ = _finalize(llm)
    assert text == "answer" and llm.calls == [{"tools": None, "effort": None}]
