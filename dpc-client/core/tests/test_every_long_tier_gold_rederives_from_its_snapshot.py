"""Every gold of the loop eval's long tier re-derives from the snapshot it grades
against, a moved constant fails exactly the golds that read it, and the
`key=value` scorer reads what the tier asks for.

The long tier (`eval/loop/tasks_long.py`) grades a model on this repository's
own source, copied out with `git archive`. A gold is two derivations agreeing —
a reading at `GOLD_COMMIT` and a mechanical parse of the snapshot — and the run
refuses when they part. These tests snapshot HEAD, as a run does by default: if
a gold drifts from the code, this file goes red for the same reason the run
would refuse. No model, no network; the git tests skip without git.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "eval" / "loop"))
sys.path.insert(0, str(REPO_ROOT / "eval"))

import run_loop_eval as R  # noqa: E402
import tasks_long as T  # noqa: E402


def _git_works() -> bool:
    if shutil.which("git") is None:
        return False
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT),
                             capture_output=True, text=True, timeout=30)
    except Exception:
        return False
    return out.returncode == 0


needs_git = pytest.mark.skipif(not _git_works(), reason="git archive of this repository unavailable")


@pytest.fixture(scope="module")
def snapshot(tmp_path_factory):
    if not _git_works():
        pytest.skip("git archive of this repository unavailable")
    dest = tmp_path_factory.mktemp("long-tier") / "snapshot"
    info = T.snapshot_source(REPO_ROOT, T.resolve_commit(REPO_ROOT, "HEAD"), dest)
    assert info["files"] > 0
    return dest


def _bad(rows):
    return {(r["task"], r["key"]) for r in rows if not r["ok"]}


@needs_git
def test_the_snapshot_copy_lands_inside_each_task_root(snapshot, tmp_path):
    root = tmp_path / "long-01-off"
    T.build_fixture(root, snapshot)
    tasks = T.tasks_for(root)

    assert tasks
    for t in tasks:
        for rel in t["files"]:
            assert (root / "src" / T.PKG / rel).is_file(), f"{t['id']}: {rel} not in the root"
        if t["files"]:  # the burn control names no file at all
            assert str(root) in t["prompt"], f"{t['id']}: prompt points outside its root"


@needs_git
def test_every_gold_agrees_with_the_snapshot(snapshot):
    rows = T.verify_golds(snapshot)
    bad = [r for r in rows if not r["ok"]]
    assert not bad, "golds the snapshot no longer supports: " + "; ".join(
        f"{r['task']}.{r['key']} gold={r['gold']!r} derived={r['derived']!r} {r.get('error', '')}"
        for r in bad)


def _mutate(src: Path, rel: str, old: str, new: str) -> None:
    p = src / T.PKG / rel
    text = p.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"mutation anchor {old!r} is not unique in {rel}"
    p.write_text(text.replace(old, new), encoding="utf-8")


@needs_git
def test_a_moved_constant_fails_exactly_the_golds_that_read_it(snapshot, tmp_path):
    mutated = tmp_path / "mutated"
    shutil.copytree(snapshot, mutated)
    _mutate(mutated, "dpc_agent/loop.py",
            "TOOL_RESULT_CHAR_CAP = 15000", "TOOL_RESULT_CHAR_CAP = 16000")
    _mutate(mutated, "dpc_agent/context.py", "if round_idx > 8:", "if round_idx > 7:")

    assert _bad(T.verify_golds(mutated)) == {
        ("long-size-caps", "loop_result_cap"),
        ("long-compaction-ladder", "first_truncation_round_idx"),
    }


@needs_git
def test_a_derivation_that_cannot_find_its_anchor_is_reported_not_crashed(snapshot, tmp_path):
    mutated = tmp_path / "mutated"
    shutil.copytree(snapshot, mutated)
    (mutated / T.PKG / "dpc_agent" / "index_keys.py").write_text("# gone\n", encoding="utf-8")

    rows = [r for r in T.verify_golds(mutated) if not r["ok"]]
    # Both tasks that read index_keys.py: the retrieval constants and the chain.
    assert {(r["task"], r["key"]) for r in rows} == {("long-retrieval-constants", "*"),
                                                     ("long-control-multistep", "*")}
    assert all("LookupError" in r["error"] for r in rows)


# -- the key=value scorer --------------------------------------------------------

@pytest.fixture
def tasks(tmp_path):
    # tasks_for only builds paths; no snapshot needs to exist for scoring.
    return {t["id"]: t for t in T.tasks_for(tmp_path / "root")}


def _gold_answer(t) -> str:
    lines = [f"{k}={str(v).lower()}" for k, v in t["gold"].items()]
    lines += [f"{k}={v}" for k, v in t.get("gold_paths", {}).items()]
    # A place inside the first accepted span of each `where` / evidence key.
    lines += [f"{k}={spans[0][0]}:{spans[0][1]}" for k, spans in t.get("gold_where", {}).items()]
    places = t.get("gold_places")
    if places:
        lines += [f"{k}={p[0][0]}:{p[0][1]}" for k, p in zip(places["keys"], places["places"])]
    extra = ", ".join(t.get("gold_order", [])) + "\n" + " ".join(t.get("gold_names", []))
    return "working notes first\n" + extra + "\n" + "\n".join(lines)


def test_a_gold_shaped_answer_passes_every_long_task(tasks):
    for t in tasks.values():
        if not T.is_scored(t):
            continue  # the burn control has no gold (its own test file)
        v = R.check(t, _gold_answer(t))
        assert v["passed"], f"{t['id']}: {v['why']}"


def test_numbers_are_compared_as_numbers(tasks):
    t = tasks["long-size-caps"]
    good = "\n".join(f"{k}={v}" for k, v in t["gold"].items())
    assert R.check(t, good.replace("read_absolute=100000", "read_absolute=100,000"))["passed"]
    assert R.check(t, good.replace("shell_timeout=120", "shell_timeout=120.0"))["passed"]
    assert R.check(t, good.replace("shell_timeout=120", "**shell_timeout** = `120`."))["passed"]
    wrong = R.check(t, good.replace("loop_result_cap=15000", "loop_result_cap=16000"))
    assert not wrong["passed"]
    assert wrong["why"] == ["loop_result_cap=16000 (want 15000)"]


def test_the_last_occurrence_of_a_key_is_the_one_scored(tasks):
    t = tasks["long-size-caps"]
    good = "\n".join(f"{k}={v}" for k, v in t["gold"].items())
    assert R.check(t, "draft: loop_result_cap=999\n" + good)["passed"]
    assert not R.check(t, good + "\nloop_result_cap=999")["passed"]


def test_a_key_the_answer_never_wrote_is_missing_not_wrong(tasks):
    t = tasks["long-size-caps"]
    good = "\n".join(f"{k}={v}" for k, v in t["gold"].items())
    v = R.check(t, good.replace("shell_timeout=120", ""))
    assert not v["passed"]
    assert v["why"] == ["no shell_timeout="]


def test_the_guard_chain_out_of_order_fails(tasks):
    t = tasks["long-guard-chain"]
    fields = "\n".join(f"{k}={v}" for k, v in t["gold"].items())
    good_order = ", ".join(t["gold_order"])
    swapped = ("ToolLimitGuard, RoundLimitGuard, ResearchLimitGuard, LoopGuard, "
               "BudgetLimitGuard, ContextLimitGuard")
    assert R.check(t, good_order + "\n" + fields)["passed"]
    v = R.check(t, swapped + "\n" + fields)
    assert not v["passed"]
    assert v["why"] == ["wrong order"]


def test_text_and_boolean_fields_match_case_insensitively(tasks):
    t = tasks["long-notes-carry-path"]
    good = "\n".join(f"{k}={v}" for k, v in t["gold"].items())
    assert R.check(t, good.replace("flag_default=false", "flag_default=False"))["passed"]
    assert not R.check(t, good.replace("wire_field=reasoning_content", "wire_field=thinking"))["passed"]
