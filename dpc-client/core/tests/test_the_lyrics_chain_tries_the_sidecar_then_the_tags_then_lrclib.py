"""The lyrics chain (ADR-051 D6): a `.lrc` sidecar, then embedded tags, then LRCLIB, then
manual; stored per track under the DPC home, never next to the person's audio. The
automatic chain (`get`, run on every track change) reads only the local LRCLIB cache; a
request goes out only from the explicit lookup ("Find on LRCLIB").

No test reaches the network: every LRCLIB client here has an `httpx.MockTransport`, and
the `no_network` fixture makes any real httpx transport fail the test.
"""

import asyncio
import gc
import json
import os
import shutil
import struct
import subprocess

import httpx
import pytest

from dpc_client_core.music import commands, lyrics as lyrics_mod
from dpc_client_core.music.commands import MUSIC_COMMANDS, MusicCommandsMixin
from dpc_client_core.music.identity import MediaTools
from dpc_client_core.music.library import MusicLibraryError
from dpc_client_core.music.lyrics import LrclibCache, LrclibClient, MusicLyrics

TRACK_ID = "trk_0123456789abcdef"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    async def refuse(self, request):
        raise AssertionError(f"a test tried to reach the network: {request.url}")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


class FakeLibrary:
    def __init__(self, path, *, artist=None, title=None, album=None, duration_s=None, source_hash="sha256:aa"):
        self.path = path
        self.entry = {
            "id": TRACK_ID, "title": title or path.stem, "artist": artist, "album": album,
            "duration_s": duration_s, "source_hash": source_hash,
            "source": {"kind": "local", "name": path.name, "uri": path.as_uri()},
        }

    def get_entry(self, track_id):
        if track_id != TRACK_ID:
            raise MusicLibraryError("unknown_track", f"no track {track_id}")
        return dict(self.entry)

    def get_track_path(self, track_id):
        return self.path


def probe_runner(tags=None, duration="215.4", calls=None):
    """A fake ffprobe: the JSON it prints for any file."""
    def run(cmd, *, timeout):
        if calls is not None:
            calls.append(cmd)
        out = {"format": {"duration": duration, "tags": dict(tags or {})}, "streams": [{"codec_type": "audio"}]}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(out).encode(), b"")
    return run


class Lrclib:
    """A mock lrclib.net: answers from `reply(request)`, records each request."""

    def __init__(self, reply):
        self.reply = reply
        self.requests = []

    def handler(self, request):
        self.requests.append(request)
        return self.reply(request)

    def client(self, **kw):
        return LrclibClient(transport=httpx.MockTransport(self.handler), min_gap_s=0, **kw)


def record(**over):
    rec = {"id": 42, "trackName": "Song", "artistName": "Band", "albumName": "LP", "duration": 215,
           "instrumental": False, "plainLyrics": "one\ntwo", "syncedLyrics": "[00:01.00] one\n[00:02.00] two"}
    rec.update(over)
    return rec


def ok(rec):
    return lambda request: httpx.Response(200, json=rec)


@pytest.fixture
def audio(tmp_path):
    folder = tmp_path / "Music"
    folder.mkdir()
    path = folder / "Band - Song.mp3"
    path.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xff\xfb" * 500)
    return path


def make(tmp_path, library, *, tags=None, lrclib=None, enabled=True, ffprobe=True, calls=None):
    tools = MediaTools(ffmpeg=None, ffprobe="ffprobe" if ffprobe else None, runner=probe_runner(tags, calls=calls))
    return MusicLyrics(tmp_path / "dpc", library, lrclib_enabled=lambda: enabled, tools=tools,
                       client=(lrclib or Lrclib(ok(record()))).client())


class ForbiddenClient:
    """An LRCLIB client that fails the test if asked: the automatic chain must not ask."""

    def __init__(self):
        self.calls = 0

    async def get(self, q):
        self.calls += 1
        raise AssertionError(f"the automatic chain asked lrclib.net: {q}")


def snapshot(folder):
    return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in sorted(folder.iterdir())}


# -- the order of the chain ---------------------------------------------------------

@pytest.mark.asyncio
async def test_the_sidecar_beats_the_tags_and_lrclib(tmp_path, audio):
    audio.with_suffix(".lrc").write_text("[la:en]\n[00:03.00] from the sidecar\nuntimed", encoding="utf-8")
    server = Lrclib(ok(record()))
    calls = []
    svc = make(tmp_path, FakeLibrary(audio, duration_s=215), tags={"lyrics-eng": "[00:01.00] from tags"},
               lrclib=server, calls=calls)
    answer = await svc.get(TRACK_ID)
    assert answer["lyrics"]["source"] == "lrc"
    assert answer["lyrics"]["lines"] == [{"t_start": 3.0, "text": "from the sidecar"}]
    assert answer["lyrics"]["language"] == "en"
    assert [s["state"] for s in answer["chain"]] == ["found", "not_needed", "not_needed", "not_needed", "available"]
    assert "1 untimed line(s) skipped" in answer["chain"][0]["detail"]
    assert server.requests == [] and calls == []


