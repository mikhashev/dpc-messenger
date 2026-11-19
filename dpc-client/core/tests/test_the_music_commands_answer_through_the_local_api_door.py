"""The music library's `local_api` door (ADR-051 D11): each command is a coroutine
on the mixin, answers `{"status": "ok"}` or `{"status": "error", "code", "message"}`,
and keeps the library under the DPC home the service's settings name.
"""

import asyncio

import pytest

from dpc_client_core.music import commands
from dpc_client_core.music.commands import MUSIC_COMMANDS, MusicCommandsMixin
from dpc_client_core.music.identity import MediaTools


class _Settings:
    def __init__(self, home, poll="0.05"):
        self.dpc_home_dir = home
        self._poll = poll

    def get(self, section, key, fallback=None):
        if (section, key) == ("music", "poll_interval_s"):
            return self._poll
        return fallback


class _Service(MusicCommandsMixin):
    def __init__(self, home, poll="0.05"):
        self.settings = _Settings(home, poll)


@pytest.fixture
def no_tools(monkeypatch):
    monkeypatch.setattr(commands.MusicLibrary, "__init__", _init_without_tools(commands.MusicLibrary.__init__))


def _init_without_tools(original):
    def init(self, home, **kw):
        kw.setdefault("tools", MediaTools(ffmpeg=None, ffprobe=None))
        kw.setdefault("save_delay_s", 0.01)
        original(self, home, **kw)
    return init


def test_every_command_is_a_coroutine_on_the_mixin():
    for name in MUSIC_COMMANDS:
        assert asyncio.iscoroutinefunction(getattr(MusicCommandsMixin, name)), name
    assert getattr(MusicCommandsMixin.rescan_music_folder, "dpc_slow", False)


@pytest.mark.asyncio
async def test_the_commands_round_trip(tmp_path, no_tools):
    music = tmp_path / "Music"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"a")
    service = _Service(tmp_path / "dpc")
    try:
        added = await service.add_music_folder(path=str(music))
        assert added["status"] == "ok"
        folder_id = added["folder"]["id"]
        rescanned = await service.rescan_music_folder(folder_id=folder_id)
        assert rescanned["status"] == "ok" and rescanned["folder"]["file_count"] == 1
        await service._music_library_instance.wait_hashed()

        [entry] = (await service.list_music_library())["entries"]
        got = await service.get_music_track(track_id=entry["id"])
        assert got["status"] == "ok" and got["entry"]["id"] == entry["id"]
        assert got["track"] is None  # no ffprobe, no duration

        created = await service.create_music_playlist(name="P", track_ids=[entry["id"]])
        playlist_id = created["playlist"]["id"]
        updated = await service.update_music_playlist(playlist_id=playlist_id, name="Q")
        assert updated["playlist"]["name"] == "Q"
        listed = await service.list_music_playlists()
        assert [p["id"] for p in listed["playlists"]][-1] == playlist_id
        assert (await service.delete_music_playlist(playlist_id=playlist_id))["status"] == "ok"

        assert len((await service.list_music_folders())["folders"]) == 1
        all_folders = await service.rescan_music_folder()
        assert all_folders["status"] == "ok" and len(all_folders["folders"]) == 1
        removed = await service.remove_music_folder(folder_id=folder_id)
        assert removed == {"status": "ok", "removed_tracks": 1}
    finally:
        await service.stop_music_library()
    assert (tmp_path / "dpc" / "music" / "folders.json").exists()


@pytest.mark.asyncio
async def test_a_refusal_is_an_error_answer_not_an_exception(tmp_path, no_tools):
    service = _Service(tmp_path / "dpc")
    try:
        answer = await service.get_music_track(track_id="trk_0000000000000000")
        bad_folder = await service.add_music_folder(path="relative/path")
    finally:
        await service.stop_music_library()
    assert answer["status"] == "error" and answer["code"] == "unknown_track"
    assert bad_folder["status"] == "error" and bad_folder["code"] == "invalid_path"


def test_a_bad_poll_interval_falls_back_to_the_default(tmp_path):
    service = _Service(tmp_path / "dpc", poll="soon")
    assert service._music_poll_interval_s() == commands.DEFAULT_POLL_INTERVAL_S


def test_the_commands_are_wired_into_the_core_service_and_the_allowlist():
    from dpc_client_core.local_api import ALLOWED_COMMANDS
    from dpc_client_core.service import CoreService

    assert issubclass(CoreService, MusicCommandsMixin)
    assert set(MUSIC_COMMANDS) <= ALLOWED_COMMANDS


def test_the_commands_that_wait_on_ffprobe_or_the_disk_walk_do_not_hold_the_socket():
    # local_api awaits an unmarked handler inline, so one probe would hold back
    # every request behind it on the same socket.
    for name in ("get_music_track", "rescan_music_folder", "get_music_lyrics"):
        assert getattr(getattr(MusicCommandsMixin, name), "dpc_slow", False), name
