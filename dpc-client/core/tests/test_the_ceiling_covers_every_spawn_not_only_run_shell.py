"""The memory ceiling has to be a property of the service, not of one function.

On 2026-08-26 a tree of two python processes reached 50 753 MB and ran for
fourteen minutes on this machine while the ceiling in `run_shell` never fired —
because the tree never went through `run_shell`. The ceiling was correct and
irrelevant, which is the worst state a guardrail can be in: the operator
believes the machine is covered.

What is asserted here is the falsifier from that entry: an unbounded allocator
started through a path that is NOT `run_shell` is killed at the ceiling, and the
log names who started it.
"""

import logging
import pathlib
import sys

import pytest

from dpc_client_core.dpc_agent.tools import git as git_tool
from dpc_client_core.dpc_agent.tools.process import run_supervised

# 20 MB every 0.2 s: past a 200 MB ceiling in about two seconds, and small
# enough that a machine notices nothing if the kill ever regresses.
ALLOCATOR = (
    "import time\n"
    "b = []\n"
    "while True:\n"
    "    b.append(bytearray(20 * 1024 * 1024))\n"
    "    time.sleep(0.2)\n"
)


def test_an_allocator_outside_run_shell_is_killed_at_the_ceiling():
    run = run_supervised(
        [sys.executable, "-c", ALLOCATOR],
        launcher="test(direct spawn)",
        timeout=60,          # far past the kill: the ceiling must be what stops it
        ceiling_mb=200,
    )

    assert run.exceeded_mb is not None, "the ceiling never fired outside run_shell"
    assert run.exceeded_mb >= 200
    assert run.killed, "killed at the ceiling but nothing said what happened to the tree"
    assert not run.timed_out, "the clock stopped it, not the ceiling — the test proves nothing"


def test_with_the_ceiling_off_the_same_allocator_is_not_stopped():
    """Neutralise the guard: without it, nothing here kills the allocator.

    Without this arm the test above is unfalsifiable - an allocator that died
    of anything at all would pass it.
    """
    run = run_supervised(
        [sys.executable, "-c", ALLOCATOR],
        launcher="test(ceiling off)",
        timeout=5,
        ceiling_mb=0,
    )

    assert run.exceeded_mb is None, "something fired with the ceiling switched off"
    assert run.timed_out, "the allocator stopped on its own - it is not a valid wedge"
    assert run.killed, "the clock stopped it but nothing killed the tree"


def test_the_spawn_names_its_launcher(caplog):
    """A spawn no log names cannot be attributed once the process is gone."""
    with caplog.at_level(logging.INFO, logger="dpc_client_core.dpc_agent.tools.process"):
        run_supervised(
            [sys.executable, "-c", "pass"],
            launcher="test(named launcher)",
            timeout=30,
        )

    spawn_lines = [r.message for r in caplog.records if "spawned pid" in r.message]
    assert spawn_lines, "no spawn was logged at all"
    assert any("test(named launcher)" in line for line in spawn_lines), spawn_lines


def test_the_git_tool_spawns_through_the_supervisor(monkeypatch):
    """Not a source grep: the call has to actually go through run_supervised.

    `git.py` used `subprocess.run(timeout=…)`, which on Windows kills the direct
    child and then waits for EOF for ever. This asserts the replacement is on the
    live path rather than merely present in the file.
    """
    seen = {}

    def fake(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["launcher"] = kwargs.get("launcher")
        raise FileNotFoundError  # stop before spawning anything

    monkeypatch.setattr(git_tool, "run_supervised", fake)
    # A real repository: the function refuses before spawning anything if the
    # path has no .git, and a test that never reaches the spawn asserts nothing.
    repo = pathlib.Path(__file__).resolve().parents[3]
    assert (repo / ".git").exists(), f"{repo} is not a git repository"
    result = git_tool._run_git_external(str(repo), ["status"])

    assert seen.get("cmd", [None])[0] == "git"
    assert seen.get("launcher"), "the git spawn does not name a launcher"
    assert result["success"] is False
