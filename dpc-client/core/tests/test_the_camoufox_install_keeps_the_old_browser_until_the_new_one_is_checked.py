"""The Camoufox install keeps the old browser until the new one is checked.

The package's own `install()` deleted the browser first and fetched second, so
a failed download left no browser at all; it checked nothing but the unzip,
fetched uBlock Origin as AMO's `latest` outside the consent, and drew tqdm bars
on stdout. Mike, 2026-10-21: stage beside the install and swap, verify the
SHA-256, pin the add-on, log instead of printing, and let only one download of
a name run whichever command asked for it. Every byte here is served by a fake
`requests.get`; nothing reaches the network.
"""

import asyncio
import hashlib
import io
import json
import logging
import os
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from dpc_client_core import camoufox_binary

pkgman = pytest.importorskip("camoufox.pkgman")

ZIP_URL = "https://example.invalid/camoufox-152.0.4-beta.28.zip"


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _zip(entries) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _browser_zip(with_executable=True) -> bytes:
    if pkgman.OS_NAME == "mac":
        entries = {"Camoufox.app/Contents/Resources/properties.json": b"{}"}
        if with_executable:
            entries["Camoufox.app/Contents/MacOS/camoufox"] = b"bin"
    else:
        entries = {"properties.json": b"{}"}
        if with_executable:
            entries[pkgman.LAUNCH_FILE[pkgman.OS_NAME]] = b"bin"
    return _zip(entries)


XPI = _zip({"manifest.json": json.dumps({"version": "1.72.2"}).encode()})


class _Response:
    def __init__(self, body):
        self._body = body
        self.headers = {"content-length": str(len(body))}

    def raise_for_status(self):
        pass

    def iter_content(self, size):
        for i in range(0, len(self._body), size):
            yield self._body[i:i + size]


@pytest.fixture
def world(tmp_path, monkeypatch):
    """An install folder holding an old browser, a pinned zip and add-on
    whose tables match what the fake server sends."""
    import requests

    install = tmp_path / "camoufox" / "Cache"
    install.mkdir(parents=True)
    (install / "version.json").write_text(json.dumps({"version": "135.0", "release": "beta.18"}))
    (install / "OLD_BROWSER").write_text("old")
    monkeypatch.setattr(pkgman, "INSTALL_DIR", install)

    served = {"zip": _browser_zip(), "xpi": XPI}
    key = camoufox_binary._platform_key()
    tag = camoufox_binary.PINNED_RELEASE_TAG
    monkeypatch.setattr(camoufox_binary, "_PINNED_ASSET_SHA256", {tag: {key: _sha(served["zip"])}})
    monkeypatch.setattr(camoufox_binary, "_PINNED_ASSET_SIZES", {tag: {key: len(served["zip"])}})
    ublock_url = "https://example.invalid/ublock_origin-1.72.2.xpi"
    monkeypatch.setattr(camoufox_binary, "_UBLOCK_ORIGIN_FILES", {
        camoufox_binary.UBLOCK_ORIGIN_VERSION: {"url": ublock_url, "size": len(XPI), "sha256": _sha(XPI)},
    })
    asked = []

    def _get(url, *a, **k):
        asked.append(url)
        return _Response(served["zip"] if url == ZIP_URL else served["xpi"])

    monkeypatch.setattr(requests, "get", _get)
    fetcher = SimpleNamespace(url=ZIP_URL, asset_size=None, asset_digest=None, verstr="152.0.4-beta.28")
    return SimpleNamespace(install=install, served=served, asked=asked, fetcher=fetcher,
                           ublock_url=ublock_url, key=key, tag=tag)


def _flip_last_byte(data: bytes) -> bytes:
    return data[:-1] + bytes([data[-1] ^ 0x01])


def _siblings(install: Path):
    return sorted(p.name for p in install.parent.iterdir())


# --- 1. the old browser stays until the new one is checked -------------------------


