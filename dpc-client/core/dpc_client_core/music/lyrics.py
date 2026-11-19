"""The lyrics chain of the music player (ADR-051 D6; SPEC §3 flows 11-13 and 12a, §6
"LRCLIB lookup").

**The chain**, per track, in order, stopping at the first hit:

1. `lrc`: a `.lrc` sidecar next to the audio file, same base name. Read only; it must
   hold timed lines (a sidecar with none is reported and skipped).
2. `tags`: lyrics embedded in the file, read through ffprobe (`run_probe`, like the
   library): the format and audio-stream tags `lyrics`, `lyrics-<lang>` (ffmpeg's name
   for an ID3 USLT frame, `lyrics-<description>-<lang>` with a description),
   `UNSYNCEDLYRICS` and `SYNCEDLYRICS`. Text with LRC stamps is taken as synced, other
   text as unsynced lines. ffprobe does not surface ID3 SYLT (synced) frames: checked
   with ffmpeg 8.1.2 on a hand-built tag holding both a USLT and a SYLT frame, where only
   the USLT came back. So SYLT-only files have no lyrics from this step.
3. `lrclib`: `GET https://lrclib.net/api/get` by artist, title, album and duration, when
   `[music] lrclib_enabled` is on (default on; Mike's call that LRCLIB is in the chain,
   2026-11-19, SPEC §12 decision 13). The record must match the track's duration within
   ±2 s; `syncedLyrics` is preferred, `plainLyrics` gives unsynced lines, and
   `instrumental` stops the chain with no lyrics.
4. `transcribe`: not run here. It is a later explicit action (a `transcribe` job); the
   chain reports it as `not_run`.
5. `manual`: always possible. `save_manual` stores the person's LRC with
   `source: "manual"`.

**Storage.** Lyrics live at `<DPC home>/music/lyrics/<track_id>.json` as `mp.lyrics/1`,
written atomically. Nothing is ever written next to the person's audio: the sidecar and
the tags are only read, and a manual edit goes to the DPC home, not to a `.lrc` beside
the file (ADR-051 Q4: tags in watched folders are never rewritten).

**Never overwrite.** Stored lyrics are returned as they are. `refresh` runs the chain
again and replaces what is stored only when it came from the chain (`lrc`, `tags`,
`lrclib`) and was not edited; manual or edited lyrics are kept and the new result is
offered as `candidate`. An explicit LRCLIB lookup stores its result only when nothing is
stored or the caller passes `replace`; otherwise it too answers with a `candidate`.

**LRCLIB.** The call runs in core with httpx (already a core dependency), never in the
webview. Requests are serialised with a small gap, carry an identified User-Agent
(`dpc-messenger-music/<version> (+<project page>)`, the form the LRCLIB docs ask for),
and honour `429 Retry-After`: until it passes no request is sent. Timeouts are short. The
outcome is cached in `<DPC home>/music/lyrics_cache.json` under the track's hash plus the
normalised query (`artist|title|album|duration`): found records for good, `not_found`,
`instrumental`, `mismatch` and `empty` for `LRCLIB_NEGATIVE_TTL_S` (7 days), errors and
rate limits never. After a network error the automatic chain skips LRCLIB for
`LRCLIB_ERROR_BACKOFF_S`, so opening tracks offline does not retry on every open; an
explicit lookup still tries. **Egress:** the query sends the artist, title, album and
duration (and this machine's IP) to lrclib.net; no audio, no path, no hash.
"""

from __future__ import annotations

import asyncio
import copy
import email.utils
import json
import locale
import logging
import math
import os
import random
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from . import schemas
from .identity import FFPROBE_TIMEOUT_S, MediaTools
from .library import MusicLibraryError
from .store import atomic_write_json, atomic_write_text, music_dir_of

logger = logging.getLogger(__name__)

LYRICS_DIR = "lyrics"
LRCLIB_CACHE_FILE = "lyrics_cache.json"

LRCLIB_BASE_URL = "https://lrclib.net"
PROJECT_URL = "https://github.com/mikhashev/dpc-messenger"
LRCLIB_DURATION_TOLERANCE_S = 2.0
LRCLIB_NEGATIVE_TTL_S = 7 * 24 * 3600
LRCLIB_CONNECT_TIMEOUT_S = 4.0
LRCLIB_TIMEOUT_S = 8.0
LRCLIB_MIN_GAP_S = 0.3            # between two requests (the docs: one at a time, 200-500 ms apart)
LRCLIB_DEFAULT_RETRY_AFTER_S = 60.0
LRCLIB_MAX_RETRY_AFTER_S = 3600.0
LRCLIB_ERROR_BACKOFF_S = 60.0     # the automatic chain skips LRCLIB this long after a network error
LRCLIB_CACHE_MAX_ENTRIES = 5000

SIDECAR_MAX_BYTES = 1024 * 1024
LRC_TEXT_MAX_CHARS = 512 * 1024

