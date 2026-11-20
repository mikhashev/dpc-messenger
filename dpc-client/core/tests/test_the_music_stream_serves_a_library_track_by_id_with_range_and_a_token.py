"""The music track stream (ADR-051 N8, D28): a loopback HTTP door that serves a library
track's bytes by `track_id` only, with `Range` (206/416), a token and an `Origin`
allowlist (`dpc_client_core/music/stream.py`)."""

import asyncio
import socket

import aiohttp
import pytest
import pytest_asyncio

from dpc_client_core.local_api import ALLOWED_COMMANDS
from dpc_client_core.music import commands
from dpc_client_core.music.commands import MUSIC_COMMANDS, MusicCommandsMixin
from dpc_client_core.music.identity import MediaTools
from dpc_client_core.music.stream import (
    ALLOWED_ORIGINS, UNSATISFIABLE, MusicStreamServer, parse_range,
)

BODY = bytes(range(256)) * 40  # 10240 bytes, every offset distinguishable
SIZE = len(BODY)
TOKEN = "test-token-not-secret"


@pytest.fixture
def track(tmp_path):
    path = tmp_path / "Music" / "song.mp3"
    path.parent.mkdir()
    path.write_bytes(BODY)
    return path


@pytest_asyncio.fixture
async def server(track, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("not audio")
    known = {"trk_abc": track, "trk_notaudio": secret, "trk_gone": tmp_path / "gone.mp3"}
    s = MusicStreamServer(lambda tid: known.get(tid), port=0, token=TOKEN)
    await s.start()
    try:
        yield s
    finally:
        await s.stop()


async def _get(server, path, *, method="GET", token=TOKEN, headers=None, query=None):
    params = dict(query or {})
    if token is not None:
        params["t"] = token
    async with aiohttp.ClientSession() as session:
        async with session.request(method, f"{server.base_url}{path}", params=params,
                                   headers=headers or {}) as r:
            return r.status, dict(r.headers), await r.read()


# -- Range parsing ---------------------------------------------------------------------

def test_a_range_header_is_read_as_rfc_9110_single_ranges():
    assert parse_range(None, 100) is None
    assert parse_range("bytes=0-9", 100) == (0, 9)
    assert parse_range("bytes=90-", 100) == (90, 99)
    assert parse_range("bytes=-10", 100) == (90, 99)
    assert parse_range("bytes=-500", 100) == (0, 99)
    assert parse_range("bytes=50-5000", 100) == (50, 99)
    assert parse_range("bytes=100-", 100) == UNSATISFIABLE
    assert parse_range("bytes=-0", 100) == UNSATISFIABLE
    assert parse_range("bytes=0-", 0) == UNSATISFIABLE
    # Ignored, so the whole file: another unit, malformed, reversed, several ranges.
    assert parse_range("items=0-9", 100) is None
    assert parse_range("bytes=abc", 100) is None
    assert parse_range("bytes=9-0", 100) is None
    assert parse_range("bytes=0-1,5-6", 100) is None


# -- the route -------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_range_answers_200_with_the_whole_file(server):
    status, headers, body = await _get(server, "/music/track/trk_abc")
    assert status == 200 and body == BODY
    assert headers["Content-Type"] == "audio/mpeg"
    assert headers["Accept-Ranges"] == "bytes"
    assert headers["Content-Length"] == str(SIZE)
    assert headers["Cache-Control"] == "no-store"
    assert "Content-Range" not in headers
    assert "Access-Control-Allow-Origin" not in headers


@pytest.mark.asyncio
@pytest.mark.parametrize("spec,start,end", [
    ("bytes=0-99", 0, 99),
    ("bytes=1000-1999", 1000, 1999),
    ("bytes=10000-", 10000, SIZE - 1),
    ("bytes=-100", SIZE - 100, SIZE - 1),
    ("bytes=10200-99999", 10200, SIZE - 1),
])
async def test_each_range_form_answers_206_with_exactly_those_bytes(server, spec, start, end):
    status, headers, body = await _get(server, "/music/track/trk_abc", headers={"Range": spec})
    assert status == 206
    assert body == BODY[start:end + 1]
    assert headers["Content-Range"] == f"bytes {start}-{end}/{SIZE}"
    assert headers["Content-Length"] == str(end - start + 1)
    assert headers["Accept-Ranges"] == "bytes"


@pytest.mark.asyncio
async def test_a_range_past_the_end_is_416_with_the_size(server):
    status, headers, body = await _get(server, "/music/track/trk_abc", headers={"Range": f"bytes={SIZE}-"})
    assert status == 416
    assert headers["Content-Range"] == f"bytes */{SIZE}"
    assert body == b""


@pytest.mark.asyncio
async def test_a_multi_range_request_is_answered_whole(server):
    status, headers, body = await _get(server, "/music/track/trk_abc", headers={"Range": "bytes=0-1,5-6"})
    assert status == 200 and body == BODY


@pytest.mark.asyncio
async def test_head_answers_the_headers_without_a_body(server):
    status, headers, body = await _get(server, "/music/track/trk_abc", method="HEAD")
    assert status == 200 and body == b""
    assert headers["Content-Length"] == str(SIZE)
    assert headers["Accept-Ranges"] == "bytes"
    status, headers, body = await _get(server, "/music/track/trk_abc", method="HEAD", headers={"Range": "bytes=0-9"})
    assert status == 206 and body == b""
    assert headers["Content-Range"] == f"bytes 0-9/{SIZE}"


# -- the guard -------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_token_or_a_wrong_one_is_403(server):
    assert (await _get(server, "/music/track/trk_abc", token=None))[0] == 403
    assert (await _get(server, "/music/track/trk_abc", token="wrong"))[0] == 403
    assert (await _get(server, "/music/track/trk_abc", token=TOKEN + "x"))[0] == 403
    assert (await _get(server, "/music/track/trk_abc", token="тест"))[0] == 403


@pytest.mark.asyncio
async def test_an_origin_outside_the_webviews_is_403_and_an_allowed_one_is_echoed(server):
    status, headers, body = await _get(server, "/music/track/trk_abc", headers={"Origin": "https://evil.example"})
    assert status == 403 and body != BODY
    assert (await _get(server, "/music/track/trk_abc", headers={"Origin": "null"}))[0] == 403
    for origin in ("tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"):
        assert origin in ALLOWED_ORIGINS
        status, headers, body = await _get(server, "/music/track/trk_abc", headers={"Origin": origin})
        assert status == 200 and body == BODY
        assert headers["Access-Control-Allow-Origin"] == origin
        assert headers["Vary"] == "Origin"
    assert "*" not in ALLOWED_ORIGINS


@pytest.mark.asyncio
async def test_a_host_that_is_not_this_listener_is_403(server):
    status, _, body = await _get(server, "/music/track/trk_abc", headers={"Host": f"evil.example:{server.port}"})
    assert status == 403 and body != BODY
    status, _, body = await _get(server, "/music/track/trk_abc", headers={"Host": f"localhost:{server.port}"})
    assert status == 200 and body == BODY


@pytest.mark.asyncio
async def test_an_unknown_missing_or_non_audio_track_is_404(server):
    assert (await _get(server, "/music/track/trk_nope"))[0] == 404
    assert (await _get(server, "/music/track/trk_gone"))[0] == 404
    assert (await _get(server, "/music/track/trk_notaudio"))[0] == 404
    assert (await _get(server, "/music/track/..%2F..%2Fsecret.txt"))[0] == 404


@pytest.mark.asyncio
async def test_no_path_parameter_or_other_route_is_accepted(server, tmp_path):
    status, _, body = await _get(server, "/music/track/trk_nope", query={"path": str(tmp_path / "Music" / "song.mp3")})
    assert status == 400 and body != BODY
    status, _, body = await _get(server, "/music/track/trk_abc", query={"path": "/etc/passwd"})
    assert status == 400 and body != BODY
    assert (await _get(server, "/music/track/"))[0] == 404
    assert (await _get(server, "/music/"))[0] == 404
    assert (await _get(server, "/files/x/y"))[0] == 404
    assert (await _get(server, "/music/track/trk_abc", method="POST"))[0] == 405


@pytest.mark.asyncio
async def test_the_listener_binds_loopback_only(server):
    assert server.bound_address()[0] == "127.0.0.1"
    assert server.base_url == f"http://127.0.0.1:{server.port}"
    for host in ("0.0.0.0", "::", "192.168.1.2", "localhost"):
        with pytest.raises(ValueError):
            MusicStreamServer(lambda tid: None, host=host)


def test_each_listener_makes_its_own_random_token():
    a, b = MusicStreamServer(lambda tid: None), MusicStreamServer(lambda tid: None)
    assert a.token != b.token and len(a.token) >= 40


# -- through the music commands ----------------------------------------------------------

class _Settings:
    def __init__(self, home, port):
        self.dpc_home_dir = home
        self._port = port

    def get(self, section, key, fallback=None):
        if (section, key) == ("music", "poll_interval_s"):
            return "0.05"
        if (section, key) == ("music", "stream_port"):
            return self._port
        return fallback


class _Service(MusicCommandsMixin):
    def __init__(self, home, port="0"):
        self.settings = _Settings(home, port)


@pytest.fixture
def no_tools(monkeypatch):
    original = commands.MusicLibrary.__init__

    def init(self, home, **kw):
        kw.setdefault("tools", MediaTools(ffmpeg=None, ffprobe=None))
        kw.setdefault("save_delay_s", 0.01)
        original(self, home, **kw)
    monkeypatch.setattr(commands.MusicLibrary, "__init__", init)


def test_the_stream_info_command_is_a_music_command_the_local_api_allows():
    assert "get_music_stream_info" in MUSIC_COMMANDS and "get_music_stream_info" in ALLOWED_COMMANDS


@pytest.mark.asyncio
async def test_a_folder_track_plays_by_its_id_from_the_stream_info(tmp_path, track, no_tools):
    service = _Service(tmp_path / "dpc")
    try:
        await service.start_music_library()
        await service.start_music_stream()
        added = await service.add_music_folder(path=str(track.parent))
        await service.rescan_music_folder(folder_id=added["folder"]["id"])
        [entry] = (await service.list_music_library())["entries"]
        info = await service.get_music_stream_info()
        assert info["status"] == "ok" and info["base_url"].startswith("http://127.0.0.1:")
        async with aiohttp.ClientSession() as session:
            url = f"{info['base_url']}/music/track/{entry['id']}"
            async with session.get(url, params={"t": info["token"]}, headers={"Range": "bytes=-16"}) as r:
                assert r.status == 206 and await r.read() == BODY[-16:]
            async with session.get(url) as r:
                assert r.status == 403
    finally:
        await service.stop_music_stream()
        await service.stop_music_library()
    assert (await service.get_music_stream_info())["code"] == "stream_unavailable"


@pytest.mark.asyncio
async def test_a_taken_port_leaves_the_stream_off_and_says_why(tmp_path, no_tools):
    taken = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    taken.bind(("127.0.0.1", 0))
    taken.listen(1)
    port = taken.getsockname()[1]
    service = _Service(tmp_path / "dpc", port=str(port))
    try:
        await service.start_music_stream()
        info = await service.get_music_stream_info()
        assert info["status"] == "error" and info["code"] == "stream_unavailable"
        assert str(port) in info["message"] and "stream_port" in info["message"]
    finally:
        taken.close()
        await service.stop_music_stream()


@pytest.mark.asyncio
async def test_a_file_larger_than_one_chunk_is_streamed_whole_and_by_range(tmp_path):
    from dpc_client_core.music.stream import CHUNK_BYTES
    big = bytes((i * 7) % 251 for i in range(CHUNK_BYTES * 2 + 1234))
    path = tmp_path / "long.flac"
    path.write_bytes(big)
    s = MusicStreamServer(lambda tid: path if tid == "trk_long" else None, port=0, token=TOKEN)
    await s.start()
    try:
        status, headers, body = await _get(s, "/music/track/trk_long")
        assert status == 200 and body == big and headers["Content-Type"] == "audio/flac"
        start, end = CHUNK_BYTES - 10, CHUNK_BYTES * 2 + 10
        status, headers, body = await _get(s, "/music/track/trk_long", headers={"Range": f"bytes={start}-{end}"})
        assert status == 206 and body == big[start:end + 1]
    finally:
        await s.stop()


def test_the_webview_csp_lets_media_load_from_the_default_stream_port_and_nothing_wider():
    import json
    from pathlib import Path
    from dpc_client_core.music.stream import DEFAULT_STREAM_PORT
    conf = Path(__file__).resolve().parents[2] / "ui" / "src-tauri" / "tauri.conf.json"
    csp = json.loads(conf.read_text(encoding="utf-8"))["app"]["security"]["csp"]
    [media] = [d.strip() for d in csp.split(";") if d.strip().startswith("media-src")]
    assert f"http://127.0.0.1:{DEFAULT_STREAM_PORT}" in media.split()
    assert "http://127.0.0.1:*" not in media and "http://localhost:*" not in media and " * " not in f" {media} "


# -- Content-Length, never chunked (what a media pipeline needs to size the stream) ---------

async def _raw(server, request_lines, *, read_body=True, connection="close"):
    """One HTTP/1.1 request on a raw socket; the status line, the headers as a lowercase
    dict, and the body. Nothing between us and what the server put on the wire."""
    reader, writer = await asyncio.open_connection(server.host, server.port)
    try:
        host = f"{server.host}:{server.port}"
        head = "\r\n".join([*request_lines, f"Host: {host}", f"Connection: {connection}", "", ""])
        writer.write(head.encode("ascii"))
        await writer.drain()
        raw = await reader.read(-1)
    finally:
        writer.close()
    head_bytes, _, body = raw.partition(b"\r\n\r\n")
    lines = head_bytes.decode("latin-1").split("\r\n")
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return lines[0], headers, body


@pytest.mark.asyncio
async def test_an_open_ended_range_is_206_with_content_length_and_not_chunked(server):
    """The browser's first media request is `Range: bytes=0-`."""
    status_line, headers, body = await _raw(
        server, [f"GET /music/track/trk_abc?t={TOKEN} HTTP/1.1", "Range: bytes=0-"])
    assert status_line.split()[1] == "206"
    assert headers["content-range"] == f"bytes 0-{SIZE - 1}/{SIZE}"
    assert headers["content-length"] == str(SIZE)
    assert "transfer-encoding" not in headers
    assert headers["accept-ranges"] == "bytes"
    assert headers["content-type"] == "audio/mpeg"
    assert body == BODY


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("range_line,status", [(None, "200"), ("Range: bytes=100-199", "206"), ("Range: bytes=-50", "206")])
async def test_every_track_answer_has_content_length_and_is_not_chunked(server, method, range_line, status):
    lines = [f"{method} /music/track/trk_abc?t={TOKEN} HTTP/1.1"] + ([range_line] if range_line else [])
    status_line, headers, body = await _raw(server, lines)
    assert status_line.split()[1] == status
    assert "transfer-encoding" not in headers
    assert int(headers["content-length"]) == (SIZE if status == "200" else (100 if "100-199" in range_line else 50))
    assert headers["accept-ranges"] == "bytes"
    assert headers["content-type"] == "audio/mpeg"
    assert (body == b"") == (method == "HEAD")


@pytest.mark.asyncio
async def test_a_track_answer_carries_validators_and_a_stale_if_range_gets_the_whole_file(server):
    _, headers, _ = await _raw(server, [f"HEAD /music/track/trk_abc?t={TOKEN} HTTP/1.1"])
    assert headers["etag"].startswith('"') and headers["last-modified"].endswith("GMT")
    status, h, body = await _raw(server, [
        f"GET /music/track/trk_abc?t={TOKEN} HTTP/1.1", "Range: bytes=10-19", f"If-Range: {headers['etag']}"])
    assert status.split()[1] == "206" and body == BODY[10:20]
    status, h, body = await _raw(server, [
        f"GET /music/track/trk_abc?t={TOKEN} HTTP/1.1", "Range: bytes=10-19", 'If-Range: "stale"'])
    assert status.split()[1] == "200" and body == BODY and "content-range" not in h


# -- no-Range mode (`&ranges=0`, the Linux webview) ---------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("range_line", [None, "Range: bytes=100-199", "Range: bytes=-50", f"Range: bytes={SIZE + 5}-"])
async def test_ranges_0_answers_200_with_the_whole_file_and_no_accept_ranges(server, method, range_line):
    lines = [f"{method} /music/track/trk_abc?t={TOKEN}&ranges=0 HTTP/1.1"] + ([range_line] if range_line else [])
    status_line, headers, body = await _raw(server, lines)
    assert status_line.split()[1] == "200"
    assert headers["content-length"] == str(SIZE)
    assert "transfer-encoding" not in headers
    assert "accept-ranges" not in headers and "content-range" not in headers
    assert headers["content-type"] == "audio/mpeg"
    assert headers["cache-control"] == "private, max-age=3600"
    assert not {"etag", "last-modified", "x-content-type-options"} & set(headers)
    assert (body == b"") == (method == "HEAD")
    if method == "GET":
        assert body == BODY


@pytest.mark.asyncio
async def test_ranges_0_ignores_if_range_and_ranges_1_is_the_default_mode(server):
    status_line, headers, body = await _raw(server, [
        f"GET /music/track/trk_abc?t={TOKEN}&ranges=0 HTTP/1.1", "Range: bytes=10-19", 'If-Range: "x"'])
    assert status_line.split()[1] == "200" and body == BODY
    status_line, headers, body = await _raw(server, [
        f"GET /music/track/trk_abc?t={TOKEN}&ranges=1 HTTP/1.1", "Range: bytes=10-19"])
    assert status_line.split()[1] == "206" and body == BODY[10:20] and headers["accept-ranges"] == "bytes"


@pytest.mark.asyncio
async def test_ranges_0_streams_a_file_larger_than_one_chunk_whole(tmp_path):
    from dpc_client_core.music.stream import CHUNK_BYTES
    big = bytes((i * 7) % 251 for i in range(CHUNK_BYTES * 2 + 1234))
    path = tmp_path / "long.flac"
    path.write_bytes(big)
    s = MusicStreamServer(lambda tid: path if tid == "trk_long" else None, port=0, token=TOKEN)
    await s.start()
    try:
        status, headers, body = await _get(s, "/music/track/trk_long", headers={"Range": "bytes=5-9"},
                                           query={"ranges": "0"})
        assert status == 200 and body == big and "Accept-Ranges" not in headers
        assert headers["Content-Length"] == str(len(big))
    finally:
        await s.stop()


@pytest.mark.asyncio
async def test_an_empty_file_in_no_range_mode_is_200_with_a_zero_length(tmp_path):
    path = tmp_path / "empty.mp3"
    path.write_bytes(b"")
    s = MusicStreamServer(lambda tid: path if tid == "trk_e" else None, port=0, token=TOKEN)
    await s.start()
    try:
        status, headers, body = await _get(s, "/music/track/trk_e", query={"ranges": "0"})
        assert status == 200 and body == b"" and headers["Content-Length"] == "0"
    finally:
        await s.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["2", "", "true", "00", "-1"])