def test_a_good_download_replaces_the_old_browser_and_leaves_nothing_beside_it(world, capsys):
    camoufox_binary._install_pinned(world.fetcher, None)

    assert not (world.install / "OLD_BROWSER").exists()
    version = json.loads((world.install / "version.json").read_text())
    assert version == {"version": "152.0.4", "release": "beta.28"}
    assert _siblings(world.install) == ["Cache"]
    # 4. nothing on the console (tqdm writes to stderr, rprint to stdout):
    # dpc-client.log is the only record.
    console = capsys.readouterr()
    assert (console.out, console.err) == ("", "")


def test_a_wrong_checksum_keeps_the_old_browser(world):
    # Same length, one byte changed: only the checksum can tell.
    world.served["zip"] = _flip_last_byte(world.served["zip"])
    with pytest.raises(ValueError, match="does not match"):
        camoufox_binary._install_pinned(world.fetcher, None)

    assert (world.install / "OLD_BROWSER").read_text() == "old"
    assert _siblings(world.install) == ["Cache"]


def test_a_zip_without_the_browser_keeps_the_old_browser(world, monkeypatch):
    bad = _browser_zip(with_executable=False)
    world.served["zip"] = bad
    monkeypatch.setattr(camoufox_binary, "_PINNED_ASSET_SHA256", {world.tag: {world.key: _sha(bad)}})
    monkeypatch.setattr(camoufox_binary, "_PINNED_ASSET_SIZES", {world.tag: {world.key: len(bad)}})

    with pytest.raises(ValueError, match="no executable"):
        camoufox_binary._install_pinned(world.fetcher, None)

    assert (world.install / "OLD_BROWSER").read_text() == "old"
    assert _siblings(world.install) == ["Cache"]


def test_a_swap_that_fails_halfway_puts_the_old_browser_back(world, monkeypatch):
    real_replace = os.replace

    def _replace(src, dst):
        if str(src).endswith(".staging"):
            raise PermissionError("the folder is in use")
        return real_replace(src, dst)

    monkeypatch.setattr(camoufox_binary.os, "replace", _replace)

    with pytest.raises(PermissionError):
        camoufox_binary._install_pinned(world.fetcher, None)

    assert (world.install / "OLD_BROWSER").read_text() == "old"
    assert _siblings(world.install) == ["Cache"]


def test_an_install_interrupted_between_the_renames_is_recovered(world):
    previous = world.install.parent / "Cache.previous"
    os.replace(world.install, previous)
    world.served["zip"] = b"not the pinned zip"

    with pytest.raises(ValueError):
        camoufox_binary._install_pinned(world.fetcher, None)

    assert (world.install / "OLD_BROWSER").read_text() == "old"
    assert not previous.exists()


# --- 2. the checksum ------------------------------------------------------------


def test_the_table_digest_wins_over_the_one_the_api_returns(world):
    world.fetcher.asset_digest = "sha256:" + "0" * 64
    camoufox_binary._install_pinned(world.fetcher, None)
    assert not (world.install / "OLD_BROWSER").exists()


def test_a_pin_with_no_table_row_is_checked_against_the_api_digest(world, monkeypatch):
    monkeypatch.setattr(camoufox_binary, "_PINNED_ASSET_SHA256", {})
    world.fetcher.asset_digest = "sha256:" + "0" * 64

    with pytest.raises(ValueError, match="does not match"):
        camoufox_binary._install_pinned(world.fetcher, None)
    assert (world.install / "OLD_BROWSER").read_text() == "old"


def test_with_no_digest_anywhere_nothing_is_downloaded(world, monkeypatch):
    monkeypatch.setattr(camoufox_binary, "_PINNED_ASSET_SHA256", {})

    with pytest.raises(ValueError, match="no SHA-256"):
        camoufox_binary._install_pinned(world.fetcher, None)
    assert world.asked == []


def test_the_shipped_digest_table_covers_every_platform_of_the_pin():
    tag = camoufox_binary.PINNED_RELEASE_TAG
    assert set(camoufox_binary._PINNED_ASSET_SHA256[tag]) == set(camoufox_binary._PINNED_ASSET_SIZES[tag])
    assert all(v.startswith("sha256:") and len(v) == 71
               for v in camoufox_binary._PINNED_ASSET_SHA256[tag].values())


# --- 3. uBlock Origin, pinned and inside the consent ----------------------------


