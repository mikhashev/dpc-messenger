"""ADR-046 point 3: the resource peaks and `last_output_at` of every child.

What is asserted: the sampler's arithmetic with injected readers (no process at
all), then the same against real children - a tree that holds memory for a few
seconds, a command shorter than the interval, output read as it arrives and
decoded exactly as text mode decoded it, the timeout path with a grandchild that
keeps the pipe open, an engine's log file, a pid a stranger could have reused.

Nothing here touches `~/.dpc`; persisted registries live in `tmp_path`. A test
kills only the processes it started.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from dpc_client_core import process_registry as pr
from dpc_client_core.dpc_agent.tools import process as process_mod
from dpc_client_core.dpc_agent.tools.process import run_supervised
from dpc_client_core.process_registry import ProcessRegistry, ProcessSample, SpawnOrigin, TreeSample

WINDOWS = sys.platform == "win32"
LINUX = sys.platform.startswith("linux")
START = 1_000.0  # the creation time every fake process of these tests has


def _fake_registry(**kwargs) -> ProcessRegistry:
    return ProcessRegistry(create_time=lambda pid: START, **kwargs)


def _tree(*processes, root_peak_rss=None, root_peak_commit=None, root_create_time=START):
    return TreeSample(root_create_time, list(processes), root_peak_rss, root_peak_commit)


def _proc(pid, rss=None, commit=None, cpu=None, read=None, write=None, create_time=START):
    return ProcessSample(pid, create_time, rss, commit, cpu, read, write)


def _sampler(registry, reading, **kwargs):
    """A sampler whose tree reader returns `reading[0]` (a list, so a test can swap it)."""
    kwargs.setdefault("probe", lambda pid, start: "same")
    kwargs.setdefault("read_vram", lambda: None)
    return pr.ResourceSampler(registry, read_tree=lambda pid: reading[0], **kwargs)


def _tool(registry, pid=7_100_001, **kwargs):
    return registry.register(pid, launcher="run_shell", launcher_kind="tool", command="x",
                             origin=SpawnOrigin(agent_id="a"), **kwargs)


def _wait(predicate, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def sampled():
    """The process's registry with a real sampler thread, stopped at the end."""
    registry = pr.get_registry()
    registry.start_sampler()
    yield registry
    registry.stop_sampler()


def _entry_of(registry, launcher):
    (entry,) = [e for e in registry.entries() if e.launcher == launcher]
    return entry


# ------------------------------------------------------------ the arithmetic, injected

