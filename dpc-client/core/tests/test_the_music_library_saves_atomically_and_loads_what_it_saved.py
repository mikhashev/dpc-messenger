"""The music library's three files round-trip, are replaced in one step, and a bad
file or record is set aside rather than lost (ADR-051 Phase 0, SPEC §6 storage).
"""

import json
import os

import pytest

from dpc_client_core.music import store
from dpc_client_core.music.identity import MediaTools
from dpc_client_core.music.library import MusicLibrary


def _library(home):
    return MusicLibrary(home, tools=MediaTools(ffmpeg=None, ffprobe=None), save_delay_s=0.01)


@pytest.mark.asyncio
async def test_folders_playlists_and_entries_survive_a_restart(tmp_path):
    home = tmp_path / "dpc"
    music = tmp_path / "Music"
    music.mkdir()
    (music / "one.mp3").write_bytes(b"ID3 not really audio 1")
    (music / "two.wav").write_bytes(b"RIFF not really audio 2")

    lib = _library(home)
    await lib.open()
    folder = await lib.add_folder(str(music), scan=False)
    await lib.rescan_folder(folder["id"])
    await lib.wait_hashed()
    ids = [e["id"] for e in lib.list_entries()]
    playlist = await lib.create_playlist("Evening", ids)
    await lib.stop()

    assert (home / "music" / "library.json").exists()
    assert (home / "music" / "folders.json").exists()
    assert (home / "music" / "playlists.json").exists()

    again = _library(home)
    await again.open()
    try:
        assert [f["id"] for f in again.list_folders()] == [folder["id"]]
        assert sorted(e["id"] for e in again.list_entries()) == sorted(ids)
        assert all(e["hash_state"] == "done" for e in again.list_entries())
        user_lists = [p for p in again.list_playlists() if p["kind"] == "user"]
        assert user_lists == [playlist]
    finally:
        await again.stop()


def test_a_write_leaves_no_temp_file_and_a_failed_replace_keeps_the_old_file(tmp_path, monkeypatch):
    target = tmp_path / "music" / "library.json"
    store.atomic_write_json(target, [{"a": 1}])
    assert json.loads(target.read_text(encoding="utf-8")) == [{"a": 1}]
    assert [p.name for p in target.parent.iterdir()] == ["library.json"]

    def refuse(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(store.os, "replace", refuse)
    with pytest.raises(OSError):
        store.atomic_write_json(target, [{"a": 2}])
    monkeypatch.undo()
    assert json.loads(target.read_text(encoding="utf-8")) == [{"a": 1}]
    assert [p.name for p in target.parent.iterdir()] == ["library.json"]


def test_a_file_that_does_not_parse_is_moved_aside_not_overwritten(tmp_path):
    home = tmp_path / "dpc"
    music_dir = home / "music"
    music_dir.mkdir(parents=True)
    (music_dir / "library.json").write_text("{ half a file", encoding="utf-8")

    lib = _library(home)
    lib.load()

    assert lib.list_entries() == []
    aside = [p.name for p in music_dir.iterdir() if p.name.startswith("library.json.corrupt-")]
    assert len(aside) == 1
    assert (music_dir / aside[0]).read_text(encoding="utf-8") == "{ half a file"


def test_a_record_that_fails_its_schema_is_kept_aside(tmp_path):
    home = tmp_path / "dpc"
    music_dir = home / "music"
    music_dir.mkdir(parents=True)
    good = {
        "schema": "mp.playlist/1", "id": "pl_0123456789abcdef", "name": "Kept", "track_ids": [],
        "created_at": "2026-11-19T00:00:00Z", "updated_at": "2026-11-19T00:00:00Z", "kind": "user",
    }
    bad = dict(good, id="pl_bad", name=None)
    (music_dir / "playlists.json").write_text(json.dumps([good, bad]), encoding="utf-8")

    lib = _library(home)
    lib.load()

    assert [p["id"] for p in lib.list_playlists() if p["kind"] == "user"] == ["pl_0123456789abcdef"]
    rejected = [p for p in music_dir.iterdir() if p.name.startswith("playlists.json.rejected-")]
    assert len(rejected) == 1
    assert json.loads(rejected[0].read_text(encoding="utf-8")) == [bad]


def test_the_library_lives_under_the_dpc_home_it_is_given(tmp_path):
    lib = MusicLibrary(tmp_path / "elsewhere")
    assert lib.library_path == tmp_path / "elsewhere" / "music" / "library.json"
    assert os.path.basename(lib.folders_path) == "folders.json"
    assert os.path.basename(lib.playlists_path) == "playlists.json"
