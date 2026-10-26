"""The device context is refreshed in the background at every start.

The constructor reads what the last collection left, `start()` launches the
refresh as a tracked background task, and the new context replaces the old one
when it lands; until then readers get the previous file, and None on a first
run. A refresh that fails or is overtaken by the stop keeps the previous
collection in memory and on disk, and says so once.

Nothing here touches the real ~/.dpc: the constructor runs in a child process
whose home is tmp_path, and the rest drives a bare service writing into tmp_path.
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from dpc_client_core import device_context_collector as collector_mod
from dpc_client_core.device_context_collector import DeviceContextCollector
from dpc_client_core.process_registry import ProcessRegistryClosed
from dpc_client_core.service import CoreService

PREVIOUS = {"schema_version": "1.1", "created_at": "2026-10-01T00:00:00+00:00",
            "hardware": {"gpu": {"model": "previous card"}}}
COLLECTED = {"schema_version": "1.1", "created_at": "2026-10-01T00:00:00+00:00",
             "hardware": {"gpu": {"model": "collected card"}}}
SLOW_S = 2.0
CORE_DIR = Path(__file__).resolve().parents[1]


class _Sentinel(Exception):
    """Raised by the first step of start() after the refresh is launched."""


class _SlowCollector(DeviceContextCollector):
    """The real collector's save path with the probing replaced by a wait."""

    def __init__(self, device_file, stop_event, seconds=SLOW_S, result=None, fail=None):
        super().__init__(settings=None, stop_event=stop_event)
        self.device_file = device_file
        self.seconds = seconds
        self.result = COLLECTED if result is None else result
        self.fail = fail

    def _generate_device_context(self):
        time.sleep(self.seconds)
        if self.fail is not None:
            raise self.fail
        return dict(self.result)


class _ProbingCollector(DeviceContextCollector):
    """Probes like the real one: through `_probe`, each failure swallowed at
    the probe site."""

    def __init__(self, device_file, stop_event, probes=20):
        super().__init__(settings=None, stop_event=stop_event)
        self.device_file = device_file
        self.probes = probes
        self.first_probe_ran = threading.Event()

    def _generate_device_context(self):
        found = {}
        for i in range(self.probes):
            try:
                self._probe(["tool", str(i)], text=True, timeout=2)
                found[str(i)] = "installed"
            except Exception:
                pass
            self.first_probe_ran.set()
        return {"schema_version": "1.1", "software": {"dev_tools": found}}


class _Pcm:
    def __init__(self):
        self.saved = []

    def load_context(self):
        return SimpleNamespace(metadata={}, knowledge={})

    def save_context(self, context):
        self.saved.append(context)


def _bare_service(previous, collector):
    svc = CoreService.__new__(CoreService)
    svc._is_running = False
    svc.settings = SimpleNamespace(get_auto_collect_device_info=lambda: True)
    svc._background_tasks = set()
    svc._device_context_stop = collector._stop_event
    svc.device_context = previous
    svc.pcm_core = _Pcm()
    svc._device_context_collector = lambda: collector
    return svc


def _refresh_task(svc):
    (task,) = [t for t in svc._background_tasks if t.get_name() == "device_context_refresh"]
    return task


def _write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


async def _run_start_until_refresh_launched(svc, monkeypatch):
    """Run the real start() up to its first step after the refresh launch."""
    from dpc_client_core.managers import llama_server_supervisor

    def stop_here(loop):
        raise _Sentinel()

    monkeypatch.setattr(llama_server_supervisor, "bind_owner_loop", stop_here)
    began = time.monotonic()
    with pytest.raises(_Sentinel):
        await svc.start()
    return time.monotonic() - began


def _warnings(caplog):
    return [r for r in caplog.records
            if r.levelno == logging.WARNING and "Device context" in r.getMessage()]


# --- the constructor: reads, never collects ------------------------------------

