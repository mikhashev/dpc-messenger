"""The music library's byte door to the webview (ADR-051 N8, decided as option (i), D28).

A loopback HTTP listener that serves a library track's bytes by its `track_id`, with
HTTP `Range` so the `<audio>` element can seek. It exists because the Linux webview
(WebKitGTK) does not play audio from Tauri's `asset:` protocol, and a `blob:` URL is a
live stream to its GStreamer pipeline (no duration, no seeking forward); and because the
asset scope is `$HOME/.dpc/**` only, so a track in a watched folder elsewhere cannot go
through `convertFileSrc` on any platform.

The one route is ``GET`` / ``HEAD`` ``/music/track/{track_id}?t=<token>``:

- **Keyed by `track_id`, never by a path.** The id is looked up in the library
  (`MusicLibrary.get_track_path`); the URL carries no path and any query key other than
  `t` is refused (400). An unknown, missing or fileless track is 404. The file must also
  have one of the library's own audio extensions (`identity.AUDIO_EXTENSIONS`) and be a
  regular file, so even a hand-edited `library.json` cannot make this serve a key file.
- **A token.** `secrets.token_urlsafe(32)`, made when the listener is created, held in
  memory only and handed to the UI by the authenticated `get_music_stream_info` command
  of `local_api`. It is compared in constant time; missing or wrong is 403. It is not the
  `~/.dpc/.ws_token`: that one opens every `local_api` command and lives on disk, and
  this one travels in a media URL (which a webview may log), so a leak of it reads
  library tracks and nothing else. A new core process makes a new token.
- **Host and Origin.** The `Host` header must be this listener's own loopback name and
  port (a DNS-rebinding page sends its own host): 403 otherwise. An `Origin`, when
  present, must be one of the DPC webview origins (`ALLOWED_ORIGINS`): 403 otherwise. A
  media element's request usually carries no `Origin`; then the token alone decides. An
  allowed `Origin` is echoed with `Vary: Origin`; there is never a `*`.
- **Loopback only.** The host must be `127.0.0.1` or `::1`; anything else is refused at
  construction. The UI's URL and the CSP `media-src` name `http://127.0.0.1:<port>`.
- **Range.** `bytes=a-b`, `bytes=a-` and `bytes=-n` answer 206 with `Content-Range`;
  a start at or past the end (or `-0`, or any range of an empty file) is 416 with
  `Content-Range: bytes */<size>`. No `Range`, a unit other than `bytes`, or a
  malformed one answers 200 with the whole file (RFC 9110 lets a server ignore `Range`).
  **A multi-range request (`bytes=0-1,5-6`) is answered 200 with the whole file**, never
  `multipart/byteranges`: no media element asks for it, and ignoring is allowed.
- The body is streamed in `CHUNK_BYTES` reads, never read whole. `Cache-Control:
  no-store`, because the URL carries the token. There is no directory listing and no
  other route (404), and other methods are 405.

The port is fixed, `[music] stream_port` (default `DEFAULT_STREAM_PORT`), because the
webview's CSP is static and names it. When the port is taken the listener does not
start, the reason is logged, and `get_music_stream_info` answers `stream_unavailable`:
the UI then falls back (Linux: `blob:` for files under `~/.dpc`). A changed port needs
the same port in `tauri.conf.json` `media-src` and a rebuilt app.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple, Union

from aiohttp import web

from .identity import AUDIO_EXTENSIONS, MIME_BY_EXTENSION

logger = logging.getLogger(__name__)

DEFAULT_STREAM_PORT = 9996  # 9997 the gateway, 9998 the file server, 9999 local_api
LOOPBACK_HOSTS = ("127.0.0.1", "::1")
CHUNK_BYTES = 256 * 1024
ROUTE = "/music/track/{track_id}"
TOKEN_PARAM = "t"

# The DPC webview's own origins: Tauri 2 on Linux and macOS (`tauri://localhost`), on
# Windows (`http://tauri.localhost`, or `https://` with `useHttpsScheme`), and the Vite
# dev server of `tauri dev` (`build.devUrl` in tauri.conf.json, `server.port` 1420).
ALLOWED_ORIGINS = frozenset({
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
    "http://localhost:1420",
    "http://127.0.0.1:1420",
})

# A library id is a prefix and hex (`library._new_id`); anything else is not one.
_TRACK_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_RANGE_SPEC = re.compile(r"^(\d*)-(\d*)$")

Unsatisfiable = str  # the marker `parse_range` returns for a 416
UNSATISFIABLE: Unsatisfiable = "unsatisfiable"


def parse_range(header: Optional[str], size: int) -> Union[None, Tuple[int, int], Unsatisfiable]:
    """The inclusive byte span `(start, end)` a `Range` header asks of a `size`-byte
    file, `None` to answer the whole file (no header, another unit, malformed, or
    several ranges), or `UNSATISFIABLE` (416)."""
    if not header:
        return None
    unit, sep, spec = header.strip().partition("=")
    if not sep or unit.strip().lower() != "bytes":
        return None
    spec = spec.strip()
    if "," in spec:
        return None  # multi-range: answered whole (module docstring)
    m = _RANGE_SPEC.match(spec)
    if not m or (not m.group(1) and not m.group(2)):
        return None
    first, last = m.group(1), m.group(2)
    if not first:  # suffix: the last n bytes
        n = int(last)
        if n == 0 or size == 0:
            return UNSATISFIABLE
        return max(0, size - n), size - 1
    start = int(first)
    end = int(last) if last else size - 1
    if last and end < start:
        return None  # invalid spec: ignored, as RFC 9110 allows
    if start >= size:
        return UNSATISFIABLE
    return start, min(end, size - 1)


class MusicStreamServer:
    """The loopback listener of the music byte door (module docstring)."""

    def __init__(
        self,
        resolve_track: Callable[[str], Optional[Path]],
        *,
        host: str = "127.0.0.1",
        port: int = DEFAULT_STREAM_PORT,
        token: Optional[str] = None,
    ):
        if host not in LOOPBACK_HOSTS:
            raise ValueError(f"the music stream listens on loopback only ({', '.join(LOOPBACK_HOSTS)}), not {host!r}")
        self._resolve_track = resolve_track
        self.host = host
        self.port = int(port)
        self.token = token or secrets.token_urlsafe(32)
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None

    # -- lifecycle -------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._site is not None

    def bound_address(self) -> Tuple[str, int]:
        if self._runner is None or not self._runner.addresses:
            raise RuntimeError("the music stream is not listening")
        address = self._runner.addresses[0]
        return str(address[0]), int(address[1])

    @property
    def base_url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    def info(self) -> Dict[str, str]:
        """What the UI needs to build a track URL: `{base_url, token}`."""
        return {"base_url": self.base_url, "token": self.token}

    async def start(self) -> None:
        app = web.Application(middlewares=[self._guard])
        # `add_get` also answers HEAD (aiohttp's `allow_head`).
        app.router.add_get(ROUTE, self._track)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        try:
            await site.start()
        except BaseException:
            await runner.cleanup()
            raise
        self._runner, self._site = runner, site
        self.port = self.bound_address()[1]
        logger.info("music: the track stream listens on %s (ADR-051 N8)", self.base_url)

    async def stop(self) -> None:
        runner, self._runner, self._site = self._runner, None, None
        if runner is not None:
            await runner.cleanup()
            logger.info("music: the track stream stopped")

    # -- the guard -------------------------------------------------------------------

    def _accepted_hosts(self) -> Tuple[str, ...]:
        names = ("127.0.0.1", "localhost") if self.host == "127.0.0.1" else ("[::1]", "localhost")
        return tuple(f"{name}:{self.port}" for name in names)

    @staticmethod
    def _refuse(status: int, text: str) -> web.Response:
        return web.Response(status=status, text=text + "\n",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @web.middleware
    async def _guard(self, request: web.Request, handler):
        """Host, then Origin, then the token, then the handler. Never logs the query,
        which carries the token."""
        host = request.headers.get("Host", "")
        if host.lower() not in self._accepted_hosts():
            logger.warning("music stream: refused %s %s, Host %r is not this listener", request.method, request.path, host)
            return self._refuse(403, "wrong host")
        origin = request.headers.get("Origin")
        if origin is not None and origin not in ALLOWED_ORIGINS:
            logger.warning("music stream: refused %s %s from Origin %r", request.method, request.path, origin)
            return self._refuse(403, "origin not allowed")
        presented = request.query.getall(TOKEN_PARAM, [])
        if len(presented) != 1 or not secrets.compare_digest(
                presented[0].encode("utf-8"), self.token.encode("utf-8")):
            logger.warning("music stream: refused %s %s, %s token", request.method, request.path,
                           "no" if not presented else "a wrong")
            return self._refuse(403, "token required")
        extra = sorted(set(request.query.keys()) - {TOKEN_PARAM})
        if extra:
            logger.warning("music stream: refused %s %s, unknown query keys %s", request.method, request.path, extra)
            return self._refuse(400, "unknown query parameter")
        return await handler(request)

    @staticmethod
    def _origin_headers(request: web.Request) -> Dict[str, str]:
        """An allowed `Origin` echoed back (the guard has refused any other), set
        before the body starts, since a streamed answer's headers go out first."""
        origin = request.headers.get("Origin")
        return {"Access-Control-Allow-Origin": origin, "Vary": "Origin"} if origin in ALLOWED_ORIGINS else {}

    # -- the route -------------------------------------------------------------------

    def _track_file(self, track_id: str) -> Optional[Path]:
        """The library's file for `track_id`, when it is one this door may serve."""
        if not _TRACK_ID.match(track_id):
            return None
        try:
            path = self._resolve_track(track_id)
        except Exception:
            logger.exception("music stream: resolving %s failed", track_id)
            return None
        if path is None:
            return None
        path = Path(path)
        if path.suffix.lower() not in AUDIO_EXTENSIONS:
            logger.warning("music stream: track %s has a non-audio file; not served", track_id)
            return None
        return path

    async def _track(self, request: web.Request) -> web.StreamResponse:
        track_id = request.match_info["track_id"]
        path = self._track_file(track_id)
        if path is None:
            return self._refuse(404, "no such track")
        try:
            f = await asyncio.to_thread(open, path, "rb")
        except FileNotFoundError:
            return self._refuse(404, "the track's file is gone")
        except OSError as exc:
            logger.warning("music stream: cannot open track %s: %s", track_id, exc)
            return self._refuse(404, "the track's file cannot be read")
        try:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode):
                return self._refuse(404, "no such track")
            size = st.st_size
            span = parse_range(request.headers.get("Range"), size)
            headers = {
                "Content-Type": MIME_BY_EXTENSION.get(path.suffix.lower(), "application/octet-stream"),
                "Accept-Ranges": "bytes",
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                **self._origin_headers(request),
            }
            if span == UNSATISFIABLE:
                headers["Content-Range"] = f"bytes */{size}"
                return web.Response(status=416, headers=headers)
            if span is None:
                status, start, end = 200, 0, size - 1
            else:
                status, (start, end) = 206, span
                headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            length = max(0, end - start + 1)
            response = web.StreamResponse(status=status, headers=headers)
            response.content_length = length
            await response.prepare(request)
            if request.method == "HEAD" or length == 0:
                await response.write_eof()
                return response
            await asyncio.to_thread(f.seek, start)
            remaining = length
            try:
                while remaining > 0:
                    chunk = await asyncio.to_thread(f.read, min(CHUNK_BYTES, remaining))
                    if not chunk:
                        break  # the file shrank under us; the client sees a short body
                    await response.write(chunk)
                    remaining -= len(chunk)
                await response.write_eof()
            except (ConnectionResetError, ConnectionError):
                # The element moved on (a seek opens a new request); nothing to report.
                logger.debug("music stream: the client left track %s mid-body", track_id)
            return response
        finally:
            f.close()
