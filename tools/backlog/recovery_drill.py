"""Watch the loss of 2026-09-09 fail to happen again, against a throwaway board.

Run it:  uv run python tools/backlog/recovery_drill.py

`verbs_fixture.py` asks what a verb writes. This asks what survives when a write goes
wrong, which is a different question and was answered by nothing until the board was
truncated to zero bytes. It needs no real backlog: it builds one in a temp directory,
reproduces the incident against it, then drives every guard that now stands between that
line and the file.

What is watched here:

  1. the incident itself — a raw `open(path, "wb")` whose write expression raises — so the
     shape being defended against is on the record rather than remembered;
  2. the same incident against a *protected* board, which is the case the tripwire exists
     for: the write is refused before it can truncate, and the board is byte-identical;
  3. every verb takes a snapshot and leaves the board whole;
  4. a verb still works against a protected board, and leaves it protected;
  5. the board is left protected when a verb refuses, and when one fails mid-write;
  6. a write that fails part-way leaves the original byte-identical;
  7. the hourly snapshot: due and changed copies, due and unchanged does not;
  8. a snapshot restores to exactly what was there before.

Stdlib only and no virtualenv, the same constraint build.py itself carries. The fixture
board and the subprocess runner are imported from verbs_fixture rather than copied — one
throwaway board, described in one place.
"""
import os
import shutil
import stat
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verbs_fixture import ARCHIVE, BACKLOG, ROADMAP, rmtree, run   # noqa: E402

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


def hours_ago(h):
    return f"{datetime.now(timezone.utc) - timedelta(hours=h):%Y%m%dT%H%M%S.%f}Z"


def writable(path):
    return bool(stat.S_IMODE(path.stat().st_mode) & 0o222)


def unprotect(path):
    """The documented escape hatch: `attrib -R` / `chmod u+w`, by another spelling."""
    os.chmod(path, stat.S_IMODE(path.stat().st_mode) | 0o200)


def board_beside(work, name, roadmap=True):
    """A second throwaway board in its own directory, with its own snapshot dir."""
    d = work / name
    d.mkdir(exist_ok=True)
    files = ("backlog.md", "backlog_closed.md") + (("ROADMAP.md",) if roadmap else ())
    for f in files:
        shutil.copyfile(work / f, d / f)
    return d


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


def the_incident_against_a_protected_board(work):
    """The same line, against the board as it is left between edits.

    This is the case the whole tripwire exists for. What is asserted is *which* exception
    arrives: a PermissionError means the open was refused, so the truncation never
    happened; the TypeError of the unprotected case would mean the file was already empty
    by the time the concatenation was evaluated.
    """
    print("\n-- 2. the same incident against a read-only board --")
    d = board_beside(work, "protected")
    code, out = run(d, "--check")
    p = d / "backlog.md"
    check("a --check run arms the board it read", code == 0 and not writable(p),
          f"exit {code}, writable={writable(p)}\n{out[-300:]}")
    check("the archive beside it is armed too", not writable(d / "backlog_closed.md"))

    before = p.read_bytes()
    j = before.index(b"## IN PROGRESS")
    add = "- **2026-09-09, CC:** an observation"
    raised = ""
    try:
        open(p, "wb").write(before[:j] + add + before[j:])
    except BaseException as exc:
        raised = f"{type(exc).__name__}: {exc}"
    print(f"          raised: {raised}")
    check("the incident write is refused at the open, before it can truncate",
          raised.startswith("PermissionError"), raised or "nothing raised at all")
    check("the board is byte-identical afterwards", p.read_bytes() == before,
          f"{len(before)} bytes before, {p.stat().st_size} after")

    # The escape hatch has to work, or the protection gets removed the first time it is
    # inconvenient — and the tool has to put the bit back on its own afterwards.
    unprotect(p)
    p.write_bytes(before[:j] + add.encode() + before[j:])
    check("a hand edit goes through once the bit is cleared",
          b"an observation" in p.read_bytes())
    code, out = run(d, "--check")
    check("the next run re-arms the board a hand edit left writable",
          not writable(p), out[-300:])


