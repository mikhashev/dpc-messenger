"""Stage-1 QA of a take: the deterministic gates of SPEC §8 "What gates a take", measured with
ffmpeg (ADR-051 decision 29; backlog card A-MUSIC-TAKE-HAS-NO-STAGE-1-QA-RECORD-...).

The runner measures every take it makes once, on the staged FLAC, and stores two things on it:

- `qa`, a valid `mp.qa/1` (`job_schemas.check_qa`) when every required field has a real number
  behind it (`integrated_lufs`, `true_peak_dbtp`, `clipped_samples`); the edit-only fields
  (`loudness_delta_lu`, the seam fields) are null for a new track, `lyrics_wer` is null (no
  judge). Otherwise `qa` is null: no number is made up to fill the schema.
- `qa_report`, the gates beside it, because `mp.qa/1` has no room for a verdict (see
  `job_schemas`). Four gates, each `pass`, `fail` or `not_measured`, and one verdict:
  `failed` if any gate failed, `passed` only if all four passed, else `not_measured`. A
  measurement that could not be made is `not_measured`, never a pass.

**One ffmpeg pass.** `-af [ebur128=peak=true:framelog=verbose,]astats=measure_perchannel=none:
measure_overall=Peak_level+Peak_count,silencedetect=n=-60dB:d=0.5`. ebur128 is in the chain
only when the caller has no loudness record yet (the runner already measured it for the A/B
level match, `loudness.py`, and hands it over: integrated loudness and true peak are then not
measured twice). The audio is not changed. The file's length comes from the input banner of the
same run (or from the caller, who often knows it exactly from the FLAC header). Blocking:
call it through `asyncio.to_thread`. Same runner, ffmpeg discovery and timeout as
`loudness.py`.

**The gates and why these numbers** (thresholds are starting guesses to tune on real takes,
SPEC §8; each is in the report's `thresholds` so a reader sees the number that judged):

- `length`. Engines differ. acestep.cpp is asked for an exact length (codes x 0.2 s), so the take
  must be within `max(2 s, 5 %)` of the request (`exact`). YuE2 ends the song itself at its end
  token and `duration_s` is only a cap (`job_schemas`), so a short take is normal: it fails only
  when it is longer than the cap by more than `LENGTH_EXACT_MIN_TOLERANCE_S` or shorter than
  `min(LENGTH_CAP_MIN_S, LENGTH_CAP_MIN_FRACTION x cap)` (10 s, nothing usable; half the cap
  when the cap is under 20 s, so a short request is not failed by every take, follow-up 14,
  Zcode #176) (`cap`). "Well outside": the existing 0.5 s
  engine-window check (`runner.length_check_of`) stays advisory; this gate is a bug detector
  for a take that is plainly the wrong length. SPEC §8 lets this gate reject a take; stage 1
  only flags it: no take is rejected or dropped by QA (the person's Keep / Discard stays final).
- `clipping`. Samples at or above 0.999 of full scale (-0.0087 dBFS) (SPEC §8). ffmpeg's `astats`
  gives the file's peak level and the number of samples that reach exactly that peak; when the
  peak is at or above 0.999 that count is `clipped_samples`, a lower bound (a sample that is
  0.9995 beside a peak of 1.0 is not counted). Any clipped sample fails.
- `true_peak`. ebur128's true peak above -1.0 dBTP fails (SPEC §8, the export ceiling). A file
  with no signal has no true peak: `not_measured`, and the silence gate fails it anyway.
- `silence`. Silence is every channel below -60 dBFS for at least 0.5 s (`silencedetect`).
  Leading silence longer than 3 s fails; a silent stretch of 1 s or more strictly inside the
  take (a dropout) fails; a file that is silent end to end fails. **A quiet tail of 1-5 s is
  normal** (decision 27, Mike 2026-11-20: "the tail can really be quiet for 1-5 seconds"), so
  trailing silence fails only when it is longer than 5 s. A cut-off last phrase is not
  measurable here: the person listens to the ending.

**Advisory, never gates** (decision 29): seam metrics (`seams`: a new take has no seam; a region-edit
take brings its seam numbers from `region_edit.seams_entry`, which fill `mp.qa/1`'s seam fields and the
advisory entry) and lyrics WER (no judge).

**Edit takes** (`edit=`): the length mode is `same` (the take must be the control's length within
`LENGTH_SAME_TOLERANCE_S`, decision 23; `length_mismatch` otherwise), the silence gate judges the
runs outside the edit zone as above, a removal's zone is expected to be silent, and a repaint's zone
with `INTERNAL_SILENCE_MAX_S` or more of silence fails as a hole (silence instead of the edit, SPEC §8
gate 5). QC ranking is off by default and
stage 2 does not exist (no judge): nothing here ranks takes.

**Against the source** (`compare_with_source`, follow-up 21): an edit take copies its control bit for bit
outside the zone, so its clipping and true-peak gates fail only for overs the take adds over the
control's (measured once per job, `measure_source_peaks`); inherited overs pass with
`inherited_from_source`. A cover keeps its own verdict and carries the source's figures beside it.

The report holds numbers and words only: no path, nothing of the request.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from . import job_schemas as js
from .identity import MediaTools
from .loudness import MEASURE_TIMEOUT_S, LoudnessError, parse_ebur128_summary

# -- thresholds (all echoed in the report) ----------------------------------------------

SILENCE_NOISE_DB = -60.0            # below this on every channel is silence
SILENCE_MIN_S = 0.5                 # silencedetect reports runs of at least this
LEADING_SILENCE_MAX_S = 3.0         # leading silence longer than this fails
INTERNAL_SILENCE_MAX_S = 1.0        # a silent run strictly inside the take of this or more fails
TAIL_SILENCE_MAX_S = 5.0            # decision 27: a quiet tail up to about 5 s is normal
EDGE_EPS_S = 0.05                   # a run this close to an edge touches the edge
CLIP_LEVEL = 0.999                  # SPEC §8: samples at abs >= 0.999
CLIP_LEVEL_DB = 20.0 * math.log10(CLIP_LEVEL)
TRUE_PEAK_MAX_DBTP = -1.0           # SPEC §8
LENGTH_EXACT_MIN_TOLERANCE_S = 2.0
LENGTH_EXACT_TOLERANCE_FRACTION = 0.05
LENGTH_CAP_MIN_S = 10.0
LENGTH_CAP_MIN_FRACTION = 0.5       # the floor is never above half the cap (Zcode #176)
LENGTH_SAME_TOLERANCE_S = 0.001     # an edit take keeps the control's length (decision 23): a frame or so
SOURCE_PEAK_TOLERANCE_DB = 0.1      # an edit take may sit this much over its source's peaks before it "adds" overs
EDIT, COVER = "edit", "cover"       # the two takes that have a source to compare with (`compare_with_source`)
LENGTH_EXACT, LENGTH_CAP, LENGTH_SAME = "exact", "cap", "same"
LENGTH_MODES = (LENGTH_EXACT, LENGTH_CAP, LENGTH_SAME)
MAX_RUNS_KEPT = 8                   # silent runs listed in the report

THRESHOLDS: Dict[str, Any] = {
    "silence_noise_db": SILENCE_NOISE_DB,
    "silence_min_s": SILENCE_MIN_S,
    "leading_silence_max_s": LEADING_SILENCE_MAX_S,
    "internal_silence_max_s": INTERNAL_SILENCE_MAX_S,
    "tail_silence_max_s": TAIL_SILENCE_MAX_S,
    "clip_level": CLIP_LEVEL,
    "true_peak_max_dbtp": TRUE_PEAK_MAX_DBTP,
    "length_exact_tolerance": f"max({LENGTH_EXACT_MIN_TOLERANCE_S:g} s, {LENGTH_EXACT_TOLERANCE_FRACTION:.0%})",
    "length_cap_min_s": LENGTH_CAP_MIN_S,
    "length_cap_min": f"min({LENGTH_CAP_MIN_S:g} s, {LENGTH_CAP_MIN_FRACTION:.0%} of the cap)",
    "length_same_tolerance_s": LENGTH_SAME_TOLERANCE_S,
    "source_peak_tolerance_db": SOURCE_PEAK_TOLERANCE_DB,
}

TOOLS_NAME = "ffmpeg ebur128+astats+silencedetect"

QaMeter = Callable[..., Dict[str, Any]]


class QaError(Exception):
    """The measurement could not be made; the message says why."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# -- parsing ffmpeg's log ---------------------------------------------------------------

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
_PEAK_DB_RE = re.compile(r"Peak level dB:\s*(-?(?:\d+(?:\.\d+)?|inf)|nan)", re.IGNORECASE)
_PEAK_COUNT_RE = re.compile(r"Peak count:\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")


