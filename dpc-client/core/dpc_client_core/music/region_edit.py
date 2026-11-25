"""Region edits of a library track: `regenerate_region`, `fix_seam` and `remove_region` (SPEC §3 flow 3, §4
"Length of a region take", §6 "Default seam", §8 "Seam QA by edit type", §12 decisions 23 and 26-29;
ADR-051 status row B4, backlog card REGION-EDIT-JOBS-HAVE-NO-ENGINE-SO-REGENERATE-REGION-AND-FIX-SEAM-ANSWER-UNSUPPORTED).

The pure part: the params of an edit job, the zone arithmetic, reading and writing the WAVs the runner
works on, the splice and the removal, and the seam measurements. The runner (`runner.py`) does the
engine calls, the files, the takes and the record; nothing here touches the store, the engine or the
library.

**The three zones** (SPEC flow 3). `job.region` is the **intent** zone (where the problem is; at most
`MAX_REGION_S`). `params.allowed_zone` is what the engine may rework (null = the intent zone; for
`fix_seam` a window of at least `FIX_SEAM_WINDOW_S` around the seam). `params.splice_bounds` is where the
take joins the source (null = the allowed zone). intent ⊆ splice bounds ⊆ allowed zone.

**regenerate_region / fix_seam: acestep.cpp repaint.** acestep.cpp's `ace-server` serves repaint
(`POST /synth` multipart, part `request` with `task_type: "repaint"`, `repainting_start` /
`repainting_end` in seconds, part `audio` the source; tools/ace-server.cpp `handle_synth`,
src/pipeline-synth.cpp `run_repaint`). The engine regenerates whole 25 Hz latent frames
(1920 samples at 48 kHz), so the window sent is the allowed zone widened to that grid
(`engine_window`). It answers wav32 so the server does not peak-normalise the output
(ace-server.cpp `synth_worker`: `audio_normalize` is skipped for `WAV_F32`), which would change the level
of the whole file against the source. **Forge's option A** (SPEC §4): the runner splices only the
splice bounds of the engine's output back into the control, so the take is the control's length by
construction and everything outside the splice bounds is the control's samples, unchanged.

**The seam** (SPEC §6 "Default seam"): a crossfade at each edge, inside the splice bounds, each edge
moved to the quietest sample pair within `ZERO_CROSS_SEARCH_MS` inward (a near-zero crossing). The shape
follows the measured coherence of the two sides over the fade (normalised correlation at lag 0, the
runner's splice is sample-aligned): **equal-gain (linear)** when |corr| >= `COHERENCE_LINEAR_MIN`, else
**equal-power**. SPEC: the length and the coherence threshold are not chosen; `CROSSFADE_MS_DEFAULT`
(75 ms, inside the 50-100 ms Forge measured passing) and 0.85 (the top of the 0.62-0.85 gap Ark
measured) are start values, recorded on every take, to tune live (decision 28: checked live).

**remove_region** removes the sound, not the time (decision 23): the region becomes silence with a fade
out and a fade in of `CROSSFADE_MS_DEFAULT` **inside** the region, so every sample outside the region is
the control's (SPEC §8 item 7: loudness outside the edit zone unchanged). No model, no card.
`cut_with_shift` (decision 27) is the one exception: on the person's word only, for a region that ends
within `NEAR_END_S` of the end, the region is cut out and what follows moves earlier; the take is shorter
by the region, and the lyrics are re-timed (`retime_lines`).

**Seam metrics** (SPEC §8, decision 29): advisory pointers, never gates. For each two-sided seam: the
RMS jump between 20 ms before the fade and 20 ms after it (dB, absolute), the click score (largest
adjacent-sample step within 10 ms of the fade over the median step in 250 ms around it) and Ark's step
over the material's own p99. A zero denominator is null with a reason, never inf (SPEC "No inf in a
record"). A removed region has no seam fields; its fades are measured instead (`removed_region`).
"""

from __future__ import annotations

import math
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from . import job_schemas as js

# -- actions and engines -------------------------------------------------------------------

REGENERATE_REGION, FIX_SEAM, REMOVE_REGION = "regenerate_region", "fix_seam", "remove_region"
REPAINT_ACTIONS = (REGENERATE_REGION, FIX_SEAM)
BUILT_ACTIONS = (REGENERATE_REGION, FIX_SEAM, REMOVE_REGION)
# `params.engine`. SPEC §4's enum ([P]) names `ace-step-1.5-api:repaint` and `ffmpeg:cut`; DPC runs
# acestep.cpp (decision 31) and removes a region in its own code, so the values say that.
ENGINE_REPAINT = "acestep:repaint"
ENGINE_REMOVE = "dpc:fade_to_silence"
ENGINE_OF_ACTION = {REGENERATE_REGION: ENGINE_REPAINT, FIX_SEAM: ENGINE_REPAINT, REMOVE_REGION: ENGINE_REMOVE}

# -- limits and start values (all echoed in the take's record) -----------------------------

