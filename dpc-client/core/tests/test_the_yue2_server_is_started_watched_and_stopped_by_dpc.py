"""ADR-051 D35 (SPEC §12 decision 40): DPC supervises the yue2.cpp server, the default music
engine.

A missing binary, backbone or VAE is `not_installed` and says which; a start reaches a
healthy server on 127.0.0.1 with batch 1 and enters it in the process registry under
`yue2_server`; two starts make one child; another backbone restarts it; a server that
loaded the wrong backbone is refused. The voice adapters are split into a music (AR) and a
sound (NAR) half with the tensor bytes unchanged, into a DPC-owned cache keyed by the
source's sha256. The server is `_fake_yue2_server.py`, a real child process.
"""

import asyncio
import hashlib
import json
import os
import sys

import pytest

from dpc_client_core import process_registry
from dpc_client_core.music.capabilities import MusicCapabilities
from dpc_client_core.music.engine_base import MusicEngineError
from dpc_client_core.music.yue2 import (
    BINARY_NAME, LOOPBACK, MAX_BATCH, SPLIT_MANIFEST, commit_of, read_config, read_safetensors_header,
)
from tests._acestep_helpers import wait_until
from tests._yue2_helpers import VOICES, install_fake_yue2, make_yue2, safetensors_bytes, yue2_env


def test_a_missing_binary_backbone_or_vae_is_not_installed_and_named(tmp_path):
    home = tmp_path / "dpc"
    status = make_yue2(home).status()
    assert status.state == "not_installed" and not status.available
    assert BINARY_NAME in status.reason and "no backbone" in status.reason and "VAE" in status.reason
    assert status.engine_id == "yue2.cpp" and status.key == "yue2"
    if sys.platform != "win32":
        assert "buildcuda.sh" in status.reason

    install_fake_yue2(home, vae=False)
    status = make_yue2(home).status()
    assert status.state == "not_installed"
    assert "binary is missing" not in status.reason and "no backbone" not in status.reason
    assert "YuE2-Vae-F32.gguf" in status.reason


def test_an_installed_engine_is_stopped_available_and_reports_its_form(tmp_path):
    home = tmp_path / "dpc"
    install_fake_yue2(home, odd_adapter=True)
    engine = make_yue2(home)
    status = engine.status()
    assert status.state == "stopped" and status.available and status.supervised
    assert status.version == "e646d52 (2026-11-17)"
    assert status.models["backbone"] == "YuE2-3B-Q8_0.gguf" and status.models["vae"] == "YuE2-Vae-F32.gguf"
    info = engine.form_info()
    assert info["backbones"]["stock"]["present"] is True
    assert info["backbones"]["rs-tw"]["present"] is False and info["backbones"]["rs-tw"]["reason"]
    assert [a["name"] for a in info["adapters"]] == list(VOICES)
    assert all(a["kind"] == "voice" and a["halves"] == ["music", "sound"] for a in info["adapters"])
    assert [a["name"] for a in info["adapters_skipped"]] == ["native-style.safetensors"]
    assert "without stated consent" in info["adapter_note"]
    report = MusicCapabilities(engines={"yue2": engine}).report()
    assert report["engines"]["yue2"]["available"] and report["engines"]["yue2"]["default"]
    assert report["engines"]["yue2"]["adapters"][0]["name"] == VOICES[0]


def test_settings_have_defaults_and_a_bad_value_falls_back(tmp_path):
    home = tmp_path / "dpc"
    config = read_config(lambda s, k, f=None: f, home)
    assert config.binary == home / "engines" / "yue2.cpp" / "build" / BINARY_NAME
    assert config.models_dir == home / "models" / "music" / "yue2"
    assert config.adapters_dir == config.models_dir / "loras" / "ruach-voices" / "voices"
    assert config.port == 9994 and config.idle_stop_s == 300.0 and config.backbone == "stock"
    assert config.device is None and config.max_seq == 0
    assert config.log_path == home / "logs" / "yue2-server.log"
    bad = {"yue2_port": "70000", "yue2_backbone": "huge", "yue2_idle_stop_s": "soon", "yue2_max_seq": "-5"}
    config = read_config(lambda s, k, f=None: bad.get(k, f), home)
    assert config.port == 9994 and config.backbone == "stock" and config.idle_stop_s == 300.0 and config.max_seq == 0