@pytest.mark.asyncio
async def test_the_tags_beat_lrclib(tmp_path, audio):
    server = Lrclib(ok(record()))
    svc = make(tmp_path, FakeLibrary(audio, duration_s=215), tags={"lyrics-eng": "Hello\nWorld"}, lrclib=server)
    answer = await svc.get(TRACK_ID)
    lyr = answer["lyrics"]
    assert lyr["source"] == "tags" and lyr["synced"] is False
    assert [l["text"] for l in lyr["lines"]] == ["Hello", "World"]
    assert answer["chain"][1]["state"] == "found" and "lyrics-eng" in answer["chain"][1]["detail"]
    assert server.requests == []


@pytest.mark.asyncio
async def test_a_sidecar_without_timed_lines_falls_through_to_the_tags(tmp_path, audio):
    audio.with_suffix(".lrc").write_text("just words", encoding="utf-8")
    svc = make(tmp_path, FakeLibrary(audio, duration_s=215), tags={"LYRICS": "[00:01.00] timed tag"})
    answer = await svc.get(TRACK_ID)
    assert answer["chain"][0]["state"] == "none" and "no timed lines" in answer["chain"][0]["detail"]
    assert answer["lyrics"]["source"] == "tags" and "synced" not in answer["lyrics"]


@pytest.mark.asyncio
async def test_without_sidecar_and_tags_the_automatic_chain_sends_nothing(tmp_path, audio):
    server = Lrclib(ok(record()))
    svc = make(tmp_path, FakeLibrary(audio, artist="Band", title="Song", album="LP", duration_s=215.4), lrclib=server)
    answer = await svc.get(TRACK_ID)
    assert answer["lyrics"] is None and answer["lrclib"] is None
    assert answer["chain"][2]["state"] == "not_run" and "Find on LRCLIB" in answer["chain"][2]["detail"]
    assert answer["lrclib_enabled"] is True
    refreshed = await svc.get(TRACK_ID, refresh=True)
    assert refreshed["chain"][2]["state"] == "not_run"
    assert server.requests == []
    assert not (tmp_path / "dpc" / "music" / "lyrics" / f"{TRACK_ID}.json").exists()


@pytest.mark.asyncio
async def test_get_music_lyrics_never_calls_the_lrclib_client(tmp_path, audio):
    """Whatever the cache holds (nothing, a negative, a found record), `get` and
    `get(refresh=True)` never ask the client."""
    forbidden = ForbiddenClient()
    tools = MediaTools(ffmpeg=None, ffprobe="ffprobe", runner=probe_runner())
    lib = FakeLibrary(audio, artist="Band", title="Song", duration_s=215)
    svc = MusicLyrics(tmp_path / "dpc", lib, tools=tools, client=forbidden)
    assert (await svc.get(TRACK_ID))["chain"][2]["state"] == "not_run"
    assert (await svc.get(TRACK_ID, refresh=True))["chain"][2]["state"] == "not_run"
    key = LrclibCache.make_key("sha256:aa", MusicLyrics.build_query(lib.entry, 215))
    await svc.cache.put(key, {"result": "not_found"})
    negative = await svc.get(TRACK_ID)
    assert negative["lrclib"]["result"] == "not_found" and negative["lrclib"]["cached"] is True
    assert negative["chain"][2]["state"] == "none"
    await svc.cache.put(key, {"result": "synced",
                              "record": {"id": 9, "artist_name": "Band", "track_name": "Song",
                                         "album_name": None, "duration": 215, "synced": True},
                              "lines": [{"t_start": 1.0, "text": "cached"}]})
    hit = await svc.get(TRACK_ID, refresh=True)
    assert hit["lyrics"]["source"] == "lrclib" and hit["lyrics"]["lines"] == [{"t_start": 1.0, "text": "cached"}]
    assert "from the local cache" in hit["chain"][2]["detail"]
    assert forbidden.calls == 0


