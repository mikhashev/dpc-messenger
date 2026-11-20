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
3. `lrclib`: **the local LRCLIB cache only**. The automatic chain (`get`, which the
   player runs on every track change) never sends a request: it shows what an earlier
   explicit lookup cached for the same track hash and query, and otherwise reports the
   step as `not_run`. The request itself, `GET https://lrclib.net/api/get` by artist,
   title, album and duration, is sent only by `lookup_lrclib`, the person's "Find on
   LRCLIB" click, and only when `[music] lrclib_enabled` is on (default on; Mike's call
   that LRCLIB is in the chain, 2026-11-19, SPEC §12 decision 13; that it is never sent
   without a click follows the review of the first build, 2026-11-20). The record must
   match the track's duration within ±2 s, compared with the unrounded duration (the
   request carries whole seconds); `syncedLyrics` is preferred, `plainLyrics` gives
   unsynced lines, and `instrumental` stops the chain with no lyrics.
4. `transcribe`: **never run by the chain.** It is the explicit `transcribe` action (the
   person's "Transcribe" click): DPC's existing speech-to-text runs on the track's file
   and its segments become timed lines. The chain reports the step as `not_run` with
   that hint, `running` while a transcription of the track is under way, or `done` when
   the stored lyrics came from it.
5. `manual`: always possible. `save_manual` stores the person's LRC with
   `source: "manual"`.

**Transcribe.** `transcribe` hands the track's file to a `transcriber` (injected by the
command layer, which picks the provider exactly as DPC's voice transcription does; this
module never loads a model). Each `{start, end, text}` segment of the result becomes one
line `[mm:ss.xx] text`; a result with text but no segments (a cloud or peer provider
answers text only) becomes unsynced lines with `synced: false`. The record is stored
with `source: "transcribe"`, the word SPEC §4 and `schemas.LYRICS_SOURCES` already have
for it (the player's badge reads "transcribed"). The answer carries `transcription`:
the provider and model the result names, the language, the segment count and whether
the lines are timed. Sung vocals over music are often misheard and segment times are
Whisper's chunk times, not an alignment to the words: nothing here claims more.
The stored record keeps the provider and model too, as `transcription: {provider,
model}` (Mike, 2026-11-20; each a string or null, the key only on `source:
"transcribe"`, and optional so records written before it still load). Over manual or
edited lyrics the action is refused (`overwrite_manual_required`, before anything
runs) unless the caller passes `overwrite_manual`; if the person saves their own lyrics
while a transcription runs, the result comes back as `candidate` and theirs are kept.

**Storage.** Lyrics live at `<DPC home>/music/lyrics/<track_id>.json` as `mp.lyrics/1`,
written atomically. Nothing is ever written next to the person's audio: the sidecar and
the tags are only read, and a manual edit goes to the DPC home, not to a `.lrc` beside
the file (ADR-051 Q4: tags in watched folders are never rewritten).

**Never overwrite.** Stored lyrics are returned as they are. `refresh` runs the chain
again and replaces what is stored only when it came from the chain (`lrc`, `tags`,
`lrclib`) and was not edited; manual or edited lyrics are kept and the new result is
offered as `candidate`. An explicit LRCLIB lookup stores its result only when nothing is
stored or the caller passes `replace`; otherwise it too answers with a `candidate`. Over
manual or edited lyrics `replace` alone is refused (`overwrite_manual_required`): the
caller must also pass `overwrite_manual`, which the player sends only after the person
confirms.

**LRCLIB.** The call runs in core with httpx (already a core dependency), never in the
webview. Requests are serialised with a small gap, carry an identified User-Agent
(`dpc-messenger-music/<version> (+<project page>)`, the form the LRCLIB docs ask for),
and honour `429 Retry-After`: until it passes no request is sent. Timeouts are short. The
outcome is cached in `<DPC home>/music/lyrics_cache.json` under the track's hash plus the
normalised query (`artist|title|album|duration`): found records for good, `not_found`,
`instrumental`, `mismatch` and `empty` for `LRCLIB_NEGATIVE_TTL_S` (7 days), errors and
rate limits never. The automatic chain shows any cached outcome; an explicit lookup uses
a cached found record but asks again past a cached negative (the person pressed the
button to try again), while a `429 Retry-After` block still holds. **Egress:** only on
the explicit lookup, the query sends the artist, title, album and duration (and this
machine's IP) to lrclib.net; no audio, no path, no hash.
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
import weakref
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

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
LRCLIB_CACHE_MAX_ENTRIES = 5000

SIDECAR_MAX_BYTES = 1024 * 1024
LRC_TEXT_MAX_CHARS = 512 * 1024

# Results of one LRCLIB lookup.
FOUND_RESULTS = ("synced", "plain")
NEGATIVE_RESULTS = ("not_found", "instrumental", "mismatch", "empty")
CACHEABLE_RESULTS = FOUND_RESULTS + NEGATIVE_RESULTS

# The stored source of transcribed lyrics: SPEC §4's word, already in schemas.LYRICS_SOURCES.
TRANSCRIBE_SOURCE = "transcribe"

# What the transcribe action is handed: the track's file in, a result out shaped like
# `LocalWhisperProvider.transcribe`'s: `text`, `language`, `duration`, `provider`,
# optionally `model` and `segments: [{start, end, text}]`. It raises on failure.
Transcriber = Callable[[Path], Awaitable[Dict[str, Any]]]

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
                lrclib: Optional[dict] = None, transcription: Optional[dict] = None) -> dict:
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
    if transcription is not None:
        record["transcription"] = {"provider": transcription.get("provider"), "model": transcription.get("model")}
    schemas.validate_lyrics(record)
    return record


# -- a transcription result → lyrics -------------------------------------------------

def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    v = float(value)
    return v if math.isfinite(v) else None


def lines_from_segments(segments: Any) -> List[dict]:
    """Timed `mp.lyrics/1` lines from `[{start, end, text}]` segments: one line per
    segment with text, whitespace collapsed, sorted by start; `t_end` kept when it is
    after the start. A segment without a usable start is dropped."""
    out: List[Tuple[float, Optional[float], str, int]] = []
    for n, seg in enumerate(segments or []):
        if not isinstance(seg, dict):
            continue
        text = _SPACES.sub(" ", str(seg.get("text") or "")).strip()
        start = _finite(seg.get("start"))
        if not text or start is None:
            continue
        end = _finite(seg.get("end"))
        out.append((max(0.0, round(start, 3)), None if end is None else round(end, 3), text, n))
    out.sort(key=lambda r: (r[0], r[3]))
    lines: List[dict] = []
    for start, end, text, _ in out:
        line: Dict[str, Any] = {"t_start": start, "text": text}
        if end is not None and end > start:
            line["t_end"] = end
        lines.append(line)
    return lines


def transcript_lyrics(track_hash: str, result: Any) -> Tuple[dict, Dict[str, Any]]:
    """`(record, info)` for a transcription result. Segments give timed lines; text
    without segments gives unsynced lines (`synced: false`). No words at all raises
    `MusicLibraryError("no_speech")`. `info` is what the answer reports about the run."""
    if not isinstance(result, dict):
        raise MusicLibraryError("transcription_failed", "the transcription gave no result")
    text = str(result.get("text") or "")
    lines = lines_from_segments(result.get("segments"))
    synced = bool(lines)
    if not synced:
        lines = plain_lines(text)
    if not lines:
        raise MusicLibraryError(
            "no_speech", "the transcription found no words in this track (it may be instrumental)")
    language = result.get("language")
    language = language.strip() if isinstance(language, str) and language.strip() not in ("", "unknown") else None
    provider = str(result["provider"]) if result.get("provider") else None
    model = str(result["model"]) if result.get("model") else None
    record = make_lyrics(track_hash, TRANSCRIBE_SOURCE, lines, synced=synced, language=language,
                         transcription={"provider": provider, "model": model})
    info: Dict[str, Any] = {
        "provider": provider,
        "model": model,
        "language": language,
        "duration_s": _finite(result.get("duration")),
        "segments": len(lines) if synced else 0,
        "synced": synced,
    }
    if result.get("fallback_reason"):
        info["fallback_reason"] = str(result["fallback_reason"])
    return record, info


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
    """`duration` is the whole seconds sent to lrclib.net and kept in the cache key;
    `duration_exact` the track's unrounded duration, which the ±2 s match is checked
    against (rounding first would widen the tolerance to ±2.5 s). It never leaves core."""

    artist: str
    title: str
    album: Optional[str]
    duration: Optional[int]
    duration_exact: Optional[float] = None

    def match_duration(self) -> Optional[float]:
        return self.duration_exact if self.duration_exact is not None else self.duration

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
        expected = q.match_duration()
        if (expected is not None and meta["duration"] is not None
                and abs(meta["duration"] - expected) > LRCLIB_DURATION_TOLERANCE_S):
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
                 transcriber: Optional[Transcriber] = None):
        self.library = library
        music_root = music_dir_of(dpc_home_dir)
        self.root = music_root / LYRICS_DIR
        self.cache = cache or LrclibCache(music_root / LRCLIB_CACHE_FILE)
        self.client = client or LrclibClient()
        self._lrclib_enabled = lrclib_enabled
        self._tools = tools
        self.transcriber = transcriber
        # The last transcribe run per track in this process: {"state": "running" | "done"
        # | "error", ...}. `get` reports it, so a player that stopped waiting (its command
        # timed out) or was reopened sees the run and its error.
        self._transcribe_runs: Dict[str, Dict[str, Any]] = {}
        # One lock per track while someone holds or waits on it: a holder and every
        # waiter keep a strong reference, so an entry vanishes only when nobody needs it.
        self._locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = weakref.WeakValueDictionary()

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
            try:
                os.replace(path, aside)
            except OSError as move_exc:
                # Locked (Windows: another process has it open) or read-only: the file
                # stays where it is and is treated as absent; the next write replaces it.
                logger.error("music: %s is not valid lyrics (%s) and could not be moved aside (%s); "
                             "treating it as absent", path, exc, move_exc)
                return None
            logger.error("music: %s is not valid lyrics (%s); moved aside to %s", path, exc, aside.name)
            return None

    async def load_stored(self, track_id: str) -> Optional[dict]:
        return await asyncio.to_thread(self._read_stored, self.lyrics_path(track_id))

    async def _store(self, track_id: str, record: dict) -> None:
        schemas.validate_lyrics(record)
        await asyncio.to_thread(atomic_write_json, self.lyrics_path(track_id), record)

    def _lock(self, track_id: str) -> asyncio.Lock:
        lock = self._locks.get(track_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[track_id] = lock
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
        exact = None
        if duration_s is not None and 1 <= round(duration_s) <= 3600:
            duration = int(round(duration_s))
            exact = float(duration_s)
        return LrclibQuery(artist=q_artist, title=q_title, album=q_album, duration=duration,
                           duration_exact=exact)

    async def _lrclib_cached(self, track_hash: str, q: LrclibQuery) -> Optional[Dict[str, Any]]:
        """The cached outcome for a query, or None. Never touches the network."""
        await self.cache.ensure_loaded()
        hit = self.cache.get(LrclibCache.make_key(track_hash, q))
        if hit is None:
            return None
        hit.pop("fetched_at", None)
        hit.pop("expires_at", None)
        return {**hit, "cached": True}

    async def _lrclib_fetch(self, track_hash: str, q: LrclibQuery) -> Dict[str, Any]:
        """The explicit lookup's outcome: a cached found record, else lrclib.net (then
        cached). A cached negative is passed over, since the person asked again; the
        client's `Retry-After` block still holds."""
        hit = await self._lrclib_cached(track_hash, q)
        if hit is not None and hit.get("result") in FOUND_RESULTS:
            return hit
        outcome = await self.client.get(q)
        await self.cache.put(LrclibCache.make_key(track_hash, q), outcome)
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
        chain's result, stored. See the module docstring for the never-overwrite rule.
        Runs with no click (every track change), so it never sends a request: its LRCLIB
        step reads the local cache only."""
        lyrics_file = self.lyrics_path(track_id)
        async with self._lock(track_id):
            entry = self.library.get_entry(track_id)
            stored = await asyncio.to_thread(self._read_stored, lyrics_file)
            enabled = self.lrclib_enabled()
            run = self.transcribe_run(track_id)
            if stored is not None and not refresh:
                return {"lyrics": stored, "from_store": True, "chain": [], "lrclib": None,
                        "lrclib_enabled": enabled, "transcribe": run}
            found, chain, lrclib = await self._run_chain(entry, enabled, stored=stored, run=run)
            answer: Dict[str, Any] = {"lyrics": stored, "from_store": False, "chain": chain,
                                      "lrclib": lrclib, "lrclib_enabled": enabled, "transcribe": run}
            if found is None:
                return answer
            if stored is not None and (stored.get("source") == "manual" or stored.get("edited")):
                answer["candidate"] = found
                answer["kept"] = "the stored lyrics are the person's edit, so they are kept"
                return answer
            await self._store(track_id, found)
            answer["lyrics"] = found
            return answer

    async def _run_chain(self, entry: dict, lrclib_enabled: bool, *, stored: Optional[dict] = None,
                         run: Optional[Dict[str, Any]] = None) -> Tuple[Optional[dict], List[dict], Optional[dict]]:
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

        # (c) LRCLIB: the local cache only. This chain runs on every track change with
        # no click, so it never sends anything; "Find on LRCLIB" (`lookup_lrclib`) does.
        lrclib: Optional[dict] = None
        instrumental = False
        if found is not None:
            chain.append(_step("lrclib", "not_needed"))
        elif not lrclib_enabled:
            chain.append(_step("lrclib", "disabled", "online lookup is off ([music] lrclib_enabled)"))
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
                outcome = await self._lrclib_cached(track_hash, q)
                if outcome is None:
                    chain.append(_step("lrclib", "not_run",
                                       "nothing cached; Find on LRCLIB asks lrclib.net (an explicit action)"))
                else:
                    lrclib = self._lrclib_answer(q, outcome)
                    result = outcome["result"]
                    if result in FOUND_RESULTS:
                        found = self._lrclib_lyrics(track_hash, outcome)
                        chain.append(_step("lrclib", "found",
                                           f"{result} lyrics, record {outcome['record']['id']} (from the local cache)"))
                    elif result == "instrumental":
                        instrumental = True
                        chain.append(_step("lrclib", "instrumental",
                                           "LRCLIB marks this track instrumental (from the local cache)"))
                    else:
                        chain.append(_step("lrclib", "none",
                                           f"{result.replace('_', ' ')} (from the local cache)"))

        # (d) transcribe: never run here, only reported. The run is the explicit
        # `transcribe` (the Transcribe click). (e) manual.
        transcribed = stored is not None and stored.get("source") == TRANSCRIBE_SOURCE
        if run is not None and run.get("state") == "running":
            chain.append(_step("transcribe", "running", "a transcription of this track is under way"))
        elif transcribed and (found is None or stored.get("edited")):
            chain.append(_step("transcribe", "done", "the stored lyrics were transcribed (machine transcription, may be wrong)"))
        elif found is not None:
            chain.append(_step("transcribe", "not_needed"))
        elif instrumental:
            chain.append(_step("transcribe", "not_run",
                               "LRCLIB marks the track instrumental; Transcribe can still be run (an explicit action)"))
        else:
            chain.append(_step("transcribe", "not_run",
                               "Transcribe runs DPC's speech-to-text on this track (an explicit action, "
                               "never part of the automatic chain)"))
        chain.append(_step("manual", "available", "Edit is always possible"))
        return found, chain, lrclib

    async def lookup_lrclib(self, track_id: str, *, artist: Optional[str] = None, title: Optional[str] = None,
                            album: Optional[str] = None, replace: bool = False,
                            overwrite_manual: bool = False) -> Dict[str, Any]:
        """The explicit "Find on LRCLIB", the only path that sends a request. A found
        result is stored when nothing is stored or `replace`; otherwise it comes back as
        `candidate`. Over manual or edited lyrics `replace` is refused with
        `overwrite_manual_required` (before anything is sent) unless `overwrite_manual`
        is passed too: the person's own lyrics are never replaced by one call."""
        lyrics_file = self.lyrics_path(track_id)
        async with self._lock(track_id):
            entry = self.library.get_entry(track_id)
            stored = await asyncio.to_thread(self._read_stored, lyrics_file)
            if (replace and not overwrite_manual and stored is not None
                    and (stored.get("source") == "manual" or stored.get("edited"))):
                raise MusicLibraryError(
                    "overwrite_manual_required",
                    "the stored lyrics are your own edit; replacing them with LRCLIB lyrics needs "
                    "an explicit confirmation (overwrite_manual)",
                )
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
            outcome = await self._lrclib_fetch(entry["source_hash"], q)
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

    def transcribe_run(self, track_id: str) -> Optional[Dict[str, Any]]:
        """The last transcribe run of the track in this process, or None."""
        run = self._transcribe_runs.get(track_id)
        return copy.deepcopy(run) if run is not None else None

    @staticmethod
    def _own_edit(stored: Optional[dict]) -> bool:
        return stored is not None and (stored.get("source") == "manual" or bool(stored.get("edited")))

    async def transcribe(self, track_id: str, *, overwrite_manual: bool = False) -> Dict[str, Any]:
        """The explicit "Transcribe": the track's file through the `transcriber`, its
        segments as timed lines, stored as `source: "transcribe"`. Never called by `get`.

        Over manual or edited lyrics it is refused with `overwrite_manual_required`
        before anything runs, unless `overwrite_manual`. One run per track at a time
        (`transcribe_running`). The track's lock is not held while the transcription
        runs, so the player can read the track's lyrics meanwhile; it is taken again to
        store, and if the person saved their own lyrics in between (and did not pass
        `overwrite_manual`), the result is returned as `candidate` and theirs are kept."""
        lyrics_file = self.lyrics_path(track_id)
        async with self._lock(track_id):
            entry = self.library.get_entry(track_id)
            stored = await asyncio.to_thread(self._read_stored, lyrics_file)
            if not overwrite_manual and self._own_edit(stored):
                raise MusicLibraryError(
                    "overwrite_manual_required",
                    "the stored lyrics are your own edit; replacing them with a transcription needs "
                    "an explicit confirmation (overwrite_manual)",
                )
            if self.transcriber is None:
                raise MusicLibraryError("transcription_unavailable",
                                        "transcription is not available in this build of the core")
            path = self.library.get_track_path(track_id)
            if path is None:
                raise MusicLibraryError(
                    "no_file", "the track has no local file now (missing, or a web track); "
                               "Transcribe needs the audio on this machine")
            current = self._transcribe_runs.get(track_id)
            if current is not None and current.get("state") == "running":
                raise MusicLibraryError("transcribe_running", "this track is already being transcribed")
            self._transcribe_runs[track_id] = {"state": "running", "started_at": _now_iso()}

        try:
            try:
                result = await self.transcriber(Path(path))
            except MusicLibraryError:
                raise
            except Exception as exc:
                logger.warning("music: transcribing %s failed: %s", track_id, exc)
                raise MusicLibraryError("transcription_failed", f"transcription failed: {exc}") from exc
            record, info = transcript_lyrics(entry["source_hash"], result)
            logger.info("music: transcribed %s with %s (%s): %d %s lines", track_id,
                        info.get("provider") or "an unnamed provider", info.get("model") or "model not named",
                        len(record["lines"]), "timed" if info["synced"] else "unsynced")

            async with self._lock(track_id):
                now_entry = self.library.get_entry(track_id)
                if now_entry.get("source_hash") != entry["source_hash"]:
                    raise MusicLibraryError(
                        "track_changed", "the track's file changed while it was transcribed; transcribe it again")
                stored = await asyncio.to_thread(self._read_stored, lyrics_file)
                answer: Dict[str, Any] = {"lyrics": stored, "applied": False, "candidate": None,
                                          "transcription": info, "lrc": format_lrc(record)}
                if not overwrite_manual and self._own_edit(stored):
                    answer["candidate"] = record
                    answer["kept"] = "you saved your own lyrics while the transcription ran, so they are kept"
                else:
                    await self._store(track_id, record)
                    answer["lyrics"], answer["applied"] = record, True
        except MusicLibraryError as exc:
            self._transcribe_runs[track_id] = {"state": "error", "code": exc.code, "message": exc.message,
                                               "finished_at": _now_iso()}
            raise
        except BaseException as exc:
            # Includes a cancel: the run is over either way, so it never stays "running".
            self._transcribe_runs[track_id] = {"state": "error", "code": "internal",
                                               "message": str(exc) or type(exc).__name__,
                                               "finished_at": _now_iso()}
            raise
        self._transcribe_runs[track_id] = {"state": "done", "applied": answer["applied"],
                                           "transcription": copy.deepcopy(info), "finished_at": _now_iso()}
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
