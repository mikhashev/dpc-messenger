"""ADR-046 point 1: the backend puts itself in a kill-on-close Job Object.

Every test that really enters the job does it in a child interpreter. Never call
`enter_kill_on_close_job()` for real in this process: it would put the pytest
run itself in a job that ends with it.

The Windows tests start real processes and kill the child interpreter with
`TerminateProcess` (psutil's `kill`), which runs none of its cleanup, then look
at the grandchildren. Each has an empty control (the same without the job) that
must leave the grandchild alive, so that "gone" cannot be a harness that kills
everything. Everything started here is killed in a `finally`.

Two checks are measurements for the ADR's Open Questions rather than
regressions, and are opt-in because they launch a real browser or Windows shell
(`DPC_JOB_CHECKS=1`): Camoufox under the job (Q4) and what ShellExecute starts
(Q3).
"""

import ast
import json
import logging
import os
import pathlib
import subprocess
import sys
import textwrap
import time

import pytest

from dpc_client_core import job_object

CORE_DIR = pathlib.Path(__file__).resolve().parents[1]
RUN_SERVICE = CORE_DIR / "run_service.py"
PROCESS_PY = CORE_DIR / "dpc_client_core" / "dpc_agent" / "tools" / "process.py"
# The Strata checkout (the provider's `strata_install_dir`): DPC_STRATA_INSTALL_DIR,
# else a `dpc-research/Strata` beside this repository. The tests that need it skip
# where it is absent.
STRATA_INSTALL_DIR = pathlib.Path(
    os.environ.get("DPC_STRATA_INSTALL_DIR")
    or CORE_DIR.parents[2] / "dpc-research" / "Strata"
)
STRATA_WINJOB = STRATA_INSTALL_DIR / "serve" / "winjob.py"

windows_only = pytest.mark.skipif(
    sys.platform != "win32", reason="Job Objects exist on Windows only"
)
opt_in = pytest.mark.skipif(
    os.environ.get("DPC_JOB_CHECKS") != "1",
    reason="launches a real browser / shell; set DPC_JOB_CHECKS=1",
)

CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

# One child interpreter, several behaviours. It reports through a JSON file and
# then sleeps until the test kills it.
CHILD = textwrap.dedent(
    r'''
    import importlib.util, json, logging, os, re, subprocess, sys, threading, time

    sys.path.insert(0, {core!r})
    from dpc_client_core import job_object

    mode, out = sys.argv[1], sys.argv[2]
    use_job = "nojob" not in mode and mode != "inner"
    report = {{"parent": os.getpid(), "mode": mode}}

    def write():
        tmp = out + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(report, fh)
        os.replace(tmp, out)

    def sleeper():
        return [sys.executable, "-c", "import time; time.sleep(600)"]

    if use_job:
        outcome = job_object.enter_kill_on_close_job()
        report["entered"] = outcome.entered
        report["outcome"] = outcome.message

    if mode.startswith("kill"):
        cmd = subprocess.Popen(
            ["cmd", "/c", "ping -n 600 127.0.0.1 >nul"], creationflags={no_window}
        )
        report["cmd"] = cmd.pid
        spec = importlib.util.spec_from_file_location("sup_process", {process!r})
        sup = importlib.util.module_from_spec(spec)
        sys.modules["sup_process"] = sup
        spec.loader.exec_module(sup)
        seen = []

        class Grab(logging.Handler):
            def emit(self, record):
                m = re.search(r"spawned pid (\d+)", record.getMessage())
                if m:
                    seen.append(int(m.group(1)))

        sup.log.addHandler(Grab())
        sup.log.setLevel(logging.INFO)
        threading.Thread(
            target=lambda: sup.run_supervised(
                sleeper(), launcher="test", timeout=500, ceiling_mb=0),
            daemon=True,
        ).start()
        import psutil
        deadline = time.time() + 20
        while time.time() < deadline:
            ping = [p.pid for p in psutil.Process(cmd.pid).children(recursive=True)
                    if p.name().lower() == "ping.exe"]
            if seen and ping:
                break
            time.sleep(0.05)
        report["sleeper"], report["ping"] = (seen or [None])[0], (ping or [None])[0]

    elif mode.startswith("breakaway"):
        try:
            p = subprocess.Popen(
                sleeper(), creationflags={breakaway} | {no_window})
            report["spawned"] = p.pid
        except OSError as exc:
            report["error"] = {{
                "type": type(exc).__name__, "winerror": getattr(exc, "winerror", None),
                "errno": exc.errno, "text": str(exc),
            }}

    elif mode.startswith("nested"):
        inner = subprocess.Popen(
            [sys.executable, sys.argv[0], "inner", out + ".inner", *sys.argv[3:]],
            creationflags={no_window},
        )
        report["inner"] = inner.pid
        deadline = time.time() + 30
        while time.time() < deadline and not os.path.exists(out + ".inner"):
            time.sleep(0.05)
        with open(out + ".inner") as fh:
            report.update(json.load(fh))

    elif mode == "strata":
        root, mcp_config, port = sys.argv[3:6]
        server = subprocess.Popen(
            [sys.executable, "-m", "serve.server", "--engine", "mock", "--port", port,
             "--mcp-config", mcp_config],
            cwd=root, creationflags={no_window}, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        )
        report["server"] = server.pid

    elif mode == "inner":
        winjob_path = sys.argv[3] if len(sys.argv) > 3 else ""
        great = subprocess.Popen(sleeper(), creationflags={no_window})
        report["great"] = great.pid
        if winjob_path:
            spec = importlib.util.spec_from_file_location("winjob", winjob_path)
            wj = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(wj)
            report["contained"] = wj.contain(great)
        else:
            k32 = job_object._load_kernel32()
            job2 = job_object._create_kill_on_close_job(k32)
            report["contained"] = bool(
                k32.AssignProcessToJobObject(job2, int(great._handle)))

    write()
    time.sleep(600)
    '''
).format(
    core=str(CORE_DIR),
    process=str(PROCESS_PY),
    no_window=CREATE_NO_WINDOW,
    breakaway=CREATE_BREAKAWAY_FROM_JOB,
)


