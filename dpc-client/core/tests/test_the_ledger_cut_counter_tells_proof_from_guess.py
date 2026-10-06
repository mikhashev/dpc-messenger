"""`THE-MODEL-STARTS-EVERY-ROUND-WITHOUT-THE-REASONING-THAT-CHOSE-THE-TOOL` stays
open as a standing counter (2026-10-06). `eval/loop/ledger_cut_count.py` reads
the node ledger and sorts budgeted rounds into proof (`no_cut`, `cut`) and
guess (`undetermined`, with the clamp band apart). Synthetic rows only — no
real ledger, no model.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "eval" / "loop"))

import ledger_cut_count as lcc  # noqa: E402

B = 10_000
PROVIDERS = {"providers": [
    {"alias": "local27", "type": "llamacpp_server", "model": "m27", "reasoning_budget_tokens": B},
    {"alias": "vendor", "type": "deepseek", "model": "v4"},
]}
T0 = datetime(2026, 10, 5, 11, 0, tzinfo=timezone.utc)
_seq = iter(range(10_000))


def _row(completion, thinking=None, source="estimated", *, alias="local27", caller="agent_x",
         task_id="t1", minute=None, duration_s=60.0, **extra):
    n = next(_seq)
    started = T0 + timedelta(minutes=n if minute is None else minute)
    return {"alias": alias, "model": "m27" if alias == "local27" else "v4", "caller": caller,
            "task_id": task_id, "started_at": started.isoformat(), "completion_tokens": completion,
            "thinking_tokens": thinking, "thinking_source": source, "duration_s": duration_s,
            "output_includes_thinking": "includes", **extra}


def _count(rows):
    return lcc.count(rows_with_dt(rows), lcc.load_budgets(PROVIDERS))


def rows_with_dt(rows):
    for r in rows:
        r["_dt"] = lcc.parse_started_at(r["started_at"])
    return rows


def _cls(row):
    b, t, how = lcc.budget_for(row, lcc.load_budgets(PROVIDERS))
    return lcc.classify(row, b, t, how)


def test_a_completion_under_the_line_is_proof_of_no_cut():
    assert _cls(_row(9_799, 9_799)) == (lcc.NO_CUT, False)
    # Even an engine count is not needed: the completion holds the reasoning.
    assert _cls(_row(5_000, None, None)) == (lcc.NO_CUT, False)


def test_the_clamp_band_is_undetermined_and_flagged():
    assert _cls(_row(10_400, 10_400)) == (lcc.UNDETERMINED, True)
    assert _cls(_row(11_000, 11_000)) == (lcc.UNDETERMINED, True)
    # Clamped but above 1.1 x B, or below B: still undetermined, outside the band.
    assert _cls(_row(11_500, 11_500)) == (lcc.UNDETERMINED, False)
    assert _cls(_row(9_900, 9_900)) == (lcc.UNDETERMINED, False)


def test_an_unclamped_estimate_over_the_budget_decides_nothing():
    # The three proven cuts of 2026-10-05 read ~2 948 on chars/4.
    assert _cls(_row(10_600, 2_948)) == (lcc.UNDETERMINED, False)


def test_a_silent_round_over_the_budget_is_a_cut():
    assert _cls(_row(10_050, 2_948, content_chars=0, tool_calls=0)) == (lcc.CUT, False)
    # Visible text, or a tool call that shares the completion: not proof.
    assert _cls(_row(10_050, 2_948, content_chars=120, tool_calls=0))[0] == lcc.UNDETERMINED
    assert _cls(_row(10_050, 2_948, content_chars=0, tool_calls=1))[0] == lcc.UNDETERMINED
    # Silent but under B (inside the 0.98 margin): not proof either.
    assert _cls(_row(9_900, 2_948, content_chars=0, tool_calls=0))[0] == lcc.UNDETERMINED


def test_an_engine_count_at_the_budget_on_a_hard_cap_is_a_cut():
    assert _cls(_row(10_500, 10_000, "engine")) == (lcc.CUT, False)
    assert _cls(_row(10_500, 4_000, "engine"))[0] == lcc.UNDETERMINED


def test_vendor_rows_are_not_applicable_and_unknown_aliases_are_unmapped():
    assert _cls(_row(50_000, 50_000, "engine", alias="vendor")) == (lcc.NOT_APPLICABLE, False)
    unknown = _row(12_000, 12_000, alias="retired")
    unknown["model"] = "nobody"
    assert _cls(unknown) == (lcc.UNMAPPED, False)
    # A retired alias whose model maps to one budget keeps it.
    retired = _row(12_000, 2_000, alias="retired")
    retired["model"] = "m27"
    assert lcc.budget_for(retired, lcc.load_budgets(PROVIDERS)) == (B, "llamacpp_server", "model")


def test_legacy_rows_without_the_new_fields_are_never_read_as_silent():
    legacy = _row(10_300, 2_948)
    assert "content_chars" not in legacy and "tool_calls" not in legacy
    assert _cls(legacy) == (lcc.UNDETERMINED, False)
    nulls = _row(10_300, 2_948, content_chars=None, tool_calls=None)
    assert _cls(nulls) == (lcc.UNDETERMINED, False)


def test_episodes_are_runs_inside_one_caller_and_task():
    rows = [
        # task t1: over, over, under, over -> upper episodes of 2 and 1
        _row(10_400, 10_400, task_id="t1", minute=0),
        _row(10_700, 3_000, task_id="t1", minute=1),
        _row(4_000, 4_000, task_id="t1", minute=2),
        _row(10_100, 10_100, task_id="t1", minute=3),
        # task t2 interleaved in time: does not break t1's run
        _row(10_200, 10_200, task_id="t2", minute=1),
        # another caller, same task id: its own sequence
        _row(10_300, 10_300, caller="agent_y", task_id="t1", minute=1),
        _row(50_000, 50_000, "engine", alias="vendor", task_id="t1", minute=1),
    ]
    rep = _count(rows)
    x = rep["callers"]["agent_x"]
    assert (x["rows"], x["rows_with_budget"]) == (6, 5)
    assert (x[lcc.NO_CUT], x[lcc.UNDETERMINED], x[lcc.NOT_APPLICABLE]) == (1, 4, 1)
    assert x["undetermined_clamp_band"] == 3
    assert x["upper"]["episode_count"] == 3 and x["upper"]["longest_run"] == 2
    # Lower: t1 rows 0 and 3 are clamped (row 1 is not) -> three singletons.
    assert x["lower"]["episode_count"] == 3 and x["lower"]["longest_run"] == 1
    assert x["minutes_over_budget"] == 4.0
    assert x["minutes_total"] == 6.0
    assert rep["callers"]["agent_y"]["upper"]["episode_count"] == 1
    assert rep["total"]["upper"]["episode_count"] == 4


def test_rows_without_a_task_id_are_episodes_of_their_own():
    rows = [_row(10_400, 10_400, task_id=None, minute=m) for m in range(3)]
    rep = _count(rows)
    assert rep["total"]["upper"]["episode_count"] == 3
    assert rep["total"]["upper"]["longest_run"] == 1
    assert rep["total"]["rows_without_task_id"] == 3


def test_the_cli_reads_partitions_only_and_always_exits_zero(tmp_path, capsys):
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    rows = [_row(10_400, 10_400), _row(3_000, 3_000)]
    (ledger / "usage-2026-10.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    (ledger / "usage-2026-10.jsonl.before-fix").write_text(json.dumps(_row(10_400, 10_400)) + "\n", encoding="utf-8")
    prov = tmp_path / "providers.json"
    prov.write_text(json.dumps(PROVIDERS), encoding="utf-8")
    assert lcc.main(["--ledger-dir", str(ledger), "--providers", str(prov), "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["files"] == ["usage-2026-10.jsonl"]
    assert rep["total"]["rows"] == 2 and rep["total"][lcc.UNDETERMINED] == 1
    assert "2026-10-01" in rep["budget_note"]
    # Text mode states the budget's provenance too.
    assert lcc.main(["--ledger-dir", str(ledger), "--providers", str(prov)]) == 0
    assert "today's providers.json" in capsys.readouterr().out
    # A missing file is reported, not raised, and still exits 0.
    assert lcc.main(["--ledger-dir", str(ledger), "--providers", str(tmp_path / "nope.json")]) == 0


def test_local_dates_become_a_half_open_utc_window():
    tz = timezone(timedelta(hours=7))
    lo, hi = lcc.local_bounds("2026-09-29", "2026-10-06", tz)
    assert lo == datetime(2026, 9, 28, 17, 0, tzinfo=timezone.utc)
    assert hi == datetime(2026, 10, 6, 17, 0, tzinfo=timezone.utc)
