"""A path the shell expands is still a path.

THE-SHELL-PATH-GATE-SEES-ONLY-LITERAL-ABSOLUTE-PATHS-SO-AN-ENVIRONMENT-VARIABLE-WALKS-OUT-OF-THE-SANDBOX:
GAIA run 20260923-0543, task-045, read `~/.dpc/providers.json` at Tier 0 as
`type %USERPROFILE%\\.dpc\\providers.json` while the literal spelling of the
same read was refused. The gate matched literal `C:\\…` and `/letter…` only.

Platform and environment are injected — `_on_windows` and the process
environment the child inherits — so every spelling runs on the Windows box and
on the Linux CI runner alike.

Falsifier: leave every resolved reference unexpanded inside
`_expand_for_path_check` (`emit`) and the 18 variable and tilde rows go red;
the unresolvable, traversal and Tier-0 rows must not move.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "eval"))

from dpc_client_core.dpc_agent.tools import shell  # noqa: E402
from dpc_client_core.dpc_agent.tools.shell import _validate_command  # noqa: E402

OUTSIDE = "Command accesses path outside sandbox:"
UNRESOLVED = "Path cannot be resolved without running the shell:"


class _Firewall:
    def get_tool_setting(self, *args, **kwargs):
        return []


class _Ctx:
    """A sandbox that is one directory, which is what the real one resolves to."""

    def __init__(self, sandbox):
        self.firewall = _Firewall()
        self.agent_root = str(sandbox)
        self._sandbox = Path(sandbox).resolve()

    def validate_extended_path(self, path):
        resolved = Path(str(path)).expanduser().resolve()
        if self._sandbox not in resolved.parents and resolved != self._sandbox:
            raise PermissionError(f"{path} is outside {self._sandbox}")
        return resolved


@pytest.fixture
def box(tmp_path, monkeypatch):
    """The real layout: the agent lives under the operator's home."""
    home = tmp_path / "home"
    root = home / ".dpc" / "agents" / "a"
    (root / "sub").mkdir(parents=True)
    (root / "work").mkdir()
    for name, value in {
        "HOME": home, "USERPROFILE": home, "TEMP": tmp_path / "temp",
        "APPDATA": home / "AppData" / "Roaming", "DPC_WORK": root / "work",
    }.items():
        monkeypatch.setenv(name, str(value))
    monkeypatch.delenv("DPC_NO_SUCH_VAR", raising=False)
    monkeypatch.setattr(shell, "_names_at_drive_root", lambda drive: frozenset(), raising=False)
    return root


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(shell, "_on_windows", lambda: True, raising=False)


@pytest.fixture
def posix(monkeypatch):
    monkeypatch.setattr(shell, "_on_windows", lambda: False, raising=False)


def _verdict(command, root, cwd=None):
    return _validate_command(command, _Ctx(root), str(cwd or root))


def _assert_outside(command, root, cwd=None):
    verdict = _verdict(command, root, cwd)
    assert verdict is not None and verdict[0] == "tier1", (command, verdict)
    assert OUTSIDE in verdict[1], (command, verdict[1])
    return verdict[1]


def _assert_unresolved(command, root, token):
    verdict = _verdict(command, root)
    assert verdict is not None and verdict[0] == "tier1", (command, verdict)
    assert UNRESOLVED in verdict[1] and token in verdict[1], (command, verdict[1])


# --- cmd.exe ------------------------------------------------------------------


@pytest.mark.parametrize("command", [
    r"type %USERPROFILE%\.dpc\providers.json",
    r"type %userprofile%\.dpc\providers.json",
    r"copy /Y providers_new.json %TEMP%\dpc-gaia-bksdu8dx\providers.json",
    r"type %APPDATA%\secret.txt",
    r'type "%USERPROFILE%\.dpc\providers.json"',
    r"type %USERPROFILE%\.dpc\agents\a\..\..\providers.json",
])
def test_a_cmd_variable_is_expanded_before_the_check(windows, box, command):
    reason = _assert_outside(command, box)
    assert "providers.json" in reason or "secret.txt" in reason, reason


@pytest.mark.parametrize("command", [
    r"type \Users\someone\.dpc\providers.json",      # drive-rooted, no letter
    r"type \\fileserver\share\providers.json",       # UNC
])
def test_a_rooted_or_unc_spelling_is_a_path(windows, box, monkeypatch, command):
    monkeypatch.setattr(shell, "_names_at_drive_root",
                        lambda drive: frozenset({"users", "windows"}), raising=False)
    _assert_outside(command, box)


def test_a_regex_that_starts_with_a_backslash_is_not_a_rooted_path(windows, box, monkeypatch):
    monkeypatch.setattr(shell, "_names_at_drive_root",
                        lambda drive: frozenset({"users", "windows"}), raising=False)
    assert _verdict(r'rg "\bword\b" notes.txt', box) is None


@pytest.mark.skipif(sys.platform != "win32", reason="a drive letter is a root only on Windows")
def test_a_lowercase_drive_letter_is_a_path(windows, box):
    """Branch 1 of PATH_PATTERNS is `[A-Z]:` and case-sensitive."""
    _assert_outside(r"type c:\windows\win.ini", box)


def test_a_caret_does_not_hide_a_drive_path(windows, box):
    """cmd removes `^` before running, so `C:^\\` is `C:\\`."""
    verdict = _verdict(r"type C:^\Windows^\win.ini", box)
    assert verdict is not None and OUTSIDE in verdict[1], verdict


# --- PowerShell ---------------------------------------------------------------


