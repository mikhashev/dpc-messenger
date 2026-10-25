"""An agent's command does not run on the service's console.

On 2026-10-22 and 2026-10-24 an agent's `run_shell` command ended in `| more`
with more than a screen of output. `more` stopped at `-- More --`, opened the
console itself to read a key with PROCESSED|LINE|ECHO input off, and was
tree-killed at the timeout before it switched them back. The console it shared
was the service's, and Ctrl+C stopped reaching the backend (backlog card
THE-SERVICE-CONSOLE-IS-LEFT-IN-RAW-MODE-SO-CTRL-C-NEVER-REACHES-THE-BACKEND).
A closed stdin did not help: `more` opens CONIN$ by name.

So `run_supervised` gives every child a hidden console of its own on Windows
(CREATE_NO_WINDOW) and a session of its own on POSIX, where a new session has
no controlling terminal and `/dev/tty` cannot be opened. The flags are tested
by injection on every platform; the console itself only on Windows.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys

import pytest

from dpc_client_core.dpc_agent.tools import process as process_mod
from dpc_client_core.dpc_agent.tools import shell
from dpc_client_core.dpc_agent.tools.process import CREATE_NO_WINDOW, run_supervised

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="the console is a Windows object")


@pytest.fixture
def popen(monkeypatch):
    """Every keyword the real Popen would have been given."""
    seen = []

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            seen.append({"cmd": cmd, **kwargs})
            self.pid = -1
            self.returncode = 0

        def communicate(self, timeout=None):
            return ("ok", "")

        def poll(self):
            return 0

    monkeypatch.setattr(process_mod.subprocess, "Popen", _FakePopen)
    return seen


def _on(monkeypatch, system):
    monkeypatch.setattr(process_mod.platform, "system", lambda: system)


# ------------------------------------------------------------ the flags

def test_on_windows_a_child_gets_a_hidden_console_of_its_own_by_default(popen, monkeypatch):
    _on(monkeypatch, "Windows")
    run_supervised(["tool"], launcher="test", timeout=5, ceiling_mb=0)
    assert popen[0]["creationflags"] == CREATE_NO_WINDOW
    assert "start_new_session" not in popen[0]


def test_a_callers_own_creationflags_are_kept_as_given(popen, monkeypatch):
    _on(monkeypatch, "Windows")
    run_supervised(["tool"], launcher="test", timeout=5, ceiling_mb=0,
                   popen_kwargs={"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
                                 if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP") else 0x200})
    assert popen[0]["creationflags"] == getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)


def test_on_posix_a_child_gets_a_session_of_its_own_and_no_windows_flag(popen, monkeypatch):
    _on(monkeypatch, "Linux")
    run_supervised(["tool"], launcher="test", timeout=5, ceiling_mb=0)
    assert popen[0]["start_new_session"] is True
    assert "creationflags" not in popen[0]


@pytest.mark.parametrize("system", ["Windows", "Linux", "Darwin"])
def test_stdin_stays_closed_on_every_platform(popen, monkeypatch, system):
    _on(monkeypatch, system)
    run_supervised(["tool"], launcher="test", timeout=5, ceiling_mb=0)
    assert popen[0]["stdin"] is subprocess.DEVNULL


def test_run_shell_git_and_djvu_all_reach_popen_with_the_hidden_console(popen, monkeypatch, tmp_path):
    """The three callers, at the far side of run_supervised."""
    _on(monkeypatch, "Windows")
    monkeypatch.setattr(shell.platform, "system", lambda: "Windows")
    from dpc_client_core.dpc_agent.tools import document, git
    from dpc_client_core.dpc_agent.tools.registry import ToolContext

    shell._execute_shell_command("echo hi", None, 10)
    git._run_git(ToolContext(agent_root=tmp_path), ["status"])
    document._run_djvu("djvused", ["book.djvu", "-e", "n"])

    assert [call["creationflags"] for call in popen] == [CREATE_NO_WINDOW] * 3


# ------------------------------------------------------------ the console itself

_CONSOLE_PIDS = (
    "import ctypes; b=(ctypes.c_uint32*64)(); "
    "n=ctypes.windll.kernel32.GetConsoleProcessList(b, 64); "
    "print(' '.join(str(b[i]) for i in range(min(n, 64))))"
)


@windows_only
def test_the_child_shares_no_console_with_the_service():
    """The child lists the processes on its console; this process is not among them."""
    import os

    run = run_supervised([sys.executable, "-c", _CONSOLE_PIDS], launcher="test", timeout=30, ceiling_mb=0)
    pids = {int(p) for p in run.stdout.split()}
    assert pids, f"the child reported no console at all: {run.stdout!r} {run.stderr!r}"
    assert os.getpid() not in pids


@windows_only
def test_without_the_default_the_same_child_would_share_this_console():
    """The control: the probe can see a shared console, so the test above can fail."""
    import os

    if not ctypes.windll.kernel32.GetConsoleWindow() and not _has_console():
        pytest.skip("this test process has no console to share")
    run = run_supervised([sys.executable, "-c", _CONSOLE_PIDS], launcher="test", timeout=30,
                         ceiling_mb=0, popen_kwargs={"creationflags": 0})
    assert os.getpid() in {int(p) for p in run.stdout.split()}


def _has_console() -> bool:
    buf = (ctypes.c_uint32 * 4)()
    return ctypes.windll.kernel32.GetConsoleProcessList(buf, 4) > 0


# ------------------------------------------------------------ chcp on its own console

@windows_only
def test_cmds_own_output_is_utf8_on_the_hidden_console():
    """cmd reads the code page at start: `chcp 65001 && echo` in one cmd printed
    `??????` on a fresh console. The second cmd starts at 65001."""
    assert shell._execute_shell_command("echo Привет", None, 20) == "Привет\n"


@windows_only
@pytest.mark.parametrize("command, expected", [
    ('echo "a|b"', '"a|b"\n'),
    ("echo a^&b", "a&b\n"),
    ("echo bang! here", "bang! here\n"),
    ("echo %OS%", "Windows_NT\n"),
    ("echo one && echo two", "one \ntwo\n"),  # cmd keeps the space before &&
    ("exit /b 3", "[exit code: 3]"),
])
def test_the_command_is_parsed_once_and_as_before(command, expected):
    """No character of the command is parsed by the outer cmd."""
    assert shell._execute_shell_command(command, None, 20) == expected