@pytest.mark.asyncio
async def test_a_cached_record_from_an_explicit_lookup_feeds_a_later_automatic_chain(tmp_path, audio):
    server = Lrclib(ok(record()))
    first = make(tmp_path, FakeLibrary(audio, artist="Band", title="Song", duration_s=215), lrclib=server,
                 tags={"LYRICS": "[00:01.00] tag"})
    await first.get(TRACK_ID)  # the tags are stored
    offered = await first.lookup_lrclib(TRACK_ID)  # fetched, cached, offered as a candidate
    assert offered["candidate"]["source"] == "lrclib"
    (tmp_path / "dpc" / "music" / "lyrics" / f"{TRACK_ID}.json").unlink()
    # A later session over the same home: no stored lyrics, no tags now, a client that must not be asked.
    later = MusicLyrics(tmp_path / "dpc", FakeLibrary(audio, artist="Band", title="Song", duration_s=215),
                        tools=MediaTools(ffmpeg=None, ffprobe="ffprobe", runner=probe_runner()),
                        client=ForbiddenClient())
    answer = await later.get(TRACK_ID)
    assert answer["lyrics"]["source"] == "lrclib" and answer["lrclib"]["cached"] is True
    assert [s["state"] for s in answer["chain"]] == ["none", "none", "found", "not_needed", "available"]
    assert len(server.requests) == 1


@pytest.mark.asyncio
async def test_the_explicit_lookup_asks_lrclib_with_the_entry_metadata(tmp_path, audio):
    server = Lrclib(ok(record()))
    svc = make(tmp_path, FakeLibrary(audio, artist="Band", title="Song", album="LP", duration_s=215.4), lrclib=server)
    answer = await svc.lookup_lrclib(TRACK_ID)
    assert answer["applied"] is True and answer["lyrics"]["source"] == "lrclib"
    assert answer["lyrics"]["lrclib"] == {"id": 42, "artist_name": "Band", "track_name": "Song",
                                          "album_name": "LP", "duration": 215, "synced": True}
    assert answer["lrclib"]["result"] == "synced" and answer["lrclib"]["cached"] is False
    [request] = server.requests
    assert request.url.path == "/api/get"
    assert dict(request.url.params) == {"artist_name": "Band", "track_name": "Song", "album_name": "LP", "duration": "215"}
    assert request.headers["user-agent"].startswith("dpc-messenger-music/")
    assert "+https://github.com/mikhashev/dpc-messenger" in request.headers["user-agent"]
    back = await svc.get(TRACK_ID)
    assert back["from_store"] is True and back["lyrics"] == answer["lyrics"]
    assert len(server.requests) == 1


@pytest.mark.asyncio
async def test_artist_and_title_come_from_the_file_name_without_tags(tmp_path, audio):
    server = Lrclib(ok(record()))
    svc = make(tmp_path, FakeLibrary(audio), lrclib=server)  # duration from ffprobe: 215.4
    await svc.lookup_lrclib(TRACK_ID)
    [request] = server.requests
    assert request.url.params["artist_name"] == "Band" and request.url.params["track_name"] == "Song"
    assert request.url.params["duration"] == "215"


@pytest.mark.asyncio
async def test_without_an_artist_or_a_duration_lrclib_is_skipped(tmp_path):
    folder = tmp_path / "M"
    folder.mkdir()
    lone = folder / "untitled.mp3"
    lone.write_bytes(b"x")
    server = Lrclib(ok(record()))
    svc = make(tmp_path, FakeLibrary(lone, duration_s=100), lrclib=server)
    answer = await svc.get(TRACK_ID)
    assert answer["lrclib"]["result"] == "no_query" and answer["lyrics"] is None
    svc2 = make(tmp_path, FakeLibrary(lone, artist="A", title="T"), lrclib=server, ffprobe=False)
    answer = await svc2.get(TRACK_ID)
    assert answer["lrclib"]["result"] == "no_duration"
    assert answer["chain"][1]["state"] == "unavailable"
    assert (await svc.lookup_lrclib(TRACK_ID))["lrclib"]["result"] == "no_query"
    assert server.requests == []


@pytest.mark.asyncio
async def test_instrumental_stops_the_chain_without_lyrics(tmp_path, audio):
    server = Lrclib(ok(record(instrumental=True, plainLyrics=None, syncedLyrics=None)))
    svc = make(tmp_path, FakeLibrary(audio, duration_s=215), lrclib=server)
    looked = await svc.lookup_lrclib(TRACK_ID)
    assert looked["applied"] is False and looked["lrclib"]["result"] == "instrumental"
    answer = await svc.get(TRACK_ID)  # the cached outcome; nothing more is sent
    assert len(server.requests) == 1
    assert answer["lyrics"] is None and answer["lrclib"]["result"] == "instrumental"
    assert answer["chain"][2]["state"] == "instrumental"
    assert "instrumental" in answer["chain"][3]["detail"]
    assert not (tmp_path / "dpc" / "music" / "lyrics" / f"{TRACK_ID}.json").exists()


