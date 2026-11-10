"""On a local engine the agent loop uses the engine's window and the engine's count.

Card ON-A-LOCAL-ENGINE-THE-LOOP-MEASURES-ITS-WINDOW-WITH-CHARS-OVER-FOUR-AND-THE-ALIAS-NUMBER-WHILE-SLEEP-ASKS-THE-ENGINE
(Mike, 2026-11-11, items 1-4; ADR-033 amendment). Sleep synthesis has counted on the engine since
2026-11-09 (596b2083); the loop still projected its next prompt with chars/4 over text blocks (~38%
under on Cyrillic) against the alias number (250000 on the Strata alias) while the engine holds
261120, and its guard left 5% of the window for an answer that can need more.

What is faked is the engine: a provider double that says it counts on its engine, a window, and a
count of one token per character (a script chars/4 cannot see). The loop, the guard and the cut are real.

Also here, two review fixes from the same round: the summary pass marks only the rounds whose summaries
it bought once one has failed (c4f3f31f), and the guard's stop text says what this round's cut did
(68cdcf4b).
"""
import asyncio
import logging
from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent import agent as agent_module
from dpc_client_core.dpc_agent import loop as loop_module
from dpc_client_core.dpc_agent.context import (
    PartialCompaction, compact_tool_history_llm, context_reserve_of, estimate_messages_tokens,
    text_of_messages, uncut_rounds,
)
from dpc_client_core.dpc_agent.guards import ContextLimitGuard
from dpc_client_core.dpc_agent.loop import run_llm_loop
from dpc_client_core.providers.base import EngineCountError

ENGINE_WINDOW = 261_120
MEASURED = 1_200  # the engine's count for every request the fake answers


class _Engine:
    """The provider behind a local alias, as the loop asks it."""

    def __init__(self, window=ENGINE_WINDOW, count_fails=False, window_fails=False, events=None):
        self._window = window
        self._count_fails = count_fails
        self._window_fails = window_fails
        self.events = events if events is not None else []
        self.window_reads = 0
        self.counts = []  # (text, as_prompt)

    def counts_on_its_engine(self):
        return True

    async def engine_context_window(self):
        self.window_reads += 1
        self.events.append("window")
        if self._window_fails:
            raise EngineCountError("no n_ctx", kind="window")
        return self._window

    async def engine_token_count(self, text, *, as_prompt=False):
        self.counts.append((text, as_prompt))
        self.events.append("count")
        if self._count_fails:
            raise EngineCountError("tokenize failed")
        return len(text)  # one token per character: far above chars/4


class _Tools:
    _ctx = None

    def __init__(self, sizes):
        self._sizes = sizes
        self.executed = 0

    def schemas(self, core_only=False, include_restricted=False):
        return [{"type": "function", "function": {"name": "noop", "parameters": {}}}]

    def get_timeout(self, name):
        return 5

    def execute(self, name, args, ctx=None):
        self.executed += 1
        size = self._sizes[min(self.executed, len(self._sizes)) - 1]
        return f"result {self.executed} " + "x" * size


class _Llm:
    """Asks for one tool call per request while `tool_rounds` last, then answers. With an
    `engine` it is the adapter of a local alias (`local_engine`), without one a vendor's."""

    def __init__(self, tool_rounds, engine=None, measured=MEASURED):
        self._tool_rounds = tool_rounds
        self._engine = engine
        self._measured = measured
        self.requests = []

    def local_engine(self):
        return self._engine

    async def chat(self, messages, tools=None, **kwargs):
        self.requests.append([dict(m) for m in messages])
        if self._engine is not None:
            self._engine.events.append("chat")
        usage = {"prompt_tokens": self._measured, "completion_tokens": 20,
                 "total_tokens": self._measured + 20}
        n = len(self.requests)
        if n <= self._tool_rounds:
            return ({"content": "", "tool_calls": [{
                "id": f"c{n}", "type": "function",
                "function": {"name": "noop", "arguments": f'{{"n": {n}}}'}}]}, usage)
        return {"content": "Done.", "tool_calls": []}, usage


run_saved = []  # per cut call of the last `_run`: what the estimate says the cut took out