def test_the_pinned_add_on_is_fetched_checked_and_placed_where_the_package_looks(world):
    camoufox_binary._install_pinned(world.fetcher, None)

    assert world.asked == [ZIP_URL, world.ublock_url]
    addon = Path(pkgman.get_path(os.path.join("addons", "UBO")))
    assert json.loads((addon / "manifest.json").read_text())["version"] == "1.72.2"


def test_a_tampered_add_on_keeps_the_old_browser(world):
    world.served["xpi"] = _flip_last_byte(XPI)
    with pytest.raises(ValueError, match="does not match"):
        camoufox_binary._install_pinned(world.fetcher, None)
    assert (world.install / "OLD_BROWSER").read_text() == "old"


def test_the_shipped_add_on_pin_is_a_versioned_file_and_not_latest():
    row = camoufox_binary._UBLOCK_ORIGIN_FILES[camoufox_binary.UBLOCK_ORIGIN_VERSION]
    assert "/latest/" not in row["url"]
    assert camoufox_binary.UBLOCK_ORIGIN_VERSION in row["url"]


def test_progress_counts_the_add_on_inside_one_total(world):
    seen = []
    camoufox_binary._install_pinned(world.fetcher, lambda done, total: seen.append((done, total)))
    total = len(world.served["zip"]) + len(XPI)
    assert seen[-1] == (total, total)


# --- 4. extraction is logged, not printed ---------------------------------------


def test_extraction_is_reported_in_the_log(world, caplog, capsys):
    with caplog.at_level(logging.INFO, logger="dpc_client_core.camoufox_binary"):
        camoufox_binary._install_pinned(world.fetcher, None)
    lines = [r.getMessage() for r in caplog.records]
    assert "Camoufox browser: extracted 100%" in lines
    assert "uBlock Origin 1.72.2: extracted 100%" in lines
    assert any(line.startswith("Camoufox browser installed: 152.0.4-beta.28 with uBlock Origin 1.72.2")
               for line in lines)
    console = capsys.readouterr()
    assert (console.out, console.err) == ("", "")


# --- 5. one download per name, whichever command asked ---------------------------


class _FakeLocalApi:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


def test_the_older_whisper_command_waits_its_turn_behind_download_model(tmp_path, monkeypatch):
    from dpc_client_core import model_download_service as mds
    from dpc_client_core.model_download_service import ModelDownloadService
    from dpc_client_core.settings import Settings
    from dpc_client_core.voice_service import VoiceService

    monkeypatch.setattr(mds, "model_size_bytes", lambda name, revision=None: (None, None))
    monkeypatch.setattr(mds, "hf_cache_path", lambda: str(tmp_path / "hub"))
    started = threading.Event()
    release = threading.Event()
    calls = []

    class _Whisper:
        alias = "my_whisper"
        config = {"type": "local_whisper", "model": "openai/whisper-tiny"}

        async def download_model_async(self, progress_callback=None):
            calls.append(1)
            started.set()
            await asyncio.to_thread(release.wait, 5)
            return {"success": True, "model_name": "openai/whisper-tiny", "message": "ok"}

    llm = SimpleNamespace(providers={"my_whisper": _Whisper()})
    local_api = _FakeLocalApi()
    service = ModelDownloadService(llm_manager=llm, settings=Settings(tmp_path), local_api=local_api)
    voice = VoiceService(llm_manager=llm, settings=Settings(tmp_path), local_api=local_api,
                         model_download_service=service)

    async def run():
        first = asyncio.create_task(service.download_model("openai/whisper-tiny", "my_whisper"))
        while not started.is_set():
            await asyncio.sleep(0.01)
        second = await voice.download_whisper_model("my_whisper")
        release.set()
        first_result = await first
        third = await voice.download_whisper_model("my_whisper")
        return first_result, second, third

    first, second, third = asyncio.run(run())

    assert first["status"] == "success"
    assert second == {"status": "error", "error": "'openai/whisper-tiny' is already being downloaded"}
    assert third["status"] == "success"  # the guard was released
    assert calls == [1, 1]
    assert "openai/whisper-tiny" not in service._in_flight