# Results of one LRCLIB lookup.
FOUND_RESULTS = ("synced", "plain")
NEGATIVE_RESULTS = ("not_found", "instrumental", "mismatch", "empty")
CACHEABLE_RESULTS = FOUND_RESULTS + NEGATIVE_RESULTS

# Tag names that hold lyrics, compared case-folded; `lyrics-*` is matched by prefix.
_LYRICS_TAG_NAMES = ("lyrics", "syncedlyrics", "unsyncedlyrics", "unsynced lyrics")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# -- LRC ---------------------------------------------------------------------------

_T = r"(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)"
_LEAD = re.compile(r"^\[" + _T + r"\]")
_END = re.compile(r"<" + _T + r">\s*$")
_WORD = re.compile(r"<" + _T + r">")
_META = re.compile(r"^\[([A-Za-z#][A-Za-z0-9_#-]*):(.*)\]$")
_SPACES = re.compile(r"\s+")


def _seconds(minutes: str, seconds: str) -> float:
    return round(int(minutes) * 60 + float(seconds.replace(":", ".")), 3)


@dataclass
class ParsedLrc:
    """`lines` are `mp.lyrics/1` lines (timed, sorted); `untimed` the lines that had no
    stamp; `offset_ms` the `[offset:]` that was applied."""

    lines: List[dict]
    language: Optional[str]
    untimed: List[str]
    offset_ms: int = 0


def parse_lrc(text: str) -> ParsedLrc:
    """Parse the LRC dialect of SPEC §4 (the prototype's `parseLrc`).

    `[mm:ss.xx] text`, also `[mm:ss]`, `[mm:ss.xxx]` and `[mm:ss:xx]`; several leading
    stamps repeat the line at each time; a trailing `<mm:ss.xx>` is the line's end;
    inner `<mm:ss.xx>` word times (enhanced LRC) are dropped; an empty stamped line ends
    the previous line; `[la:xx]` is the language and `[offset:±ms]` shifts every stamp
    (a positive offset shows the lyrics earlier, as in the LRC convention); other
    `[tag:value]` lines are ignored; a line with no stamp is counted in `untimed`."""
    raw: List[Tuple[float, Optional[float], str, int]] = []
    untimed: List[str] = []
    language: Optional[str] = None
    offset_ms = 0
    for n, line in enumerate(str(text).splitlines()):
        s = line.strip().lstrip("﻿").strip()
        if not s:
            continue
        meta = _META.match(s)
        if meta:
            key, value = meta.group(1).lower(), meta.group(2).strip()
            if key in ("la", "lang", "language"):
                language = value or None
            elif key == "offset":
                try:
                    offset_ms = int(float(value))
                except ValueError:
                    pass
            continue
        times: List[float] = []
        rest = s
        while True:
            m = _LEAD.match(rest)
            if not m:
                break
            times.append(_seconds(m.group(1), m.group(2)))
            rest = rest[m.end():].lstrip()
        if not times:
            untimed.append(s)
            continue
        t_end: Optional[float] = None
        end = _END.search(rest)
        if end:
            t_end = _seconds(end.group(1), end.group(2))
            rest = rest[:end.start()]
        words = _SPACES.sub(" ", _WORD.sub("", rest)).strip()
        for t in times:
            raw.append((t, t_end, words, n))
    shift = offset_ms / 1000.0
    if shift:
        raw = [(max(0.0, round(t - shift, 3)), None if e is None else max(0.0, round(e - shift, 3)), w, n)
               for t, e, w, n in raw]
    raw.sort(key=lambda r: (r[0], r[3]))
    lines: List[dict] = []
    for t, t_end, words, _ in raw:
        if not words:
            if lines and "t_end" not in lines[-1] and t > lines[-1]["t_start"]:
                lines[-1]["t_end"] = t
            continue
        out: Dict[str, Any] = {"t_start": t, "text": words}
        if t_end is not None and t_end > t:
            out["t_end"] = t_end
        lines.append(out)
    return ParsedLrc(lines=lines, language=language, untimed=untimed, offset_ms=offset_ms)