def _run(tmp_path, monkeypatch, llm, tools, config=None):
    """The loop's own rounds; returns (text, the guard's reads, the cuts' reads, what the strip carried)."""
    config = {} if config is None else config
    monkeypatch.setattr(loop_module, "load_agent_config", lambda _name: config)
    guard_reads, cut_reads, strips = [], [], []
    run_saved.clear()
    real_cut = loop_module.apply_compaction
    real_guard = ContextLimitGuard.between_rounds

    async def _cut(messages, *, last_prompt_tokens, state, **kwargs):
        cut_reads.append((last_prompt_tokens, state.window))
        out = await real_cut(messages, last_prompt_tokens=last_prompt_tokens, state=state, **kwargs)
        run_saved.append(estimate_messages_tokens(messages) - estimate_messages_tokens(out))
        return out

    async def _guard(self, ctx):
        guard_reads.append((ctx.last_prompt_tokens, ctx.context_window, ctx.state.context_reserve))
        return await real_guard(self, ctx)

    real_strip = loop_module.round_progress_payload

    def _strip(speed, **kwargs):
        strips.append((kwargs["context_window"], kwargs["context_reserve"]))
        return real_strip(speed, **kwargs)

    monkeypatch.setattr(loop_module, "apply_compaction", _cut)
    monkeypatch.setattr(ContextLimitGuard, "between_rounds", _guard)
    monkeypatch.setattr(loop_module, "round_progress_payload", _strip)
    text, usage, trace = asyncio.run(run_llm_loop(
        messages=[{"role": "user", "content": "go"}], tools=tools, llm=llm,
        agent_root=tmp_path / "agent", emit_progress=lambda *a, **k: None, task_id="t",
        context_window=204_800, context_reserve=16_384,
    ))
    return text, guard_reads, cut_reads, strips


# --- (a) the window -----------------------------------------------------------------------------------------------

def test_the_engines_window_is_read_once_after_its_first_answer_and_not_before(tmp_path, monkeypatch):
    engine = _Engine()
    llm = _Llm(tool_rounds=3, engine=engine)
    _, guard_reads, cut_reads, strips = _run(tmp_path, monkeypatch, llm, _Tools([100] * 3))

    # Round one runs on the alias numbers (nothing has answered, so asking would start the engine
    # just to ask); every later round divides by what the engine reported.
    assert cut_reads[0][1] == 204_800 and guard_reads[0][1] == 204_800
    assert all(window == ENGINE_WINDOW for _, window in cut_reads[1:])
    assert all(window == ENGINE_WINDOW for _, window, _ in guard_reads[1:])
    assert engine.window_reads == 1
    assert engine.events.index("window") > engine.events.index("chat"), "read before anything answered"
    # The strip carries the same pair the guard divides by, from the second round on.
    assert strips[-1] == (ENGINE_WINDOW, context_reserve_of(ENGINE_WINDOW))


def test_the_agents_own_context_window_caps_the_engines_when_lower(tmp_path, monkeypatch):
    engine = _Engine()
    llm = _Llm(tool_rounds=2, engine=engine)
    _, guard_reads, cut_reads, _ = _run(tmp_path, monkeypatch, llm, _Tools([100] * 2),
                                        config={"context_window": 100_000})

    assert cut_reads[1][1] == 100_000 and guard_reads[1][1] == 100_000


def test_an_own_window_above_the_engines_does_not_raise_it(tmp_path, monkeypatch):
    engine = _Engine()
    llm = _Llm(tool_rounds=2, engine=engine)
    _, guard_reads, _, _ = _run(tmp_path, monkeypatch, llm, _Tools([100] * 2),
                                config={"context_window": 400_000})

    assert guard_reads[1][1] == ENGINE_WINDOW


def test_a_provider_that_is_not_a_local_engine_keeps_the_current_numbers(tmp_path, monkeypatch):
    llm = _Llm(tool_rounds=3, engine=None)
    _, guard_reads, cut_reads, strips = _run(tmp_path, monkeypatch, llm, _Tools([100] * 3),
                                             config={"context_window": 5_000})

    assert {window for _, window in cut_reads} == {5_000}
    assert {(window, reserve) for _, window, reserve in guard_reads} == {(5_000, 0)}
    assert set(strips) == {(204_800, 16_384)}, "the strip keeps the caller's pair on a cloud"


def test_a_window_the_engine_cannot_give_leaves_the_alias_numbers_and_is_said_once(
        tmp_path, monkeypatch, caplog):
    engine = _Engine(window_fails=True)
    llm = _Llm(tool_rounds=4, engine=engine)
    with caplog.at_level(logging.WARNING, logger=loop_module.log.name):
        _, guard_reads, _, _ = _run(tmp_path, monkeypatch, llm, _Tools([100] * 4))

    assert {window for _, window, _ in guard_reads} == {204_800}
    said = [r for r in caplog.records if "could not be asked for its window" in r.getMessage()]
    assert len(said) == 1