def a_verb_works_against_a_protected_board(work):
    print("\n-- 4. a verb writes through the protection and restores it --")
    board = work / "backlog.md"
    check("the board is read-only before the verb runs", not writable(board))
    code, out = run(work, "append", "BETA-ENTRY-POINTS-AT-ALPHA",
                    "--text=written while the board was read-only", "--by=CC")
    text = board.read_text(encoding="utf-8")
    check("the verb wrote anyway",
          code == 0 and "written while the board was read-only" in text, out[-400:])
    check("the board is read-only again afterwards", not writable(board),
          "the replace leaves the temp file's mode behind, so this is the re-apply")
    code, out = run(work, "close", "DELTA-ENTRY-HAS-NO-BODY", "--session=S2026-09-09.2",
                    "--resolution=moot", "--evidence=the drill needed a closed entry",
                    "--by=CC")
    check("the archive a close rewrote is read-only too",
          code == 0 and not writable(work / "backlog_closed.md"), out[-400:])

    # The two checks above pass even with the restoration deleted, because a successful
    # `_commit` ends by rendering the roadmap in a subprocess and *that* run arms the board
    # on its way in. Measured: with `_protect` removed from `_atomic_write_bytes` the drill
    # stayed green. So the restoration gets a case where nothing can follow it — a `close`
    # whose second write dies, leaving the first write's own `finally` as the only thing
    # that could have put the bit back.
    d = board_beside(work, "half-written")
    run(d, "--check")
    board, arch = d / "backlog.md", d / "backlog_closed.md"
    (d / ("backlog_closed.md" + BLOCKED_SUFFIX)).mkdir()      # only the archive's tmp path
    check("the isolating board starts read-only", not writable(board))
    arc_before = arch.read_bytes()
    code, out = run(d, "close", "ALPHA-ENTRY-EXISTS", "--session=S2026-09-09.3",
                    "--resolution=moot", "--evidence=the drill needed a failing archive",
                    "--by=CC", env={"DPC_BACKLOG_TMP_SUFFIX": BLOCKED_SUFFIX})
    # The reason matters: exit 2 would mean it never got as far as writing anything.
    check("the close died on its second write, not before it",
          code != 0 and "PermissionError" in out and "backlog_closed.md" in out,
          f"exit {code}" + out[-400:])
    check("its first write had already landed",
          "### ALPHA-ENTRY-EXISTS" not in board.read_text(encoding="utf-8"), out[-300:])
    check("and that write put the bit back with nothing following it", not writable(board),
          "the process died before the roadmap subprocess, so only the finally in "
          "_atomic_write_bytes could have")
    check("the archive it never reached is byte-identical and still read-only",
          not writable(arch) and arch.read_bytes() == arc_before)


def a_refusal_leaves_the_board_protected(work):
    print("\n-- 5. a verb that refuses leaves the board protected --")
    board = work / "backlog.md"
    before = board.read_bytes()
    code, out = run(work, "append", "NO-SUCH-ENTRY-AT-ALL", "--text=x", "--by=CC")
    check("the verb refused before writing", code != 0, out[-300:])
    check("the board is still read-only after a refusal", not writable(board))
    check("and byte-identical", board.read_bytes() == before)

    # Refused by the checker rather than by argument parsing: this one reaches _commit.
    code, out = run(work, "add", "REFUSED-BY-THE-CHECKER", "--desc=описание",
                    "--priority=LOW", "--axis=honesty", "--origin=CC: drill",
                    "--observed=z", "--by=CC")
    check("a write the checker refuses is not written", code == 1
          and b"REFUSED-BY-THE-CHECKER" not in board.read_bytes(), out[-300:])
    check("the board is still read-only after that refusal too", not writable(board))


def verbs_snapshot_and_keep_the_board_whole(work):
    print("\n-- 3. every verb snapshots first, and leaves a whole board --")
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
    print("\n-- 6. a write that fails part-way leaves the original untouched --")
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
    print("\n-- 7a. an unchanged board is not copied again --")
    n_snaps = len(snaps(work))
    code, out = run(work, "append", "BETA-ENTRY-POINTS-AT-ALPHA",
                    "--text=an observation that does change the board", "--by=CC")
    check("the verb ran", code == 0, out[-300:])
    check("no second copy of identical content",
          len(snaps(work)) == n_snaps and "unchanged since" in out,
          f"{n_snaps} -> {len(snaps(work))}\n{out[-400:]}")


