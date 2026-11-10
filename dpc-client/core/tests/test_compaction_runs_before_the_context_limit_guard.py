"""The round's cut acts on the size the next request would have, before the guard reads it.

Loop audit 2026-11-09: BETWEEN_ROUNDS fired ContextLimitGuard first and `apply_compaction` after
it, and both read the previous request's measured size, which holds none of the tool results the
round just added. One oversized result got through to the engine. ADR-045 decision 6 says
compaction acts and `ContextLimitGuard` is the last line, so the loop now projects the next prompt
(measured size plus an estimate of what was appended), cuts at that size, takes the cut's savings
off, and only then lets the guard read it.

Also here: the guard's stop text names what was true. Compaction is off for every agent on the
node, and "compaction could not reduce it further" was told to a run that never had a summariser
(Ark, 2026-11-06, 95.4% of 250000).
"""
import asyncio

import pytest

from dpc_client_core.dpc_agent import loop as loop_module
from dpc_client_core.dpc_agent.context import estimate_messages_tokens
from dpc_client_core.dpc_agent.guards import ContextLimitGuard
from dpc_client_core.dpc_agent.loop import run_llm_loop

WINDOW = 5_000
MEASURED = 1_200  # the engine's count for every request the fake answers


class _Tools:
    """One tool, `noop`, whose result is whatever the script says for that call."""

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
    """Asks for one tool call per request while `tool_rounds` last, then answers."""

    def __init__(self, tool_rounds, measured=MEASURED):
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


def _run(tmp_path, monkeypatch, llm, tools, config=None, spy_compaction=True, compaction_llm=None):
    config = {"context_window": WINDOW} if config is None else config
    monkeypatch.setattr(loop_module, "load_agent_config", lambda _name: config)
    if compaction_llm is not None:
        monkeypatch.setattr(loop_module, "_llm_manager_for_agent", lambda *a, **k: compaction_llm)
    events = []
    real_cut = loop_module.apply_compaction
    real_guard = ContextLimitGuard.between_rounds

    async def _cut(messages, *, last_prompt_tokens, **kwargs):
        events.append(("cut", last_prompt_tokens))
        return await real_cut(messages, last_prompt_tokens=last_prompt_tokens, **kwargs)

    async def _guard(self, ctx):
        events.append(("guard", ctx.last_prompt_tokens))
        return await real_guard(self, ctx)

    if spy_compaction:
        monkeypatch.setattr(loop_module, "apply_compaction", _cut)
    monkeypatch.setattr(ContextLimitGuard, "between_rounds", _guard)
    text, usage, trace = asyncio.run(run_llm_loop(
        messages=[{"role": "user", "content": "go"}], tools=tools, llm=llm,
        agent_root=tmp_path / "agent", emit_progress=lambda *a, **k: None, task_id="t",
    ))
    return text, trace, events


# Eight earlier rounds with results long enough for a cut to replace (> 2000 chars), then a ninth
# whose result is the most the loop lets through (15000 chars, ~3.8k tokens) against a 5000 window.
_EARLY = [3_000] * 8