class TestThePeaks:
    def test_the_tree_peak_is_the_sum_and_never_below_the_roots_own_peak(self):
        registry = _fake_registry()
        entry = _tool(registry)
        reading = [_tree(_proc(entry.pid, rss=50, commit=60), _proc(7_100_009, rss=80, commit=90),
                         root_peak_rss=200, root_peak_commit=210)]
        sampler = _sampler(registry, reading)
        sampler.tick()
        assert (entry.peak_rss_mb, entry.peak_commit_mb) == (200.0, 210.0), "the root's own peak is a floor"
        reading[0] = _tree(_proc(entry.pid, rss=150, commit=160), _proc(7_100_009, rss=180, commit=190))
        sampler.tick()
        assert (entry.peak_rss_mb, entry.peak_commit_mb) == (330.0, 350.0), "the tree's sum"
        reading[0] = _tree(_proc(entry.pid, rss=10, commit=10))
        sampler.tick()
        assert (entry.peak_rss_mb, entry.peak_commit_mb) == (330.0, 350.0), "a peak does not fall"

    def test_commit_stays_none_where_the_os_gives_none(self):
        registry = _fake_registry()
        entry = _tool(registry)
        sampler = _sampler(registry, [_tree(_proc(entry.pid, rss=50), root_peak_rss=70)])
        sampler.tick()
        assert entry.peak_rss_mb == 70.0 and entry.peak_commit_mb is None

    def test_cpu_and_io_do_not_fall_when_a_child_exits(self):
        registry = _fake_registry()
        entry = _tool(registry)
        reading = [_tree(_proc(entry.pid, cpu=1.0, read=100, write=10),
                         _proc(7_100_009, cpu=2.0, read=1000, write=20))]
        sampler = _sampler(registry, reading)
        sampler.tick()
        assert (entry.cpu_seconds, entry.io_read_bytes, entry.io_write_bytes) == (3.0, 1100, 30)
        reading[0] = _tree(_proc(entry.pid, cpu=1.5, read=150, write=15))  # the child is gone
        sampler.tick()
        assert (entry.cpu_seconds, entry.io_read_bytes, entry.io_write_bytes) == (3.5, 1150, 35)
        # A new process on the child's pid is a new process: its figures add up.
        reading[0] = _tree(_proc(entry.pid, cpu=1.5), _proc(7_100_009, cpu=0.5, create_time=START + 50))
        sampler.tick()
        assert entry.cpu_seconds == 4.0

    def test_io_stays_none_where_psutil_has_none(self):
        registry = _fake_registry()
        entry = _tool(registry)
        _sampler(registry, [_tree(_proc(entry.pid, rss=5, cpu=0.1))]).tick()
        assert entry.cpu_seconds == 0.1
        assert entry.io_read_bytes is None and entry.io_write_bytes is None

    def test_vram_is_read_for_an_engine_over_its_tree_and_never_for_a_command(self):
        registry = _fake_registry()
        engine = registry.register(7_100_101, launcher="strata", launcher_kind="subsystem", command="strata")
        command = _tool(registry, pid=7_100_102)
        calls = []

        def vram():
            calls.append(1)
            return {7_100_101: 1000.0, 7_100_103: 500.0, 7_100_102: 4000.0, 1234: 9999.0}

        trees = {7_100_101: _tree(_proc(7_100_101, rss=1), _proc(7_100_103, rss=1)),
                 7_100_102: _tree(_proc(7_100_102, rss=1))}
        sampler = pr.ResourceSampler(registry, probe=lambda pid, start: "same",
                                     read_tree=lambda pid: trees[pid], read_vram=vram)
        sampler.tick()
        assert engine.peak_vram_mib == 1500.0, "the engine and its child, not a stranger"
        assert command.peak_vram_mib is None
        sampler.tick([command.pid])
        assert len(calls) == 1, "a tick with no engine in it does not read the card"


class TestOnlyTheRecordedProcessIsSampled:
    def test_a_pid_whose_probe_says_other_is_not_sampled(self):
        registry = _fake_registry()
        entry = _tool(registry)
        sampler = _sampler(registry, [_tree(_proc(entry.pid, rss=500))], probe=lambda pid, start: "other")
        sampler.tick()
        assert entry.peak_rss_mb is None

    def test_a_walk_whose_root_changed_hands_after_the_probe_is_dropped(self):
        registry = _fake_registry()
        entry = _tool(registry)
        sampler = _sampler(registry, [_tree(_proc(entry.pid, rss=500), root_create_time=START + 3600)])
        sampler.tick()
        assert entry.peak_rss_mb is None

    def test_a_real_child_is_sampled_and_a_stranger_on_its_pid_is_not(self):
        """The real probe and the real walk: the same live process, recorded once with
        its own creation time and once with another (what a reused pid looks like)."""
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            own = ProcessRegistry()
            ours = own.register(child.pid, launcher="run_shell", launcher_kind="tool", command="sleep",
                                origin=SpawnOrigin(agent_id="a"))
            assert ours.start_time is not None
            pr.ResourceSampler(own, read_vram=lambda: None).tick()
            assert ours.peak_rss_mb and ours.peak_rss_mb > 0, "the control: the process is readable"

            stranger = ProcessRegistry(create_time=lambda pid: pr.create_time_of(pid) - 3600)
            theirs = stranger.register(child.pid, launcher="run_shell", launcher_kind="tool",
                                       command="sleep", origin=SpawnOrigin(agent_id="a"))
            pr.ResourceSampler(stranger, read_vram=lambda: None).tick()
            assert theirs.peak_rss_mb is None and theirs.cpu_seconds is None
        finally:
            child.kill()
            child.wait(timeout=10)


