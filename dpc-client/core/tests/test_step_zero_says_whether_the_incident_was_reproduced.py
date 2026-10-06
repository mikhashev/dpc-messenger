"""Step 0 of the `preserve_reasoning` A/B says whether the off arm reproduced the
incident at all, and the verdict never ranks an arm on a run that did not.

`eval/loop/round_metrics.py` reads per-round counters into a step-0 line and a
side-by-side verdict. These tests feed it synthetic rounds shaped like the
2026-10-05 incident (26 rounds, 7 at the note budget, prompt 67 k -> 93.6 k,
per the board card) and like runs that only look like it. No model.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "eval" / "loop"))

import round_metrics as M  # noqa: E402

BUDGET = 10000


def _rows(n, hits, silent=(), first_prompt=67000, peak=93603):
    rec = M.RoundRecorder()
    for i in range(1, n + 1):
        notes = 10500 if i in hits else 1500
        rec.record({"content": "" if i in silent else "text",
                    "thinking": "same plan" if i > 2 else f"plan {i}"},
                   {"reasoning_tokens": notes, "thinking_source": "engine", "completion_tokens": notes + 100,
                    "prompt_tokens": first_prompt + (peak - first_prompt) * i // n}, 1.0)
    return rec.rows


def _result(id_, arm, metrics, passed=True, timed_out=False, **extra):
    m = dict(metrics)
    m.update({"compaction_round": None, "compactions": 0, "compaction_failures": 0,
              "rounds_to_completion": m["rounds"] if passed else None})
    m.update(extra)
    return {"id": id_, "arm": arm, "passed": passed, "timed_out": timed_out,
            "seconds": 100.0, "metrics": m}


INCIDENT_HITS = {14, 18, 19, 20, 22, 24, 25}


def test_the_incident_shape_is_counted_as_the_card_describes_it():
    m = M.summarise_rounds(_rows(26, INCIDENT_HITS, silent=set(range(14, 27)) - {16}), BUDGET)
    assert m["budget_hit_rounds"] == sorted(INCIDENT_HITS)
    assert m["silent_rounds"] == 12
    assert m["peak_prompt_tokens"] == 93603
    assert (m["deep_rounds"], m["deep_budget_hits"]) == (26, 7)
    assert M.summarise_rounds(_rows(26, INCIDENT_HITS), None)["budget_hits"] is None


def test_step0_says_yes_on_the_incident_and_prints_both_thresholds():
    m = M.summarise_rounds(_rows(26, INCIDENT_HITS), BUDGET)
    s = M.step0([_result("a", "off", m)])
    assert s["reproduced"] is True
    assert (s["budget_hits"], s["peak_prompt"]) == (7, 93603)
    assert (s["deep_budget_hits"], s["deep_rounds"]) == (7, 26)
    line = M.step0_line(s)
    assert "yes" in line
    assert "budget hits 7" in line
    assert "peak prompt 93603" in line
    assert "share past 60000: 7/26 = 0.27" in line
    assert "reproducing: a" in line


def test_step0_says_no_on_a_shallow_off_arm_and_says_why():
    shallow = M.summarise_rounds(_rows(5, set(), first_prompt=12000, peak=20000), BUDGET)
    s = M.step0([_result("a", "off", shallow), _result("a", "on", shallow)])
    assert s["reproduced"] is False
    assert "measured nothing" in s["why"]
    assert M.step0_line(s).startswith("incident symptom reproduced in the off arm: no")


def test_two_capped_thoughts_in_a_long_deep_run_are_not_the_incident():
    # Two hits, peak past 60 k — the old rule's yes — but 2 of 12 deep rounds.
    m = M.summarise_rounds(_rows(12, {3, 9}, first_prompt=61000, peak=90000), BUDGET)
    assert (m["budget_hits"], m["deep_rounds"], m["deep_budget_hits"]) == (2, 12, 2)
    assert M.task_symptom(m) is False
    assert M.step0([_result("a", "off", m)])["reproduced"] is False


def test_step0_is_not_measured_without_an_off_arm_or_without_a_budget():
    m = M.summarise_rounds(_rows(26, INCIDENT_HITS), BUDGET)
    assert M.step0([_result("a", "on", m)])["reproduced"] is None
    unbudgeted = M.summarise_rounds(_rows(26, INCIDENT_HITS), None)
    s = M.step0([_result("a", "off", unbudgeted)])
    assert s["reproduced"] is None
    assert "not measured" in M.step0_line(s)


def test_step0_names_off_arm_tasks_the_harness_cut_off():
    shallow = M.summarise_rounds(_rows(5, set(), first_prompt=12000, peak=20000), BUDGET)
    s = M.step0([_result("a", "off", shallow, passed=False, timed_out=True)])
    assert s["timed_out_off"] == ["a"]
    assert "harness timeout" in s["why"]


def test_axes_carry_no_direction_field():
    assert all(len(axis) == 2 for axis in M.AXES)


def test_the_verdict_warns_instead_of_ranking():
    m = M.summarise_rounds(_rows(26, INCIDENT_HITS), BUDGET)
    quiet_on = dict(m, budget_hits=1, silent_rounds=20)
    txt = "\n".join(M.verdict_lines([
        _result("a", "off", m),
        _result("a", "on", quiet_on, compaction_round=9, compactions=2),
    ]))
    assert "larger share of rounds" in txt
    assert "compacted earlier" in txt
    assert "not read as a result" in txt


def test_a_timed_out_task_is_its_own_axis_and_its_own_warning():
    m = M.summarise_rounds(_rows(26, INCIDENT_HITS), BUDGET)
    lines = M.verdict_lines([_result("a", "off", m),
                             _result("a", "on", m, passed=False, timed_out=True)])
    assert any(line.strip().startswith("timed_out") and "on=" in line and "True" in line
               for line in lines)
    assert any("hit the harness timeout" in line and "not a failure" in line for line in lines)


def test_totals_are_printed_over_all_tasks_and_over_the_reproducing_subset():
    deep = M.summarise_rounds(_rows(26, INCIDENT_HITS), BUDGET)
    shallow = M.summarise_rounds(_rows(5, set(), first_prompt=12000, peak=20000), BUDGET)
    lines = M.verdict_lines([
        _result("deep", "off", deep), _result("deep", "on", deep),
        _result("flat", "off", shallow), _result("flat", "on", shallow),
    ])
    txt = "\n".join(lines)
    assert "all 2 paired task(s)" in txt
    assert "reproducing subset, 1 task(s): deep" in txt
    i_all = lines.index("  all 2 paired task(s)")
    i_sub = next(i for i, line in enumerate(lines) if "reproducing subset, 1" in line)

    def rounds_total(block):
        line = next(line for line in block if line.strip().startswith("rounds "))
        return re.sub(r"=\s+", "=", line).split()

    assert rounds_total(lines[i_all:i_sub]) == ["rounds", "off=31", "on=31"]
    assert rounds_total(lines[i_sub:]) == ["rounds", "off=26", "on=26"]


def test_an_empty_reproducing_subset_is_said_out_loud():
    shallow = M.summarise_rounds(_rows(5, set(), first_prompt=12000, peak=20000), BUDGET)
    lines = M.verdict_lines([_result("flat", "off", shallow), _result("flat", "on", shallow)])
    assert any("reproducing subset: empty" in line for line in lines)