def parse_duration(stderr: str) -> Optional[float]:
    """The input's length in seconds from ffmpeg's banner (`Duration: 00:00:04.00`), or None."""
    m = _DURATION_RE.search(stderr)
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def parse_astats(stderr: str) -> Optional[Dict[str, Optional[float]]]:
    """`{peak_db, peak_count}` of the Overall block of ffmpeg's astats log; `peak_db` is None
    for a file with no signal (`-inf`). None when there is no Overall block or no peak level."""
    at = stderr.rfind("] Overall")
    if at < 0:
        return None
    block = stderr[at:]
    m_db = _PEAK_DB_RE.search(block)
    if not m_db:
        return None
    raw = m_db.group(1).lower()
    try:
        value = float(raw)
    except ValueError:
        return None
    m_count = _PEAK_COUNT_RE.search(block)
    return {"peak_db": value if math.isfinite(value) else None,
            "peak_count": int(float(m_count.group(1))) if m_count else None}


def parse_silences(stderr: str, duration_s: Optional[float]) -> List[Tuple[float, float]]:
    """The silent runs `[(start, end)]` in seconds that `silencedetect` logged. A run that
    started and never ended (the file's end came first; older ffmpeg builds log no end) ends at
    the file's length when it is known and is dropped when it is not."""
    runs: List[Tuple[float, float]] = []
    open_at: Optional[float] = None
    for line in stderr.splitlines():
        s = _SILENCE_START_RE.search(line)
        if s:
            open_at = max(0.0, float(s.group(1)))
            continue
        e = _SILENCE_END_RE.search(line)
        if e and open_at is not None:
            runs.append((open_at, float(e.group(1))))
            open_at = None
    if open_at is not None and duration_s is not None and duration_s > open_at:
        runs.append((open_at, duration_s))
    return runs


