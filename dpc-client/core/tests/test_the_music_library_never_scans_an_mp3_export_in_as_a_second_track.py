"""The library never takes DPC's own MP3 exports in as tracks (`library.py`, `export.py`; Mike
#210 after the duplicate clutter of 2026-11-21): a file the export index lists is skipped by
path, a file tagged `DPC_EXPORT_OF` is dropped wherever it was copied, and the export
commands answer through the `local_api` door with the source resolved by track id."""

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from dpc_client_core.music import commands
from dpc_client_core.music.commands import MUSIC_COMMANDS, MusicCommandsMixin
from dpc_client_core.music.export import (
    MusicExporter, build_id3v24, default_export_root, txxx_frame,
)
from dpc_client_core.music.identity import MediaTools
from dpc_client_core.music.library import MusicLibrary


def _export_like(path: Path, source_hash: str = "md5:feed"):
    path.write_bytes(build_id3v24([txxx_frame("DPC_EXPORT_OF", source_hash)]) + b"\xff\xfb" + path.name.encode())


def _no_tools():
    return MediaTools(ffmpeg=None, ffprobe=None)


async def _library(home, folder, tools=None):
    lib = MusicLibrary(home, tools=tools or _no_tools(), save_delay_s=0.01)
    await lib.start(poll=False)
    added = await lib.add_folder(str(folder), scan=False)
    return lib, added["id"]


@pytest.mark.asyncio
async def test_a_tagged_export_in_a_watched_folder_is_left_out_and_not_read_again(tmp_path, caplog):
    music = tmp_path / "Music"
    music.mkdir()
    (music / "mine.mp3").write_bytes(b"\xff\xfbmy own song")
    _export_like(music / "Band - Song.mp3")
    lib, fid = await _library(tmp_path / "dpc", music)
    try:
        with caplog.at_level("INFO"):
            await lib.rescan_folder(fid)
            await lib.wait_hashed()
        names = [Path(e["path"]).name for e in lib.list_entries()]
        assert names == ["mine.mp3"]
        assert any("DPC export of md5:feed" in r.message for r in caplog.records)
        folder = await lib.rescan_folder(fid)
        await lib.wait_hashed()
        assert [Path(e["path"]).name for e in lib.list_entries()] == ["mine.mp3"]
        assert folder["file_count"] == 1
    finally:
        await lib.stop()