# --- (b) the count ------------------------------------------------------------------------------------------------

def test_the_projection_adds_what_the_engine_counts_of_the_appended_messages(tmp_path, monkeypatch):
    engine = _Engine()
    llm = _Llm(tool_rounds=3, engine=engine)
    _, _, cut_reads, _ = _run(tmp_path, monkeypatch, llm, _Tools([100] * 3))

    assert cut_reads[0][0] == 0, "round one has no measured size and is not projected"
    appended = llm.requests[1][len(llm.requests[0]):]  # the reply and the result of round one
    text = text_of_messages(appended)
    assert cut_reads[1][0] == MEASURED + len(text)
    assert cut_reads[1][0] > MEASURED + estimate_messages_tokens(appended), "chars/4 would have said less"
    assert (text, False) in engine.counts


def test_one_count_per_round_for_what_was_appended(tmp_path, monkeypatch):
    engine = _Engine()
    llm = _Llm(tool_rounds=4, engine=engine)
    _run(tmp_path, monkeypatch, llm, _Tools([100] * 4))

    # Rounds 2..5 each project once; round one asks for nothing.
    assert len(engine.counts) == 4
    assert all(as_prompt is False for _, as_prompt in engine.counts)


def test_a_count_that_fails_falls_back_to_the_estimate_and_is_said_once(tmp_path, monkeypatch, caplog):
    engine = _Engine(count_fails=True)
    llm = _Llm(tool_rounds=4, engine=engine)
    with caplog.at_level(logging.WARNING, logger=loop_module.log.name):
        _, _, cut_reads, _ = _run(tmp_path, monkeypatch, llm, _Tools([100] * 4))

    appended = llm.requests[1][len(llm.requests[0]):]
    assert cut_reads[1][0] == MEASURED + estimate_messages_tokens(appended)
    said = [r for r in caplog.records if "to count what the round appended" in r.getMessage()]
    assert len(said) == 1
    assert len(engine.counts) == 1, "a sick engine is asked once, not once per round"
    assert engine.window_reads == 1, "the window was still the engine's"


def test_a_cut_takes_its_savings_off_scaled_by_what_the_engine_counted_over_the_estimate(tmp_path, monkeypatch):
    """Eight old results the cut replaces and a ninth that is big: the savings come off at the
    engine's share over the estimate, so a script chars/4 under-counts does not leave the guard
    reading the projection as if the cut had freed less than it did."""
    window = 30_000
    engine = _Engine(window=window)
    llm = _Llm(tool_rounds=9, engine=engine, measured=int(window * 0.5))
    tools = _Tools([3_000] * 8 + [14_000])
    config = {"compaction_enabled": False}
    _, guard_reads, cut_reads, _ = _run(tmp_path, monkeypatch, llm, tools, config=config)

    cut_told = cut_reads[9][0]
    guard_read = guard_reads[9][0]
    assert cut_told > window * 0.85, "the projection is under pressure, so the cut acts"
    assert run_saved[9] > 0
    # The engine counted this round's appended text at far over the estimate: the scale is capped at 2.5.
    assert cut_told - guard_read == int(run_saved[9] * 2.5)


# --- (c) the answer reserve ---------------------------------------------------------------------------------------

def test_on_a_local_engine_the_guard_stops_at_the_window_less_the_answer_reserve(tmp_path, monkeypatch):
    window = 5_000  # reserve: max(5% = 250, min(16384, 20% = 1000)) = 1000, so the limit is 4000
    assert context_reserve_of(window) == 1_000
    measured = 4_100  # 82% of the window: under 95%, over window - reserve
    engine = _Engine(window=window)
    llm = _Llm(tool_rounds=3, engine=engine, measured=measured)
    text, guard_reads, _, _ = _run(tmp_path, monkeypatch, llm, _Tools([50] * 3))

    assert text.startswith("[CONTEXT_LIMIT]")
    assert "which leaves less than 1000 tokens for the answer" in text
    assert guard_reads[-1][2] == 1_000
    assert len(llm.requests) == 1, "stopped before the second request, the one that would not fit"


def test_the_same_numbers_on_a_cloud_keep_the_ratio_and_go_on(tmp_path, monkeypatch):
    llm = _Llm(tool_rounds=3, engine=None, measured=4_100)
    text, _, _, _ = _run(tmp_path, monkeypatch, llm, _Tools([50] * 3), config={"context_window": 5_000})

    assert text.startswith("Done.")