class TestFailuresAreSaidOnce:
    def test_a_reader_that_fails_leaves_none_and_warns_once(self, caplog):
        registry = _fake_registry()
        entry = _tool(registry)

        def broken(pid):
            raise RuntimeError("the walk broke")

        sampler = pr.ResourceSampler(registry, probe=lambda pid, start: "same", read_tree=broken,
                                     read_vram=lambda: None)
        with caplog.at_level(logging.WARNING, logger="dpc_client_core.process_registry"):
            sampler.tick()
            sampler.tick()
            sampler.tick()
        assert entry.peak_rss_mb is None
        warnings = [r for r in caplog.records if "resource sampler" in r.getMessage()]
        assert len(warnings) == 1 and "the walk broke" in warnings[0].getMessage()

    def test_with_psutil_missing_every_figure_is_none_after_one_warning(self, monkeypatch, caplog):
        registry = _fake_registry()
        entry = _tool(registry)
        monkeypatch.setitem(sys.modules, "psutil", None)  # `import psutil` raises ImportError
        sampler = pr.ResourceSampler(registry, probe=lambda pid, start: "same", read_vram=lambda: None)
        with caplog.at_level(logging.WARNING, logger="dpc_client_core.process_registry"):
            sampler.tick()
            sampler.tick()
        for name in ("peak_rss_mb", "peak_commit_mb", "peak_vram_mib", "cpu_seconds",
                     "io_read_bytes", "io_write_bytes"):
            assert getattr(entry, name) is None
        warnings = [r.getMessage() for r in caplog.records if "resource sampler" in r.getMessage()]
        assert len(warnings) == 1 and "psutil" in warnings[0]


# ------------------------------------------------------------ the thread

class TestTheThread:
    def test_a_new_entry_is_sampled_at_once_not_at_the_next_tick(self):
        registry = _fake_registry()
        seen = []

        def read(pid):
            seen.append(pid)
            return _tree(_proc(pid, rss=5))

        registry.start_sampler(interval_s=60.0, probe=lambda pid, start: "same", read_tree=read,
                               read_vram=lambda: None)
        try:
            time.sleep(0.2)  # past the thread's first tick, into its 60 s sleep
            entry = _tool(registry)
            assert _wait(lambda: entry.pid in seen, 3.0), "the registration woke no sample"
            assert entry.peak_rss_mb == 5.0
        finally:
            registry.stop_sampler()

    def test_a_new_entry_gets_its_early_samples_then_the_regular_tick(self):
        registry = _fake_registry()
        seen = []

        def read(pid):
            seen.append(time.monotonic())
            return _tree(_proc(pid, rss=5))

        registry.start_sampler(interval_s=60.0, early_samples_s=(0.0, 0.1, 0.3), probe=lambda pid, start: "same",
                               read_tree=read, read_vram=lambda: None)
        try:
            time.sleep(0.2)
            _tool(registry)
            assert _wait(lambda: len(seen) >= 3, 3.0), f"early samples: {len(seen)}"
            time.sleep(0.5)
            assert len(seen) == 3, "after the early samples it waits for the regular tick"
        finally:
            registry.stop_sampler()

    def test_the_sampler_stops_at_finish_shutdown(self):
        registry = _fake_registry()
        sampler = registry.start_sampler(probe=lambda pid, start: "gone", read_vram=lambda: None)
        assert _wait(lambda: sampler.running, 2.0)
        registry.begin_shutdown()
        registry.finish_shutdown(kill=lambda pid: "not called", probe=lambda pid, start: "gone")
        assert not sampler.running and registry._sampler is None

    def test_a_registry_that_never_started_one_stops_without_it(self):
        registry = ProcessRegistry()
        registry.stop_sampler()
        registry.begin_shutdown()
        assert registry.finish_shutdown() == []


