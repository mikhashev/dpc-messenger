"""A command's root process decides what the Job Object may do (ADR-046, amendment of 2026-11-04).

While the root lives, the job names the command's processes for the polled sum and kills
them at the ceiling or the timeout. That closes the real hole: a chain whose middle exits,
whose leaf a walk by parent link cannot reach (measured at 1032 MB, ceiling 200, with a
result that said it was killed). Once the root has exited nothing here kills anything,
however the pipes stand. `start` from cmd passes the inheritable pipes on and cmd has no way
to give them up, so a server started that way holds them; the first form of this design
killed it at the timeout, and that is what an agent's server must not suffer (Mike's call,
2026-11-04). A runaway a command left behind is not watched: the accepted cost.

Every memory-using child is `bounded_allocator`'s; every sleeper here ends by itself.
"""

from __future__ import annotations

import gc
import json
import logging
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from dpc_client_core import job_object, process_registry
from dpc_client_core.dpc_agent.tools import process as process_mod
from dpc_client_core.dpc_agent.tools import shell as shell_tool
from dpc_client_core.dpc_agent.tools.process import SupervisedRun, run_supervised
from dpc_client_core.process_registry import SpawnOrigin, spawn_origin
from tests.bounded_allocator import TIMEOUT_S, allocator

psutil = pytest.importorskip("psutil")

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Job Objects exist on Windows only")
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="the POSIX walk, with no job")

CORE = Path(__file__).resolve().parents[1]
PROCESS_LOG = "dpc_client_core.dpc_agent.tools.process"
VERIFIED = "the command and its descendants were killed"
SLEEP_S = 40  # a sleeper ends by itself, whatever happens here


def _alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).is_running()
    except psutil.Error:
        return False


