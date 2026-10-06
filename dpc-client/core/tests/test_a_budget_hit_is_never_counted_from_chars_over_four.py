"""The note budget is checked against a count, never against chars / 4 — and a
bound that cannot decide says so.

The burn control's rounds produced ~12 050 completion tokens on a 10 000 budget
with ~2 600 visible characters, yet the provider's estimate (note chars / 4)
read ~3 000 on digit-heavy notes and the instrument printed "burn produced: no".
The completion always holds the reasoning, so under the threshold it proves no
hit; over it, it may be a long answer, so a round known only by its completion
is undetermined. Every round below is synthetic: no model, no tokenizer binary,
no real `~/.dpc`.
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


def _digit_tokenizer(text):
    # A digit is one token in the measured vocabulary; letters run ~4 to a token.
    return sum(1 for c in text if c.isdigit()) + sum(1 for c in text if not c.isdigit()) // 4


def _record(count_tokens=None, msg=None, usage=None):
    rec = M.RoundRecorder()
    rec.count_tokens = count_tokens
    rec.record(msg if msg is not None else {"content": CONTENT, "thinking": NOTE},
               dict(usage or USAGE), 1.0)
    return rec.rows


def _burn(rows, repeat=1):
    m = M.summarise_rounds(rows, BUDGET)
    m.update(compaction_round=None, compactions=0, compaction_failures=0,
             rounds_to_completion=None)
    return {"id": "long-control-burn", "arm": "off", "repeat": repeat, "kind": M.KIND_BURN,
            "passed": None, "timed_out": False, "seconds": 1.0, "metrics": m}


# -- the three states ----------------------------------------------------------------

def test_the_burn_round_counted_with_the_tokenizer_is_a_hit():
    rows = _record(count_tokens=_digit_tokenizer)
    row = rows[0]
    assert (row["reasoning_tokens"], row["thinking_source"]) == (12023, M.SOURCE_TOKENIZER)
    assert (row["reasoning_tokens_reported"], row["thinking_source_reported"]) == (3005, "estimated")
    m = M.summarise_rounds(rows, BUDGET)
    assert (m["budget_hits"], m["budget_undetermined"]) == (1, 0)
    assert m["max_reasoning_tokens_counted"] == 12023
    s0 = M.step0([_burn(rows)])
    assert s0["burn_control"]["state"] == "yes" and s0["burn_control"]["produced"] is True
    assert M.burn_line(s0["burn_control"]).startswith(
        "burn produced: yes (max reasoning tokens 12023 of budget 10000")
    assert s0["outcome"]["code"] == "burn-produced"


def test_the_same_round_without_tokenizer_or_text_is_undetermined_not_a_hit():
    rows = _record()
    assert (rows[0]["reasoning_tokens"], rows[0]["thinking_source"]) == (12050, M.SOURCE_UPPER_BOUND)
    m = M.summarise_rounds(rows, BUDGET)
    assert (m["budget_hits"], m["budget_undetermined"]) == (0, 1)
    assert m["silent_budget_hits"] == 0 and m["max_reasoning_tokens_counted"] is None
    s0 = M.step0([_burn(rows), _burn(_record(), repeat=2)])
    burn = s0["burn_control"]
    assert burn["state"] == "undetermined" and burn["produced"] is None
    assert burn["runs_undetermined"] == 2
    assert M.burn_line(burn).startswith(M.BURN_UNDETERMINED_LINE)
    # Neither reading of the card: not "can produce burning", not "0 of N".
    assert s0["outcome"]["code"] == "burn-undetermined"
    assert M.BURN_PRODUCED not in s0["outcome"]["text"]
    assert M.BURN_ABSENT not in s0["outcome"]["text"]


def test_a_completion_of_800_is_not_a_hit_whatever_the_estimate_says():
    for usage in ({"reasoning_tokens": 9990, "thinking_source": "estimated", "completion_tokens": 800},
                  {"reasoning_tokens": 9990, "completion_tokens": 800},
                  {"completion_tokens": 800}):
        rows = _record(msg={"content": "ok", "thinking": "1 2 3"}, usage=usage)
        assert M.budget_state(rows[0], BUDGET) == M.NO_HIT
        m = M.summarise_rounds(rows, BUDGET)
        assert (m["budget_hits"], m["budget_undetermined"]) == (0, 0)
    s0 = M.step0([_burn(rows)])
    assert s0["burn_control"]["state"] == "no" and s0["burn_control"]["produced"] is False
    assert s0["outcome"]["code"] == "burn-absent"


def test_a_failing_tokenizer_falls_back_to_the_bound_and_stays_undetermined():
    def broken(_text):
        raise RuntimeError("no count")
    rows = _record(count_tokens=broken)
    assert rows[0]["thinking_source"] == M.SOURCE_UPPER_BOUND
    assert M.budget_state(rows[0], BUDGET) == M.UNDETERMINED


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
    assert M.budget_state(rows[0], BUDGET) == M.HIT
    # A long visible answer takes the round under the threshold: no hit.
    long_answer = _record(count_tokens=lambda t: len(t), msg={"content": "y" * 3000})
    assert M.budget_state(long_answer[0], BUDGET) == M.NO_HIT


def test_an_engine_count_decides_either_way():
    hit = _record(usage=dict(USAGE, reasoning_tokens=11000, thinking_source="engine"))
    assert (hit[0]["reasoning_tokens"], hit[0]["thinking_source"]) == (11000, M.SOURCE_ENGINE)
    assert M.budget_state(hit[0], BUDGET) == M.HIT
    short = _record(usage=dict(USAGE, reasoning_tokens=4000, thinking_source="engine"))
    assert M.budget_state(short[0], BUDGET) == M.NO_HIT


def test_an_unattributed_count_is_not_trusted():
    rows = _record(usage={"reasoning_tokens": 11000, "completion_tokens": 12050})
    assert rows[0]["thinking_source"] == M.SOURCE_UPPER_BOUND
    assert M.budget_state(rows[0], BUDGET) == M.UNDETERMINED


def test_a_row_recorded_before_the_correction_is_rescored_as_undetermined():
    old = {"round": 1, "content_chars": 2598, "tool_calls": 0, "note_chars": 12023,
           "note_opening": "", "reasoning_tokens": 3005, "thinking_source": "estimated",
           "completion_tokens": 12050, "prompt_tokens": 3871, "served_effort": "medium"}
    # Even unconverted, the estimate decides nothing.
    raw = M.summarise_rounds([dict(old)], BUDGET)
    assert (raw["budget_hits"], raw["budget_undetermined"]) == (0, 1)
    fixed = M.correct_row(dict(old))
    assert (fixed["reasoning_tokens"], fixed["thinking_source"]) == (12050, M.SOURCE_UPPER_BOUND)
    assert fixed["reasoning_tokens_reported"] == 3005
    assert M.correct_row(fixed) == fixed  # idempotent
    m = M.summarise_rounds([fixed], BUDGET)
    assert (m["budget_hits"], m["budget_undetermined"]) == (0, 1)
    assert m["thinking_sources"] == {M.SOURCE_UPPER_BOUND: 1}


# -- the streak and the spread ---------------------------------------------------------

def _engine_rows(spec):
    """spec: (silent, reasoning) per round, engine-counted, on a deep prompt."""
    rec = M.RoundRecorder()
    for i, (silent, n) in enumerate(spec, 1):
        rec.record({"content": "" if silent else "text", "thinking": f"plan {i}"},
                   {"reasoning_tokens": n, "thinking_source": "engine",
                    "completion_tokens": n + 50, "prompt_tokens": 70000 + i}, 1.0)
    return rec.rows


def test_the_longest_silent_budget_streak_counts_only_consecutive_silent_hits():
    spec = ([(True, 10100)] * 3 + [(False, 10100)] + [(True, 10100)] * 7
            + [(True, 500)] + [(True, 10100)] * 2)
    m = M.summarise_rounds(_engine_rows(spec), BUDGET)
    assert m["budget_hits"] == 13
    assert m["silent_budget_hits"] == 12
    assert m["longest_silent_budget_streak"] == 7
    assert m["longest_silent_streak"] == 10  # silence alone runs on through the short round
    assert M.summarise_rounds(_engine_rows(spec), None)["longest_silent_budget_streak"] is None


def test_the_summary_carries_the_median_and_quartiles_of_the_per_round_count():
    m = M.summarise_rounds(_engine_rows([(False, n) for n in (100, 200, 300, 400, 500)]), BUDGET)
    assert m["reasoning_tokens_quartiles"] == {"q1": 200.0, "median": 300.0, "q3": 400.0}
    one = M.summarise_rounds(_engine_rows([(False, 700)]), BUDGET)
    assert one["reasoning_tokens_quartiles"] == {"q1": 700.0, "median": 700.0, "q3": 700.0}
    assert M.summarise_rounds([], BUDGET)["reasoning_tokens_quartiles"] is None


# -- step 0 for the incident tasks -------------------------------------------------------

def _task(rows, id_="long-audit-claims", repeat=1):
    m = M.summarise_rounds(rows, BUDGET)
    m.update(compaction_round=None, compactions=0, compaction_failures=0,
             rounds_to_completion=None)
    return {"id": id_, "arm": "off", "repeat": repeat, "kind": "incident", "passed": False,
            "timed_out": False, "seconds": 1.0, "metrics": m}


def test_an_incident_run_known_only_by_completions_is_undetermined_not_no():
    rec = M.RoundRecorder()
    for i in range(1, 9):
        big = i in (3, 4, 5)
        rec.record({"content": "", "thinking": "n"},
                   {"reasoning_tokens": 40, "thinking_source": "estimated",
                    "completion_tokens": 10150 if big else 900,
                    "prompt_tokens": 70000 + i}, 1.0)
    s0 = M.step0([_task(rec.rows)])
    assert s0["reproduced"] is None and s0["undetermined"] is True
    assert s0["budget_undetermined"] == 3
    assert M.step0_line(s0).startswith("incident symptom reproduced in the off arm: undetermined")
    assert s0["outcome"]["code"] == "undetermined"
    assert any("undetermined 1/1 run(s) (3 round(s))" in line for line in M.step0_task_lines(s0))


def test_an_incident_run_with_counted_silent_hits_reports_its_streak():
    s0 = M.step0([_task(_engine_rows([(True, 10100)] * 7 + [(False, 300)]))])
    assert s0["reproduced"] is True
    assert s0["longest_silent_budget_streak"] == 7
    assert "longest silent-at-budget run: 7" in M.step0_line(s0)