_CONSTRUCT = textwrap.dedent("""
    import json, sys, time
    from pathlib import Path
    from unittest.mock import patch
    from dpc_client_core import service as svc_mod
    from dpc_client_core.device_context_collector import DeviceContextCollector

    marker = Path(sys.argv[1])

    def slow_generate(self):
        marker.write_text("collected")
        time.sleep(%(slow)s)
        return {"collected": True}

    with patch.object(DeviceContextCollector, "_generate_device_context", slow_generate), \\
         patch.object(svc_mod, "P2PManager"), patch.object(svc_mod, "HubClient"), \\
         patch.object(svc_mod, "LLMManager"), patch.object(svc_mod, "LocalApiServer"):
        began = time.monotonic()
        service = svc_mod.CoreService(skip_knowledge_index=True)
        elapsed = time.monotonic() - began
    print(json.dumps({"home": str(svc_mod.DPC_HOME_DIR), "elapsed": elapsed,
                      "collected": marker.exists(), "device_context": service.device_context}))
""" % {"slow": SLOW_S})


@pytest.mark.parametrize("previous", [PREVIOUS, None], ids=["previous_file", "first_run"])
def test_the_constructor_reads_the_previous_file_and_collects_nothing(tmp_path, previous):
    home = tmp_path / "home"
    dpc = home / ".dpc"
    dpc.mkdir(parents=True)
    if previous is not None:
        _write(dpc / "device_context.json", previous)
    marker = tmp_path / "collected.marker"
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), DPC_HOME=str(dpc))
    run = subprocess.run(
        [sys.executable, "-c", _CONSTRUCT, str(marker)],
        cwd=CORE_DIR, env=env, capture_output=True, text=True, timeout=180,
    )
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])
    assert Path(out["home"]) == dpc  # the child never saw the real ~/.dpc
    assert out["collected"] is False
    assert out["elapsed"] < SLOW_S
    assert out["device_context"] == previous
    if previous is not None:
        assert json.loads((dpc / "device_context.json").read_text(encoding="utf-8")) == previous


# --- start(): launches, does not wait ------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("previous", [PREVIOUS, None], ids=["previous_file", "first_run"])
async def test_the_start_does_not_wait_and_the_previous_context_serves_until_the_new_lands(
        tmp_path, monkeypatch, caplog, previous):
    device_file = tmp_path / "device_context.json"
    if previous is not None:
        _write(device_file, previous)
    collector = _SlowCollector(device_file, threading.Event())
    svc = _bare_service(previous, collector)

    with caplog.at_level(logging.INFO, logger="dpc_client_core"):
        elapsed = await _run_start_until_refresh_launched(svc, monkeypatch)
        assert elapsed < 0.5, f"start() waited {elapsed:.2f}s for the collection"

        task = _refresh_task(svc)
        await asyncio.sleep(0.3)
        assert not task.done()
        assert svc.device_context == previous

        await asyncio.wait_for(task, timeout=SLOW_S + 10)

    assert svc.device_context == COLLECTED
    assert json.loads(device_file.read_text(encoding="utf-8")) == COLLECTED
    (saved,) = svc.pcm_core.saved
    assert saved.metadata["external_contexts"]["device_context"]["file"] == "device_context.json"
    landed = [r.getMessage() for r in caplog.records
              if r.getMessage().startswith("Device context refreshed in the background in ")]
    assert len(landed) == 1 and landed[0].endswith(" s")
    assert not _warnings(caplog)
    assert task not in svc._background_tasks


@pytest.mark.asyncio
async def test_the_refresh_is_not_launched_when_collection_is_off(tmp_path, monkeypatch):
    collector = _SlowCollector(tmp_path / "device_context.json", threading.Event())
    svc = _bare_service(PREVIOUS, collector)
    svc.settings = SimpleNamespace(get_auto_collect_device_info=lambda: False)
    await _run_start_until_refresh_launched(svc, monkeypatch)
    assert not svc._background_tasks
    assert not (tmp_path / "device_context.json").exists()


