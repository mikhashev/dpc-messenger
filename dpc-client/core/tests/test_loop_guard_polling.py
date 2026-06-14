"""LoopGuard polling-tool exception + comfyui_progress timing.

Covers the long-generation fix: LoopGuard must not kill an agent that is
legitimately polling a slow render (output keeps advancing), while still
stopping a genuinely-stuck poll (output unchanged). Also covers the
per-iteration timing helper that feeds the agent sec/it + ETA.
"""

from __future__ import annotations

import pytest

from dpc_client_core.dpc_agent.hooks import HookAction, HookContext, LoopState
from dpc_client_core.dpc_agent.guards import LoopGuard
from dpc_client_core.dpc_agent.tools.comfyui import (
    _format_progress_timing,
    _LAST_PROGRESS,
)

POLL = "comfyui_progress"
POLL_ARGS = {"timeout": 30}


def poll_ctx(prev_output=None, call_name: str = POLL, call_args=None):
    """A round that calls ``call_name`` and whose just-executed result
    (from the previous round) is ``prev_output`` on ``call_name``.
    ``prev_output=None`` means no executed result yet (first round).
    """
    state = {"recent_tool_args": [{"name": call_name, "args": call_args or POLL_ARGS}]}
    if prev_output is not None:
        state["recent_tool_results"] = [{"name": call_name, "output": prev_output}]
    ctx = HookContext(
        agent_id="t", task_id="t", session_id="t", round_idx=1, state=LoopState()
    )
    for k, v in state.items():
        setattr(ctx.state, k, v)
    return ctx


class TestLoopGuardPolling:
    @pytest.mark.asyncio
    async def test_first_poll_no_reset(self):
        # No prior result -> nothing to compare, just counts normally, no stop.
        g = LoopGuard(max_duplicate_calls=5)
        assert await g.after_llm_call(poll_ctx(prev_output=None)) is None
        assert max(g._counts.values()) == 1

    @pytest.mark.asyncio
    async def test_advancing_poll_never_stops(self):
        # Each poll returns a new step -> counter keeps resetting -> never fires,
        # even far beyond max_duplicate_calls rounds.
        g = LoopGuard(max_duplicate_calls=5)
        assert await g.after_llm_call(poll_ctx(prev_output=None)) is None
        for step in range(1, 16):
            res = await g.after_llm_call(poll_ctx(prev_output=f"progress: step {step}/30"))
            assert res is None
        assert max(g._counts.values()) <= 1

    @pytest.mark.asyncio
    async def test_stuck_poll_stops_at_max(self):
        # Identical output every poll -> counter climbs -> stops at the cap.
        g = LoopGuard(max_duplicate_calls=5)
        results = [await g.after_llm_call(poll_ctx(prev_output=None))]
        for _ in range(8):
            results.append(await g.after_llm_call(poll_ctx(prev_output="progress: step 9/30")))
        first_stop = next((i for i, r in enumerate(results) if r is not None), None)
        # seed round counts to 1, then 4 identical climbs reach the cap of 5.
        assert first_stop == 4
        assert results[first_stop] == HookAction.STOP_LOOP

    @pytest.mark.asyncio
    async def test_non_polling_tool_not_reset_by_output_change(self):
        # The reset is ONLY for polling tools. A normal tool whose result text
        # happens to change must still trip the guard at the cap.
        g = LoopGuard(max_duplicate_calls=3)
        res = None
        for i in range(3):
            ctx = poll_ctx(
                prev_output=f"different output {i}",
                call_name="read_file",
                call_args={"path": "a.txt"},
            )
            # rename the result tool too so it is the non-polling 'read_file'
            ctx.state.recent_tool_results = [{"name": "read_file", "output": f"different output {i}"}]
            res = await g.after_llm_call(ctx)
        assert res == HookAction.STOP_LOOP


class TestProgressTiming:
    def setup_method(self):
        _LAST_PROGRESS.clear()

    def test_single_sample_no_prev_has_no_rate(self):
        out = _format_progress_timing([(5, 30, 100.0)], "url")
        assert "step 5/30" in out
        assert "~" not in out  # no rate/ETA when there's nothing to measure
        assert "poll again" in out

    def test_cross_call_rate(self):
        # First call seeds (5 @ t=100), second call (6 @ t=128) -> 28 s/it.
        _format_progress_timing([(5, 30, 100.0)], "url")
        out = _format_progress_timing([(6, 30, 128.0)], "url")
        assert "~28.0s/it" in out
        assert "ETA ~11m12s" in out  # (30-6)*28 = 672s

    def test_two_in_call_samples_rate(self):
        out = _format_progress_timing([(1, 30, 0.0), (3, 30, 40.0)], "url")
        assert "~20.0s/it" in out  # 40s / 2 steps
        assert "ETA ~9m00s" in out  # (30-3)*20 = 540s

    def test_new_generation_resets_rate(self):
        # Prev sample at step 30; new gen at step 1 -> negative delta -> no rate.
        _format_progress_timing([(30, 30, 100.0)], "url")
        out = _format_progress_timing([(1, 30, 200.0)], "url")
        assert "~" not in out  # negative step delta -> no rate
        assert "poll again" in out

    def test_empty_samples_returns_blank(self):
        assert _format_progress_timing([], "url") == ""
