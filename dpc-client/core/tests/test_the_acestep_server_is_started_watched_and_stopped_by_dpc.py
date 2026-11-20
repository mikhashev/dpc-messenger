"""ADR-051 D30 (SPEC §12 decision 31): DPC supervises the acestep.cpp server.

A missing binary or model file is `not_installed` and says which; a start reaches a
healthy server on 127.0.0.1 and enters the child in the process registry; two starts make
one child; a stop asks first and the child goes; a child that exits on its own is an
`error` until a start succeeds; a start that fails says why with the log tail; the idle
timer frees the card. The server is `_fake_acestep_server.py`, a real child process, so
the process handling runs on Windows and on Linux CI alike.
"""

import asyncio
import socket
import sys

import pytest

from dpc_client_core import process_registry
from dpc_client_core.music import acestep
from dpc_client_core.music.acestep import BINARY_NAME, LOOPBACK, MAX_BATCH, find_binary, read_config
from dpc_client_core.music.capabilities import MusicCapabilities
from tests._acestep_helpers import free_env, install_fake_engine, make_engine, wait_until


def test_a_missing_binary_or_model_is_not_installed_and_named(tmp_path):
    home = tmp_path / "dpc"
    engine = make_engine(home)
    status = engine.status()
    assert status.state == "not_installed" and not status.available
    assert BINARY_NAME in status.reason
    assert "acestep-5Hz-lm-4B-Q8_0.gguf" in status.reason
    if sys.platform != "win32":
        assert "build recipe for Windows only" in status.reason

    install_fake_engine(home, models=("acestep-5Hz-lm-4B-Q8_0.gguf", "vae-BF16.gguf"))
    status = make_engine(home).status()
    assert status.state == "not_installed"
    assert "binary is missing" not in status.reason
    assert "acestep-v15-turbo-Q8_0.gguf" in status.reason and "Qwen3-Embedding-0.6B-Q8_0.gguf" in status.reason
    assert "vae-BF16.gguf" not in status.reason


def test_an_installed_engine_is_stopped_and_available_and_jobs_can_queue(tmp_path):
    home = tmp_path / "dpc"
    install_fake_engine(home)
    engine = make_engine(home)
    status = engine.status()
    assert status.state == "stopped" and status.available and status.supervised
    assert status.version == "fake0000 (2026-11-21)"  # from version.h before any start
    assert status.models["dit"] == "acestep-v15-turbo-Q8_0.gguf"
    report = MusicCapabilities(engine.status).report()
    assert report["job_types"]["generate_from_text"]["status"] == "supported"


def test_the_binary_is_found_a_few_folders_down_and_settings_have_defaults(tmp_path):
    home = tmp_path / "dpc"
    nested = home / "engines" / "acestep.cpp" / "build" / "bin" / "Release"
    nested.mkdir(parents=True)
    (nested / BINARY_NAME).write_bytes(b"x")
    assert find_binary(home / "engines" / "acestep.cpp" / "build") == nested / BINARY_NAME

    config = read_config(lambda s, k, f=None: f, home)
    assert config.binary == nested / BINARY_NAME
    assert config.models_dir == home / "models" / "music" / "ace-step"
    assert config.port == 9995 and config.idle_stop_s == 300.0 and config.device is None
    assert config.log_path == home / "logs" / "acestep-server.log"

    bad = {"acestep_port": "99999", "acestep_idle_stop_s": "-1", "acestep_binary": str(tmp_path / "x" / "srv")}
    config = read_config(lambda s, k, f=None: bad.get(k, f), home)
    assert config.port == 9995 and config.idle_stop_s == 300.0
    assert config.binary == tmp_path / "x" / "srv"


def test_the_command_binds_loopback_and_names_the_models_folder(tmp_path):
    home = tmp_path / "dpc"
    engine = acestep.AceStepServer(read_config(lambda s, k, f=None: {"acestep_device": "CUDA0"}.get(k, f), home),
                                   dpc_home=home)
    cmd = engine.build_command(9995)
    assert cmd[cmd.index("--host") + 1] == LOOPBACK
    assert cmd[cmd.index("--port") + 1] == "9995"
    assert cmd[cmd.index("--models") + 1] == str(home / "models" / "music" / "ace-step")
    assert cmd[cmd.index("--max-batch") + 1] == str(MAX_BATCH)
    assert cmd[cmd.index("--device") + 1] == "CUDA0"
    assert "0.0.0.0" not in cmd


