"""ADR-051 D21: a generated take enters the music library once.

`MusicLibrary.register_generated` is the only entry point and the only place that sets
`origin: "generated"`; the "Generated" smart list is derived from it on read. A repeat
call, a file the library already holds, and a later folder scan all leave one entry.
"""

import asyncio
import os

import pytest

from dpc_client_core.music import schemas
from dpc_client_core.music.identity import MediaTools
from dpc_client_core.music.library import (
    SMART_ALL_ID, SMART_GENERATED_ID, MusicLibrary, MusicLibraryError,
)


def _library(home, **kwargs):
    return MusicLibrary(home, tools=MediaTools(ffmpeg=None, ffprobe=None), save_delay_s=0.01, **kwargs)


def _take(folder, name="take1.mp3", size=700):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(os.urandom(size))
    return str(path)


def _smart_ids(lib, list_id):
    return next(p for p in lib.list_playlists() if p["id"] == list_id)["track_ids"]


@pytest.mark.asyncio
async def test_registering_a_take_twice_yields_one_entry_and_one_event(tmp_path):
    events = []
    lib = _library(tmp_path / "dpc", on_added=events.append)
    await lib.open()
    try:
        path = _take(tmp_path / "dpc" / "music" / "generated" / "job_1")
        first = await lib.register_generated(path, job_id="job_1", lineage={"model": "m"})
        second = await lib.register_generated(path, job_id="job_1", lineage={"model": "m"})
        entries = lib.list_entries()
    finally:
        await lib.stop()
    assert second["id"] == first["id"]
    assert [e["id"] for e in entries] == [first["id"]]
    assert [e["id"] for e in events] == [first["id"]]
    assert first["origin"] == "generated" and first["hash_state"] == "done"
    assert first["source"]["kind"] == "generated"
    assert first["source"]["lineage"] == {"model": "m", "job_id": "job_1"}
    schemas.validate_library_entry(entries[0])


@pytest.mark.asyncio
async def test_two_registrations_at_once_yield_one_entry_and_one_event(tmp_path):
    events = []
    lib = _library(tmp_path / "dpc", on_added=events.append)
    await lib.open()
    try:
        path = _take(tmp_path / "dpc" / "music" / "generated" / "job_1")
        a, b = await asyncio.gather(
            lib.register_generated(path, job_id="job_1"), lib.register_generated(path, job_id="job_1"))
        entries = lib.list_entries()
    finally:
        await lib.stop()
    assert a["id"] == b["id"] and len(entries) == 1 and len(events) == 1


@pytest.mark.asyncio
async def test_the_same_audio_at_a_second_path_is_the_one_entry_with_an_alt_path(tmp_path):
    events = []
    lib = _library(tmp_path / "dpc", on_added=events.append)
    await lib.open()
    try:
        first_path = _take(tmp_path / "gen" / "job_1")
        copy_dir = tmp_path / "gen" / "job_2"
        copy_dir.mkdir()
        copy_path = str(copy_dir / "take1.mp3")
        with open(first_path, "rb") as src, open(copy_path, "wb") as dst:
            dst.write(src.read())
        first = await lib.register_generated(first_path, job_id="job_1")
        again = await lib.register_generated(copy_path, job_id="job_2")
        entries = lib.list_entries()
    finally:
        await lib.stop()
    assert again["id"] == first["id"] and len(entries) == 1 and len(events) == 1
    assert entries[0]["alt_paths"] == [copy_path]
    assert entries[0]["source"]["lineage"]["job_id"] == "job_1"


@pytest.mark.asyncio
async def test_a_later_folder_scan_keeps_one_entry_with_origin_generated(tmp_path):
    events = []
    lib = _library(tmp_path / "dpc", on_added=events.append)
    await lib.open()
    try:
        watched = tmp_path / "Music"
        path = _take(watched)
        take = await lib.register_generated(path, job_id="job_1")
        folder = await lib.add_folder(str(watched), scan=False)
        await lib.rescan_folder(folder["id"])
        await lib.wait_hashed()
        await lib.rescan_folder(folder["id"])
        await lib.wait_hashed()
        entries = lib.list_entries()
        shown = lib.list_folders()[0]
    finally:
        await lib.stop()
    assert [e["id"] for e in entries] == [take["id"]]
    assert entries[0]["origin"] == "generated" and entries[0]["hash_state"] == "done"
    assert entries[0]["missing"] is False and entries[0]["alt_paths"] == []
    assert shown["file_count"] == 1 and shown["hashed_count"] == 1
    assert len(events) == 1