def classify_silences(runs: List[Tuple[float, float]], duration_s: float) -> Dict[str, Any]:
    """`{whole, leading_s, tail_s, longest_internal_s, runs}` from the silent runs."""
    leading = tail = longest = 0.0
    whole = False
    for start, end in runs:
        length = max(0.0, end - start)
        at_start = start <= EDGE_EPS_S
        at_end = end >= duration_s - EDGE_EPS_S
        if at_start and at_end:
            whole = True
        elif at_start:
            leading = max(leading, length)
        elif at_end:
            tail = max(tail, length)
        else:
            longest = max(longest, length)
    return {"whole": whole, "leading_s": round(leading, 2), "tail_s": round(tail, 2),
            "longest_internal_s": round(longest, 2),
            "runs": [[round(a, 2), round(b, 2)] for a, b in runs[:MAX_RUNS_KEPT]]}


# -- the one ffmpeg pass ------------------------------------------------------------------

def _chain(with_loudness: bool, plain: bool) -> str:
    parts: List[str] = []
    if with_loudness:
        parts.append("ebur128=peak=true" if plain else "ebur128=peak=true:framelog=verbose")
    parts.append("astats" if plain else "astats=measure_perchannel=none:measure_overall=Peak_level+Peak_count")
    parts.append(f"silencedetect=n={SILENCE_NOISE_DB:g}dB:d={SILENCE_MIN_S:g}")
    return ",".join(parts)


def _command(ffmpeg: str, path: Path, with_loudness: bool, plain: bool) -> list:
    return [ffmpeg, "-nostdin", "-hide_banner", "-nostats", "-i", str(path), "-map", "0:a:0",
            "-af", _chain(with_loudness, plain), "-f", "null", "-"]


def run_ffmpeg_pass(path: Path, tools: Optional[MediaTools], with_loudness: bool) -> str:
    """ffmpeg's log of the one measuring pass. Raises `QaError` (no file, no ffmpeg, ffmpeg
    failed). An ffmpeg that does not know an option is asked again with the plain filters."""
    path = Path(path)
    if not path.is_file():
        raise QaError(f"the file is not there: {path.name}")
    found = tools or MediaTools.discover()
    if not found.ffmpeg:
        raise QaError("ffmpeg is not on PATH; stage-1 QA is measured with ffmpeg")
    result = found.runner(_command(found.ffmpeg, path, with_loudness, False), timeout=MEASURE_TIMEOUT_S)
    err = (result.stderr or b"").decode("utf-8", errors="replace")
    if result.returncode != 0 and ("framelog" in err or "measure_" in err or "Option" in err):
        result = found.runner(_command(found.ffmpeg, path, with_loudness, True), timeout=MEASURE_TIMEOUT_S)
        err = (result.stderr or b"").decode("utf-8", errors="replace")
    if result.returncode != 0:
        raise QaError(f"ffmpeg could not measure {path.name} (rc {result.returncode}): {err.strip()[-300:]}")
    return err


# -- the gates ----------------------------------------------------------------------------

def _gate(status: str, detail: str, **values: Any) -> Dict[str, Any]:
    return {"status": status, "detail": detail, **values}


def cap_floor_s(requested_s: float) -> float:
    """The shortest take a `cap` request accepts: `LENGTH_CAP_MIN_S`, or half the cap when that
    is less (a 6 s request with a fixed 10 s floor failed every take)."""
    return min(LENGTH_CAP_MIN_S, LENGTH_CAP_MIN_FRACTION * requested_s)


