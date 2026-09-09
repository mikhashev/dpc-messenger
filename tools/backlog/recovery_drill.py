"""Watch the loss of 2026-09-09 fail to happen again, against a throwaway board.

Run it:  uv run python tools/backlog/recovery_drill.py

`verbs_fixture.py` asks what a verb writes. This asks what survives when a write goes
wrong, which is a different question and was answered by nothing until the board was
truncated to zero bytes. It needs no real backlog: it builds one in a temp directory,
reproduces the incident against it, then drives every guard that now stands between that
line and the file.

Four things are watched here:

  1. the incident itself — a raw `open(path, "wb")` whose write expression raises — so the
     shape being defended against is on the record rather than remembered;
  2. every verb takes a snapshot and leaves the board whole;
  3. a write that fails part-way leaves the original byte-identical;
  4. a snapshot restores to exactly what was there before.

Stdlib only and no virtualenv, the same constraint build.py itself carries. The fixture
board and the subprocess runner are imported from verbs_fixture rather than copied — one
throwaway board, described in one place.
"""
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verbs_fixture import ARCHIVE, BACKLOG, ROADMAP, run   # noqa: E402

BLOCKED_SUFFIX = ".blocked-tmp"
passed, failed = [], []


def check(label, condition, detail=""):
    (passed if condition else failed).append(label)
    print(f"  {'ok  ' if condition else 'FAIL'}  {label}"
          + (f"\n          {detail}" if not condition and detail else ""))


def snaps(work):
    return sorted((work / "backups").glob("backlog.*.auto.md"))


def stamp(days_ago, hhmmss="120000"):
    d = datetime.now(timezone.utc).date() - timedelta(days=days_ago)
    return f"{d:%Y%m%d}T{hhmmss}.000000Z"


def incident(work):
    """The exact line that lost the board, run against a copy of one.

    `open(p, "wb")` truncates at open, before the argument expression is evaluated. The
    concatenation then raises, so nothing is written and the file is left empty — and any
    guard the script carried sat after the open and could not fire.
    """
    print("\n-- 1. the incident, reproduced --")
    p = work / "incident.md"
    shutil.copyfile(work / "backlog.md", p)
    before = p.stat().st_size
    add = "- **2026-09-09, CC:** an observation"          # str, where the rest is bytes
    b = p.read_bytes()
    j = b.index(b"## IN PROGRESS")
    raised = ""
    try:
        open(p, "wb").write(b[:j] + add + b[j:])
    except TypeError as exc:
        raised = str(exc)
    after = p.stat().st_size
    print(f"          {before} bytes before · {after} bytes after · raised: {raised}")
    check("the incident still reproduces: the raising write leaves an empty file",
          before > 0 and after == 0 and raised,
          f"{before} -> {after}, raised {raised!r}")
    check("nothing of the board survived that write",
          p.read_bytes() == b"", repr(p.read_bytes()[:80]))


def verbs_snapshot_and_keep_the_board_whole(work):
    print("\n-- 2. every verb snapshots first, and leaves a whole board --")
    verbs = [
        ("add", ("add", "DRILL-ENTRY-WAS-ADDED", "--desc=an entry written by the drill",
                 "--priority=LOW", "--axis=honesty", "--origin=CC: recovery drill",
                 "--observed=written by build.py add", "--by=CC")),
        ("append", ("append", "DRILL-ENTRY-WAS-ADDED",
                    "--text=an observation appended by the drill", "--by=CC")),
        ("move", ("move", "DRILL-ENTRY-WAS-ADDED", "--to=IN PROGRESS", "--by=CC")),
        ("rename", ("rename", "DRILL-ENTRY-WAS-ADDED", "DRILL-ENTRY-WAS-RENAMED",
                    "--by=CC")),
        ("close", ("close", "DRILL-ENTRY-WAS-RENAMED", "--session=S2026-09-09.1",
                   "--resolution=fixed", "--evidence=commit deadbeef, seen in the drill",
                   "--by=CC")),
    ]
    for name, args in verbs:
        before = len(snaps(work))
        code, out = run(work, *args)
        board = (work / "backlog.md").read_text(encoding="utf-8")
        check(f"{name} took a snapshot before writing",
              code == 0 and len(snaps(work)) > before,
              f"exit {code}, {before} -> {len(snaps(work))} snapshots\n{out[-300:]}")
        check(f"{name} left a whole board behind",
              board.strip() and "## OPEN" in board, out[-300:])
    check("close snapshotted the archive it was about to rewrite",
          bool(sorted((work / "backups").glob("backlog_closed.*.auto.md"))),
          str([p.name for p in (work / "backups").glob("*")]))
    code, out = run(work, "--check")
    check("the board every verb touched still passes --check", code == 0, out[-400:])


