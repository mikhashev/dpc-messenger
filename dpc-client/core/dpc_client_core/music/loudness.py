"""Integrated loudness of a take, measured with ffmpeg's `ebur128` filter (SPEC §8, §12
decision 37; slice 1 step 4).

The review's plain A/B plays the takes at matched loudness (decision 37: "a plain A/B
with level matching"; `docs/EVOLUTION.md` §9, display gain only). The gain is computed in
the player from each take's integrated loudness, so the runner measures every take once,
after its FLAC is written, and `measure_music_take_loudness` measures a take that has no
measurement yet. The file is never changed: the gain lives in the listening path only.

**The record** is `mp.take/1`'s extension field `loudness`::

    {"lufs_i": -9.8, "lra": 4.1, "true_peak_dbtp": -0.9,
     "measured_by": "ffmpeg ebur128", "measured_at": "2026-11-21T20:00:00Z"}

It is not `qa`: `mp.qa/1` is `additionalProperties: false` with every field required
(seams, clipping, lyrics WER...), so a loudness-only record would not be a valid `mp.qa/1`.
When stage-1 QA is built, `qa.integrated_lufs` and `qa.true_peak_dbtp` take these numbers.
A value ebur128 cannot give (a silent file's `-inf` peak) is null. ffmpeg floors the
integrated loudness at -70 LUFS; a file shorter than one 400 ms gating block reads -70
too, and the player does not level-match against it.

**The command line** is `ffmpeg -nostdin -hide_banner -nostats -i <file> -map 0:a:0
-af ebur128=peak=true:framelog=verbose -f null -`. The summary is logged at `info`, so the
log level stays at its default; `framelog=verbose` keeps the per-100 ms lines out of
stderr (an ffmpeg without the option is asked again without it). Only the text after the
last `Summary:` is read.

These numbers are advisory (decision 29): they never gate, accept or reject a take.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Optional

from .identity import MediaTools

MEASURED_BY = "ffmpeg ebur128"
LOUDNESS_KEYS = ("lufs_i", "lra", "true_peak_dbtp", "measured_by", "measured_at")
LUFS_FLOOR = -70.0          # ffmpeg's ebur128 never reports below this
MEASURE_TIMEOUT_S = 300.0

LoudnessMeter = Callable[[Path], Dict[str, object]]


class LoudnessError(Exception):
    """The measurement could not be made; the message says why."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


_NUM = r"(-?(?:\d+(?:\.\d+)?|inf)|nan)"
_I_RE = re.compile(r"\bI:\s*" + _NUM + r"\s*LUFS", re.IGNORECASE)
_LRA_RE = re.compile(r"\bLRA:\s*" + _NUM + r"\s*LU\b", re.IGNORECASE)
_PEAK_RE = re.compile(r"\bPeak:\s*" + _NUM + r"\s*dB(?:FS|TP)", re.IGNORECASE)


def _number(text: Optional[str]) -> Optional[float]:
    if text is None:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return round(value, 2) if math.isfinite(value) else None


def parse_ebur128_summary(stderr: str) -> Dict[str, Optional[float]]:
    """`{lufs_i, lra, true_peak_dbtp}` from ffmpeg's ebur128 summary (the text after the
    last `Summary:`). Raises `LoudnessError` when there is no summary or no integrated
    loudness in it."""
    at = stderr.rfind("Summary:")
    if at < 0:
        raise LoudnessError("ffmpeg printed no ebur128 summary")
    summary = stderr[at:]
    m_i = _I_RE.search(summary)
    lufs = _number(m_i.group(1)) if m_i else None
    if lufs is None:
        raise LoudnessError("the ebur128 summary has no integrated loudness")
    m_lra = _LRA_RE.search(summary)
    m_peak = _PEAK_RE.search(summary)
    return {
        "lufs_i": lufs,
        "lra": _number(m_lra.group(1)) if m_lra else None,
        "true_peak_dbtp": _number(m_peak.group(1)) if m_peak else None,
    }


def loudness_record(values: Dict[str, Optional[float]], *, measured_at: Optional[str] = None) -> Dict[str, object]:
    """The take's `loudness` record from parsed values."""
    return {
        "lufs_i": values.get("lufs_i"),
        "lra": values.get("lra"),
        "true_peak_dbtp": values.get("true_peak_dbtp"),
        "measured_by": MEASURED_BY,
        "measured_at": measured_at or _now(),
    }


def _command(ffmpeg: str, path: Path, framelog: bool) -> list:
    filt = "ebur128=peak=true:framelog=verbose" if framelog else "ebur128=peak=true"
    return [ffmpeg, "-nostdin", "-hide_banner", "-nostats", "-i", str(path), "-map", "0:a:0",
            "-af", filt, "-f", "null", "-"]


def measure_loudness(path: Path, tools: Optional[MediaTools] = None) -> Dict[str, object]:
    """Measure one audio file. Blocking: call it through `asyncio.to_thread`. Raises
    `LoudnessError` (no ffmpeg, no file, ffmpeg failed, no summary)."""
    path = Path(path)
    if not path.is_file():
        raise LoudnessError(f"the file is not there: {path.name}")
    found = tools or MediaTools.discover()
    if not found.ffmpeg:
        raise LoudnessError("ffmpeg is not on PATH; the loudness is measured with ffmpeg ebur128")
    result = found.runner(_command(found.ffmpeg, path, True), timeout=MEASURE_TIMEOUT_S)
    err = (result.stderr or b"").decode("utf-8", errors="replace")
    if result.returncode != 0 and "framelog" in err:
        # An ffmpeg older than the framelog option: ask again without it.
        result = found.runner(_command(found.ffmpeg, path, False), timeout=MEASURE_TIMEOUT_S)
        err = (result.stderr or b"").decode("utf-8", errors="replace")
    if result.returncode != 0:
        raise LoudnessError(f"ffmpeg could not measure {path.name} (rc {result.returncode}): {err.strip()[-300:]}")
    return loudness_record(parse_ebur128_summary(err))


def ffmpeg_loudness_meter(tools: Optional[MediaTools] = None) -> LoudnessMeter:
    """A meter for the runner: `meter(path) -> loudness record`, raising `LoudnessError`."""
    def meter(path: Path) -> Dict[str, object]:
        return measure_loudness(path, tools)
    return meter
