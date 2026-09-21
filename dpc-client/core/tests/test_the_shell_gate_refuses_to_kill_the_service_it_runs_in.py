"""An agent asked to tidy up killed the service it was running inside.

2026-09-21 00:56 local. An agent looked for "lingering python", ran
`tasklist | findstr /i "python.exe"`, then
`taskkill /PID 6520 /F & …`. **Pid 6520 was the D-PC backend itself** —
`python.exe run_service.py`, the process the agent lives in. The log ends at
`spawned pid …`.

Two separate defects met:

1. Nothing in the gate knew what a kill is. `HARDLINE_PATTERNS` covers disk
   format and shutdown/reboot; killing a process by pid or by image name had
   no rule at any tier, and `os.getpid()` appeared nowhere under
   `dpc_agent/tools/`.
2. The only reason the command reached a person at all was a **false** one:
   `Command accesses path outside sandbox: /PID`. Branch 2 of `PATH_PATTERNS`
   reads a Windows switch as a POSIX path
   ([[THE-SANDBOX-PATH-RULE-READS-A-WINDOWS-SWITCH-AS-A-PATH]]). The three
   harmless commands before it had asked with the same false reason (`/i`,
   `/b`), so the operator had just clicked yes three times and answered the
   fourth in 1.5 seconds.

The two halves have to land together: repairing the parser alone deletes the
accident that saved nothing — the command would then fall through to
«allowed» with no question at all. `test_the_kill_rule_does_not_lean_on_the_path_scan`
is that dependency written down.
"""

from __future__ import annotations

import os
import sys
from collections import namedtuple
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dpc_client_core.dpc_agent.tools import process as process_tool  # noqa: E402
from dpc_client_core.dpc_agent.tools import shell  # noqa: E402
from dpc_client_core.dpc_agent.tools.shell import _validate_command, run_shell  # noqa: E402


# --- the service this test pretends to be -----------------------------------
# Never the real pids: a test that reads `os.getpid()` passes for the wrong
# reason on the machine that wrote it and cannot express «an ancestor» at all.

_Identity = namedtuple("_Identity", "own_pid ancestors names cmdline")

SERVICE = _Identity(
    own_pid=6520,                          # python.exe run_service.py — the incident's pid
    ancestors=(4242, 77),                  # the venv launcher, and what started it
    names=frozenset({"python.exe"}),
    cmdline=r"C:\dpc\.venv\Scripts\python.exe run_service.py",
)

OTHER = 47676          # somebody else's process — a question, not a refusal
UNRELATED = 4243       # and one an agent has an ordinary reason to kill
_KNOWN = {
    6520: "python.exe run_service.py",
    4242: "python.exe -m uv run",
    77: "cmd.exe",
    OTHER: "python.exe qwen21_verify.py",
    UNRELATED: "notepad.exe draft.txt",
}


@pytest.fixture
def service(monkeypatch):
    """Both providers the kill rules read, and nothing else."""
    monkeypatch.setattr(shell, "_service_identity", lambda: SERVICE, raising=False)
    monkeypatch.setattr(
        shell, "_describe_pid", lambda pid: _KNOWN.get(pid, "not found"), raising=False
    )
    return SERVICE


def tier_of(command, ctx=None, cwd=""):
    verdict = _validate_command(command, ctx, cwd)
    return verdict[0] if verdict else "tier0"


def reason_of(command, ctx=None, cwd=""):
    verdict = _validate_command(command, ctx, cwd)
    return verdict[1] if verdict else ""


def _says_it_is_us(reason: str, pid: int) -> bool:
    return "D-PC service" in reason and str(pid) in reason


# --- Part 1: the command from the incident ----------------------------------


def test_the_incident_command_is_refused_outright(service):
    verdict = _validate_command("taskkill /PID 6520 /F")

    assert verdict is not None, "the command that killed the service was allowed"
    assert verdict[0] == "tier2", "a kill of this service must not be approvable"
    assert _says_it_is_us(verdict[1], 6520), verdict[1]
    assert "python.exe run_service.py" in verdict[1], (
        "the refusal has to say what the process is, in plain words"
    )


def test_a_harmless_segment_in_front_does_not_hide_it(service):
    assert tier_of('echo checking for lingering python & taskkill /PID 6520 /F') == "tier2"


def test_a_tier1_match_earlier_in_the_line_does_not_hide_it(service):
    """The file's tier-major ordering: every segment is checked for a hard block
    before any of them is checked for a soft one."""
    verdict = _validate_command("sudo ls && taskkill /PID 6520 /F")

    assert verdict is not None and verdict[0] == "tier2", verdict
    assert _says_it_is_us(verdict[1], 6520), verdict[1]


def test_the_whole_incident_line_including_the_ampersand_chain(service):
    assert tier_of("taskkill /PID 6520 /F & echo done & dir") == "tier2"


# --- Part 1: every spelling of a kill by pid --------------------------------

_BY_PID = [
    "taskkill /PID {pid} /F",
    "taskkill /F /PID {pid}",
    "taskkill /f /t /pid {pid}",
    "taskkill /PID:{pid} /F",
    "taskkill /PID 999999 /PID {pid} /F",
    'taskkill /FI "PID eq {pid}" /F',
    "tskill {pid}",
    "kill {pid}",
    "kill -9 {pid}",
    "kill -KILL {pid}",
    "kill -s SIGKILL {pid}",
    "kill -n 9 {pid}",
    "kill 1234 {pid}",
    "Stop-Process -Id {pid}",
    "Stop-Process -Id {pid} -Force",
    "Stop-Process -Id:{pid}",
    "Stop-Process -Id 1,{pid},3",
    "Stop-Process {pid}",
    "spps -Id {pid}",
    "stop-process -id {pid} -force",
    "wmic process where processid={pid} delete",
    'wmic process where "processid={pid}" call terminate',
    "sudo kill -9 {pid}",
]


@pytest.mark.parametrize("template", _BY_PID)
def test_killing_this_services_own_pid_is_refused(service, template):
    command = template.format(pid=SERVICE.own_pid)
    verdict = _validate_command(command)

    assert verdict is not None and verdict[0] == "tier2", (command, verdict)
    assert _says_it_is_us(verdict[1], SERVICE.own_pid), (command, verdict[1])


@pytest.mark.parametrize("template", _BY_PID)
def test_killing_an_ancestor_of_this_service_is_refused(service, template):
    """On Windows the venv launcher and the real interpreter are one service."""
    command = template.format(pid=SERVICE.ancestors[0])
    verdict = _validate_command(command)

    assert verdict is not None and verdict[0] == "tier2", (command, verdict)
    assert _says_it_is_us(verdict[1], SERVICE.ancestors[0]), (command, verdict[1])


@pytest.mark.parametrize("template", _BY_PID)
def test_killing_somebody_elses_process_asks_rather_than_refuses(service, template):
    command = template.format(pid=OTHER)
    verdict = _validate_command(command)

    assert verdict is not None and verdict[0] == "tier1", (command, verdict)
    assert str(OTHER) in verdict[1] and "qwen21_verify.py" in verdict[1], (
        "the dialog must say which process is about to die"
    )
    assert "D-PC service" not in verdict[1], "it is not us; do not claim it is"


@pytest.mark.parametrize("command", [
    "taskkill /PID %PID% /F",
    "taskkill /PID %errorlevel% /F",
    "kill -9 $pid",
    "kill $(cat run.pid)",
    "kill `cat run.pid`",
    # `Stop-Process -Id $pid` left this list on 2026-09-21: in PowerShell `$PID`
    # is the host running the command, not an unset variable, so it is a
    # refusal now — test_the_powershell_host_variable_is_refused_too.
])
def test_a_pid_the_gate_cannot_read_is_a_question_not_a_pass(service, command):
    """Fail closed: an argument that is not a literal number cannot be shown safe."""
    verdict = _validate_command(command)

    assert verdict is not None and verdict[0] == "tier1", (command, verdict)
    assert "could not" in verdict[1] or "cannot" in verdict[1], verdict[1]


@pytest.mark.parametrize("command,token", [
    ("taskkill /PID %PID% /F", "%PID%"),
    ("kill $pid", "$pid"),
    ("kill $(cat run.pid)", "$(cat"),
    ("Stop-Process -Id $p", "$p"),
])
def test_the_dialog_says_which_id_it_could_not_read(service, command, token):
    """The tier is not the whole property. Dropping the unreadable token still
    leaves Tier 1 — the fallback note covers it — but the person is then told
    only that *something* could not be read, which is the incident's dialog
    again: true, and about nothing they can act on.
    """
    verdict = _validate_command(command)

    assert verdict is not None and verdict[0] == "tier1", (command, verdict)
    assert token in verdict[1], (command, verdict[1])


# --- Part 1: the same kill with a wider net, by image name ------------------


@pytest.mark.parametrize("command", [
    "taskkill /IM python.exe",
    "taskkill /IM python.exe /F",
    "taskkill /im python*",
    'taskkill /FI "IMAGENAME eq python.exe" /F',
    "taskkill /IM:python.exe",
    "pkill python",
    "pkill -9 python",
    "killall python",
    "killall -9 python",
    "Stop-Process -Name python",
    "Stop-Process -Name python -Force",
    "tskill python",
])
def test_killing_by_the_name_this_service_runs_under_is_refused(service, command):
    verdict = _validate_command(command)

    assert verdict is not None and verdict[0] == "tier2", (command, verdict)
    assert "D-PC service" in verdict[1], (command, verdict[1])


def test_a_pattern_matched_against_the_whole_command_line(service):
    """`pkill -f run_service` names no process — it names what we are running."""
    verdict = _validate_command("pkill -f run_service")

    assert verdict is not None and verdict[0] == "tier2", verdict
    assert "D-PC service" in verdict[1], verdict[1]


