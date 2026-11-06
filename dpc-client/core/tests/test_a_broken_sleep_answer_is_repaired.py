"""A sleep answer that is almost JSON is mended, not thrown away.

Seven sleep answers failed between 2026-08-21 and 2026-11-06, each a long
answer broken in one or two places: quotes inside a string left unescaped, a
closing quote dropped before the closer, a cut mid-string (at the token
ceiling, or at `finish=stop` mid-word), a placeholder echoed from the
prompt's outline. Mike's call, 2026-11-06: repair the answer, and only when
the repair fails take the failure path that already exists. The fixtures are
synthetic copies of those breakages, not the answers themselves.
"""

from __future__ import annotations

import json
import logging

import pytest

from dpc_client_core.dpc_agent.sleep_pipeline import (
    _capture_unparsable,
    _parse_llm_answer,
)

SYNTHESIS = ("morning_brief", "sleep_findings")


def _synthesis(text: str):
    return _parse_llm_answer(text, required_dicts=SYNTHESIS)


class TestEachSeenBreakageIsMended:
    def test_quotes_inside_a_string_are_escaped(self):
        text = ('{"morning_brief": {"summary": "a pattern "claim wider than the '
                'measurement" again"}, "sleep_findings": {}}')
        parsed = _synthesis(text)
        assert parsed.data["morning_brief"]["summary"] == (
            'a pattern "claim wider than the measurement" again')
        assert parsed.fixes == ["escaped_quote", "escaped_quote"]

    def test_a_closing_quote_dropped_before_the_closer_is_restored(self):
        text = '{"date": "2026-08-21", "summary": "the server became honest.\n}'
        parsed = _parse_llm_answer(text)
        assert parsed.data == {"date": "2026-08-21", "summary": "the server became honest."}
        assert parsed.fixes == ["closed_string_at_newline"]

    def test_a_cut_inside_the_relations_keeps_every_relation_before_it(self):
        text = ('{"morning_brief": {"summary": "b"}, "sleep_findings": {},\n'
                ' "extracted_relations": [\n'
                '  {"source": "A", "target": "B", "relation_type": "SUPPORTS", '
                '"confidence": 0.9, "justification": "A was measured to support B twice"},\n'
                '  {"source": "C", "target": "D", "relation_type": "CONTRADICTS", '
                '"confidence": 0.88, "justification": "C contradicts D when m')
        parsed = _synthesis(text)
        relations = parsed.data["extracted_relations"]
        assert [r["source"] for r in relations] == ["A", "C"]
        assert relations[1]["justification"] == "C contradicts D when m"
        assert parsed.fixes == ["closed_string_at_end", "closed_containers_at_end"]

    def test_a_stop_mid_word_in_the_findings_still_yields_the_brief(self):
        text = ('{"morning_brief": {"summary": "the brief"}, "sleep_findings": '
                '{"behavioral_observations": [{"observation": "agents verif')
        parsed = _synthesis(text)
        assert parsed.data["morning_brief"] == {"summary": "the brief"}
        assert parsed.data["sleep_findings"]["behavioral_observations"] == [
            {"observation": "agents verif"}]

    def test_a_cut_inside_a_key_drops_that_member(self):
        text = ('{"morning_brief": {"summary": "b"}, "sleep_findings": {}, '
                '"extracted_relations": [{"source": "A", "tar')
        parsed = _synthesis(text)
        assert parsed.data["extracted_relations"] == [{"source": "A"}]
        assert "dropped_cut_member" in parsed.fixes

    def test_an_echoed_placeholder_is_dropped_and_the_real_list_kept(self):
        text = ('{"morning_brief": {"summary": "b",\n'
                '    "extracted_relations": [...placeholder — filled below...]\n'
                '  },\n'
                '  "sleep_findings": {},\n'
                '  "extracted_relations": [{"source": "A", "target": "B"}]}')
        parsed = _synthesis(text)
        assert parsed.data["morning_brief"] == {"summary": "b"}
        assert parsed.data["extracted_relations"] == [{"source": "A", "target": "B"}]
        assert sorted(parsed.fixes) == ["dropped_placeholder", "dropped_trailing_comma"]

    def test_a_trailing_comma_before_a_closer_is_dropped(self):
        parsed = _synthesis('{"morning_brief": {"summary": "b",}, "sleep_findings": {},}')
        assert parsed.data == {"morning_brief": {"summary": "b"}, "sleep_findings": {}}
        assert parsed.fixes == ["dropped_trailing_comma", "dropped_trailing_comma"]

    def test_a_fenced_answer_is_repaired_inside_the_fence(self):
        parsed = _synthesis('```json\n{"morning_brief": {"summary": "b",}, '
                            '"sleep_findings": {}}\n```')
        assert parsed.data["morning_brief"] == {"summary": "b"}


class TestWhatIsNotMended:
    def test_a_valid_answer_reports_no_fixes(self):
        text = json.dumps({"morning_brief": {"summary": 'a "quoted" word'},
                           "sleep_findings": {}})
        parsed = _synthesis(text)
        assert parsed.fixes == [] and parsed.strict_error is None

    def test_prose_raises_the_strict_parsers_own_error(self):
        with pytest.raises(json.JSONDecodeError) as excinfo:
            _synthesis("not json at all")
        assert excinfo.value.pos == 0

    def test_json_then_prose_is_still_extra_data(self):
        with pytest.raises(json.JSONDecodeError, match="Extra data"):
            _synthesis('{"morning_brief": {}, "sleep_findings": {}}\n\nAnd some notes after it.')

    def test_a_repair_without_the_synthesis_shape_is_refused(self):
        cut = '{"morning_brief": {"summary": "zzz'
        with pytest.raises(json.JSONDecodeError):
            _synthesis(cut)
        # The same text is an acceptable session analysis, which needs no keys.
        assert _parse_llm_answer(cut).data == {"morning_brief": {"summary": "zzz"}}

    def test_a_placeholder_shaped_string_value_is_left_alone(self):
        text = json.dumps({"morning_brief": {"summary": "[...placeholder...]"},
                           "sleep_findings": {}})
        assert _synthesis(text).data["morning_brief"]["summary"] == "[...placeholder...]"


def test_a_repaired_answer_keeps_its_raw_text_and_says_so(tmp_path, caplog):
    text = '{"morning_brief": {"summary": "b",}, "sleep_findings": {}}'
    parsed = _synthesis(text)
    caplog.set_level(logging.WARNING)
    path = _capture_unparsable(text, parsed.strict_error, label="synthesis",
                               dump_dir=tmp_path, repaired_fixes=parsed.fixes)
    assert path is not None and path.name.startswith("repaired_synthesis_")
    assert path.read_text(encoding="utf-8") == text
    assert "synthesis answer repaired, 1 fixes" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