@pytest.mark.asyncio
async def test_the_disabled_setting_sends_nothing(tmp_path, audio):
    server = Lrclib(ok(record()))
    svc = make(tmp_path, FakeLibrary(audio, artist="Band", title="Song", duration_s=215), lrclib=server, enabled=False)
    answer = await svc.get(TRACK_ID)
    assert answer["chain"][2]["state"] == "disabled" and answer["lyrics"] is None
    assert answer["lrclib_enabled"] is False
    explicit = await svc.lookup_lrclib(TRACK_ID)
    assert explicit["lrclib"]["result"] == "disabled"
    assert server.requests == []
    assert not (tmp_path / "dpc" / "music" / "lyrics_cache.json").exists()


@pytest.mark.asyncio
async def test_a_network_error_is_not_cached_and_the_next_click_tries_again(tmp_path, audio):
    def down(request):
        raise httpx.ConnectError("offline", request=request)
    server = Lrclib(down)
    svc = make(tmp_path, FakeLibrary(audio, duration_s=215), lrclib=server)
    first = await svc.lookup_lrclib(TRACK_ID)
    assert first["lrclib"]["result"] == "error" and "could not reach" in first["lrclib"]["error"]
    chain = await svc.get(TRACK_ID)
    assert chain["chain"][2]["state"] == "not_run" and len(server.requests) == 1
    await svc.lookup_lrclib(TRACK_ID)
    assert len(server.requests) == 2


@pytest.mark.asyncio
async def test_an_explicit_lookup_asks_again_past_a_cached_not_found_but_not_past_a_429(tmp_path, audio):
    replies = [httpx.Response(404), httpx.Response(429, headers={"Retry-After": "120"}),
               httpx.Response(200, json=record())]
    server = Lrclib(lambda request: replies.pop(0))
    svc = make(tmp_path, FakeLibrary(audio, duration_s=215), lrclib=server)
    assert (await svc.lookup_lrclib(TRACK_ID))["lrclib"]["result"] == "not_found"
    assert (await svc.get(TRACK_ID))["lrclib"]["result"] == "not_found"  # the chain shows the cached negative
    limited = await svc.lookup_lrclib(TRACK_ID)  # the button passes over the cached negative
    assert limited["lrclib"]["result"] == "rate_limited" and len(server.requests) == 2
    blocked = await svc.lookup_lrclib(TRACK_ID)  # but the Retry-After block holds
    assert blocked["lrclib"]["result"] == "rate_limited" and len(server.requests) == 2
    svc.client.blocked_until = -1.0
    found = await svc.lookup_lrclib(TRACK_ID)
    assert found["applied"] is True and len(server.requests) == 3
    again = await svc.lookup_lrclib(TRACK_ID)  # a cached found record is used, not fetched again
    assert again["lrclib"]["cached"] is True and len(server.requests) == 3


# -- storage and the never-overwrite rule --------------------------------------------

@pytest.mark.asyncio
async def test_found_lyrics_are_stored_and_returned_from_the_store(tmp_path, audio):
    server = Lrclib(ok(record()))
    calls = []
    svc = make(tmp_path, FakeLibrary(audio, duration_s=215), lrclib=server, calls=calls)
    await svc.lookup_lrclib(TRACK_ID)
    stored_file = tmp_path / "dpc" / "music" / "lyrics" / f"{TRACK_ID}.json"
    on_disk = json.loads(stored_file.read_text(encoding="utf-8"))
    assert on_disk["source"] == "lrclib" and on_disk["track_hash"] == "sha256:aa"
    calls.clear()
    again = await svc.get(TRACK_ID)
    assert again["from_store"] is True and again["lyrics"] == on_disk
    assert len(server.requests) == 1 and calls == []


@pytest.mark.asyncio
async def test_refresh_replaces_chain_lyrics_but_keeps_a_manual_edit(tmp_path, audio):
    svc = make(tmp_path, FakeLibrary(audio, duration_s=215), tags={"LYRICS": "[00:01.00] old tag"})
    assert (await svc.get(TRACK_ID))["lyrics"]["source"] == "tags"
    audio.with_suffix(".lrc").write_text("[00:02.00] new sidecar", encoding="utf-8")
    refreshed = await svc.get(TRACK_ID, refresh=True)
    assert refreshed["lyrics"]["source"] == "lrc"
    await svc.save_manual(TRACK_ID, "[00:05.00] my own words")
    kept = await svc.get(TRACK_ID, refresh=True)
    assert kept["lyrics"]["source"] == "manual" and kept["candidate"]["source"] == "lrc"
    assert "kept" in kept
    assert (await svc.get(TRACK_ID))["lyrics"]["lines"] == [{"t_start": 5.0, "text": "my own words"}]