@pytest.mark.parametrize("command", [
    "taskkill /IM chrome.exe /F",
    "taskkill /IM pythonw.exe /F",   # a different executable, not ours
    "pkill node",
    "killall firefox",
    "Stop-Process -Name ollama",
    "pkill -f comfyui",
])
def test_killing_by_a_name_that_is_not_ours_asks_rather_than_refuses(service, command):
    verdict = _validate_command(command)

    assert verdict is not None and verdict[0] == "tier1", (command, verdict)
    assert "D-PC service" not in verdict[1], (command, verdict[1])


def test_the_name_rule_follows_the_executable_this_service_actually_runs(monkeypatch):
    """The set is read at call time, so a pythonw service protects pythonw."""
    monkeypatch.setattr(
        shell, "_service_identity",
        lambda: SERVICE._replace(names=frozenset({"pythonw.exe"})),
        raising=False,
    )
    monkeypatch.setattr(shell, "_describe_pid", lambda pid: "pythonw.exe", raising=False)

    assert tier_of("taskkill /IM pythonw.exe /F") == "tier2"
    assert tier_of("taskkill /IM python.exe /F") == "tier1"


# --- Part 1: the same on every platform -------------------------------------


@pytest.mark.parametrize("osname", ["nt", "posix"])
@pytest.mark.parametrize("command", [
    "taskkill /PID 6520 /F",
    "kill -9 6520",
    "pkill -f run_service",
    "taskkill /IM python.exe /F",
])
def test_the_protected_set_is_not_a_windows_rule(service, monkeypatch, osname, command):
    """POSIX `start_new_session` stops a tree kill from climbing to us; an
    explicit `kill <pid>` or `pkill -f run_service` reaches us regardless."""
    monkeypatch.setattr(os, "name", osname)
    monkeypatch.setattr(shell, "_on_windows", lambda: osname == "nt", raising=False)

    assert tier_of(command) == "tier2", (osname, command)


# --- Part 1: and a legitimate kill has to keep working ----------------------


@pytest.mark.parametrize("command", [
    f"taskkill /PID {UNRELATED} /F",
    f"kill {UNRELATED}",
    f"kill -9 {UNRELATED}",
])
def test_an_ordinary_kill_is_asked_and_named(service, command):
    verdict = _validate_command(command)

    assert verdict is not None and verdict[0] == "tier1", (command, verdict)
    assert verdict[1] == f"Kills process {UNRELATED}: notepad.exe draft.txt", verdict[1]


@pytest.mark.parametrize("on_windows", [True, False])
def test_an_approved_kill_runs(service, tmp_path, monkeypatch, on_windows):
    """A suite of refusals would pass while breaking the person who asked.

    The target is spelled `kill <pid>` rather than `taskkill /PID <pid> /F`
    because the switch exemption is Windows-only by design: off Windows this row
    collected a second, false «outside sandbox: /PID» reason beside the kill and
    failed, which is the incident's own dialog turning up inside its own test.
    Only `_on_windows` is patched, not `os.name`: `_Ctx` builds a `Path`, and a
    `PosixPath` cannot be instantiated on this box.
    """
    monkeypatch.setattr(shell, "_on_windows", lambda: on_windows, raising=False)
    monkeypatch.setattr(shell, "_names_at_drive_root",
                        lambda drive: frozenset(), raising=False)
    spawned = []
    monkeypatch.setattr(shell, "_execute_shell_command",
                        lambda command, cwd, timeout: spawned.append(command) or "SUCCESS")
    granted = []

    def _approve(ctx, command, reason, cwd, timeout):
        granted.append(reason)
        return shell._execute_shell_command(command, cwd or str(ctx.agent_root), timeout)

    monkeypatch.setattr(shell, "_request_approval", _approve)

    answer = run_shell(_Ctx(tmp_path), f"kill {UNRELATED}")

    assert answer == "SUCCESS", answer
    assert spawned == [f"kill {UNRELATED}"]
    assert granted == [f"Kills process {UNRELATED}: notepad.exe draft.txt"]


# --- the hard block for «every process I own» -------------------------------


@pytest.mark.parametrize("command", [
    "kill -9 -1",
    "kill -s KILL -1",
    "kill -KILL -1",
    "kill -TERM -1",
    "kill -- -1",
])
def test_signalling_every_process_the_user_owns_is_a_hard_block(service, command):
    """The rule wanted a literal `-9`, so every other spelling of the same
    signal walked past it — and each one takes the service with it."""
    assert tier_of(command) == "tier2", command


@pytest.mark.parametrize("command", [
    f"kill -1 {UNRELATED}",     # -1 is SIGHUP here, and the target is a pid
    f"kill {UNRELATED}",
])
def test_a_signal_number_is_not_a_target(service, command):
    assert tier_of(command) == "tier1", command


def test_the_price_of_leaving_that_pattern_unanchored(service):
    """Measured rather than assumed, like the `rm -rf /` case next door.

    The pattern is unanchored, as every HARDLINE pattern is, so a command that
    merely carries the word and ends in `-1` is blocked. Anchoring it at the
    verb would fix that and would simultaneously unblock
    `echo "kill -9 -1"`, which is blocked today — this is the direction the
    file has already chosen twice.
    """
    assert tier_of('git log --grep "kill" -1') == "tier2"
    assert tier_of('echo "kill -9 -1"') == "tier2"
    assert tier_of("git log -1") == "tier0"


# --- Part 1: what must not be swept up --------------------------------------


@pytest.mark.parametrize("command", [
    "echo taskkill",
    "type killer.txt",
    "cat notes-about-kill.txt",
    'git commit -m "kill the bug"',
    'git log --grep "pkill"',
    "grep -r taskkill .",
    "python make_killer.py",
    "ls",
])
def test_a_word_is_not_a_verb(service, command):
    assert tier_of(command) == "tier0", command


def test_the_services_own_tree_kill_does_not_pass_through_this_gate():
    """`process.py` kills what the runtime itself spawned, with an argument
    list and no shell — an agent may signal only what its runtime started, and
    that path never reaches `_validate_command`."""
    source = Path(process_tool.__file__).read_text(encoding="utf-8")

    assert "_validate_command" not in source
    assert "from .shell import" not in source and "import shell" not in source
    assert '["taskkill", "/PID", str(pid), "/T", "/F"]' in source, (
        "the tree kill is still a list argv, not a shell string"
    )


# --- the same kill, handed to another shell ---------------------------------
# A verb-first parser reads the wrapper and stops. Every row below was measured
# tier0 or tier1-naming-the-wrapper before this change, and a dialog that names
# `cmd /c` to somebody who has been clicking yes is the incident in a new coat.

_WRAPPED = [
    'cmd.exe /c "taskkill /PID {pid} /F"',
    "cmd /c taskkill /PID {pid} /F",
    "cmd /k taskkill /PID {pid} /F",
    'cmd /c "echo hi && taskkill /PID {pid} /F"',
    "start /b taskkill /PID {pid} /F",
    'start "a job" taskkill /PID {pid} /F',
    "call taskkill /PID {pid} /F",
    "@taskkill /PID {pid} /F",
    'powershell -Command "Stop-Process -Id {pid} -Force"',
    'pwsh -c "kill {pid}"',
    'bash -c "kill -9 {pid}"',
    "sh -c 'kill {pid}'",
    'zsh -c "pkill -f run_service"',
    "echo x | xargs kill {pid}",
]


@pytest.mark.parametrize("template", _WRAPPED)
def test_a_kill_handed_to_another_shell_is_still_a_kill(service, template):
    command = template.format(pid=SERVICE.own_pid)
    verdict = _validate_command(command)

    assert verdict is not None and verdict[0] == "tier2", (command, verdict)
    assert "D-PC service" in verdict[1], (command, verdict[1])


@pytest.mark.parametrize("template", _WRAPPED)
def test_a_wrapped_kill_of_an_ancestor_is_refused_too(service, template):
    if "run_service" in template:
        pytest.skip("that row is a name pattern, not a pid")
    command = template.format(pid=SERVICE.ancestors[0])

    assert tier_of(command) == "tier2", command


@pytest.mark.parametrize("osname", ["nt", "posix"])
@pytest.mark.parametrize("template", [
    'cmd /c "taskkill /PID {pid} /F"',
    'bash -c "kill -9 {pid}"',
])
def test_the_wrapper_rule_is_not_a_windows_rule(service, monkeypatch, osname, template):
    monkeypatch.setattr(os, "name", osname)
    monkeypatch.setattr(shell, "_on_windows", lambda: osname == "nt", raising=False)

    assert tier_of(template.format(pid=SERVICE.own_pid)) == "tier2", (osname, template)


@pytest.mark.parametrize("command,wrapper", [
    (f'cmd /c "taskkill /PID {OTHER} /F"', "cmd"),
    (f'powershell -Command "Stop-Process -Id {OTHER}"', "powershell"),
    (f'bash -c "kill -9 {OTHER}"', "bash"),
])
def test_the_kill_is_named_in_front_of_the_wrapper(service, command, wrapper):
    """The reason has to open with what dies, not with which shell was used."""
    verdict = _validate_command(command)

    assert verdict is not None and verdict[0] == "tier1", (command, verdict)
    assert verdict[1].startswith(f"Kills process {OTHER}: python.exe qwen21_verify.py"), verdict[1]
    assert "Requires approval:" in verdict[1], verdict[1]


def test_a_pid_arriving_on_stdin_cannot_be_shown_safe(service):
    """`echo <pid> | xargs kill` — the target is in another segment, so the
    kill itself names none and fails closed."""
    verdict = _validate_command(f"echo {SERVICE.own_pid} | xargs kill")

    assert verdict is not None and verdict[0] == "tier1", verdict
    assert "could not identify" in verdict[1], verdict[1]


def test_an_encoded_command_says_the_gate_is_blind(service):
    verdict = _validate_command("powershell -enc VABhAHMAawBrAGkAbABs")

    assert verdict is not None and verdict[0] == "tier1", verdict
    assert verdict[1].startswith("A kill cannot be ruled out"), verdict[1]


