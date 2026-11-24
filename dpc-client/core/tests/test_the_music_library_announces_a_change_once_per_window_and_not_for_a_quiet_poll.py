"""`music_library_changed` (Mike #84-3: events instead of the UI's periodic re-read): every
change to what the library lists announces itself once per coalescing window, carrying a
version and the ids it knows; a poll that finds nothing announces nothing; export changes
reach the same event through the service."""

import asyncio
import os

import pytest

from dpc_client_core.music.commands import MusicCommandsMixin
from dpc_client_core.music.identity import MediaTools
from dpc_client_core.music.library import MusicLibrary
from tests._music_hide_helpers import folder_with_tracks, generated_dir, take

DELAY = 0.15


def _library(home, **kw):
    events = []
    lib = MusicLibrary(home, tools=MediaTools(ffmpeg=None, ffprobe=None), save_delay_s=0.01,
                       on_changed=events.append, change_delay_s=DELAY, **kw)
    return lib, events


async def _quiet():
    await asyncio.sleep(DELAY * 3)


@pytest.mark.asyncio
async def test_a_burst_of_changes_is_one_event_with_the_union_of_what_happened(tmp_path):
    lib, events = _library(tmp_path / "dpc")
    await lib.open()
    try:
        _, ids = await folder_with_tracks(lib, tmp_path / "Music")
        await _quiet()
        events.clear()

        a, b = ids["a.mp3"], ids["b.mp3"]
        await lib.set_hidden(a, True)
        await lib.set_hidden(a, False)
        await lib.set_hidden(b, True)
        playlist = await lib.create_playlist("P", [a])
        await lib.update_playlist(playlist["id"], name="Q")
        await lib.delete_playlist(playlist["id"])
        assert events == []
        await _quiet()

        assert len(events) == 1
        [event] = events
        assert event["reasons"] == ["playlist", "track"]
        assert event["track_ids"] == sorted([a, b]) and event["track_ids_truncated"] is False
        assert event["version"] == lib.change_version
    finally:
        await lib.stop()


@pytest.mark.asyncio
async def test_changes_in_separate_windows_are_separate_events_with_rising_versions(tmp_path):
    lib, events = _library(tmp_path / "dpc")
    await lib.open()
    try:
        _, ids = await folder_with_tracks(lib, tmp_path / "Music")
        await _quiet()
        events.clear()
        first = lib.change_version
        await lib.set_hidden(ids["a.mp3"], True)
        await _quiet()
        await lib.set_hidden(ids["a.mp3"], False)
        await _quiet()
        assert [e["version"] for e in events] == [first + 1, first + 2]
    finally:
        await lib.stop()


@pytest.mark.asyncio
async def test_a_change_that_changes_nothing_announces_nothing(tmp_path):
    lib, events = _library(tmp_path / "dpc")
    await lib.open()
    try:
        _, ids = await folder_with_tracks(lib, tmp_path / "Music")
        await _quiet()
        events.clear()
        await lib.set_hidden(ids["a.mp3"], False)
        await lib.remove_missing()
        await _quiet()
        assert events == []
    finally:
        await lib.stop()


@pytest.mark.asyncio
async def test_a_poll_that_finds_nothing_is_silent_and_one_that_finds_a_file_is_not(tmp_path):
    lib, events = _library(tmp_path / "dpc")
    await lib.open()
    try:
        folder, _ = await folder_with_tracks(lib, tmp_path / "Music")
        await lib.rescan_folder(folder["id"])  # the folder's hashed count settles on this one
        await _quiet()
        events.clear()

        for _ in range(3):
            await lib.rescan_folder(folder["id"])
        await _quiet()
        assert events == []

        (tmp_path / "Music" / "c.mp3").write_bytes(os.urandom(300))
        await lib.rescan_folder(folder["id"])
        await lib.wait_hashed()
        await _quiet()
        assert len(events) == 1
        assert {"scan", "hash"} & set(events[0]["reasons"])

        events.clear()
        (tmp_path / "Music" / "c.mp3").unlink()
        await lib.rescan_folder(folder["id"])
        await _quiet()
        assert len(events) == 1 and events[0]["reasons"] == ["scan"]
    finally:
        await lib.stop()