def test_one_oversized_result_is_cut_for_before_the_guard_reads_the_size(tmp_path, monkeypatch):
    llm = _Llm(tool_rounds=9)
    tools = _Tools(_EARLY + [40_000])  # capped by the loop at 15000 chars
    text, trace, events = _run(tmp_path, monkeypatch, llm, tools)

    # Round 10 is the first whose request holds the oversized result.
    cut_for_round_10 = [e for e in events if e[0] == "cut"][9][1]
    assert cut_for_round_10 >= MEASURED + 3_500, "the cut was told the stale size, not the projection"
    # The cut came first in every round, then the guard.
    order = [e[0] for e in events]
    assert order == ["cut", "guard"] * (len(order) // 2)
    # The old results went, so the request that left carries the new one whole and fits.
    last_request = llm.requests[-1]
    assert sum(1 for m in last_request if m.get("_compacted")) >= 7
    assert "[CONTEXT_LIMIT]" not in text and text.startswith("Done.")


def test_the_guard_reads_the_projected_size_with_the_new_tool_results(tmp_path, monkeypatch):
    llm = _Llm(tool_rounds=9)
    tools = _Tools([100] * 8 + [80_000])  # the earlier results are too short for a cut to take anything
    text, trace, events = _run(tmp_path, monkeypatch, llm, tools)

    guard_read = [e[1] for e in events if e[0] == "guard"]
    assert guard_read[-1] >= 0.95 * WINDOW
    assert text.startswith("[CONTEXT_LIMIT]") and trace["turn_end"] == "guard"
    assert len(llm.requests) == 9, "the request holding the oversized result was sent"


def test_the_projection_is_the_measured_size_plus_what_the_round_appended(tmp_path, monkeypatch):
    llm = _Llm(tool_rounds=2)
    tools = _Tools([100, 100])
    _, _, events = _run(tmp_path, monkeypatch, llm, tools)

    cuts = [e[1] for e in events if e[0] == "cut"]
    appended = llm.requests[1][len(llm.requests[0]):]  # the reply and the result of round one
    assert cuts[0] == 0, "round one has no measured size and is not projected"
    assert cuts[1] == MEASURED + estimate_messages_tokens(appended)
    assert cuts[1] > MEASURED


def test_the_savings_of_the_cut_come_off_before_the_guard_reads_it(tmp_path, monkeypatch):
    llm = _Llm(tool_rounds=9)
    tools = _Tools(_EARLY + [40_000])
    _, _, events = _run(tmp_path, monkeypatch, llm, tools)

    cut_told = [e[1] for e in events if e[0] == "cut"][9]
    guard_read = [e[1] for e in events if e[0] == "guard"][9]
    assert guard_read < cut_told
    assert guard_read < 0.95 * WINDOW <= cut_told


def test_a_round_far_below_the_limits_is_left_alone(tmp_path, monkeypatch):
    llm = _Llm(tool_rounds=3)
    tools = _Tools([300, 300, 300])
    text, trace, events = _run(tmp_path, monkeypatch, llm, tools)

    assert text.startswith("Done.")
    assert all(not m.get("_compacted") for request in llm.requests for m in request)
    # No cut: the request holds what the earlier one held and what the round appended.
    assert llm.requests[2][:len(llm.requests[1])] == llm.requests[1]
    assert max(e[1] for e in events) < 0.3 * WINDOW


# --- the stop text ---------------------------------------------------------------------------------------------------

def _stopped_text(tmp_path, monkeypatch, config, compaction_llm=None, keep_the_list=False):
    llm = _Llm(tool_rounds=2, measured=int(WINDOW * 0.99))
    tools = _Tools([100, 100])
    if keep_the_list:
        async def _a_cut_that_is_not_enough(messages, **kwargs):
            return list(messages)  # a new list: the pass changed the prompt, the guard still fires
        monkeypatch.setattr(loop_module, "apply_compaction", _a_cut_that_is_not_enough)
    text, _, _ = _run(tmp_path, monkeypatch, llm, tools, config=config,
                      spy_compaction=not keep_the_list, compaction_llm=compaction_llm)
    return text


def test_with_compaction_off_the_stop_text_says_there_is_no_summariser(tmp_path, monkeypatch):
    text = _stopped_text(tmp_path, monkeypatch, {"context_window": WINDOW})

    assert text.startswith("[CONTEXT_LIMIT]")
    assert "compaction is off for this agent" in text and "no summariser" in text
    assert "turn compaction on" in text
    assert "could not reduce it further" not in text


def test_with_compaction_on_the_stop_text_says_the_cut_did_not_bring_it_under(tmp_path, monkeypatch):
    text = _stopped_text(
        tmp_path, monkeypatch, {"context_window": WINDOW, "compaction_enabled": True},
        compaction_llm=object(), keep_the_list=True)

    assert text.startswith("[CONTEXT_LIMIT]")
    assert "compaction cut the old tool results and could not bring it under" in text
    assert "compaction is off" not in text and "turn compaction on" not in text


def test_enabled_without_a_model_to_summarise_with_is_told_as_off(tmp_path, monkeypatch):
    text = _stopped_text(tmp_path, monkeypatch, {"context_window": WINDOW, "compaction_enabled": True})

    assert "compaction is off for this agent" in text


@pytest.mark.parametrize("compaction_on, said", [
    (None, "the cut of old tool results could not bring it under"),
    (True, "compaction cut the old tool results and could not bring it under"),
    (False, "compaction is off for this agent, so there is no summariser"),
])
def test_the_guards_own_text_in_each_setting(compaction_on, said):
    guard = ContextLimitGuard(ratio=0.95, compaction_on=compaction_on)
    guard._seen_ratio = 0.97

    message = guard.stop_message()
    assert message.startswith("[CONTEXT_LIMIT] The next request would be 97% of the model's context window")
    assert said in message