@pytest.mark.asyncio
async def test_an_explicit_lookup_replaces_stored_lyrics_only_when_asked(tmp_path, audio):
    server = Lrclib(ok(record()))
    svc = make(tmp_path, FakeLibrary(audio, duration_s=215), tags={"LYRICS": "[00:01.00] tag"}, lrclib=server)
    await svc.get(TRACK_ID)
    offered = await svc.lookup_lrclib(TRACK_ID, artist="Band", title="Song")
    assert offered["applied"] is False and offered["lyrics"]["source"] == "tags"
    assert offered["candidate"]["source"] == "lrclib"
    taken = await svc.lookup_lrclib(TRACK_ID, artist="Band", title="Song", replace=True)
    assert taken["applied"] is True and taken["lyrics"]["source"] == "lrclib"
    assert taken["lrclib"]["cached"] is True and len(server.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("prior", ["manual", "edited"])
async def test_replace_over_the_persons_own_lyrics_needs_overwrite_manual(tmp_path, audio, prior):
    server = Lrclib(ok(record()))
    svc = make(tmp_path, FakeLibrary(audio, duration_s=215), lrclib=server,
               tags={"LYRICS": "[00:01.00] tag"} if prior == "edited" else None)
    if prior == "edited":
        await svc.get(TRACK_ID)  # the tags are stored, then edited
    await svc.save_manual(TRACK_ID, "[00:05.00] my own words")
    mine = await svc.load_stored(TRACK_ID)
    assert mine["source"] == "manual" and bool(mine.get("edited")) is (prior == "edited")

    with pytest.raises(MusicLibraryError) as refused:
        await svc.lookup_lrclib(TRACK_ID, replace=True)
    assert refused.value.code == "overwrite_manual_required"
    assert server.requests == []  # refused before anything was sent
    assert await svc.load_stored(TRACK_ID) == mine

    offered = await svc.lookup_lrclib(TRACK_ID)  # no replace: a candidate, the edit kept
    assert offered["applied"] is False and offered["candidate"]["source"] == "lrclib"
    assert await svc.load_stored(TRACK_ID) == mine

    taken = await svc.lookup_lrclib(TRACK_ID, replace=True, overwrite_manual=True)
    assert taken["applied"] is True and taken["lyrics"]["source"] == "lrclib"
    assert (await svc.load_stored(TRACK_ID))["source"] == "lrclib"
    assert len(server.requests) == 1  # the confirmed replace used the cached record


@pytest.mark.asyncio
async def test_overwrite_manual_without_replace_does_not_replace(tmp_path, audio):
    svc = make(tmp_path, FakeLibrary(audio, duration_s=215))
    await svc.save_manual(TRACK_ID, "[00:05.00] mine")
    answer = await svc.lookup_lrclib(TRACK_ID, overwrite_manual=True)
    assert answer["applied"] is False and answer["lyrics"]["source"] == "manual"
    assert answer["candidate"]["source"] == "lrclib"


@pytest.mark.asyncio
async def test_two_concurrent_lookups_on_one_track_send_one_request_and_store_once(tmp_path, audio):
    class SlowTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.requests = []

        async def handle_async_request(self, request):
            self.requests.append(request)
            await asyncio.sleep(0.05)
            return httpx.Response(200, json=record())

    transport = SlowTransport()
    tools = MediaTools(ffmpeg=None, ffprobe="ffprobe", runner=probe_runner())
    svc = MusicLyrics(tmp_path / "dpc", FakeLibrary(audio, artist="Band", title="Song", duration_s=215),
                      tools=tools, client=LrclibClient(transport=transport, min_gap_s=0))
    a, b = await asyncio.gather(svc.lookup_lrclib(TRACK_ID), svc.lookup_lrclib(TRACK_ID))
    assert len(transport.requests) == 1
    assert sorted([a["applied"], b["applied"]]) == [False, True]
    second = b if a["applied"] else a
    assert second["lrclib"]["cached"] is True and second["candidate"]["source"] == "lrclib"
    assert (await svc.load_stored(TRACK_ID))["source"] == "lrclib"


@pytest.mark.asyncio
async def test_the_per_track_locks_do_not_pile_up(tmp_path, audio):
    svc = make(tmp_path, FakeLibrary(audio, duration_s=215), tags={"LYRICS": "[00:01.00] tag"})
    await svc.get(TRACK_ID)
    await svc.lookup_lrclib(TRACK_ID)
    await svc.save_manual(TRACK_ID, "[00:01.00] mine")
    gc.collect()
    assert len(svc._locks) == 0
    held = svc._lock(TRACK_ID)
    assert svc._lock(TRACK_ID) is held  # the same lock while someone has it
    del held
    gc.collect()
    assert len(svc._locks) == 0


@pytest.mark.asyncio
async def test_a_manual_edit_round_trips(tmp_path, audio):
    svc = make(tmp_path, FakeLibrary(audio, duration_s=215))
    first = await svc.lookup_lrclib(TRACK_ID)  # LRCLIB synced lyrics are stored
    text = lyrics_mod.format_lrc(first["lyrics"]) + "\n[00:03.00] a third line <00:04.50>"
    saved = (await svc.save_manual(TRACK_ID, text))["lyrics"]
    assert saved["source"] == "manual" and saved["edited"] is True and "lrclib" not in saved
    back = await svc.get(TRACK_ID)
    assert back["lyrics"] == saved
    assert back["lyrics"]["lines"][-1] == {"t_start": 3.0, "t_end": 4.5, "text": "a third line"}
    assert lyrics_mod.parse_lrc(lyrics_mod.format_lrc(saved)).lines == saved["lines"]


@pytest.mark.asyncio
async def test_a_save_with_unstamped_lines_or_nothing_is_refused(tmp_path, audio):
    svc = make(tmp_path, FakeLibrary(audio, duration_s=215))
    with pytest.raises(MusicLibraryError) as unstamped:
        await svc.save_manual(TRACK_ID, "[00:01.00] a\nb\nc")
    assert unstamped.value.code == "unstamped_lines" and "2 lines have no time stamp" in unstamped.value.message
    with pytest.raises(MusicLibraryError) as empty:
        await svc.save_manual(TRACK_ID, "[ar:x]\n")
    assert empty.value.code == "empty_lyrics"
    with pytest.raises(MusicLibraryError) as bad_id:
        await svc.save_manual("../../evil", "[00:01.00] a")
    assert bad_id.value.code == "invalid_track_id"
    assert not (tmp_path / "dpc" / "music" / "lyrics").exists()


@pytest.mark.asyncio
async def test_a_corrupt_stored_file_is_moved_aside(tmp_path, audio):
    svc = make(tmp_path, FakeLibrary(audio, duration_s=215), tags={"LYRICS": "[00:01.00] tag"})
    stored = tmp_path / "dpc" / "music" / "lyrics" / f"{TRACK_ID}.json"
    stored.parent.mkdir(parents=True)
    stored.write_text("{not json", encoding="utf-8")
    answer = await svc.get(TRACK_ID)
    assert answer["lyrics"]["source"] == "tags"
    assert any(p.name.startswith(f"{TRACK_ID}.json.corrupt-") for p in stored.parent.iterdir())


@pytest.mark.asyncio
async def test_a_corrupt_stored_file_that_cannot_be_moved_is_treated_as_absent(tmp_path, audio, monkeypatch, caplog):
    """A file another process holds open cannot be renamed on Windows; the command must
    not fail on it."""
    svc = make(tmp_path, FakeLibrary(audio, duration_s=215), tags={"LYRICS": "[00:01.00] tag"})
    stored = tmp_path / "dpc" / "music" / "lyrics" / f"{TRACK_ID}.json"
    stored.parent.mkdir(parents=True)
    stored.write_text("{not json", encoding="utf-8")
    real_replace = os.replace

    def locked(src, dst):
        if ".corrupt-" in str(dst):
            raise PermissionError(13, "The process cannot access the file because it is being used by another process")
        return real_replace(src, dst)
    monkeypatch.setattr(lyrics_mod.os, "replace", locked)
    assert await svc.load_stored(TRACK_ID) is None
    assert stored.read_text(encoding="utf-8") == "{not json"
    assert "could not be moved aside" in caplog.text
    answer = await svc.get(TRACK_ID)  # the command answers; the chain's result replaces the bad file
    assert answer["lyrics"]["source"] == "tags"
    assert json.loads(stored.read_text(encoding="utf-8"))["source"] == "tags"


@pytest.mark.asyncio
async def test_nothing_is_written_into_the_persons_folder(tmp_path, audio):
    (audio.parent / "Other - Track.flac").write_bytes(b"fLaC....")
    before = snapshot(audio.parent)
    svc = make(tmp_path, FakeLibrary(audio, duration_s=215))
    await svc.get(TRACK_ID)
    await svc.lookup_lrclib(TRACK_ID, replace=True)
    await svc.save_manual(TRACK_ID, "[00:01.00] mine")
    await svc.lookup_lrclib(TRACK_ID, replace=True, overwrite_manual=True)
    await svc.get(TRACK_ID, refresh=True)
    assert snapshot(audio.parent) == before
    assert not audio.with_suffix(".lrc").exists()
    written = sorted(p.relative_to(tmp_path / "dpc").as_posix() for p in (tmp_path / "dpc").rglob("*") if p.is_file())
    assert written == ["music/lyrics/trk_0123456789abcdef.json", "music/lyrics_cache.json"]


@pytest.mark.asyncio
async def test_an_unknown_track_is_refused(tmp_path, audio):
    svc = make(tmp_path, FakeLibrary(audio))
    with pytest.raises(MusicLibraryError) as exc:
        await svc.get("trk_ffffffffffffffff")
    assert exc.value.code == "unknown_track"


# -- real ffprobe on a hand-built ID3 tag (skipped without ffmpeg) ---------------------

def _id3v24(frames):
    def syncsafe(n):
        return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])
    body = b"".join(fid + syncsafe(len(data)) + b"\x00\x00" + data for fid, data in frames)
    return b"ID3\x04\x00\x00" + syncsafe(len(body)) + body


