"""The LRCLIB step of the lyrics chain (SPEC §6 "LRCLIB lookup"): what each answer of
lrclib.net becomes, the ±2 s duration rule, 429 Retry-After, timeouts, and the cache per
track hash plus normalised query with its TTL. Every request goes to an
`httpx.MockTransport`; no test reaches the network."""

import json

import httpx
import pytest

from dpc_client_core.music import lyrics as lyrics_mod
from dpc_client_core.music.lyrics import LrclibCache, LrclibClient, LrclibQuery, parse_retry_after

Q = LrclibQuery(artist="Band", title="Song", album=None, duration=215)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    async def refuse(self, request):
        raise AssertionError(f"a test tried to reach the network: {request.url}")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


def client_for(handler, **kw):
    seen = []

    def wrapped(request):
        seen.append(request)
        return handler(request)
    kw.setdefault("min_gap_s", 0)
    return LrclibClient(transport=httpx.MockTransport(wrapped), **kw), seen


def rec(**over):
    r = {"id": 7, "trackName": "Song", "artistName": "Band", "albumName": None, "duration": 215.0,
         "instrumental": False, "plainLyrics": "a\nb", "syncedLyrics": "[00:01.00] a\n[00:02.00] b"}
    r.update(over)
    return r


@pytest.mark.asyncio
async def test_synced_lyrics_are_preferred():
    client, seen = client_for(lambda r: httpx.Response(200, json=rec()))
    out = await client.get(Q)
    assert out["result"] == "synced"
    assert out["lines"] == [{"t_start": 1.0, "text": "a"}, {"t_start": 2.0, "text": "b"}]
    assert out["record"] == {"id": 7, "artist_name": "Band", "track_name": "Song", "album_name": None,
                             "duration": 215.0, "synced": True}
    assert "album_name" not in seen[0].url.params


@pytest.mark.asyncio
async def test_plain_only_gives_unsynced_lines():
    client, _ = client_for(lambda r: httpx.Response(200, json=rec(syncedLyrics=None)))
    out = await client.get(Q)
    assert out["result"] == "plain" and out["record"]["synced"] is False
    assert out["lines"] == [{"t_start": None, "text": "a"}, {"t_start": None, "text": "b"}]


@pytest.mark.asyncio
async def test_synced_text_without_stamps_falls_back_to_plain():
    client, _ = client_for(lambda r: httpx.Response(200, json=rec(syncedLyrics="no stamps")))
    assert (await client.get(Q))["result"] == "plain"


@pytest.mark.asyncio
async def test_instrumental_and_an_empty_record():
    client, _ = client_for(lambda r: httpx.Response(200, json=rec(instrumental=True, plainLyrics=None, syncedLyrics=None)))
    assert (await client.get(Q))["result"] == "instrumental"
    client, _ = client_for(lambda r: httpx.Response(200, json=rec(plainLyrics="", syncedLyrics=None)))
    assert (await client.get(Q))["result"] == "empty"


@pytest.mark.asyncio
async def test_404_is_not_found():
    client, _ = client_for(lambda r: httpx.Response(404, json={"name": "TrackNotFound"}))
    assert await client.get(Q) == {"result": "not_found"}


@pytest.mark.asyncio
@pytest.mark.parametrize("duration,result", [(213.0, "synced"), (217.0, "synced"), (212.9, "mismatch"), (218, "mismatch"),
                                             (None, "synced")])
async def test_a_record_more_than_two_seconds_off_is_not_used(duration, result):
    client, _ = client_for(lambda r: httpx.Response(200, json=rec(duration=duration)))
    out = await client.get(Q)
    assert out["result"] == result
    if result == "mismatch":
        assert "lines" not in out and out["record"]["duration"] == duration


@pytest.mark.asyncio
async def test_429_blocks_until_retry_after_passes():
    now = [100.0]
    client, seen = client_for(lambda r: httpx.Response(429, headers={"Retry-After": "30"}),
                              clock=lambda: now[0], jitter=lambda: 0.5)
    out = await client.get(Q)
    assert out["result"] == "rate_limited" and out["retry_after"] == 31
    now[0] += 20
    blocked = await client.get(Q)
    assert blocked["result"] == "rate_limited" and blocked["retry_after"] == 11
    assert len(seen) == 1  # nothing was sent while blocked
    now[0] += 11
    await client.get(Q)
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_429_without_retry_after_waits_the_default():
    now = [0.0]
    client, _ = client_for(lambda r: httpx.Response(429), clock=lambda: now[0], jitter=lambda: 0.0)
    out = await client.get(Q)
    assert out["retry_after"] == int(lyrics_mod.LRCLIB_DEFAULT_RETRY_AFTER_S)


def test_retry_after_reads_seconds_and_dates():
    assert parse_retry_after("12") == 12
    assert parse_retry_after("-5") == 0
    assert parse_retry_after("999999") == lyrics_mod.LRCLIB_MAX_RETRY_AFTER_S
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT", now=1445412470.0) == pytest.approx(10.0)
    assert parse_retry_after("soon") is None and parse_retry_after(None) is None