TAKES_PER_JOB = 2                 # SPEC §7 limits.takes_per_job; remove_region makes 1
MAX_REGION_S = 30.0               # SPEC §7 limits.max_region_s, on the intent zone
MAX_ALLOWED_S = 60.0              # a sanity bound on the engine's window
FIX_SEAM_WINDOW_S = 2.0           # fix_seam's allowed zone: at least this, centred on the region
CROSSFADE_MS_DEFAULT = 75.0       # start value (SPEC §6: length not chosen; 50-100 ms passed once)
CROSSFADE_MS_MIN, CROSSFADE_MS_MAX = 10.0, 500.0
COHERENCE_LINEAR_MIN = 0.85       # start value (SPEC §6: threshold not chosen, between ~0.62 and ~0.85)
ZERO_CROSS_SEARCH_MS = 10.0
NEAR_END_S = 10.0                 # decision 27: "near the end" for cut_with_shift (start value)
EDGE_EPS_S = 0.01                 # a region may reach the track's probed end within this

RATE = 48000                      # acestep.cpp works at 48 kHz stereo; the control is decoded to it
CHANNELS = 2
GRID_HZ = 25                      # acestep.cpp latent frames per second (1920 samples at 48 kHz)
WINDOW_NUDGE_S = 0.002            # sent past a grid line so the engine's int() lands on it

JUMP_WINDOW_MS = 20.0             # SPEC §8: RMS of 20 ms before vs 20 ms after
CLICK_SPAN_MS = 10.0
LOCAL_WINDOW_MS = 250.0

ENGINE_OUTPUT_FORMAT = "wav32"    # float: ace-server skips its peak normalisation for it
CONTROL_FORMAT = "wav s24 48 kHz stereo"

_PARAM_KEYS = ("engine", "takes", "seed", "allowed_zone", "splice_bounds", "crossfade_ms", "caption", "lyrics",
               "cut_with_shift")
_SOURCE_KEYS = ("source_content_hash", "source_duration_s")
MAX_TEXT = 4000


