"""A model download reports its progress, at most twice a second, and blocks nothing.

`model_download_progress` (Mike's word, 2026-10-21) is sent for every download
the consent flow runs — the Camoufox browser binary, Whisper, bge-m3. The rate
is limited by time because the file-transfer screen lost its websocket at ~13
events a second. No test here reaches the network.
"""

import asyncio
import os
import threading
import time
from types import SimpleNamespace

import pytest

from dpc_client_core import camoufox_binary
from dpc_client_core import model_download_service as mds
from dpc_client_core.model_download_service import ModelDownloadService
from dpc_client_core.settings import Settings


class _FakeLocalApi:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


def _service(tmp_path, llm_manager=None):
    return ModelDownloadService(
        llm_manager=llm_manager, settings=Settings(tmp_path), local_api=_FakeLocalApi()
    )


def _progress(service):
    return [p for n, p in service.local_api.events if n == "model_download_progress"]


# --- the throttle --------------------------------------------------------------


def test_reports_from_a_thread_are_limited_by_time(tmp_path):
    service = _service(tmp_path)
    now = [100.0]
    service._clock = lambda: now[0]

    async def run():
        service._set_status("camoufox", "downloading")
        report = service.progress_reporter("camoufox")

        def _worker():
            for done in range(1, 51):          # 50 chunks inside one instant
                report(done, 1000)
            now[0] += mds.PROGRESS_MIN_INTERVAL_S
            report(60, 1000)                   # the interval has passed
            report(61, 1000)                   # too soon again
            report(1000, 1000)                 # the total always goes out

        await asyncio.to_thread(_worker)
        await asyncio.sleep(0.05)

    asyncio.run(run())

    assert [p["bytes_done"] for p in _progress(service)] == [1, 60, 1000]
    assert _progress(service)[-1] == {
        "model_name": "camoufox", "bytes_done": 1000, "total_bytes": 1000,
        "progress_percent": 100.0,
    }


def test_an_unknown_total_reports_bytes_without_a_percent(tmp_path):
    service = _service(tmp_path)

    async def run():
        service._set_status("x/y", "downloading")
        service.progress_reporter("x/y")(5, None)
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert _progress(service) == [
        {"model_name": "x/y", "bytes_done": 5, "total_bytes": None, "progress_percent": None},
    ]


def test_a_report_after_completion_does_not_reopen_the_download(tmp_path):
    service = _service(tmp_path)

    async def run():
        await service.emit_started("camoufox")
        report = service.progress_reporter("camoufox")
        await service.emit_completed("camoufox")
        report(10, 100)
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert _progress(service) == []
    assert service.get_status("camoufox") == {"state": "cached"}


# --- the Camoufox binary: background, with progress --------------------------------


def test_the_browser_download_runs_in_the_background_and_reports_bytes(tmp_path, monkeypatch):
    halfway = threading.Event()
    release = threading.Event()

    def _download(on_progress=None):
        on_progress(250, 1000)
        halfway.set()
        release.wait(5)
        on_progress(1000, 1000)
        return "152.0.4-beta.28"

    monkeypatch.setattr(camoufox_binary, "download", _download)
    service = _service(tmp_path)

    async def run():
        task = asyncio.create_task(service.download_model("camoufox"))
        for _ in range(500):
            if halfway.is_set() or task.done():
                break
            await asyncio.sleep(0.01)
        assert halfway.is_set(), task.result() if task.done() else "download never started"
        await asyncio.sleep(0.05)
        # The loop answers while the download thread is blocked: what a status
        # query from the UI sees in the middle of it.
        middle = service.get_status("camoufox")
        release.set()
        return middle, await task

    middle, result = asyncio.run(run())

    assert middle == {
        "state": "downloading", "bytes_done": 250, "total_bytes": 1000,
        "progress_percent": 25.0,
    }
    assert result["status"] == "success"
    names = [n for n, _ in service.local_api.events]
    assert names[0] == "model_download_started"
    assert names[-1] == "model_download_completed"
    assert [p["bytes_done"] for p in _progress(service)] == [250, 1000]


