"""A sleep response that will not parse must explain itself before it dies.

2026-08-20: the group synthesis failed with `JSONDecodeError: Extra data:
line 1 column 22 (char 21)` and nothing else was recorded. Two readings fit
that one number — the model wrote a real brief and the ceiling cut it, or
the model wrote a stub and then kept talking — and they need opposite
repairs, so the thread could not choose between them. The evidence had
existed for one instant and was thrown away by the re-raise.

These tests do not check that the capture "runs without error": that was
already true of the code that lost the evidence. They check that the record
CARRIES each field a reader needs to tell the two cases apart.
"""

from __future__ import annotations

import json
import logging

import pytest

from dpc_client_core.dpc_agent.sleep_pipeline import (
    _capture_unparsable,
    _last_usage_of,
    _parse_llm_json,
)


def _fail(text: str) -> json.JSONDecodeError:
    """The exception the pipeline actually sees, produced by the real parser."""
    with pytest.raises(json.JSONDecodeError) as excinfo:
        _parse_llm_json(text)
    return excinfo.value


# The three shapes the thread argued about, each named by what it would mean.
STUB_THEN_PROSE = '{"session_count":116}\n\nHere is what I found across the sessions. ' + "x" * 900
BRIEF_THEN_PROSE = '{"morning_brief":{"summary":"' + "y" * 600 + '"}}\n\nAnd a few notes after it.'
CUT_MID_OBJECT = '{"morning_brief":{"summary":"' + "z" * 600


class TestTheRecordCarriesWhatDistinguishesTheTwoReadings:
    def test_the_head_shows_which_of_the_two_shapes_it_was(self, caplog, tmp_path):
        caplog.set_level(logging.ERROR)
        _capture_unparsable(
            STUB_THEN_PROSE, _fail(STUB_THEN_PROSE),
            label="synthesis", usage={}, dump_dir=tmp_path,
        )
        record = caplog.text
        # The stub is short enough to sit inside the head window whole, so a
        # reader can see the whole first value and that prose follows it.
        assert '{"session_count":116}' in record
        assert "Here is what I found" in record

    def test_a_real_brief_is_distinguishable_from_a_stub_in_the_same_window(self, caplog, tmp_path):
        caplog.set_level(logging.ERROR)
        _capture_unparsable(
            BRIEF_THEN_PROSE, _fail(BRIEF_THEN_PROSE),
            label="synthesis", usage={}, dump_dir=tmp_path,
        )
        assert '"morning_brief"' in caplog.text

    def test_the_tail_says_whether_the_text_stops_mid_word(self, caplog, tmp_path):
        caplog.set_level(logging.ERROR)
        _capture_unparsable(
            CUT_MID_OBJECT, _fail(CUT_MID_OBJECT),
            label="synthesis", usage={}, dump_dir=tmp_path,
        )
        assert "--- last 200 ---" in caplog.text
        assert caplog.text.rstrip().endswith("z" * 20)

    def test_the_error_position_and_message_are_in_the_record(self, caplog, tmp_path):
        caplog.set_level(logging.ERROR)
        err = _fail(STUB_THEN_PROSE)
        _capture_unparsable(
            STUB_THEN_PROSE, err, label="synthesis", usage={}, dump_dir=tmp_path,
        )
        assert f"at char {err.pos}" in caplog.text
        assert err.msg in caplog.text

    def test_the_ceiling_signals_are_in_the_record(self, caplog, tmp_path):
        """`finish_reason` and the completion count are the pair that says
        whether the ceiling was reached — the trigger a detector must use,
        because `Extra data` alone never means truncation."""
        caplog.set_level(logging.ERROR)
        _capture_unparsable(
            STUB_THEN_PROSE, _fail(STUB_THEN_PROSE),
            label="synthesis",
            usage={"completion_tokens": 8192, "finish_reason": "length"},
            dump_dir=tmp_path,
        )
        assert "completion_tokens=8192" in caplog.text
        assert "finish=length" in caplog.text

    def test_a_provider_that_reports_nothing_is_visible_as_unknown(self, caplog, tmp_path):
        """Absent is not zero: a missing signal must read as missing, or a
        reader takes the absence for a measurement."""
        caplog.set_level(logging.ERROR)
        _capture_unparsable(
            STUB_THEN_PROSE, _fail(STUB_THEN_PROSE),
            label="synthesis", usage={}, dump_dir=tmp_path,
        )
        assert "completion_tokens=?" in caplog.text
        assert "finish=?" in caplog.text