@pytest.mark.asyncio
@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg and ffprobe")
async def test_a_real_uslt_frame_is_read_and_a_sylt_frame_is_not(tmp_path):
    raw = tmp_path / "raw.mp3"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-id3v2_version", "0", "-write_xing", "0", str(raw)], check=True, timeout=60)
    folder = tmp_path / "Music"
    folder.mkdir()

    def runner(cmd, *, timeout):
        return subprocess.run(cmd, capture_output=True, timeout=timeout)

    tools = MediaTools(ffmpeg=shutil.which("ffmpeg"), ffprobe=shutil.which("ffprobe"), runner=runner)
    uslt = b"\x03eng\x00" + "Привет\n[00:01.00] timed".encode("utf-8")
    sylt = b"\x03eng\x02\x01\x00" + b"sylt one\x00" + struct.pack(">I", 1000)
    with_uslt = folder / "Band - Uslt.mp3"
    with_uslt.write_bytes(_id3v24([(b"USLT", uslt), (b"SYLT", sylt)]) + raw.read_bytes())
    only_sylt = folder / "Band - Sylt.mp3"
    only_sylt.write_bytes(_id3v24([(b"SYLT", sylt)]) + raw.read_bytes())

    svc = MusicLyrics(tmp_path / "dpc", FakeLibrary(with_uslt), lrclib_enabled=lambda: False, tools=tools,
                      client=Lrclib(ok(record())).client())
    answer = await svc.get(TRACK_ID)
    assert answer["lyrics"]["source"] == "tags"
    assert answer["lyrics"]["lines"] == [{"t_start": 1.0, "text": "timed"}]
    assert "lyrics-eng" in answer["chain"][1]["detail"]

    svc2 = MusicLyrics(tmp_path / "dpc2", FakeLibrary(only_sylt), lrclib_enabled=lambda: False, tools=tools,
                       client=Lrclib(ok(record())).client())
    answer = await svc2.get(TRACK_ID)
    assert answer["lyrics"] is None and answer["chain"][1]["state"] == "none"