def test_the_command_binds_loopback_with_batch_one_and_the_split_adapters(tmp_path):
    home = tmp_path / "dpc"
    models = install_fake_yue2(home)
    engine = make_yue2(home, settings={"yue2_device": "CUDA0", "yue2_max_seq": "8192"})
    engine.prepare_adapters()
    cmd = engine.build_command(9994)
    assert cmd[cmd.index("--host") + 1] == LOOPBACK and "0.0.0.0" not in cmd
    assert cmd[cmd.index("--port") + 1] == "9994"
    assert cmd[cmd.index("--max-batch") + 1] == str(MAX_BATCH) == "1"
    assert cmd[cmd.index("--model") + 1] == str(models / "serveurperso" / "YuE2-3B-Q8_0.gguf")
    assert cmd[cmd.index("--vae") + 1] == str(models / "serveurperso" / "YuE2-Vae-F32.gguf")
    assert cmd[cmd.index("--adapters") + 1] == str(models / "loras" / ".split")
    assert cmd[cmd.index("--device") + 1] == "CUDA0" and cmd[cmd.index("--max-seq") + 1] == "8192"


def test_a_two_half_adapter_is_split_with_its_tensor_bytes_unchanged_and_cached_by_source_hash(tmp_path):
    home = tmp_path / "dpc"
    models = install_fake_yue2(home)
    engine = make_yue2(home)
    entries = engine.prepare_adapters()
    split = models / "loras" / ".split"
    name = VOICES[1]
    source = models / "loras" / "ruach-voices" / "voices" / name / f"{name}-e029.safetensors"
    src_header, src_start = read_safetensors_header(source)
    src_bytes = source.read_bytes()
    for half, prefix in (("music", "text_encoders."), ("sound", "diffusion_model.")):
        path = split / f"{name}-{half}.safetensors"
        header, start = read_safetensors_header(path)
        keys = [k for k in header if k != "__metadata__"]
        assert keys and all(k.startswith(prefix) for k in keys)
        assert header["__metadata__"]["part"] == half and header["__metadata__"]["who"] == name
        data = path.read_bytes()
        for k in keys:
            a, b = header[k]["data_offsets"]
            sa, sb = src_header[k]["data_offsets"]
            assert data[start + a:start + b] == src_bytes[src_start + sa:src_start + sb]
        assert entries[name]["halves"][half]["sha256"] == "sha256:" + hashlib.sha256(data).hexdigest()
    assert entries[name]["source_sha256"] == "sha256:" + hashlib.sha256(src_bytes).hexdigest()
    manifest = json.loads((split / SPLIT_MANIFEST).read_text(encoding="utf-8"))
    assert manifest["adapters"][name]["source_sha256"] == entries[name]["source_sha256"]

    music = split / f"{name}-music.safetensors"
    before = music.stat().st_mtime_ns
    assert make_yue2(home).prepare_adapters() == entries   # unchanged source: reused
    assert music.stat().st_mtime_ns == before

    keys = ["text_encoders.x.lora_A.weight", "diffusion_model.y.lora_A.weight", "diffusion_model.z.lora_B.weight"]
    source.write_bytes(safetensors_bytes(keys))
    os.utime(source, ns=(before + 10 ** 9, before + 10 ** 9))
    again = make_yue2(home).prepare_adapters()
    assert again[name]["source_sha256"] != entries[name]["source_sha256"]
    assert again[name]["halves"]["sound"]["tensors"] == 2

    import shutil
    shutil.rmtree(source.parent)
    left = make_yue2(home).prepare_adapters()
    assert name not in left and not (split / f"{name}-music.safetensors").exists()


