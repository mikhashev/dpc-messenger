"""A timed-out command must not outlive its timeout, and must not outlive the process.

On 2026-08-25 a GAIA run finished its last task, wrote its report, and then sat
for nine and a half hours. `py-spy` on it:

    MainThread:        _python_exit → threading._shutdown → join
    dpc_agent_tool_0:  _execute_shell_command → subprocess.run
                       → communicate → _communicate → join

`subprocess.run(timeout=…)` did what it promises: it killed the process it
started, which was a shell. What the agent had written and launched was
`python step1.py` — a grandchild. It survived, holding the inherited pipe, and
on Windows `run()` then calls `communicate()` **a second time with no timeout**
to collect the reader threads. EOF never came. The tool's worker is not a
daemon thread, so the interpreter joined it at exit and never left.

Cost, measured: two grandchildren at 84.4 and 80.9 GB of committed memory, free
RAM at 0.0 of 61.7, the page file at 41.4 GB with a 118.6 GB peak, and 28 854
MiB of VRAM held by a llama-server the hung parent could no longer reap.

Two properties are pinned here, and the second is the one that mattered: the
descendants die, **and the interpreter can still exit**.
"""

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from dpc_client_core.dpc_agent.tools.shell import (
    _DRAIN_AFTER_KILL_SECONDS,
    _drain_after_kill,
    _execute_shell_command,
)

CORE = Path(__file__).resolve().parents[1]


def _is_alive(pid: int) -> bool:
    """Is this exact process still running?

    An earlier version of the descendant test counted *every* python process on
    the machine across a two-second window. Several agents live here, so any
    unrelated python born in that window failed the test — a false red, on a
    busy machine, for a reason having nothing to do with the code. Johnny
    flagged it. One pid is not a statistic and cannot drift.
    """
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"(Get-Process -Id {pid} -ErrorAction SilentlyContinue | Measure-Object).Count"],
        capture_output=True, text=True,
    ).stdout.strip()
    return out not in ("", "0")


# The grandchild inherits stdout on purpose: that is the case that hung, and a
# fix that kills the tree but still drains without a bound passes without it.
GRANDCHILD = (
    'python -c "import subprocess, sys, time; '
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)']); "
    'time.sleep(600)"'
)


def test_a_timed_out_command_returns_at_its_timeout_not_later():
    started = time.time()

    result = _execute_shell_command(GRANDCHILD, None, 5)

    elapsed = time.time() - started
    assert elapsed < 5 + _DRAIN_AFTER_KILL_SECONDS + 10, (
        f"took {elapsed:.1f}s — the old code waited for an EOF that never came"
    )
    assert "timed out" in result


def test_the_descendants_do_not_survive_the_timeout(tmp_path):
    """The grandchild names itself, so the assertion is about that process and
    not about how many pythons happen to be running on the machine."""
    pidfile = tmp_path / "grandchild.pid"
    command = (
        'python -c "import subprocess, sys, time; '
        f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)']); "
        f"open(r'{pidfile}', 'w').write(str(child.pid)); "
        'time.sleep(600)"'
    )

    _execute_shell_command(command, None, 5)
    for _ in range(20):
        if pidfile.exists():
            break
        time.sleep(0.2)
    assert pidfile.exists(), "the grandchild never started; the test proves nothing"
    grandchild_pid = int(pidfile.read_text().strip())

    for _ in range(25):
        if not _is_alive(grandchild_pid):
            break
        time.sleep(0.2)

    assert not _is_alive(grandchild_pid), (
        f"pid {grandchild_pid} outlived the command; killing the shell alone "
        "leaves the grandchild running"
    )


def test_the_answer_says_what_happened_to_the_children():
    """A timeout that silently leaves a process behind is how 165 GB went missing."""
    result = _execute_shell_command(GRANDCHILD, None, 5)

    assert "killed" in result.lower()


def test_the_interpreter_can_still_exit_after_a_timed_out_command():
    """The property that actually broke. The tool returning is not enough — the
    old code returned too, and then the process would not shut down."""
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(CORE)!r})
        from dpc_client_core.dpc_agent.tools.shell import _execute_shell_command
        _execute_shell_command({GRANDCHILD!r}, None, 5)
        print("returned")
    """)
    started = time.time()

    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=120,
    )

    elapsed = time.time() - started
    assert "returned" in proc.stdout, proc.stderr[-400:]
    assert elapsed < 60, (
        f"the interpreter took {elapsed:.0f}s to exit; before the fix it never did"
    )


def test_the_drain_is_bounded_even_when_the_kill_misses():
    """A descendant that escapes the kill must cost seconds, not the process."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        started = time.time()
        out, err = _drain_after_kill(proc)  # deliberately not killed first
        elapsed = time.time() - started

        assert elapsed < _DRAIN_AFTER_KILL_SECONDS + 5
        assert out == "" and err == "", "an abandoned drain reports nothing, not a hang"
    finally:
        proc.kill()
        proc.wait(timeout=10)


# --- and the ordinary path must be untouched --------------------------------

def test_output_and_exit_code_still_come_back():
    assert "hello" in _execute_shell_command("echo hello", None, 10)

    failed = _execute_shell_command('python -c "import sys; sys.exit(3)"', None, 20)
    assert "exit code: 3" in failed


def test_a_command_with_no_output_says_so():
    assert _execute_shell_command('python -c "pass"', None, 20) == "(no output)"
