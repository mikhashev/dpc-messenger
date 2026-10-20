"""A download into a cache that is not empty reports only what it adds.

Ark and Zcode's review of a475fdac, 2026-10-21: the bge-m3 folder on the
Windows node holds two revisions (4.6 GB) against a 2.3 GB total, so the bar
stood at 100 % from its first tick and every report counted as the final one,
which skipped the throttle. And a failed size lookup must not be repeated on
every refused browser call while the node is offline.
"""

import asyncio

from dpc_client_core import camoufox_binary
from dpc_client_core import model_download_service as mds
from dpc_client_core.model_download_service import ModelDownloadService
from dpc_client_core.settings import Settings


class _FakeLocalApi:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


def _progress(service):
    return [p for n, p in service.local_api.events if n == "model_download_progress"]


def test_bytes_already_in_the_repo_folder_are_not_progress(tmp_path, monkeypatch):
    cache = tmp_path / "hub"
    blobs = cache / "models--org--name" / "blobs"
    blobs.mkdir(parents=True)
    (blobs / "old-revision").write_bytes(b"o" * 5000)   # more than the total
    monkeypatch.setattr(mds, "hf_cache_path", lambda: str(cache))
    monkeypatch.setattr(mds, "model_size_bytes", lambda name: (2000, "measured"))
    monkeypatch.setattr(mds, "PROGRESS_MIN_INTERVAL_S", 0.02)
    service = ModelDownloadService(llm_manager=None, settings=Settings(tmp_path),
                                   local_api=_FakeLocalApi())

    async def run():
        service._set_status("org/name", "downloading")
        async with service.hf_progress("org/name"):
            for _ in range(2):
                with open(blobs / "new.incomplete", "ab") as f:
                    f.write(b"n" * 500)
                await asyncio.sleep(0.1)

    asyncio.run(run())

    done = [p["bytes_done"] for p in _progress(service)]
    assert done and max(done) == 1000
    assert all(p["progress_percent"] < 100.0 for p in _progress(service))


def test_a_count_that_stays_past_the_total_is_throttled(tmp_path):
    service = ModelDownloadService(llm_manager=None, settings=Settings(tmp_path),
                                   local_api=_FakeLocalApi())
    service._clock = lambda: 100.0                       # one instant

    async def run():
        service._set_status("org/name", "downloading")
        report = service.progress_reporter("org/name")

        def _worker():
            for done in range(1000, 1010):
                report(done, 1000)

        await asyncio.to_thread(_worker)
        await asyncio.sleep(0.05)

    asyncio.run(run())

    assert [p["bytes_done"] for p in _progress(service)] == [1000]


def test_a_failed_size_lookup_is_not_repeated_on_every_refusal(monkeypatch):
    monkeypatch.setattr(camoufox_binary, "PINNED_RELEASE_TAG", "v999.0-beta.99")
    monkeypatch.setattr(camoufox_binary, "_live_size", None)
    monkeypatch.setattr(camoufox_binary, "_offer_failed_at", None)
    calls = []

    def _offline_fetcher():
        calls.append(1)
        raise OSError("offline")

    monkeypatch.setattr(camoufox_binary, "_fetcher_class", lambda: _offline_fetcher)

    assert camoufox_binary.size_bytes() is None
    assert camoufox_binary.size_bytes() is None
    assert camoufox_binary.size_text() == "unknown size"
    assert len(calls) == 1
