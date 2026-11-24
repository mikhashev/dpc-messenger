"""Shared setup for the yue2.cpp engine tests: a fake install under a temp DPC home (binary,
backbones, VAE, two-half voice adapters in safetensors), a supervisor that runs
`_fake_yue2_server.py` in place of `yue-server`, and a rig with both music engines, a real
job store, library and runner."""

import asyncio
import json
import stat
import struct
import sys
from pathlib import Path

from dpc_client_core.music.capabilities import MusicCapabilities
from dpc_client_core.music.engines import MusicEngines
from dpc_client_core.music.identity import MediaTools
from dpc_client_core.music.jobs import MusicJobStore
from dpc_client_core.music.library import MusicLibrary
from dpc_client_core.music.runner import MusicJobRunner
from dpc_client_core.music.yue2 import BACKBONE_FILES, BINARY_NAME, VAE_FILES, Yue2Server, read_config

from tests._acestep_helpers import fake_flac, fake_loudness, install_fake_engine, make_engine

FAKE_SERVER = Path(__file__).with_name("_fake_yue2_server.py")
VOICES = ("voice-ru-f-contralto-167", "voice-ru-m-bass-82")


class FakeYue2Server(Yue2Server):
    """The real supervisor, launching the fake server script with the real flags."""

    def build_command(self, port):
        cmd = super().build_command(port)
        return [sys.executable, str(FAKE_SERVER), *cmd[1:]]


def safetensors_bytes(keys, metadata=None) -> bytes:
    """A small valid safetensors file: one F32 tensor of 4 values per key."""
    header, data, off = {}, b"", 0
    if metadata:
        header["__metadata__"] = dict(metadata)
    for i, k in enumerate(keys):
        chunk = struct.pack("<4f", *([float(i)] * 4))
        header[k] = {"dtype": "F32", "shape": [2, 2], "data_offsets": [off, off + len(chunk)]}
        off += len(chunk)
        data += chunk
    hb = json.dumps(header).encode()
    return struct.pack("<Q", len(hb)) + hb + data


def install_fake_yue2(home: Path, *, binary=True, backbones=("stock",), vae=True, voices=VOICES,
                      odd_adapter=False) -> Path:
    build = home / "engines" / "yue2.cpp" / "build"
    build.mkdir(parents=True, exist_ok=True)
    if binary:
        exe = build / BINARY_NAME
        exe.write_bytes(b"fake yue-server")
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
        (build / "version.h").write_text('#pragma once\n#define YUE2_VERSION "e646d52 (2026-11-17)"\n')
        (build / ("ggml-cuda.dll" if sys.platform == "win32" else "libggml-cuda.so")).write_bytes(b"ggml")
    models = home / "models" / "music" / "yue2"
    for choice in backbones:
        path = models / BACKBONE_FILES[choice][0]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"backbone {choice}".encode())
    if vae:
        path = models / VAE_FILES[0]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"vae f32")
    voices_dir = models / "loras" / "ruach-voices" / "voices"
    for name in voices:
        folder = voices_dir / name
        folder.mkdir(parents=True, exist_ok=True)
        keys = [f"text_encoders.layers.{i}.lora_A.weight" for i in range(2)] + \
               [f"diffusion_model.layers.{i}.lora_A.weight" for i in range(3)]
        (folder / f"{name}-e029.safetensors").write_bytes(safetensors_bytes(keys, {"rank": "16", "who": name}))
    if voices:
        (voices_dir / "samples").mkdir(parents=True, exist_ok=True)
        (voices_dir / "samples" / "a.mp3").write_bytes(b"mp3")
        (voices_dir / "README.md").write_text("voices")
    if odd_adapter:
        voices_dir.mkdir(parents=True, exist_ok=True)
        (voices_dir / "native-style.safetensors").write_bytes(safetensors_bytes(["layers.0.nar_mlp.lora_A"]))
    return models


def make_yue2(home: Path, *, settings=None, **kw) -> FakeYue2Server:
    values = {"yue2_port": "0", **(settings or {})}

    def get(section, key, fallback=None):
        return values.get(key, fallback)

    def config():
        return read_config(get, home)
    return FakeYue2Server(config(), dpc_home=home, config_source=config, **kw)


def yue2_params(**over):
    params = {"lyrics": "[verse]\nТысячи умов, один огонь\nсветит сквозь ночь\n", "style": "indie rock, male vocal",
              "variants": 2, "duration_s": 60, "seed": 42}
    params.update(over)
    return params


class DualRig:
    """Both engines (yue2 fake, acestep fake), a job store, a library and a runner over one
    temp DPC home."""

    def __init__(self, home: Path, *, start_runner=True, yue2_settings=None, ace_settings=None):
        self.home = home
        self.yue2 = make_yue2(home, settings=yue2_settings)
        self.ace = make_engine(home, settings=ace_settings)
        self.engines = MusicEngines({"yue2": self.yue2, "acestep": self.ace}, poll_s=0.05)
        self.store = MusicJobStore(home, capabilities=MusicCapabilities(engines=dict(self.engines.items())))
        self.store.open()
        self.library = MusicLibrary(home, tools=MediaTools(ffmpeg=None, ffprobe=None), save_delay_s=0.01)
        self.events = []
        self.runner = MusicJobRunner(
            self.store, self.engines, self._library, dpc_home=home,
            converter=fake_flac, on_job_change=self.events.append, card_poll_s=0.05, engine_poll_s=0.02,
            loudness_meter=fake_loudness)
        self._start_runner = start_runner

    async def _library(self):
        return self.library

    def propose(self, params):
        """Create a job as `propose_music_job` does: the store, then wake the runner."""
        job = self.store.create_job("generate_from_text", params)
        self.runner.wake()
        return job

    async def __aenter__(self):
        await self.library.open()
        if self._start_runner:
            self.runner.start()
        return self

    async def __aexit__(self, *exc):
        await self.runner.stop()
        await self.engines.shutdown()
        await self.library.stop()

    async def wait_for(self, job_id, states, timeout=60.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            job = self.store.get_job(job_id)
            if job["state"] in states:
                return job
            if loop.time() > deadline:
                raise AssertionError(f"job {job_id} is {job['state']} ({job.get('state_reason')}), not {states}")
            await asyncio.sleep(0.02)


def install_both(home: Path, **kw) -> None:
    install_fake_yue2(home, **kw)
    install_fake_engine(home)


def yue2_env(monkeypatch, modes="", record=None):
    monkeypatch.setenv("FAKE_YUE2_MODE", modes)
    monkeypatch.setenv("FAKE_ACESTEP_MODE", "")
    monkeypatch.delenv("FAKE_ACESTEP_RECORD", raising=False)
    if record is not None:
        monkeypatch.setenv("FAKE_YUE2_RECORD", str(record))
    else:
        monkeypatch.delenv("FAKE_YUE2_RECORD", raising=False)
