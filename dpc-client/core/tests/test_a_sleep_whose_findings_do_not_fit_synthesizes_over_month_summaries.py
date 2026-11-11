"""Findings that do not fit one synthesis prompt are summarised by month, not trimmed.

Until 2026-11-11 the synthesis read the findings of every session ever, newest first,
and dropped the oldest to fit the window: Ark's 1:1 brief had 469 findings at about 896
tokens each (about 420K) against a 244 736-token budget, so about 42 percent of the
sessions would have left the brief. Mike's call, 2026-11-11 (ADR-014 amendment): when
they fit, one call as before; when they do not, one summary per calendar month made from
that month's raw findings, cached while its successful findings are unchanged, and a
synthesis over the summaries. The period, the counts and the last date come from raw
session dates by code.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from dpc_client_core.dpc_agent import sleep_pipeline as sleep_mod
from dpc_client_core.dpc_agent.sleep_pipeline import (
    MONTH_SUMMARY_CEILING_SHARE,
    MONTH_SUMMARY_FILE_PREFIX,
    MONTH_SUMMARY_PROMPT,
    SYNTHESIS_FROM_MONTH_SUMMARIES_PROMPT,
    SYNTHESIS_PROMPT,
    _month_summary_prompt,
    run_sleep,
)

AGENT = "agent_month_summaries"
WRONG = "1999-01 to 1999-02"
MONTH_ASKS = "Condense them into one month summary"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    sleep_mod._SLEEP_PIPELINE_LOCK = asyncio.Lock()
    monkeypatch.setattr(sleep_mod, "_export_graph_snapshot", lambda *a, **k: None)
    from dpc_client_core.dpc_agent import knowledge_graph
    monkeypatch.setattr(knowledge_graph.KnowledgeGraph, "extract_entities_gliner",
                        lambda self, texts: [], raising=False)


def _add_sessions(conv: Path, sessions):
    """sessions: (name, date, pad, flag). The pad sizes the finding the fake model writes."""
    with open(conv / "digest.jsonl", "a", encoding="utf-8") as digest:
        for name, date, pad, flag in sessions:
            digest.write(json.dumps({"archive_file": f"{name}.json", "date": f"{date}T10:00:00",
                                     "message_count": 2}) + "\n")
            (conv / "archive" / f"{name}.json").write_text(json.dumps({"messages": [
                {"role": "user", "content": f"{name} pad={pad} {flag}"},
                {"role": "assistant", "content": "done"},
            ]}), encoding="utf-8")


def _conversation(tmp_path: Path, sessions) -> Path:
    conv = tmp_path / "conversations" / AGENT
    (conv / "archive").mkdir(parents=True)
    _add_sessions(conv, sessions)
    return conv


def _six_in_three_months(pad=2500):
    return [("s1", "2026-08-03", pad, ""), ("s2", "2026-08-17", pad, ""),
            ("s3", "2026-09-02", pad, ""), ("s4", "2026-09-20", pad, ""),
            ("s5", "2026-10-05", pad, ""), ("s6", "2026-10-20", pad, "")]


class _Engine:
    """One token per character, so a test sizes a prompt by its text."""

    model = "qwen"

    def __init__(self, window):
        self.window = window

    def counts_on_its_engine(self):
        return True

    async def engine_context_window(self):
        return self.window

    async def engine_token_count(self, text, *, as_prompt=False):
        return len(text)


class _Manager:
    def __init__(self, provider, window=250_000, month_pad=10):
        self.providers = {"engine": provider}
        self.default_provider = "engine"
        self.window = window
        self.month_pad = month_pad
        self.fail_months = set()
        self.boom = False
        self.analysis, self.months, self.synthesis = [], [], []

    async def query(self, prompt, provider_alias=None, **kwargs):
        if prompt.startswith("Analyze this single conversation session"):
            self.analysis.append(prompt)
            if self.boom and "BOOM" in prompt:
                raise RuntimeError("boom")
            pad = int(re.search(r"pad=(\d+)", prompt).group(1))
            name = re.search(r"(s\d+) pad=", prompt).group(1)
            return json.dumps({"summary": f"{name} " + "x" * pad})
        if MONTH_ASKS in prompt:
            month = re.search(r"sessions from (\d{4}-\d{2}|undated)", prompt).group(1)
            self.months.append((month, prompt, kwargs))
            if month in self.fail_months:
                raise RuntimeError(f"boom in {month}")
            return json.dumps({"summary": f"condensed {month} " + "y" * self.month_pad,
                               "period": WRONG, "month": WRONG})
        self.synthesis.append((prompt, kwargs))
        return json.dumps({
            "morning_brief": {"sessions_analyzed": 999, "period": WRONG,
                              "last_session": {"date": WRONG}, "summary": "brief"},
            "sleep_findings": {},
        })

    def count_tokens(self, text, model):
        return len(text) // 4

    def alias_context_window(self, alias):
        return self.window


def _info(result):
    return result["morning_brief"]["synthesis_budget"]


def _cache_files(conv: Path, month="*"):
    return sorted((conv / "sleep_results").glob(f"{MONTH_SUMMARY_FILE_PREFIX}all_{month}_*.json"))


async def _sleep(conv, manager):
    result = await run_sleep(conv, manager, provider_alias="engine")
    assert result["status"] == "completed", result
    return result


@pytest.mark.asyncio
async def test_findings_that_fit_go_in_one_call_with_no_month_summary(tmp_path):
    conv = _conversation(tmp_path, _six_in_three_months(pad=300))
    manager = _Manager(_Engine(100_000))

    result = await _sleep(conv, manager)

    assert manager.months == [] and len(manager.synthesis) == 1
    prompt, _ = manager.synthesis[0]
    assert "per-session analysis results from 6 recent sessions" in prompt
    assert "MONTH SUMMARIES" not in prompt
    info = _info(result)
    assert "month_summaries" not in info
    assert info["total_findings"] == info["included_findings"] == 6
    assert _cache_files(conv) == []


@pytest.mark.asyncio
async def test_findings_that_do_not_fit_make_one_summary_per_month_and_one_synthesis_over_them(tmp_path):
    conv = _conversation(tmp_path, _six_in_three_months())
    manager = _Manager(_Engine(20_000))

    result = await _sleep(conv, manager)

    assert [m for m, _, _ in manager.months] == ["2026-08", "2026-09", "2026-10"]
    assert len(manager.analysis) == 6 and len(manager.synthesis) == 1
    # A month summary reads only that month's raw findings.
    for month, prompt, _ in manager.months:
        assert prompt.count("pad=") == 0 and prompt.count(" xxxxxxxx") == 2
        assert f"sessions from {month}" in prompt
    prompt, _ = manager.synthesis[0]
    assert "MONTH SUMMARIES (oldest first" in prompt and "PER-SESSION FINDINGS" not in prompt
    assert prompt.index("Month 2026-08") < prompt.index("Month 2026-09") < prompt.index("Month 2026-10")
    assert "xxxxxxxx" not in prompt.split("--- MONTH SUMMARIES (oldest first")[1]
    info = _info(result)
    assert info["total_findings"] == info["included_findings"] == 6
    summaries = info["month_summaries"]
    assert (summaries["built"], summaries["cached"], summaries["failed"]) == (3, 0, 0)
    assert [m["month"] for m in summaries["months"]] == ["2026-08", "2026-09", "2026-10"]
    assert info["prompt_tokens_on_engine"] == len(prompt)


@pytest.mark.asyncio
async def test_the_period_the_counts_and_the_last_date_are_set_from_raw_sessions_by_code(tmp_path):
    conv = _conversation(tmp_path, _six_in_three_months())
    manager = _Manager(_Engine(20_000))

    result = await _sleep(conv, manager)

    brief = result["morning_brief"]
    assert brief["period"] == "2026-08-03 to 2026-10-20"
    assert brief["sessions_analyzed"] == 6
    assert brief["last_session"]["date"] == "2026-10-20"
    prompt, _ = manager.synthesis[0]
    assert "Month 2026-09 (2 sessions, 2026-09-02 to 2026-09-20):" in prompt   # header from raw dates
    assert WRONG not in prompt
    assert _info(result)["oldest_included_date"] == "2026-08-03"


@pytest.mark.asyncio
async def test_a_month_summary_is_reused_while_its_findings_are_unchanged_and_rebuilt_when_one_is_added(tmp_path):
    conv = _conversation(tmp_path, _six_in_three_months())
    manager = _Manager(_Engine(20_000))
    await _sleep(conv, manager)
    assert len(manager.months) == 3 and len(_cache_files(conv)) == 3

    manager.months.clear()
    second = await _sleep(conv, manager)
    assert manager.months == []                       # nothing was asked again
    assert (_info(second)["month_summaries"]["built"], _info(second)["month_summaries"]["cached"]) == (0, 3)

    _add_sessions(conv, [("s7", "2026-09-28", 300, "")])
    third = await _sleep(conv, manager)
    assert [m for m, _, _ in manager.months] == ["2026-09"]   # only the month that gained one
    assert _info(third)["month_summaries"]["cached"] == 2
    assert len(_cache_files(conv, "2026-09")) == 1            # the old September summary is gone
    assert len(_cache_files(conv)) == 3


@pytest.mark.asyncio
async def test_a_changed_prompt_template_invalidates_the_cache(tmp_path, monkeypatch):
    conv = _conversation(tmp_path, _six_in_three_months())
    manager = _Manager(_Engine(20_000))
    await _sleep(conv, manager)
    manager.months.clear()

    monkeypatch.setattr(sleep_mod, "MONTH_SUMMARY_PROMPT", MONTH_SUMMARY_PROMPT + "\n- one more rule\n")
    await _sleep(conv, manager)

    assert len(manager.months) == 3


@pytest.mark.asyncio
async def test_an_errored_session_is_left_out_and_its_month_is_not_cached(tmp_path):
    sessions = _six_in_three_months()
    sessions[2] = ("s3", "2026-09-02", 2500, "BOOM")
    conv = _conversation(tmp_path, sessions)
    manager = _Manager(_Engine(20_000))
    manager.boom = True

    first = await _sleep(conv, manager)

    september = next(p for m, p, _ in manager.months if m == "2026-09")
    assert "results of 1 sessions from 2026-09" in september
    assert '"s3 x' not in september and '"s4 x' in september
    assert _cache_files(conv, "2026-09") == [] and len(_cache_files(conv)) == 2
    record = next(m for m in _info(first)["month_summaries"]["months"] if m["month"] == "2026-09")
    assert record["cache"].startswith("not frozen") and record["findings"] == 1

    # Tomorrow: the errored session is retried (still failing), the frozen months are reused.
    manager.months.clear()
    await _sleep(conv, manager)
    assert [m for m, _, _ in manager.months] == ["2026-09"]

    # It recovers: the month now holds two findings, is summarised again and frozen.
    manager.boom = False
    manager.months.clear()
    await _sleep(conv, manager)
    assert [m for m, _, _ in manager.months] == ["2026-09"]
    assert len(_cache_files(conv, "2026-09")) == 1
    manager.months.clear()
    await _sleep(conv, manager)
    assert manager.months == []


@pytest.mark.asyncio
async def test_a_month_above_the_ceiling_is_split_into_consecutive_parts(tmp_path):
    sessions = [(f"s{i}", f"2026-09-{i:02d}", 3000, "") for i in range(1, 7)]
    conv = _conversation(tmp_path, sessions)
    manager = _Manager(_Engine(20_000))

    result = await _sleep(conv, manager)

    info = _info(result)["month_summaries"]
    assert [m for m, _, _ in manager.months] == ["2026-09"] * 3
    assert info["parts"] == 3 and info["months"][0]["parts"] == 3
    names = [re.findall(r"\bs\d\b(?= x)", prompt) for _, prompt, _ in manager.months]
    assert names == [["s1", "s2"], ["s3", "s4"], ["s5", "s6"]]          # consecutive, nothing twice
    prompt, _ = manager.synthesis[0]
    assert "Month 2026-09 (part 2 of 3, 2 sessions, 2026-09-03 to 2026-09-04):" in prompt
    assert _info(result)["included_findings"] == 6
    assert len(_cache_files(conv, "2026-09")) == 3


@pytest.mark.asyncio
async def test_the_ceiling_is_a_share_of_the_prompt_limit_less_the_map_template(tmp_path):
    conv = _conversation(tmp_path, _six_in_three_months())
    manager = _Manager(_Engine(20_000))

    result = await _sleep(conv, manager)

    limit = 20_000 - sleep_mod._synthesis_output_reserve(20_000)
    map_template = len(_month_summary_prompt("0000-00", 1, 1, []))
    assert _info(result)["month_summaries"]["ceiling_tokens"] == int(
        MONTH_SUMMARY_CEILING_SHARE * (limit - map_template))
    # Each call is sent with the synthesis's own answer room and effort.
    _, _, kwargs = manager.months[0]
    assert kwargs["max_tokens"] == sleep_mod._synthesis_output_reserve(20_000)
    assert kwargs["reasoning_effort"] == sleep_mod.SYNTHESIS_EFFORT


@pytest.mark.asyncio
async def test_a_failed_summary_falls_back_to_the_raw_findings_that_fit_and_says_so(tmp_path):
    conv = _conversation(tmp_path, _six_in_three_months())
    # Two 4 000-character summaries leave room for one raw finding (2 650) but not two.
    manager = _Manager(_Engine(20_000), month_pad=4000)
    manager.fail_months = {"2026-09"}

    result = await _sleep(conv, manager)

    info = _info(result)
    summaries = info["month_summaries"]
    assert summaries["failed"] == 1
    assert summaries["failures"][0]["month"] == "2026-09" and "boom in 2026-09" in summaries["failures"][0]["error"]
    september = next(m for m in summaries["months"] if m["month"] == "2026-09")
    assert (september["failed"], september["fallback_findings"], september["findings_in_prompt"]) == (1, 1, 1)
    prompt, _ = manager.synthesis[0]
    assert '"s4 x' in prompt and '"s3 x' not in prompt              # newest first
    assert "Month 2026-08" in prompt and "Month 2026-10" in prompt and "Month 2026-09" not in prompt
    assert info["included_findings"] == 5 and info["total_findings"] == 6
    assert _cache_files(conv, "2026-09") == []                      # a failure is not frozen


@pytest.mark.asyncio
async def test_a_failed_summary_with_room_for_all_falls_back_to_every_finding_of_its_month(tmp_path):
    conv = _conversation(tmp_path, _six_in_three_months())
    manager = _Manager(_Engine(20_000), month_pad=10)
    manager.fail_months = {"2026-08"}

    result = await _sleep(conv, manager)

    prompt, _ = manager.synthesis[0]
    assert '"s1 x' in prompt and '"s2 x' in prompt
    assert _info(result)["included_findings"] == 6


@pytest.mark.asyncio
async def test_on_the_estimate_path_over_budget_goes_the_same_way(tmp_path):
    from types import SimpleNamespace
    conv = _conversation(tmp_path, _six_in_three_months(pad=6000))
    manager = _Manager(SimpleNamespace(model="cloud-model"), window=16_000)

    result = await _sleep(conv, manager)

    assert len(manager.months) >= 3 and len(manager.synthesis) == 1
    assert "MONTH SUMMARIES" in manager.synthesis[0][0]
    info = _info(result)
    assert "prompt_tokens_on_engine" not in info and info["month_summaries"]["failed"] == 0
    assert info["included_findings"] == info["total_findings"] == 6


def test_the_prompt_over_month_summaries_is_the_synthesis_prompt_with_its_input_renamed():
    keys = dict(n=3, period="p", most_recent="m", findings="f", entity_section="")
    derived = SYNTHESIS_FROM_MONTH_SUMMARIES_PROMPT.format(**keys)

    assert SYNTHESIS_FROM_MONTH_SUMMARIES_PROMPT != SYNTHESIS_PROMPT
    assert "PER-SESSION FINDINGS" not in derived and "recent sessions" not in derived
    assert "--- MONTH SUMMARIES (oldest first" in derived and "Do NOT pick from --- MONTH SUMMARIES ---" in derived
    assert "the dates inside it are session dates" in derived
    assert "--- MOST RECENT SESSION" in derived
    assert MONTH_SUMMARY_PROMPT.format(n=1, month="2026-09", part_note="", target_tokens=1, findings="f")