async def test_a_bad_ranges_value_is_400_and_so_is_a_repeated_one(server, bad):
    status, _, body = await _get(server, "/music/track/trk_abc", query={"ranges": bad})
    assert status == 400 and body != BODY
    status_line, _, body = await _raw(server, [f"GET /music/track/trk_abc?t={TOKEN}&ranges=0&ranges=1 HTTP/1.1"])
    assert status_line.split()[1] == "400" and body != BODY


@pytest.mark.asyncio
async def test_ranges_does_not_open_the_door_to_other_keys_or_skip_the_token(server):
    assert (await _get(server, "/music/track/trk_abc", query={"ranges": "0", "path": "/etc/passwd"}))[0] == 400
    assert (await _get(server, "/music/track/trk_abc", token=None, query={"ranges": "0"}))[0] == 403
    assert (await _get(server, "/music/track/trk_abc", token="wrong", query={"ranges": "0"}))[0] == 403
    assert (await _get(server, "/music/track/trk_abc", query={"ranges": "0"}, headers={"Origin": "https://evil.example"}))[0] == 403
    assert (await _get(server, "/music/track/trk_abc", query={"ranges": "0"}, headers={"Host": "evil.example"}))[0] == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("value,expected", [(None, False), ("false", False), ("true", True), ("1", True), ("garbage", False)])
