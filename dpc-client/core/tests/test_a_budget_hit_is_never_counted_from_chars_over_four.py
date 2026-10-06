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


# -- round 3: finish_reason, the incident's unit, loud fallbacks, derived hits ----------

def test_finish_reason_and_max_tokens_reach_every_row_and_length_is_counted():
    rec = M.RoundRecorder()
    rec.record({"content": "", "thinking": "n"},
               dict(USAGE, finish_reason="length", max_tokens=16384), 1.0)
    # A server that caps only the reasoning and then calls a tool says tool_calls.
    rec.record({"content": "", "thinking": "n"}, dict(USAGE, finish_reason="tool_calls"), 1.0)
    assert [(r["finish_reason"], r["max_tokens"]) for r in rec.rows] == [
        ("length", 16384), ("tool_calls", None)]
    m = M.summarise_rounds(rec.rows, BUDGET)
    assert (m["length_rounds"], m["length_round_list"]) == (1, [1])
    burn = M.step0([_burn(rec.rows)])["burn_control"]
    assert burn["length_rounds"] == 1 and "length-cut rounds: 1" in M.burn_line(burn)
    # No round carrying a finish_reason (none given, or a row from before the
    # field) is "not recorded", never 0.
    assert M.summarise_rounds(_record(), BUDGET)["length_rounds"] is None
    legacy = [{k: v for k, v in _record()[0].items() if k != "finish_reason"}]
    assert M.summarise_rounds(legacy, BUDGET)["length_rounds"] is None
    assert "length-cut rounds: not recorded" in M.burn_line(
        M.step0([_burn(legacy)])["burn_control"])


def _incident_round(rec, count_tokens=None, thinking="0123456789" * 1100):
    """One synthetic round shaped like the incident's capped ones: silent, two
    tool calls, the provider's estimate clamped to the completion."""
    rec.count_tokens = count_tokens
    rec.record({"content": "", "thinking": thinking,
                "tool_calls": [{"function": {"name": "t", "arguments": "{}"}}] * 2},
               {"reasoning_tokens": 10150, "thinking_source": "estimated",
                "completion_tokens": 10150, "prompt_tokens": 85000,
                "finish_reason": "tool_calls"}, 1.0)


def test_the_incident_constant_is_in_the_completion_unit_and_never_compared_with_hits():
    assert not hasattr(M, "INCIDENT_SILENT_BUDGET_HITS")
    rec = M.RoundRecorder()
    for _ in range(7):
        _incident_round(rec)
    m = M.summarise_rounds(rec.rows, BUDGET)
    assert m["silent_completion_at_budget"] == M.INCIDENT_SILENT_COMPLETION_AT_BUDGET == 7
    assert m["silent_budget_hits"] == M.INCIDENT_SILENT_BUDGET_HITS_NEW_RULE == 0
    assert m["silent_budget_undetermined"] == M.INCIDENT_SILENT_BUDGET_UNDETERMINED_NEW_RULE == 7
    assert m["length_rounds"] == 0  # the caveat: a capped trace still said tool_calls
    # With the model's tokenizer over the notes the same rows are hits.
    counted = M.RoundRecorder()
    for _ in range(7):
        _incident_round(counted, count_tokens=lambda t: len(t))
    mc = M.summarise_rounds(counted.rows, BUDGET)
    assert (mc["silent_budget_hits"], mc["thinking_sources"]) == (7, {M.SOURCE_TOKENIZER: 7})
    # ...and with a tokenizer but no note text, by the derived source.
    derived = M.RoundRecorder()
    for _ in range(7):
        _incident_round(derived, count_tokens=lambda t: len(t) // 4, thinking="")
    md = M.summarise_rounds(derived.rows, BUDGET)
    assert (md["silent_budget_hits"], md["budget_hits_derived"]) == (7, 7)
    line = M.step0_line(M.step0([_task(rec.rows)]))
    assert "the incident: 7, completion unit" in line
    assert "[the incident: 7]" not in line


def test_a_failing_tokenizer_is_counted_and_printed_not_swallowed():
    def broken(_text):
        raise RuntimeError("no count")
    rows = _record(count_tokens=broken)
    assert rows[0]["tokenizer_error"] == "RuntimeError: no count"
    m = M.summarise_rounds(rows, BUDGET)
    assert (m["tokenizer_failed_rows"], m["tokenizer_first_error"]) == (1, "RuntimeError: no count")
    line = M.burn_line(M.step0([_burn(rows)])["burn_control"])
    assert "TOKENIZER FAILED on 1 round(s), first: RuntimeError: no count" in line
    assert "sources: completion_upper_bound=1" in line


def test_the_burn_line_prints_the_source_histogram():
    line = M.burn_line(M.step0([_burn(_record(count_tokens=_digit_tokenizer))])["burn_control"])
    assert "sources: tokenizer=1" in line and "TOKENIZER FAILED" not in line


def test_a_verdict_resting_only_on_derived_hits_says_so():
    rows = _record(count_tokens=lambda t: len(t) // 4, msg={"content": "short"})
    assert rows[0]["thinking_source"] == M.SOURCE_MINUS_VISIBLE
    s0 = M.step0([_burn(rows)])
    burn = s0["burn_control"]
    assert burn["state"] == "yes" and burn["derived_only"] is True
    assert M.burn_line(burn).startswith(f"burn produced: yes [{M.DERIVED_NOTE}]")
    assert s0["outcome"]["derived"] is True and M.DERIVED_NOTE in s0["outcome"]["text"]
    # One measured hit beside it: no longer derived only.
    mixed = M.step0([_burn(rows), _burn(_record(count_tokens=_digit_tokenizer), repeat=2)])
    assert mixed["burn_control"]["derived_only"] is False
    assert M.DERIVED_NOTE not in M.burn_line(mixed["burn_control"])
    # The incident tasks the same way.
    rec = M.RoundRecorder()
    for _ in range(3):
        _incident_round(rec, count_tokens=lambda t: len(t) // 4, thinking="")
    inc = M.step0([_task(rec.rows)])
    assert inc["reproduced"] is True and inc["derived_only"] is True
    assert M.DERIVED_NOTE in M.step0_line(inc)


def test_the_run_start_line_is_loud_without_a_tokenizer():
    sys.path.insert(0, str(REPO_ROOT / "eval"))
    import run_loop_eval as R
    loud = R.tokenizer_banner(None)
    assert loud.startswith("!!! NO TOKENIZER") and "'no' or 'undetermined'" in loud

    class _Tok:
        def describe(self):
            return "llama-tokenize.exe (vocabulary only) on m.gguf"
    assert R.tokenizer_banner(_Tok()) == (
        "reasoning count per round: tokenizer (llama-tokenize.exe (vocabulary only) on m.gguf)")
