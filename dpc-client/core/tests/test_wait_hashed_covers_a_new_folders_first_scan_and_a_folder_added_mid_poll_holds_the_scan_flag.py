"""Two timing gaps around a watched folder's first scan (follow-up 21).

- E. `add_folder(scan=True)` starts its first scan in the background. `wait_hashed()` waited only for the
  hash queue, which is idle until the walk has queued something, so a call right after the add returned
  with the folder unscanned (the live GPU probe of 2026-11-26 needed an explicit `rescan_folder`). It now
  waits for those first scans too.
- G. A poll pass walks a snapshot of the folder list, but its "every folder scanned cleanly" check read
  the live list, so a folder added during the pass raised `initial_scan_done` without ever being scanned
  (Zcode #204). The check now covers the pass's snapshot and requires every live watched folder to have
  had a clean walk in this process.
"""

import asyncio

import pytest

from dpc_client_core.music.identity import MediaTools
from dpc_client_core.music.library import MusicLibrary


def _library(home):
    return MusicLibrary(home, tools=MediaTools(ffmpeg=None, ffprobe=None), save_delay_s=0.01)


def _folder(path, names):
    path.mkdir(parents=True)
    for name in names:
        (path / name).write_bytes(name.encode("utf-8") * 64)
    return path


@pytest.mark.asyncio
async def test_wait_hashed_right_after_add_folder_sees_the_folder_scanned_and_hashed(tmp_path):
    music = _folder(tmp_path / "Music", ["a.mp3", "b.flac", "c.wav"])
    lib = _library(tmp_path / "dpc")
    await lib.open()
    try:
        await lib.add_folder(str(music))          # scan=True: the walk runs in the background
        await lib.wait_hashed()
        entries = lib.list_entries()
        assert sorted(e["source"]["name"] for e in entries) == ["a.mp3", "b.flac", "c.wav"]
        assert all(e["hash_state"] == "done" for e in entries)
        assert not lib._first_scans               # the finished scan is not held
    finally:
        await lib.stop()


@pytest.mark.asyncio
async def test_wait_hashed_times_out_on_a_scan_that_does_not_end_and_leaves_it_running(tmp_path, monkeypatch):
    music = _folder(tmp_path / "Music", ["a.mp3"])
    lib = _library(tmp_path / "dpc")
    await lib.open()
    gate = asyncio.Event()
    real = lib.rescan_folder

    async def slow(folder_id):
        await gate.wait()
        return await real(folder_id)

    monkeypatch.setattr(lib, "rescan_folder", slow)
    try:
        await lib.add_folder(str(music))
        with pytest.raises(asyncio.TimeoutError):
            await lib.wait_hashed(timeout=0.05)
        (task,) = lib._first_scans
        assert not task.done()                    # not cancelled by the timeout
        gate.set()
        await lib.wait_hashed()
        assert [e["source"]["name"] for e in lib.list_entries()] == ["a.mp3"]
    finally:
        await lib.stop()


@pytest.mark.asyncio
async def test_a_folder_added_during_a_poll_pass_holds_the_flag_until_a_pass_scans_it(tmp_path, monkeypatch):
    first = _folder(tmp_path / "First", ["a.mp3"])
    second = _folder(tmp_path / "Second", ["b.mp3"])
    lib = _library(tmp_path / "dpc")
    await lib.open()
    try:
        await lib.add_folder(str(first), scan=False)
        real = lib.rescan_folder
        added = []

        async def add_mid_pass(folder_id):
            out = await real(folder_id)
            if not added:                         # the person adds a folder while the pass runs
                added.append(await lib.add_folder(str(second), scan=False))
            return out

        monkeypatch.setattr(lib, "rescan_folder", add_mid_pass)
        await lib._poll_pass()
        assert added and lib.initial_scan_done is False          # the second folder was not in this pass
        assert not lib._watched_folders_scanned_clean()
        await lib._poll_pass()                                  # the next pass walks it
        assert lib.initial_scan_done is True
        assert sorted(e["source"]["name"] for e in lib.list_entries()) == ["a.mp3", "b.mp3"]
    finally:
        await lib.stop()


@pytest.mark.asyncio
async def test_a_folder_that_failed_its_last_walk_or_was_never_walked_is_not_clean(tmp_path):
    first = _folder(tmp_path / "First", ["a.mp3"])
    lib = _library(tmp_path / "dpc")
    await lib.open()
    try:
        folder = await lib.add_folder(str(first), scan=False)
        assert not lib._watched_folders_scanned_clean()          # never walked in this process
        await lib.rescan_all()
        assert lib.initial_scan_done is True and folder["id"] in lib._scanned_clean
        first.rename(tmp_path / "Gone")                          # the next walk cannot read it
        await lib.rescan_folder(folder["id"])
        assert folder["id"] not in lib._scanned_clean and not lib._watched_folders_scanned_clean()
        await lib.remove_folder(folder["id"])
        assert lib._watched_folders_scanned_clean() and folder["id"] not in lib._scanned_clean
    finally:
        await lib.stop()