def test_the_reserve_rule_is_the_pre_turn_checks(tmp_path):
    for window in (5_000, 32_768, 131_072, ENGINE_WINDOW, 1_000_000):
        assert context_reserve_of(window) == max(
            int(window * 0.05), min(agent_module.CONTEXT_ROUND_RESERVE_TOKENS, int(window * 0.2)))
    # On a window a 5% share cannot hold an answer in, the guard leaves more than the ratio did.
    assert ENGINE_WINDOW - context_reserve_of(ENGINE_WINDOW) < 0.95 * ENGINE_WINDOW


def test_the_guard_without_a_reserve_keeps_the_ratio_limit():
    guard = ContextLimitGuard(ratio=0.95)
    state = SimpleNamespace(last_prompt_tokens=4_900, context_window=5_000, context_reserve=0,
                            cut_this_round=None)
    ctx = SimpleNamespace(context_window=5_000, last_prompt_tokens=4_900, state=state)
    assert asyncio.run(guard.between_rounds(ctx)) is not None
    ctx.last_prompt_tokens = 4_700
    assert asyncio.run(ContextLimitGuard(ratio=0.95).between_rounds(ctx)) is None


# --- the pre-flight in agent.py -----------------------------------------------------------------------------------

def _agent_double(tmp_path):
    return SimpleNamespace(llm=SimpleNamespace(), agent_root=tmp_path / "agent_x")


def test_the_pre_turn_check_counts_the_built_prompt_on_the_engine(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_module, "load_agent_config", lambda _name: {})
    engine = _Engine()
    messages = [{"role": "system", "content": "s" * 400}, {"role": "user", "content": "\u043f\u0440\u0438\u0432\u0435\u0442 " * 50}]
    got = asyncio.run(agent_module.DpcAgent._measure_on_the_engine(
        _agent_double(tmp_path), engine, messages, "conv", None, None))

    text = text_of_messages(messages)
    assert got == (len(text), ENGINE_WINDOW)
    assert engine.counts == [(text, True)], "the whole prompt, template included"
    assert got[0] > estimate_messages_tokens(messages)


def test_the_pre_turn_window_is_capped_by_the_agents_own_context_window(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_module, "load_agent_config", lambda _name: {"context_window": 90_000})
    got = asyncio.run(agent_module.DpcAgent._measure_on_the_engine(
        _agent_double(tmp_path), _Engine(), [{"role": "user", "content": "hi"}], "conv", None, None))

    assert got[1] == 90_000