def _start_child(tmp_path, mode, *extra):
    script = tmp_path / "child.py"
    script.write_text(CHILD, encoding="utf-8")
    out = tmp_path / f"{mode}.json"
    proc = subprocess.Popen(
        [sys.executable, str(script), mode, str(out), *extra],
        creationflags=CREATE_NO_WINDOW,
    )
    return proc, out


def _read_report(proc, out, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if out.exists():
            try:
                return json.loads(out.read_text())
            except ValueError:
                pass
        assert proc.poll() is None, "the child interpreter died before reporting"
        time.sleep(0.05)
    raise AssertionError("the child interpreter never reported")


def _handles(pids):
    import psutil

    found = []
    for pid in pids:
        try:
            found.append(psutil.Process(pid))
        except psutil.NoSuchProcess:
            pass
    return found


def _wait_gone(procs, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not any(p.is_running() for p in procs):
            return True
        time.sleep(0.1)
    return not any(p.is_running() for p in procs)


def _terminate_all(procs):
    for p in procs:
        try:
            p.kill()
        except Exception:
            pass


@windows_only
def test_nothing_survives_the_backend_when_it_is_terminated(tmp_path):
    import psutil

    child, out = _start_child(tmp_path, "kill")
    victims = []
    try:
        report = _read_report(child, out)
        assert report["entered"], report
        assert None not in (report["sleeper"], report["cmd"], report["ping"]), report
        victims = _handles([report["sleeper"], report["cmd"], report["ping"]])
        assert len(victims) == 3
        psutil.Process(child.pid).kill()  # TerminateProcess: no cleanup runs
        assert _wait_gone(victims, 5), [
            p.pid for p in victims if p.is_running()
        ]
    finally:
        _terminate_all(victims)
        child.kill()


@windows_only
def test_without_the_job_the_same_grandchildren_survive_the_terminate(tmp_path):
    """The empty control: the harness itself kills nothing."""
    import psutil

    child, out = _start_child(tmp_path, "kill_nojob")
    victims = []
    try:
        report = _read_report(child, out)
        assert "entered" not in report
        victims = _handles([report["sleeper"], report["cmd"], report["ping"]])
        assert len(victims) == 3
        psutil.Process(child.pid).kill()
        time.sleep(2)
        assert all(p.is_running() for p in victims)
    finally:
        _terminate_all(victims)
        child.kill()


def _nested(tmp_path, *extra):
    import psutil

    child, out = _start_child(tmp_path, "nested", *extra)
    victims = []
    try:
        report = _read_report(child, out)
        assert report["entered"], report
        assert report["contained"] is True, "the nested job refused its member"
        victims = _handles([report["inner"], report["great"]])
        assert len(victims) == 2
        psutil.Process(child.pid).kill()
        assert _wait_gone(victims, 5), [p.pid for p in victims if p.is_running()]
    finally:
        _terminate_all(victims)
        child.kill()


@windows_only
def test_a_job_nested_inside_the_backends_job_dies_with_it(tmp_path):
    _nested(tmp_path)


@windows_only
@pytest.mark.skipif(not STRATA_WINJOB.exists(), reason="the Strata checkout is not on this machine")
def test_the_strata_launchers_own_contain_nests_inside_the_backends_job(tmp_path):
    _nested(tmp_path, str(STRATA_WINJOB))


STRATA_ROOT = STRATA_WINJOB.parents[1]


@windows_only
@pytest.mark.skipif(not STRATA_WINJOB.exists(), reason="the Strata checkout is not on this machine")
def test_the_real_strata_launcher_with_a_mock_engine_and_an_mcp_child_dies_with_the_backend(tmp_path):
    """The launcher's own process tree, no GPU: `serve.server --engine mock`
    starts an MCP server child through its `contain()`. Port is a free one, so
    a Strata the owner runs is never touched."""
    import json as _json
    import socket

    import psutil

    fake = STRATA_ROOT / "serve" / "mcp_fake_server.py"
    config = tmp_path / "mcp.json"
    config.write_text(
        _json.dumps({"mcpServers": {"fake": {"command": sys.executable, "args": [str(fake)]}}}),
        encoding="utf-8",
    )
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    child, out = _start_child(tmp_path, "strata", str(STRATA_ROOT), str(config), str(port))
    victims = []
    try:
        report = _read_report(child, out)
        assert report["entered"], report
        server = psutil.Process(report["server"])
        deadline = time.time() + 60
        mcp = []
        while time.time() < deadline and not mcp:
            assert server.is_running(), "the mock Strata server exited by itself"
            mcp = [p for p in server.children(recursive=True) if "mcp_fake_server" in " ".join(p.cmdline())]
            time.sleep(0.2)
        assert mcp, "the launcher never started its MCP child"
        victims = [server, *server.children(recursive=True)]
        psutil.Process(child.pid).kill()
        assert _wait_gone(victims, 8), [p.pid for p in victims if p.is_running()]
    finally:
        _terminate_all(victims)
        child.kill()


class FakeKernel32:
    """Only what job_object calls; one step can be told to fail."""

    def __init__(self, fail_at=None):
        self.fail_at = fail_at
        self.closed = []

    def CreateJobObjectW(self, attrs, name):
        return 0 if self.fail_at == "CreateJobObjectW" else 1234

    def SetInformationJobObject(self, job, cls, info, size):
        return 0 if self.fail_at == "SetInformationJobObject" else 1

    def GetCurrentProcess(self):
        return -1

    def AssignProcessToJobObject(self, job, proc):
        return 0 if self.fail_at == "AssignProcessToJobObject" else 1

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1


@pytest.fixture
def no_real_job(monkeypatch):
    import ctypes

    monkeypatch.setattr(job_object, "_job_handle", None)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)


@pytest.mark.parametrize(
    "step", ["CreateJobObjectW", "SetInformationJobObject", "AssignProcessToJobObject"]
)
def test_a_step_that_fails_is_a_warning_naming_it_and_startup_goes_on(
    step, no_real_job, monkeypatch, caplog
):
    fake = FakeKernel32(fail_at=step)
    monkeypatch.setattr(job_object, "_load_kernel32", lambda: fake)

    outcome = job_object.enter_kill_on_close_job(platform="win32")

    assert outcome.level == logging.WARNING and not outcome.entered
    assert step in outcome.message and "Windows error 5" in outcome.message
    assert job_object._job_handle is None
    if step != "CreateJobObjectW":
        assert fake.closed == [1234], "a job that could not be used must not leak its handle"
    with caplog.at_level(logging.DEBUG, logger="dpc_client_core.job_object"):
        outcome.log()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and step in warnings[0].getMessage()


def test_a_kernel32_that_cannot_be_loaded_is_a_warning_too(no_real_job, monkeypatch):
    def boom():
        raise OSError("no kernel32 here")

    monkeypatch.setattr(job_object, "_load_kernel32", boom)
    outcome = job_object.enter_kill_on_close_job(platform="win32")
    assert outcome.level == logging.WARNING and "no kernel32 here" in outcome.message


def test_when_every_step_works_the_job_is_in_place_and_says_so_at_info(no_real_job, monkeypatch):
    fake = FakeKernel32()
    monkeypatch.setattr(job_object, "_load_kernel32", lambda: fake)
    outcome = job_object.enter_kill_on_close_job(platform="win32")
    assert outcome.level == logging.INFO and outcome.entered
    assert job_object._job_handle == 1234
    assert job_object.enter_kill_on_close_job(platform="win32").entered  # idempotent


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_elsewhere_it_does_nothing(platform, no_real_job):
    outcome = job_object.enter_kill_on_close_job(platform=platform)
    assert not outcome.entered and outcome.level == logging.DEBUG
    assert job_object._job_handle is None


def test_the_module_pulls_in_nothing_heavy():
    tree = ast.parse((CORE_DIR / "dpc_client_core" / "job_object.py").read_text(encoding="utf-8"))
    top = set()
    for n in tree.body:
        if isinstance(n, ast.Import):
            top.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            top.add(n.module.split(".")[0])
    assert top <= {"__future__", "logging", "os", "sys", "dataclasses", "typing"}


def test_run_service_enters_the_job_before_anything_spawns_and_logs_it_after_logging():
    tree = ast.parse(RUN_SERVICE.read_text(encoding="utf-8"))
    main = next(
        n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "main"
    )
    first = {}
    for n in ast.walk(main):
        if isinstance(n, ast.Call):
            f = n.func
            name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
            if name:
                first[name] = min(first.get(name, 10**9), n.lineno)
    assert "enter_kill_on_close_job" in first, "main() never enters the job"
    for spawner in ("dependency_setup", "CoreService", "setup_logging", "start_console_guard"):
        assert first["enter_kill_on_close_job"] < first[spawner], spawner
    assert first["setup_logging"] < first["log"], "the outcome is logged before logging exists"


@windows_only
def test_a_member_of_the_job_that_asks_for_breakaway_fails_to_start(tmp_path):
    child, out = _start_child(tmp_path, "breakaway")
    try:
        report = _read_report(child, out)
        assert report["entered"], report
        assert "spawned" not in report, f"a child escaped the job: {report}"
        assert report["error"]["winerror"] == 5, report  # ERROR_ACCESS_DENIED
        print("breakaway refused with:", report["error"])
    finally:
        child.kill()


@windows_only
def test_without_the_job_the_same_breakaway_request_starts_a_process(tmp_path):
    """The control: the flag alone is harmless outside a job, so the refusal
    above is the job's doing."""
    child, out = _start_child(tmp_path, "breakaway_nojob")
    spawned = []
    try:
        report = _read_report(child, out)
        assert "entered" not in report
        spawned = _handles([report["spawned"]]) if report.get("spawned") else []
        assert spawned, f"the control did not start a process: {report}"
    finally:
        _terminate_all(spawned)
        child.kill()


CAMOUFOX_CHILD = textwrap.dedent(
    r'''
    import json, sys, os
    sys.path.insert(0, {core!r})
    from dpc_client_core import job_object
    import ctypes
    from ctypes import wintypes
    import psutil
    report = {{}}
    outcome = job_object.enter_kill_on_close_job()
    report["entered"] = outcome.entered
    from dpc_client_core.dpc_agent.tools import browser as b
    b._require_camoufox_binary()
    from camoufox.sync_api import Camoufox
    k32 = job_object._load_kernel32()
    def in_my_job(pid):
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not h:
            return None
        r = wintypes.BOOL()
        k32.IsProcessInJob(h, job_object._job_handle, ctypes.byref(r))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(r.value)
    try:
        with Camoufox(headless=True, **b._camoufox_launch_kwargs()) as browser:
            page = browser.new_page()
            page.goto("about:blank")
            kids = psutil.Process().children(recursive=True)
            report["during"] = [[p.pid, p.name(), in_my_job(p.pid)] for p in kids]
        report["closed"] = True
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)[:500]
    report["left_after_close"] = [p.name() for p in psutil.Process().children(recursive=True)]
    print("REPORT" + json.dumps(report))
    '''
).format(core=str(CORE_DIR))


@windows_only
@opt_in
def test_camoufox_launches_browses_and_closes_inside_the_job(tmp_path):
    """ADR-046 Open Question Q4. Headless, as the agent's browser tool does it."""
    pytest.importorskip("camoufox")
    from dpc_client_core import camoufox_binary

    if camoufox_binary.missing_reason() is not None:
        pytest.skip("the Camoufox browser binary is not installed")
    script = tmp_path / "camoufox_child.py"
    script.write_text(CAMOUFOX_CHILD, encoding="utf-8")
    run = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=180,
        creationflags=CREATE_NO_WINDOW,
    )
    line = next((l for l in run.stdout.splitlines() if l.startswith("REPORT")), None)
    assert line, (run.stdout[-2000:], run.stderr[-2000:])
    report = json.loads(line[len("REPORT"):])
    print("Camoufox under the job:", report)
    assert report["entered"] and "error" not in report, report
    assert report["closed"] and report["during"]
    assert all(row[2] for row in report["during"]), "a browser process is outside the job"
    assert report["left_after_close"] == [], report


