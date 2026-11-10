"""A paid summary is kept when a later summary call fails (ADR-033, ADR-045 decision 1).

Design: the summaries live in the returned transcript (marked `_compacted`), so a
later batch skips them with no cache; the failure carries the partial list out and
the wrapper's deterministic ladder treats only the messages not summarised.
"""
import asyncio

import pytest

from dpc_client_core.dpc_agent.context import (
    CompactionState, PartialCompaction, apply_compaction, compact_tool_history_llm,
    _KEEP_ASIS_CHARS,
)


class _Scripted:
    """Answers 'SUMMARY-n' per call; the calls listed in `fail_on` (1-based) raise."""

    def __init__(self, fail_on=()):
        self.calls = 0
        self.fail_on = set(fail_on)

    async def query(self, prompt, provider_alias=None, **kwargs):
        self.calls += 1
        if self.calls in self.fail_on:
            raise RuntimeError("simulated failure")
        return f"SUMMARY-{self.calls}"


def _messages(n_rounds):
    msgs = []
    for r in range(n_rounds):
        msgs.append({"role": "assistant", "content": "",
                     "tool_calls": [{"id": f"t{r}", "function": {"name": "read_file", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"t{r}", "content": f"R{r}" + "X" * (_KEEP_ASIS_CHARS + 500),
                     "_output_at": f"out/{r}.txt"})
    return msgs


def _tools(msgs):
    return [m for m in msgs if m["role"] == "tool"]


def test_the_first_summary_is_kept_and_the_failed_one_is_left_for_the_fallback():
    async def run():
        lm = _Scripted(fail_on={2})
        with pytest.raises(PartialCompaction) as ei:
            await compact_tool_history_llm(_messages(10), lm, "flash", keep_recent=6)
        tools = _tools(ei.value.result)
        assert tools[0]["content"].startswith("SUMMARY-1") and tools[0]["_compacted"]
        assert tools[1]["content"].startswith("R1") and not tools[1].get("_compacted")
        assert tools[2]["content"].startswith("R2")
        assert lm.calls == 2  # no paid call after the failure
    asyncio.run(run())


def test_the_wrapper_keeps_the_summary_and_cuts_the_rest_with_the_address():
    async def run():
        lm = _Scripted(fail_on={2})
        st = CompactionState({"compaction_enabled": True, "context_window": 1000})
        notes = []
        out = await apply_compaction(_messages(12), state=st, last_prompt_tokens=999,
                                     llm_manager=lm, notify=notes.append, round_idx=1)
        tools = _tools(out)
        assert tools[0]["content"].startswith("SUMMARY-1")
        assert tools[1]["content"].startswith("[Cut from context:") and "out/1.txt" in tools[1]["content"]
        assert st.fail_streak == 1 and len(notes) == 1
        assert lm.calls == 2
    asyncio.run(run())


def test_the_next_batch_does_not_ask_again_for_a_summarised_result():
    async def run():
        lm = _Scripted(fail_on={2})
        with pytest.raises(PartialCompaction) as ei:
            await compact_tool_history_llm(_messages(10), lm, "flash", keep_recent=6)
        lm2 = _Scripted()
        out = await compact_tool_history_llm(ei.value.result, lm2, "flash", keep_recent=6)
        # 4 old rounds, the first already summarised: three calls, not four.
        assert lm2.calls == 3
        assert out[1]["content"].startswith("SUMMARY-1")  # the earlier summary survived untouched
    asyncio.run(run())


def test_the_breaker_still_counts_the_failure_and_opens_after_max_fails():
    async def run():
        lm = _Scripted(fail_on=set(range(1, 100)))
        st = CompactionState({"compaction_enabled": True, "context_window": 1000})
        for n in range(1, st.max_fails + 1):
            await apply_compaction(_messages(12), state=st, last_prompt_tokens=999, llm_manager=lm, round_idx=n)
            assert st.fail_streak == n
        calls = lm.calls
        await apply_compaction(_messages(12), state=st, last_prompt_tokens=999, llm_manager=lm, round_idx=9)
        assert lm.calls == calls  # circuit open: the model is not called
    asyncio.run(run())


def test_all_success_path_is_unchanged():
    async def run():
        lm = _Scripted()
        out = await compact_tool_history_llm(_messages(10), lm, "flash", keep_recent=6)
        assert lm.calls == 4
        tools = _tools(out)
        assert [t["content"].split("\n")[0] for t in tools[:4]] == [f"SUMMARY-{n}" for n in range(1, 5)]
        assert all(t["_compacted"] for t in tools[:4]) and "_compacted" not in tools[4]
        st = CompactionState({"compaction_enabled": True, "context_window": 1000})
        await apply_compaction(_messages(12), state=st, last_prompt_tokens=999, llm_manager=_Scripted(), round_idx=1)
        assert st.fail_streak == 0
    asyncio.run(run())
