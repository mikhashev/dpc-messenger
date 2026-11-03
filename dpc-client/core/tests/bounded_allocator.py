"""A child that grows like a runaway and cannot be one.

The tests of the memory ceiling need a process that keeps allocating until
something stops it. Written as an endless loop that process is safe only while
the kill works, and that is the thing under test: on 2026-11-04 a kill missed in
a full run and one such child reached 27.6 GiB beside the owner's live backend.

So the bound is in the program, not in the test: at most 1 GiB, at most 90 s,
then it exits, guard or no guard, orphan or not. A test must set its ceiling well
below `MOST_MB` and its timeout below `LIVES_AT_MOST_S`, so that a run with the
guard off ends in a failed assertion rather than in a child that stopped by itself.
"""

MOST_MB = 1024
LIVES_AT_MOST_S = 90
STEP_MB = 20
STEP_S = 0.2

# Below LIVES_AT_MOST_S: the clock of the test fires while the child still holds.
TIMEOUT_S = 60


def allocator(most_mb: int = MOST_MB, lives_s: float = LIVES_AT_MOST_S) -> str:
    """The program, for `python -c`: one line, no double quote, so it fits every
    route a test sends it by (a list argument, `cmd` and a git alias through `sh`)."""
    return (
        f"import time; deadline = time.monotonic() + {lives_s}; b = []; "
        f"[(b.append(bytearray({STEP_MB} * 1024 * 1024)), time.sleep({STEP_S})) "
        f"for _ in range({most_mb // STEP_MB}) if time.monotonic() < deadline]; "
        f"time.sleep(max(0, deadline - time.monotonic()))"
    )


def hungry_grandchild() -> str:
    """A run_shell command whose child is the allocator: the shape of the incident,
    and the shape a parent-only watcher cannot see. The parent's sleep has the
    same bound, so no wrapper outlives the allocator by much."""
    return (
        'python -c "import subprocess, sys, time; '
        f"subprocess.Popen([sys.executable, '-c', '{allocator()}']); "
        f'time.sleep({LIVES_AT_MOST_S})"'
    )