@pytest.mark.asyncio
async def test_a_folder_added_and_removed_and_a_take_kept_each_announce(tmp_path):
    lib, events = _library(tmp_path / "dpc")
    await lib.open()
    try:
        music = tmp_path / "Music"
        music.mkdir()
        folder = await lib.add_folder(str(music), scan=False)
        await _quiet()
        assert [e["reasons"] for e in events] == [["folder"]]
        events.clear()

        entry = await lib.register_generated(str(take(generated_dir(tmp_path / "dpc"))), job_id="job_1")
        await _quiet()
        assert len(events) == 1 and events[0]["reasons"] == ["take"] and events[0]["track_ids"] == [entry["id"]]
        events.clear()

        await lib.register_generated(entry["path"], job_id="job_1")
        await _quiet()
        assert events == []

        await lib.delete_generated_file(entry["id"])
        await _quiet()
        assert len(events) == 1 and events[0]["reasons"] == ["track"]
        events.clear()

        await lib.remove_folder(folder["id"])
        await _quiet()
        assert [e["reasons"] for e in events] == [["folder"]]
    finally:
        await lib.stop()


@pytest.mark.asyncio
async def test_a_removed_missing_track_and_a_late_hash_announce(tmp_path):
    lib, events = _library(tmp_path / "dpc")
    await lib.open()
    try:
        folder, ids = await folder_with_tracks(lib, tmp_path / "Music")
        await _quiet()
        events.clear()
        (tmp_path / "Music" / "a.mp3").unlink()
        await lib.rescan_folder(folder["id"])
        await _quiet()
        assert len(events) == 1 and lib.get_entry(ids["a.mp3"])["missing"] is True
        events.clear()

        await lib.remove_missing(ids["a.mp3"])
        await _quiet()
        assert len(events) == 1 and events[0]["track_ids"] == [ids["a.mp3"]]
    finally:
        await lib.stop()


@pytest.mark.asyncio
async def test_a_failing_listener_does_not_break_the_library(tmp_path):
    def boom(_payload):
        raise RuntimeError("listener failed")

    lib = MusicLibrary(tmp_path / "dpc", tools=MediaTools(ffmpeg=None, ffprobe=None), save_delay_s=0.01,
                       on_changed=boom, change_delay_s=0.01)
    await lib.open()
    try:
        _, ids = await folder_with_tracks(lib, tmp_path / "Music")
        await lib.set_hidden(ids["a.mp3"], True)
        await asyncio.sleep(0.1)
        assert lib.get_entry(ids["a.mp3"])["hidden"] is True
    finally:
        await lib.stop()


@pytest.mark.asyncio
async def test_stopping_the_library_cancels_a_pending_event(tmp_path):
    lib, events = _library(tmp_path / "dpc")
    await lib.open()
    _, ids = await folder_with_tracks(lib, tmp_path / "Music")
    await _quiet()
    events.clear()
    await lib.set_hidden(ids["a.mp3"], True)
    await lib.stop()
    await _quiet()
    assert events == []


# -- through the service ---------------------------------------------------------------------


class _Settings:
    def __init__(self, home):
        self.dpc_home_dir = home

    def get(self, section, key, fallback=None):
        return "60" if (section, key) == ("music", "poll_interval_s") else fallback


class _Service(MusicCommandsMixin):
    def __init__(self, home):
        self.settings = _Settings(home)
        self.sent = []

    def _music_broadcast(self, event, payload):
        self.sent.append((event, payload))


@pytest.mark.asyncio
async def test_the_service_broadcasts_music_library_changed_for_a_hide_and_an_export_delete(tmp_path, monkeypatch):
    original = MusicLibrary.__init__

    def init(self, home, **kw):
        kw.setdefault("tools", MediaTools(ffmpeg=None, ffprobe=None))
        kw.setdefault("save_delay_s", 0.01)
        kw.setdefault("change_delay_s", 0.05)
        original(self, home, **kw)

    monkeypatch.setattr(MusicLibrary, "__init__", init)
    service = _Service(tmp_path / "dpc")
    try:
        music = tmp_path / "Music"
        music.mkdir()
        (music / "a.mp3").write_bytes(os.urandom(300))
        added = await service.add_music_folder(path=str(music), watch=False)
        await service.rescan_music_folder(folder_id=added["folder"]["id"])
        library = await service._music()
        await library.wait_hashed()
        await asyncio.sleep(0.3)
        service.sent.clear()

        [entry] = (await service.list_music_library())["entries"]
        await service.hide_music_track(track_id=entry["id"])
        await asyncio.sleep(0.3)
        changed = [p for e, p in service.sent if e == "music_library_changed"]
        assert len(changed) == 1 and changed[0]["reasons"] == ["track"] and changed[0]["track_ids"] == [entry["id"]]
        service.sent.clear()

        library.notify_changed("export", [entry["id"]])
        library.notify_changed("export", [entry["id"]])
        await asyncio.sleep(0.3)
        assert [p["reasons"] for e, p in service.sent if e == "music_library_changed"] == [["export"]]
    finally:
        await service.stop_music_library()