@pytest.mark.parametrize("command", [
    "cmd /c dir",
    "cmd.exe /c dir",
    "cmd /k dir",
    "cmd.exe /k dir",
])
def test_every_spelling_of_the_cmd_wrapper_needs_approval(service, command):
    """`\\bcmd\\s+/c\\b` missed `cmd.exe` and missed `/k`, which is the same
    wrapper with the window left open."""
    verdict = _validate_command(command)

    assert verdict is not None and verdict[0] == "tier1", (command, verdict)


@pytest.mark.parametrize("command", [
    "cmd /c echo hi",
    'bash -c "ls -la"',
    "dir",
])
def test_a_wrapper_around_ordinary_work_is_no_worse_than_before(service, command):
    """`cmd /c` and `bash -c` were always Tier 1; looking inside must not
    promote them, and `dir` must stay free."""
    verdict = _validate_command(command)
    if command == "dir":
        assert verdict is None
    else:
        assert verdict is not None and verdict[0] == "tier1", (command, verdict)
        assert "Kills" not in verdict[1], verdict[1]


# --- the last resort, and what it costs -------------------------------------


@pytest.mark.parametrize("command", [
    "for /f %i in ('echo {pid}') do taskkill /PID %i /F",
    'python -c "import os; os.kill({pid}, 9)"',
    # the idiomatic PowerShell pipeline kill: the pid and the verb sit in
    # different halves of a pipe that quoting keeps in one segment
    'powershell -Command "Get-Process -Id {pid} | Stop-Process -Force"',
])
def test_a_command_the_gate_cannot_parse_but_can_read_is_refused(service, command):
    """Spellings are endless, so the net is coarse: a kill verb and this
    service's own pid in one segment is refused without parsing either."""
    verdict = _validate_command(command.format(pid=SERVICE.own_pid))

    assert verdict is not None and verdict[0] == "tier2", verdict
    assert str(SERVICE.own_pid) in verdict[1], verdict[1]


def test_arbitrary_code_remains_the_recorded_boundary(service):
    """ADR-030 answers `python -c` with Tier 1 and does not parse it. That is
    unchanged for every string but one: a *protected pid* beside a kill word is
    caught by the last-resort check above, not by reading the Python."""
    verdict = _validate_command('python -c "print(1)"')
    assert verdict is not None and verdict[0] == "tier1"

    verdict = _validate_command(f'python -c "import os; os.kill({OTHER}, 9)"')
    assert verdict is not None and verdict[0] == "tier1", verdict


def test_the_limit_of_the_last_resort_check(service):
    """It reads one segment, so a pid parked in a variable in an earlier
    segment is out of its reach. That is the same boundary the `xargs` row
    needs — widening this to the whole line would refuse
    `echo <pid> | xargs kill`, which must stay a question. Still not silent:
    the kill itself has no literal target, so it asks."""
    verdict = _validate_command(f"set P={SERVICE.own_pid} && taskkill /PID %P% /F")

    assert verdict is not None and verdict[0] == "tier1", verdict
    assert "could not identify" in verdict[1], verdict[1]


def test_the_price_of_the_last_resort_check(service):
    """Named as a price, like the unanchored pattern next door: text that
    merely carries a kill word and the pid is refused as well."""
    assert tier_of(f"echo taskkill /PID {SERVICE.own_pid} /F") == "tier2"
    assert tier_of(f"echo the service is pkill-proof, pid {SERVICE.own_pid}") == "tier2"
    assert tier_of(f"echo pid {SERVICE.own_pid}") == "tier0"
    assert tier_of("echo taskkill") == "tier0"


# --- Part 1: it is a hard block, and configuration cannot waive it ----------


class _Firewall:
    def __init__(self, whitelist):
        self._whitelist = whitelist

    def get_tool_setting(self, *args, **kwargs):
        return self._whitelist


class _Ctx:
    """A sandbox that is one directory, which is what the real one resolves to."""

    def __init__(self, sandbox, whitelist=None):
        self.firewall = _Firewall(whitelist or [])
        self.agent_root = str(sandbox)
        self._sandbox = Path(sandbox).resolve()

    def validate_extended_path(self, path):
        resolved = Path(os.path.expanduser(str(path)))
        try:
            resolved = resolved.resolve()
        except OSError:
            pass
        if self._sandbox not in resolved.parents and resolved != self._sandbox:
            raise PermissionError(f"{path} is outside {self._sandbox}")
        return True


def test_a_whitelist_cannot_waive_a_kill_of_this_service(service, tmp_path):
    ctx = _Ctx(tmp_path, whitelist=["taskkill"])

    verdict = _validate_command("taskkill /PID 6520 /F", ctx, str(tmp_path))

    assert verdict is not None and verdict[0] == "tier2", verdict


def test_a_whitelist_still_waives_an_ordinary_kill(service, tmp_path):
    """The existing semantics, unchanged: a soft finding is whitelistable."""
    ctx = _Ctx(tmp_path, whitelist=["taskkill"])

    assert _validate_command(f"taskkill /PID {OTHER} /F", ctx, str(tmp_path)) is None


def test_the_spawn_is_never_reached_for_a_kill_of_this_service(service, tmp_path, monkeypatch):
    ran = []
    asked = []
    monkeypatch.setattr(shell, "_execute_shell_command",
                        lambda *a, **k: ran.append(a) or "RAN")
    monkeypatch.setattr(shell, "_request_approval",
                        lambda *a, **k: asked.append(a) or "ASKED")

    answer = run_shell(_Ctx(tmp_path), "taskkill /PID 6520 /F")

    assert ran == [], "the service's own kill reached the subprocess"
    assert asked == [], "a kill of this service must not even be offered for approval"
    assert answer.startswith("⛔"), answer
    assert "D-PC service" in answer, answer


# --- the dependency between the two halves ----------------------------------


def test_the_kill_rule_does_not_lean_on_the_path_scan(service, tmp_path, monkeypatch):
    """Part 3 alone would have made the incident SILENT.

    The only thing that stopped `taskkill /PID 6520 /F` in front of a person
    was the parser bug that read `/PID` as a path. With that repaired and no
    kill rule, the same command carries no path-like token at all and falls
    through to «allowed».
    """
    monkeypatch.setattr(shell, "_on_windows", lambda: True, raising=False)
    monkeypatch.setattr(shell, "_names_at_drive_root",
                        lambda drive: frozenset({"users", "windows"}), raising=False)
    ctx = _Ctx(tmp_path)

    # Nothing here is a path any more — the verdict is the kill rule's alone.
    assert "outside sandbox" not in reason_of(f"taskkill /PID {OTHER} /F", ctx, str(tmp_path))
    assert tier_of(f"taskkill /PID {OTHER} /F", ctx, str(tmp_path)) == "tier1"
    assert tier_of("taskkill /PID 6520 /F", ctx, str(tmp_path)) == "tier2"


def test_both_halves_of_the_incidents_shape_reach_the_dialog(service, tmp_path, monkeypatch):
    """Reasons accumulate. A truthful reason that hides the dangerous half is
    worse than a false one, and the kill is the half that has to be read first."""
    monkeypatch.setattr(shell, "_on_windows", lambda: True, raising=False)
    monkeypatch.setattr(shell, "_names_at_drive_root",
                        lambda drive: frozenset({"users", "windows"}), raising=False)
    ctx = _Ctx(tmp_path)

    reason = reason_of(
        rf"taskkill /PID {OTHER} /F & cd /d C:\outside\sandbox & python x.py",
        ctx, str(tmp_path),
    )

    assert reason.startswith("Kills "), reason
    assert str(OTHER) in reason and "qwen21_verify.py" in reason, reason
    assert "outside sandbox" in reason and r"C:\outside\sandbox" in reason, reason
    assert "\n" not in reason and "\r" not in reason, "the dialog renders one line"


# --- Part 3: a Windows switch is not a path ---------------------------------
# The falsifier of [[THE-SANDBOX-PATH-RULE-READS-A-WINDOWS-SWITCH-AS-A-PATH]],
# encoded: of the 52 recorded «outside sandbox» reasons, the 36 switch matches
# and the URL fragment must stop firing; /tmp, /dev/null and the C:\ paths must
# still fire. A change that also drops one of those nine has narrowed too far.


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(shell, "_on_windows", lambda: True, raising=False)
    monkeypatch.setattr(
        shell, "_names_at_drive_root",
        lambda drive: frozenset({"users", "windows", "programdata", "perflogs"}),
        raising=False,
    )


@pytest.fixture
def posix(monkeypatch):
    monkeypatch.setattr(shell, "_on_windows", lambda: False, raising=False)
    monkeypatch.setattr(shell, "_names_at_drive_root",
                        lambda drive: frozenset(), raising=False)


# The switch tokens the UI log recorded, one command each. `cd /d C:\sandbox`
# keeps a real branch-1 path on purpose: the switch must stop firing while the
# path beside it still does.
_RECORDED_SWITCHES = [
    ("cd /d C:\\sandbox", "/d"),
    ("dir /b", "/b"),
    ("sort /n numbers.txt", "/n"),
    ('tasklist /FI "IMAGENAME eq python.exe"', "/FI"),
    ("sort /N numbers.txt", "/N"),
    ("dir /s", "/s"),
    ("xcopy a b /Y", "/Y"),
    ('findstr /i "x" notes.txt', "/i"),
    ("xcopy a b /y", "/y"),
    ('findstr /R "x" notes.txt', "/R"),
    ("more /c notes.txt", "/c"),
    ("dir /od", "/od"),
    ("robocopy a b /MIR", "/MIR"),
]


@pytest.mark.parametrize("command,switch", _RECORDED_SWITCHES)
def test_a_windows_switch_no_longer_reads_as_a_path(windows, tmp_path, command, switch):
    reason = reason_of(command, _Ctx(tmp_path), str(tmp_path))

    assert f"outside sandbox: {switch}" not in reason, (command, reason)