SHELL_CHILD = textwrap.dedent(
    r'''
    import ctypes, json, os, subprocess, sys, time
    from ctypes import wintypes
    sys.path.insert(0, {core!r})
    from dpc_client_core import job_object
    import psutil
    scratch = sys.argv[1]
    report = {{"entered": job_object.enter_kill_on_close_job().entered}}

    class SEI(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                    ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR),
                    ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
                    ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                    ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR),
                    ("hkeyClass", wintypes.HKEY), ("dwHotKey", wintypes.DWORD),
                    ("hIcon", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SEI)]
    k32 = job_object._load_kernel32()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessId.argtypes = [wintypes.HANDLE]
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]

    def in_job(handle):
        r = wintypes.BOOL()
        k32.IsProcessInJob(handle, job_object._job_handle, ctypes.byref(r))
        return bool(r.value)

    def execute(file, params=None):
        sei = SEI()
        sei.cbSize = ctypes.sizeof(SEI)
        sei.fMask = 0x40  # SEE_MASK_NOCLOSEPROCESS
        sei.lpFile, sei.lpParameters, sei.nShow = file, params, 0  # SW_HIDE
        ok = shell32.ShellExecuteExW(ctypes.byref(sei))
        row = {{"file": file, "ok": bool(ok), "hProcess": bool(sei.hProcess)}}
        if ok and sei.hProcess:
            pid = kernel.GetProcessId(sei.hProcess)
            row.update(pid=pid, in_backend_job=in_job(sei.hProcess))
            try:
                row["name"] = psutil.Process(pid).name()
            except Exception as exc:
                row["name"] = type(exc).__name__
            kernel.TerminateProcess(sei.hProcess, 1)
            kernel.CloseHandle(sei.hProcess)
        return row

    before = {{p.pid for p in psutil.process_iter()}}
    report["notepad_exe"] = execute("notepad.exe", scratch)
    time.sleep(2)
    report["txt_document"] = execute(scratch)
    time.sleep(2)
    mine = []
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        if p.pid in before:
            continue
        if os.path.basename(scratch) in " ".join(p.info["cmdline"] or []):
            h = kernel.OpenProcess(0x1000, False, p.pid)
            mine.append([p.pid, p.info["name"], in_job(h) if h else None])
            if h:
                kernel.CloseHandle(h)
            p.kill()
    report["other_processes_with_the_file"] = mine
    print("REPORT" + json.dumps(report))
    '''
).format(core=str(CORE_DIR))