def retention_keeps_a_week_then_a_month(work):
    print("\n-- 7b. retention: every snapshot for 7 days, one a day for 30, then nothing --")
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


def the_hourly_snapshot(work):
    """Taken by the clock rather than by a verb, which is what covers a hand edit.

    All four cells of due x changed, because the two skips are independent and either one
    alone makes the other look enforced: an unchanged board is skipped by the content hash
    whether or not an hour has passed, so only a *changed* board inside the hour says
    anything about the interval.

    Its own directory: the main board is snapshotted by every verb the drill runs, so the
    counts there would say nothing about this trigger.
    """
    print("\n-- 8. the hourly snapshot --")
    d = board_beside(work, "hourly")
    back = d / "backups"
    board = d / "backlog.md"

    def age_the_newest():
        # Age is read from the name, so this is how an hour is made to have passed. The
        # loop is a guard, not a loop: with the trigger disabled there is nothing to age,
        # and the checks below have to report that rather than die and hide what follows.
        for newest in snaps(d)[-1:]:
            newest.rename(back / f"backlog.{hours_ago(2)}.auto.md")

    def hand_edit(marker):
        unprotect(board)
        board.write_text(
            board.read_text(encoding="utf-8").replace(
                "## IN PROGRESS", f"- **2026-09-09, CC:** {marker}\n\n## IN PROGRESS"),
            encoding="utf-8")

    code, out = run(d, "--check")
    check("the first --check on a board with no snapshot takes one",
          code == 0 and len(snaps(d)) == 1, f"{len(snaps(d))} snapshots " + out[-300:])

    run(d, "--check")
    check("not due, unchanged: no copy", len(snaps(d)) == 1, f"{len(snaps(d))}")

    hand_edit("edited inside the hour")
    code, out = run(d, "--check")
    check("not due but changed: still no copy — the interval is what holds here",
          len(snaps(d)) == 1, f"{len(snaps(d))} snapshots " + out[-300:])
    check("that run re-armed the board the hand edit left writable", not writable(board))

    age_the_newest()
    code, out = run(d, "--snapshot")
    check("due and changed: the hand edit is copied",
          len(snaps(d)) == 2, f"{len(snaps(d))} snapshots " + out[-400:])
    check("the copy holds the hand edit the tool never made",
          bool(snaps(d)) and "edited inside the hour" in
          max(snaps(d), key=lambda q: q.name).read_text(encoding="utf-8"),
          f"{len(snaps(d))} snapshots")

    age_the_newest()
    code, out = run(d, "--check")
    check("due but unchanged: no second copy of identical content",
          len(snaps(d)) == 2 and "unchanged since" in out,
          f"{len(snaps(d))} snapshots " + out[-400:])


def a_snapshot_restores_what_was_there(work):
    print("\n-- 9. a snapshot restores the board it was taken from --")
    before = (work / "backlog.md").read_bytes()
    code, out = run(work, "append", "BETA-ENTRY-POINTS-AT-ALPHA",
                    "--text=the edit a restore has to undo", "--by=CC")
    check("the verb that takes the snapshot ran", code == 0, out[-300:])
    newest = max(snaps(work), key=lambda p: p.name)
    check("the newest snapshot holds the board as it was before that verb",
          newest.read_bytes() == before,
          f"{newest.name}: {len(newest.read_bytes())} bytes vs {len(before)} before")

    # Now lose it the way it was lost, and put it back. The loss has to be staged by
    # hand — with the tripwire armed the board cannot be emptied at all, which is case 2.
    unprotect(work / "backlog.md")
    (work / "backlog.md").write_bytes(b"")
    check("the board is empty, as it was on 2026-09-09",
          (work / "backlog.md").stat().st_size == 0)
    unprotect(work / "backlog.md")
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
        the_incident_against_a_protected_board(work)
        verbs_snapshot_and_keep_the_board_whole(work)
        a_verb_works_against_a_protected_board(work)
        a_refusal_leaves_the_board_protected(work)
        a_failing_write_leaves_the_original(work)
        an_unchanged_board_is_not_snapshotted_twice(work)
        retention_keeps_a_week_then_a_month(work)
        the_hourly_snapshot(work)
        a_snapshot_restores_what_was_there(work)
    finally:
        rmtree(work)

    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        for f in failed:
            print(f"  FAILED  {f}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