def test_a_switch_beside_a_real_path_leaves_the_path_firing(windows, tmp_path):
    """The control for the case above: narrowing branch 2 must not reach branch 1."""
    reason = reason_of("cd /d C:\\sandbox", _Ctx(tmp_path), str(tmp_path))

    assert reason == "Command accesses path outside sandbox: C:\\sandbox", reason


@pytest.mark.parametrize("command", [
    "curl https://api.github.com/repos/anthropics/claude-code/releases",
    "curl -s https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "wget https://en.wikipedia.org/w/index.php?title=Mercury",
    "curl http://journals.le.ac.uk/index.php/jist",
])
def test_the_inside_of_a_url_is_not_a_path_on_either_platform(tmp_path, command, monkeypatch):
    """`https://host/path` leaves `/host/path`, which branch 2 read as absolute.
    About half of every eval refusal was the agent stopped from fetching a page."""
    for on_windows in (True, False):
        monkeypatch.setattr(shell, "_on_windows", lambda: on_windows, raising=False)
        monkeypatch.setattr(shell, "_names_at_drive_root",
                            lambda drive: frozenset(), raising=False)
        reason = reason_of(command, _Ctx(tmp_path), str(tmp_path))
        assert "outside sandbox" not in reason, (command, on_windows, reason)


@pytest.mark.parametrize("command,fragment", [
    ("python /tmp/j_shell_old.py", "/tmp/j_shell_old.py"),
    ("echo hi > /dev/null", "/dev/null"),
    ("type /Users/someone/secret.txt", "/Users/someone/secret.txt"),
    ("type /Windows/win.ini", "/Windows/win.ini"),
    ("dir /Users", "/Users"),                       # exists at the drive root
    ("echo x > /evil", "/evil"),                    # a redirect target, never a switch
    (r"type C:\Users\mikha\.dpc\node.key", r"C:\Users\mikha\.dpc\node.key"),
    (r"dir C:\Users\mikha\.dpc\agents", r"C:\Users\mikha\.dpc\agents"),
])
def test_a_real_path_still_fires_on_windows(windows, tmp_path, command, fragment):
    reason = reason_of(command, _Ctx(tmp_path), str(tmp_path))

    assert "outside sandbox" in reason, (command, reason)
    assert fragment in reason, (command, reason)


def test_the_msys_spelling_still_fires_because_that_half_was_not_done(windows, tmp_path):
    """Recorded honestly rather than claimed: the board's first step (b) —
    teaching `validate_extended_path` that `/c/Users/…` is `C:\\Users\\…` — is
    not in this change, so those six fires are still there."""
    reason = reason_of("cat /c/Users/mikha/Documents/dpc-messenger/README.md",
                       _Ctx(tmp_path), str(tmp_path))

    assert "outside sandbox" in reason, reason


@pytest.mark.parametrize("command,fragment", [
    ("cat /etc/passwd", "/etc/passwd"),
    ("ls /etc", "/etc"),
    ("cat /tmp", "/tmp"),
    ("ls /dev", "/dev"),
    ("ls /usr", "/usr"),
    ("ls /var", "/var"),
    ("ls /bin", "/bin"),
    ("dir /b", "/b"),
    ("sort /n numbers.txt", "/n"),
])
def test_posix_behaviour_is_exactly_what_it_was(posix, tmp_path, command, fragment):
    """The switch exemption is Windows-only. On POSIX `/b` is a path that does
    not exist, and it was Tier 1 before this change — it still is."""
    reason = reason_of(command, _Ctx(tmp_path), str(tmp_path))

    assert "outside sandbox" in reason, (command, reason)
    assert fragment in reason, (command, reason)


# Each of the three conditions on top of the board's ≤3-letter rule has to be
# the deciding one somewhere, or it is decoration that a mutation deletes for
# free. `/Users` and `/Windows` above are five letters and nine — the length
# rule already keeps them, so they prove nothing about the guards.


@pytest.fixture
def short_names_at_root(monkeypatch):
    """A drive whose root really does hold one-to-three-letter entries."""
    monkeypatch.setattr(shell, "_on_windows", lambda: True, raising=False)
    monkeypatch.setattr(shell, "_names_at_drive_root",
                        lambda drive: frozenset({"tmp", "dev", "c", "bin"}), raising=False)


@pytest.mark.parametrize("command,fragment", [
    ("type /tmp", "/tmp"),
    ("dir /dev", "/dev"),
    ("cd /c", "/c"),
    ("more /bin", "/bin"),
])
def test_a_short_name_that_exists_at_the_drive_root_is_still_a_path(
    short_names_at_root, tmp_path, command, fragment
):
    """The condition the length rule cannot cover: `C:\\tmp` and `C:\\c` exist on
    real machines — measured on this one, whose root holds `c`, `dev` and `tmp`.
    """
    reason = reason_of(command, _Ctx(tmp_path), str(tmp_path))

    assert "outside sandbox" in reason, (command, reason)
    assert fragment in reason, (command, reason)


def test_a_short_name_absent_from_the_root_is_the_switch_it_looks_like(
    short_names_at_root, tmp_path
):
    """The control: `/b` is not at that root, so it is still a switch."""
    assert reason_of("dir /b", _Ctx(tmp_path), str(tmp_path)) == ""


@pytest.mark.parametrize("command,fragment", [
    ("dir /b", "/b"),
    ("type /tmp", "/tmp"),
    ("findstr /i x f", "/i"),
])
def test_an_unreadable_drive_root_keeps_the_old_verdict(tmp_path, monkeypatch, command, fragment):
    """Unreadable is not «nothing is there». With no answer from the disk the
    gate must not start exempting tokens it cannot check."""
    monkeypatch.setattr(shell, "_on_windows", lambda: True, raising=False)
    monkeypatch.setattr(shell, "_names_at_drive_root", lambda drive: None, raising=False)

    reason = reason_of(command, _Ctx(tmp_path), str(tmp_path))

    assert "outside sandbox" in reason, (command, reason)
    assert fragment in reason, (command, reason)


@pytest.mark.parametrize("command,fragment", [
    ("echo x > /ev", "/ev"),
    ("echo x >> /o", "/o"),
    ("type nul > /a", "/a"),
    ("sort < /in", "/in"),
    ("echo x 2> /er", "/er"),
])
def test_a_redirect_target_is_never_a_switch(short_names_at_root, tmp_path, command, fragment):
    """`> /evil` is four letters, so the length rule hid this one: a file being
    written to is a path whatever its name is short enough to look like."""
    reason = reason_of(command, _Ctx(tmp_path), str(tmp_path))

    assert "outside sandbox" in reason, (command, reason)
    assert fragment in reason, (command, reason)


def test_a_switch_before_a_redirect_is_still_a_switch(short_names_at_root, tmp_path):
    """The control: only the target of the redirect is affected."""
    assert reason_of("dir /b > out.txt", _Ctx(tmp_path), str(tmp_path)) == ""


@pytest.mark.parametrize("command,fragment", [
    ("sfc /scannow", "/scannow"),
    ("msiexec /quiet", "/quiet"),
    ("cl /nologo", "/nologo"),
    ("robocopy a b /MIRR", "/MIRR"),
])
def test_a_switch_longer_than_three_letters_still_asks(short_names_at_root, tmp_path,
                                                      command, fragment):
    """A recorded cost of the board's rule, not an oversight.

    The narrowing agreed on the board is «a one-to-three-letter token»: it was
    measured against the 36 switches the UI log actually recorded, every one of
    which fits. Longer switches — `/scannow`, `/quiet`, `/nologo`, and anything
    with a `:value` — still reach the operator with a reason that calls them a
    path. Widening the rule is a separate decision with its own falsifier; this
    pins where the boundary currently is so it cannot drift by accident.
    """
    reason = reason_of(command, _Ctx(tmp_path), str(tmp_path))

    assert "outside sandbox" in reason, (command, reason)
    assert fragment in reason, (command, reason)


def test_the_three_letter_boundary_itself(short_names_at_root, tmp_path):
    """The pair that fixes the length: `/MIR` is exempt, `/MIRR` is not."""
    assert reason_of("robocopy a b /MIR", _Ctx(tmp_path), str(tmp_path)) == ""
    assert "outside sandbox" in reason_of("robocopy a b /MIRR", _Ctx(tmp_path), str(tmp_path))


def test_ordinary_work_inside_the_sandbox_stays_ungated(windows, tmp_path):
    inside = tmp_path / "work"
    inside.mkdir()

    assert _validate_command(f"cd /d {inside}", _Ctx(tmp_path), str(tmp_path)) is None
    assert _validate_command("dir /b", _Ctx(tmp_path), str(tmp_path)) is None


# --- the providers themselves, on this machine ------------------------------


def test_only_our_own_kind_of_ancestor_lends_its_name(monkeypatch):
    """A developer's box has Explorer, a terminal and an editor above the
    service. Their PIDs are protected; their NAMES are not — `taskkill /IM
    explorer.exe` is a question, not a hard block. What does lend its name is
    the launcher/interpreter pair, which is the same executable twice.

    The service this row reasons about is stated rather than inherited from
    whichever interpreter runs the suite: on a Linux node `sys.executable` is
    `python3`, so the stem set was `{python3}` and the `python.exe` ancestor
    below lent nothing (CI 35536703944).
    """
    monkeypatch.setattr(sys, "executable", r"C:\dpc\.venv\Scripts\python.exe")
    monkeypatch.setattr(shell, "_own_process_name", lambda: "python.exe", raising=False)
    monkeypatch.setattr(shell, "_ancestor_processes", lambda pid: [
        (4242, "python.exe"), (77, "explorer.exe"), (78, "bash.exe"), (79, "uv.exe"),
    ], raising=False)
    monkeypatch.setattr(shell, "_SERVICE_IDENTITY", None, raising=False)

    identity = shell._service_identity()

    assert "python.exe" in identity.names
    assert "explorer.exe" not in identity.names
    assert "bash.exe" not in identity.names
    assert "uv.exe" not in identity.names
    assert identity.ancestors == (4242, 77, 78, 79), "their pids stay protected"

    monkeypatch.setattr(shell, "_SERVICE_IDENTITY", None, raising=False)


