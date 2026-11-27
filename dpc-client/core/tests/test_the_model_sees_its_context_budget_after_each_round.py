"""After each tool round the model reads how full this turn's context is, in words.

THE-MODEL-SEES-ITS-CONTEXT-BUDGET-ONLY-AT-TURN-START (ADR-045 decision 6). tokens_limit and
tokens_after_last_response reached the model once, in the turn-start runtime section, and were
never refreshed between rounds. Live 2026-11-27: an agent at round 27 planned on "3.6 %
(tokens_after_last_response 9003 measured at 2026-11-01)" while the window held 49,412 of
250,000 tokens. The loop now puts one line at the end of the round's last tool result, from
its own projection (the size ContextLimitGuard reads) against the window the guard enforces,
with a warning on the way to the guard's stop; the turn-start figure says it was measured
before this turn.

Review of the first build (2026-11-27): a line sent as its own user message moved the turn
base a KV snapshot hashes (kv_snapshots.base_end), printed the 204,800 default for a window
nobody knew, and warned at 80 % of a window whose guard stops at 80 % on a small engine.
"""
import asyncio
import json
import re

import pytest

from dpc_client_core.dpc_agent import loop as loop_module
from dpc_client_core.dpc_agent.context import (
    STALE_TURN_START_NOTE, _build_runtime_section, compact_tool_history_llm, context_reserve_of,
    estimate_messages_tokens, tool_output_of,
)
from dpc_client_core.dpc_agent.guards import CONTEXT_LIMIT_RATIO, ContextLimitGuard, context_stop_at
from dpc_client_core.dpc_agent.hooks import HookAction, HookContext
from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter, messages_to_prompt
from dpc_client_core.dpc_agent.loop import (
    CONTEXT_BUDGET_WARN_SHARE, CONTEXT_BUDGET_WARNING, _with_context_line, context_budget_line,
    run_llm_loop,
)
from dpc_client_core.managers import kv_snapshots
from dpc_client_core.providers.base import anthropic_to_openai_messages