def test_a_pre_turn_count_the_engine_cannot_give_is_said_and_returns_none(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(agent_module, "load_agent_config", lambda _name: {})
    with caplog.at_level(logging.WARNING, logger=agent_module.log.name):
        got = asyncio.run(agent_module.DpcAgent._measure_on_the_engine(
            _agent_double(tmp_path), _Engine(count_fails=True),
            [{"role": "user", "content": "hi"}], "conv", None, None))

    assert got is None
    assert any("could not be asked for the prompt's tokens" in r.getMessage() for r in caplog.records)


def test_a_stop_during_the_pre_turn_count_ends_the_wait(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_module, "load_agent_config", lambda _name: {})

    class _Slow(_Engine):
        async def engine_context_window(self):
            await asyncio.sleep(30)

    async def go():
        stop = asyncio.Event()
        task = asyncio.ensure_future(agent_module.DpcAgent._measure_on_the_engine(
            _agent_double(tmp_path), _Slow(), [{"role": "user", "content": "hi"}], "conv", stop, None))
        await asyncio.sleep(0.05)
        stop.set()
        return await asyncio.wait_for(task, timeout=5)

    assert asyncio.run(go()) is None


# --- review fix 1: a failed pass marks only what it bought ------------------------------------------------------------

class _Summariser:
    """Answers the calls it is told to and fails the one it is told to."""

    def __init__(self, fail_on=()):
        self.fail_on = set(fail_on)
        self.calls = 0

    async def query(self, prompt, provider_alias=None):
        self.calls += 1
        if self.calls in self.fail_on:
            raise RuntimeError("summariser down")
        return f"summary {self.calls}"


def _history(rounds, size=3_000):
    msgs = [{"role": "user", "content": "go"}]
    for n in range(1, rounds + 1):
        msgs.append({"role": "assistant", "content": f"note {n}", "thinking": "t" * 50, "tool_calls": [{
            "id": f"c{n}", "type": "function", "function": {"name": "noop", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{n}", "content": f"r{n} " + "x" * size})
    return msgs


def test_after_the_first_failure_the_pass_marks_no_round_it_did_not_buy():
    msgs = _history(10)  # keep_recent 6: rounds 1-4 are old
    summariser = _Summariser(fail_on={2})
    with pytest.raises(PartialCompaction) as raised:
        asyncio.run(compact_tool_history_llm(msgs, summariser, None, keep_recent=6))
    partial = raised.value.result

    assert summariser.calls == 2, "no paid call after the failure"
    marked = [m for m in partial if m.get("_compacted")]
    # Round 1: bought, so its result and its assistant message are marked. Rounds 2-4: nothing.
    assert [m.get("content", "")[:9] for m in marked if m["role"] == "tool"] == ["summary 1"]
    assert [m["content"] for m in marked if m["role"] == "assistant"] == ["note 1"]
    assert uncut_rounds(partial) == 9, "only round 1 counts as cut"
    assert all(not m.get("_compacted") for m in partial[3:])


def test_the_next_batch_buys_the_summaries_the_failed_pass_did_not():
    msgs = _history(10)
    first = _Summariser(fail_on={2})
    with pytest.raises(PartialCompaction) as raised:
        asyncio.run(compact_tool_history_llm(msgs, first, None, keep_recent=6))

    second = _Summariser()
    out = asyncio.run(compact_tool_history_llm(raised.value.result, second, None, keep_recent=6))

    assert second.calls == 3, "rounds 2, 3 and 4 are bought again; round 1 is not"
    # Round 1 keeps the summary the failed pass bought; the other three are the second pass's own.
    assert [m["content"] for m in out if m["role"] == "tool" and m.get("_compacted")][:4] == [
        "summary 1", "summary 1", "summary 2", "summary 3"]
    assert uncut_rounds(out) == 6


def test_a_pass_with_no_failure_marks_every_old_round_as_before():
    msgs = _history(10)
    out = asyncio.run(compact_tool_history_llm(msgs, _Summariser(), None, keep_recent=6))

    assert uncut_rounds(out) == 6
    assert all(m.get("_compacted") for m in out if m["role"] == "tool" and m["tool_call_id"] in
               {"c1", "c2", "c3", "c4"})


# --- review fix 2: the stop text says what this round's cut did -----------------------------------------------------------

def _fired(compaction_on, cut, reserve=0):
    guard = ContextLimitGuard(ratio=0.95, compaction_on=compaction_on)
    state = SimpleNamespace(context_reserve=reserve, cut_this_round=cut)
    ctx = SimpleNamespace(context_window=5_000, last_prompt_tokens=4_900, state=state)
    assert asyncio.run(guard.between_rounds(ctx)) is not None
    return guard.stop_message()


def test_with_compaction_on_a_cut_that_happened_is_told_as_a_cut():
    text = _fired(True, True)
    assert "compaction cut the old tool results and could not bring it under" in text
    assert "nothing left to cut" not in text


def test_with_compaction_on_and_no_cut_possible_the_text_says_so():
    text = _fired(True, False)
    assert "compaction found nothing left to cut this round" in text
    assert "too few earlier tool rounds" in text
    assert "compaction cut the old tool results" not in text


def test_with_compaction_off_the_cut_is_the_plain_one_in_both_cases():
    cut = _fired(False, True)
    none = _fired(False, False)

    assert "compaction is off for this agent" in cut and "the plain cut of old tool results could not bring it under" in cut
    assert "compaction is off for this agent" in none and "nothing left for the plain cut to take this round" in none
    assert "turn compaction on" in cut and "turn compaction on" in none


def test_the_loop_tells_the_guard_whether_its_cut_changed_the_prompt(tmp_path, monkeypatch):
    """Two real turns: nothing to cut (two rounds), then a list the cut did change."""
    llm = _Llm(tool_rounds=2, measured=int(5_000 * 0.99))
    monkeypatch.setattr(loop_module, "load_agent_config", lambda _name: {"context_window": 5_000})
    text, _, _ = asyncio.run(run_llm_loop(
        messages=[{"role": "user", "content": "go"}], tools=_Tools([100, 100]), llm=llm,
        agent_root=tmp_path / "agent", emit_progress=lambda *a, **k: None, task_id="t"))

    assert text.startswith("[CONTEXT_LIMIT]")
    assert "there was nothing left to cut" in text or "nothing left for the plain cut" in text