def test_the_same_rule_on_a_posix_node(monkeypatch):
    """The twin of the row above, in the shape the other node runs: `python3`
    launched by `python3`, under `uv` under a shell. The launcher lends its name;
    `uv` and `bash` lend only their pids, exactly as on Windows."""
    monkeypatch.setattr(sys, "executable", "/home/mike/dpc/.venv/bin/python3")
    monkeypatch.setattr(shell, "_own_process_name", lambda: "python3", raising=False)
    monkeypatch.setattr(shell, "_ancestor_processes", lambda pid: [
        (4242, "python3"), (77, "uv"), (78, "bash"),
    ], raising=False)
    monkeypatch.setattr(shell, "_SERVICE_IDENTITY", None, raising=False)

    identity = shell._service_identity()

    assert identity.names == frozenset({"python3"}), identity.names
    assert identity.ancestors == (4242, 77, 78), "their pids stay protected"

    monkeypatch.setattr(shell, "_SERVICE_IDENTITY", None, raising=False)


@pytest.mark.parametrize("command", [
    "taskkill /IM explorer.exe /F",
    "taskkill /IM bash.exe /F",
    "pkill -9 code",
])
def test_an_ancestors_name_is_a_question_not_a_refusal(service, command):
    verdict = _validate_command(command)

    assert verdict is not None and verdict[0] == "tier1", (command, verdict)
    assert "running)" in verdict[1], (command, verdict[1])


def test_the_service_identity_names_this_process():
    """The one place the real provider is exercised: everything else patches it."""
    identity = shell._service_identity()

    assert identity.own_pid == os.getpid()
    assert os.path.basename(sys.executable).lower() in {n.lower() for n in identity.names}
    assert isinstance(identity.ancestors, tuple)
    assert os.getpid() not in identity.ancestors, "a process is not its own ancestor"


def test_a_pid_can_be_described_and_a_missing_one_says_so():
    described = shell._describe_pid(os.getpid())
    assert "python" in described.lower(), described
    assert "\n" not in described and len(described) <= 200

    absent = 2 ** 22 - 3
    try:
        import psutil
        missing = not psutil.pid_exists(absent)
    except Exception:
        missing = True
    if missing:
        assert shell._describe_pid(absent) in ("not found", "unreadable")


def test_this_process_cannot_be_asked_to_kill_itself():
    """No patching at all — the real provider, the real pid."""
    verdict = _validate_command(f"taskkill /PID {os.getpid()} /F")

    assert verdict is not None and verdict[0] == "tier2", verdict
    assert str(os.getpid()) in verdict[1], verdict[1]


# ===========================================================================
# 2026-09-21, the follow-up. Three classes the first round left open, each one
# measured on the committed gate before it was written down here.
# ===========================================================================


# --- F3: a wrapper is a wrapper with switches in front of its `-c` ----------
# `\b(bash|sh|zsh|fish)\s+-c\b` and `\bcmd(?:\.exe)?\s+/[ck]\b` want the command
# switch to follow the name immediately. Every spelling below was measured
# tier0 — SILENT — with a kill of this service inside it.

_SWITCHY_WRAPPERS = [
    'cmd /s /c "{cmd}"',
    'cmd /d /s /c "{cmd}"',
    'cmd.exe /v:on /c "{cmd}"',
    'bash -lc "{cmd}"',
    'bash -l -c "{cmd}"',
    'bash --norc -c "{cmd}"',
    'bash --login -c "{cmd}"',
    "sh -ec '{cmd}'",
    'zsh -ic "{cmd}"',
    'bash -xec "{cmd}"',
]


@pytest.mark.parametrize("wrapper", _SWITCHY_WRAPPERS)
def test_a_switch_between_the_shell_and_its_command_does_not_hide_a_kill(service, wrapper):
    command = wrapper.format(cmd="taskkill /IM python.exe /F")
    verdict = _validate_command(command)

    assert verdict is not None and verdict[0] == "tier2", (command, verdict)
    assert "D-PC service" in verdict[1], (command, verdict[1])


@pytest.mark.parametrize("wrapper", _SWITCHY_WRAPPERS)
def test_a_switchy_wrapper_around_ordinary_work_is_its_plain_form(service, wrapper):
    """`bash -lc "ls"` must be no worse than `bash -c "ls"` — and no better.
    The plain form has been Tier 1 since ADR-030; recognising one more spelling
    of the same wrapper must not promote it past that."""
    verdict = _validate_command(wrapper.format(cmd="ls -la"))

    assert verdict is not None and verdict[0] == "tier1", (wrapper, verdict)
    assert "Kills" not in verdict[1], verdict[1]


@pytest.mark.parametrize("command", [
    'bash -o pipefail -c "pkill python"',   # a switch with a value: `-c` is never reached
    "bash -s kill 12345",                   # the program arrives on stdin
    "sh -s -- taskkill /IM python.exe",
    "powershell -File tidy.ps1 taskkill",
    # the eighth verb, which this note could not see until `_carries_a_kill_word`
    # became its one reader — measured tier0 on fdf7514a.
    "bash -s wmic process where ProcessId=12345 delete",
])
def test_a_wrapper_the_gate_cannot_open_says_so_rather_than_passing(service, command):
    """The fail-closed half of F3. A known shell, a kill word, and no command
    string the gate could extract: it cannot be shown safe, so it is asked — and
    the sentence says the gate could not read inside the wrapper rather than
    inventing a target."""
    verdict = _validate_command(command)

    assert verdict is not None and verdict[0] == "tier1", (command, verdict)
    assert verdict[1].startswith("A kill cannot be ruled out"), verdict[1]
    assert "could not read" in verdict[1], verdict[1]


@pytest.mark.parametrize("command", [
    "bash -s ls",                 # a wrapper, no kill word: unchanged
    "bash script.sh",
    "cat kill.sh",                # a kill word and a `.sh`, but the verb is `cat`
    'git commit -m "fix bash kill handling"',
])
def test_the_fail_closed_wrapper_rule_needs_both_halves(service, command):
    """Not every unparsed wrapper is a question: it takes a shell in verb
    position *and* a kill word in the same segment."""
    assert tier_of(command) == "tier0", command


# --- F2: `$PPID` and `$$` are not unreadable ids — they are this service ----
# In the shell this gate spawns, `$PPID` is the process that started it, which
# is the D-PC service, and `$$` is the shell whose death takes the command with
# it. Both were measured tier1 "could not identify", one click from the
# incident.

_SELF_VARIABLES = ["$PPID", "${PPID}", "$$", "${$}"]


@pytest.mark.parametrize("token", _SELF_VARIABLES)
@pytest.mark.parametrize("template", [
    "kill {t}",
    "kill -9 {t}",
    "kill -s KILL {t}",
    "kill -- {t}",
    'bash -lc "kill {t}"',
    'sh -c "kill -9 {t}"',
    "taskkill /PID {t} /F",
])
def test_the_variable_that_names_this_service_is_refused(service, token, template):
    command = template.format(t=token)
    verdict = _validate_command(command)

    assert verdict is not None and verdict[0] == "tier2", (command, verdict)
    assert "D-PC service" in verdict[1], (command, verdict[1])
    assert str(SERVICE.own_pid) in verdict[1], (command, verdict[1])


def test_the_refusal_says_which_process_the_variable_is(service):
    """Naming the token is not enough: the person has to be told what it means."""
    ppid = reason_of("kill $PPID")
    assert "$PPID" in ppid and "started this shell" in ppid, ppid

    itself = reason_of("kill $$")
    assert "$$" in itself and "this shell itself" in itself, itself


@pytest.mark.parametrize("command", [
    "Stop-Process -Id $PID",
    "Stop-Process -Id $pid -Force",
    "Stop-Process -Id ${PID}",
    'powershell -Command "Stop-Process -Id $PID"',
    'powershell -NoProfile -Command "kill $PID"',
    'pwsh -c "Stop-Process -Id $pid"',
])
def test_the_powershell_host_variable_is_refused_too(service, command):
    """`$PID` is the PowerShell host. For `powershell -Command` that host is the
    child this gate spawned, so killing it aborts the command before it can
    report anything back; when the gate's own shell is the host, it is this
    service. Refused either way, and the sentence says which."""
    verdict = _validate_command(command)

    assert verdict is not None and verdict[0] == "tier2", (command, verdict)
    assert "PowerShell host" in verdict[1], (command, verdict[1])
    assert "D-PC service" in verdict[1], (command, verdict[1])


@pytest.mark.parametrize("command,token", [
    # POSIX has no `$pid`: it is an ordinary variable, usually unset. The
    # PowerShell reading above is decided by the shell the piece belongs to.
    ("kill -9 $pid", "$pid"),
    ('bash -lc "kill $pid"', "$pid"),
    ("kill $(cat run.pid)", "$(cat"),
    ("kill `cat run.pid`", "`cat"),
    ("taskkill /PID %PID% /F", "%PID%"),
    # cmd.exe has no built-in `%PPID%`, so it stays an unresolved id.
    ("taskkill /PID %PPID% /F", "%PPID%"),
    ("Stop-Process -Id $p", "$p"),
])
def test_every_other_non_literal_target_stays_a_named_question(service, command, token):
    verdict = _validate_command(command)

    assert verdict is not None and verdict[0] == "tier1", (command, verdict)
    assert token in verdict[1], (command, verdict[1])


# --- F1: pkill reads an extended regular expression, not a substring -------


@pytest.mark.parametrize("command", [
    "pkill pytho",              # a prefix reaches the name: pkill matches anywhere
    "pkill ^pyth",
    "pkill pyth.n",
    "pkill 'python|node'",
    "pkill -f run.serv",        # the dot the substring test could never match
    "pkill -f 'run_servic.'",
    "pkill -f '^C:.dpc'",
    "pkill -x python.exe",      # -x is exact, and this one is exactly us
    "pkill -x python",          # the stem is the same name without its `.exe`
    "pkill -x pytho.",          # an anchored regex that fits the stem and not `python.exe`
])
def test_a_pkill_pattern_reaches_this_service_the_way_pkill_would(service, command):
    verdict = _validate_command(command)

    assert verdict is not None and verdict[0] == "tier2", (command, verdict)
    assert "D-PC service" in verdict[1], (command, verdict[1])