def _wait_for(predicate, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


def _gone(pid: int) -> bool:
    """A job reads empty when its members have begun to exit; the address space goes
    a moment later (0.1 ms and 9 ms for 251 MB, measured 2026-11-04)."""
    return _wait_for(lambda: not _alive(pid), 3)


class Shape:
    """Scripts in a temporary folder and the pids they report. Every pid a leaf
    reported is killed at the end, by that pid: nothing else is touched."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.pidfiles = []
        self.count = 0

    def script(self, body: str) -> str:
        self.count += 1
        path = self.folder / f"script_{self.count}.py"
        path.write_text(textwrap.dedent(body), encoding="utf-8")
        return str(path)

    def leaf(self, program: str) -> str:
        """A program that writes its own pid first: `sys.executable` may be a venv's
        launcher shim, and the interpreter that does the work is the shim's child."""
        pidfile = self.folder / f"leaf_{len(self.pidfiles)}.pid"
        self.pidfiles.append(pidfile)
        return self.script(f"import os\nopen({str(pidfile)!r}, 'w').write(str(os.getpid()))\n{program}\n")

    def starts(self, leaf: str, *, quiet: bool = False, then_sleep: float = 0) -> str:
        """A program that starts `leaf` and then exits, or sleeps. `quiet` gives the
        leaf output of its own; otherwise it holds the command's pipes."""
        redirect = "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL" if quiet else ""
        return self.script(f"""
            import subprocess, sys, time
            subprocess.Popen([sys.executable, {leaf!r}], {redirect})
            time.sleep({then_sleep})
        """)

    def pid(self, index: int = 0, seconds: float = 20.0) -> int:
        path = self.pidfiles[index]
        assert _wait_for(lambda: path.exists() and path.read_text().strip(), seconds), "the leaf never started"
        return int(path.read_text().strip())

    def cleanup(self) -> None:
        for path in self.pidfiles:
            try:
                psutil.Process(int(path.read_text().strip())).kill()
            except (OSError, ValueError, psutil.Error):
                pass


@pytest.fixture
def shape(tmp_path):
    made = Shape(tmp_path)
    yield made
    made.cleanup()


def _sleeper(seconds: float = SLEEP_S) -> str:
    return f"import time\ntime.sleep({seconds})"


def _run(script: str, **kwargs) -> SupervisedRun:
    kwargs.setdefault("launcher", "test(root decides)")
    kwargs.setdefault("timeout", TIMEOUT_S)
    return run_supervised([sys.executable, script], **kwargs)


def _middle(shape: Shape, program: str) -> str:
    """A live top, a middle that exits, a leaf the walk from the top cannot reach."""
    middle = shape.starts(shape.leaf(program))
    return shape.starts(middle, then_sleep=SLEEP_S)


def _shell(script: str) -> str:
    return f'"{sys.executable}" "{script}"'


# ------------------------------------------------- the real hole: the root lives, a link broke

@windows_only
def test_the_middle_of_a_chain_exits_and_the_ceiling_kills_the_leaf(shape, monkeypatch):
    monkeypatch.setattr(shell_tool, "_MEMORY_CEILING_MB", 200)
    started = time.monotonic()

    result = shell_tool._execute_shell_command(_shell(_middle(shape, allocator())), None, TIMEOUT_S)

    assert "over the 200 MB ceiling" in result and VERIFIED in result, result
    assert time.monotonic() - started < TIMEOUT_S / 2, "the clock stopped it, not the ceiling"
    assert _gone(shape.pid()), "the sentence said killed and the leaf is alive"


@windows_only
def test_the_middle_of_a_chain_exits_and_the_timeout_kills_the_leaf(shape, monkeypatch):
    monkeypatch.setattr(shell_tool, "_MEMORY_CEILING_MB", 0)

    result = shell_tool._execute_shell_command(_shell(_middle(shape, _sleeper())), None, 5)

    assert "timed out after 5s" in result and VERIFIED in result, result
    assert _gone(shape.pid()), "the sentence said killed and the leaf is alive"


@windows_only
def test_the_sum_counts_two_children_while_the_root_lives(shape):
    """Each stays under the ceiling and neither parent link is walkable; only the sum passes it."""
    holder = allocator(most_mb=100, lives_s=30)
    first, second = shape.leaf(holder), shape.leaf(holder)
    middle = shape.script(f"""
        import subprocess, sys
        subprocess.Popen([sys.executable, {first!r}])
        subprocess.Popen([sys.executable, {second!r}])
    """)

    run = _run(shape.starts(middle, then_sleep=SLEEP_S), ceiling_mb=200)

    assert run.exceeded_mb is not None and run.exceeded_mb > 200, run
    assert _gone(shape.pid(0)) and _gone(shape.pid(1))


@windows_only
def test_one_of_those_children_alone_stays_under_the_same_ceiling(shape):
    """The control for the sum."""
    leaf = shape.leaf(allocator(most_mb=100, lives_s=30))
    middle = shape.starts(leaf)

    run = _run(shape.starts(middle, then_sleep=SLEEP_S), timeout=6, ceiling_mb=200)

    assert run.exceeded_mb is None, run
    assert run.timed_out and _gone(shape.pid())


@windows_only
def test_a_stop_while_the_root_lives_reaches_the_orphan(shape):
    top = _middle(shape, _sleeper())
    runs = []
    origin = SpawnOrigin(agent_id="agent_stop", conversation_id="c", tool="run_shell", round=1)

    def _in_thread():
        with spawn_origin(origin):
            runs.append(_run(top, launcher="run_shell", ceiling_mb=0))

    thread = threading.Thread(target=_in_thread, daemon=True)
    thread.start()
    pid = shape.pid()

    process_registry.get_registry().stop_commands()

    assert _gone(pid), "the registry's kill stopped at the gap"
    thread.join(20)
    assert runs and runs[0].exit_reason == "backend_stop", runs


# --------------------------------------------- the root exited: what it left stays, however started

_FORMS = {
    "start_b_redirected": ('start "" /b "{py}" "{leaf}" >nul 2>&1', True),
    "start_min": ('start "" /min "{py}" "{leaf}"', True),
    "powershell_start_process": (
        "powershell -NoProfile -Command \"Start-Process -WindowStyle Hidden "
        "-FilePath '{py}' -ArgumentList '{leaf}'\"", False),
}


@windows_only
@pytest.mark.parametrize("name", list(_FORMS))
def test_a_server_started_the_way_an_agent_starts_one_stays(name, shape, monkeypatch):
    monkeypatch.setattr(shell_tool, "_MEMORY_CEILING_MB", 2048)
    form, holds_the_pipes = _FORMS[name]
    leaf = shape.leaf(_sleeper())
    timeout = 6

    started = time.monotonic()
    result = shell_tool._execute_shell_command(form.format(py=sys.executable, leaf=leaf), None, timeout)
    took = time.monotonic() - started

    pid = shape.pid()
    time.sleep(0.5)
    assert _alive(pid), f"{name} did not survive the call: {result!r}"
    assert "were killed" not in result and "timed out" not in result, result
    assert took < timeout + 3, f"{took:.1f} s: no drain after a timeout that kills nothing"
    if holds_the_pipes:
        assert "ended (exit code 0)" in result and "Nothing was killed" in result, result
    else:
        assert took < timeout - 1, f"{took:.1f} s: this form gives up the pipes, so nothing waits"
        assert "Nothing was killed" not in result, result


@windows_only
def test_what_it_had_printed_comes_back_with_the_sentence(shape):
    leaf = shape.leaf(_sleeper())
    top = shape.script(f"""
        import subprocess, sys
        print('before it left', flush=True)
        subprocess.Popen([sys.executable, {leaf!r}])
    """)

    result = shell_tool._execute_shell_command(_shell(top), None, 5)

    assert "before it left" in result and "Nothing was killed" in result, result
    assert _alive(shape.pid())


@windows_only
def test_the_run_says_left_running_and_the_entry_says_exited(shape):
    leaf = shape.leaf(_sleeper())
    top = shape.script(f"""
        import subprocess, sys
        subprocess.Popen([sys.executable, {leaf!r}])
        sys.exit(3)
    """)

    run = _run(top, timeout=4, ceiling_mb=2048)

    assert run.left_running and not run.timed_out and run.returncode == 3, run
    assert run.killed == "" and run.exit_reason == "exited", run
    assert _alive(shape.pid())


@windows_only
def test_a_probe_with_a_child_holding_its_pipes_answers_and_does_not_raise(shape):
    leaf = shape.leaf(_sleeper())

    done = process_mod.run_probe([sys.executable, shape.starts(leaf)], launcher="device_probe", timeout=3)

    assert done.returncode == 0
    assert _alive(shape.pid())


@windows_only
def test_a_runaway_the_command_left_is_not_watched_the_accepted_cost(shape, caplog):
    """Past the ceiling, root gone, pipes held: no kill, no sentence of one."""
    leaf = shape.leaf(allocator(most_mb=300, lives_s=25))

    run = _run(shape.starts(leaf), timeout=6, ceiling_mb=200)

    pid = shape.pid()
    assert run.left_running and run.exceeded_mb is None and run.killed == "", run
    assert _wait_for(lambda: _alive(pid) and psutil.Process(pid).memory_info().rss > 200 * 2**20, 10), (
        "the allocator never passed the ceiling: this proves nothing")
    time.sleep(2.5)  # past a poll
    assert _alive(pid)


@windows_only
def test_a_stop_after_the_root_exited_kills_nothing(shape):
    """At the backend's stop its own kill-on-close job takes what is left, not this code."""
    leaf = shape.leaf(_sleeper())
    top = shape.starts(leaf)
    runs = []
    origin = SpawnOrigin(agent_id="agent_stop", conversation_id="c", tool="run_shell", round=1)

    def _in_thread():
        with spawn_origin(origin):
            runs.append(_run(top, launcher="run_shell", timeout=6, ceiling_mb=0))

    thread = threading.Thread(target=_in_thread, daemon=True)
    thread.start()
    pid = shape.pid()
    time.sleep(1.5)  # the root has exited; the call is still waiting on the pipes

    process_registry.get_registry().stop_commands()

    assert _alive(pid), "a kill reached what the ended command left"
    thread.join(20)
    assert runs and runs[0].left_running and runs[0].killed == "", runs


@windows_only
def test_with_no_job_the_same_shape_also_returns_and_kills_nothing(shape, monkeypatch):
    monkeypatch.setattr(process_mod, "_new_command_job", lambda: None)
    leaf = shape.leaf(_sleeper())

    run = _run(shape.starts(leaf), timeout=4, ceiling_mb=0)

    assert run.left_running and not run.timed_out, run
    assert _alive(shape.pid())


_BACKEND = textwrap.dedent("""
    import json, os, subprocess, sys, time
    sys.path.insert(0, sys.argv[1])
    from dpc_client_core import job_object
    from dpc_client_core.dpc_agent.tools.process import run_supervised
    report = {"entered": job_object.enter_kill_on_close_job().entered if sys.argv[4] == "job" else None}
    run = run_supervised([sys.executable, sys.argv[2]], launcher="test(left)", timeout=30, ceiling_mb=2048)
    report["ok"] = run.ok
    with open(sys.argv[3] + ".tmp", "w") as fh:
        json.dump(report, fh)
    os.replace(sys.argv[3] + ".tmp", sys.argv[3])
    time.sleep(40)
""")


def _backend_with_a_left_server(shape, tmp_path, mode):
    top = shape.starts(shape.leaf(_sleeper()), quiet=True)
    script = tmp_path / "backend.py"
    script.write_text(_BACKEND, encoding="utf-8")
    out = tmp_path / "report.json"
    backend = subprocess.Popen(
        [sys.executable, str(script), str(CORE), top, str(out), mode],
        creationflags=process_mod.CREATE_NO_WINDOW, stdin=subprocess.DEVNULL,
    )
    assert _wait_for(out.exists, 60), "the backend never reported"
    report = json.loads(out.read_text())
    assert report["ok"] is True, report
    pid = shape.pid()
    assert _alive(pid), "a normal end killed what was left"
    return backend, pid, report


def _terminate_tree(backend, spare: int) -> None:
    """TerminateProcess, no cleanup runs. The server's parent has exited, so it is not on this walk."""
    parent = psutil.Process(backend.pid)
    tree = [parent, *parent.children(recursive=True)]
    assert spare not in {proc.pid for proc in tree}, "the server is on the walk: this proves nothing"
    for proc in tree:
        proc.kill()


@windows_only
def test_what_a_command_left_dies_with_the_backends_job(shape, tmp_path):
    backend, pid, report = _backend_with_a_left_server(shape, tmp_path, "job")
    try:
        assert report["entered"] is True
        _terminate_tree(backend, pid)
        assert _wait_for(lambda: not _alive(pid), 10), "it outlived the backend's job"
    finally:
        backend.kill()


@windows_only
def test_without_the_backends_job_the_same_server_outlives_it(shape, tmp_path):
    """The control: closing the command's handle ends nothing, so the kill above is the backend's job's."""
    backend, pid, _ = _backend_with_a_left_server(shape, tmp_path, "nojob")
    try:
        _terminate_tree(backend, pid)
        time.sleep(2)
        assert _alive(pid)
    finally:
        backend.kill()


@windows_only
def test_a_job_the_commands_child_makes_for_itself_nests_and_its_members_are_killed(shape):
    """The shape of Strata's `serve/winjob.py`: a kill-on-close job of its own."""
    leaf = shape.leaf(_sleeper())
    top = shape.script(f"""
        import subprocess, sys, time
        sys.path.insert(0, {str(CORE)!r})
        from dpc_client_core import job_object as j
        k32 = j._load_kernel32()
        own = j._create_kill_on_close_job(k32)
        child = subprocess.Popen([sys.executable, {leaf!r}])
        print("contained", bool(k32.AssignProcessToJobObject(own, int(child._handle))), flush=True)
        time.sleep({SLEEP_S})
    """)

    run = _run(top, timeout=8, ceiling_mb=0)

    assert "contained True" in run.stdout, (run.stdout, run.stderr)
    assert run.killed == VERIFIED, run.killed
    assert _gone(shape.pid())


# ------------------------------------------------------- the sentence is what was verified

@windows_only
def test_a_member_still_there_after_the_kill_is_not_reported_as_killed(shape, monkeypatch):
    real_kill = job_object.CommandJob.kill

    def misses_one(self, *args, **kwargs):
        real_kill(self, *args, **kwargs)  # really ends them; only the report is under test
        return [4242]

    monkeypatch.setattr(job_object.CommandJob, "kill", misses_one)
    run = _run(shape.starts(shape.leaf(_sleeper()), then_sleep=SLEEP_S), timeout=3, ceiling_mb=0)

    assert run.timed_out
    assert "were killed" not in run.killed and "may survive" in run.killed, run.killed


def test_the_agent_is_not_told_killed_when_the_kill_missed(monkeypatch):
    """The tool's own words came before the verdict: «and were killed - it did not reach»."""
    partial = "the kill did not reach the whole tree (taskkill exited 5); a descendant may survive"
    monkeypatch.setattr(shell_tool, "run_supervised",
                        lambda *a, **k: SupervisedRun(exceeded_mb=300, ceiling_mb=200, killed=partial))

    result = shell_tool._execute_shell_command("echo hi", None, 10)

    assert "over the 200 MB ceiling" in result and partial in result
    assert "were killed" not in result, result


def test_a_taskkill_that_exited_zero_claims_only_the_walk(monkeypatch):
    """With no job (one that could not be made, a pid from the registry) nothing can
    name an orphan, so success of `taskkill /T` covers what its walk reached."""
    monkeypatch.setattr(process_mod.platform, "system", lambda: "Windows")
    monkeypatch.setattr(process_mod.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, b"", b""))

    said = process_mod.kill_pid_tree(4242)

    assert said != VERIFIED and "parent" in said and "may survive" in said, said