class EditError(ValueError):
    """An edit request or an edit step breaks a rule; `field` names the param when there is one."""

    def __init__(self, message: str, field: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.field = field


def takes_for(action: str) -> int:
    return 1 if action == REMOVE_REGION else TAKES_PER_JOB


# -- params --------------------------------------------------------------------------------

def _num(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise EditError(f"{where} must be a number", where.split(".")[0])
    return float(value)


def _range(value: Any, where: str) -> Optional[Tuple[float, float]]:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"t_start", "t_end"}:
        raise EditError(f"{where} must be {{t_start, t_end}} or null", where)
    a, b = _num(value["t_start"], f"{where}.t_start"), _num(value["t_end"], f"{where}.t_end")
    if a < 0 or b <= a:
        raise EditError(f"{where} needs 0 <= t_start < t_end", where)
    return a, b


def _as_range(r: Tuple[float, float]) -> Dict[str, float]:
    return {"t_start": round(r[0], 4), "t_end": round(r[1], 4)}


def normalize_params_edit(action: str, params: Any, region: Any, *, duration_s: Optional[float],
                          content_hash: Optional[str] = None, by_person: bool = True) -> Dict[str, Any]:
    """The complete params of an edit job, or `EditError`. `region` is the intent zone; `duration_s`
    the base track's probed length (the store gets it from the library), `content_hash` its audio
    hash (the runner refuses a file that changed since, SPEC §6 `hash_mismatch`). `by_person`: only the
    person may ask for `cut_with_shift` (decision 27)."""
    if action not in BUILT_ACTIONS:
        raise EditError(f"the {action!r} edit is not built", "action")
    if not isinstance(params, Mapping):
        raise EditError("params must be an object", "params")
    extra = sorted(set(params) - set(_PARAM_KEYS) - set(_SOURCE_KEYS))
    if extra:
        raise EditError(f"params has undeclared key(s) for an edit: {', '.join(extra)}", extra[0])
    intent = _range(region, "region")
    if intent is None:
        raise EditError("an edit job needs a region {t_start, t_end}", "region")
    if duration_s is None or not duration_s > 0:
        raise EditError("the track's length is not known yet (it is still being read); try again shortly", "track_id")
    if intent[1] > duration_s + EDGE_EPS_S:
        raise EditError(f"the region ends at {intent[1]:g} s, past the end of the track ({duration_s:g} s)", "region")
    intent = (intent[0], min(intent[1], duration_s))
    if intent[1] - intent[0] > MAX_REGION_S + 1e-9:
        raise EditError(f"the region is {intent[1] - intent[0]:.1f} s; at most {MAX_REGION_S:g} s (limits.max_region_s)",
                        "region")
    engine = params.get("engine")
    if engine is not None and engine != ENGINE_OF_ACTION[action]:
        raise EditError(f"engine for {action} is {ENGINE_OF_ACTION[action]!r}", "engine")
    takes = params.get("takes")
    if takes is not None and (isinstance(takes, bool) or takes != takes_for(action)):
        raise EditError(f"a {action} job makes {takes_for(action)} take(s)", "takes")
    crossfade = params.get("crossfade_ms")
    crossfade = CROSSFADE_MS_DEFAULT if crossfade is None else _num(crossfade, "crossfade_ms")
    if not CROSSFADE_MS_MIN <= crossfade <= CROSSFADE_MS_MAX:
        raise EditError(f"crossfade_ms must be from {CROSSFADE_MS_MIN:g} to {CROSSFADE_MS_MAX:g}", "crossfade_ms")
    out: Dict[str, Any] = {"engine": ENGINE_OF_ACTION[action], "takes": takes_for(action), "seed": None,
                           "allowed_zone": None, "splice_bounds": None, "crossfade_ms": crossfade,
                           "caption": None, "lyrics": None, "cut_with_shift": False,
                           "source_content_hash": content_hash if isinstance(content_hash, str) else None,
                           "source_duration_s": round(float(duration_s), 4)}
    if action == REMOVE_REGION:
        for k in ("seed", "allowed_zone", "splice_bounds", "caption", "lyrics"):
            if params.get(k) is not None:
                raise EditError(f"{k} is for a repaint; remove_region uses no model", k)
        cut = params.get("cut_with_shift", False)
        if not isinstance(cut, bool):
            raise EditError("cut_with_shift must be true or false", "cut_with_shift")
        if cut:
            if not by_person:
                raise EditError("cut_with_shift shortens the track and re-times its lyrics: only the person may ask "
                                "for it (decision 27)", "cut_with_shift")
            if intent[1] < duration_s - NEAR_END_S:
                raise EditError(f"cut_with_shift is for a region near the end: it must end within {NEAR_END_S:g} s of "
                                f"the end ({duration_s:g} s)", "cut_with_shift")
        if 2 * crossfade / 1000.0 > intent[1] - intent[0]:
            out["crossfade_ms"] = round((intent[1] - intent[0]) * 500.0, 3)
        out["cut_with_shift"] = cut
        return out
    if params.get("cut_with_shift"):
        raise EditError("cut_with_shift is for remove_region only", "cut_with_shift")
    seed = params.get("seed")
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int) or not js.SEED_MIN <= seed <= js.SEED_MAX):
        raise EditError(f"seed must be a whole number from {js.SEED_MIN} to {js.SEED_MAX}, or null", "seed")
    out["seed"] = seed
    for k in ("caption", "lyrics"):
        v = params.get(k)
        if v is not None:
            if not isinstance(v, str) or (k == "caption" and not v.strip()):
                raise EditError(f"{k} must be text" + (" (not blank)" if k == "caption" else ""), k)
            if len(v) > MAX_TEXT:
                raise EditError(f"{k} is {len(v)} characters; at most {MAX_TEXT}", k)
            out[k] = v.strip() if k == "caption" else v
    allowed = _range(params.get("allowed_zone"), "allowed_zone")
    if allowed is None:
        allowed = intent
        if action == FIX_SEAM and intent[1] - intent[0] < FIX_SEAM_WINDOW_S:
            c, half = (intent[0] + intent[1]) / 2.0, FIX_SEAM_WINDOW_S / 2.0
            allowed = (max(0.0, c - half), min(duration_s, c + half))
    if allowed[0] > intent[0] + 1e-9 or allowed[1] < intent[1] - 1e-9:
        raise EditError("allowed_zone must contain the region (the intent zone)", "allowed_zone")
    if allowed[1] > duration_s + EDGE_EPS_S:
        raise EditError("allowed_zone ends past the end of the track", "allowed_zone")
    allowed = (allowed[0], min(allowed[1], duration_s))
    if allowed[1] - allowed[0] > MAX_ALLOWED_S + 1e-9:
        raise EditError(f"allowed_zone is {allowed[1] - allowed[0]:.1f} s; at most {MAX_ALLOWED_S:g} s", "allowed_zone")
    splice = _range(params.get("splice_bounds"), "splice_bounds") or allowed
    if splice[0] < allowed[0] - 1e-9 or splice[1] > allowed[1] + 1e-9:
        raise EditError("splice_bounds must lie inside allowed_zone", "splice_bounds")
    if splice[0] > intent[0] + 1e-9 or splice[1] < intent[1] - 1e-9:
        raise EditError("splice_bounds must contain the region (the intent zone)", "splice_bounds")
    if 2 * crossfade / 1000.0 > splice[1] - splice[0]:
        raise EditError(f"two {crossfade:g} ms crossfades do not fit in the {splice[1] - splice[0]:.2f} s splice", "crossfade_ms")
    if params.get("allowed_zone") is not None or allowed != intent:
        out["allowed_zone"] = _as_range(allowed)
    if params.get("splice_bounds") is not None:
        out["splice_bounds"] = _as_range(splice)
    return out


def zones_of(region: Mapping[str, Any], params: Mapping[str, Any]) -> Dict[str, Tuple[float, float]]:
    """`{intent, allowed, splice}` in seconds from a job's region and normalised params."""
    intent = (float(region["t_start"]), float(region["t_end"]))
    a = params.get("allowed_zone")
    allowed = (float(a["t_start"]), float(a["t_end"])) if a else intent
    s = params.get("splice_bounds")
    splice = (float(s["t_start"]), float(s["t_end"])) if s else allowed
    return {"intent": intent, "allowed": allowed, "splice": splice}


def region_words(action: str, region: Mapping[str, Any]) -> str:
    """"regenerate 0:12.0-0:18.5": the region named for a card or a title."""
    def t(x: float) -> str:
        m, s = divmod(max(0.0, float(x)), 60.0)
        return f"{int(m)}:{s:04.1f}"
    verb = {REGENERATE_REGION: "regenerate", FIX_SEAM: "fix seam", REMOVE_REGION: "remove"}.get(action, action)
    return f"{verb} {t(region['t_start'])}-{t(region['t_end'])}"


