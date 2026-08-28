"""Sleep asked for a budget and never for a depth, so both its calls thought at the template's own rung.

2026-08-21, one group sleep, three failures on the local model and every usage
line reading `effort=server-default`:

- synthesis, Warren: 16 384 of 16 384, `finish=length` — the model never opened a
  JSON document at all, it continued the prompt for 40 203 characters;
- session analysis, Johnny: 8 192 of 8 192, `finish=length`, of which **4 010 were
  the think block** — that call asks for no ceiling and no cap, so it ran on the
  provider default and had its JSON cut;
- synthesis, Johnny: a well-formed brief that stopped mid-word at `finish=stop`
  with 11 217 tokens of its ceiling unused.

The third is the one the clamp cannot reach: nothing about a token budget stops a
model ending its answer early. The entry that predicted it said so before it was
seen — only an effort word closes that mode.
"""

import json
from pathlib import Path

import pytest

from dpc_client_core.providers.base import REASONING_EFFORTS
from dpc_client_core.dpc_agent.sleep_pipeline import (
    ANALYSIS_EFFORT,
    ANALYSIS_THINKING_BUDGET_TOKENS,
    SYNTHESIS_EFFORT,
    _analysis_request_limits,
    _analyze_single_session,
    _synthesis_request_limits,
)

WINDOW = 262144  # the window all three failures were measured on


class RecordingManager:
    """An llm_manager that keeps what the call asked for."""

    def __init__(self, reply: str):
        self.reply = reply
        self.calls = []

    async def query(self, prompt, **kwargs):
        self.calls.append(kwargs)
        return self.reply


@pytest.fixture
def one_session(tmp_path: Path):
    archive = tmp_path / "2026-08-21T06-23-03_reset_session.json"
    archive.write_text(
        json.dumps({"messages": [{"sender_name": "Mike", "content": "делай"}]}),
        encoding="utf-8",
    )
    return {
        "source": "group_archive",
        "archive_path": str(archive),
        "archive_file": archive.name,
        "date": "2026-08-21",
        "message_count": 1,
        "duration_mins": 1,
        "tool_stats": {},
    }


class TestBothCallsNameADepth:

    def test_the_synthesis_asks_for_one(self):
        assert _synthesis_request_limits(WINDOW)["reasoning_effort"] in REASONING_EFFORTS

    def test_the_session_analysis_asks_for_one(self):
        assert _analysis_request_limits()["reasoning_effort"] in REASONING_EFFORTS

    def test_neither_asks_for_the_deepest_rung(self):
        """`server-default` was the deepest rung on this model — that is the
        state being left, so landing back on it is the failure to catch."""
        assert SYNTHESIS_EFFORT not in ("high", "max")
        assert ANALYSIS_EFFORT not in ("high", "max")

    def test_a_summary_does_not_think_deeper_than_the_synthesis(self):
        """One session into a fixed schema is not a reasoning task; a hundred
        of them into a brief is the closest thing here to one."""
        assert REASONING_EFFORTS.index(ANALYSIS_EFFORT) <= REASONING_EFFORTS.index(SYNTHESIS_EFFORT)


class TestTheAnalysisCallCarriesACapItNeverHad:

    def test_a_thinking_budget_travels_with_it(self):
        assert _analysis_request_limits()["reasoning_budget_tokens"] == ANALYSIS_THINKING_BUDGET_TOKENS

    def test_the_budget_leaves_the_answer_more_than_the_run_that_was_cut(self):
        """The cut run kept 4 182 tokens for the answer out of 8 192. Against the
        same ceiling this cap has to leave more than that, or nothing changed."""
        assert 8192 - _analysis_request_limits()["reasoning_budget_tokens"] > 4182

    def test_it_asks_for_no_ceiling_of_its_own(self):
        """The provider's ceiling stays the ceiling: this call's failure was the
        think block inside it, and a number invented here would cap the providers
        whose room is larger."""
        assert "max_tokens" not in _analysis_request_limits()


class TestTheLimitsReachTheCall:

    @pytest.mark.asyncio
    async def test_the_session_analysis_sends_both(self, tmp_path: Path, one_session):
        manager = RecordingManager(json.dumps({"summary": "one session"}))

        await _analyze_single_session(one_session, tmp_path, manager, provider_alias="local")

        assert len(manager.calls) == 1
        sent = manager.calls[0]
        assert sent["reasoning_effort"] == ANALYSIS_EFFORT
        assert sent["reasoning_budget_tokens"] == ANALYSIS_THINKING_BUDGET_TOKENS
        assert sent["provider_alias"] == "local"

    @pytest.mark.asyncio
    async def test_the_finding_still_comes_back(self, tmp_path: Path, one_session):
        """The limits ride along; they do not replace what the call returns."""
        manager = RecordingManager(json.dumps({"summary": "one session"}))

        finding = await _analyze_single_session(one_session, tmp_path, manager, provider_alias="local")

        assert finding["summary"] == "one session"
        assert finding["source"] == "group_archive"
        assert finding["digest_date"] == "2026-08-21"