async def test_the_stream_info_says_whether_linux_may_ask_for_ranges(tmp_path, value, expected):
    class _S(_Settings):
        def get(self, section, key, fallback=None):
            if (section, key) == ("music", "stream_ranges_linux"):
                return fallback if value is None else value
            return super().get(section, key, fallback)

    class _Svc(MusicCommandsMixin):
        def __init__(self):
            self.settings = _S(tmp_path / "dpc", "0")
    svc = _Svc()
    try:
        await svc.start_music_stream()
        assert (await svc.get_music_stream_info())["ranges_linux"] is expected
    finally:
        await svc.stop_music_stream()


@pytest.mark.asyncio
async def test_ranges_0_mirrors_the_9998_headers_and_closes_a_keep_alive_connection(server):
    """WebKitGTK plays the 9998 file server's answer with a full duration; see `_track`."""
    status_line, headers, body = await _raw(server, [
        f"GET /music/track/trk_abc?t={TOKEN}&ranges=0 HTTP/1.1", "Range: bytes=10-19",
        'If-Range: "x"', 'If-None-Match: "x"', "Origin: tauri://localhost"], connection="keep-alive")
    assert status_line.split()[1] == "200" and body == BODY  # returned: the server closed the socket
    assert headers["connection"].lower() == "close"
    assert headers["content-length"] == str(SIZE) and "transfer-encoding" not in headers
    assert headers["cache-control"] == "private, max-age=3600"
    assert not {"etag", "last-modified", "x-content-type-options", "accept-ranges"} & set(headers)
    assert headers["access-control-allow-origin"] == "tauri://localhost"


@pytest.mark.asyncio
async def test_ranges_0_never_sends_a_wildcard_origin(server):
    _, headers, _ = await _raw(server, [f"GET /music/track/trk_abc?t={TOKEN}&ranges=0 HTTP/1.1"])
    assert "access-control-allow-origin" not in headers


@pytest.mark.asyncio
async def test_ranges_1_headers_are_unchanged(server):
    _, headers, _ = await _raw(server, [f"GET /music/track/trk_abc?t={TOKEN}&ranges=1 HTTP/1.1"])
    assert headers["cache-control"] == "no-store" and headers["x-content-type-options"] == "nosniff"
    assert "etag" in headers and "last-modified" in headers and headers["accept-ranges"] == "bytes"