@windows_only
@opt_in
def test_what_shellexecute_starts_inside_the_job_for_the_oauth_browser_question(tmp_path):
    """ADR-046 Open Question Q3. A hidden notepad, killed at once."""
    scratch = tmp_path / "dpc_job_probe_scratch.txt"
    scratch.write_text("probe", encoding="utf-8")
    script = tmp_path / "shell_child.py"
    script.write_text(SHELL_CHILD, encoding="utf-8")
    run = subprocess.run(
        [sys.executable, str(script), str(scratch)], capture_output=True, text=True,
        timeout=120, creationflags=CREATE_NO_WINDOW,
    )
    line = next((l for l in run.stdout.splitlines() if l.startswith("REPORT")), None)
    assert line, (run.stdout[-2000:], run.stderr[-2000:])
    report = json.loads(line[len("REPORT"):])
    print("ShellExecute under the job:", report)
    assert report["entered"]
    assert report["notepad_exe"]["ok"]


def _routes(monkeypatch, job_in_place, platform, launch):
    calls = {"fallback": [], "launch": []}
    monkeypatch.setattr(job_object, "_job_handle", 1234 if job_in_place else None)

    def spy_launch(url):
        calls["launch"].append(url)
        launch(url)

    road = job_object.open_url_outside_the_job(
        "https://hub.example/login/google",
        platform=platform, launch=spy_launch, fallback=calls["fallback"].append,
    )
    return road, calls