# ------------------------------------------------------------ the throttled write

class TestThePeaksReachTheFileWhenTheyRiseEnough:
    def test_a_rise_of_a_quarter_is_written_a_small_one_is_not_and_never_twice_in_the_throttle(self, tmp_path, monkeypatch):
        path = tmp_path / "process_registry.json"
        registry = _fake_registry(path=path)
        entry = _tool(registry)
        clock = [0.0]
        reading = [None]
        sampler = _sampler(registry, reading, monotonic=lambda: clock[0])
        writes = []
        original = registry._persist_locked
        monkeypatch.setattr(registry, "_persist_locked", lambda: (writes.append(clock[0]), original())[1])

        def tick_at(t, rss):
            clock[0] = t
            reading[0] = _tree(_proc(entry.pid, rss=rss))
            sampler.tick()

        tick_at(0.0, 50)       # from nothing to 50 MB: under the 64 MB floor
        assert writes == []
        tick_at(2.0, 200)      # +200 MB, far above a quarter
        assert writes == [2.0]
        assert json.loads(path.read_text(encoding="utf-8"))["entries"][0]["peak_rss_mb"] == 200.0
        tick_at(4.0, 400)      # a rise, inside the throttle: not yet
        assert writes == [2.0]
        tick_at(12.5, 400)     # the throttle passed: the deferred rise is written
        assert writes == [2.0, 12.5]
        assert json.loads(path.read_text(encoding="utf-8"))["entries"][0]["peak_rss_mb"] == 400.0
        tick_at(30.0, 460)     # +60 MB and +15 %: neither floor
        tick_at(45.0, 470)
        assert writes == [2.0, 12.5]

    def test_a_spawn_write_resets_the_base(self, tmp_path):
        registry = _fake_registry(path=tmp_path / "process_registry.json")
        entry = _tool(registry)
        entry.peak_rss_mb = 900.0
        _tool(registry, pid=7_100_002)  # a spawn rewrites the file, peaks included
        assert registry._live[entry.pid].written["peak_rss_mb"] == 900.0

    def test_an_in_memory_registry_writes_nothing(self, monkeypatch):
        registry = _fake_registry()
        entry = _tool(registry)
        writes = []
        monkeypatch.setattr(registry, "_persist_locked", lambda: writes.append(1))
        _sampler(registry, [_tree(_proc(entry.pid, rss=5000))]).tick()
        assert entry.peak_rss_mb == 5000.0 and writes == []


# ------------------------------------------------------------ real children through run_supervised

HOLDER = textwrap.dedent("""
    import time
    b = b"x" * (150 * 1024 * 1024)   # filled, so the pages are resident
    time.sleep(3)
    print(len(b))
""")


def test_a_child_that_holds_150_mb_for_three_seconds_has_a_peak_of_at_least_100(sampled):
    run = run_supervised([sys.executable, "-c", HOLDER], launcher="test(holder)", timeout=60, ceiling_mb=0)
    assert run.returncode == 0, run.stderr
    entry = _entry_of(sampled, "test(holder)")
    assert entry.peak_rss_mb is not None and entry.peak_rss_mb >= 100, entry
    if WINDOWS:
        assert entry.peak_commit_mb is not None and entry.peak_commit_mb >= 100, entry
    else:
        assert entry.peak_commit_mb is None, "POSIX has no cheap commit figure"
    assert entry.cpu_seconds is not None and entry.cpu_seconds >= 0


