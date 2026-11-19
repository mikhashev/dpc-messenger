"""A scan indexes a watched folder in place: audio files by extension in any case,
recursively, nothing copied, and the files' bytes and mtimes unchanged (ADR-051 Q4:
tags in watched folders are never written). Paths compare across Unicode forms
and, where the platform folds case, across case.
"""

import hashlib
import json
import os
import unicodedata

import pytest

from dpc_client_core.music.identity import MediaTools
from dpc_client_core.music.library import MusicLibrary, MusicLibraryError, path_key


def _library(home, tools=None):
    return MusicLibrary(home, tools=tools or MediaTools(ffmpeg=None, ffprobe=None), save_delay_s=0.01)


def _fingerprint(folder):
    out = {}
    for root, _dirs, files in os.walk(folder):
        for name in files:
            p = os.path.join(root, name)
            st = os.stat(p)
            with open(p, "rb") as f:
                out[p] = (hashlib.sha256(f.read()).hexdigest(), st.st_size, st.st_mtime_ns)
    return out


@pytest.mark.asyncio
async def test_a_scan_indexes_audio_files_in_place_and_skips_the_rest(tmp_path):
    music = tmp_path / "Music"
    (music / "Album" / "CD1").mkdir(parents=True)
    (music / "a.mp3").write_bytes(b"a-bytes")
    (music / "Album" / "CD1" / "B.FLAC").write_bytes(b"not a real flac")
    (music / "Album" / "c.Opus").write_bytes(b"c-bytes")
    (music / "Album" / "cover.jpg").write_bytes(b"jpeg")
    (music / "notes.txt").write_text("x")

    lib = _library(tmp_path / "dpc")
    await lib.open()
    try:
        folder = await lib.add_folder(str(music), scan=False)
        scanned = await lib.rescan_folder(folder["id"])
        await lib.wait_hashed()
        entries = lib.list_entries()
    finally:
        await lib.stop()

    names = sorted(e["source"]["name"] for e in entries)
    assert names == ["B.FLAC", "a.mp3", "c.Opus"]
    assert scanned["file_count"] == 3 and scanned["last_scan_at"]
    assert scanned["watch_state"] == "polling"
    for e in entries:
        assert e["origin"] == "folder" and e["folder_id"] == folder["id"]
        assert os.path.isfile(e["path"]) and str(music) in e["path"]  # indexed in place
        assert e["missing"] is False and e["hash_state"] == "done"
        assert e["title"] == os.path.splitext(e["source"]["name"])[0]  # no ffprobe: the file name
    # Nothing was copied into the library folder.
    assert not (tmp_path / "dpc" / "music" / "files").exists()
    # The first scan's state is on disk.
    on_disk = json.loads((tmp_path / "dpc" / "music" / "folders.json").read_text(encoding="utf-8"))
    assert on_disk[0]["file_count"] == 3


@pytest.mark.asyncio
async def test_a_scan_and_a_hash_leave_every_file_byte_for_byte_as_it_was(tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    for i in range(3):
        (music / f"t{i}.mp3").write_bytes(os.urandom(2048))
    before = _fingerprint(music)

    lib = _library(tmp_path / "dpc", tools=MediaTools.discover())  # real ffmpeg and ffprobe when present
    await lib.open()
    try:
        folder = await lib.add_folder(str(music), scan=False)
        await lib.rescan_folder(folder["id"])
        await lib.wait_hashed()
        await lib.get_track(lib.list_entries()[0]["id"])
        await lib.rescan_folder(folder["id"])
        await lib.wait_hashed()
    finally:
        await lib.stop()

    assert _fingerprint(music) == before


@pytest.mark.asyncio
async def test_an_unreachable_folder_is_marked_unavailable_and_its_tracks_stay(tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"a")
    lib = _library(tmp_path / "dpc")
    await lib.open()
    try:
        folder = await lib.add_folder(str(music), scan=False)
        await lib.rescan_folder(folder["id"])
        await lib.wait_hashed()
        (music / "a.mp3").unlink()
        music.rmdir()  # the drive is unplugged
        state = await lib.rescan_folder(folder["id"])
        entries = lib.list_entries()
    finally:
        await lib.stop()
    assert state["watch_state"] == "unavailable" and state["state_reason"]
    assert len(entries) == 1 and entries[0]["missing"] is False


@pytest.mark.asyncio
async def test_a_folder_cannot_be_added_twice_or_nested(tmp_path):
    music = tmp_path / "Music"
    (music / "Sub").mkdir(parents=True)
    lib = _library(tmp_path / "dpc")
    await lib.open()
    try:
        await lib.add_folder(str(music), scan=False)
        with pytest.raises(MusicLibraryError) as same:
            await lib.add_folder(str(music) + os.sep, scan=False)
        with pytest.raises(MusicLibraryError) as nested:
            await lib.add_folder(str(music / "Sub"), scan=False)
        with pytest.raises(MusicLibraryError) as relative:
            await lib.add_folder("Music", scan=False)
        with pytest.raises(MusicLibraryError) as absent:
            await lib.add_folder(str(tmp_path / "nowhere"), scan=False)
    finally:
        await lib.stop()
    assert same.value.code == "already_added"
    assert nested.value.code == "nested_folder"
    assert relative.value.code == "invalid_path"
    assert absent.value.code == "not_a_folder"


@pytest.mark.asyncio
async def test_a_stored_nfd_path_matches_the_nfc_name_on_disk(tmp_path):
    # macOS hands out decomposed (NFD) names; a library written there and a listing
    # in NFC must still be one track, not a new entry plus a missing one.
    music = tmp_path / "Music"
    music.mkdir()
    nfc_name = unicodedata.normalize("NFC", "Тысячи умов – café.mp3")
    (music / nfc_name).write_bytes(b"song")
    lib = _library(tmp_path / "dpc")
    await lib.open()
    try:
        folder = await lib.add_folder(str(music), scan=False)
        await lib.rescan_folder(folder["id"])
        await lib.wait_hashed()
        [entry] = lib.list_entries()
        stored = lib._entries[entry["id"]]
        stored["path"] = unicodedata.normalize("NFD", stored["path"])
        assert stored["path"] != str(music / nfc_name)

        await lib.rescan_folder(folder["id"])
        await lib.wait_hashed()
        entries = lib.list_entries()
    finally:
        await lib.stop()
    assert [e["id"] for e in entries] == [entry["id"]]
    assert entries[0]["missing"] is False


def test_path_keys_fold_case_only_where_the_platform_does():
    folds = os.path.normcase("A") == "a"
    assert (path_key("/M/Song.MP3") == path_key("/m/song.mp3")) is folds
    assert path_key(unicodedata.normalize("NFD", "/m/é.mp3")) == path_key("/m/é.mp3")


@pytest.mark.asyncio
@pytest.mark.skipif(os.path.normcase("A") != "a", reason="paths are case-sensitive on this platform")
async def test_a_case_only_difference_is_the_same_file_on_windows(tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    (music / "song.mp3").write_bytes(b"x")
    lib = _library(tmp_path / "dpc")
    await lib.open()
    try:
        folder = await lib.add_folder(str(music), scan=False)
        await lib.rescan_folder(folder["id"])
        await lib.wait_hashed()
        [entry] = lib.list_entries()
        lib._entries[entry["id"]]["path"] = str(music / "SONG.MP3")
        await lib.rescan_folder(folder["id"])
        await lib.wait_hashed()
        entries = lib.list_entries()
    finally:
        await lib.stop()
    assert [e["id"] for e in entries] == [entry["id"]] and not entries[0]["missing"]