def a_failing_write_leaves_the_original(work):
    """Force `open(tmp, "wb")` inside _atomic_write_bytes to fail, mid-verb.

    A directory standing where the temporary file goes makes the open raise for a reason
    the code cannot special-case — which is the point: the guarantee has to hold for any
    failure, not for a list of ones somebody thought of.
    """
    print("\n-- 3. a write that fails part-way leaves the original untouched --")
    blocker = work / ("backlog.md" + BLOCKED_SUFFIX)
    blocker.mkdir(exist_ok=True)
    before = (work / "backlog.md").read_bytes()
    before_arc = (work / "backlog_closed.md").read_bytes()
    n_snaps = len(snaps(work))
    code, out = run(work, "append", "BETA-ENTRY-POINTS-AT-ALPHA",
                    "--text=this write is going to fail", "--by=CC",
                    env={"DPC_BACKLOG_TMP_SUFFIX": BLOCKED_SUFFIX})
    after = (work / "backlog.md").read_bytes()
    check("the forced failure actually happened", code != 0, f"exit {code}\n{out[-300:]}")
    check("backlog.md is byte-identical after the failed write",
          after == before,
          f"{len(before)} bytes before, {len(after)} after")
    check("nothing of the failed write reached the board",
          b"this write is going to fail" not in after)
    check("backlog_closed.md is byte-identical too",
          (work / "backlog_closed.md").read_bytes() == before_arc)
    check("the snapshot was still taken before the write failed",
          len(snaps(work)) == n_snaps + 1,
          f"{n_snaps} -> {len(snaps(work))}")
    check("no temporary file was left lying beside the board",
          not [p for p in work.glob("backlog.md.tmp-*")],
          str([p.name for p in work.glob("backlog.md.*")]))
    blocker.rmdir()
    return before


def an_unchanged_board_is_not_snapshotted_twice(work):
    """The previous step left the board exactly as the last snapshot recorded it."""
    print("\n-- 4. an unchanged board is not copied again --")
    n_snaps = len(snaps(work))
    code, out = run(work, "append", "BETA-ENTRY-POINTS-AT-ALPHA",
                    "--text=an observation that does change the board", "--by=CC")
    check("the verb ran", code == 0, out[-300:])
    check("no second copy of identical content",
          len(snaps(work)) == n_snaps and "unchanged since" in out,
          f"{n_snaps} -> {len(snaps(work))}\n{out[-400:]}")


def retention_keeps_a_week_then_a_month(work):
    print("\n-- 5. retention: every snapshot for 7 days, one a day for 30, then nothing --")
    back = work / "backups"
    planted = {
        "old":        back / f"backlog.{stamp(40)}.auto.md",
        "window_old": back / f"backlog.{stamp(15, '010000')}.auto.md",
        "window_new": back / f"backlog.{stamp(15, '020000')}.auto.md",
        "recent_a":   back / f"backlog.{stamp(3, '010000')}.auto.md",
        "recent_b":   back / f"backlog.{stamp(3, '020000')}.auto.md",
        "by_hand":    back / "backlog_2026-01-01_pre-something.md",
    }
    for p in planted.values():
        p.write_text("planted by the recovery drill\n", encoding="utf-8")
    run(work, "append", "BETA-ENTRY-POINTS-AT-ALPHA",
        "--text=a write whose only job is to run the pruner", "--by=CC")
    check("a snapshot older than 30 days is pruned", not planted["old"].exists())
    check("in the 7-to-30-day window only the newest of a day survives",
          planted["window_new"].exists() and not planted["window_old"].exists(),
          f"new={planted['window_new'].exists()} old={planted['window_old'].exists()}")
    check("inside 7 days every snapshot is kept",
          planted["recent_a"].exists() and planted["recent_b"].exists())
    check("a hand-made copy in the same directory is never touched",
          planted["by_hand"].exists())


def a_snapshot_restores_what_was_there(work):
    print("\n-- 6. a snapshot restores the board it was taken from --")
    before = (work / "backlog.md").read_bytes()
    code, out = run(work, "append", "BETA-ENTRY-POINTS-AT-ALPHA",
                    "--text=the edit a restore has to undo", "--by=CC")
    check("the verb that takes the snapshot ran", code == 0, out[-300:])
    newest = max(snaps(work), key=lambda p: p.name)
    check("the newest snapshot holds the board as it was before that verb",
          newest.read_bytes() == before,
          f"{newest.name}: {len(newest.read_bytes())} bytes vs {len(before)} before")

    # Now lose it the way it was lost, and put it back.
    (work / "backlog.md").write_bytes(b"")
    check("the board is empty, as it was on 2026-09-09",
          (work / "backlog.md").stat().st_size == 0)
    shutil.copyfile(newest, work / "backlog.md")
    check("the restored board is byte-identical to what the snapshot held",
          (work / "backlog.md").read_bytes() == before)
    code, out = run(work, "--check")
    check("the restored board passes --check", code == 0, out[-400:])


def main():
    work = Path(tempfile.mkdtemp(prefix="backlog-drill-"))
    print(f"drill in {work}")
    try:
        (work / "backlog.md").write_text(BACKLOG, encoding="utf-8")
        (work / "backlog_closed.md").write_text(ARCHIVE, encoding="utf-8")
        (work / "ROADMAP.md").write_text(ROADMAP, encoding="utf-8")
        run(work, "--roadmap")

        incident(work)
        verbs_snapshot_and_keep_the_board_whole(work)
        a_failing_write_leaves_the_original(work)
        an_unchanged_board_is_not_snapshotted_twice(work)
        retention_keeps_a_week_then_a_month(work)
        a_snapshot_restores_what_was_there(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        for f in failed:
            print(f"  FAILED  {f}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
