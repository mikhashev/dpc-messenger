"""The scorer grades the span the prompt asked for, and nothing else.

It used to try three candidates — the FINAL ANSWER span, the last line, the
whole text — and, for a numeric gold, the last number anywhere inside them. So
"the correct count is 3, not 2" scored as an answer of 2, while a correct
answer whose marker lacked a colon was rejected. Wrong in both directions from
one cause: it scored text the answer never offered as its answer.
"""

import sys
from pathlib import Path

import pytest

EVAL = Path(__file__).resolve().parents[3] / "eval"
sys.path.insert(0, str(EVAL))
sys.path.insert(0, str(EVAL / "gaia"))

import run_gaia_eval as gaia  # noqa: E402

score = gaia.scores_as_correct


# --- what it used to accept and a person calls wrong ------------------------

@pytest.mark.parametrize("answer, gold", [
    ("The correct count is 3, not 2.", "2"),          # last number in free text
    ("FINAL ANSWER: not 2003", "2003"),               # a negation scored as the value
    ("FINAL ANSWER: 6 or 7", "7"),                    # a hedge scored as a choice
    ("I considered 5 and settled on 9.", "9"),        # no span at all
])
def test_a_number_the_answer_did_not_offer_is_not_the_answer(answer, gold):
    assert score(answer, gold) is False


def test_an_answer_with_no_final_answer_line_is_a_miss_even_when_the_text_matches():
    # The whole-text fallback made this a pass; a missing span is now a miss,
    # because "the answer is somewhere in there" is not what was asked for.
    assert score("The castle.", "The castle") is False


# --- what it used to reject and a person calls right ------------------------

def test_a_missing_colon_is_a_formatting_slip_not_a_wrong_answer():
    assert score("FINAL ANSWER THE CASTLE", "THE CASTLE") is True


def test_a_comma_in_a_number_is_a_thousands_separator_not_a_list():
    assert score("FINAL ANSWER: 1234", "1,234") is True


def test_the_marker_survives_bold_markdown():
    assert score("**FINAL ANSWER:** 42", "42") is True


# --- structure --------------------------------------------------------------

def test_the_span_comes_from_the_last_marker_line():
    answer = "FINAL ANSWER: 1\nno, wait\nFINAL ANSWER: 2"
    assert score(answer, "2") is True
    assert score(answer, "1") is False


def test_prose_that_merely_mentions_the_words_is_not_a_span():
    # Without a separator the marker must open the line, or "the final answer
    # is 42" would answer "is 42" and a sentence about the task would score.
    assert gaia.extract_final_answer("I think the final answer is 42") is None


def test_a_separator_makes_a_mid_line_marker_unambiguous():
    assert gaia.extract_final_answer("So, FINAL ANSWER: 42") == "42"


def test_an_empty_span_is_no_span():
    assert gaia.extract_final_answer("FINAL ANSWER:") is None


def test_ordinary_matching_still_works():
    assert score("FINAL ANSWER: 42", "42") is True
    assert score("FINAL ANSWER: 42.0", "42") is True
    assert score("FINAL ANSWER: The Castle", "the castle") is True
    assert score("FINAL ANSWER: b, e", "b, e") is True
    assert score("FINAL ANSWER: b", "b, e") is False
    assert score("", "42") is False
