"""A planned cut whose replaceable results are small is skipped, not paid for.

Live 2026-10-22 12:47:48: usage 32%, hysteresis stuck on (the release level of a
250k window is never reached), a batch of six rounds due, and the only thing to
replace was one 4,092-char result. The prompt went 80k -> 80.4k tokens, the prefix
cache broke from message 27 and ~17k tokens were read again, plus a summariser call.
The guard (`CUT_MIN_FREED_TOKENS`) skips such a cut on the normal path; the uncut
rounds keep piling up, so the same test runs on a bigger batch next round. The
pressure path is never held back.
"""

from __future__ import annotations

import asyncio
import logging

from dpc_client_core.dpc_agent import context as ctx_mod
from dpc_client_core.dpc_agent.context import (
    CUT_MIN_BATCH, CUT_MIN_FREED_TOKENS, EMERGENCY_KEEP, OFF_ROUND_BOUND, UNDER_PRESSURE,
    CompactionState, apply_compaction, freeable_tokens, uncut_rounds,
)

WINDOW = 250_000


class _Summariser:
    def __init__(self):
        self.calls = 0

    async def query(self, prompt, provider_alias=None, **kwargs):
        self.calls += 1
        return f"SUMMARY {self.calls}"


def _round(r, size):
    return [
        {"role": "assistant", "content": f"step {r}", "tool_calls": [{"id": f"t{r}"}]},
        {"role": "tool", "tool_call_id": f"t{r}", "content": f"{r}:" + "X" * size},
    ]


def _history(sizes):
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    for r, size in enumerate(sizes):
        msgs += _round(r, size)
    return msgs


def _stuck_on(enabled=True):
    """Compaction on, usage between release (0.3) and threshold (0.5): the live state."""
    state = CompactionState({"compaction_enabled": enabled, "compaction_threshold": 0.5,
                             "context_window": WINDOW})
    state.compacting = True
    return state


def _run(messages, state, ratio, manager, round_idx=19):
    return asyncio.run(apply_compaction(
        messages, state=state, last_prompt_tokens=int(ratio * WINDOW),
        llm_manager=manager, round_idx=round_idx,
    ))


def _live_shape():
    """Twelve rounds, a batch (6) due, one 4,092-char result among small ones."""
    sizes = [100] * 12
    sizes[2] = 4092
    return _history(sizes)


def test_the_live_shape_is_not_cut(caplog):
    msgs = _live_shape()
    assert uncut_rounds(msgs) - 6 >= CUT_MIN_BATCH
    assert freeable_tokens(msgs, 6) < CUT_MIN_FREED_TOKENS
    state, lm = _stuck_on(), _Summariser()
    before = [dict(m) for m in msgs]
    with caplog.at_level(logging.INFO, logger="dpc_client_core.dpc_agent.context"):
        out = _run(msgs, state, 0.32, lm)
    assert out == before and lm.calls == 0
    assert state.compacting is True            # hysteresis untouched by a skip
    lines = [r.getMessage() for r in caplog.records]
    assert any(l.startswith("context cut skipped: round=19 would free ~") for l in lines)
    assert not any(l.startswith("context cut:") for l in lines)


def test_enough_to_free_is_cut_as_before():
    msgs = _history([7000] * 6 + [100] * 6)      # ~10k tokens replaceable
    assert freeable_tokens(msgs, 6) >= CUT_MIN_FREED_TOKENS
    lm = _Summariser()
    out = _run(msgs, _stuck_on(), 0.32, lm)
    assert lm.calls == 6
    assert uncut_rounds(out) == 6


def test_pressure_cuts_even_when_little_is_replaceable(caplog):
    msgs = _live_shape()
    state, lm = _stuck_on(), _Summariser()
    with caplog.at_level(logging.INFO, logger="dpc_client_core.dpc_agent.context"):
        out = _run(msgs, state, UNDER_PRESSURE + 0.02, lm)
    assert uncut_rounds(out) == EMERGENCY_KEEP and lm.calls == 1
    assert not any("skipped" in r.getMessage() for r in caplog.records)


def test_a_skip_keeps_piling_up_and_the_cut_fires_when_the_batch_is_worth_it(caplog):
    state, lm = _stuck_on(), _Summariser()
    msgs = _live_shape()
    with caplog.at_level(logging.DEBUG, logger="dpc_client_core.dpc_agent.context"):
        # three more rounds with small results: still skipped, and logged at INFO once
        for r in range(12, 15):
            msgs = _run(msgs, state, 0.32, lm, round_idx=r)
            msgs += _round(r, 100)
        assert lm.calls == 0 and uncut_rounds(msgs) == 15
        # large results arrive, then age out of the kept tail behind six more rounds
        for r in range(15, 21):
            msgs += _round(r, 7000)
        for r in range(21, 27):
            msgs += _round(r, 100)
        out = _run(msgs, state, 0.32, lm, round_idx=27)
    assert lm.calls > 0 and uncut_rounds(out) == 6
    infos = [r for r in caplog.records
             if r.levelno == logging.INFO and r.getMessage().startswith("context cut skipped")]
    assert len(infos) == 1
    assert sum("context cut skipped" in r.getMessage() for r in caplog.records) == 3
    assert state.skip_logged is False          # a real cut re-arms the INFO line


def test_the_compaction_off_round_bound_is_guarded_too():
    sizes = [100] * OFF_ROUND_BOUND
    sizes[1] = 4092
    msgs = _history(sizes)
    state = _stuck_on(enabled=False)
    state.compacting = False
    assert _run(msgs, state, 0.1, None) == msgs
    # with real material the bound still cuts
    big = _history([7000] * 6 + [100] * (OFF_ROUND_BOUND - 6))
    assert uncut_rounds(_run(big, state, 0.1, None)) == 6
