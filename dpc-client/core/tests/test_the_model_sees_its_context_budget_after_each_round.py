"""After each tool round the model reads how full this turn's context is, in words.

THE-MODEL-SEES-ITS-CONTEXT-BUDGET-ONLY-AT-TURN-START (ADR-045 decision 6). tokens_limit and
tokens_after_last_response reached the model once, in the turn-start runtime section, and were
never refreshed between rounds. Live 2026-11-27: an agent at round 27 planned on "3.6 %
(tokens_after_last_response 9003 measured at 2026-11-01)" while the window held 49,412 of
250,000 tokens. The loop now appends one line after each tool round, from its own projection
(the size ContextLimitGuard reads), never into the prefix the engine already holds, with a
warning from 80 %; the turn-start figure says it was measured before this turn.
"""
import asyncio
import json
import re

from dpc_client_core.dpc_agent import loop as loop_module
from dpc_client_core.dpc_agent.context import (
    STALE_TURN_START_NOTE, _build_runtime_section, estimate_messages_tokens,
)
from dpc_client_core.dpc_agent.guards import CONTEXT_LIMIT_RATIO, ContextLimitGuard
from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter, messages_to_prompt
from dpc_client_core.dpc_agent.loop import (
    CONTEXT_BUDGET_WARN_RATIO, context_budget_line, run_llm_loop,
)

WARNING = " Nearing the limit (80% or more): finish the task or summarise before reading more."
LINE = re.compile(r"^\[Context budget, this turn, after round (\d+): ([\d,]+) of ([\d,]+) "
                  r"tokens in use \((\d+)%\)\.\]")


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
    """One tool call per request while `tool_rounds` last, then an answer; the prompt size it
    reports is `measured`, the same for every request."""

    def __init__(self, tool_rounds, measured):
        self._tool_rounds = tool_rounds
        self._measured = measured
        self.requests = []

    async def chat(self, messages, tools=None, **kwargs):
        self.requests.append([dict(m) for m in messages])
        usage = {"prompt_tokens": self._measured, "completion_tokens": 20,
                 "total_tokens": self._measured + 20}
        n = len(self.requests)
        if n <= self._tool_rounds:
            return ({"content": "", "tool_calls": [{
                "id": f"c{n}", "type": "function",
                "function": {"name": "noop", "arguments": f'{{"n": {n}}}'}}]}, usage)
        return {"content": "Done.", "tool_calls": []}, usage


def _run(tmp_path, monkeypatch, llm, tools, config, context_window=None, guard_reads=None):
    monkeypatch.setattr(loop_module, "load_agent_config", lambda _name: config)
    if guard_reads is not None:
        real_guard = ContextLimitGuard.between_rounds

        async def _guard(self, ctx):
            guard_reads.append(ctx.last_prompt_tokens)
            return await real_guard(self, ctx)

        monkeypatch.setattr(ContextLimitGuard, "between_rounds", _guard)
    return asyncio.run(run_llm_loop(
        messages=[{"role": "user", "content": "go"}], tools=tools, llm=llm,
        agent_root=tmp_path / "agent", emit_progress=lambda *a, **k: None, task_id="t",
        context_window=context_window,
    ))


def _lines(request):
    return [m for m in request if m.get("_context_budget")]


# --- the line itself ------------------------------------------------------------------------


def test_the_line_says_in_words_what_it_is():
    assert context_budget_line(27, 49_412, 250_000) == (
        "[Context budget, this turn, after round 27: 49,412 of 250,000 tokens in use (20%).]")


def test_the_warning_comes_at_eighty_percent_and_not_before():
    assert CONTEXT_BUDGET_WARN_RATIO == 0.80 < CONTEXT_LIMIT_RATIO
    assert context_budget_line(5, 200_000, 250_000) == (
        "[Context budget, this turn, after round 5: 200,000 of 250,000 tokens in use (80%).]"
        + WARNING)
    assert context_budget_line(5, 197_000, 250_000) == (
        "[Context budget, this turn, after round 5: 197,000 of 250,000 tokens in use (79%).]")
    # A line that shows 80% warns: the warning reads the figure the line prints.
    assert context_budget_line(5, 198_800, 250_000).endswith("(80%).]" + WARNING)


def test_nothing_is_said_without_both_numbers():
    assert context_budget_line(3, 10_000, 0) is None
    assert context_budget_line(3, 0, 250_000) is None
    assert context_budget_line(0, 10_000, 250_000) is None


# --- in the loop ----------------------------------------------------------------------------


def test_after_a_tool_round_the_next_request_ends_with_the_line(tmp_path, monkeypatch):
    llm = _Llm(tool_rounds=2, measured=49_000)
    _run(tmp_path, monkeypatch, llm, _Tools([400, 400]), {"context_window": 250_000})

    assert _lines(llm.requests[0]) == [], "round one follows no tool round"
    for k in (1, 2):
        request = llm.requests[k]
        last = request[-1]
        assert last.get("_context_budget") and last["role"] == "user" and last["_runtime_note"]
        assert request[-2]["role"] == "tool", "the line follows the round's tool results"
        # The loop's own projection: the measured prompt plus what the round appended.
        appended = request[len(llm.requests[k - 1]):-1]
        expected = 49_000 + estimate_messages_tokens(appended)
        assert last["content"] == context_budget_line(k, expected, 250_000)
        assert LINE.match(last["content"]).group(1) == str(k)
    assert "Nearing the limit" not in llm.requests[2][-1]["content"]