def test_a_command_shorter_than_the_interval_still_gets_a_reading(sampled):
    """What a 0.2 s command gets, per platform (the promise in the module docstring):
    Windows - the early samples and the child's own lifetime figures through its
    handle, so RSS, commit, CPU and IO; Linux - the early samples, so RSS, CPU and
    IO, no commit; macOS - the early samples, so RSS and CPU, no commit, no IO."""
    run = run_supervised([sys.executable, "-c", "import time; time.sleep(0.2)"],
                         launcher="test(short)", timeout=30, ceiling_mb=0)
    assert run.returncode == 0, run.stderr
    entry = _entry_of(sampled, "test(short)")
    assert entry.peak_rss_mb is not None and entry.peak_rss_mb > 0, entry
    assert entry.cpu_seconds is not None, entry
    if WINDOWS:
        assert entry.peak_commit_mb is not None and entry.peak_commit_mb > 0, entry
    else:
        assert entry.peak_commit_mb is None
    if WINDOWS or LINUX:
        assert entry.io_read_bytes is not None and entry.io_write_bytes is not None, entry
    else:
        assert entry.io_read_bytes is None


def test_the_time_of_the_last_output_is_after_the_second_print_and_the_text_is_unchanged():
    body = "import time; print('first', flush=True); time.sleep(2.5); print('second', flush=True)"
    before = time.time()
    run = run_supervised([sys.executable, "-c", body], launcher="test(output)", timeout=30, ceiling_mb=0)
    assert run.stdout == "first\nsecond\n" and run.stderr == ""
    entry = _entry_of(pr.get_registry(), "test(output)")
    assert entry.last_output_at is not None, "nothing noted the output"
    at = pr._epoch(entry.last_output_at)
    assert at >= before + 2.0, f"the last output is the second print, 2.5 s in: {at - before:.2f} s"
    assert at <= pr._epoch(entry.ended_at)


ODD_OUTPUT = textwrap.dedent(r"""
    import sys, time
    out = sys.stdout.buffer
    out.write(b"a\r\nb\rc\n\xff\xfe bad \xe2")   # CRLF, a lone CR, invalid bytes, half a euro sign
    out.flush()
    time.sleep(0.3)
    out.write(b"\x82\xac end\r")                  # the euro's other half, in a later read
    out.flush()
    sys.stderr.buffer.write(b"err\r\n\xc3")
""")


def test_output_with_crlf_a_lone_cr_and_invalid_bytes_comes_back_as_text_mode_gave_it():
    cmd = [sys.executable, "-c", ODD_OUTPUT]
    old = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                         stdin=subprocess.DEVNULL, timeout=30)
    run = run_supervised(cmd, launcher="test(odd)", timeout=30, ceiling_mb=0)
    assert (run.stdout, run.stderr) == (old.stdout, old.stderr)
    replacement, euro = chr(0xFFFD), chr(0x20AC)
    assert run.stdout == f"a\nb\nc\n{replacement * 2} bad {euro} end\n", "the euro split across reads stays whole"
    assert run.stderr == f"err\n{replacement}"


def test_the_timeout_returns_within_its_bound_when_a_grandchild_holds_the_pipe(tmp_path, monkeypatch):
    """The child starts a grandchild that inherits stdout and sleeps, and the tree
    kill is made to miss it: the pipes never reach their end, and the call still
    returns within the timeout plus the bounded drain."""
    marker = tmp_path / "grandchild.pid"
    body = textwrap.dedent(f"""
        import subprocess, sys, time
        g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        open({str(marker)!r}, "w").write(str(g.pid))
        print("started", flush=True)
        time.sleep(60)
    """)
    real_kill_tree = process_mod._kill_process_tree  # the cleanup below must really kill
    monkeypatch.setattr(process_mod, "_kill_process_tree", lambda process: "the kill was made to miss")
    started = time.monotonic()
    try:
        run = run_supervised([sys.executable, "-c", body], launcher="test(held)", timeout=3, ceiling_mb=0)
        elapsed = time.monotonic() - started
        assert run.timed_out and run.killed == "the kill was made to miss"
        assert elapsed < 3 + process_mod._DRAIN_AFTER_KILL_SECONDS + 5, f"{elapsed:.1f} s"
        assert (run.stdout, run.stderr) == ("", ""), "an abandoned drain reports nothing, as before"
        assert _entry_of(pr.get_registry(), "test(held)").exit_reason == "timeout"
    finally:
        # The processes this test started, the grandchild first while it surely lives.
        pids = [int(marker.read_text())] if marker.exists() else []
        pids += [e.pid for e in pr.get_registry().entries() if e.launcher == "test(held)"]
        for pid in pids:
            # Not `kill_pid_tree`: it would call the patched miss above and kill nothing.
            real_kill_tree(process_mod._PidHandle(pid))