# -- the engine window ---------------------------------------------------------------------

@dataclass(frozen=True)
class EngineWindow:
    frame_start: int      # latent frames, [frame_start, frame_end)
    frame_end: int
    sent_start_s: float   # what goes into repainting_start / repainting_end
    sent_end_s: float

    @property
    def start_s(self) -> float:
        return self.frame_start / GRID_HZ

    @property
    def end_s(self) -> float:
        return self.frame_end / GRID_HZ

    def as_dict(self) -> Dict[str, Any]:
        return {"t_start": round(self.start_s, 4), "t_end": round(self.end_s, 4), "grid_hz": GRID_HZ,
                "repainting_start": self.sent_start_s, "repainting_end": self.sent_end_s}


def engine_window(allowed: Tuple[float, float], control_frames: int, rate: int = RATE) -> EngineWindow:
    """The allowed zone widened to acestep.cpp's 25 Hz latent grid and clamped to the control. The
    values sent sit `WINDOW_NUDGE_S` past each grid line, so the engine's `(int)(s * 25)`
    (pipeline-synth-ops.cpp) lands on the line and a float that falls just short cannot drop a frame."""
    total = int(math.ceil(control_frames / (rate / GRID_HZ)))
    f0 = max(0, int(math.floor(allowed[0] * GRID_HZ + 1e-9)))
    f1 = min(total, int(math.ceil(allowed[1] * GRID_HZ - 1e-9)))
    if f1 <= f0:
        f1 = min(total, f0 + 1)
    return EngineWindow(f0, f1, round(f0 / GRID_HZ + WINDOW_NUDGE_S, 4), round(f1 / GRID_HZ + WINDOW_NUDGE_S, 4))


def build_repaint_request(*, caption: str, lyrics: str, window: EngineWindow, seed: int, takes: int,
                          inference_steps: int, synth_model: str, vae: str) -> bytes:
    """The `request` part of acestep.cpp's `/synth` multipart for a repaint (ARCHITECTURE.md "Repaint";
    request.cpp field names). One request with `synth_batch_size` = the takes: the source is shared by
    the batch (ace-server.cpp `handle_synth`) and variant i gets seed + i (`synth_worker`)."""
    import json
    req = {"task_type": "repaint", "caption": caption, "lyrics": lyrics,
           "repainting_start": window.sent_start_s, "repainting_end": window.sent_end_s,
           "seed": int(seed), "synth_batch_size": int(takes), "inference_steps": int(inference_steps),
           "output_format": ENGINE_OUTPUT_FORMAT, "synth_model": synth_model, "vae": vae}
    return json.dumps(req, ensure_ascii=False).encode("utf-8")


# -- WAV in and out ------------------------------------------------------------------------

@dataclass
class Pcm:
    """A WAV's samples as float32 `(frames, channels)`, read through a memory map when it is PCM24 or
    float, so a long track is not held twice."""
    data: np.ndarray
    rate: int

    @property
    def frames(self) -> int:
        return int(self.data.shape[0])

    @property
    def channels(self) -> int:
        return int(self.data.shape[1])


def _wav_layout(path: Path, start: int = 0, end: Optional[int] = None) -> Tuple[int, int, int, int, int, int]:
    """(format tag, channels, rate, bits, data offset, data size) of a RIFF/WAVE inside `path[start:end]`."""
    with open(path, "rb") as f:
        f.seek(start)
        limit = (end if end is not None else os.path.getsize(path))
        head = f.read(12)
        if len(head) != 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            raise EditError("not a RIFF/WAVE file")
        fmt = None
        while f.tell() + 8 <= limit:
            ch = f.read(8)
            cid, size = ch[:4], int.from_bytes(ch[4:8], "little")
            if cid == b"fmt ":
                body = f.read(size)
                tag = int.from_bytes(body[0:2], "little")
                if tag == 0xFFFE and len(body) >= 26:
                    tag = int.from_bytes(body[24:26], "little")
                fmt = (tag, int.from_bytes(body[2:4], "little"), int.from_bytes(body[4:8], "little"),
                       int.from_bytes(body[14:16], "little"))
                if size % 2:
                    f.seek(1, os.SEEK_CUR)
            elif cid == b"data":
                if fmt is None:
                    raise EditError("data chunk before fmt chunk")
                offset = f.tell()
                return (*fmt, offset, min(size, limit - offset))
            else:
                f.seek(size + (size % 2), os.SEEK_CUR)
    raise EditError("no data chunk")


