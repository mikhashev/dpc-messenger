"""A command may run for its timeout; nothing said how much it may take while doing so.

On 2026-08-25 an agent wrote `step1.py`, ran it twice, and the two copies took
**84.4 and 80.9 GB** of committed memory. Free RAM reached 0.0 of 61.7 GB and
the page file 41.4 GB with a 118.6 GB peak — on the operator's working machine.

Every gate we had looked at the **verb**, and the verb was `python step1.py`:
ordinary, correctly ungated, and ruinous. The classifier fixes of the same day
closed the verb level and left this one open by design; this is the other half.

The ceiling is deliberately generous — a guardrail that stops a real build is
switched off within a week, and the failure it exists for is two orders of
magnitude above ordinary work, not two times.
"""

import time

import pytest

from dpc_client_core.dpc_agent.tools import shell as shell_tool
from dpc_client_core.dpc_agent.tools.shell import _execute_shell_command

# A grandchild that grows steadily — the shape of the incident, and the shape a
# parent-only watcher cannot see.
HUNGRY_GRANDCHILD = (
    'python -c "import subprocess, sys, time; '
    "subprocess.Popen([sys.executable, '-c', "
    "'import time\\nb=[]\\nwhile True:\\n    b.append(bytearray(20*1024*1024))\\n    time.sleep(0.2)']); "
    'time.sleep(300)"'
)


def test_a_growing_command_is_killed_at_the_ceiling(monkeypatch):
    monkeypatch.setattr(shell_tool, "_MEMORY_CEILING_MB", 200)
    started = time.time()

    result = _execute_shell_command(HUNGRY_GRANDCHILD, None, 120)

    elapsed = time.time() - started
    assert "ceiling" in result, result[:200]
    assert elapsed < 60, (
        f"took {elapsed:.0f}s — the ceiling must bite long before the timeout, "
        "which is what 9.5 hours of runaway looked like"
    )


def test_the_refusal_tells_the_agent_the_number_and_how_to_raise_it(monkeypatch):
    """A refusal the agent cannot act on gets retried verbatim."""
    monkeypatch.setattr(shell_tool, "_MEMORY_CEILING_MB", 200)

    result = _execute_shell_command(HUNGRY_GRANDCHILD, None, 120)

    assert "200 MB ceiling" in result
    assert "DPC_SHELL_MEMORY_LIMIT_MB" in result, "say which knob moves it"
    assert "killed" in result


def test_the_watcher_measures_the_whole_tree_not_just_the_shell(monkeypatch):
    """The runaway was a grandchild. A watcher reading only its own child sees
    an innocent shell while the machine fills up."""
    monkeypatch.setattr(shell_tool, "_MEMORY_CEILING_MB", 200)

    result = _execute_shell_command(HUNGRY_GRANDCHILD, None, 120)

    assert "ceiling" in result, (
        "the shell itself never grows; only its child does, and it was killed"
    )


def test_ordinary_work_is_not_touched_by_the_ceiling(monkeypatch):
    monkeypatch.setattr(shell_tool, "_MEMORY_CEILING_MB", 8192)

    assert "hello" in _execute_shell_command("echo hello", None, 10)
    assert _execute_shell_command('python -c "pass"', None, 20) == "(no output)"
    assert "exit code: 3" in _execute_shell_command(
        'python -c "import sys; sys.exit(3)"', None, 20
    )


def test_a_modest_allocation_under_the_ceiling_completes(monkeypatch):
    """The guard must not fire on work that is merely not tiny."""
    monkeypatch.setattr(shell_tool, "_MEMORY_CEILING_MB", 2048)

    result = _execute_shell_command(
        'python -c "b = bytearray(100*1024*1024); print(len(b))"', None, 60
    )

    assert "104857600" in result
    assert "ceiling" not in result


def test_the_ceiling_can_be_switched_off(monkeypatch):
    """Zero means no watcher at all — so an operator who needs the whole
    machine for one command can have it, and the refusal above is not a wall."""
    monkeypatch.setattr(shell_tool, "_MEMORY_CEILING_MB", 0)

    result = _execute_shell_command(HUNGRY_GRANDCHILD, None, 8)

    assert "ceiling" not in result
    assert "timed out" in result, "with no ceiling only the clock stops it"
