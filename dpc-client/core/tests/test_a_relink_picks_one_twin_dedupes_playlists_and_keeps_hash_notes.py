"""Three small identity rules: two missing entries with one audio hash relink
deterministically (the likeliest one takes the path, the other merges into it); a
collapse leaves no duplicate position in a playlist; and why a hash is the kind it is
(`hash_notes`) is saved in the entry.
"""

import copy
import json
import os
import subprocess

import pytest

from dpc_client_core.music.identity import MediaTools
from dpc_client_core.music.library import MusicLibrary


def _library(home, tools=None):
    return MusicLibrary(home, tools=tools or MediaTools(ffmpeg=None, ffprobe=None), save_delay_s=0.01)


async def _scan(lib, folder_id):
    await lib.rescan_folder(folder_id)
    await lib.wait_hashed()


def _playlist(pl_id, track_ids):
    return {"schema": "mp.playlist/1", "id": pl_id, "name": pl_id, "track_ids": track_ids,
            "created_at": "2026-11-19T00:00:00Z", "updated_at": "2026-11-19T00:00:00Z", "kind": "user"}


@pytest.mark.asyncio
async def test_two_missing_entries_with_one_hash_relink_the_likeliest_and_merge_the_other(tmp_path):
    home = tmp_path / "dpc"
    music = tmp_path / "Music"
    music.mkdir()
    data = os.urandom(1024)
    (music / "song.mp3").write_bytes(data)
    lib = _library(home)
    await lib.open()
    folder = await lib.add_folder(str(music), scan=False)
    await _scan(lib, folder["id"])
    (music / "song.mp3").unlink()
    await _scan(lib, folder["id"])
    [a] = lib.list_entries()
    await lib.stop()
    assert a["missing"] is True

    # A twin from an older library: the same audio, another name, added earlier.
    twin = copy.deepcopy(a)
    twin["id"] = "trk_0000000000000001"
    twin["path"] = str(music / "Old" / "other name.mp3")
    twin["added_at"] = "2020-01-01T00:00:00.000Z"
    records = json.loads(lib.library_path.read_text(encoding="utf-8"))
    lib.library_path.write_text(json.dumps([twin, *records]), encoding="utf-8")
    lib.playlists_path.write_text(json.dumps([
        _playlist("pl_both", [a["id"], twin["id"]]),
        _playlist("pl_twin", [twin["id"]]),
    ]), encoding="utf-8")

    again = _library(home)
    await again.open()
    try:
        (music / "Moved").mkdir()
        (music / "Moved" / "song.mp3").write_bytes(data)
        await _scan(again, folder["id"])
        entries = again.list_entries()
        lists = {p["id"]: p["track_ids"] for p in again.list_playlists() if p["kind"] == "user"}
    finally:
        await again.stop()

    # The same file name wins over the older entry; the twin is merged, not left missing.
    assert [(e["id"], e["missing"], e["path"]) for e in entries] == [
        (a["id"], False, str(music / "Moved" / "song.mp3"))]
    assert lists == {"pl_both": [a["id"]], "pl_twin": [a["id"]]}


@pytest.mark.asyncio
async def test_a_collapsed_duplicate_leaves_no_duplicate_playlist_position(tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    data = os.urandom(1024)
    (music / "a.mp3").write_bytes(data)
    lib = _library(tmp_path / "dpc")
    await lib.open()
    try:
        folder = await lib.add_folder(str(music), scan=False)
        await _scan(lib, folder["id"])
        [a] = lib.list_entries()
        (music / "copy.mp3").write_bytes(data)
        await lib.rescan_folder(folder["id"])
        # The copy is a pending entry until hashed; a playlist can hold it meanwhile.
        [new_id] = [i for i in lib._entries if i != a["id"]]
        lib._playlists["pl_mix"] = _playlist("pl_mix", [a["id"], new_id])
        lib._playlists["pl_new"] = _playlist("pl_new", [new_id])
        lib._playlists["pl_repeat"] = _playlist("pl_repeat", [a["id"], a["id"], new_id])
        await lib.wait_hashed()
        entries = lib.list_entries()
        lists = {p["id"]: p["track_ids"] for p in lib.list_playlists() if p["kind"] == "user"}
    finally:
        await lib.stop()
    assert [e["id"] for e in entries] == [a["id"]]
    assert entries[0]["alt_paths"] == [str(music / "copy.mp3")]
    assert lists["pl_mix"] == [a["id"]]
    assert lists["pl_new"] == [a["id"]]
    assert lists["pl_repeat"] == [a["id"], a["id"]]  # the person's own repeat stays


@pytest.mark.asyncio
async def test_hash_notes_are_saved_in_the_entry(tmp_path):
    def ffmpeg_fails(cmd, *, timeout):
        return subprocess.CompletedProcess(cmd, 1, b"", b"Invalid data found")

    music = tmp_path / "Music"
    music.mkdir()
    (music / "a.mp3").write_bytes(os.urandom(256))
    (music / "b.mp3").write_bytes(os.urandom(256))
    home = tmp_path / "dpc"
    absent = _library(home)
    await absent.open()
    folder = await absent.add_folder(str(music), scan=False)
    await _scan(absent, folder["id"])
    await absent.stop()
    saved = json.loads(absent.library_path.read_text(encoding="utf-8"))

    failing = _library(tmp_path / "dpc2", tools=MediaTools(ffmpeg="ffmpeg", ffprobe=None, runner=ffmpeg_fails))
    await failing.open()
    try:
        folder2 = await failing.add_folder(str(music), scan=False)
        await _scan(failing, folder2["id"])
        failed_notes = [e["hash_notes"] for e in failing.list_entries()]
    finally:
        await failing.stop()

    assert [e["hash_notes"] for e in saved] == [["ffmpeg absent"], ["ffmpeg absent"]]
    assert all(e["hash_kind"] == "file" for e in saved)
    assert failed_notes == [["ffmpeg could not read the audio"]] * 2