def test_the_fetcher_hands_every_chunk_to_the_reporter(monkeypatch, tmp_path):
    import hashlib

    import requests

    body = b"x" * (3 * 1024 * 1024)

    class _Response:
        headers = {"content-length": str(len(body))}

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            for i in range(0, len(body), size):
                yield body[i:i + size]

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Response())
    seen = []

    camoufox_binary._fetch_verified(
        "https://example.invalid/c.zip", tmp_path / "c.zip", label="Camoufox browser",
        expected_size=len(body), expected_sha256="sha256:" + hashlib.sha256(body).hexdigest(),
        on_bytes=seen.append,
    )

    assert seen == [1 << 20, 1 << 20, 1 << 20]


# --- Hugging Face downloads: the cache folder is read while it grows -------------


@pytest.fixture
def hf_cache(tmp_path, monkeypatch):
    cache = tmp_path / "hub"
    cache.mkdir()
    monkeypatch.setattr(mds, "hf_cache_path", lambda: str(cache))
    monkeypatch.setattr(mds, "model_size_bytes", lambda name, revision=None: (3000, "measured"))
    monkeypatch.setattr(mds, "PROGRESS_MIN_INTERVAL_S", 0.02)
    return cache


def _grow(cache, model_name, steps=3, chunk=1000, pause=0.08):
    blobs = cache / ("models--" + model_name.replace("/", "--")) / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    partial = blobs / "abc123.incomplete"
    for _ in range(steps):
        with open(partial, "ab") as f:
            f.write(b"x" * chunk)
        time.sleep(pause)


def test_the_embedding_download_reports_the_bytes_reaching_its_cache(tmp_path, hf_cache, monkeypatch):
    import sentence_transformers

    class _FakeST:
        def __init__(self, name, **kwargs):
            _grow(hf_cache, name)

    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", _FakeST)
    service = _service(tmp_path)

    result = asyncio.run(service.download_model("BAAI/bge-m3"))

    assert result["status"] == "success"
    done = [p["bytes_done"] for p in _progress(service)]
    assert done and done == sorted(done) and done[-1] == 3000
    assert _progress(service)[-1]["progress_percent"] == 100.0


class _WhisperProvider:
    def __init__(self, cache, model="openai/whisper-tiny"):
        self.alias = "my_whisper"
        self.config = {"type": "local_whisper", "model": model}
        self._cache = cache

    async def download_model_async(self, progress_callback=None):
        await asyncio.to_thread(_grow, self._cache, self.config["model"])
        return {"success": True, "model_name": self.config["model"]}


def test_the_whisper_download_reports_its_progress(tmp_path, hf_cache):
    provider = _WhisperProvider(hf_cache)
    service = _service(tmp_path, SimpleNamespace(providers={"my_whisper": provider}))

    result = asyncio.run(service.download_model("openai/whisper-tiny", "my_whisper"))

    assert result["status"] == "success"
    assert _progress(service) and _progress(service)[-1]["bytes_done"] == 3000


def test_the_older_whisper_command_reports_its_progress_too(tmp_path, hf_cache):
    from dpc_client_core.voice_service import VoiceService

    provider = _WhisperProvider(hf_cache)
    llm = SimpleNamespace(providers={"my_whisper": provider})
    service = _service(tmp_path, llm)
    voice = VoiceService(
        llm_manager=llm, settings=Settings(tmp_path), local_api=service.local_api,
        model_download_service=service,
    )

    result = asyncio.run(voice.download_whisper_model("my_whisper"))

    assert result["status"] == "success"
    assert _progress(service) and _progress(service)[-1]["bytes_done"] == 3000


def test_a_snapshot_symlink_is_not_counted_twice(tmp_path):
    folder = tmp_path / "models--a--b"
    (folder / "blobs").mkdir(parents=True)
    (folder / "snapshots" / "rev").mkdir(parents=True)
    blob = folder / "blobs" / "abc"
    blob.write_bytes(b"x" * 100)
    try:
        os.symlink(blob, folder / "snapshots" / "rev" / "model.bin")
    except (OSError, NotImplementedError):
        pytest.skip("this filesystem gives no symlinks without privileges")
    assert mds._hf_repo_bytes(str(folder)) == 100
