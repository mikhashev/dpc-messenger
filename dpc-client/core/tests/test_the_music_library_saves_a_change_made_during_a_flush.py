"""A change made while a save is in its thread is saved too, without waiting for the
next change; and a failed final save at shutdown is logged, not raised.
"""

import asyncio
import json
import logging
import threading

import pytest

from dpc_client_core.music import library as library_module
from dpc_client_core.music.identity import MediaTools
from dpc_client_core.music.library import MusicLibrary


async def _until(condition, timeout=10.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("the condition did not become true in time")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_a_mutation_during_a_flush_is_saved_by_the_same_save_task(tmp_path, monkeypatch):
    lib = MusicLibrary(tmp_path / "dpc", tools=MediaTools(ffmpeg=None, ffprobe=None), save_delay_s=0.01)
    await lib.open()
    playlist = await lib.create_playlist("start")

    entered = threading.Event()
    gate = threading.Event()
    real_write = library_module.atomic_write_text

    def slow_write(path, text):
        if not gate.is_set():
            entered.set()
            gate.wait(10)
        real_write(path, text)

    monkeypatch.setattr(library_module, "atomic_write_text", slow_write)
    try:
        lib._playlists[playlist["id"]]["name"] = "during-1"
        lib._mark_dirty()
        await _until(entered.is_set)          # the save task is now inside its thread
        task = lib._save_task
        lib._playlists[playlist["id"]]["name"] = "during-2"
        lib._mark_dirty()                      # no new task: the running one must pick it up
        assert lib._save_task is task
        gate.set()
        await asyncio.wait_for(task, 10)
        on_disk = json.loads(lib.playlists_path.read_text(encoding="utf-8"))
        dirty = lib._dirty
    finally:
        gate.set()
        await lib.stop()
    assert [p["name"] for p in on_disk] == ["during-2"]
    assert dirty is False


@pytest.mark.asyncio
async def test_a_failed_final_save_at_shutdown_is_logged_not_raised(tmp_path, monkeypatch, caplog):
    lib = MusicLibrary(tmp_path / "dpc", tools=MediaTools(ffmpeg=None, ffprobe=None), save_delay_s=60)
    await lib.open()
    lib._mark_dirty()

    def refuse(path, text):
        raise OSError("disk full")

    monkeypatch.setattr(library_module, "atomic_write_text", refuse)
    with caplog.at_level(logging.ERROR, logger=library_module.__name__):
        await lib.stop()  # must not raise
    assert any("final save at shutdown failed" in r.getMessage() for r in caplog.records)
    assert lib._dirty is True
