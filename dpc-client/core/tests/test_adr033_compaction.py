"""ADR-033 tool-history compaction — unit + integration coverage.

Covers the incremental summarizer (`compact_tool_history_llm`) and the loop decision
logic (`CompactionState` + `apply_compaction`): toggle, window trigger, hysteresis,
strategy fallback ladder, circuit breaker, and notification.
"""
import asyncio

from dpc_client_core.dpc_agent.context import (
    CompactionState, apply_compaction, compact_tool_history_llm, _KEEP_ASIS_CHARS,
)


class _FakeLLM:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    async def query(self, prompt, provider_alias=None, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("simulated failure")
        return "SUMMARY"


def _messages(n_rounds, big=True):
    msgs = []
    for r in range(n_rounds):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"t{r}"}]})
        size = _KEEP_ASIS_CHARS + 500 if big else 50
        msgs.append({"role": "tool", "tool_call_id": f"t{r}", "content": "X" * size})
    return msgs


# --- compact_tool_history_llm (incremental summarizer) ---

def test_summarizes_old_big_results_keeps_recent():
    async def run():
        lm = _FakeLLM()
        out = await compact_tool_history_llm(_messages(10), lm, "flash", keep_recent=6)
        assert lm.calls == 4  # 10 rounds - 6 kept = 4 old summarized
        recent = [m for m in out if m.get("role") == "tool" and not m.get("_compacted")]
        assert len(recent) == 6
    asyncio.run(run())


def test_small_results_kept_verbatim_no_call():
    async def run():
        lm = _FakeLLM()
        out = await compact_tool_history_llm(_messages(10, big=False), lm, "flash", keep_recent=6)
        assert lm.calls == 0
        marked = [m for m in out if m.get("role") == "tool" and m.get("_compacted")]
        assert len(marked) == 4 and all(len(str(m["content"])) == 50 for m in marked)
    asyncio.run(run())


def test_incremental_skips_already_compacted():
    async def run():
        lm = _FakeLLM()
        out = await compact_tool_history_llm(_messages(10), lm, "flash", keep_recent=6)
        lm2 = _FakeLLM()
        await compact_tool_history_llm(out, lm2, "flash", keep_recent=6)
        assert lm2.calls == 0
    asyncio.run(run())


def test_below_keep_recent_unchanged():
    async def run():
        lm = _FakeLLM()
        msgs = _messages(5)
        out = await compact_tool_history_llm(msgs, lm, "flash", keep_recent=6)
        assert out is msgs and lm.calls == 0
    asyncio.run(run())


def test_raises_on_model_failure():
    async def run():
        lm = _FakeLLM(fail=True)
        try:
            await compact_tool_history_llm(_messages(10), lm, "flash", keep_recent=6)
            assert False, "expected exception"
        except RuntimeError:
            pass
    asyncio.run(run())


# --- CompactionState + apply_compaction (loop decision logic) ---

def test_state_config_and_defaults():
    st = CompactionState({})
    assert st.enabled is False and abs(st.threshold - 0.8) < 1e-9
    assert abs(st.release - 0.6) < 1e-9 and st.window == 204800
    st2 = CompactionState({"compaction_enabled": True, "compaction_provider": "p",
                           "compaction_threshold": 0.7, "context_window": 1000})
    assert st2.enabled and st2.provider == "p" and st2.window == 1000
    assert abs(st2.release - 0.5) < 1e-9


def test_toggle_off_uses_round_count_truncation():
    async def run():
        st = CompactionState({"compaction_enabled": False})
        lm = _FakeLLM()
        msgs = _messages(12)
        out = await apply_compaction(msgs, state=st, last_prompt_tokens=999999, llm_manager=lm, round_idx=5)
        assert out is msgs and lm.calls == 0
        out = await apply_compaction(msgs, state=st, last_prompt_tokens=999999, llm_manager=lm, round_idx=9)
        assert lm.calls == 0
        assert any(len(str(m.get("content", ""))) <= 205 for m in out if m.get("role") == "tool")
    asyncio.run(run())


def test_trigger_and_hysteresis():
    async def run():
        st = CompactionState({"compaction_enabled": True, "context_window": 1000})
        lm = _FakeLLM()
        # below threshold: no compaction
        out = await apply_compaction(_messages(12), state=st, last_prompt_tokens=500, llm_manager=lm, round_idx=1)
        assert st.compacting is False and lm.calls == 0
        # cross 0.8: fires
        await apply_compaction(_messages(12), state=st, last_prompt_tokens=850, llm_manager=lm, round_idx=2)
        assert st.compacting is True and lm.calls > 0
        # hold in deadband
        await apply_compaction(_messages(12), state=st, last_prompt_tokens=700, llm_manager=lm, round_idx=3)
        assert st.compacting is True
        # drop below release: exit
        await apply_compaction(_messages(12), state=st, last_prompt_tokens=550, llm_manager=lm, round_idx=4)
        assert st.compacting is False
    asyncio.run(run())


def test_ladder_and_circuit_breaker():
    async def run():
        st = CompactionState({"compaction_enabled": True, "context_window": 1000})
        lmf = _FakeLLM(fail=True)
        notes = []
        for i in (1, 2, 3):
            await apply_compaction(_messages(30), state=st, last_prompt_tokens=850,
                                   llm_manager=lmf, notify=notes.append, round_idx=i)
            assert st.fail_streak == i
        assert len(notes) == 3
        before = lmf.calls
        await apply_compaction(_messages(30), state=st, last_prompt_tokens=850,
                               llm_manager=lmf, notify=notes.append, round_idx=4)
        assert lmf.calls == before  # circuit broken: no more model calls
    asyncio.run(run())


def test_fail_streak_resets_on_success():
    async def run():
        st = CompactionState({"compaction_enabled": True, "context_window": 1000})
        await apply_compaction(_messages(30), state=st, last_prompt_tokens=850,
                               llm_manager=_FakeLLM(fail=True), notify=lambda m: None, round_idx=1)
        assert st.fail_streak == 1
        await apply_compaction(_messages(30), state=st, last_prompt_tokens=850,
                               llm_manager=_FakeLLM(fail=False), round_idx=2)
        assert st.fail_streak == 0
    asyncio.run(run())


def test_none_llm_manager_is_safe():
    async def run():
        st = CompactionState({"compaction_enabled": True, "context_window": 1000})
        await apply_compaction(_messages(12), state=st, last_prompt_tokens=850, llm_manager=None, round_idx=10)
    asyncio.run(run())
