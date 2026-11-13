"""The context number on the strip fell between rounds and nothing said why.

Live 2026-11-13: 157,463 -> 146,607 between rounds 41 and 46, a context cut with no
word on the screen. `apply_compaction` now records the run's last cut on its
`CompactionState` as `last_context_cut` — the round, why (`rounds`: the
compaction-off round bound; `pressure`: at or above `UNDER_PRESSURE`; `summary`:
the summariser ran), what it freed by the chars/4 estimate and how many rounds it
kept — and the loop carries it on every round's strip until the next cut. A
skipped cut changes nothing.
"""

from __future__ import annotations

import asyncio

from dpc_client_core.dpc_agent import loop as loop_mod
from dpc_client_core.dpc_agent.context import (
    EMERGENCY_KEEP, OFF_ROUND_BOUND, UNDER_PRESSURE, CompactionState, apply_compaction,
)
from dpc_client_core.dpc_agent.loop import round_progress_payload, run_llm_loop

WINDOW = 250_000


class _Summariser:
    def __init__(self):
        self.calls = 0

    async def query(self, prompt, provider_alias=None, **kwargs):
        self.calls += 1
        return f"SUMMARY {self.calls}"


def _history(sizes):
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    for r, size in enumerate(sizes):
        msgs += [
            {"role": "assistant", "content": f"step {r}", "tool_calls": [{"id": f"t{r}"}]},
            {"role": "tool", "tool_call_id": f"t{r}", "content": f"{r}:" + "X" * size},
        ]
    return msgs


def _state(enabled):
    return CompactionState({"compaction_enabled": enabled, "compaction_threshold": 0.5,
                            "context_window": WINDOW})


def _run(messages, state, ratio, manager, round_idx):
    return asyncio.run(apply_compaction(
        messages, state=state, last_prompt_tokens=int(ratio * WINDOW),
        llm_manager=manager, round_idx=round_idx,
    ))


class TestACutIsRecorded:

    def test_nothing_before_the_first_cut(self):
        assert _state(False).last_context_cut is None

    def test_the_compaction_off_round_bound_is_rounds(self):
        state = _state(False)
        _run(_history([7000] * 6 + [100] * (OFF_ROUND_BOUND - 6)), state, 0.1, None, 46)
        cut = state.last_context_cut
        assert (cut["round"], cut["reason"], cut["kept_rounds"]) == (46, "rounds", 6)
        assert cut["freed_tokens"] > 9000      # six 7,000-char results, chars/4

    def test_at_the_pressure_line_it_is_pressure(self):
        state = _state(False)
        _run(_history([7000] * 10), state, UNDER_PRESSURE + 0.02, None, 12)
        cut = state.last_context_cut
        assert (cut["round"], cut["reason"], cut["kept_rounds"]) == (12, "pressure", EMERGENCY_KEEP)
        assert cut["freed_tokens"] > 0

    def test_the_summariser_is_summary(self):
        state, lm = _state(True), _Summariser()
        state.compacting = True
        _run(_history([7000] * 6 + [100] * 6), state, 0.32, lm, 20)
        cut = state.last_context_cut
        assert lm.calls > 0
        assert (cut["round"], cut["reason"], cut["kept_rounds"]) == (20, "summary", 6)
        assert cut["freed_tokens"] > 0


class TestASkippedCutChangesNothing:

    def test_a_cut_too_small_to_pay_keeps_the_last_one(self):
        state = _state(False)
        _run(_history([7000] * 6 + [100] * (OFF_ROUND_BOUND - 6)), state, 0.1, None, 46)
        recorded = dict(state.last_context_cut)
        small = _history([100] * OFF_ROUND_BOUND)       # the size guard skips this one
        assert _run(small, state, 0.1, None, 50) == small
        assert state.last_context_cut == recorded

    def test_no_cut_due_records_none(self):
        state = _state(False)
        msgs = _history([7000] * 4)
        assert _run(msgs, state, 0.1, None, 5) is msgs
        assert state.last_context_cut is None


class TestTheStripCarriesIt:

    CUT = {"round": 46, "reason": "rounds", "freed_tokens": 10856, "kept_rounds": 6}

    def test_the_payload_carries_the_last_cut(self):
        payload = round_progress_payload(
            None, round_idx=47, prompt_tokens=146607, context_window=WINDOW,
            context_reserve=None, last_context_cut=self.CUT,
        )
        assert payload["last_context_cut"] == self.CUT
        assert payload["last_context_cut"] is not self.CUT

    def test_absent_before_the_first_cut(self):
        payload = round_progress_payload(
            None, round_idx=3, prompt_tokens=1000, context_window=WINDOW, context_reserve=None,
        )
        assert "last_context_cut" not in payload

    def test_it_reaches_the_callback_the_ui_listens_on(self, tmp_path, monkeypatch):
        cut = self.CUT

        class _CutState(CompactionState):
            def __init__(self, cfg):
                super().__init__(cfg)
                self.last_context_cut = dict(cut)

        monkeypatch.setattr(loop_mod, "CompactionState", _CutState)
        monkeypatch.setattr(loop_mod, "load_agent_config", lambda _name: {})
        strips = []

        def emit(message, tool=None, round=None, tool_calls=None, speed=None):
            if speed:
                strips.append(speed)

        class _Llm:
            async def chat(self, messages, tools=None, **kwargs):
                return {"content": "done", "tool_calls": []}, {
                    "prompt_tokens": 3030, "completion_tokens": 10, "total_tokens": 3040}

        class _Tools:
            _ctx = None

            def schemas(self, core_only=False, include_restricted=False):
                return []

        response, _u, _t = asyncio.run(run_llm_loop(
            messages=[{"role": "user", "content": "hi"}], tools=_Tools(), llm=_Llm(),
            agent_root=tmp_path, emit_progress=emit, context_window=WINDOW,
        ))
        assert response == "done"
        assert strips and strips[-1]["last_context_cut"] == cut