# --------------------------------------------------------- the job is entered before the child runs

@windows_only
def test_the_child_is_a_member_before_it_runs_a_single_instruction(monkeypatch):
    real_popen, real_wake = subprocess.Popen, process_mod._wake
    flags, members = [], []

    def spy_popen(*args, **kwargs):
        flags.append(kwargs["creationflags"])
        return real_popen(*args, **kwargs)

    def spy_wake(process):
        members.append((process.pid, process_mod._job_of(process).members()))
        return real_wake(process)

    monkeypatch.setattr(process_mod.subprocess, "Popen", spy_popen)
    monkeypatch.setattr(process_mod, "_wake", spy_wake)

    run = run_supervised([sys.executable, "-c", "print('ran')"], launcher="test(order)", timeout=30, ceiling_mb=0)

    assert run.stdout.strip() == "ran"
    assert flags[0] == process_mod.CREATE_NO_WINDOW | process_mod.CREATE_SUSPENDED
    (pid, seen), = members
    assert seen == [pid], "woken before it was a member: its first child could be born outside"


@windows_only
def test_a_callers_own_flags_stay_beside_the_suspended_one(monkeypatch):
    real_popen = subprocess.Popen
    flags = []

    def spy_popen(*args, **kwargs):
        flags.append(kwargs["creationflags"])
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(process_mod.subprocess, "Popen", spy_popen)
    run_supervised([sys.executable, "-c", "pass"], launcher="test(flags)", timeout=30, ceiling_mb=0,
                   popen_kwargs={"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP})

    assert flags == [subprocess.CREATE_NEW_PROCESS_GROUP | process_mod.CREATE_SUSPENDED]


# ---------------------------------------------------------------- a job failure is never a failed command

def _boom_create(platform=None):
    raise job_object._StepFailed("CreateJobObjectW", 5)


def _boom_enter(self, process):
    raise job_object._StepFailed("AssignProcessToJobObject", 5)


@windows_only
@pytest.mark.parametrize("step, patch", [
    ("CreateJobObjectW", lambda mp: mp.setattr(job_object, "create_command_job", _boom_create)),
    ("AssignProcessToJobObject", lambda mp: mp.setattr(job_object.CommandJob, "enter", _boom_enter)),
])
def test_a_job_that_fails_falls_back_to_the_walk_with_a_log_line(step, patch, monkeypatch, caplog, shape):
    patch(monkeypatch)
    monkeypatch.setattr(process_mod, "_job_failures_announced", set())

    with caplog.at_level(logging.WARNING, logger=PROCESS_LOG):
        run = run_supervised([sys.executable, "-c", "print('still ran')"], launcher="test(no job)",
                             timeout=30, ceiling_mb=200)
        walked = _run(shape.starts(shape.leaf(_sleeper()), then_sleep=SLEEP_S), timeout=4, ceiling_mb=0)

    assert run.ok and run.stdout.strip() == "still ran", run
    lines = [r.getMessage() for r in caplog.records]
    assert any(step in line and "parent link" in line for line in lines), lines
    assert sum(step in line for line in lines) == 1, f"said once per failure, not per command: {lines}"
    assert "parent" in walked.killed and walked.killed != VERIFIED, walked.killed
    assert _gone(shape.pid()), "the walk reached a chain with every link alive"


class _SpiedPopen:
    """The children `run_supervised` starts, and their flags; `kill_fails` breaks the kill of the first."""

    def __init__(self, monkeypatch, kill_fails=False):
        self.started = []
        real_popen = subprocess.Popen

        def spy(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            if kill_fails and not self.started:
                child.real_kill = child.kill

                def _refuses():
                    raise PermissionError("denied by the test")

                child.kill = _refuses
            self.started.append((child, kwargs["creationflags"]))
            return child

        monkeypatch.setattr(process_mod.subprocess, "Popen", spy)


def _cannot_wake(process):
    raise RuntimeError("no resume for this test")


@windows_only
def test_a_child_that_cannot_be_woken_is_started_again_and_the_reason_is_said_once(monkeypatch, caplog):
    spied = _SpiedPopen(monkeypatch)
    monkeypatch.setattr(process_mod, "_wake", _cannot_wake)
    monkeypatch.setattr(process_mod, "_job_failures_announced", set())

    with caplog.at_level(logging.WARNING, logger=PROCESS_LOG):
        for _ in range(2):
            run = run_supervised([sys.executable, "-c", "print('second try')"], launcher="test(no wake)",
                                 timeout=30, ceiling_mb=0)
            assert run.ok and run.stdout.strip() == "second try", run

    flags = [flag for _, flag in spied.started]
    assert flags[:2] == [process_mod.CREATE_NO_WINDOW | process_mod.CREATE_SUSPENDED, process_mod.CREATE_NO_WINDOW]
    assert spied.started[0][0].poll() is not None, "the suspended one was left behind, frozen"
    lines = [r.getMessage() for r in caplog.records if "could not be woken" in r.getMessage()]
    assert len(lines) == 1 and "no resume for this test" in lines[0], lines


@windows_only
def test_a_suspended_child_that_cannot_be_killed_either_is_said(monkeypatch, caplog):
    spied = _SpiedPopen(monkeypatch, kill_fails=True)
    monkeypatch.setattr(process_mod, "_wake", _cannot_wake)
    monkeypatch.setattr(process_mod, "_job_failures_announced", set())

    with caplog.at_level(logging.WARNING, logger=PROCESS_LOG):
        run = run_supervised([sys.executable, "-c", "print('second try')"], launcher="test(no kill)",
                             timeout=30, ceiling_mb=0)
    frozen = spied.started[0][0]
    frozen.real_kill()
    frozen.wait(5)

    assert run.ok and run.stdout.strip() == "second try", run
    assert any("could not be killed either" in r.getMessage() and "denied by the test" in r.getMessage()
               for r in caplog.records), [r.getMessage() for r in caplog.records]


# ------------------------------------------------------------------ the handle closes on every path

def _handles_grown(action, times: int = 20) -> int:
    """Handles this process gained over `times` runs of `action`, once its objects are collected."""
    for _ in range(2):
        action()
    gc.collect()
    time.sleep(0.5)
    before = psutil.Process().num_handles()
    for _ in range(times):
        action()
    gc.collect()
    time.sleep(0.5)
    return psutil.Process().num_handles() - before


def _clean():
    run_supervised(["cmd", "/c", "exit"], launcher="test(clean)", timeout=30, ceiling_mb=2048)


@windows_only
def test_commands_that_end_cleanly_leave_no_handle():
    grown = _handles_grown(_clean)
    assert grown < 10, f"{grown} handles after 20 commands: a job per command"


@windows_only
def test_a_binary_that_cannot_start_leaves_no_handle():
    def missing():
        with pytest.raises(OSError):
            run_supervised(["no-such-binary-for-this-test"], launcher="test(clean)", timeout=30, ceiling_mb=2048)

    grown = _handles_grown(missing)
    assert grown < 10, f"{grown} handles after 20 refused starts"


@windows_only
def test_a_failure_after_the_spawn_leaves_no_handle(monkeypatch):
    def _no_reader(*args, **kwargs):
        raise RuntimeError("no readers for this test")

    monkeypatch.setattr(process_mod, "_OutputReader", _no_reader)
    monkeypatch.setattr(process_mod, "_registry", lambda: None)  # an open entry holds the Popen

    def fails():
        with pytest.raises(RuntimeError):
            run_supervised(["cmd", "/c", "exit"], launcher="test(reader)", timeout=30, ceiling_mb=0)

    grown = _handles_grown(fails)
    assert grown < 10, f"{grown} handles after 20 failures past the spawn"


@windows_only
def test_a_refused_registration_leaves_no_handle(monkeypatch):
    registry = process_registry.get_registry()

    def _refuses(*args, **kwargs):
        raise RuntimeError("no entry for this test")

    monkeypatch.setattr(registry, "register", _refuses)

    def fails():
        with pytest.raises(RuntimeError):
            run_supervised(["cmd", "/c", "exit"], launcher="test(register)", timeout=30, ceiling_mb=0)

    grown = _handles_grown(fails)
    assert grown < 10, f"{grown} handles after 20 refused registrations"


@windows_only
def test_commands_that_left_something_leave_no_job_handle_and_no_thread_of_ours(shape):
    leaves = [shape.leaf(_sleeper(20)) for _ in range(10)]
    before_threads = {t.name for t in threading.enumerate()}
    before = psutil.Process().num_handles()
    for leaf in leaves:
        assert _run(shape.starts(leaf, quiet=True), ceiling_mb=2048).ok
    gc.collect()
    time.sleep(0.5)

    assert psutil.Process().num_handles() - before < 8, "the job handles of ten ended commands"
    assert {t.name for t in threading.enumerate()} <= before_threads, "a thread stayed behind the commands"
    assert all(_alive(shape.pid(i)) for i in range(10))


@windows_only
def test_the_readers_of_a_left_pipe_leave_when_the_holder_ends(shape):
    """Bounded by what holds the pipe: two blocked daemon threads until it ends."""
    leaf = shape.leaf(_sleeper())

    run = _run(shape.starts(leaf), timeout=2, ceiling_mb=0)

    assert run.left_running
    readers = [t for t in threading.enumerate() if t.name.startswith("dpc-output-")]
    assert readers, "the readers had already left: this proves nothing"
    psutil.Process(shape.pid()).kill()
    assert _wait_for(lambda: not any(t.is_alive() for t in readers), 10)


# --------------------------------------------------------------------------- off Windows: no change

@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_no_job_is_made_off_windows(platform):
    assert job_object.create_command_job(platform=platform) is None


@posix_only
def test_off_windows_a_command_gets_no_job_and_what_it_left_stays(tmp_path):
    made = Shape(tmp_path)
    try:
        assert process_mod._new_command_job() is None
        run = _run(made.starts(made.leaf(_sleeper(5)), quiet=True), ceiling_mb=2048)
        assert run.ok and not run.left_running
        assert _alive(made.pid())
    finally:
        made.cleanup()


@posix_only
def test_off_windows_a_timeout_with_the_pipes_held_is_what_it_was(tmp_path):
    made = Shape(tmp_path)
    try:
        run = _run(made.starts(made.leaf(_sleeper(8))), timeout=2, ceiling_mb=0)
        assert run.timed_out and not run.left_running, run
    finally:
        made.cleanup()


def test_the_module_keeps_working_with_no_job_module(monkeypatch):
    """A harness that loads `process.py` by path, with no package on the path."""
    monkeypatch.setattr(process_mod, "_job_module", lambda: None)
    assert process_mod._new_command_job() is None
    assert process_mod._job_of(object()) is None
    run = run_supervised([sys.executable, "-c", "print('plain')"], launcher="test(plain)", timeout=30, ceiling_mb=0)
    assert run.ok and run.stdout.strip() == "plain"