@pytest.mark.parametrize("command", [
    r'powershell -Command "Get-Content $env:USERPROFILE\.dpc\providers.json"',
    r'powershell -Command "Get-Content ${env:USERPROFILE}\.dpc\providers.json"',
    r"Get-Content $env:USERPROFILE\.dpc\providers.json",
])
def test_a_powershell_env_reference_is_expanded(windows, box, command):
    reason = _assert_outside(command, box)
    assert "providers.json" in reason, reason


def test_the_eval_approver_refuses_the_outside_part(posix, box):
    """An inline-code wrapper alone is approved; one that leaves the sandbox
    through a variable must carry a part the approver does not answer."""
    from _harness.auto_approve import Tier1AutoApprover

    command = 'bash -c "cat $HOME/.dpc/providers.json"'
    verdict = _verdict(command, box)
    assert verdict is not None and verdict[0] == "tier1", verdict
    approve, _why = Tier1AutoApprover().verdict({"reason": verdict[1], "command": "x"})
    assert not approve, verdict[1]


# --- POSIX sh -----------------------------------------------------------------


@pytest.mark.parametrize("command", [
    "cat $HOME/.dpc/providers.json",
    "cat ${HOME}/.dpc/providers.json",
    'cat "$HOME/.dpc/providers.json"',
    "cat ~/.dpc/providers.json",
    "ls ~",
    "cp x.json ~/",
    "cat --file=$HOME/.dpc/providers.json",
    "cat < ~/.dpc/providers.json",
])
def test_a_posix_variable_or_tilde_is_expanded(posix, box, command):
    _assert_outside(command, box)


def test_a_bare_cd_goes_home_on_posix(posix, box):
    _assert_outside("cd && cat .dpc/providers.json", box)


# --- relative traversal -------------------------------------------------------


def test_backslash_traversal_out_of_the_root(windows, box):
    _assert_outside(r"type ..\..\..\.dpc\providers.json", box)


def test_slash_traversal_out_of_the_root(posix, box):
    _assert_outside("cat ../../../.dpc/providers.json", box)


def test_a_cd_up_and_out_is_seen(posix, box):
    _assert_outside("cd ../.. && cat providers.json", box)


def test_traversal_is_resolved_from_where_the_cd_left_it(windows, box):
    """`cd sub` then three levels up leaves the root by two."""
    _assert_outside(r"cd sub && type ..\..\..\x.txt", box)


# --- what cannot be known without running the shell --------------------------


@pytest.mark.parametrize("command,token", [
    ("cat $DPC_NO_SUCH_VAR/providers.json", "$DPC_NO_SUCH_VAR"),
    ("cat ${DPC_NO_SUCH_VAR}", "DPC_NO_SUCH_VAR"),
    ("cat $(cat where.txt) x", "$(cat where.txt)"),
    ("cat `cat where.txt`", "`cat where.txt`"),
    ("cat ${HOME:-/}etc/passwd", "${HOME:-/}"),
    ("cat ~nosuchuser_dpc/x", "~nosuchuser_dpc"),
])
def test_an_unresolvable_posix_spelling_is_tier_one(posix, box, command, token):
    _assert_unresolved(command, box, token)


@pytest.mark.parametrize("command,token", [
    (r"type %DPC_NO_SUCH_VAR%\providers.json", "%DPC_NO_SUCH_VAR%"),
    (r"type %USERPROFILE:~0,3%x", "%USERPROFILE:~0,3%"),
    (r"type %~dp0providers.json", "%~dp0"),
    (r"for /f %i in (paths.txt) do type %i", "%i"),
    ('bash -c "cat $(cat where.txt)"', "$(cat where.txt)"),
    ('powershell -Command "iex (Get-Content c.txt)"', "Invoke-Expression"),
    ('powershell -Command "Get-Content $env:DPC_NO_SUCH_VAR"', "$env:DPC_NO_SUCH_VAR"),
])
def test_an_unresolvable_windows_spelling_is_tier_one(windows, box, command, token):
    _assert_unresolved(command, box, token)


# --- ordinary work stays Tier 0 ----------------------------------------------


@pytest.mark.parametrize("command", [
    r"python .\script.py",
    r"type .\notes.txt",
    "cd sub && dir",
    r"cd sub && type ..\notes.txt",
    r"type %DPC_WORK%\notes.txt",
    r'type "%DPC_WORK%\notes.txt"',
    "dir /b",
    "curl https://en.wikipedia.org/w/index.php?title=Mercury",
    "echo %USERPROFILE%",
    "echo %PATH%",
    "git log --format=%H%n -3",
    "git diff HEAD~1",
    "type $HOME",  # cmd does not expand `$`: a literal file name here
])
def test_ordinary_windows_work_is_still_tier_zero(windows, box, command):
    assert _verdict(command, box) is None, command


@pytest.mark.parametrize("command", [
    "python ./script.py",
    "cat ./notes.txt",
    "cd sub && ls",
    "cd sub && cat ../notes.txt",
    "cat $DPC_WORK/notes.txt",
    "cat ${DPC_WORK}/notes.txt",
    "ls ${DPC_WORK}",
    "cat '$HOME/not-expanded'",
    "awk '{print $NF}' notes.txt",
    "echo $HOME",
    "echo $PATH",
    "cat $PWD/notes.txt",
    "curl -s https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "git diff HEAD~1",
    "date +%Y%m%d",
])
def test_ordinary_posix_work_is_still_tier_zero(posix, box, command):
    assert _verdict(command, box) is None, command


def test_the_command_that_runs_is_not_rewritten(posix, box, monkeypatch):
    """The expansion is a view for the check; the shell still gets the text."""
    spawned = []
    monkeypatch.setattr(shell, "_execute_shell_command",
                        lambda command, cwd, timeout: spawned.append(command) or "ok")

    assert shell.run_shell(_Ctx(box), "cat $DPC_WORK/notes.txt") == "ok"
    assert spawned == ["cat $DPC_WORK/notes.txt"]