@pytest.mark.asyncio
async def test_a_file_the_export_index_lists_is_skipped_by_path(tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    (music / "listed.mp3").write_bytes(b"\xff\xfbno tag, but listed")
    (music / "other.mp3").write_bytes(b"\xff\xfbother")
    lib, fid = await _library(tmp_path / "dpc", music)
    lib.exclude_path = lambda p: "listed in the index" if Path(p).name == "listed.mp3" else None
    try:
        folder = await lib.rescan_folder(fid)
        await lib.wait_hashed()
        assert [Path(e["path"]).name for e in lib.list_entries()] == ["other.mp3"]
        assert folder["file_count"] == 1
    finally:
        await lib.stop()


@pytest.mark.asyncio
async def test_an_export_marker_in_any_format_is_caught_from_the_ffprobe_tags(tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    (music / "copy.flac").write_bytes(b"fLaC not really")

    def runner(cmd, *, timeout):
        if "ffprobe" in cmd[0]:
            out = json.dumps({"format": {"tags": {"DPC_EXPORT_OF": "md5:beef"}}, "streams": []}).encode()
            return subprocess.CompletedProcess(cmd, 0, out, b"")
        return subprocess.CompletedProcess(cmd, 1, b"", b"no")

    lib, fid = await _library(tmp_path / "dpc", music, MediaTools(ffmpeg="ffmpeg", ffprobe="ffprobe", runner=runner))
    try:
        await lib.rescan_folder(fid)
        await lib.wait_hashed()
        assert lib.list_entries() == []
    finally:
        await lib.stop()


def test_the_default_export_root_is_one_the_library_refuses_to_watch(tmp_path):
    home = tmp_path / "dpc"
    root = default_export_root(home)
    assert root == home / "exports" / "music"
    root.mkdir(parents=True)

    async def go():
        lib = MusicLibrary(home, tools=_no_tools(), save_delay_s=0.01)
        await lib.open()
        try:
            await lib.add_folder(str(root), scan=False)
        finally:
            await lib.stop()

    with pytest.raises(commands.MusicLibraryError) as e:
        asyncio.run(go())
    assert e.value.code == "protected_folder"


# -- the commands ---------------------------------------------------------------------------------


class _Settings:
    def __init__(self, home, export_dir=""):
        self.dpc_home_dir = home
        self.export_dir = export_dir

    def get(self, section, key, fallback=None):
        if (section, key) == ("music", "poll_interval_s"):
            return "0.05"
        if (section, key) == ("music", "export_dir"):
            return self.export_dir
        return fallback


class _Service(MusicCommandsMixin):
    def __init__(self, home, export_dir=""):
        self.settings = _Settings(home, export_dir)
        self.events = []

        class _Api:
            async def broadcast_event(api, event, payload):
                self.events.append((event, payload))
        self.local_api = _Api()


class _FakeFfmpeg:
    def __init__(self):
        self.encodes = 0

    def __call__(self, cmd, *, timeout):
        if "-encoders" in cmd:
            return subprocess.CompletedProcess(cmd, 0, b"libmp3lame", b"")
        if "-version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, b"ffmpeg version test", b"")
        self.encodes += 1
        Path(cmd[-1]).write_bytes(b"\xff\xfbencoded")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")


@pytest.fixture
def no_tools(monkeypatch):
    original = commands.MusicLibrary.__init__

    def init(self, home, **kw):
        kw.setdefault("tools", _no_tools())
        kw.setdefault("save_delay_s", 0.01)
        original(self, home, **kw)
    monkeypatch.setattr(commands.MusicLibrary, "__init__", init)


def test_the_export_commands_are_registered_slow_and_allowed():
    from dpc_client_core.local_api import ALLOWED_COMMANDS

    for name in ("export_music_track", "export_music_playlist", "get_music_export_info"):
        assert name in MUSIC_COMMANDS and name in ALLOWED_COMMANDS, name
        assert asyncio.iscoroutinefunction(getattr(MusicCommandsMixin, name)), name
        assert getattr(getattr(MusicCommandsMixin, name), "dpc_slow", False), name


@pytest.mark.asyncio
async def test_export_by_track_id_answers_the_file_once_and_info_lists_it(tmp_path, no_tools):
    music = tmp_path / "Music"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"\xff\xfbsong a")
    service = _Service(tmp_path / "dpc")
    fake = _FakeFfmpeg()
    root = default_export_root(tmp_path / "dpc")
    service._music_exporter_instance = MusicExporter(
        root, tools=MediaTools(ffmpeg="ffmpeg", ffprobe=None, runner=fake), measure=lambda p: {"true_peak_dbtp": -3.0})
    try:
        folder = (await service.add_music_folder(path=str(music)))["folder"]
        await service.rescan_music_folder(folder_id=folder["id"])
        await service._music_library_instance.wait_hashed()
        [entry] = (await service.list_music_library())["entries"]
        first = await service.export_music_track(track_id=entry["id"])
        assert first["status"] == "ok" and first["already_exported"] is False
        assert Path(first["path"]).parent == root and first["name"] == "a.mp3"
        second = await service.export_music_track(track_id=entry["id"], preset="mp3-v0")
        assert second["already_exported"] is True and fake.encodes == 1
        await asyncio.sleep(0)
        assert service.events == [("music_export_done", {"track_id": entry["id"], "path": first["path"]})]

        info = await service.get_music_export_info()
        assert info["status"] == "ok" and info["export_root"] == str(root)
        assert info["inside_watched_folder"] is False and info["default_preset"] == "mp3-v0"
        assert [p["id"] for p in info["presets"]] == ["mp3-v0", "mp3-320"]
        assert [x["name"] for x in info["exports"][entry["id"]]] == ["a.mp3"]
        one = await service.get_music_export_info(track_id=entry["id"])
        assert list(one["exports"]) == [entry["id"]]

        # the export is not scanned back in, even after a rescan
        await service.rescan_music_folder(folder_id=folder["id"])
        await service._music_library_instance.wait_hashed()
        assert len((await service.list_music_library())["entries"]) == 1

        playlist = (await service.create_music_playlist(name="P", track_ids=[entry["id"]]))["playlist"]
        exported = await service.export_music_playlist(playlist_id=playlist["id"])
        assert exported["status"] == "ok" and fake.encodes == 1
        assert [t["name"] for t in exported["tracks"]] == ["01 - a.mp3"]
        smart = await service.export_music_playlist(playlist_id="pl_smart_all")
        assert smart["status"] == "error" and smart["code"] == "smart_list"

        bad = await service.export_music_track(track_id=entry["id"], preset="flac")
        unknown = await service.export_music_track(track_id="trk_0000000000000000")
        assert bad["code"] == "invalid_preset" and unknown["code"] == "unknown_track"
    finally:
        await service.stop_music_library()


@pytest.mark.asyncio
async def test_an_export_dir_inside_a_watched_folder_is_reported_for_the_ui_to_warn(tmp_path, no_tools):
    music = tmp_path / "Music"
    (music / "exports").mkdir(parents=True)
    service = _Service(tmp_path / "dpc", export_dir=str(music / "exports"))
    try:
        await service.add_music_folder(path=str(music))
        info = await service.get_music_export_info()
        assert info["inside_watched_folder"] is True and info["watched_folder"] == str(music)
        assert info["export_root"] == str(music / "exports")
        service.settings.export_dir = "relative/dir"
        refused = await service.get_music_export_info()
        assert refused["status"] == "error" and refused["code"] == "invalid_export_dir"
    finally:
        await service.stop_music_library()