@pytest.mark.asyncio
async def test_a_file_a_folder_scan_took_first_is_returned_as_it_is_and_not_flipped(tmp_path):
    events = []
    lib = _library(tmp_path / "dpc", on_added=events.append)
    await lib.open()
    try:
        watched = tmp_path / "Music"
        path = _take(watched)
        folder = await lib.add_folder(str(watched), scan=False)
        await lib.rescan_folder(folder["id"])
        await lib.wait_hashed()
        [scanned] = lib.list_entries()
        got = await lib.register_generated(path, job_id="job_1")
        entries = lib.list_entries()
    finally:
        await lib.stop()
    assert got["id"] == scanned["id"] and len(entries) == 1
    assert entries[0]["origin"] == "folder"
    assert events == []


@pytest.mark.asyncio
async def test_the_generated_smart_list_shows_a_take_once(tmp_path):
    lib = _library(tmp_path / "dpc")
    await lib.open()
    try:
        watched = tmp_path / "Music"
        taken = _take(watched, "take1.mp3")
        other = _take(watched, "song.mp3")
        take = await lib.register_generated(taken, job_id="job_1")
        await lib.register_generated(taken, job_id="job_1")
        folder = await lib.add_folder(str(watched), scan=False)
        await lib.rescan_folder(folder["id"])
        await lib.wait_hashed()
        generated = _smart_ids(lib, SMART_GENERATED_ID)
        everything = _smart_ids(lib, SMART_ALL_ID)
        entries = lib.list_entries()
    finally:
        await lib.stop()
    assert generated == [take["id"]]
    assert len(everything) == 2 and len(set(everything)) == 2
    assert [e["origin"] for e in entries if e["path"] == other] == ["folder"]


@pytest.mark.asyncio
async def test_a_generated_take_survives_a_reload_and_removing_a_watched_folder(tmp_path):
    home = tmp_path / "dpc"
    watched = tmp_path / "Music"
    lib = _library(home)
    await lib.open()
    try:
        take = await lib.register_generated(_take(watched), job_id="job_1")
        folder = await lib.add_folder(str(watched), scan=False)
        await lib.rescan_folder(folder["id"])
        await lib.wait_hashed()
        await lib.remove_folder(folder["id"])
    finally:
        await lib.stop()
    reloaded = _library(home)
    await reloaded.open()
    try:
        entries = reloaded.list_entries()
        generated = _smart_ids(reloaded, SMART_GENERATED_ID)
        repeat = await reloaded.register_generated(take["path"], job_id="job_1")
        after = reloaded.list_entries()
    finally:
        await reloaded.stop()
    assert [e["id"] for e in entries] == [take["id"]] and generated == [take["id"]]
    assert repeat["id"] == take["id"] and len(after) == 1


@pytest.mark.asyncio
async def test_a_bad_take_path_is_refused_and_adds_nothing(tmp_path):
    events = []
    lib = _library(tmp_path / "dpc", on_added=events.append)
    await lib.open()
    try:
        (tmp_path / "notes.txt").write_text("x")
        cases = [
            ("relative.mp3", "job_1", "invalid_path"),
            (str(tmp_path / "gone.mp3"), "job_1", "unreadable"),
            (str(tmp_path / "notes.txt"), "job_1", "not_audio"),
            (_take(tmp_path / "gen"), " ", "invalid_job"),
        ]
        for path, job, code in cases:
            with pytest.raises(MusicLibraryError) as raised:
                await lib.register_generated(path, job_id=job)
            assert raised.value.code == code
        assert lib.list_entries() == [] and events == []
    finally:
        await lib.stop()