# --- failures keep the previous collection -------------------------------------

@pytest.mark.asyncio
async def test_a_collection_that_raises_keeps_the_previous_context_and_warns_once(tmp_path, caplog):
    device_file = tmp_path / "device_context.json"
    _write(device_file, PREVIOUS)
    before = device_file.read_bytes()
    collector = _SlowCollector(device_file, threading.Event(), seconds=0.05,
                               fail=RuntimeError("probe exploded"))
    svc = _bare_service(PREVIOUS, collector)

    with caplog.at_level(logging.INFO, logger="dpc_client_core"):
        svc._start_device_context_refresh()
        await asyncio.wait_for(_refresh_task(svc), timeout=10)

    assert svc.device_context == PREVIOUS
    assert device_file.read_bytes() == before
    assert not svc.pcm_core.saved
    (warning,) = _warnings(caplog)
    assert "probe exploded" in warning.getMessage()


def _probe_answers(cmd, **kwargs):
    time.sleep(0.05)
    return subprocess.CompletedProcess(cmd, 0, "tool 1.2\n", "")


def _probe_refused(cmd, **kwargs):
    raise ProcessRegistryClosed("the backend is stopping; it starts no new process")


@pytest.mark.asyncio
@pytest.mark.parametrize("probe", [_probe_answers, _probe_refused],
                         ids=["stop_event_set_mid_run", "registry_refuses_the_probes"])
async def test_a_stop_during_the_collection_abandons_it_and_writes_nothing(
        tmp_path, monkeypatch, caplog, probe):
    monkeypatch.setattr(collector_mod, "_device_probe", probe)
    device_file = tmp_path / "device_context.json"
    _write(device_file, PREVIOUS)
    before = device_file.read_bytes()
    collector = _ProbingCollector(device_file, threading.Event())
    svc = _bare_service(PREVIOUS, collector)

    with caplog.at_level(logging.INFO, logger="dpc_client_core"):
        svc._start_device_context_refresh()
        task = _refresh_task(svc)
        assert await asyncio.to_thread(collector.first_probe_ran.wait, 10)

        # What shutdown() does: set the stop, then cancel and gather the tasks.
        began = time.monotonic()
        svc._device_context_stop.set()
        task.cancel()
        results = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError)
        assert time.monotonic() - began < 1.0

        # The worker thread ends on its own, its remaining probes skipped.
        deadline = time.monotonic() + 10
        while not _warnings(caplog) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

    assert collector._stopped
    assert device_file.read_bytes() == before
    assert svc.device_context == PREVIOUS
    assert not svc.pcm_core.saved
    (warning,) = _warnings(caplog)
    assert "the backend is stopping" in warning.getMessage()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not list(tmp_path.glob("*.tmp"))


# --- the write is atomic -------------------------------------------------------

def test_the_file_is_replaced_whole_and_a_failed_replace_leaves_the_previous_one(tmp_path, monkeypatch):
    device_file = tmp_path / "device_context.json"
    _write(device_file, PREVIOUS)
    collector = _SlowCollector(device_file, None, seconds=0)
    real_replace = os.replace
    seen = []

    def watching_replace(src, dst):
        if Path(dst) == device_file:
            # Until the swap the reader's file is the previous one, whole.
            seen.append((json.loads(Path(dst).read_text(encoding="utf-8")),
                         json.loads(Path(src).read_text(encoding="utf-8"))))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", watching_replace)
    collector.collect_and_save()
    assert seen == [(PREVIOUS, COLLECTED)]
    assert json.loads(device_file.read_text(encoding="utf-8")) == COLLECTED

    _write(device_file, PREVIOUS)
    before = device_file.read_bytes()

    def failing_replace(src, dst):
        raise OSError("disk went away")

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(OSError):
        collector.collect_and_save()
    assert device_file.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["device_context.json"]