@pytest.mark.asyncio
async def test_start_reaches_a_healthy_server_once_and_stop_ends_it(tmp_path, monkeypatch):
    yue2_env(monkeypatch)
    home = tmp_path / "dpc"
    install_fake_yue2(home)
    changes = []
    engine = make_yue2(home, on_change=changes.append)
    try:
        await asyncio.gather(engine.start(), engine.start())
        assert engine.status().state == "running"
        pid = engine.details()["pid"]
        await engine.start()
        assert engine.details()["pid"] == pid
        entries = [e for e in process_registry.get_registry().open_entries() if e.launcher == "yue2_server"]
        assert [e.pid for e in entries] == [pid]
        assert engine.running_backbone == "stock"
        assert sorted(engine._served_adapters) == sorted(f"{v}-{h}.safetensors" for v in VOICES
                                                         for h in ("music", "sound"))
        assert {"starting", "running"} <= {c.state for c in changes}
        assert all(c.key == "yue2" for c in changes)
        proc = engine._proc
        assert await engine.stop() is True
        assert proc.returncode is not None and engine.status().state == "stopped"
        assert not [e for e in process_registry.get_registry().open_entries() if e.launcher == "yue2_server"]
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_another_backbone_restarts_the_server_and_a_missing_one_is_refused(tmp_path, monkeypatch):
    yue2_env(monkeypatch)
    home = tmp_path / "dpc"
    install_fake_yue2(home, backbones=("stock", "rs-tw"))
    engine = make_yue2(home)
    try:
        await engine.start()
        first = engine.details()["pid"]
        await engine.start(backbone="rs-tw")
        assert engine.running_backbone == "rs-tw" and engine.details()["pid"] != first
        assert engine.status().models["backbone"] == "YuE2-3B-RS-TW-2.0x0.75-Q8_0.gguf"
        assert engine._last_exit["why"] == "swap"
    finally:
        await engine.shutdown()
    home2 = tmp_path / "dpc2"
    install_fake_yue2(home2)
    with pytest.raises(MusicEngineError) as err:
        await make_yue2(home2).start(backbone="rs-tw")
    assert err.value.code == "not_installed" and "rs-tw" in err.value.message


@pytest.mark.asyncio
async def test_a_server_that_loaded_the_wrong_backbone_is_refused(tmp_path, monkeypatch):
    yue2_env(monkeypatch, "wrong_model")
    home = tmp_path / "dpc"
    install_fake_yue2(home)
    engine = make_yue2(home)
    with pytest.raises(MusicEngineError) as err:
        await engine.start()
    assert err.value.code == "start_failed" and "other-backbone.gguf" in err.value.message
    assert not engine.alive() and engine.status().state == "error"


@pytest.mark.asyncio
async def test_a_child_that_exits_on_its_own_is_an_error(tmp_path, monkeypatch):
    yue2_env(monkeypatch)
    home = tmp_path / "dpc"
    install_fake_yue2(home)
    engine = make_yue2(home)
    try:
        await engine.start()
        engine._proc.kill()
        await wait_until(lambda: engine.state == "error")
        assert "exited on its own" in engine.status().reason
    finally:
        await engine.shutdown()


def test_identity_names_the_commit_backbone_and_both_adapter_halves_without_paths(tmp_path):
    home = tmp_path / "dpc"
    install_fake_yue2(home)
    engine = make_yue2(home)
    engine.prepare_adapters()
    ident = engine.identity(backbone="stock", adapters=[{"name": VOICES[0], "music": 0.45, "sound": 0.35}])
    assert ident["engine"] == "yue2.cpp" and ident["commit"] == "e646d52" == commit_of("e646d52 (2026-11-17)")
    assert ident["models"]["backbone"]["choice"] == "stock"
    assert ident["models"]["backbone"]["sha256"].startswith("sha256:")
    assert ident["models"]["vae"]["file"] == "YuE2-Vae-F32.gguf"
    (a,) = ident["adapters"]
    assert a["name"] == VOICES[0] and a["source"]["sha256"].startswith("sha256:")
    assert a["music"]["scale"] == 0.45 and a["sound"]["scale"] == 0.35
    assert a["music"]["file"] == f"{VOICES[0]}-music.safetensors" and a["sound"]["sha256"].startswith("sha256:")
    assert str(tmp_path) not in repr(ident)
