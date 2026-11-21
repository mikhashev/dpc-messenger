"""Shared setup for the acestep.cpp engine and job runner tests: a fake install under a temp
DPC home, a supervisor that runs `_fake_acestep_server.py` in place of `ace-server`, a
pure-Python stand-in for the ffmpeg FLAC conversion, and a runner wired to a real job
store and library."""

import asyncio
import stat
import struct
import sys
from pathlib import Path

from dpc_client_core.music.acestep import BINARY_NAME, MODEL_FILES, AceStepServer, read_config
from dpc_client_core.music.capabilities import MusicCapabilities
from dpc_client_core.music.identity import MediaTools
from dpc_client_core.music.jobs import MusicJobStore
from dpc_client_core.music.library import MusicLibrary
from dpc_client_core.music.runner import MusicJobRunner, pcm_md5_and_sha256, read_wav_info

FAKE_SERVER = Path(__file__).with_name("_fake_acestep_server.py")


class FakeAceStepServer(AceStepServer):
    """The real supervisor, launching the fake server script with the real flags."""

    def build_command(self, port):
        cmd = super().build_command(port)
        return [sys.executable, str(FAKE_SERVER), *cmd[1:]]


def install_fake_engine(home: Path, *, binary=True, models=tuple(MODEL_FILES.values())) -> None:
    build = home / "engines" / "acestep.cpp" / "build"
    build.mkdir(parents=True, exist_ok=True)
    if binary:
        exe = build / BINARY_NAME
        exe.write_bytes(b"fake ace-server")
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
        (build / "version.h").write_text('#pragma once\n#define ACE_VERSION "fake0000 (2026-11-21)"\n')
        (build / ("ggml-cuda.dll" if sys.platform == "win32" else "libggml-cuda.so")).write_bytes(b"ggml")
    folder = home / "models" / "music" / "ace-step"
    folder.mkdir(parents=True, exist_ok=True)
    for name in models:
        (folder / name).write_bytes(name.encode())


def make_engine(home: Path, *, settings=None, **kw) -> FakeAceStepServer:
    values = {"acestep_port": "0", **(settings or {})}

    def get(section, key, fallback=None):
        return values.get(key, fallback)

    def config():
        return read_config(get, home)
    return FakeAceStepServer(config(), dpc_home=home, config_source=config, **kw)


def fake_flac(wav: Path, flac: Path, tags) -> None:
    """A FLAC header with the STREAMINFO a lossless encoder would write (no frames), for
    machines without ffmpeg."""
    info = read_wav_info(wav)
    md5, _ = pcm_md5_and_sha256(wav, info)
    frame = info.channels * ((info.bits + 7) // 8)
    total = info.data_size // frame
    packed = (info.sample_rate << 44) | ((info.channels - 1) << 41) | ((info.bits - 1) << 36) | total
    si = struct.pack(">HH", 4096, 4096) + (0).to_bytes(3, "big") * 2 + packed.to_bytes(8, "big") + bytes.fromhex(md5)
    flac.write_bytes(b"fLaC" + bytes([0x80]) + len(si).to_bytes(3, "big") + si)
    fake_flac.calls.append(dict(tags))


fake_flac.calls = []


def fake_loudness(flac: Path) -> dict:
    """A loudness record without ffmpeg: variant 1 reads -12.5 LUFS, the others -14.0."""
    lufs = -12.5 if flac.stem.endswith("_v1") else -14.0
    return {"lufs_i": lufs, "lra": 3.2, "true_peak_dbtp": -1.4, "measured_by": "ffmpeg ebur128",
            "measured_at": "2026-11-21T20:00:00Z"}


class Rig:
    """An engine, a job store, a library and a runner over one temp DPC home."""

    def __init__(self, home: Path, *, holders=None, converter=fake_flac, engine_settings=None, start_runner=True,
                 loudness_meter=fake_loudness):
        self.home = home
        self.holders = list(holders or [])
        self.engine = make_engine(home, settings=engine_settings)
        self.store = MusicJobStore(home, capabilities=MusicCapabilities(self.engine.status))
        self.store.open()
        self.library = MusicLibrary(home, tools=MediaTools(ffmpeg=None, ffprobe=None), save_delay_s=0.01)
        self.events = []
        self.runner = MusicJobRunner(
            self.store, self.engine, self._library, dpc_home=home, card_holders=lambda: list(self.holders),
            converter=converter, on_job_change=self.events.append, card_poll_s=0.05, engine_poll_s=0.02,
            loudness_meter=loudness_meter)
        self._start_runner = start_runner

    async def _library(self):
        return self.library

    async def __aenter__(self):
        await self.library.open()
        if self._start_runner:
            self.runner.start()
        return self

    async def __aexit__(self, *exc):
        await self.runner.stop()
        await self.engine.shutdown()
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


async def wait_until(predicate, timeout=30.0, step=0.02):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(step)


def from_text(**over):
    params = {"lyrics": "[verse]\nТысячи умов, один огонь\n", "style": "indie rock, warm guitars", "variants": 2,
              "duration_s": 30, "seed": 42}
    params.update(over)
    return params


def files_in(folder: Path):
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


def free_env(monkeypatch, modes="", record=None):
    monkeypatch.setenv("FAKE_ACESTEP_MODE", modes)
    if record is not None:
        monkeypatch.setenv("FAKE_ACESTEP_RECORD", str(record))
    else:
        monkeypatch.delenv("FAKE_ACESTEP_RECORD", raising=False)

