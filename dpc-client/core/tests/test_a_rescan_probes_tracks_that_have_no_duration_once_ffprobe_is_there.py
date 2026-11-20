"""A track scanned before ffprobe was available keeps `duration_s = None`, and a rescan
skips unchanged mtimes, so it would stay that way. A rescan that finds ffprobe now probes
those entries again (no re-hash), once per process, and only them."""

import json
import os
import types

import pytest

from dpc_client_core.music.identity import MediaTools
from dpc_client_core.music.library import MusicLibrary


def _probe_runner(calls):
    def run(cmd, *, timeout):
        calls.append(cmd[-1])
        out = {"format": {"duration": "204.912", "tags": {"title": "Night Bus", "artist": "Mike", "album": "Demos"}},
               "streams": [{"sample_rate": "44100", "channels": 2}]}
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(out).encode())
    return run


def _library(home):
    return MusicLibrary(home, tools=MediaTools(ffmpeg=None, ffprobe=None), save_delay_s=0.01)


@pytest.mark.asyncio
async def test_a_rescan_gives_a_late_ffprobe_the_tracks_that_have_no_duration(tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    (music / "night-bus.mp3").write_bytes(os.urandom(800))
    (music / "other.mp3").write_bytes(os.urandom(900))
    lib = _library(tmp_path / "dpc")
    await lib.open()
    try:
        folder = await lib.add_folder(str(music), scan=False)
        await lib.rescan_folder(folder["id"])
        await lib.wait_hashed()
        entries = {e["source"]["name"]: e for e in lib.list_entries()}
        assert all(e["hash_state"] == "done" and e["duration_s"] is None for e in entries.values())
        hashes = {n: e["content_hash"] for n, e in entries.items()}

        # Still no ffprobe: a rescan changes nothing and probes nothing.
        await lib.rescan_folder(folder["id"])
        await lib.wait_hashed()
        assert all(e["duration_s"] is None for e in lib.list_entries())

        # ffprobe arrives; the files are untouched (same mtime).
        calls = []
        lib._tools = MediaTools(ffmpeg=None, ffprobe="ffprobe", runner=_probe_runner(calls))
        await lib.rescan_folder(folder["id"])
        await lib.wait_hashed()
        after = {e["source"]["name"]: e for e in lib.list_entries()}
        assert sorted(os.path.basename(c) for c in calls) == ["night-bus.mp3", "other.mp3"]
        for name, e in after.items():
            assert e["duration_s"] == 204.912 and e["artist"] == "Mike" and e["album"] == "Demos"
            assert e["content_hash"] == hashes[name] and e["id"] == entries[name]["id"]  # not re-hashed
        assert after["night-bus.mp3"]["title"] == "Night Bus"

        # Once per entry: the next rescans probe nothing more.
        calls.clear()
        await lib.rescan_folder(folder["id"])
        await lib.wait_hashed()
        assert calls == []
    finally:
        await lib.stop()


@pytest.mark.asyncio
async def test_a_track_that_ffprobe_cannot_read_is_probed_once_not_on_every_poll(tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    (music / "broken.mp3").write_bytes(os.urandom(700))
    lib = _library(tmp_path / "dpc")
    await lib.open()
    try:
        folder = await lib.add_folder(str(music), scan=False)
        await lib.rescan_folder(folder["id"])
        await lib.wait_hashed()
        calls = []

        def failing(cmd, *, timeout):
            calls.append(cmd[-1])
            return types.SimpleNamespace(returncode=1, stdout=b"")
        lib._tools = MediaTools(ffmpeg=None, ffprobe="ffprobe", runner=failing)
        for _ in range(3):
            await lib.rescan_folder(folder["id"])
            await lib.wait_hashed()
        assert len(calls) == 1
        assert lib.list_entries()[0]["duration_s"] is None
    finally:
        await lib.stop()


@pytest.mark.asyncio
async def test_entries_that_already_have_a_duration_are_never_probed_again(tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    (music / "a.mp3").write_bytes(os.urandom(700))
    calls = []
    lib = MusicLibrary(tmp_path / "dpc", tools=MediaTools(ffmpeg=None, ffprobe="ffprobe", runner=_probe_runner(calls)),
                       save_delay_s=0.01)
    await lib.open()
    try:
        folder = await lib.add_folder(str(music), scan=False)
        await lib.rescan_folder(folder["id"])
        await lib.wait_hashed()
        assert lib.list_entries()[0]["duration_s"] == 204.912
        calls.clear()
        await lib.rescan_folder(folder["id"])
        await lib.wait_hashed()
        assert calls == []
    finally:
        await lib.stop()