@pytest.mark.asyncio
async def test_a_timeout_and_a_server_error_are_errors():
    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)
    client, _ = client_for(slow)
    out = await client.get(Q)
    assert out["result"] == "error" and "did not answer in time" in out["error"]
    client, _ = client_for(lambda r: httpx.Response(503))
    assert (await client.get(Q)) == {"result": "error", "error": "lrclib.net answered HTTP 503"}
    client, _ = client_for(lambda r: httpx.Response(200, text="<html>"))
    assert "not JSON" in (await client.get(Q))["error"]
    client, _ = client_for(lambda r: httpx.Response(200, json={"name": "x"}))
    assert "no record id" in (await client.get(Q))["error"]


def test_the_query_key_is_normalised():
    a = LrclibQuery(artist="  The  Band ", title="SONG", album=None, duration=215)
    b = LrclibQuery(artist="the band", title="song", album="", duration=215)
    assert a.key() == b.key() == "the band|song||215"
    assert LrclibCache.make_key("sha256:x", a) == "sha256:x|the band|song||215"
    assert a.key() != LrclibQuery(artist="the band", title="song", album=None, duration=216).key()


@pytest.mark.asyncio
async def test_the_cache_keeps_found_records_and_negatives_for_a_week(tmp_path):
    now = [1_000_000.0]
    path = tmp_path / "lyrics_cache.json"
    cache = LrclibCache(path, clock=lambda: now[0])
    await cache.ensure_loaded()
    await cache.put("k_found", {"result": "synced", "record": {"id": 1}, "lines": [{"t_start": 1.0, "text": "a"}]})
    await cache.put("k_none", {"result": "not_found"})
    await cache.put("k_err", {"result": "error", "error": "x"})
    await cache.put("k_429", {"result": "rate_limited", "retry_after": 3})
    assert cache.get("k_err") is None and cache.get("k_429") is None
    assert cache.get("k_found")["lines"] == [{"t_start": 1.0, "text": "a"}]
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data["entries"]) == {"k_found", "k_none"} and data["entries"]["k_found"]["expires_at"] is None

    reloaded = LrclibCache(path, clock=lambda: now[0])
    await reloaded.ensure_loaded()
    assert reloaded.get("k_none")["result"] == "not_found"
    now[0] += lyrics_mod.LRCLIB_NEGATIVE_TTL_S + 1
    assert reloaded.get("k_none") is None
    assert reloaded.get("k_found") is not None


@pytest.mark.asyncio
async def test_the_cache_is_bounded_and_survives_a_corrupt_file(tmp_path):
    now = [0.0]
    path = tmp_path / "c.json"
    cache = LrclibCache(path, max_entries=2, clock=lambda: now[0])
    for i in range(3):
        now[0] += 1
        await cache.put(f"k{i}", {"result": "not_found"})
    assert cache.get("k0") is None and cache.get("k2") is not None
    path.write_text("{broken", encoding="utf-8")
    fresh = LrclibCache(path)
    await fresh.ensure_loaded()
    assert fresh.get("k2") is None
    assert any(p.name.startswith("c.json.corrupt-") for p in tmp_path.iterdir())


class _Library:
    def __init__(self, path):
        self.path = path

    def get_entry(self, track_id):
        return {"id": track_id, "title": "Song", "artist": "Band", "album": None, "duration_s": 215.2,
                "source_hash": "sha256:bb", "source": {"kind": "local", "name": self.path.name, "uri": ""}}

    def get_track_path(self, track_id):
        return self.path


@pytest.mark.asyncio
async def test_a_second_lookup_is_served_from_the_cache_with_the_network_off(tmp_path):
    from dpc_client_core.music.identity import MediaTools
    from dpc_client_core.music.lyrics import MusicLyrics

    track = "trk_00000000000000aa"
    audio = tmp_path / "Band - Song.mp3"
    audio.write_bytes(b"x")
    online = [True]

    def handler(request):
        if not online[0]:
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200, json=rec())
    client, seen = client_for(handler)
    svc = MusicLyrics(tmp_path / "dpc", _Library(audio), tools=MediaTools(), client=client)
    first = await svc.lookup_lrclib(track)
    assert first["applied"] is True and first["lrclib"]["cached"] is False
    online[0] = False
    again = await MusicLyrics(tmp_path / "dpc", _Library(audio), tools=MediaTools(), client=client).lookup_lrclib(track)
    assert again["lrclib"]["result"] == "synced" and again["lrclib"]["cached"] is True
    assert again["candidate"]["lines"] == first["lyrics"]["lines"]
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_a_cached_not_found_is_asked_again_after_its_ttl(tmp_path):
    from dpc_client_core.music.identity import MediaTools
    from dpc_client_core.music.lyrics import MusicLyrics

    track = "trk_00000000000000bb"
    audio = tmp_path / "Band - Song.mp3"
    audio.write_bytes(b"x")
    now = [5_000.0]
    client, seen = client_for(lambda r: httpx.Response(404))
    cache = LrclibCache(tmp_path / "dpc" / "music" / "lyrics_cache.json", clock=lambda: now[0])
    svc = MusicLyrics(tmp_path / "dpc", _Library(audio), tools=MediaTools(), client=client, cache=cache)
    assert (await svc.get(track))["lrclib"]["result"] == "not_found"
    second = await svc.get(track)
    assert second["lrclib"] == {**second["lrclib"], "result": "not_found", "cached": True}
    assert len(seen) == 1
    now[0] += lyrics_mod.LRCLIB_NEGATIVE_TTL_S + 1
    await svc.get(track)
    assert len(seen) == 2