# -- the local_api door ---------------------------------------------------------------

class _Settings:
    def __init__(self, home, values=None):
        self.dpc_home_dir = home
        self.values = values or {}

    def get(self, section, key, fallback=None):
        return self.values.get((section, key), fallback)


class _Service(MusicCommandsMixin):
    def __init__(self, home, values=None):
        self.settings = _Settings(home, values)


def test_the_lyrics_commands_are_coroutines_and_in_the_allowlist():
    from dpc_client_core.local_api import ALLOWED_COMMANDS

    for name in ("get_music_lyrics", "save_music_lyrics", "lookup_music_lyrics_lrclib"):
        assert name in MUSIC_COMMANDS and name in ALLOWED_COMMANDS
        assert asyncio.iscoroutinefunction(getattr(MusicCommandsMixin, name))
    assert getattr(MusicCommandsMixin.get_music_lyrics, "dpc_slow", False)
    assert getattr(MusicCommandsMixin.lookup_music_lyrics_lrclib, "dpc_slow", False)
    assert not getattr(MusicCommandsMixin.save_music_lyrics, "dpc_slow", False)


@pytest.mark.asyncio
async def test_the_lookup_command_passes_overwrite_manual_only_when_it_is_true(tmp_path, monkeypatch):
    seen = []

    class FakeLyrics:
        async def lookup_lrclib(self, track_id, **kw):
            seen.append(kw)
            if kw["replace"] and not kw["overwrite_manual"]:
                raise MusicLibraryError("overwrite_manual_required", "needs confirmation")
            return {"lyrics": None, "applied": True, "candidate": None}

    service = _Service(tmp_path)

    async def fake_lyrics():
        return FakeLyrics()
    monkeypatch.setattr(service, "_music_lyrics", fake_lyrics)
    refused = await service.lookup_music_lyrics_lrclib(track_id=TRACK_ID, replace=True)
    assert refused == {"status": "error", "code": "overwrite_manual_required", "message": "needs confirmation"}
    truthy = await service.lookup_music_lyrics_lrclib(track_id=TRACK_ID, replace=True, overwrite_manual="yes")
    assert truthy["status"] == "error"  # only a real true confirms
    confirmed = await service.lookup_music_lyrics_lrclib(track_id=TRACK_ID, replace=True, overwrite_manual=True)
    assert confirmed["status"] == "ok"
    assert [kw["overwrite_manual"] for kw in seen] == [False, False, True]


