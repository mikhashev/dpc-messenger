"""A file that disappears keeps its entry as `missing` (never deleted) and comes back
by itself; the same audio at two paths is one entry with `alt_paths` (ADR-051 Q4
layer 5, SPEC flow 22).
"""

import os

import pytest

from dpc_client_core.music.identity import MediaTools
from dpc_client_core.music.library import MusicLibrary


def _library(home):
    return MusicLibrary(home, tools=MediaTools(ffmpeg=None, ffprobe=None), save_delay_s=0.01)


async def _scan(lib, folder_id):
    await lib.rescan_folder(folder_id)
    await lib.wait_hashed()


@pytest.mark.asyncio
async def test_a_deleted_file_is_kept_as_missing_and_returns_when_it_reappears(tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    data = os.urandom(1024)
    (music / "gone.mp3").write_bytes(data)
    lib = _library(tmp_path / "dpc")
    await lib.open()
    try:
        folder = await lib.add_folder(str(music), scan=False)
        await _scan(lib, folder["id"])
        [entry] = lib.list_entries()
        playlist = await lib.create_playlist("P", [entry["id"]])

        (music / "gone.mp3").unlink()
        await _scan(lib, folder["id"])
        [missing] = lib.list_entries()
        present_only = lib.list_entries(include_missing=False)
        assert lib.get_track_path(entry["id"]) is None

        (music / "back.mp3").write_bytes(data)
        await _scan(lib, folder["id"])
        [back] = lib.list_entries()
        lists = lib.list_playlists()
    finally:
        await lib.stop()

    assert missing["id"] == entry["id"] and missing["missing"] is True
    assert present_only == []
    assert back["id"] == entry["id"] and back["missing"] is False
    assert back["path"] == str(music / "back.mp3")
    assert [p for p in lists if p["id"] == playlist["id"]][0]["track_ids"] == [entry["id"]]


@pytest.mark.asyncio
async def test_identical_audio_at_two_paths_is_one_entry_with_alt_paths(tmp_path):
    music = tmp_path / "Music"
    (music / "Copies").mkdir(parents=True)
    data = os.urandom(2048)
    (music / "a.mp3").write_bytes(data)
    (music / "Copies" / "a (1).mp3").write_bytes(data)
    (music / "other.mp3").write_bytes(os.urandom(2048))
    lib = _library(tmp_path / "dpc")
    await lib.open()
    try:
        folder = await lib.add_folder(str(music), scan=False)
        await _scan(lib, folder["id"])
        entries = lib.list_entries()
        await _scan(lib, folder["id"])  # a second scan adds nothing
        again = lib.list_entries()
        folder_state = lib.list_folders()[0]
    finally:
        await lib.stop()

    assert len(entries) == 2
    [dup] = [e for e in entries if e["alt_paths"]]
    paths = {dup["path"], *dup["alt_paths"]}
    assert paths == {str(music / "a.mp3"), str(music / "Copies" / "a (1).mp3")}
    assert sorted(e["id"] for e in again) == sorted(e["id"] for e in entries)
    assert folder_state["file_count"] == 3 and folder_state["hashed_count"] == 3


@pytest.mark.asyncio
async def test_when_the_primary_copy_goes_the_duplicate_takes_over(tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    data = os.urandom(2048)
    (music / "a.mp3").write_bytes(data)
    (music / "b.mp3").write_bytes(data)
    lib = _library(tmp_path / "dpc")
    await lib.open()
    try:
        folder = await lib.add_folder(str(music), scan=False)
        await _scan(lib, folder["id"])
        [entry] = lib.list_entries()
        os.unlink(entry["path"])
        await _scan(lib, folder["id"])
        [after] = lib.list_entries()
    finally:
        await lib.stop()
    assert after["id"] == entry["id"]
    assert after["missing"] is False and after["alt_paths"] == []
    assert after["path"] == entry["alt_paths"][0]


@pytest.mark.asyncio
async def test_removing_a_folder_drops_its_entries_and_never_the_files(tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"a")
    lib = _library(tmp_path / "dpc")
    await lib.open()
    try:
        folder = await lib.add_folder(str(music), scan=False)
        await _scan(lib, folder["id"])
        [entry] = lib.list_entries()
        playlist = await lib.create_playlist("P", [entry["id"]])
        removed = await lib.remove_folder(folder["id"])
        entries = lib.list_entries()
        lists = lib.list_playlists()
    finally:
        await lib.stop()
    assert removed == 1 and entries == [] and lib.list_folders() == []
    assert (music / "a.mp3").read_bytes() == b"a"
    assert [p for p in lists if p["id"] == playlist["id"]][0]["track_ids"] == []
