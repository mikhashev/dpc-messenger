"""Every record the core writes has the shape SPEC §4 declares for it, and a record
with an undeclared key, a wrong id or a broken conditional is refused.
"""

import copy

import pytest

from dpc_client_core.music import schemas
from dpc_client_core.music.identity import MediaTools
from dpc_client_core.music.library import MusicLibrary


@pytest.mark.asyncio
async def test_what_a_scan_writes_validates(tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"\xff\xfb fake mp3")
    lib = MusicLibrary(tmp_path / "dpc", tools=MediaTools(ffmpeg=None, ffprobe=None), save_delay_s=0.01)
    await lib.open()
    try:
        folder = await lib.add_folder(str(music), scan=False)
        await lib.rescan_folder(folder["id"])
        pending = lib.list_entries()
        await lib.wait_hashed()
        done = lib.list_entries()
        playlists = lib.list_playlists()
        await lib.create_playlist("x", [done[0]["id"]])
    finally:
        await lib.stop()

    for entry in pending + done:
        schemas.validate_library_entry(entry)
    schemas.validate_music_folder(lib.list_folders()[0])
    for playlist in playlists:
        schemas.validate_playlist(playlist)
    assert pending[0]["hash_state"] == "pending" and pending[0]["content_hash"] is None
    assert done[0]["hash_state"] == "done" and done[0]["hash_kind"] == "file"
    assert done[0]["source_hash"] == done[0]["content_hash"]


def _entry():
    return {
        "schema": "mp.library_entry/1", "id": "trk_0123456789abcdef", "title": "t",
        "source": {"kind": "local", "name": "t.mp3", "uri": "file:///t.mp3"},
        "source_hash": "sha256:00", "content_hash": "sha256:00", "duration_s": None,
        "added_at": "2026-11-19T00:00:00Z", "persist": "full", "placeholder": None,
        "origin": "folder", "path": "/t.mp3", "folder_id": "mf_0123456789abcdef",
        "hash_state": "done", "hash_kind": "audio_stream",
    }


@pytest.mark.parametrize("break_it, word", [
    (lambda e: e.update(extra=1), "undeclared"),
    (lambda e: e.update(id="trk_XYZ"), "trk_"),
    (lambda e: e.pop("title"), "missing"),
    (lambda e: e.update(hash_state="pending"), "pending"),
    (lambda e: e.update(folder_id=None), "folder entry"),
    (lambda e: e.update(hash_kind="crc32"), "hash_kind"),
    (lambda e: e["source"].update(kind="tape"), "source.kind"),
    (lambda e: e.update(file_stat={"size_bytes": 1}), "file_stat"),
])
def test_a_broken_library_entry_is_refused(break_it, word):
    entry = _entry()
    schemas.validate_library_entry(copy.deepcopy(entry))
    break_it(entry)
    with pytest.raises(schemas.SchemaError, match=word):
        schemas.validate_library_entry(entry)


def test_folder_watch_state_follows_watch():
    folder = {
        "schema": "mp.music_folder/1", "id": "mf_0123456789abcdef", "path": "/m",
        "added_at": "2026-11-19T00:00:00Z", "last_scan_at": None, "watch": False,
        "file_count": 0, "watch_state": "off",
    }
    schemas.validate_music_folder(folder)
    folder["watch_state"] = "polling"
    with pytest.raises(schemas.SchemaError, match="watch_state"):
        schemas.validate_music_folder(folder)


def test_a_smart_playlist_needs_its_rule_and_ids_are_track_ids():
    playlist = {
        "schema": "mp.playlist/1", "id": "pl_smart_all", "name": "All", "track_ids": [],
        "created_at": "x", "updated_at": "x", "kind": "smart",
    }
    with pytest.raises(schemas.SchemaError, match="rule"):
        schemas.validate_playlist(playlist)
    playlist["rule"] = "all"
    schemas.validate_playlist(playlist)
    playlist["track_ids"] = ["not-a-track"]
    with pytest.raises(schemas.SchemaError, match="track_ids"):
        schemas.validate_playlist(playlist)


def test_a_track_needs_a_duration():
    track = {
        "schema": "mp.track/1", "id": "trk_0123456789abcdef",
        "source": {"kind": "local", "name": "t", "uri": "file:///t"},
        "source_hash": "sha256:00", "editable": True, "duration_s": 1.5,
    }
    schemas.validate_track(track)
    track["duration_s"] = None
    with pytest.raises(schemas.SchemaError, match="duration_s"):
        schemas.validate_track(track)