class TestTheCeilingIsReadableWithoutOutsideKnowledge:
    """`completion_tokens=8192` only means "cut at the cap" to a reader who
    already knows the cap is 8192. The window rides beside the count — and it
    is the fallback for the day the pinned server returns no `finish_reason`
    at all, which is unverified on b10472 (review, 2026-08-20)."""

    def test_the_count_carries_its_window(self, caplog, tmp_path):
        caplog.set_level(logging.ERROR)
        _capture_unparsable(
            STUB_THEN_PROSE, _fail(STUB_THEN_PROSE),
            label="synthesis",
            usage={"completion_tokens": 8192, "max_tokens": 8192},
            dump_dir=tmp_path,
        )
        assert "completion_tokens=8192/8192" in caplog.text

    def test_an_unknown_window_reads_as_unknown(self, caplog, tmp_path):
        caplog.set_level(logging.ERROR)
        _capture_unparsable(
            STUB_THEN_PROSE, _fail(STUB_THEN_PROSE),
            label="synthesis", usage={"completion_tokens": 8192}, dump_dir=tmp_path,
        )
        assert "completion_tokens=8192/?" in caplog.text


class TestTheFullBodySurvivesTheLog:
    def test_the_dump_holds_the_whole_response_not_the_window(self, caplog, tmp_path):
        caplog.set_level(logging.ERROR)
        long_body = STUB_THEN_PROSE + "T" * 5000
        path = _capture_unparsable(
            long_body, _fail(long_body), label="synthesis", usage={}, dump_dir=tmp_path,
        )
        assert path is not None and path.exists()
        assert path.read_text(encoding="utf-8") == long_body
        assert str(path) in caplog.text

    def test_the_label_separates_the_two_call_sites(self, tmp_path):
        path = _capture_unparsable(
            STUB_THEN_PROSE, _fail(STUB_THEN_PROSE),
            label="session-analysis", usage={}, dump_dir=tmp_path,
        )
        assert path is not None and path.name.startswith("failed_session-analysis_")


class TestTheInstrumentCannotBecomeASecondFailure:
    def test_an_unwritable_dump_directory_still_leaves_the_record(self, caplog, tmp_path):
        caplog.set_level(logging.WARNING)
        blocker = tmp_path / "sleep_results"
        blocker.write_text("not a directory", encoding="utf-8")
        path = _capture_unparsable(
            STUB_THEN_PROSE, _fail(STUB_THEN_PROSE),
            label="synthesis", usage={}, dump_dir=blocker,
        )
        assert path is None
        assert "dump=-" in caplog.text
        assert '{"session_count":116}' in caplog.text

    def test_a_lone_surrogate_does_not_destroy_the_evidence(self, caplog, tmp_path):
        """Ran, not remembered (review, 2026-08-20): strict utf-8 raises
        UnicodeEncodeError on a lone surrogate, and that is a ValueError and
        not an OSError — a narrow guard here would let the instrument mask the
        very parse error it was called to explain."""
        caplog.set_level(logging.ERROR)
        poisoned = STUB_THEN_PROSE + chr(0xD800)
        path = _capture_unparsable(
            poisoned, _fail(poisoned), label="synthesis", usage={}, dump_dir=tmp_path,
        )
        assert path is not None and path.exists()
        assert '{"session_count":116}' in caplog.text

    def test_no_dump_directory_is_allowed(self, caplog):
        caplog.set_level(logging.ERROR)
        path = _capture_unparsable(
            STUB_THEN_PROSE, _fail(STUB_THEN_PROSE),
            label="synthesis", usage={}, dump_dir=None,
        )
        assert path is None
        assert '{"session_count":116}' in caplog.text


class TestReadingTheProvidersLastUsage:
    def test_the_named_alias_is_asked(self):
        class _Provider:
            def get_last_usage(self):
                return {"finish_reason": "length", "completion_tokens": 8192}

        class _Manager:
            providers = {"llama.cpp": _Provider()}

        assert _last_usage_of(_Manager(), "llama.cpp")["finish_reason"] == "length"

    def test_no_alias_falls_back_to_the_default_provider(self):
        class _Provider:
            def get_last_usage(self):
                return {"finish_reason": "stop"}

        class _Manager:
            providers = {"deepseek_flash": _Provider()}
            default_provider = "deepseek_flash"

        assert _last_usage_of(_Manager(), None)["finish_reason"] == "stop"

    @pytest.mark.parametrize(
        "manager",
        [
            pytest.param(object(), id="manager-without-providers"),
            pytest.param(type("M", (), {"providers": {}})(), id="alias-not-registered"),
        ],
    )
    def test_anything_missing_reads_as_nothing_rather_than_raising(self, manager):
        assert _last_usage_of(manager, "llama.cpp") == {}

    def test_a_provider_that_raises_does_not_take_the_diagnostic_with_it(self):
        class _Provider:
            def get_last_usage(self):
                raise RuntimeError("provider is mid-restart")

        class _Manager:
            providers = {"llama.cpp": _Provider()}

        assert _last_usage_of(_Manager(), "llama.cpp") == {}