@pytest.mark.parametrize("command", [
    "pkill -x pytho",           # anchored: a prefix no longer reaches us
    "pkill ^ython",
    "pkill notepad",
    "killall pytho",            # killall matches a whole name, not a pattern
    "killall -x pytho",
])
def test_the_regex_reading_does_not_widen_past_what_the_verb_does(service, command):
    verdict = _validate_command(command)

    assert verdict is not None and verdict[0] == "tier1", (command, verdict)
    assert "D-PC service" not in verdict[1], (command, verdict[1])


def test_killall_reads_a_regex_only_when_it_is_asked_to(service):
    assert tier_of("killall pytho") == "tier1"
    assert tier_of("killall -r pytho") == "tier2"
    assert tier_of("killall python.exe") == "tier2"


@pytest.mark.parametrize("command,tier", [
    ("taskkill /IM pytho", "tier1"),        # /IM takes a wildcard, never a regex
    ("taskkill /IM pyth.n /F", "tier1"),
    ("taskkill /IM python* /F", "tier2"),
    ("taskkill /IM python.exe /F", "tier2"),
])
def test_taskkill_by_image_name_stays_a_wildcard(service, command, tier):
    assert tier_of(command) == tier, command


def test_a_pattern_the_gate_cannot_compile_is_refused_not_ignored(service):
    """Fail closed: a pattern that does not compile cannot be shown not to match
    us, and pkill's own reader may well accept what `re` rejects."""
    verdict = _validate_command("pkill '['")

    assert verdict is not None and verdict[0] == "tier2", verdict
    assert "D-PC service" in verdict[1], verdict[1]
    assert "regular expression" in verdict[1], verdict[1]


def test_a_pattern_too_long_to_read_is_refused_as_well(service):
    """The guard against a catastrophic pattern is a length cap, not a timeout:
    over the cap the gate stops reading and refuses."""
    long_pattern = "a|" * 100 + "z"          # 201 characters

    verdict = _validate_command("pkill '%s'" % long_pattern)

    assert verdict is not None and verdict[0] == "tier2", verdict
    assert "D-PC service" in verdict[1], verdict[1]
    assert "200" in verdict[1], verdict[1]


class _Proc:
    def __init__(self, name):
        self.info = {"name": name}


@pytest.fixture
def running(monkeypatch):
    """The machine's process list, replaced by one this test can state."""
    psutil = pytest.importorskip("psutil")

    def install(*names):
        monkeypatch.setattr(psutil, "process_iter",
                            lambda attrs=None: [_Proc(n) for n in names])
    return install


def test_the_count_in_the_dialog_uses_the_verbs_own_matching(service, monkeypatch, running):
    """«Kills every process named pytho (0 running)» was a false statement to the
    person: pkill would have killed two. A wrong count in the dialog is the same
    defect class as the incident's wrong reason."""
    monkeypatch.setattr(
        shell, "_service_identity",
        lambda: SERVICE._replace(names=frozenset({"nodejs.exe"}),
                                 cmdline="nodejs.exe server.js"),
        raising=False,
    )
    running("python.exe", "python.exe", "notepad.exe")

    assert "(2 running)" in reason_of("pkill pytho"), reason_of("pkill pytho")
    assert "(0 running)" in reason_of("taskkill /IM pytho"), reason_of("taskkill /IM pytho")
    assert "(2 running)" in reason_of("killall python"), reason_of("killall python")


def test_on_posix_the_protected_name_carries_its_version(monkeypatch):
    """The consequence the reviewer raised, pinned. On Linux the name is
    `python3.12`, so `pkill python3` and `pkill python` both reach the service —
    and `killall python3`, which matches the whole name, does not."""
    monkeypatch.setattr(
        shell, "_service_identity",
        lambda: SERVICE._replace(names=frozenset({"python3.12"}),
                                 cmdline="/usr/bin/python3.12 run_service.py"),
        raising=False,
    )
    monkeypatch.setattr(shell, "_describe_pid",
                        lambda pid: "python3.12 run_service.py", raising=False)

    assert tier_of("pkill python3") == "tier2"
    assert tier_of("pkill python") == "tier2"
    assert tier_of("pkill -f run_service") == "tier2"
    assert tier_of("killall python3") == "tier1"
    assert tier_of("pkill node") == "tier1"


# --- F1 again: the pattern reader was never reached by a pattern -------------
# `_UNREADABLE_PID` was written for a *pid* token that is a shell expansion —
# `$p`, `%i`, `$(cat f)` — and `_pattern_kill_targets` applied it to an argument
# that for `pkill` and `killall -r` is a regular expression by definition. So
# every regex carrying `(`, `|`, `^` or `$` was filed as an id nobody could read
# and asked with that sentence, while pkill would have killed this service.
# Every row below was measured tier1 "could not identify" on dd1a5661.

_A_REGEX_THAT_REACHES_US = [
    "pkill '(py|no)thon'",
    "pkill '^python$'",
    'pkill "^python$"',
    "pkill -f 'run_(service|x)'",
    "killall -r '(py|no)thon'",
    "pkill -x '(python|node)'",
    r"pkill -f '^.*run_service\.py$'",
    'bash -c "pkill \'(py|no)thon\'"',
    "for p in 1; do pkill '^python$'; done",
]


@pytest.mark.parametrize("command", _A_REGEX_THAT_REACHES_US)
def test_a_regex_the_verb_would_match_us_with_is_refused(service, command):
    verdict = _validate_command(command)

    assert verdict is not None and verdict[0] == "tier2", (command, verdict)
    assert "D-PC service" in verdict[1], (command, verdict[1])


@pytest.mark.parametrize("command,pattern", [
    ("pkill '^sleep$'", "^sleep$"),
    ("pkill '(foo|bar)baz'", "(foo|bar)baz"),
    ("killall -r '^note(pad)?$'", "^note(pad)?$"),
])
def test_a_regex_that_misses_us_asks_with_the_pattern_reason(service, command, pattern):
    """The tier was not the only thing wrong: a person told «the id is
    (foo|bar)baz» has been handed the gate's confusion instead of the fact that a
    net is about to be cast, and the count that says how wide it is went missing
    with it."""
    verdict = _validate_command(command)

    assert verdict is not None and verdict[0] == "tier1", (command, verdict)
    assert verdict[1].startswith(f"Kills every process matching {pattern}"), verdict[1]
    assert "could not identify" not in verdict[1], verdict[1]


@pytest.mark.parametrize("command,token", [
    ("pkill $p", "$p"),
    ('pkill "$NAME"', "$NAME"),
    ("pkill $(cat name.txt)", "$(cat"),
    ("pkill ${p}", "${p"),              # `_strip_grouping` has eaten the `}`
    ("pkill $_", "$_"),
    ("pkill %NAME%", "%NAME%"),         # cmd's spelling of the same unknown
    ("pkill %i", "%i"),
])
def test_a_pattern_that_is_a_shell_expansion_is_still_an_unreadable_id(service, command, token):
    """The window this change must not close. A value the gate cannot know is not
    a pattern it can test, so the id sentence stays — and stays alone: a second
    note calling the same token a pattern would be the gate guessing out loud.
    """
    verdict = _validate_command(command)

    assert verdict is not None and verdict[0] == "tier1", (command, verdict)
    assert verdict[1] == (
        f"Kills a process the gate could not identify: the id is {token}"
    ), verdict[1]


@pytest.mark.parametrize("command", [
    "pkill '$x|python'",
    "pkill 'run_$name|python'",
    "pkill '%i|python'",
])
def test_a_token_that_is_both_an_expansion_and_a_pattern_still_refuses(service, command):
    """Where the two readings disagree, the refusal wins. `_tokens` has thrown the
    quotes away, and in single quotes nothing expands on POSIX — so a token the
    gate reads as a value is tested as a pattern as well, whenever it is one.
    `pkill '$x|sleep'`, the same shape reaching nothing of ours, stays the
    question below."""
    verdict = _validate_command(command)

    assert verdict is not None and verdict[0] == "tier2", (command, verdict)
    assert "D-PC service" in verdict[1], (command, verdict[1])


def test_the_same_shape_that_reaches_nothing_of_ours_is_the_id_question(service):
    assert reason_of("pkill '$x|sleep'") == (
        "Kills a process the gate could not identify: the id is $x|sleep"
    )


def test_a_cmd_variable_in_a_loop_body_keeps_the_bodys_own_sentence(service):
    """Measured, against the expectation that it would say «could not identify»:
    `_kill_verb` gives up on the `for` header, so the parser never sees `pkill`
    here at all and the body rule is what speaks. `%i` is an expansion either
    way, so no pattern is invented for it."""
    verdict = _validate_command("for /f %i in (n.txt) do pkill %i")

    assert verdict is not None and verdict[0] == "tier1", verdict
    assert _SAYS_A_BODY in verdict[1], verdict[1]
    assert "matching %i" not in verdict[1], verdict[1]


@pytest.mark.parametrize("command", [
    "pgrep '(py|no)thon'",
    "echo '^python$'",
])
def test_a_regex_outside_a_kill_verb_is_still_nothing(service, command):
    assert tier_of(command) == "tier0", command


def test_a_posix_bracket_class_is_translated_rather_than_silently_missed(service):
    """`re` has no `[[:alpha:]]`: it read a nested set, matched nothing and said
    "(0 running)" for a pattern pkill would have matched — with a FutureWarning
    beside it. The eight common classes are rewritten before compiling."""
    verdict = _validate_command("pkill '[[:alpha:]]ython'")

    assert verdict is not None and verdict[0] == "tier2", verdict
    assert "D-PC service" in verdict[1], verdict[1]