def lrc_stamp(t: Optional[float]) -> str:
    """`mm:ss.xx` for a time in seconds."""
    t = max(0.0, float(t or 0.0))
    m = int(t // 60)
    s = round((t - m * 60) * 100) / 100
    if s >= 60:
        m += 1
        s -= 60
    return f"{m:02d}:{s:05.2f}"


def format_lrc(lyrics: Optional[dict]) -> str:
    """LRC text for `mp.lyrics/1` (the prototype's `toLrc`): unsynced lines are written
    without a stamp, so an edit can time them."""
    if not lyrics:
        return ""
    out: List[str] = []
    if lyrics.get("language"):
        out.append(f"[la:{lyrics['language']}]")
    for line in lyrics.get("lines") or []:
        if line.get("t_start") is None:
            out.append(line["text"])
            continue
        end = f" <{lrc_stamp(line['t_end'])}>" if line.get("t_end") is not None else ""
        out.append(f"[{lrc_stamp(line['t_start'])}] {line['text']}{end}")
    return "\n".join(out)


def plain_lines(text: str) -> List[dict]:
    """Unsynced lines from plain text: one per non-empty line, `t_start` null."""
    return [{"t_start": None, "text": s.strip()} for s in str(text).splitlines() if s.strip()]


def lines_from_text(text: str) -> Tuple[List[dict], bool, Optional[str]]:
    """`(lines, synced, language)` of a lyrics text that may or may not be LRC."""
    parsed = parse_lrc(text)
    if parsed.lines:
        return parsed.lines, True, parsed.language
    return plain_lines(text), False, parsed.language


def make_lyrics(track_hash: str, source: str, lines: List[dict], *, synced: bool = True,
                language: Optional[str] = None, edited: bool = False,
                lrclib: Optional[dict] = None) -> dict:
    record: Dict[str, Any] = {
        "schema": schemas.LYRICS,
        "track_hash": track_hash,
        "source": source,
        "language": language,
        "lines": copy.deepcopy(lines),
        "updated_at": _now_iso(),
    }
    if not synced:
        record["synced"] = False
    if edited:
        record["edited"] = True
    if lrclib is not None:
        record["lrclib"] = dict(lrclib)
    schemas.validate_lyrics(record)
    return record


# -- reading the person's files (never writing) --------------------------------------

def _decode(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    preferred = locale.getpreferredencoding(False) or "latin-1"
    try:
        return data.decode(preferred)
    except (UnicodeDecodeError, LookupError):
        return data.decode("latin-1")


def find_sidecar(audio: Path) -> Optional[Path]:
    """The `.lrc` next to `audio` with the same base name (`.lrc` or `.LRC`), or None."""
    for suffix in (".lrc", ".LRC", ".Lrc"):
        candidate = audio.with_suffix(suffix)
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def read_sidecar(audio: Path) -> Optional[Tuple[Path, str]]:
    """`(path, text)` of the sidecar, or None. Blocking; opened `rb` only. Raises
    `OSError` when it exists but cannot be read, and `ValueError` when it is too big."""
    sidecar = find_sidecar(audio)
    if sidecar is None:
        return None
    with open(sidecar, "rb") as f:
        data = f.read(SIDECAR_MAX_BYTES + 1)
    if len(data) > SIDECAR_MAX_BYTES:
        raise ValueError(f"{sidecar.name} is larger than {SIDECAR_MAX_BYTES} bytes")
    return sidecar, _decode(data)


def read_tags(path: Path, tools: MediaTools) -> Optional[Dict[str, Any]]:
    """`{"tags": {...}, "duration_s": float|None}` from ffprobe (format tags over the
    first audio stream's tags), or None without ffprobe or when it fails. Blocking."""
    if not tools.ffprobe:
        return None
    cmd = [tools.ffprobe, "-v", "error", "-show_format", "-show_streams",
           "-select_streams", "a:0", "-of", "json", str(path)]
    try:
        done = tools.runner(cmd, timeout=FFPROBE_TIMEOUT_S)
    except Exception as exc:
        logger.info("music: ffprobe could not run on %s: %s", path, exc)
        return None
    if done.returncode != 0:
        return None
    raw = done.stdout.decode("utf-8", "replace") if isinstance(done.stdout, bytes) else (done.stdout or "")
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    stream = streams[0] if streams and isinstance(streams[0], dict) else {}
    tags: Dict[str, Any] = {}
    tags.update(stream.get("tags") or {})
    tags.update(fmt.get("tags") or {})
    duration = None
    for value in (fmt.get("duration"), stream.get("duration")):
        try:
            duration = float(value)
            break
        except (TypeError, ValueError):
            continue
    return {"tags": tags, "duration_s": duration}


def pick_lyrics_tag(tags: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """`(tag name, text)` of the lyrics tag to use: the first that holds LRC stamps,
    else the first non-empty one, in the order of `_LYRICS_TAG_NAMES` then `lyrics-*`."""
    named: List[Tuple[str, str]] = []
    prefixed: List[Tuple[str, str]] = []
    for key, value in tags.items():
        if not isinstance(key, str) or not isinstance(value, str) or not value.strip():
            continue
        k = key.casefold()
        if k in _LYRICS_TAG_NAMES:
            named.append((key, value))
        elif k.startswith("lyrics-") or k.startswith("lyrics_"):
            prefixed.append((key, value))
    named.sort(key=lambda kv: _LYRICS_TAG_NAMES.index(kv[0].casefold()))
    candidates = named + sorted(prefixed, key=lambda kv: kv[0].casefold())
    for key, value in candidates:
        if parse_lrc(value).lines:
            return key, value
    return candidates[0] if candidates else None


_SPLIT = re.compile(r"^(.+?)\s+[-–—]\s+(.+)$")


def parse_artist_title(file_name: str) -> Tuple[str, str]:
    """`Artist - Title.ext` (also en and em dashes) → (artist, title); without a match
    the whole base name is the title and the artist is empty."""
    base = re.sub(r"\.[^./\\]+$", "", str(file_name or "")).strip()
    m = _SPLIT.match(base)
    return (m.group(1).strip(), m.group(2).strip()) if m else ("", base)


# -- LRCLIB --------------------------------------------------------------------------

def _norm(text: Optional[str]) -> str:
    return _SPACES.sub(" ", unicodedata.normalize("NFKC", text or "")).strip().casefold()


@dataclass
class LrclibQuery:
    artist: str
    title: str
    album: Optional[str]
    duration: Optional[int]

    def key(self) -> str:
        return "|".join((_norm(self.artist), _norm(self.title), _norm(self.album),
                         "" if self.duration is None else str(self.duration)))

    def params(self) -> Dict[str, str]:
        p = {"artist_name": self.artist, "track_name": self.title}
        if self.album:
            p["album_name"] = self.album
        if self.duration is not None:
            p["duration"] = str(self.duration)
        return p

    def as_dict(self) -> Dict[str, Any]:
        return {"artist": self.artist, "title": self.title, "album": self.album, "duration": self.duration}


def default_user_agent() -> str:
    try:
        from ..__version__ import __version__ as version
    except Exception:  # pragma: no cover - the version module always imports
        version = "unknown"
    return f"dpc-messenger-music/{version} (+{PROJECT_URL})"


def parse_retry_after(value: Optional[str], now: Optional[float] = None) -> Optional[float]:
    """Seconds to wait from a `Retry-After` header (seconds or an HTTP date), capped."""
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = when.timestamp() - (time.time() if now is None else now)
    if math.isnan(seconds):
        return None
    return max(0.0, min(seconds, LRCLIB_MAX_RETRY_AFTER_S))


class LrclibClient:
    """One request at a time to `GET /api/get`, with the User-Agent, the gap between
    requests and the `Retry-After` block. `transport` is for tests (an
    `httpx.MockTransport`); None uses the network."""

    def __init__(self, *, transport: Optional[httpx.AsyncBaseTransport] = None,
                 base_url: str = LRCLIB_BASE_URL, user_agent: Optional[str] = None,
                 min_gap_s: float = LRCLIB_MIN_GAP_S,
                 clock: Callable[[], float] = time.monotonic,
                 jitter: Callable[[], float] = lambda: random.uniform(0.0, 1.0)):
        self._transport = transport
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent or default_user_agent()
        self.min_gap_s = min_gap_s
        self._clock = clock
        self._jitter = jitter
        self._lock: Optional[asyncio.Lock] = None
        self._last_sent = -math.inf
        self.blocked_until = -math.inf
        self.requests_sent = 0

    def retry_after_s(self) -> Optional[float]:
        left = self.blocked_until - self._clock()
        return math.ceil(left) if left > 0 else None

    async def get(self, q: LrclibQuery) -> Dict[str, Any]:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            wait = self.retry_after_s()
            if wait is not None:
                return {"result": "rate_limited", "retry_after": wait,
                        "error": f"lrclib.net asked to wait; {wait} s left"}
            gap = self._last_sent + self.min_gap_s - self._clock()
            if gap > 0:
                await asyncio.sleep(gap)
            self._last_sent = self._clock()
            self.requests_sent += 1
            timeout = httpx.Timeout(LRCLIB_TIMEOUT_S, connect=LRCLIB_CONNECT_TIMEOUT_S)
            try:
                async with httpx.AsyncClient(transport=self._transport, timeout=timeout,
                                             headers={"User-Agent": self.user_agent},
                                             follow_redirects=False) as client:
                    response = await client.get(f"{self.base_url}/api/get", params=q.params())
            except httpx.TimeoutException as exc:
                return {"result": "error", "error": f"lrclib.net did not answer in time ({type(exc).__name__})"}
            except httpx.HTTPError as exc:
                return {"result": "error", "error": f"could not reach lrclib.net ({type(exc).__name__}: {exc})"}
            return self._read(response, q)

    def _read(self, response: httpx.Response, q: LrclibQuery) -> Dict[str, Any]:
        status = response.status_code
        if status == 404:
            return {"result": "not_found"}
        if status == 429:
            wait = parse_retry_after(response.headers.get("retry-after"))
            if wait is None:
                wait = LRCLIB_DEFAULT_RETRY_AFTER_S
            self.blocked_until = self._clock() + wait + self._jitter()
            left = self.retry_after_s() or 0
            return {"result": "rate_limited", "retry_after": left,
                    "error": f"lrclib.net answered 429 (rate limited); retry after {left} s"}
        if not 200 <= status < 300:
            return {"result": "error", "error": f"lrclib.net answered HTTP {status}"}
        try:
            rec = response.json()
        except ValueError:
            return {"result": "error", "error": "lrclib.net answered, but the body is not JSON"}
        if not isinstance(rec, dict) or not isinstance(rec.get("id"), int) or isinstance(rec.get("id"), bool):
            return {"result": "error", "error": "lrclib.net answered, but with no record id"}
        duration = rec.get("duration")
        meta = {
            "id": rec["id"],
            "artist_name": str(rec.get("artistName") or ""),
            "track_name": str(rec.get("trackName") or ""),
            "album_name": None if rec.get("albumName") is None else str(rec.get("albumName")),
            "duration": duration if isinstance(duration, (int, float)) and not isinstance(duration, bool) else None,
        }
        if rec.get("instrumental") is True:
            return {"result": "instrumental", "record": {**meta, "synced": False}}
        if (q.duration is not None and meta["duration"] is not None
                and abs(meta["duration"] - q.duration) > LRCLIB_DURATION_TOLERANCE_S):
            return {"result": "mismatch", "record": {**meta, "synced": False}}
        synced = rec.get("syncedLyrics")
        if isinstance(synced, str):
            parsed = parse_lrc(synced)
            if parsed.lines:
                return {"result": "synced", "record": {**meta, "synced": True}, "lines": parsed.lines}
        plain = rec.get("plainLyrics")
        if isinstance(plain, str) and plain_lines(plain):
            return {"result": "plain", "record": {**meta, "synced": False}, "lines": plain_lines(plain)}
        return {"result": "empty", "record": {**meta, "synced": False}}


class LrclibCache:
    """The LRCLIB outcomes on disk, keyed by track hash plus normalised query."""

    def __init__(self, path: Path, *, negative_ttl_s: float = LRCLIB_NEGATIVE_TTL_S,
                 max_entries: int = LRCLIB_CACHE_MAX_ENTRIES, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.negative_ttl_s = negative_ttl_s
        self.max_entries = max_entries
        self._clock = clock
        self._entries: Optional[Dict[str, dict]] = None
        self._disk_lock = threading.Lock()
        self._seq = 0
        self._written = 0

    def _load(self) -> Dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            entries = data.get("entries") if isinstance(data, dict) else None
            if not isinstance(entries, dict):
                raise ValueError("no 'entries' object")
        except (ValueError, UnicodeDecodeError, OSError) as exc:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            aside = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
            logger.error("music: %s does not parse (%s); moved aside to %s", self.path, exc, aside.name)
            try:
                os.replace(self.path, aside)
            except OSError:
                pass
            return {}
        return {k: v for k, v in entries.items() if isinstance(k, str) and isinstance(v, dict)}

    async def ensure_loaded(self) -> None:
        if self._entries is None:
            entries = await asyncio.to_thread(self._load)
            if self._entries is None:
                self._entries = entries

    @staticmethod
    def make_key(track_hash: str, q: LrclibQuery) -> str:
        return f"{track_hash}|{q.key()}"

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        entry = (self._entries or {}).get(key)
        if entry is None:
            return None
        expires = entry.get("expires_at")
        if isinstance(expires, (int, float)) and expires <= self._clock():
            del self._entries[key]
            return None
        if entry.get("result") not in CACHEABLE_RESULTS:
            return None
        return copy.deepcopy(entry)

    async def put(self, key: str, outcome: Dict[str, Any]) -> None:
        if outcome.get("result") not in CACHEABLE_RESULTS:
            return
        await self.ensure_loaded()
        now = self._clock()
        entry = {k: copy.deepcopy(v) for k, v in outcome.items() if k in ("result", "record", "lines")}
        entry["fetched_at"] = now
        entry["expires_at"] = None if outcome["result"] in FOUND_RESULTS else now + self.negative_ttl_s
        self._entries[key] = entry
        if len(self._entries) > self.max_entries:
            for old in sorted(self._entries, key=lambda k: self._entries[k].get("fetched_at") or 0)[
                    :len(self._entries) - self.max_entries]:
                del self._entries[old]
        self._seq += 1
        seq, text = self._seq, json.dumps({"version": 1, "entries": self._entries}, ensure_ascii=False)
        try:
            await asyncio.to_thread(self._write, seq, text)
        except OSError:
            logger.exception("music: writing the LRCLIB cache failed")

    def _write(self, seq: int, text: str) -> None:
        with self._disk_lock:
            if seq <= self._written:
                return
            atomic_write_text(self.path, text)
            self._written = seq


# -- the service ---------------------------------------------------------------------

def _step(step: str, state: str, detail: str = "") -> Dict[str, str]:
    return {"step": step, "state": state, "detail": detail}


class MusicLyrics:
    """The lyrics chain over one `MusicLibrary`. Every method runs on the service's
    event loop; file reads, ffprobe and cache writes leave it through
    `asyncio.to_thread`, and the LRCLIB request is asynchronous."""

    def __init__(self, dpc_home_dir: Path, library: Any, *,
                 lrclib_enabled: Callable[[], bool] = lambda: True,
                 tools: Optional[MediaTools] = None,
                 client: Optional[LrclibClient] = None,
                 cache: Optional[LrclibCache] = None,
                 clock: Callable[[], float] = time.monotonic):
        self.library = library
        music_root = music_dir_of(dpc_home_dir)
        self.root = music_root / LYRICS_DIR
        self.cache = cache or LrclibCache(music_root / LRCLIB_CACHE_FILE)
        self.client = client or LrclibClient()
        self._lrclib_enabled = lrclib_enabled
        self._tools = tools
        self._clock = clock
        self._last_error_at = -math.inf
        self._locks: Dict[str, asyncio.Lock] = {}

    # -- storage ---------------------------------------------------------------

    def lyrics_path(self, track_id: str) -> Path:
        if not isinstance(track_id, str) or not schemas.TRACK_ID_RE.match(track_id):
            raise MusicLibraryError("invalid_track_id", f"not a track id: {track_id!r}")
        return self.root / f"{track_id}.json"

    def _read_stored(self, path: Path) -> Optional[dict]:
        if not path.exists():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            schemas.validate_lyrics(record)
            return record
        except (ValueError, UnicodeDecodeError, schemas.SchemaError) as exc:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            aside = path.with_name(f"{path.name}.corrupt-{stamp}")
            logger.error("music: %s is not valid lyrics (%s); moved aside to %s", path, exc, aside.name)
            os.replace(path, aside)
            return None

    async def load_stored(self, track_id: str) -> Optional[dict]:
        return await asyncio.to_thread(self._read_stored, self.lyrics_path(track_id))

    async def _store(self, track_id: str, record: dict) -> None:
        schemas.validate_lyrics(record)
        await asyncio.to_thread(atomic_write_json, self.lyrics_path(track_id), record)

    def _lock(self, track_id: str) -> asyncio.Lock:
        lock = self._locks.get(track_id)
        if lock is None:
            lock = self._locks[track_id] = asyncio.Lock()
        return lock

    async def _media_tools(self) -> MediaTools:
        if self._tools is None:
            self._tools = await asyncio.to_thread(MediaTools.discover)
        return self._tools

    def lrclib_enabled(self) -> bool:
        try:
            return bool(self._lrclib_enabled())
        except Exception:
            logger.exception("music: reading the LRCLIB setting failed; treating it as off")
            return False

    # -- the query ---------------------------------------------------------------

    @staticmethod
    def build_query(entry: dict, duration_s: Optional[float], *, artist: Optional[str] = None,
                    title: Optional[str] = None, album: Optional[str] = None) -> LrclibQuery:
        """Artist and title from the entry's tags (ffprobe), else from the file name
        `Artist - Title`; overrides from the person win. The duration is rounded to
        whole seconds, None outside 1-3600 s."""
        e_artist = (entry.get("artist") or "").strip()
        e_title = (entry.get("title") or "").strip()
        if not e_artist:
            f_artist, f_title = parse_artist_title((entry.get("source") or {}).get("name") or e_title)
            e_artist = f_artist
            if f_artist:
                e_title = f_title
        q_artist = artist.strip() if isinstance(artist, str) and artist.strip() else e_artist
        q_title = title.strip() if isinstance(title, str) and title.strip() else e_title
        q_album = album.strip() if isinstance(album, str) and album.strip() else (entry.get("album") or None)
        duration = None
        if duration_s is not None and 1 <= round(duration_s) <= 3600:
            duration = int(round(duration_s))
        return LrclibQuery(artist=q_artist, title=q_title, album=q_album, duration=duration)

    async def _lrclib(self, track_hash: str, q: LrclibQuery) -> Dict[str, Any]:
        """The outcome for a query: from the cache, else from lrclib.net (then cached)."""
        await self.cache.ensure_loaded()
        key = LrclibCache.make_key(track_hash, q)
        hit = self.cache.get(key)
        if hit is not None:
            hit.pop("fetched_at", None)
            hit.pop("expires_at", None)
            return {**hit, "cached": True}
        outcome = await self.client.get(q)
        if outcome.get("result") == "error":
            self._last_error_at = self._clock()
        await self.cache.put(key, outcome)
        return {**outcome, "cached": False}

    @staticmethod
    def _lrclib_answer(q: Optional[LrclibQuery], outcome: Dict[str, Any]) -> Dict[str, Any]:
        out = {"result": outcome["result"], "cached": bool(outcome.get("cached")),
               "query": q.as_dict() if q else None, "record": outcome.get("record")}
        for k in ("retry_after", "error"):
            if k in outcome:
                out[k] = outcome[k]
        return out

    @staticmethod
    def _lrclib_lyrics(track_hash: str, outcome: Dict[str, Any]) -> dict:
        return make_lyrics(track_hash, "lrclib", outcome["lines"], synced=outcome["result"] == "synced",
                           lrclib=outcome["record"])

    async def _duration(self, entry: dict, path: Optional[Path], probed: Optional[Dict[str, Any]]) -> Optional[float]:
        if entry.get("duration_s") is not None:
            return float(entry["duration_s"])
        if probed is not None:
            return probed.get("duration_s")
        if path is None:
            return None
        tools = await self._media_tools()
        info = await asyncio.to_thread(read_tags, path, tools)
        return info.get("duration_s") if info else None

    # -- the commands ------------------------------------------------------------

    async def get(self, track_id: str, *, refresh: bool = False) -> Dict[str, Any]:
        """The track's lyrics: stored ones as they are, else (or with `refresh`) the
        chain's result, stored. See the module docstring for the never-overwrite rule."""
        lyrics_file = self.lyrics_path(track_id)
        async with self._lock(track_id):
            entry = self.library.get_entry(track_id)
            stored = await asyncio.to_thread(self._read_stored, lyrics_file)
            enabled = self.lrclib_enabled()
            if stored is not None and not refresh:
                return {"lyrics": stored, "from_store": True, "chain": [], "lrclib": None,
                        "lrclib_enabled": enabled}
            found, chain, lrclib = await self._run_chain(entry, enabled)
            answer: Dict[str, Any] = {"lyrics": stored, "from_store": False, "chain": chain,
                                      "lrclib": lrclib, "lrclib_enabled": enabled}
            if found is None:
                return answer
            if stored is not None and (stored.get("source") == "manual" or stored.get("edited")):
                answer["candidate"] = found
                answer["kept"] = "the stored lyrics are the person's edit, so they are kept"
                return answer
            await self._store(track_id, found)
            answer["lyrics"] = found
            return answer

    async def _run_chain(self, entry: dict, lrclib_enabled: bool) -> Tuple[Optional[dict], List[dict], Optional[dict]]:
        track_hash = entry["source_hash"]
        path = self.library.get_track_path(entry["id"])
        chain: List[dict] = []
        found: Optional[dict] = None

        # (a) the .lrc sidecar
        if path is None:
            chain.append(_step("lrc", "skipped", "the track has no file now (missing or a web track)"))
        else:
            try:
                sidecar = await asyncio.to_thread(read_sidecar, path)
            except (OSError, ValueError) as exc:
                sidecar = None
                chain.append(_step("lrc", "error", f"the sidecar could not be read: {exc}"))
            else:
                if sidecar is None:
                    chain.append(_step("lrc", "none", f"no {path.stem}.lrc next to the file"))
                else:
                    parsed = parse_lrc(sidecar[1])
                    if parsed.lines:
                        found = make_lyrics(track_hash, "lrc", parsed.lines, language=parsed.language)
                        extra = f"; {len(parsed.untimed)} untimed line(s) skipped" if parsed.untimed else ""
                        chain.append(_step("lrc", "found", f"{sidecar[0].name}: {len(parsed.lines)} timed lines{extra}"))
                    else:
                        chain.append(_step("lrc", "none", f"{sidecar[0].name} has no timed lines"))

        # (b) embedded tags
        probed: Optional[Dict[str, Any]] = None
        if found is not None:
            chain.append(_step("tags", "not_needed"))
        elif path is None:
            chain.append(_step("tags", "skipped", "the track has no file now"))
        else:
            tools = await self._media_tools()
            if not tools.ffprobe:
                chain.append(_step("tags", "unavailable", "ffprobe was not found, so embedded tags cannot be read"))
            else:
                probed = await asyncio.to_thread(read_tags, path, tools)
                picked = pick_lyrics_tag(probed["tags"]) if probed else None
                if probed is None:
                    chain.append(_step("tags", "error", "ffprobe could not read the file"))
                elif picked is None:
                    chain.append(_step("tags", "none", "no lyrics tag (ID3 SYLT is not read by ffprobe)"))
                else:
                    lines, synced, language = lines_from_text(picked[1])
                    found = make_lyrics(track_hash, "tags", lines, synced=synced, language=language)
                    chain.append(_step("tags", "found",
                                       f"tag {picked[0]}: {len(lines)} {'timed' if synced else 'unsynced'} lines"))

        # (c) LRCLIB
        lrclib: Optional[dict] = None
        instrumental = False
        if found is not None:
            chain.append(_step("lrclib", "not_needed"))
        elif not lrclib_enabled:
            chain.append(_step("lrclib", "disabled", "online lookup is off ([music] lrclib_enabled)"))
        elif self._clock() - self._last_error_at < LRCLIB_ERROR_BACKOFF_S:
            chain.append(_step("lrclib", "skipped",
                               "lrclib.net could not be reached a moment ago; use Find on LRCLIB to try again"))
        else:
            duration = await self._duration(entry, path, probed)
            q = self.build_query(entry, duration)
            if not q.artist or not q.title:
                lrclib = {"result": "no_query", "cached": False, "query": q.as_dict(), "record": None}
                chain.append(_step("lrclib", "skipped", "needs an artist and a title (tags, or a file named \"Artist - Title\")"))
            elif q.duration is None:
                lrclib = {"result": "no_duration", "cached": False, "query": q.as_dict(), "record": None}
                chain.append(_step("lrclib", "skipped", "the track's duration is unknown, so a match cannot be checked"))
            else:
                outcome = await self._lrclib(track_hash, q)
                lrclib = self._lrclib_answer(q, outcome)
                result = outcome["result"]
                if result in FOUND_RESULTS:
                    found = self._lrclib_lyrics(track_hash, outcome)
                    chain.append(_step("lrclib", "found", f"{result} lyrics, record {outcome['record']['id']}"
                                       + (" (from the local cache)" if outcome.get("cached") else "")))
                elif result == "instrumental":
                    instrumental = True
                    chain.append(_step("lrclib", "instrumental", "LRCLIB marks this track instrumental"))
                else:
                    chain.append(_step("lrclib", "none" if result in NEGATIVE_RESULTS else "error",
                                       outcome.get("error") or result.replace("_", " ")))

        # (d) transcribe and (e) manual
        if found is not None:
            chain.append(_step("transcribe", "not_needed"))
        elif instrumental:
            chain.append(_step("transcribe", "not_run", "LRCLIB marks the track instrumental"))
        else:
            chain.append(_step("transcribe", "not_run", "an explicit action, not part of the automatic chain (not built yet)"))
        chain.append(_step("manual", "available", "Edit is always possible"))
        return found, chain, lrclib

    async def lookup_lrclib(self, track_id: str, *, artist: Optional[str] = None, title: Optional[str] = None,
                            album: Optional[str] = None, replace: bool = False) -> Dict[str, Any]:
        """The explicit "Find on LRCLIB". A found result is stored when nothing is
        stored or `replace`; otherwise it comes back as `candidate`."""
        lyrics_file = self.lyrics_path(track_id)
        async with self._lock(track_id):
            entry = self.library.get_entry(track_id)
            stored = await asyncio.to_thread(self._read_stored, lyrics_file)
            answer: Dict[str, Any] = {"lyrics": stored, "applied": False, "candidate": None}
            if not self.lrclib_enabled():
                answer["lrclib"] = {"result": "disabled", "cached": False, "query": None, "record": None}
                return answer
            path = self.library.get_track_path(track_id)
            duration = await self._duration(entry, path, None)
            q = self.build_query(entry, duration, artist=artist, title=title, album=album)
            if not q.artist or not q.title:
                answer["lrclib"] = {"result": "no_query", "cached": False, "query": q.as_dict(), "record": None}
                return answer
            if q.duration is None:
                answer["lrclib"] = {"result": "no_duration", "cached": False, "query": q.as_dict(), "record": None}
                return answer
            outcome = await self._lrclib(entry["source_hash"], q)
            answer["lrclib"] = self._lrclib_answer(q, outcome)
            if outcome["result"] not in FOUND_RESULTS:
                return answer
            new = self._lrclib_lyrics(entry["source_hash"], outcome)
            if stored is None or replace:
                await self._store(track_id, new)
                answer["lyrics"], answer["applied"] = new, True
            else:
                answer["candidate"] = new
            return answer

    async def save_manual(self, track_id: str, lrc_text: str) -> Dict[str, Any]:
        """Store the person's LRC as `source: "manual"`. Refused while a line has no
        stamp (SPEC flow 13), or when there is no timed line at all."""
        lyrics_file = self.lyrics_path(track_id)
        if not isinstance(lrc_text, str):
            raise MusicLibraryError("invalid_lyrics", "lrc_text must be a string")
        if len(lrc_text) > LRC_TEXT_MAX_CHARS:
            raise MusicLibraryError("too_large", f"lyrics longer than {LRC_TEXT_MAX_CHARS} characters")
        parsed = parse_lrc(lrc_text)
        if parsed.untimed:
            n = len(parsed.untimed)
            raise MusicLibraryError(
                "unstamped_lines",
                f"{n} line{'s have' if n != 1 else ' has'} no time stamp; stamp {'them' if n != 1 else 'it'} "
                f"(Stamp current time) or delete {'them' if n != 1 else 'it'} before saving",
            )
        if not parsed.lines:
            raise MusicLibraryError("empty_lyrics", "no timed line to save")
        async with self._lock(track_id):
            entry = self.library.get_entry(track_id)
            stored = await asyncio.to_thread(self._read_stored, lyrics_file)
            edited = stored is not None and (stored.get("source") != "manual" or bool(stored.get("edited")))
            record = make_lyrics(entry["source_hash"], "manual", parsed.lines, language=parsed.language, edited=edited)
            await self._store(track_id, record)
            return {"lyrics": record}