def read_pcm(path: Path, start: int = 0, end: Optional[int] = None) -> Pcm:
    """The samples of the WAV at `path[start:end]` (a whole file, or one part of a multipart answer on
    disk) as float32 in [-1, 1]: PCM 16/24/32 bit or IEEE float 32."""
    tag, channels, rate, bits, offset, size = _wav_layout(Path(path), start, end)
    if channels < 1 or rate < 1:
        raise EditError("the WAV has no channels or no rate")
    width = (bits + 7) // 8
    frames = size // (width * channels)
    count = frames * channels
    if tag == 3 and bits == 32:
        raw = np.memmap(path, dtype="<f4", mode="r", offset=offset, shape=(count,))
        data = np.asarray(raw, dtype=np.float32)
    elif tag == 1 and bits == 16:
        data = np.memmap(path, dtype="<i2", mode="r", offset=offset, shape=(count,)).astype(np.float32) / 32768.0
    elif tag == 1 and bits == 32:
        data = (np.memmap(path, dtype="<i4", mode="r", offset=offset, shape=(count,)).astype(np.float64)
                / 2147483648.0).astype(np.float32)
    elif tag == 1 and bits == 24:
        b = np.memmap(path, dtype=np.uint8, mode="r", offset=offset, shape=(count * 3,)).reshape(count, 3)
        ints = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) | (b[:, 2].astype(np.int32) << 16))
        ints = np.where(ints & 0x800000, ints - 0x1000000, ints)
        data = ints.astype(np.float32) / 8388608.0
    else:
        raise EditError(f"unsupported WAV sample format (tag {tag}, {bits} bit)")
    return Pcm(data.reshape(frames, channels), rate)


def _s24_bytes(block: np.ndarray) -> bytes:
    ints = np.clip(np.rint(np.asarray(block, dtype=np.float64) * 8388608.0), -8388608, 8388607).astype("<i4")
    return ints.reshape(-1).view(np.uint8).reshape(-1, 4)[:, :3].tobytes()


def write_wav_s24(dest: Path, pieces: Iterable[np.ndarray], *, rate: int, channels: int, chunk: int = 1 << 18) -> int:
    """Write the frames of `pieces` (each `(n, channels)`) one after another as a 24-bit PCM WAV,
    converting `chunk` frames at a time; samples beyond full scale are clipped (the take's QA counts
    them). Returns the frame count. The header is written last, with the sizes."""
    frames = 0
    with open(dest, "wb") as f:
        f.write(b"\0" * 44)
        for piece in pieces:
            if piece.shape[0] == 0:
                continue
            if piece.ndim != 2 or piece.shape[1] != channels:
                raise EditError("a piece of the take has the wrong channel count")
            for i in range(0, piece.shape[0], chunk):
                f.write(_s24_bytes(piece[i:i + chunk]))
            frames += piece.shape[0]
        data = frames * channels * 3
        f.seek(0)
        f.write(b"RIFF" + struct.pack("<I", 36 + data) + b"WAVE" + b"fmt " +
                struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * channels * 3, channels * 3, 24) +
                b"data" + struct.pack("<I", data))
        f.flush()
        os.fsync(f.fileno())
    return frames


# -- the splice ----------------------------------------------------------------------------

def _mono(block: np.ndarray) -> np.ndarray:
    return np.asarray(block, dtype=np.float64).mean(axis=1)


def coherence(a: np.ndarray, b: np.ndarray) -> Optional[float]:
    """Normalised correlation of two blocks at lag 0 (mono sums), or None when one side is silent."""
    x, y = _mono(a), _mono(b)
    den = math.sqrt(float(np.dot(x, x)) * float(np.dot(y, y)))
    if den <= 1e-12:
        return None
    return round(float(np.dot(x, y)) / den, 4)


def _quietest(ctrl: np.ndarray, other: np.ndarray, lo: int, hi: int) -> int:
    """The index in [lo, hi] where |control| + |other| (mono) is smallest: a near-zero crossing of both."""
    lo, hi = max(0, lo), max(lo, hi)
    if hi <= lo:
        return lo
    score = np.abs(_mono(ctrl[lo:hi + 1])) + np.abs(_mono(other[lo:hi + 1]))
    return lo + int(np.argmin(score))


def _gains(n: int, shape: str) -> Tuple[np.ndarray, np.ndarray]:
    """(fade in, fade out) gains over n frames, as columns."""
    t = (np.arange(n, dtype=np.float64) + 0.5) / max(1, n)
    if shape == "equal_gain":
        g_in = t
        g_out = 1.0 - t
    else:
        g_in = np.sin(t * math.pi / 2.0)
        g_out = np.cos(t * math.pi / 2.0)
    return g_in[:, None], g_out[:, None]


def _shape_for(corr: Optional[float]) -> str:
    return "equal_gain" if corr is not None and abs(corr) >= COHERENCE_LINEAR_MIN else "equal_power"


@dataclass
class Seam:
    """One edge where the take joins the control, in frames: the fade spans [start, end)."""
    side: str                 # "start" | "end"
    start: int
    end: int
    shape: str
    coherence: Optional[float]
    kind: str = "crossfade"   # "crossfade" | "fade_out" | "fade_in"

    def as_dict(self, rate: int) -> Dict[str, Any]:
        return {"side": self.side, "kind": self.kind, "t_start": round(self.start / rate, 4),
                "t_end": round(self.end / rate, 4), "fade_ms": round((self.end - self.start) * 1000.0 / rate, 2),
                "shape": self.shape, "coherence": self.coherence}


@dataclass
class SpliceResult:
    pieces: List[np.ndarray]
    frames: int
    seams: List[Seam]
    splice_frames: Tuple[int, int]
    notes: List[str] = field(default_factory=list)