def test_a_posix_class_this_gate_has_no_translation_for_fails_closed(service):
    verdict = _validate_command("pkill '[[:nope:]]ython'")

    assert verdict is not None and verdict[0] == "tier2", verdict
    assert "[:nope:]" in verdict[1], verdict[1]


def test_reading_a_pattern_warns_about_nothing(service, recwarn):
    """A warning on stderr from inside the gate is noise in a log somebody reads
    to find out why a command was refused."""
    assert tier_of("pkill '[[:alpha:]]ython'") == "tier2"
    assert tier_of("pkill '[[:digit:]]'") == "tier1"
    # No POSIX class to translate away, and `re` still calls it a nested set.
    assert tier_of("pkill '[[ab]]ython'") == "tier1"

    assert [str(w.message) for w in recwarn.list if w.category is FutureWarning] == []


@pytest.fixture
def posix_service(monkeypatch):
    """The same service on Linux, where the executable carries no `.exe`."""
    identity = SERVICE._replace(
        names=frozenset({"python3"}),
        cmdline="/home/mike/dpc/.venv/bin/python3 run_service.py",
    )
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(shell, "_on_windows", lambda: False, raising=False)
    monkeypatch.setattr(shell, "_service_identity", lambda: identity, raising=False)
    monkeypatch.setattr(shell, "_describe_pid",
                        lambda pid: "python3 run_service.py", raising=False)
    return identity


@pytest.mark.parametrize("command", [
    "pkill '(py|no)thon3'",
    "pkill '^python3$'",
    "killall -r '(py|no)thon3'",
    "pkill -f 'run_(service|x)'",
])
def test_the_same_regexes_reach_a_posix_service(posix_service, command):
    """The shape this box cannot be: `python3`, no `.exe`, and the pattern the
    other node's CI would have had to survive."""
    verdict = _validate_command(command)

    assert verdict is not None and verdict[0] == "tier2", (command, verdict)
    assert "D-PC service" in verdict[1], (command, verdict[1])


def test_a_posix_service_still_asks_about_a_regex_that_misses_it(posix_service):
    assert tier_of("pkill '^sleep$'") == "tier1"
    assert "could not identify" not in reason_of("pkill '^sleep$'")


# --- a generator, not a list ------------------------------------------------
# The fourteen wrapped spellings in `_WRAPPED` are the ones somebody thought
# of. These tables are multiplied instead, so a spelling nobody typed is
# covered too; the cost is that the tables, not the rows, are what a reader has
# to keep honest.

_WRAPPERS = [
    "{cmd}",                                 # no wrapper at all
    'cmd /c "{cmd}"',
    'cmd.exe /c "{cmd}"',
    'cmd /s /c "{cmd}"',
    'cmd /d /s /c "{cmd}"',
    'cmd /k "{cmd}"',
    'bash -c "{cmd}"',
    'bash -lc "{cmd}"',
    'bash -l -c "{cmd}"',
    'bash --norc -c "{cmd}"',
    "sh -ec '{cmd}'",
    'zsh -ic "{cmd}"',
    'powershell -Command "{cmd}"',
    'pwsh -NoProfile -Command "{cmd}"',
    'sudo bash -lc "{cmd}"',
]

_BY_PID_VERBS = [
    "taskkill /PID {t} /F",
    "kill -9 {t}",
    "Stop-Process -Id {t}",
    "tskill {t}",
]
_BY_NAME_VERBS = [
    "taskkill /IM {t} /F",
    "pkill {t}",
    "killall {t}",
    "Stop-Process -Name {t}",
]

# Every way this suite can name the service, against the verb that can take it.
_NAMES_US = (
    [(verb, str(SERVICE.own_pid)) for verb in _BY_PID_VERBS]
    + [(verb, str(SERVICE.ancestors[0])) for verb in _BY_PID_VERBS]
    + [(verb, "$PPID") for verb in _BY_PID_VERBS]
    + [(verb, "python.exe") for verb in _BY_NAME_VERBS]
)


@pytest.mark.parametrize("wrapper", _WRAPPERS)
@pytest.mark.parametrize("verb,target", _NAMES_US)
def test_every_wrapper_around_every_way_of_naming_us(service, wrapper, verb, target):
    command = wrapper.format(cmd=verb.format(t=target))
    verdict = _validate_command(command)

    assert verdict is not None and verdict[0] == "tier2", (command, verdict)
    assert "D-PC service" in verdict[1], (command, verdict[1])


@pytest.mark.parametrize("wrapper", _WRAPPERS)
@pytest.mark.parametrize("verb", _BY_PID_VERBS)
def test_the_same_wrappers_around_somebody_elses_pid_ask_and_name_the_victim(
    service, wrapper, verb
):
    """The control matrix: the wrappers must not turn an ordinary kill into a
    refusal, and what dies still has to be the head of the sentence."""
    command = wrapper.format(cmd=verb.format(t=UNRELATED))
    verdict = _validate_command(command)

    assert verdict is not None and verdict[0] == "tier1", (command, verdict)
    assert verdict[1].startswith("Kills process %d: notepad.exe draft.txt" % UNRELATED), (
        command, verdict[1],
    )


@pytest.mark.parametrize("command,fragment", [
    ("set P=%d && taskkill /PID %%P%% /F" % SERVICE.own_pid, "%P%"),
    ('bash -o pipefail -c "pkill python"', "could not read"),
    ("taskkill /PID $(cat run.pid) /F", "$(cat"),
])
def test_what_the_matrix_does_not_reach(service, command, fragment):
    """Three boundaries of the generator above, written down rather than implied.

    It multiplies *spellings*; it cannot reach a target the gate has no way to
    evaluate. Each row is Tier 1 — a question with the reason named — and none
    is Tier 2: a target computed at run time, a target inside a wrapper whose
    command string the gate could not extract, and a target set in an earlier
    segment of the same line. A pkill *pattern* used to fall in the first of
    those and no longer does — `pkill '(py|no)thon'` is read as the regular
    expression it is, and refused.
    """
    verdict = _validate_command(command)

    assert verdict is not None and verdict[0] == "tier1", (command, verdict)
    assert fragment in verdict[1], (command, verdict[1])


# --- the target lives inside a body nobody reads ----------------------------

_SAYS_A_BODY = "body the gate does not read"

_BODY_ROWS = [
    "for /f %i in (pid.txt) do taskkill /PID %i /F",
    "for /f \"tokens=2\" %i in ('tasklist ^| findstr python') do taskkill /PID %i /F",
    "find . -name '*.pid' -exec kill {} +",
    "Get-Content pid.txt | ForEach-Object { Stop-Process -Id $_ }",
    "cat pids.txt | while read p; do kill $p; done",
    "if exist run.pid (taskkill /F /IM notepad.exe)",
    "if x; then kill $p; fi",
    # No braces: the keyword alone has to open the body here.
    "Get-Process notepad | ForEach-Object -MemberName Kill",
]


@pytest.mark.parametrize("command", _BODY_ROWS)
def test_a_kill_inside_a_body_the_gate_does_not_read_is_a_question(service, command):
    """Every row was measured tier0 — SILENT — on f9431bbb, the incident's own
    reconnaissance turned into a loop among them.

    `do` is not a segment operator, so the `for` line is one segment, and
    `_kill_verb` walks it from the left and gives up at `for`: the first token
    that is neither a verb nor a wrapper. The last-resort net caught only the
    variant that spells a protected pid out in the line. The answer is a second
    trigger for that net rather than a grammar for each construct — a kill word
    plus something that opens a body is Tier 1.
    """
    verdict = _validate_command(command)

    assert verdict is not None and verdict[0] == "tier1", (command, verdict)
    assert _SAYS_A_BODY in verdict[1], (command, verdict[1])
    assert verdict[1].startswith("A kill cannot be ruled out"), (command, verdict[1])


def test_the_escaped_pipe_does_not_cut_the_loop_in_half(service):
    """`^|` is cmd's escape and `_split_segments` has never heard of it. It does
    not have to: the `^|` in the incident's reconnaissance sits inside single
    quotes, which the splitter honours, so the whole loop stays one segment.
    The unquoted spelling is cut at the `|` — and the half carrying `do` and the
    kill word is still Tier 1, which is why the rule sits on the body keyword
    rather than on the loop header."""
    quoted = "for /f %i in ('tasklist ^| findstr python') do taskkill /PID %i /F"
    assert shell._split_segments(quoted) == [quoted]

    unquoted = "for /f %i in (tasklist ^| findstr python) do taskkill /PID %i /F"
    assert len(shell._split_segments(unquoted)) == 2
    assert tier_of(unquoted) == "tier1"
    assert _SAYS_A_BODY in reason_of(unquoted)


# --- and the rules that can name us still outrank it ------------------------


@pytest.mark.parametrize("command", [
    "for /f %%i in ('echo %d') do taskkill /PID %%i /F" % SERVICE.own_pid,
    "for %i in (1) do kill $PPID",
    "while true; do pkill python; done",
])
def test_a_body_does_not_downgrade_a_kill_that_names_this_service(service, command):
    """Tier 2 keeps coming from the rules that can say what dies: the
    last-resort net's literal pid, the self-naming variable it now also holds,
    and our own image name — which the refusal path reaches because it reads
    the body, while the naming path stops at the keyword."""
    verdict = _validate_command(command)

    assert verdict is not None and verdict[0] == "tier2", (command, verdict)
    assert "D-PC service" in verdict[1], (command, verdict[1])
    assert _SAYS_A_BODY not in verdict[1], (command, verdict[1])


def test_the_self_naming_variable_is_a_protected_token_of_the_net_too(service):
    """The net's second trigger, independent of the body reader: a standalone
    `$PPID` or `$$` beside a kill word is this service, however unparseable the
    line around them is. It carries the price the literal pid next door does —
    text merely holding both is refused — and quoting is what stops it, because
    a quoted string is one token and not the variable."""
    assert tier_of("echo kill $PPID") == "tier2"
    assert tier_of("echo kill $$") == "tier2"
    assert tier_of('echo "kill $PPID"') == "tier0"


