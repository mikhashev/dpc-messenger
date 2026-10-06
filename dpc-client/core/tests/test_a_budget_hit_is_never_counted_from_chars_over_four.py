"""The note budget is checked against a count, never against chars / 4.

The burn control's rounds produced ~12 050 completion tokens on a 10 000 budget
with ~2 600 visible characters, yet the provider's estimate (note chars / 4)
read ~3 000 on digit-heavy notes and the instrument printed "burn produced: no".
Every round below is synthetic: no model, no tokenizer binary, no real `~/.dpc`.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "eval" / "loop"))

import round_metrics as M  # noqa: E402

BUDGET = 10000
NOTE = "".join(str((i * 7919 + 3) % 10) for i in range(12023))  # digits only
CONTENT = "x" * 2598
USAGE = {"reasoning_tokens": len(NOTE) // 4, "thinking_source": "estimated",
         "completion_tokens": 12050, "prompt_tokens": 3871}


def _record(count_tokens=None, msg=None, usage=None):
    rec = M.RoundRecorder()
    rec.count_tokens = count_tokens
    rec.record(msg if msg is not None else {"content": CONTENT, "thinking": NOTE},
               dict(usage or USAGE), 1.0)
    return rec.rows


def _burn(rows):
    m = M.summarise_rounds(rows, BUDGET)
    m.update(compaction_round=None, compactions=0, compaction_failures=0,
             rounds_to_completion=None)
    return {"id": "long-control-burn", "arm": "off", "repeat": 1, "kind": M.KIND_BURN,
            "passed": None, "timed_out": False, "seconds": 1.0, "metrics": m}


def test_the_burn_round_is_a_budget_hit_without_a_tokenizer():
    rows = _record()
    row = rows[0]
    assert (row["reasoning_tokens"], row["thinking_source"]) == (12050, M.SOURCE_UPPER_BOUND)
    assert (row["reasoning_tokens_reported"], row["thinking_source_reported"]) == (3005, "estimated")
    m = M.summarise_rounds(rows, BUDGET)
    assert m["budget_hits"] == 1 and m["silent_budget_hits"] == 0
    assert m["max_reasoning_tokens"] == 12050
    assert m["reasoning_tokens_by_round"] == [12050]
    s0 = M.step0([_burn(rows)])
    assert s0["burn_control"]["produced"] is True
    assert M.burn_line(s0["burn_control"]).startswith("burn produced: yes")
    assert s0["outcome"]["code"] == "burn-produced"


def test_the_tokenizer_counts_the_note_when_it_is_there():
    # A digit is roughly one token in a BPE vocabulary; chars / 4 is a quarter of that.
    rows = _record(count_tokens=lambda text: len(text) * 9 // 10)
    assert rows[0]["thinking_source"] == M.SOURCE_TOKENIZER
    assert rows[0]["reasoning_tokens"] == 12023 * 9 // 10
    assert M.summarise_rounds(rows, BUDGET)["budget_hits"] == 1


def test_a_failing_tokenizer_falls_back_to_the_upper_bound():
    def broken(_text):
        raise RuntimeError("no count")
    rows = _record(count_tokens=broken)
    assert (rows[0]["reasoning_tokens"], rows[0]["thinking_source"]) == (12050, M.SOURCE_UPPER_BOUND)
    assert M.summarise_rounds(rows, BUDGET)["budget_hits"] == 1


def test_with_no_note_text_the_visible_output_is_taken_off_the_completion():
    msg = {"content": CONTENT,
           "tool_calls": [{"function": {"name": "read_file", "arguments": '{"path": "a.py"}'}}]}
    seen = []

    def count(text):
        seen.append(text)
        return len(text) // 2
    rows = _record(count_tokens=count, msg=msg)
    assert rows[0]["thinking_source"] == M.SOURCE_MINUS_VISIBLE
    assert "read_file" in seen[0] and '"a.py"' in seen[0]
    assert rows[0]["reasoning_tokens"] == 12050 - len(seen[0]) // 2


def test_an_engine_count_is_taken_as_is():
    rows = _record(usage=dict(USAGE, reasoning_tokens=11000, thinking_source="engine"),
                   count_tokens=lambda text: 1)
    assert (rows[0]["reasoning_tokens"], rows[0]["thinking_source"]) == (11000, M.SOURCE_ENGINE)


def test_an_unattributed_count_is_not_trusted_either():
    rows = _record(usage={"reasoning_tokens": 3005, "completion_tokens": 12050})
    assert rows[0]["thinking_source"] == M.SOURCE_UPPER_BOUND
    assert M.summarise_rounds(rows, BUDGET)["budget_hits"] == 1


def test_a_row_recorded_before_the_correction_is_rescored_from_its_completion():
    old = {"round": 1, "content_chars": 2598, "tool_calls": 0, "note_chars": 12023,
           "note_opening": "", "reasoning_tokens": 3005, "thinking_source": "estimated",
           "completion_tokens": 12050, "prompt_tokens": 3871, "served_effort": "medium"}
    # Even unconverted, the budget check does not believe the estimate.
    assert M.summarise_rounds([dict(old)], BUDGET)["budget_hits"] == 1
    fixed = M.correct_row(dict(old))
    assert (fixed["reasoning_tokens"], fixed["thinking_source"]) == (12050, M.SOURCE_UPPER_BOUND)
    assert fixed["reasoning_tokens_reported"] == 3005
    assert M.correct_row(fixed) == fixed  # idempotent
    m = M.summarise_rounds([fixed], BUDGET)
    assert m["note_tokens_estimated_rows"] == 1
    assert m["thinking_sources"] == {M.SOURCE_UPPER_BOUND: 1}


def test_a_short_round_stays_below_the_budget():
    rows = _record(msg={"content": "ok", "thinking": "1 2 3"},
                   usage={"reasoning_tokens": 2, "thinking_source": "estimated",
                          "completion_tokens": 900})
    assert M.summarise_rounds(rows, BUDGET)["budget_hits"] == 0
    s0 = M.step0([_burn(rows)])
    assert s0["burn_control"]["produced"] is False