def length_gate(requested_s: Optional[float], received_s: Optional[float], mode: str, *,
                same_tolerance_s: Optional[float] = None) -> Dict[str, Any]:
    """Received length against the request. `exact`: within `max(2 s, 5 %)` of it. `cap`: not
    over it by more than the minimum tolerance and not under `cap_floor_s(requested_s)`. `same`:
    an edit take within `LENGTH_SAME_TOLERANCE_S` of the control; a cover passes its engine's
    `same_tolerance_s` against its source's length (music/similar.py `COVER_SAME_TOLERANCE_S`)."""
    if received_s is None:
        return _gate(js.GATE_NOT_MEASURED, "the length of the file could not be read", mode=mode)
    if requested_s is None or requested_s <= 0:
        return _gate(js.GATE_NOT_MEASURED, "the request has no length to compare with", mode=mode,
                     received_s=round(received_s, 2))
    tol = max(LENGTH_EXACT_MIN_TOLERANCE_S, LENGTH_EXACT_TOLERANCE_FRACTION * requested_s)
    delta = round(received_s - requested_s, 2)
    common = {"mode": mode, "requested_s": round(requested_s, 2), "received_s": round(received_s, 2),
              "delta_s": delta}
    if mode == LENGTH_SAME:
        tol_same = LENGTH_SAME_TOLERANCE_S if same_tolerance_s is None else float(same_tolerance_s)
        ok = abs(received_s - requested_s) <= tol_same
        words = (f"{received_s:.3f} s against the control's {requested_s:.3f} s "
                 f"(an edit keeps the length, decision 23)" if same_tolerance_s is None else
                 f"{received_s:.3f} s against the source's {requested_s:.3f} s "
                 f"(a cover is as long as its source, within {tol_same:g} s)")
        return _gate(js.GATE_PASS if ok else js.GATE_FAIL, words if ok else f"length_mismatch: {words}",
                     tolerance_s=tol_same,
                     **{**common, "requested_s": round(requested_s, 4), "received_s": round(received_s, 4),
                        "delta_s": round(received_s - requested_s, 4)})
    if mode == LENGTH_CAP:
        if received_s > requested_s + LENGTH_EXACT_MIN_TOLERANCE_S:
            return _gate(js.GATE_FAIL, f"{received_s:.1f} s is longer than the {requested_s:g} s cap",
                         tolerance_s=LENGTH_EXACT_MIN_TOLERANCE_S, **common)
        floor = cap_floor_s(requested_s)
        if received_s < floor:
            return _gate(js.GATE_FAIL, f"{received_s:.1f} s is under {floor:g} s, too short to be a song "
                                       f"for a {requested_s:g} s cap",
                         tolerance_s=LENGTH_EXACT_MIN_TOLERANCE_S, floor_s=round(floor, 2), **common)
        return _gate(js.GATE_PASS, f"{received_s:.1f} s within the {requested_s:g} s cap (the engine ends the song itself)",
                     tolerance_s=LENGTH_EXACT_MIN_TOLERANCE_S, **common)
    if abs(delta) > tol:
        return _gate(js.GATE_FAIL, f"{received_s:.1f} s against {requested_s:g} s requested "
                                   f"({delta:+.1f} s, tolerance {tol:.1f} s)", tolerance_s=round(tol, 2), **common)
    return _gate(js.GATE_PASS, f"{received_s:.1f} s against {requested_s:g} s requested "
                               f"({delta:+.1f} s, tolerance {tol:.1f} s)", tolerance_s=round(tol, 2), **common)


def clipping_gate(astats: Optional[Mapping[str, Any]]) -> Tuple[Dict[str, Any], Optional[int]]:
    """`(gate, clipped_samples)`; `clipped_samples` is None when it was not measured."""
    if astats is None:
        return _gate(js.GATE_NOT_MEASURED, "ffmpeg astats printed no peak level"), None
    peak_db, count = astats.get("peak_db"), astats.get("peak_count")
    if peak_db is None:
        return _gate(js.GATE_PASS, "no signal, nothing to clip", peak_dbfs=None, clipped_samples=0), 0
    if peak_db < CLIP_LEVEL_DB:
        return _gate(js.GATE_PASS, f"sample peak {peak_db:.2f} dBFS, under {CLIP_LEVEL_DB:.4f} dBFS (0.999)",
                     peak_dbfs=round(peak_db, 2), clipped_samples=0), 0
    if count is None:
        return _gate(js.GATE_NOT_MEASURED, f"sample peak {peak_db:.2f} dBFS reaches 0.999 but astats gave no count",
                     peak_dbfs=round(peak_db, 2)), None
    return _gate(js.GATE_FAIL, f"{count} sample(s) at the peak of {peak_db:.2f} dBFS (0.999 or above)",
                 peak_dbfs=round(peak_db, 2), clipped_samples=count), count


def true_peak_gate(true_peak_dbtp: Optional[float]) -> Dict[str, Any]:
    if true_peak_dbtp is None:
        return _gate(js.GATE_NOT_MEASURED, "no true peak: the file has no signal or ebur128 gave none")
    if true_peak_dbtp > TRUE_PEAK_MAX_DBTP:
        return _gate(js.GATE_FAIL, f"true peak {true_peak_dbtp:.2f} dBTP is above {TRUE_PEAK_MAX_DBTP:g} dBTP",
                     true_peak_dbtp=true_peak_dbtp)
    return _gate(js.GATE_PASS, f"true peak {true_peak_dbtp:.2f} dBTP, at or under {TRUE_PEAK_MAX_DBTP:g} dBTP",
                 true_peak_dbtp=true_peak_dbtp)