def test_the_earlier_messages_of_the_next_request_are_unchanged(tmp_path, monkeypatch):
    llm = _Llm(tool_rounds=4, measured=30_000)
    _run(tmp_path, monkeypatch, llm, _Tools([300] * 4), {"context_window": 250_000})

    for before, after in zip(llm.requests, llm.requests[1:]):
        assert after[:len(before)] == before, "the line rewrote the prefix the engine holds"
    # Each round adds one line and none is moved or edited: the latest is the last one.
    assert [len(_lines(r)) for r in llm.requests] == [0, 1, 2, 3, 4]


def test_the_line_warns_near_the_limit(tmp_path, monkeypatch):
    llm = _Llm(tool_rounds=1, measured=8_500)
    _run(tmp_path, monkeypatch, llm, _Tools([100]), {"context_window": 10_000})

    line = llm.requests[1][-1]["content"]
    assert LINE.match(line) and line.endswith(WARNING)


def test_the_line_carries_the_size_the_guard_read_after_a_cut(tmp_path, monkeypatch):
    # Eight rounds of results a cut can replace, then one large enough to push the projection
    # over the pressure line: the cut acts, and the line says what is left, as the guard reads it.
    guard_reads = []
    llm = _Llm(tool_rounds=9, measured=1_200)
    _run(tmp_path, monkeypatch, llm, _Tools([3_000] * 8 + [40_000]),
         {"context_window": 5_000}, guard_reads=guard_reads)

    assert any(m.get("_compacted") for m in llm.requests[-1]), "the scenario cuts"
    for k, request in enumerate(llm.requests[1:], start=1):
        match = LINE.match(request[-1]["content"])
        assert int(match.group(2).replace(",", "")) == guard_reads[k]
        assert match.group(3) == "5,000"


def test_no_line_when_the_window_is_unknown(tmp_path, monkeypatch):
    # No context_window in the config, no alias to resolve one, no engine, no caller's figure:
    # the compaction state's generic fallback is not a window to report.
    llm = _Llm(tool_rounds=2, measured=20_000)
    _run(tmp_path, monkeypatch, llm, _Tools([100, 100]), {})
    assert all(_lines(r) == [] for r in llm.requests)


def test_the_callers_window_serves_when_the_config_has_none(tmp_path, monkeypatch):
    llm = _Llm(tool_rounds=1, measured=20_000)
    _run(tmp_path, monkeypatch, llm, _Tools([100]), {}, context_window=131_072)
    assert LINE.match(llm.requests[1][-1]["content"]).group(3) == "131,072"


def test_compaction_on_still_gets_the_line(tmp_path, monkeypatch):
    llm = _Llm(tool_rounds=2, measured=10_000)
    _run(tmp_path, monkeypatch, llm, _Tools([100, 100]),
         {"context_window": 100_000, "compaction_enabled": True})
    assert [len(_lines(r)) for r in llm.requests] == [0, 1, 2]


# --- every provider path renders it after the tool results ----------------------------------


_ROUND = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "go"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "noop", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "c1", "content": "result 1"},
    {"role": "user", "content": context_budget_line(1, 49_412, 250_000),
     "_runtime_note": True, "_context_budget": True},
]


def test_the_text_path_renders_the_line_as_a_turn_after_the_result():
    prompt = messages_to_prompt(_ROUND)
    assert prompt.endswith("[TOOL RESULT: c1]\nresult 1\n\n[USER]\n"
                           + context_budget_line(1, 49_412, 250_000))


def test_the_native_path_joins_the_line_to_the_tool_results_turn():
    _, turns = DpcLlmAdapter._convert_messages_to_anthropic(_ROUND)
    last = turns[-1]
    assert last["role"] == "user" and turns[-2]["role"] == "assistant", "roles still alternate"
    assert last["content"][0]["type"] == "tool_result"
    assert last["content"][-1]["text"] == context_budget_line(1, 49_412, 250_000)


# --- the turn-start figure ------------------------------------------------------------------


def _session(state, tmp_path):
    text = _build_runtime_section(tmp_path, {"id": "c", "type": "chat"}, state)
    return json.loads(text.split("\n\n", 1)[1])["session"]


def test_the_turn_start_figure_says_it_was_measured_before_this_turn(tmp_path):
    session = _session({"tokens_limit": 250_000, "tokens_after_last_response": 9003,
                        "tokens_after_last_response_at": "2026-11-01T10:00:00+00:00",
                        "context_usage_percent": 3.6}, tmp_path)
    assert session["tokens_after_last_response_at"] == "2026-11-01T10:00:00+00:00"
    assert session["measured_before_this_turn"] is True
    assert session["note"] == STALE_TURN_START_NOTE == (
        "measured before this turn; the per-round context line is the live number")


def test_no_note_when_nothing_was_measured(tmp_path):
    session = _session({"tokens_limit": 250_000, "tokens_after_last_response": 0}, tmp_path)
    assert session["tokens_after_last_response"] is None
    assert "note" not in session and "measured_before_this_turn" not in session