@pytest.mark.asyncio
async def test_start_reaches_a_healthy_server_once_and_stop_ends_it(tmp_path, monkeypatch):
    free_env(monkeypatch)
    home = tmp_path / "dpc"
    install_fake_engine(home)
    changes = []
    engine = make_engine(home, on_change=changes.append)
    try:
        await asyncio.gather(engine.start(), engine.start())
        status = engine.status()
        assert status.state == "running", status.reason
        pid = engine.details()["pid"]
        assert isinstance(pid, int)
        assert engine.version == "fake0000 (2026-11-21)"  # from /props
        await engine.start()  # running: no second child
        assert engine.details()["pid"] == pid
        entries = [e for e in process_registry.get_registry().open_entries() if e.launcher == "acestep_server"]
        assert [e.pid for e in entries] == [pid]
        assert "starting" in [c.state for c in changes] and "running" in [c.state for c in changes]
        proc = engine._proc

        assert await engine.stop() is True
        assert engine.status().state == "stopped"
        assert proc.returncode is not None
        assert not [e for e in process_registry.get_registry().open_entries() if e.launcher == "acestep_server"]
        if sys.platform != "win32":
            assert proc.returncode == 0  # SIGTERM was handled: a graceful stop
            assert "graceful stop" in engine.tail_log()
        assert await engine.stop() is False
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_a_child_that_exits_on_its_own_is_an_error_until_a_start_succeeds(tmp_path, monkeypatch):
    free_env(monkeypatch)
    home = tmp_path / "dpc"
    install_fake_engine(home)
    engine = make_engine(home)
    try:
        await engine.start()
        engine._proc.kill()
        await wait_until(lambda: engine.state == "error")
        status = engine.status()
        assert status.state == "error" and not status.available
        assert "exited on its own" in status.reason
        assert engine.details()["last_exit"]["why"] == "exited on its own"
        assert MusicCapabilities(engine.status).unsupported_reason("generate_from_text") == status.reason
        await asyncio.sleep(0.3)
        assert engine.state == "error"  # nothing restarts it by itself
        await engine.start()
        assert engine.status().state == "running"
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_a_start_that_fails_is_an_error_with_the_log_tail(tmp_path, monkeypatch):
    free_env(monkeypatch, "exit_at_start")
    home = tmp_path / "dpc"
    install_fake_engine(home)
    engine = make_engine(home)
    with pytest.raises(acestep.AceStepError) as err:
        await engine.start()
    assert err.value.code == "start_failed"
    status = engine.status()
    assert status.state == "error"
    assert "exit_at_start" in status.reason  # the child's own last log line
    assert not engine.alive()
    assert not process_registry.get_registry().open_entries()


@pytest.mark.asyncio
async def test_a_server_without_the_turbo_model_is_refused(tmp_path, monkeypatch):
    free_env(monkeypatch, "missing_model")
    home = tmp_path / "dpc"
    install_fake_engine(home)
    engine = make_engine(home)
    with pytest.raises(acestep.AceStepError) as err:
        await engine.start()
    assert "acestep-v15-turbo-Q8_0.gguf" in err.value.message
    assert not engine.alive()


@pytest.mark.asyncio
async def test_a_taken_port_is_refused_before_anything_starts(tmp_path, monkeypatch):
    free_env(monkeypatch)
    home = tmp_path / "dpc"
    install_fake_engine(home)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((LOOPBACK, 0))
        s.listen(1)
        port = s.getsockname()[1]
        engine = make_engine(home, settings={"acestep_port": str(port)})
        with pytest.raises(acestep.AceStepError) as err:
            await engine.start()
    assert err.value.code == "port_taken"
    assert engine.status().state == "error" and str(port) in engine.status().reason
    assert not process_registry.get_registry().open_entries()


@pytest.mark.asyncio
async def test_a_not_installed_engine_refuses_to_start(tmp_path):
    engine = make_engine(tmp_path / "dpc")
    with pytest.raises(acestep.AceStepError) as err:
        await engine.start()
    assert err.value.code == "not_installed"


@pytest.mark.asyncio
async def test_the_idle_timer_stops_a_server_nobody_uses(tmp_path, monkeypatch):
    free_env(monkeypatch)
    home = tmp_path / "dpc"
    install_fake_engine(home)
    engine = make_engine(home, settings={"acestep_idle_stop_s": "0.3"})
    try:
        await engine.start()
        async with engine.in_use():
            await asyncio.sleep(0.6)
            assert engine.state == "running"  # busy: the timer waits
        await wait_until(lambda: engine.state == "stopped", timeout=10)
        assert "idle_unload" in engine.status().reason
    finally:
        await engine.shutdown()


def test_identity_names_files_with_sizes_and_hashes_but_no_paths(tmp_path):
    home = tmp_path / "dpc"
    install_fake_engine(home)
    engine = make_engine(home)
    ident = engine.identity()
    assert ident["engine"] == "acestep.cpp" and ident["version"] == "fake0000 (2026-11-21)"
    assert ident["binaries"][0]["file"] == BINARY_NAME
    assert all(b["sha256"].startswith("sha256:") for b in ident["binaries"])
    assert any("ggml" in b["file"] for b in ident["binaries"])
    assert ident["models"]["dit"]["file"] == "acestep-v15-turbo-Q8_0.gguf"
    assert ident["models"]["dit"]["bytes"] == len(b"acestep-v15-turbo-Q8_0.gguf")
    assert str(tmp_path) not in repr(ident)
    cache = home / "music" / "acestep_identity_cache.json"
    assert cache.is_file()
    calls = []
    monkeypatch_sha = acestep._sha256_file
    acestep._sha256_file = lambda p: calls.append(p) or monkeypatch_sha(p)
    try:
        assert make_engine(home).identity() == ident
        assert calls == []  # every hash came from the cache
    finally:
        acestep._sha256_file = monkeypatch_sha