def split_runs_by_zone(runs: List[Tuple[float, float]],
                       zone: Optional[Tuple[float, float]]) -> Tuple[List[Tuple[float, float]], float]:
    """`(runs outside the edit zone, the longest silent stretch inside it)`: a run that crosses an edge
    of the zone is cut there (SPEC §8 gates 4 and 5: silence outside the edit zone, silence instead of
    the edit)."""
    if zone is None:
        return list(runs), 0.0
    z0, z1 = zone
    outside: List[Tuple[float, float]] = []
    inside = 0.0
    for a, b in runs:
        if b <= z0 or a >= z1:
            outside.append((a, b))
            continue
        inside = max(inside, min(b, z1) - max(a, z0))
        if a < z0:
            outside.append((a, z0))
        if b > z1:
            outside.append((z1, b))
    return outside, round(inside, 2)


def silence_gate(found: Optional[Mapping[str, Any]], *, zone_silence_s: Optional[float] = None,
                 zone_expects_silence: bool = False) -> Dict[str, Any]:
    """The silence gate. With an edit zone (`zone_silence_s` is the longest silence inside it, and the
    runs in `found` are those outside it): a removal expects its zone to be silent; a repaint whose zone
    holds `INTERNAL_SILENCE_MAX_S` or more of silence has a hole (silence instead of the edit)."""
    if found is None:
        return _gate(js.GATE_NOT_MEASURED, "the file's length is unknown, so silence cannot be placed")
    why: List[str] = []
    if zone_silence_s is not None and not zone_expects_silence and zone_silence_s >= INTERNAL_SILENCE_MAX_S:
        why.append(f"a hole: {zone_silence_s:.1f} s of silence inside the edited region, where the edit should "
                   f"have put sound (from {INTERNAL_SILENCE_MAX_S:g} s)")
    if found["whole"]:
        why.append("the whole take is silent")
    if found["leading_s"] > LEADING_SILENCE_MAX_S:
        why.append(f"{found['leading_s']:.1f} s of silence at the start (over {LEADING_SILENCE_MAX_S:g} s)")
    if found["longest_internal_s"] >= INTERNAL_SILENCE_MAX_S:
        why.append(f"a {found['longest_internal_s']:.1f} s dropout inside the take (from {INTERNAL_SILENCE_MAX_S:g} s)")
    if found["tail_s"] > TAIL_SILENCE_MAX_S:
        why.append(f"{found['tail_s']:.1f} s of silence at the end (over {TAIL_SILENCE_MAX_S:g} s; a quiet tail up to "
                   f"{TAIL_SILENCE_MAX_S:g} s is normal)")
    values = {"leading_s": found["leading_s"], "tail_s": found["tail_s"],
              "longest_internal_s": found["longest_internal_s"], "whole_take": found["whole"]}
    zone_words = ""
    if zone_silence_s is not None:
        values["edit_zone_silence_s"] = zone_silence_s
        values["edit_zone_expects_silence"] = zone_expects_silence
        zone_words = (" outside the removed region (its silence is the edit)" if zone_expects_silence
                      else f"; {zone_silence_s:.1f} s of silence inside the edited region")
    if why:
        return _gate(js.GATE_FAIL, "; ".join(why), **values)
    return _gate(js.GATE_PASS, f"start {found['leading_s']:.1f} s, end {found['tail_s']:.1f} s, longest dropout "
                               f"{found['longest_internal_s']:.1f} s{zone_words}: all within bounds", **values)


# -- the record ---------------------------------------------------------------------------

_ADVISORY: Dict[str, Dict[str, Any]] = {
    "seams": {"status": js.GATE_NOT_MEASURED,
              "reason": "a new take has no seam; seam metrics apply to region-edit takes (region_edit.py) and are "
                        "advisory only (decision 29)"},
    "lyrics_wer": {"status": js.GATE_NOT_MEASURED,
                   "reason": "needs the judge; advisory, never a gate (decision 29)"},
}


def _advisory() -> Dict[str, Dict[str, Any]]:
    return {k: dict(v) for k, v in _ADVISORY.items()}


SEAM_FIELDS = ("seam_start_jump_db", "seam_end_jump_db", "seam_click_score")