# --- a small matrix: every construct against every verb ---------------------

_BODY_CONSTRUCTS = [
    "for /f %i in (pid.txt) do {cmd}",
    "cat pids.txt | while read p; do {cmd}; done",
    "if x; then {cmd}; fi",
    "if exist run.pid ({cmd})",
    "find . -name '*.pid' -exec {cmd} +",
    "Get-Content pid.txt | ForEach-Object {{ {cmd} }}",
    "Get-Content pid.txt | % {{ {cmd} }}",
    "foreach ($p in $pids) {{ {cmd} }}",
]

_BODY_KILLS = [
    "taskkill /PID %i /F",
    "kill $p",
    "Stop-Process -Id $_",
    "killall $n",
]


@pytest.mark.parametrize("construct", _BODY_CONSTRUCTS)
@pytest.mark.parametrize("verb", _BODY_KILLS)
def test_every_construct_around_every_verb_asks(service, construct, verb):
    """The generator further up multiplies wrappers by ways of naming us, and a
    body is neither — it is the case where nothing names anything. So it gets
    its own table, multiplied for the same reason: the construct nobody has
    typed yet is covered by the table, not by a row."""
    command = construct.format(cmd=verb)
    verdict = _validate_command(command)

    assert verdict is not None and verdict[0] == "tier1", (command, verdict)
    assert _SAYS_A_BODY in verdict[1], (command, verdict[1])


@pytest.mark.parametrize("construct", _BODY_CONSTRUCTS)
def test_the_same_constructs_around_ordinary_work_stay_where_they_were(service, construct):
    """The control table: a body is not a finding. Only a kill word inside one
    is, and `type` carries none."""
    assert tier_of(construct.format(cmd="type %f")) == "tier0", construct


# --- the negative controls, each measured before the rule was written -------


@pytest.mark.parametrize("command,tier,fragment", [
    ("echo taskkill", "tier0", ""),
    ('git commit -m "kill the bug"', "tier0", ""),
    ("echo done", "tier0", ""),
    ("for %f in (*.txt) do type %f", "tier0", ""),
    ("kill 4243", "tier1", "Kills process 4243: notepad.exe draft.txt"),
    ("cat pids.txt | xargs -I{} kill {}", "tier1", "could not identify"),
    ("ps aux | grep python | awk '{print $2}' | xargs kill", "tier1", "could not identify"),
    ("find . -name '*.log' -exec rm {} +", "tier1", "Requires approval"),
    # `{}` is find's placeholder, not a script block: with a word after it and
    # no other opener, reading it as one would turn this into a question.
    ("echo kill {} now", "tier0", ""),
    # The parser has already said what dies, so the body sentence would be the
    # same fact told twice.
    ("kill 4243 -exec true", "tier1", "Kills process 4243: notepad.exe draft.txt"),
])
def test_the_body_rule_leaves_these_exactly_as_they_were(service, command, tier, fragment):
    """`xargs` is the reason `xargs -I` is not in the construct table: it is
    already a wrapper the parser walks through, so the new sentence would be
    suppressed as a duplicate in every line that could carry it."""
    verdict = _validate_command(command)

    assert (verdict[0] if verdict else "tier0") == tier, (command, verdict)
    if fragment:
        assert fragment in verdict[1], (command, verdict[1])
    if verdict:
        assert _SAYS_A_BODY not in verdict[1], (command, verdict[1])


def test_the_price_of_the_body_rule(service):
    """Named as a price, like the unanchored HARDLINE pattern and the
    last-resort net before it.

    The keyword has to be a *token* of the command, which `_tokens` gives for
    free: it strips quotes and keeps a quoted string whole, so
    `echo "do not kill it"` has no token `do` and stays where it was. Written
    without the quotes it has one, and it is Tier 1 — a sentence about a body
    that is not there. That is the trade this file has now made three times."""
    assert tier_of('echo "do not kill it"') == "tier0"
    assert tier_of("echo do not kill it") == "tier1"
    assert _SAYS_A_BODY in reason_of("echo do not kill it")

    # One word apart from the control row in the table above
    # (`for %f in (*.txt) do type %f`, tier0): a loop over text files is ordinary
    # work until a kill word appears in its body, and then it is a question even
    # though `echo` is all that would run. The boundary is the word, and it is
    # declared here rather than left to be discovered.
    assert tier_of("for %f in (*.txt) do type %f") == "tier0"
    assert tier_of("for %f in (*.txt) do echo kill") == "tier1"
    assert _SAYS_A_BODY in reason_of("for %f in (*.txt) do echo kill")


def test_a_whitelisted_loop_waives_the_body_finding(service, tmp_path):
    """The per-agent whitelist semantics are unchanged, and unchanged means
    this: the finding is Tier 1, so an agent configured with `for` waives it —
    prefix-matched on the segment, exactly as `taskkill` waives an ordinary
    kill. Only the Tier 2 refusals are beyond configuration, and a loop that
    names this service stays one."""
    ctx = _Ctx(tmp_path, whitelist=["for"])

    assert _validate_command(
        "for /f %i in (pid.txt) do taskkill /PID %i /F", ctx, str(tmp_path)
    ) is None

    verdict = _validate_command("while true; do pkill python; done", ctx, str(tmp_path))
    assert verdict is not None and verdict[0] == "tier2", verdict


# --- «what is a kill verb» was written twice --------------------------------
# `_KILL_VERBS`, which the parser walks, holds eight; the plain-word regex the
# three wordy rules were gated on — the unreadable-wrapper note, the unread-body
# rule and the last-resort net — was a second, hand-written list of seven. The
# one it lacked was `wmic`, so a wmic kill the parser cannot reach tripped
# nothing at all. Both rows below were measured tier0 on fdf7514a.

_WMIC_IN_AN_UNREAD_BODY = [
    "for /f %i in (pid.txt) do wmic process where ProcessId=%i delete",
    "for /f %i in (pid.txt) do wmic process where ProcessId=%i call terminate",
    "Get-Content p.txt | ForEach-Object { wmic process where ProcessId=$_ delete }",
]


@pytest.mark.parametrize("command", _WMIC_IN_AN_UNREAD_BODY)
def test_a_wmic_kill_inside_a_body_the_gate_does_not_read_is_a_question(service, command):
    """The target comes out of a file, so no rule can name what dies — which is
    exactly the case the body rule exists for, and it never saw these."""
    verdict = _validate_command(command)

    assert verdict is not None and verdict[0] == "tier1", (command, verdict)
    assert verdict[1].startswith("A kill cannot be ruled out"), (command, verdict[1])
    assert _SAYS_A_BODY in verdict[1], (command, verdict[1])


@pytest.mark.parametrize("command", [
    "wmic process get ProcessId,Name",
    "wmic cpu get name",
    "for %i in (1) do wmic cpu get name",
    "echo 6520 & wmic os get caption",
])
def test_a_wmic_that_kills_nothing_is_not_a_kill_word(service, command):
    """The price of the fix, kept at zero. `wmic` is a broad word — listing
    processes, reading the cpu, reading the OS caption — so adding it to the
    plain words would turn every such line inside a loop into a question, and
    would have the last-resort net refuse `echo 6520 & wmic os get caption`
    because this service's pid stands beside it. It counts as a kill word only
    under the condition that already decides a wmic kill: the `process` class
    and a `delete` or `terminate`.
    """
    assert tier_of(command) == "tier0", command


@pytest.mark.parametrize("command,tier", [
    # the parser reaches this one on its own, and always did
    (f"wmic process where ProcessId={UNRELATED} delete", "tier1"),
    # and the refusal path reads bodies, so these two were never silent
    (f"for %i in (1) do wmic process where ProcessId={SERVICE.own_pid} delete", "tier2"),
    ('for %i in (1) do wmic process where name="python.exe" delete', "tier2"),
])
def test_the_wmic_rows_that_already_decided_keep_their_tier(service, command, tier):
    assert tier_of(command) == tier, command


def test_the_dialog_still_names_the_victim_of_an_ordinary_wmic_kill(service):
    """The control for the row above: one producer must not cost the sentence."""
    assert reason_of(f"wmic process where ProcessId={UNRELATED} delete") == (
        f"Kills process {UNRELATED}: notepad.exe draft.txt"
    )


# One minimal killing spelling per verb. The table is the point: it is compared
# against `_KILL_VERBS` itself, so a verb added to the set without a row here
# turns this test red rather than quietly leaving the wordy rules blind — which
# is the defect this section exists for.
_A_KILLING_SPELLING = {
    "taskkill": "taskkill /PID 12345 /F",
    "tskill": "tskill 12345",
    "kill": "kill 12345",
    "pkill": "pkill notepad",
    "killall": "killall notepad",
    "stop-process": "Stop-Process -Id 12345",
    "spps": "spps -Id 12345",
    "wmic": "wmic process where ProcessId=12345 delete",
}


def test_every_kill_verb_the_parser_knows_is_a_kill_word_to_the_wordy_rules():
    assert set(_A_KILLING_SPELLING) == set(shell._KILL_VERBS), (
        "a verb was added to _KILL_VERBS without a row here — the rules that look "
        "for a kill word without parsing one would not see it"
    )
    for verb, spelling in sorted(_A_KILLING_SPELLING.items()):
        assert shell._carries_a_kill_word(spelling), (verb, spelling)


def test_the_qualified_verb_is_not_a_kill_word_on_its_own():
    """The other half of the guard: `wmic` is in the set and is deliberately
    *not* in the plain words, so the reader must be asked, not the regex."""
    assert not shell._KILL_WORD_RE.search("wmic process where ProcessId=1 delete")
    assert shell._carries_a_kill_word("wmic process where ProcessId=1 delete")
    assert not shell._carries_a_kill_word("wmic cpu get name")
    assert not shell._carries_a_kill_word("wmic process get ProcessId,Name")
