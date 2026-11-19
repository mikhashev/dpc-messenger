"""A watched folder picks up new and removed files without a button: the library
polls it (ADR-051 D4; N6 left the watcher open, polling adds no dependency). A folder
added with `watch: false` is not polled.
"""

import asyncio
import os

import pytest

from dpc_client_core.music.identity import MediaTools
from dpc_client_core.music.library import MusicLibrary


async def _until(condition, timeout=10.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("the condition did not become true in time")
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_polling_finds_a_new_file_and_marks_a_removed_one_missing(tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    (music / "first.mp3").write_bytes(os.urandom(512))
    lib = MusicLibrary(tmp_path / "dpc", tools=MediaTools(ffmpeg=None, ffprobe=None),
                       poll_interval_s=0.05, save_delay_s=0.01)
    await lib.start()
    try:
        await lib.add_folder(str(music))
        await _until(lambda: [e["hash_state"] for e in lib.list_entries()] == ["done"])

        (music / "second.flac").write_bytes(os.urandom(512))
        await _until(lambda: len(lib.list_entries()) == 2)
        await lib.wait_hashed()

        (music / "first.mp3").unlink()
        await _until(lambda: [e["source"]["name"] for e in lib.list_entries() if e["missing"]] == ["first.mp3"])
        folder = lib.list_folders()[0]
    finally:
        await lib.stop()
    assert folder["watch_state"] == "polling" and "0.05" in folder["state_reason"]
    assert folder["file_count"] == 1


@pytest.mark.asyncio
async def test_a_folder_that_is_not_watched_waits_for_a_rescan(tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    lib = MusicLibrary(tmp_path / "dpc", tools=MediaTools(ffmpeg=None, ffprobe=None),
                       poll_interval_s=0.05, save_delay_s=0.01)
    await lib.start()
    try:
        folder = await lib.add_folder(str(music), watch=False)
        await _until(lambda: lib.list_folders()[0]["last_scan_at"] is not None)
        (music / "late.mp3").write_bytes(b"late")
        await asyncio.sleep(0.3)
        assert lib.list_entries() == []
        await lib.rescan_folder(folder["id"])
        entries = lib.list_entries()
    finally:
        await lib.stop()
    assert lib.list_folders()[0]["watch_state"] == "off"
    assert [e["source"]["name"] for e in entries] == ["late.mp3"]