@pytest.mark.asyncio
async def test_the_get_command_sends_nothing_over_a_real_library(tmp_path, monkeypatch):
    def init(original):
        def wrapped(self, home, **kw):
            kw.setdefault("tools", MediaTools(ffmpeg=None, ffprobe=None))
            kw.setdefault("save_delay_s", 0.01)
            original(self, home, **kw)
        return wrapped
    monkeypatch.setattr(commands.MusicLibrary, "__init__", init(commands.MusicLibrary.__init__))
    music = tmp_path / "Music"
    music.mkdir()
    (music / "Band - Song.mp3").write_bytes(b"audio bytes")
    service = _Service(tmp_path / "dpc", {("music", "lrclib_enabled"): "true"})
    try:
        folder = (await service.add_music_folder(path=str(music)))["folder"]
        await service.rescan_music_folder(folder_id=folder["id"])
        await service._music_library_instance.wait_hashed()
        [entry] = (await service.list_music_library())["entries"]
        lyr = await service._music_lyrics()
        lyr._tools = MediaTools(ffmpeg=None, ffprobe=None)
        forbidden = ForbiddenClient()
        lyr.client = forbidden
        for refresh in (False, True):
            got = await service.get_music_lyrics(track_id=entry["id"], refresh=refresh)
            assert got["status"] == "ok" and got["lyrics"] is None and got["lrclib_enabled"] is True
        assert forbidden.calls == 0
    finally:
        await service.stop_music_library()


@pytest.mark.parametrize("raw,expected", [(None, True), ("true", True), ("0", False), ("off", False),
                                          ("No", False), ("yes", True), ("maybe", True)])
def test_the_lrclib_setting_is_read_with_a_fallback(tmp_path, raw, expected):
    values = {} if raw is None else {("music", "lrclib_enabled"): raw}
    assert _Service(tmp_path, values)._music_lrclib_enabled() is expected


@pytest.mark.asyncio
async def test_the_commands_round_trip_over_a_real_library(tmp_path, monkeypatch):
    def init(original):
        def wrapped(self, home, **kw):
            kw.setdefault("tools", MediaTools(ffmpeg=None, ffprobe=None))
            kw.setdefault("save_delay_s", 0.01)
            original(self, home, **kw)
        return wrapped
    monkeypatch.setattr(commands.MusicLibrary, "__init__", init(commands.MusicLibrary.__init__))
    music = tmp_path / "Music"
    music.mkdir()
    song = music / "Band - Song.mp3"
    song.write_bytes(b"audio bytes")
    song.with_suffix(".lrc").write_text("[00:01.00] sidecar line", encoding="utf-8")
    service = _Service(tmp_path / "dpc", {("music", "lrclib_enabled"): "false"})
    try:
        folder = (await service.add_music_folder(path=str(music)))["folder"]
        await service.rescan_music_folder(folder_id=folder["id"])
        await service._music_library_instance.wait_hashed()
        [entry] = (await service.list_music_library())["entries"]
        lyr = await service._music_lyrics()
        lyr._tools = MediaTools(ffmpeg=None, ffprobe=None)
        lyr.client = Lrclib(ok(record())).client()

        got = await service.get_music_lyrics(track_id=entry["id"])
        assert got["status"] == "ok" and got["lyrics"]["source"] == "lrc"
        saved = await service.save_music_lyrics(track_id=entry["id"], lrc_text="[00:02.00] mine")
        assert saved["status"] == "ok" and saved["lyrics"]["source"] == "manual"
        refused = await service.save_music_lyrics(track_id=entry["id"], lrc_text="no stamp")
        assert refused == {"status": "error", "code": "unstamped_lines", "message": refused["message"]}
        looked = await service.lookup_music_lyrics_lrclib(track_id=entry["id"])
        assert looked["status"] == "ok" and looked["lrclib"]["result"] == "disabled"
        unknown = await service.get_music_lyrics(track_id="trk_ffffffffffffffff")
        assert unknown["status"] == "error" and unknown["code"] == "unknown_track"
    finally:
        await service.stop_music_library()
    assert (tmp_path / "dpc" / "music" / "lyrics" / f"{entry['id']}.json").exists()
    assert sorted(p.name for p in music.iterdir()) == ["Band - Song.lrc", "Band - Song.mp3"]