def splice(control: Pcm, engine: Pcm, splice_s: Tuple[float, float], crossfade_ms: float) -> SpliceResult:
    """The take of a repaint: the control outside the splice bounds, the engine's output inside, a
    crossfade at each edge (shape by coherence), each edge moved inward to a near-zero crossing.
    The take has exactly the control's frames (Forge's option A). Raises `EditError` when the engine's
    output does not cover the splice bounds or has another rate or channel count."""
    if engine.rate != control.rate or engine.channels != control.channels:
        raise EditError(f"the engine answered {engine.rate} Hz x {engine.channels}, the control is "
                        f"{control.rate} Hz x {control.channels}")
    rate, n = control.rate, control.frames
    s0 = max(0, int(round(splice_s[0] * rate)))
    s1 = min(n, int(round(splice_s[1] * rate)))
    if engine.frames < s1:
        raise EditError(f"the engine's output ({engine.frames / rate:.3f} s) ends before the splice ({s1 / rate:.3f} s)")
    xf = max(1, int(round(crossfade_ms * rate / 1000.0)))
    search = int(round(ZERO_CROSS_SEARCH_MS * rate / 1000.0))
    if s1 - s0 < 2 * xf:
        raise EditError("the crossfades do not fit inside the splice bounds")
    seams: List[Seam] = []
    notes: List[str] = []
    c, e = control.data, engine.data
    if s0 > 0:
        a = _quietest(c, e, s0, min(s0 + search, s1 - 2 * xf))
        corr = coherence(c[a:a + xf], e[a:a + xf])
        seams.append(Seam("start", a, a + xf, _shape_for(corr), corr))
    else:
        a = 0
        notes.append("the splice starts at the track's start: no seam there")
    if s1 < n:
        b = _quietest(c, e, max(a + 2 * xf, s1 - search), s1)
        corr = coherence(c[b - xf:b], e[b - xf:b])
        seams.append(Seam("end", b - xf, b, _shape_for(corr), corr))
    else:
        b = n
        notes.append("the splice reaches the track's end: no seam there")
    pieces: List[np.ndarray] = [c[:a]]
    inner_from, inner_to = a, b
    for seam in seams:
        if seam.side == "start":
            g_in, g_out = _gains(xf, seam.shape)
            pieces.append((c[seam.start:seam.end] * g_out + e[seam.start:seam.end] * g_in).astype(np.float32))
            inner_from = seam.end
    end_seam = next((s for s in seams if s.side == "end"), None)
    if end_seam is not None:
        inner_to = end_seam.start
    pieces.append(e[inner_from:inner_to])
    if end_seam is not None:
        g_in, g_out = _gains(xf, end_seam.shape)
        pieces.append((e[end_seam.start:end_seam.end] * g_out + c[end_seam.start:end_seam.end] * g_in).astype(np.float32))
    pieces.append(c[b:])
    frames = sum(p.shape[0] for p in pieces)
    if frames != n:
        raise EditError(f"the splice made {frames} frames from a control of {n}: a bug (length_mismatch)")
    return SpliceResult(pieces, frames, seams, (a, b), notes)


