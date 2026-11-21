"""Waveform peaks of a library track, for the player's waveform view.

The webview never decodes audio (WebKitGTK cannot be relied on for it): ffmpeg decodes the
track to mono, low-rate 16-bit PCM through a pipe and this module reduces it to `n`
buckets of `[min, max]`, each in -1..1.

**The command line** is `ffmpeg -nostdin -hide_banner -loglevel error -i <file> -map 0:a:0
-vn -ac 1 -ar 8000 -f s16le -`. 8 kHz is far above what 2000 buckets of a few minutes need;
a 10 minute track is 9.6 MB of PCM in memory for the length of the run.

**The cache** is `<DPC home>/music/peaks/<content_hash>.json`, keyed by the library entry's
content hash so it survives a rename or a relink::

    {"version": 1, "n": 2000, "duration_s": 183.4, "sample_rate_used": 8000,
     "peaks": [[-0.31, 0.29], ...]}

It is written atomically (temp file, then `os.replace`) and read back only when its
version and bucket count match; anything else is recomputed and replaced. An entry whose
hash is not known yet is computed but not cached.

Work is bounded: one ffmpeg at a time (a process-wide lock) with a timeout.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
from array import array
from pathlib import Path
from typing import Any, Dict, List, Optional

from .identity import MediaTools
from .store import music_dir_of

PEAKS_VERSION = 1
PEAKS_DIR_NAME = "peaks"
SAMPLE_RATE = 8000
DEFAULT_BUCKETS = 2000
MIN_BUCKETS = 50
MAX_BUCKETS = 8000
PEAKS_TIMEOUT_S = 120.0

_lock = threading.Lock()
_SAFE_HASH = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class PeaksError(Exception):
    """The peaks could not be made; `code` is `no_file`, `no_ffmpeg`, `decode_failed`,
    `timeout` or `invalid_params`."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def clamp_buckets(n: Any) -> int:
    if n is None:
        return DEFAULT_BUCKETS
    if isinstance(n, bool) or not isinstance(n, int):
        raise PeaksError("invalid_params", "n must be an integer")
    return max(MIN_BUCKETS, min(MAX_BUCKETS, n))


def reduce_pcm(samples: "array", n: int) -> List[List[float]]:
    """`n` buckets of `[min, max]` (each -1..1, 3 decimals) from signed 16-bit samples.
    Buckets split the samples evenly; fewer samples than buckets give fewer buckets."""
    total = len(samples)
    if total == 0:
        return []
    n = min(n, total)
    out: List[List[float]] = []
    for i in range(n):
        lo, hi = i * total // n, (i + 1) * total // n
        chunk = samples[lo:hi]
        out.append([round(min(chunk) / 32768.0, 3), round(max(chunk) / 32767.0, 3)])
    return out


def _decode(path: Path, tools: MediaTools) -> "array":
    cmd = [tools.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path),
           "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]
    try:
        result = tools.runner(cmd, timeout=PEAKS_TIMEOUT_S)
    except Exception as exc:
        if exc.__class__.__name__ == "TimeoutExpired":
            raise PeaksError("timeout", f"decoding {path.name} took longer than {int(PEAKS_TIMEOUT_S)} s")
        raise PeaksError("decode_failed", f"ffmpeg could not be run: {exc}")
    if result.returncode != 0:
        err = (result.stderr or b"").decode("utf-8", errors="replace").strip()[-300:]
        raise PeaksError("decode_failed", f"ffmpeg could not decode {path.name} (rc {result.returncode}): {err}")
    data = result.stdout or b""
    data = data[: len(data) - (len(data) % 2)]
    samples = array("h")
    samples.frombytes(data)
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def cache_path(dpc_home_dir: Path, content_hash: str) -> Path:
    """`<hash>.json`; the hash's `kind:hex` colon becomes `-` (a colon is not a Windows file name)."""
    return music_dir_of(dpc_home_dir) / PEAKS_DIR_NAME / f"{content_hash.replace(':', '-')}.json"


def _read_cache(path: Path, n: int) -> Optional[Dict[str, Any]]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("version") != PEAKS_VERSION or doc.get("n") != n:
        return None
    if not isinstance(doc.get("peaks"), list) or not isinstance(doc.get("duration_s"), (int, float)):
        return None
    return doc


def _write_cache(path: Path, doc: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.stem + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, separators=(",", ":"))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get_peaks(path: Optional[Path], content_hash: Optional[str], dpc_home_dir: Path, n: Any = None,
              tools: Optional[MediaTools] = None) -> Dict[str, Any]:
    """`{peaks, duration_s, n, sample_rate_used, cached}`. Blocking: call it through
    `asyncio.to_thread`. Raises `PeaksError`."""
    n = clamp_buckets(n)
    cache = cache_path(dpc_home_dir, content_hash) if content_hash and _SAFE_HASH.match(content_hash.replace(":", "-")) else None
    if cache is not None:
        doc = _read_cache(cache, n)
        if doc is not None:
            return {**doc, "cached": True}
    if path is None or not Path(path).is_file():
        raise PeaksError("no_file", "the track's file is not there")
    found = tools or MediaTools.discover()
    if not found.ffmpeg:
        raise PeaksError("no_ffmpeg", "ffmpeg is not on PATH; the waveform is decoded with ffmpeg")
    with _lock:
        if cache is not None:  # another request may have made it while this one waited
            doc = _read_cache(cache, n)
            if doc is not None:
                return {**doc, "cached": True}
        samples = _decode(Path(path), found)
        if not len(samples):
            raise PeaksError("decode_failed", "the track has no audio to draw")
        doc = {
            "version": PEAKS_VERSION,
            "n": n,
            "duration_s": round(len(samples) / SAMPLE_RATE, 3),
            "sample_rate_used": SAMPLE_RATE,
            "peaks": reduce_pcm(samples, n),
        }
        if cache is not None:
            try:
                _write_cache(cache, doc)
            except OSError:
                pass  # the answer is still good; it is computed again next time
    return {**doc, "cached": False}