WARNING = " Nearing the context limit: finish the task or summarise before reading more."
LINE = re.compile(r"\[Context budget, this turn, after round (\d+): ([\d,]+) of ([\d,]+) "
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
    reports is `measured`, the same for every request, or with `measured=None` the estimate of
    the request plus 500 (a prompt that shrinks after a cut). Each request is kept as sent."""

    def __init__(self, tool_rounds, measured):
        self._tool_rounds = tool_rounds
        self._measured = measured
        self.requests = []

    async def chat(self, messages, tools=None, **kwargs):
        self.requests.append([dict(m) for m in messages])
        measured = (estimate_messages_tokens(messages) + 500 if self._measured is None
                    else self._measured)
        usage = {"prompt_tokens": measured, "completion_tokens": 20, "total_tokens": measured + 20}
        n = len(self.requests)
        if n <= self._tool_rounds:
            return ({"content": "", "tool_calls": [{
                "id": f"c{n}", "type": "function",
                "function": {"name": "noop", "arguments": f'{{"n": {n}}}'}}]}, usage)
        return {"content": "Done.", "tool_calls": []}, usage


def _run(tmp_path, monkeypatch, llm, tools, config, context_window=None, guard_seen=None):
    monkeypatch.setattr(loop_module, "load_agent_config", lambda _name: config)
    if guard_seen is not None:
        real_guard = ContextLimitGuard.between_rounds

        async def _guard(self, ctx):
            guard_seen.append((ctx.last_prompt_tokens, ctx.context_window))
            return await real_guard(self, ctx)

        monkeypatch.setattr(ContextLimitGuard, "between_rounds", _guard)
    return asyncio.run(run_llm_loop(
        messages=[{"role": "user", "content": "go"}], tools=tools, llm=llm,
        agent_root=tmp_path / "agent", emit_progress=lambda *a, **k: None, task_id="t",
        context_window=context_window,
    ))


def _lines(request):
    """The context lines a request carries, in order."""
    return [m["_context_line"] for m in request if m.get("_context_line")]


def _without_lines(request):
    return [dict(m, content=tool_output_of(m)) if m.get("_context_line") else m for m in request]


def _call(n):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": f"c{n}", "type": "function", "function": {"name": "noop", "arguments": "{}"}}]}


# --- the line itself ------------------------------------------------------------------------


def test_the_line_says_in_words_what_it_is():
    assert context_budget_line(27, 49_412, 250_000) == (
        "[Context budget, this turn, after round 27: 49,412 of 250,000 tokens in use (20%).]")


def test_the_warning_comes_on_the_way_to_the_guards_stop():
    assert CONTEXT_BUDGET_WARNING == WARNING and "%" not in WARNING
    # A cloud window: the guard stops at 95 % (237,500 of 250,000); the warning at 85 % of that.
    stop = context_stop_at(250_000)
    assert stop == int(250_000 * CONTEXT_LIMIT_RATIO) == 237_500
    assert CONTEXT_BUDGET_WARN_SHARE * stop == 201_875
    assert context_budget_line(5, 201_875, 250_000) == (
        "[Context budget, this turn, after round 5: 201,875 of 250,000 tokens in use (81%).]" + WARNING)
    assert context_budget_line(5, 201_874, 250_000) == (
        "[Context budget, this turn, after round 5: 201,874 of 250,000 tokens in use (81%).]")


def test_a_small_engine_window_is_warned_before_its_reserve_stops_it():
    # 65,536 on a local engine: the answer reserve is 20 % (13,107), so the guard stops at 80 %
    # of the window; a warning tied to 80 % of the window would never have been read.
    window = 65_536
    stop = context_stop_at(window, context_reserve_of(window))
    assert stop == window - 13_107
    assert context_budget_line(4, 46_000, window, stop_at=stop) == (
        "[Context budget, this turn, after round 4: 46,000 of 65,536 tokens in use (70%).]" + WARNING)
    assert not context_budget_line(4, 40_000, window, stop_at=stop).endswith(WARNING)


@pytest.mark.parametrize("window,reserve", [(250_000, 0), (65_536, 13_107), (262_144, 16_384)])
def test_the_stop_point_is_the_guards_own(window, reserve):
    stop = context_stop_at(window, reserve)

    def fires(used):
        ctx = HookContext(agent_id="a", task_id="t", session_id="s", round_idx=3)
        ctx.state.context_window, ctx.state.context_reserve = window, reserve
        ctx.state.last_prompt_tokens = used
        return asyncio.run(ContextLimitGuard().between_rounds(ctx)) is HookAction.STOP_LOOP

    assert fires(stop + 1) and not fires(stop - 1)


def test_nothing_is_said_without_both_numbers():
    assert context_budget_line(3, 10_000, 0) is None
    assert context_budget_line(3, 0, 250_000) is None
    assert context_budget_line(0, 10_000, 250_000) is None


def test_the_line_goes_on_the_last_result_after_the_last_call():
    told = {"role": "user", "content": "told", "_runtime_note": True}
    first = {"role": "tool", "tool_call_id": "a", "content": "one"}
    last = {"role": "tool", "tool_call_id": "b", "content": "two"}
    msgs = [{"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}]}, first, last, told]
    assert _with_context_line(msgs, "[L]")
    assert msgs[2] == {**last, "content": "two\n\n[L]", "_context_line": "[L]"}
    assert msgs[1] is first and msgs[3] is told and last["content"] == "two", "copied, not edited"
    assert not _with_context_line([{"role": "user", "content": "x"}, {"role": "assistant"}], "[L]")


# --- in the loop ----------------------------------------------------------------------------


def test_after_a_tool_round_the_result_ends_with_the_line(tmp_path, monkeypatch):
    llm = _Llm(tool_rounds=2, measured=49_000)
    seen = []
    _run(tmp_path, monkeypatch, llm, _Tools([400, 400]), {"context_window": 250_000}, guard_seen=seen)

    assert _lines(llm.requests[0]) == [], "round one follows no tool round"
    for k in (1, 2):
        request = llm.requests[k]
        last = request[-1]
        assert last["role"] == "tool" and last["content"].endswith("\n\n" + last["_context_line"])
        # The loop's own projection, the number the guard read: the measured prompt plus what
        # the round appended (without the line, which is put on after the projection).
        appended = _without_lines(request[len(llm.requests[k - 1]):])
        expected = 49_000 + estimate_messages_tokens(appended)
        assert seen[k] == (expected, 250_000)
        assert last["_context_line"] == context_budget_line(k, expected, 250_000)
        assert LINE.fullmatch(last["_context_line"]).group(1) == str(k)
    assert WARNING not in llm.requests[2][-1]["content"]


def test_the_earlier_messages_of_the_next_request_are_unchanged(tmp_path, monkeypatch):
    llm = _Llm(tool_rounds=4, measured=30_000)
    _run(tmp_path, monkeypatch, llm, _Tools([300] * 4), {"context_window": 250_000})

    for before, after in zip(llm.requests, llm.requests[1:]):
        assert after[:len(before)] == before, "the line rewrote the prefix the engine holds"
    assert [len(_lines(r)) for r in llm.requests] == [0, 1, 2, 3, 4]


def _wire(request):
    """The request as llama-server receives it: the adapter's Anthropic shape, then the
    shared OpenAI converter."""
    system, turns = DpcLlmAdapter._convert_messages_to_anthropic(
        [{"role": "system", "content": "sys"}] + request)
    return anthropic_to_openai_messages(system, turns)


def test_the_turn_base_a_kv_snapshot_hashes_does_not_move(tmp_path, monkeypatch):
    llm = _Llm(tool_rounds=3, measured=30_000)
    _run(tmp_path, monkeypatch, llm, _Tools([300] * 3), {"context_window": 250_000})

    for request in llm.requests[1:]:
        wire, base = _wire(request), _wire(_without_lines(request))
        assert [m["role"] for m in wire] == [m["role"] for m in base]
        assert kv_snapshots.base_end(wire) == kv_snapshots.base_end(base) == 2  # system, "go"
        assert wire[-1]["role"] == "tool" and wire[-1]["content"].endswith(request[-1]["_context_line"])
        assert kv_snapshots.prefix_digest(wire, 2) == kv_snapshots.prefix_digest(base, 2)


def test_the_line_warns_near_the_limit(tmp_path, monkeypatch):
    llm = _Llm(tool_rounds=1, measured=8_500)
    _run(tmp_path, monkeypatch, llm, _Tools([100]), {"context_window": 10_000})
    assert llm.requests[1][-1]["content"].endswith(WARNING)


def test_after_a_cut_the_line_is_the_guards_size_and_no_placeholder_keeps_an_old_line(
        tmp_path, monkeypatch):
    seen = []
    llm = _Llm(tool_rounds=9, measured=None)
    _run(tmp_path, monkeypatch, llm, _Tools([3_000] * 8 + [40_000]),
         {"context_window": 12_000}, guard_seen=seen)

    placeholders = [m for m in llm.requests[-1]
                    if m.get("_compacted") and m.get("role") == "tool"
                    and m["content"].startswith("[Cut from context")]
    assert placeholders, "the scenario cuts"
    assert all("[Context budget" not in m["content"] for m in placeholders)
    for k, request in enumerate(llm.requests[1:], start=1):
        match = LINE.fullmatch(request[-1]["_context_line"])
        assert (int(match.group(2).replace(",", "")), 12_000) == seen[k]


def test_a_summary_is_made_of_the_output_and_not_of_an_old_line():
    asked = []

    class _Manager:
        async def query(self, prompt, provider_alias=None):
            asked.append(prompt)
            return "summary"

    line = context_budget_line(1, 9_000, 250_000)
    msgs = [{"role": "user", "content": "go"}]
    for n in range(3):
        msgs += [_call(n), {"role": "tool", "tool_call_id": f"c{n}", "content": "y" * 3_000}]
    head = msgs[:3]
    assert _with_context_line(head, line)  # the first round's result, as the loop leaves it
    msgs[2] = head[2]

    out = asyncio.run(compact_tool_history_llm(msgs, _Manager(), None, keep_recent=1))
    assert len(asked) == 2 and all("[Context budget" not in p for p in asked)
    assert out[2]["content"] == "summary"


def test_no_line_for_a_window_nobody_knows(tmp_path, monkeypatch):
    # What production hands over when the window is unknown: no context_window in the config,
    # no alias that names one, and agent.py's `tokens_limit or 204800` as the caller's figure.
    llm = _Llm(tool_rounds=2, measured=20_000)
    _run(tmp_path, monkeypatch, llm, _Tools([100, 100]), {}, context_window=204_800)
    assert all(_lines(r) == [] for r in llm.requests)
    assert all("[Context budget" not in str(m.get("content")) for r in llm.requests for m in r)


def test_the_line_window_is_the_guards_window(tmp_path, monkeypatch):
    seen = []
    llm = _Llm(tool_rounds=1, measured=20_000)
    _run(tmp_path, monkeypatch, llm, _Tools([100]), {"context_window": 131_072},
         context_window=204_800, guard_seen=seen)
    assert LINE.fullmatch(_lines(llm.requests[1])[0]).group(3) == f"{seen[1][1]:,}" == "131,072"


def test_a_callers_figure_the_guard_does_not_enforce_is_not_said(tmp_path, monkeypatch):
    # No window in the config or the alias: the guard enforces the compaction state's fallback,
    # so a line with the caller's 131,072 would say one window while the guard keeps another.
    seen = []
    llm = _Llm(tool_rounds=1, measured=20_000)
    _run(tmp_path, monkeypatch, llm, _Tools([100]), {}, context_window=131_072, guard_seen=seen)
    assert seen[1][1] == 204_800 and _lines(llm.requests[1]) == []


def test_compaction_on_still_gets_the_line(tmp_path, monkeypatch):
    llm = _Llm(tool_rounds=2, measured=10_000)
    _run(tmp_path, monkeypatch, llm, _Tools([100, 100]),
         {"context_window": 100_000, "compaction_enabled": True})
    assert [len(_lines(r)) for r in llm.requests] == [0, 1, 2]


# --- every provider path carries it inside the tool result ----------------------------------


_LINE = context_budget_line(1, 49_412, 250_000)
_ROUND = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "go"},
    _call(1),
    {"role": "tool", "tool_call_id": "c1", "content": "result 1\n\n" + _LINE, "_context_line": _LINE},
]


def test_the_text_path_renders_the_line_inside_the_result():
    assert messages_to_prompt(_ROUND).endswith("[TOOL RESULT: c1]\nresult 1\n\n" + _LINE)


def test_the_anthropic_shape_carries_the_line_in_the_tool_result_block():
    _, turns = DpcLlmAdapter._convert_messages_to_anthropic(_ROUND)
    assert [t["role"] for t in turns] == ["user", "assistant", "user"]
    assert [b["type"] for b in turns[-1]["content"]] == ["tool_result"]
    assert turns[-1]["content"][0]["content"] == "result 1\n\n" + _LINE


def test_the_openai_shape_carries_the_line_in_the_tool_message():
    wire = _wire(_ROUND[1:])
    assert [m["role"] for m in wire] == ["system", "user", "assistant", "tool"]
    assert wire[-1]["content"] == "result 1\n\n" + _LINE


def test_a_context_result_re_cut_for_a_peer_keeps_the_line():
    from dpc_client_core.dpc_agent.tools.core import PEER_CONTEXT_WITHHELD

    msgs = [{"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function",
             "function": {"name": "get_dpc_context", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "private\n\n" + _LINE, "_context_line": _LINE}]
    out = DpcLlmAdapter._messages_for_peer(object(), msgs, "peer-x", None)
    assert out[1]["content"] == f"{PEER_CONTEXT_WITHHELD}\n\n{_LINE}"


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
        "measured before this turn; after each tool round a live context line follows "
        "when the window is known")


def test_no_note_when_nothing_was_measured(tmp_path):
    session = _session({"tokens_limit": 250_000, "tokens_after_last_response": 0}, tmp_path)
    assert session["tokens_after_last_response"] is None
    assert "note" not in session and "measured_before_this_turn" not in session