def _seam_number(value: Any) -> Optional[float]:
    """A seam field as `mp.qa/1` holds it: a finite number >= 0, else null (never inf or NaN)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def thresholds_of(gates: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    """The report's `thresholds`: `THRESHOLDS`, with the same-length tolerance the length gate actually
    used (a cover's 0.05 s or 2 s, `similar.COVER_SAME_TOLERANCE_S`; the probe of 2026-11-26 found the
    record saying 0.001 beside a gate that had judged with 0.05)."""
    out = dict(THRESHOLDS)
    length = gates.get("length") or {}
    tol = length.get("tolerance_s")
    if length.get("mode") == LENGTH_SAME and isinstance(tol, (int, float)) and not isinstance(tol, bool):
        out["length_same_tolerance_s"] = tol
    return out


def build_record(*, gates: Mapping[str, Dict[str, Any]], measurements: Mapping[str, Any],
                 integrated_lufs: Optional[float], true_peak_dbtp: Optional[float], clipped_samples: Optional[int],
                 measured_by: str, measured_at: Optional[str] = None,
                 note: Optional[str] = None, seam_fields: Optional[Mapping[str, Any]] = None,
                 seams_advisory: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """`{qa, qa_report}`: the `mp.qa/1` when its three required numbers exist, else None, and
    the report, whose verdict is the one the gates give. An edit take passes its seam numbers
    (`seam_fields`, `region_edit.seams_entry`) and its advisory `seams` entry; they never gate."""
    qa: Optional[Dict[str, Any]] = None
    seams = {k: _seam_number((seam_fields or {}).get(k)) for k in SEAM_FIELDS}
    if (integrated_lufs is not None and true_peak_dbtp is not None and clipped_samples is not None
            and math.isfinite(integrated_lufs) and math.isfinite(true_peak_dbtp)):
        qa = {"integrated_lufs": integrated_lufs, "true_peak_dbtp": true_peak_dbtp, "loudness_delta_lu": None,
              **seams, "clipped_samples": clipped_samples, "lyrics_wer": None, "measured_by": measured_by}
    advisory = _advisory()
    if seams_advisory is not None:
        entry = dict(seams_advisory)
        if entry.get("status") not in ("measured", js.GATE_NOT_MEASURED):
            entry["status"] = js.GATE_NOT_MEASURED   # an advisory entry never passes or fails (decision 29)
        advisory["seams"] = entry
    report: Dict[str, Any] = {
        "stage": 1,
        "verdict": js.qa_verdict_of(gates),
        "gates": {name: dict(gates[name]) for name in js.QA_GATES},
        "advisory": advisory,
        "measurements": dict(measurements),
        "thresholds": thresholds_of(gates),
        "measured_by": measured_by,
        "measured_at": measured_at or _now(),
    }
    if note:
        report["note"] = note
    return {"qa": qa, "qa_report": report}


def not_measured_record(reason: str, *, requested_s: Optional[float] = None, length_mode: str = LENGTH_EXACT,
                        measured_by: str = TOOLS_NAME) -> Dict[str, Any]:
    """The record for a take whose QA could not run: no `qa`, every gate `not_measured` with
    the reason, verdict `not_measured`."""
    why = reason.strip() or "stage-1 QA could not run"
    gates = {name: _gate(js.GATE_NOT_MEASURED, why) for name in js.QA_GATES}
    return build_record(gates=gates, measurements={"duration_requested_s": requested_s, "length_mode": length_mode},
                        integrated_lufs=None, true_peak_dbtp=None, clipped_samples=None, measured_by=measured_by,
                        note=why)


def measure_take_qa(path: Path, *, requested_s: Optional[float], length_mode: str = LENGTH_EXACT,
                    duration_s: Optional[float] = None, loudness: Optional[Mapping[str, Any]] = None,
                    tool_version: Optional[str] = None, tools: Optional[MediaTools] = None,
                    edit: Optional[Mapping[str, Any]] = None,
                    same_tolerance_s: Optional[float] = None) -> Dict[str, Any]:
    """Measure one take file and return `{qa, qa_report}`. `loudness` is the take's ebur128
    record when it has one with both the integrated loudness and the true peak (then ebur128 is
    not run again; a record missing either is measured again, Zcode #176); `duration_s` its exact
    length when
    known. `edit` is a region-edit take's `{action, zone: (t0, t1) or None, expects_silence,
    seams: {entry, qa_fields}}` (`region_edit`): the silence gate judges outside the zone and looks
    for a hole inside it, and the seam numbers fill `mp.qa/1`. Blocking: call it through
    `asyncio.to_thread`. Raises `QaError` when ffmpeg cannot be run; a measurement ffmpeg ran but
    could not read is a `not_measured` gate."""
    if length_mode not in LENGTH_MODES:
        raise QaError(f"unknown length mode {length_mode!r}")
    have_loudness = (isinstance(loudness, Mapping)
                     and isinstance(loudness.get("lufs_i"), (int, float))
                     and isinstance(loudness.get("true_peak_dbtp"), (int, float)))
    err = run_ffmpeg_pass(Path(path), tools, with_loudness=not have_loudness)
    by = f"{tool_version}: ebur128+astats+silencedetect" if tool_version else TOOLS_NAME

    duration = duration_s if isinstance(duration_s, (int, float)) and duration_s > 0 else parse_duration(err)
    if have_loudness:
        lufs = loudness.get("lufs_i")
        peak = loudness.get("true_peak_dbtp")
        loud_source = "the take's ebur128 loudness record"
    else:
        try:
            parsed = parse_ebur128_summary(err)
        except LoudnessError:
            parsed = {"lufs_i": None, "true_peak_dbtp": None}
        lufs, peak = parsed["lufs_i"], parsed["true_peak_dbtp"]
        loud_source = "ebur128 in the same ffmpeg pass"
    peak = peak if isinstance(peak, (int, float)) else None
    lufs = lufs if isinstance(lufs, (int, float)) else None

    astats = parse_astats(err)
    clip, clipped = clipping_gate(astats)
    zone = tuple(edit["zone"]) if edit and edit.get("zone") else None
    zone_silence: Optional[float] = None
    silence = None
    if duration:
        runs, inside = split_runs_by_zone(parse_silences(err, duration), zone)
        silence = classify_silences(runs, duration)
        zone_silence = inside if zone is not None else None
    gates = {
        "length": length_gate(requested_s, duration, length_mode, same_tolerance_s=same_tolerance_s),
        "clipping": clip,
        "true_peak": true_peak_gate(peak),
        "silence": silence_gate(silence, zone_silence_s=zone_silence,
                                zone_expects_silence=bool(edit and edit.get("expects_silence"))),
    }
    measurements: Dict[str, Any] = {
        "duration_s": round(duration, 2) if duration else None,
        "duration_requested_s": requested_s,
        "length_mode": length_mode,
        "integrated_lufs": lufs,
        "true_peak_dbtp": peak,
        "loudness_source": loud_source,
        "sample_peak_dbfs": astats.get("peak_db") if astats else None,
        "clipped_samples": clipped,
        "leading_silence_s": silence["leading_s"] if silence else None,
        "tail_silence_s": silence["tail_s"] if silence else None,
        "longest_internal_silence_s": silence["longest_internal_s"] if silence else None,
        "silent_runs_s": silence["runs"] if silence else None,
    }
    seams = (edit or {}).get("seams") or {}
    if edit:
        measurements["edit_zone_s"] = [round(zone[0], 3), round(zone[1], 3)] if zone else None
        measurements["edit_zone_silence_s"] = zone_silence
    return build_record(gates=gates, measurements=measurements, integrated_lufs=lufs, true_peak_dbtp=peak,
                        clipped_samples=clipped, measured_by=by, seam_fields=seams.get("qa_fields"),
                        seams_advisory=seams.get("entry"))


def ffmpeg_qa_meter(tools: Optional[MediaTools] = None) -> QaMeter:
    """A meter for the runner: `meter(path, requested_s=, length_mode=, duration_s=, loudness=,
    tool_version=) -> {qa, qa_report}`, raising `QaError`."""
    def meter(path: Path, **kwargs: Any) -> Dict[str, Any]:
        return measure_take_qa(path, tools=tools, **kwargs)
    return meter


# -- the source comparison --------------------------------------------------------------------

SourceMeter = Callable[[Path], Dict[str, Any]]


def measure_source_peaks(path: Path, tools: Optional[MediaTools] = None) -> Dict[str, Any]:
    """The peaks of the file a take was made from, measured with the same ffmpeg pass as a take:
    `{measured: True, true_peak_dbtp, sample_peak_dbfs, clipped_samples, integrated_lufs, measured_by}`.
    Raises `QaError` when ffmpeg cannot run. Blocking: call it through `asyncio.to_thread`."""
    err = run_ffmpeg_pass(Path(path), tools, with_loudness=True)
    try:
        loud = parse_ebur128_summary(err)
    except LoudnessError:
        loud = {"lufs_i": None, "true_peak_dbtp": None}
    astats = parse_astats(err)
    _clip_gate, clipped = clipping_gate(astats)

    def num(v: Any) -> Optional[float]:
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None
    return {"measured": True, "true_peak_dbtp": num(loud.get("true_peak_dbtp")),
            "sample_peak_dbfs": num(astats.get("peak_db")) if astats else None,
            "clipped_samples": clipped, "integrated_lufs": num(loud.get("lufs_i")), "measured_by": TOOLS_NAME}


def ffmpeg_source_meter(tools: Optional[MediaTools] = None) -> SourceMeter:
    """A source meter for the runner: `meter(path) -> measure_source_peaks(...)`, raising `QaError`."""
    def meter(path: Path) -> Dict[str, Any]:
        return measure_source_peaks(path, tools)
    return meter


def _db(v: Optional[float], unit: str) -> str:
    return f"{v:+.2f} {unit}" if isinstance(v, (int, float)) else "unknown"


def _edit_clip_inherited(clip: Mapping[str, Any], s_peak: Any, s_clip: Any) -> bool:
    """A failed clipping gate of an edit take whose overs are the source's: the take's peak within
    `SOURCE_PEAK_TOLERANCE_DB` of the source's and no more samples at it. astats counts the samples at the
    peak (a lower bound), so a take whose peak is lower but still clipped can read as adding overs: flagged,
    never hidden."""
    t_peak, t_clip = clip.get("peak_dbfs"), clip.get("clipped_samples")
    return (isinstance(s_clip, int) and s_clip > 0 and isinstance(s_peak, (int, float))
            and isinstance(t_peak, (int, float)) and isinstance(t_clip, int)
            and t_peak <= s_peak + SOURCE_PEAK_TOLERANCE_DB and t_clip <= s_clip)


def compare_with_source(record: Mapping[str, Any], source: Optional[Mapping[str, Any]],
                        relation: str) -> Dict[str, Any]:
    """`{qa, qa_report}` with the clipping and true-peak gates read against the take's source.

    `edit`: the take is the source outside its zone, bit for bit, so the source's overs come along by
    design. A gate fails only when the take adds overs (`_edit_clip_inherited`; a true peak more than
    `SOURCE_PEAK_TOLERANCE_DB` over the source's); an inherited over passes with
    `inherited_from_source: true` and a detail that says so. `cover`: a new rendering, so the verdict stays
    the take's own and the source's figures are only written beside it. The source's numbers go in
    `measurements.source` and in both gates; with no measurement the gates stay as measured. Flags only."""
    if relation not in (EDIT, COVER):
        raise ValueError(f"unknown source relation {relation!r}")
    report = dict(record["qa_report"])
    out = {"qa": record.get("qa"), "qa_report": report}
    gates = {k: dict(v) for k, v in (report.get("gates") or {}).items()}
    src = dict(source) if isinstance(source, Mapping) else {"measured": False, "reason": "no source measurement"}
    src["relation"] = relation
    report["measurements"] = {**(report.get("measurements") or {}), "source": src}
    report["gates"] = gates
    if not src.get("measured"):
        return out
    s_tp, s_peak, s_clip = src.get("true_peak_dbtp"), src.get("sample_peak_dbfs"), src.get("clipped_samples")
    clip, tp = gates.get("clipping") or {}, gates.get("true_peak") or {}
    clip_src = (f"the source has {s_clip if s_clip is not None else 'an unknown number of'} clipped sample(s), "
                f"sample peak {_db(s_peak, 'dBFS')}")
    tp_src = f"the source's true peak is {_db(s_tp, 'dBTP')}"
    if clip:
        clip.update(source_clipped_samples=s_clip, source_peak_dbfs=s_peak)
    if tp:
        tp["source_true_peak_dbtp"] = s_tp
    if relation == COVER:
        for gate, said in ((clip, clip_src), (tp, tp_src)):
            if gate:
                gate["detail"] = f"{gate['detail']} ({said}; a cover is a new rendering, judged on its own)"
        return out
    if clip.get("status") == js.GATE_FAIL:
        if _edit_clip_inherited(clip, s_peak, s_clip):
            clip.update(status=js.GATE_PASS, inherited_from_source=True,
                        detail=f"advisory: inherited from the source: {clip['detail']}; {clip_src} (the take adds "
                               f"none, within {SOURCE_PEAK_TOLERANCE_DB:g} dB)")
        else:
            clip.update(inherited_from_source=False, detail=f"{clip['detail']}; {clip_src}, so the take adds clipping")
    if tp.get("status") == js.GATE_FAIL:
        t_tp = tp.get("true_peak_dbtp")
        if isinstance(s_tp, (int, float)) and isinstance(t_tp, (int, float)) and t_tp <= s_tp + SOURCE_PEAK_TOLERANCE_DB:
            tp.update(status=js.GATE_PASS, inherited_from_source=True,
                      detail=f"advisory: inherited from the source: {tp['detail']}; {tp_src} (the take raises it by "
                             f"no more than {SOURCE_PEAK_TOLERANCE_DB:g} dB)")
        else:
            tp.update(inherited_from_source=False, detail=f"{tp['detail']}; {tp_src}, so the take raises it")
    report["verdict"] = js.qa_verdict_of(gates)
    return out


def qa_summary(take: Mapping[str, Any]) -> Dict[str, Any]:
    """What an agent or a list needs of a take's QA: `{verdict, failed, not_measured, text}`.
    A take with no report (written before QA existed) is `not_measured` with that reason."""
    report = take.get("qa_report")
    if not isinstance(report, Mapping):
        return {"verdict": js.QA_NOT_MEASURED, "failed": [], "not_measured": list(js.QA_GATES),
                "text": "QA not measured (no stage-1 record on this take)"}
    gates = report.get("gates") or {}
    failed = [g for g in js.QA_GATES if (gates.get(g) or {}).get("status") == js.GATE_FAIL]
    unknown = [g for g in js.QA_GATES if (gates.get(g) or {}).get("status") == js.GATE_NOT_MEASURED]
    verdict = report.get("verdict")
    if verdict == js.QA_FAILED:
        text = "QA failed: " + ", ".join(failed)
    elif verdict == js.QA_PASSED:
        text = "QA passed"
    else:
        text = "QA not measured" + (": " + ", ".join(unknown) if unknown else "")
    return {"verdict": verdict, "failed": failed, "not_measured": unknown, "text": text}