def _fine(url):
    pass


def _refused(url):
    raise OSError("explorer.exe will not start")


def test_with_the_job_in_place_on_windows_the_url_goes_through_explorer(monkeypatch):
    road, calls = _routes(monkeypatch, True, "win32", _fine)
    assert road == "explorer" and calls["launch"] and not calls["fallback"]


def test_without_the_job_the_backend_opens_the_url_itself(monkeypatch):
    road, calls = _routes(monkeypatch, False, "win32", _fine)
    assert road == "webbrowser" and not calls["launch"] and calls["fallback"]


def test_a_hand_off_that_cannot_start_falls_back_and_says_so(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="dpc_client_core.job_object"):
        road, calls = _routes(monkeypatch, True, "win32", _refused)
    assert road == "webbrowser" and calls["fallback"]
    assert any("explorer.exe" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_off_windows_the_url_is_opened_by_the_backend(monkeypatch, platform):
    road, calls = _routes(monkeypatch, True, platform, _fine)
    assert road == "webbrowser" and not calls["launch"] and calls["fallback"]


def test_the_oauth_login_opens_its_url_through_the_helper_and_not_webbrowser():
    source = (CORE_DIR / "dpc_client_core" / "hub_client.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    called = {
        n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")
        for n in ast.walk(tree) if isinstance(n, ast.Call)
    }
    assert "open_url_outside_the_job" in called
    assert "webbrowser" not in source


HANDOFF_CHILD = textwrap.dedent(
    r'''
    import ctypes, json, os, sys, time
    from ctypes import wintypes
    sys.path.insert(0, {core!r})
    from dpc_client_core import job_object
    import psutil
    report = {{"entered": job_object.enter_kill_on_close_job().entered}}
    k32 = job_object._load_kernel32()
    kn = ctypes.WinDLL("kernel32", use_last_error=True)
    kn.OpenProcess.restype = wintypes.HANDLE

    def in_job(pid):
        h = kn.OpenProcess(0x1000, False, pid)
        if not h:
            return None
        r = wintypes.BOOL()
        k32.IsProcessInJob(h, job_object._job_handle, ctypes.byref(r))
        kn.CloseHandle(h)
        return bool(r.value)

    def watch(launch, tag):
        target = os.path.join(sys.argv[1], tag + ".vbs")
        open(target, "w").write("WScript.Sleep 30000" + chr(13) + chr(10))
        before = {{p.pid for p in psutil.process_iter()}}
        row = {{"launched_by": launch(target)}}
        deadline = time.time() + 15
        while time.time() < deadline and "pid" not in row:
            for p in psutil.process_iter(["pid", "name", "cmdline", "ppid"]):
                if (p.pid not in before and tag + ".vbs" in " ".join(p.info["cmdline"] or [])
                        and p.info["name"].lower() == "wscript.exe"):
                    try:
                        parent = psutil.Process(p.info["ppid"]).name()
                    except psutil.Error:
                        parent = None
                    row.update(pid=p.pid, in_job=in_job(p.pid), parent=parent)
                    p.kill()
            time.sleep(0.2)
        return row

    def through_helper(target):
        return job_object.open_url_outside_the_job(target, fallback=os.startfile)

    def direct(target):
        os.startfile(target)
        return "startfile"

    report["helper"] = watch(through_helper, "via_helper")
    report["control"] = watch(direct, "direct")
    print("REPORT" + json.dumps(report))
    '''
).format(core=str(CORE_DIR))


@windows_only
@opt_in
def test_the_hand_off_starts_a_classic_target_outside_the_job_and_a_direct_start_does_not(tmp_path):
    """A .vbs run by wscript.exe stands in for the browser, so no window opens.
    The helper is given `os.startfile` as its fallback: if it took that road
    instead of explorer.exe, the target would land inside the job and this fails."""
    script = tmp_path / "handoff_child.py"
    script.write_text(HANDOFF_CHILD, encoding="utf-8")
    run = subprocess.run(
        [sys.executable, str(script), str(tmp_path)], capture_output=True, text=True,
        timeout=120, creationflags=CREATE_NO_WINDOW,
    )
    line = next((l for l in run.stdout.splitlines() if l.startswith("REPORT")), None)
    assert line, (run.stdout[-2000:], run.stderr[-2000:])
    report = json.loads(line[len("REPORT"):])
    print("explorer hand-off:", report)
    assert report["entered"]
    assert report["control"].get("in_job") is True, "the control must land inside the job"
    assert report["helper"]["launched_by"] == "explorer"
    assert report["helper"].get("in_job") is False, report["helper"]