def remove(control: Pcm, region_s: Tuple[float, float], fade_ms: float, *, cut_with_shift: bool = False) -> SpliceResult:
    """The take of a remove: the region silent with a fade out and a fade in inside it (the length kept,
    decision 23), or with `cut_with_shift` the region cut out, a fade out before the cut and a fade in
    after it (decision 27)."""
    rate, n = control.rate, control.frames
    r0 = max(0, int(round(region_s[0] * rate)))
    r1 = min(n, int(round(region_s[1] * rate)))
    if r1 <= r0:
        raise EditError("the region is empty at the control's rate")
    f = max(1, int(round(fade_ms * rate / 1000.0)))
    c = control.data
    seams: List[Seam] = []
    if not cut_with_shift:
        f = min(f, (r1 - r0) // 2) or 1
        _g, g_out = _gains(f, "equal_power")   # a quarter-cosine fade out; reversed, a fade in
        g_in = g_out[::-1]
        pieces = [c[:r0]]
        if r0 > 0:
            pieces.append((c[r0:r0 + f] * g_out).astype(np.float32))
            seams.append(Seam("start", r0, r0 + f, "cosine", None, "fade_out"))
            lo = r0 + f
        else:
            lo = r0
        hi = r1 - f if r1 < n else r1
        pieces.append(np.zeros((max(0, hi - lo), control.channels), dtype=np.float32))
        if r1 < n:
            pieces.append((c[r1 - f:r1] * g_in).astype(np.float32))
            seams.append(Seam("end", r1 - f, r1, "cosine", None, "fade_in"))
        pieces.append(c[r1:])
        frames = sum(p.shape[0] for p in pieces)
        if frames != n:
            raise EditError(f"the removal made {frames} frames from a control of {n}: a bug (length_mismatch)")
        return SpliceResult(pieces, frames, seams, (r0, r1))
    # cut with shift: [0, r0) with a fade out at its end, then [r1, n) with a fade in at its start
    head_f = min(f, r0)
    tail_f = min(f, n - r1)
    pieces = []
    if r0 > 0:
        pieces.append(c[:r0 - head_f])
        _g, go = _gains(head_f, "equal_power")
        pieces.append((c[r0 - head_f:r0] * go).astype(np.float32))
        seams.append(Seam("start", r0 - head_f, r0, "cosine", None, "fade_out"))
    if r1 < n:
        _g, go = _gains(tail_f, "equal_power")
        pieces.append((c[r1:r1 + tail_f] * go[::-1]).astype(np.float32))
        pieces.append(c[r1 + tail_f:])
        seams.append(Seam("end", r0, r0 + tail_f, "cosine", None, "fade_in"))
    frames = sum(p.shape[0] for p in pieces)
    if frames != n - (r1 - r0):
        raise EditError("the cut made the wrong number of frames: a bug (length_mismatch)")
    return SpliceResult(pieces, frames, seams, (r0, r1), ["cut with shift: the take is shorter by the region"])


def retime_lines(lines: Sequence[Mapping[str, Any]], region_s: Tuple[float, float]) -> List[Dict[str, Any]]:
    """Lyrics lines (`mp.lyrics/1`) after a cut with shift: a line inside the region is dropped, a line
    after it moves earlier by the region's length, a line that overlaps an edge is clipped to it (one
    that starts inside and runs past the end starts at the cut)."""
    a, b = region_s
    cut = b - a
    out: List[Dict[str, Any]] = []
    for line in lines:
        t0, t1 = line.get("t_start"), line.get("t_end")
        if not isinstance(t0, (int, float)) or isinstance(t0, bool):
            out.append(dict(line))   # an unsynced line has no time to move
            continue
        has_end = isinstance(t1, (int, float)) and not isinstance(t1, bool)
        if a <= t0 < b:
            if has_end and t1 > b:
                out.append({**line, "t_start": round(a, 3), "t_end": round(t1 - cut, 3)})
            continue
        new = dict(line)
        if t0 >= b:
            new["t_start"] = round(t0 - cut, 3)
            if isinstance(t1, (int, float)) and not isinstance(t1, bool):
                new["t_end"] = round(t1 - cut, 3)
        elif isinstance(t1, (int, float)) and not isinstance(t1, bool) and t1 > a:
            new["t_end"] = round(a if t1 <= b else t1 - cut, 3)
        out.append(new)
    return out


# -- seam metrics (advisory) ---------------------------------------------------------------

def _rms(block: np.ndarray) -> float:
    if block.size == 0:
        return 0.0
    x = np.asarray(block, dtype=np.float64)
    return math.sqrt(float(np.mean(x * x)))


def seam_metrics(take: np.ndarray, seam: Seam, rate: int) -> Dict[str, Any]:
    """The advisory numbers of one seam of the take (SPEC §8): `jump_db`, `click_score`, `step_vs_p99`,
    each a finite number or null with a reason in `null_reasons`."""
    n = take.shape[0]
    w = int(round(JUMP_WINDOW_MS * rate / 1000.0))
    span = int(round(CLICK_SPAN_MS * rate / 1000.0))
    local = int(round(LOCAL_WINDOW_MS * rate / 1000.0))
    out: Dict[str, Any] = {"side": seam.side, "t_s": round((seam.start + seam.end) / 2.0 / rate, 4)}
    reasons: Dict[str, str] = {}
    before = take[max(0, seam.start - w):seam.start]
    after = take[seam.end:min(n, seam.end + w)]
    rb, ra = _rms(before), _rms(after)
    if before.shape[0] < w or after.shape[0] < w:
        out["jump_db"] = None
        reasons["jump_db"] = "the seam is within 20 ms of the track's edge"
    elif rb <= 1e-9 or ra <= 1e-9:
        out["jump_db"] = None
        reasons["jump_db"] = "denominator_zero: digital silence on one side"
    else:
        out["jump_db"] = round(abs(20.0 * math.log10(ra / rb)), 3)
    lo, hi = max(1, seam.start - local), min(n, seam.end + local)
    mono = _mono(take[lo - 1:hi])
    steps = np.abs(np.diff(mono))
    near_lo, near_hi = max(0, seam.start - span - lo), max(0, seam.end + span - lo)
    near = steps[near_lo:near_hi]
    if near.size == 0 or steps.size == 0:
        out["click_score"] = out["step_vs_p99"] = None
        reasons["click_score"] = reasons["step_vs_p99"] = "no samples around the seam"
    else:
        peak = float(near.max())
        median = float(np.median(steps))
        p99 = float(np.percentile(steps, 99))
        if median <= 1e-12:
            out["click_score"] = None
            reasons["click_score"] = "denominator_zero: the local median step is zero (silence)"
        else:
            out["click_score"] = round(peak / median, 3)
        if p99 <= 1e-12:
            out["step_vs_p99"] = None
            reasons["step_vs_p99"] = "denominator_zero: the local p99 step is zero (silence)"
        else:
            out["step_vs_p99"] = round(peak / p99, 3)
    if reasons:
        out["null_reasons"] = reasons
    return out


def fade_metrics(take: np.ndarray, seam: Seam, rate: int) -> Dict[str, Any]:
    """A removed region's fade (SPEC §8 "Seam QA by edit type", `mp.qa/2` sketch `removed_region`): its
    length and the sample step at its outer edge against the material's own p99 step (Ark)."""
    n = take.shape[0]
    local = int(round(LOCAL_WINDOW_MS * rate / 1000.0))
    edge = seam.start if seam.kind == "fade_out" else seam.end
    lo, hi = max(1, edge - local), min(n, edge + local)
    steps = np.abs(np.diff(_mono(take[lo - 1:hi])))
    out: Dict[str, Any] = {"side": seam.side, "kind": seam.kind, "t_s": round(edge / rate, 4),
                           "fade_ms": round((seam.end - seam.start) * 1000.0 / rate, 2)}
    i = edge - lo
    if steps.size == 0 or not 0 <= i < steps.size:
        out["boundary_step_vs_p99"] = None
        out["null_reasons"] = {"boundary_step_vs_p99": "no samples around the fade"}
        return out
    p99 = float(np.percentile(steps, 99))
    if p99 <= 1e-12:
        out["boundary_step_vs_p99"] = None
        out["null_reasons"] = {"boundary_step_vs_p99": "denominator_zero: the local p99 step is zero (silence)"}
    else:
        out["boundary_step_vs_p99"] = round(float(steps[i]) / p99, 3)
    return out


def frames_between(result: SpliceResult, lo: int, hi: int) -> np.ndarray:
    """Frames [lo, hi) of the take, cut from its pieces (the whole take is never put together)."""
    lo, hi = max(0, lo), min(result.frames, hi)
    out: List[np.ndarray] = []
    at = 0
    for p in result.pieces:
        n = p.shape[0]
        a, b = max(lo, at), min(hi, at + n)
        if a < b:
            out.append(np.asarray(p[a - at:b - at], dtype=np.float32))
        at += n
        if at >= hi:
            break
    if not out:
        return np.zeros((0, result.pieces[0].shape[1] if result.pieces else CHANNELS), dtype=np.float32)
    return np.concatenate(out, axis=0)


class _Local:
    """A seam's neighbourhood of the take with the seam moved into its coordinates, so the metric
    functions see a short array and `take.shape[0]` is still the edge of the track where it is."""

    def __init__(self, result: SpliceResult, seam: Seam, rate: int):
        pad = int(round((LOCAL_WINDOW_MS + JUMP_WINDOW_MS + CLICK_SPAN_MS) * rate / 1000.0)) + 2
        self.lo = max(0, seam.start - pad)
        hi = min(result.frames, seam.end + pad)
        self.take = frames_between(result, self.lo, hi)
        self.seam = Seam(seam.side, seam.start - self.lo, seam.end - self.lo, seam.shape, seam.coherence, seam.kind)


def _shift_t(m: Dict[str, Any], lo: int, rate: int) -> Dict[str, Any]:
    if "t_s" in m:
        m["t_s"] = round(m["t_s"] + lo / rate, 4)
    return m


def seams_entry(action: str, result: SpliceResult, rate: int, *, crossfade_ms: float,
                window: Optional[EngineWindow] = None) -> Dict[str, Any]:
    """The advisory `seams` entry of the take's `qa_report` and the three `mp.qa/1` seam numbers.
    Returns `{entry, qa_fields}`; the entry's status is `measured` (never pass/fail: decision 29).
    A seam within `LOCAL_WINDOW_MS` of the track's edge is measured on what there is."""
    measured: List[Dict[str, Any]] = []
    if action == REMOVE_REGION:
        fades = []
        for s in result.seams:
            loc = _Local(result, s, rate)
            fades.append(_shift_t(fade_metrics(loc.take, loc.seam, rate), loc.lo, rate))
        entry = {"status": "measured", "kind": "removed_region",
                 "reason": "a removed region's edges are fades to and from silence, not seams: the seam fields are "
                           "null and the fades are measured (SPEC §8); advisory, never a gate (decision 29)",
                 "removed_region": {"fade_ms": round(crossfade_ms, 2), "edges": fades,
                                    "seams": [s.as_dict(rate) for s in result.seams]},
                 "notes": list(result.notes)}
        return {"entry": entry, "qa_fields": {"seam_start_jump_db": None, "seam_end_jump_db": None,
                                              "seam_click_score": None}}
    for s in result.seams:
        loc = _Local(result, s, rate)
        measured.append({**s.as_dict(rate), **_shift_t(seam_metrics(loc.take, loc.seam, rate), loc.lo, rate)})
    by_side = {m["side"]: m for m in measured}
    clicks = [m["click_score"] for m in measured if m.get("click_score") is not None]
    qa_fields = {"seam_start_jump_db": (by_side.get("start") or {}).get("jump_db"),
                 "seam_end_jump_db": (by_side.get("end") or {}).get("jump_db"),
                 "seam_click_score": max(clicks) if clicks else None}
    entry = {"status": "measured", "kind": "crossfade",
             "reason": "where to listen: advisory pointers, never a gate (decision 29); the 1.5 dB jump and the "
                       "click score > 8 marks are display hints only",
             "seams": measured, "crossfade_ms": round(crossfade_ms, 2),
             "coherence_linear_min": COHERENCE_LINEAR_MIN,
             "display_hints": {"jump_db": 1.5, "click_score": 8.0},
             "notes": list(result.notes)}
    if window is not None:
        entry["engine_window"] = window.as_dict()
    return {"entry": entry, "qa_fields": qa_fields}