# ------------------------------------------------------------ engines: the log file's mtime

def test_an_engines_last_output_is_its_logs_mtime_from_its_start_on(tmp_path):
    log = tmp_path / "engine.log"
    log.write_text("old run\n")
    registry = _fake_registry()
    entry = registry.register(7_100_201, launcher="llamacpp_server", launcher_kind="subsystem",
                              command="llama-server", log_paths=[log])
    sampler = pr.ResourceSampler(registry, probe=lambda pid, start: "gone", read_vram=lambda: None)

    old = time.time() - 3600
    os.utime(log, (old, old))
    sampler.tick()
    assert entry.last_output_at is None, "the last run's mtime is not this process's output"

    written = time.time() + 5
    os.utime(log, (written, written))
    sampler.tick()
    assert entry.last_output_at == pr._iso(written)


@pytest.mark.asyncio
async def test_a_strata_entry_reads_the_newer_of_the_launchers_and_the_engines_log(tmp_path, monkeypatch):
    from tests.test_the_process_registry_records_how_every_child_ends import _FakeChild, _exec_returning, _strata

    sup = _strata(tmp_path, monkeypatch)
    _exec_returning(monkeypatch, _FakeChild())
    proc = await sup._spawn(["strata"], {})
    registry = pr.get_registry()
    assert registry._live[proc.pid].log_paths == (sup._log_path, sup._engine_log_path)
    for path in (sup._log_path, sup._engine_log_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    now = time.time()
    os.utime(sup._log_path, (now + 5, now + 5))
    os.utime(sup._engine_log_path, (now + 9, now + 9))
    pr.ResourceSampler(registry, probe=lambda pid, start: "gone", read_vram=lambda: None).tick()
    (entry,) = registry.open_entries("subsystem")
    assert entry.last_output_at == pr._iso(now + 9)


@pytest.mark.asyncio
async def test_a_llama_server_entry_reads_its_log(tmp_path, monkeypatch):
    from tests.test_the_process_registry_records_how_every_child_ends import _FakeChild, _exec_returning, _llama

    sup = _llama(tmp_path, monkeypatch)
    _exec_returning(monkeypatch, _FakeChild())
    proc = await sup._spawn(["llama-server"], {})
    assert pr.get_registry()._live[proc.pid].log_paths == (sup._log_path,)


# ------------------------------------------------------------ the VRAM readers

@pytest.mark.skipif(not WINDOWS, reason="the PDH counter is a Windows object")
def test_the_windows_gpu_counter_reader_returns_a_mapping_without_raising():
    started = time.perf_counter()
    reading = pr.read_vram_windows()
    elapsed = time.perf_counter() - started
    assert reading is None or isinstance(reading, dict)
    if reading:
        assert all(isinstance(pid, int) and mib >= 0 for pid, mib in reading.items())
    assert elapsed < 5.0, f"one reading took {elapsed:.1f} s"


def test_the_nvml_reader_returns_none_when_the_library_is_absent():
    assert pr.read_vram_nvml("libnvidia-ml-that-is-not-here.so.0") is None


def test_macos_has_no_vram_reader():
    assert pr._default_vram_reader("darwin")() is None
