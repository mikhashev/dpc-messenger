"""What the music service can do now: the `mp.capabilities/1` report (SPEC §7).

The player renders only what this report says, and `propose_job` answers `unsupported`
for whatever it does not mark `supported` (SPEC §5, §7). So the report must not claim an
engine that does not exist: the engine part comes from a pluggable probe, and the default
probe answers "engine adapter not built".

**The engine probe.** A callable returning `EngineStatus`. The acestep.cpp adapter (the
next step, backlog card DPC-DOES-NOT-START-OR-WATCH-THE-ACESTEP-CPP-SERVER-...) supplies
its own. DPC supervises that engine itself (Mike, 2026-11-20/21; SPEC decision 31 is
being rewritten), so an engine that is installed but stopped is still `available`: DPC
starts it when a job runs. The states:

- `not_built`: no adapter in DPC (today);
- `not_installed`: the adapter exists, but the binary or the weights are missing;
- `stopped`: installed, not running; DPC can start it;
- `starting`, `running`: DPC started it;
- `error`: the probe or the engine failed; `reason` says how.

`available` is true only for `stopped`, `starting` and `running`. Whether the GPU card is
free is not part of availability: a job on an available engine is queued and waits for
the card (SPEC decision 32).

**Two engines** (SPEC §12 decision 40, Mike #198): yue2.cpp, the default, and acestep.cpp.
The report keeps `engine` (the default engine's status, for an older reader) and adds
`engines` (each by its job-param word, `yue2` and `acestep`, with what its form needs:
backbones, adapters found, limits) and `default_engine`. `generate_from_text` is
`supported` when at least one engine is available; a proposal is checked against the
engine it names (`unsupported_reason(type, params)`), and its YuE2 backbone and adapters
against the files present (`check_params`). A single probe (`MusicCapabilities(probe)`)
answers for every engine: a stand-in for tests and start-up, never what the service builds.

**Statuses** are SPEC §7's `supported | planned | unsupported`. `planned` means on the
roadmap and not built in DPC; `propose_job` answers `unsupported` for it, as SPEC §7 says a
real backend does. `features.lyrics_*` and `features.transcribe` describe what the DPC
lyrics chain already does (`music/lyrics.py`), outside the job store.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional

from . import job_schemas as js

logger = logging.getLogger(__name__)

BACKEND_ID = "music-jobs"
ENGINE_ID = "acestep.cpp"
ENGINE_NAME = "acestep.cpp (ACE-Step 1.5, GGML)"
YUE2_ENGINE_ID = "yue2.cpp"
YUE2_ENGINE_NAME = "yue2.cpp (YuE2 3B, GGML)"

ENGINE_STATES = ("not_built", "not_installed", "stopped", "starting", "running", "error")
AVAILABLE_ENGINE_STATES = frozenset({"stopped", "starting", "running"})

SUPPORTED, PLANNED, UNSUPPORTED = "supported", "planned", "unsupported"

# The job types that are not built in DPC, with why (SPEC §7, §7.1; ADR-051 Implementation
# Status). `generate_from_text` is missing on purpose: its status follows the engine.
_NOT_BUILT_JOB_TYPES: Dict[str, Dict[str, str]] = {
    "edit": {"status": PLANNED, "reason": "region edits are not built in DPC (repaint, continuation, ffmpeg cut)"},
    "generate_similar": {"status": PLANNED, "reason": "same-style generation is not built in DPC"},
    "generate_from_lyrics": {"status": PLANNED, "reason": "song from a track's lyrics is not built in DPC"},
    "cover": {"status": PLANNED, "reason": "cover is not built in DPC (YuE2-Studio or ACE-Step cover)"},
    "transcribe": {"status": PLANNED, "reason": "transcribe runs as the lyrics command, not as a job yet"},
    "qc": {"status": PLANNED, "reason": "the QC layer is not built in DPC"},
    "generate_from_melody": {"status": PLANNED, "reason": "musician mode is not built in DPC"},
    "voice_clone": {"status": UNSUPPORTED, "reason": "no voice clone engine is chosen (research card, flow 26)"},
}

_EDIT_ACTIONS: Dict[str, Dict[str, str]] = {
    "note": {"status": SUPPORTED, "reason": "timecoded comments, mp.comment/1 (music/comments.py); the other four actions are planned"},
    "regenerate_region": {"status": PLANNED, "reason": "repaint is not built in DPC"},
    "extend_ending": {"status": PLANNED, "reason": "continuation is not built in DPC"},
    "fix_seam": {"status": PLANNED, "reason": "repaint around the seam is not built in DPC"},
    "remove_region": {"status": PLANNED, "reason": "ffmpeg fade to silence, length kept, not built in DPC"},
}

# SPEC §7 / §8: the canonical stage-1 list of mp.qa/1, and the advisory ones (decision 29).
QA_FIELDS = ("integrated_lufs", "true_peak_dbtp", "loudness_delta_lu", "seam_start_jump_db", "seam_end_jump_db",
             "seam_click_score", "clipped_samples", "lyrics_wer")
QA_ADVISORY = ("seam_start_jump_db", "seam_end_jump_db", "seam_click_score", "lyrics_wer")

MEASURED = "measured"
# What produces each field today. `measured` means a producer exists in DPC (loudness.py:
# integrated LUFS, LRA and true peak through ffmpeg ebur128); `planned` means nothing
# produces it yet, so a UI must not present it as a measurement.
QA_FIELD_STATUS: Dict[str, Dict[str, str]] = {
    "integrated_lufs": {"status": MEASURED, "reason": "loudness.py, ffmpeg ebur128"},
    "true_peak_dbtp": {"status": MEASURED, "reason": "loudness.py, ffmpeg ebur128"},
    "loudness_delta_lu": {"status": PLANNED, "reason": "nothing produces it yet"},
    "seam_start_jump_db": {"status": PLANNED, "reason": "seam analysis is not built in DPC"},
    "seam_end_jump_db": {"status": PLANNED, "reason": "seam analysis is not built in DPC"},
    "seam_click_score": {"status": PLANNED, "reason": "seam analysis is not built in DPC"},
    "clipped_samples": {"status": PLANNED, "reason": "nothing produces it yet"},
    "lyrics_wer": {"status": PLANNED, "reason": "needs the judge; not built in DPC"},
}

_FEATURES: Dict[str, Dict[str, str]] = {
    "audio_reference": {"status": PLANNED, "reason": "reference audio for a new song is not built in DPC"},
    "cover": {"status": PLANNED, "reason": "not built in DPC"},
    "auto_caption": {"status": PLANNED, "reason": "not built in DPC"},
    "transcribe": {"status": SUPPORTED, "reason": "DPC local Whisper through transcribe_music_lyrics; segment timestamps"},
    "lyrics_tags": {"status": SUPPORTED, "reason": "sidecar .lrc and file tags through ffprobe; ID3 SYLT is not read"},
    "lyrics_lrclib": {"status": SUPPORTED, "reason": "lrclib.net from core, on the person's explicit lookup"},
    "word_timings": {"status": PLANNED, "reason": "no forced aligner installed"},
    "qc": {"status": PLANNED, "reason": "stage-1 QA through ffmpeg and the judge are not built in DPC"},
}


@dataclass(frozen=True)
class EngineStatus:
    """One answer of an engine probe. `available` must agree with `state`."""

    state: str
    reason: str
    engine_id: str = ENGINE_ID
    name: str = ENGINE_NAME
    supervised: bool = False      # true when DPC starts and stops the engine
    version: Optional[str] = None
    models: Mapping[str, str] = field(default_factory=dict)
    key: Optional[str] = None     # the job-param word: "yue2", "acestep"

    def __post_init__(self) -> None:
        if self.state not in ENGINE_STATES:
            raise ValueError(f"engine state must be one of {ENGINE_STATES}, got {self.state!r}")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("an engine status needs a reason")

    @property
    def available(self) -> bool:
        return self.state in AVAILABLE_ENGINE_STATES

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.engine_id, "key": self.key, "name": self.name, "state": self.state,
            "available": self.available, "reason": self.reason, "supervised": self.supervised,
            "version": self.version, "models": dict(self.models),
        }


EngineProbe = Callable[[], EngineStatus]


def adapter_not_built_probe() -> EngineStatus:
    """The probe until the acestep.cpp adapter exists."""
    return EngineStatus(state="not_built", reason="engine adapter not built")


class MusicCapabilities:
    """The capability report, with the engine part read from the probes on every call
    (SPEC §7: re-read on connect and before each send).

    `engines` maps an engine word (`yue2`, `acestep`) to its supervisor, or to anything with
    a `status()`; a supervisor that has `form_info()` (`Yue2Server`) adds what the form
    needs (backbones present, adapters found). `engine_probe` alone answers for every
    engine (see the module docstring)."""

    def __init__(self, engine_probe: Optional[EngineProbe] = None, *,
                 engines: Optional[Mapping[str, Any]] = None,
                 default_engine: str = js.DEFAULT_FROM_TEXT_ENGINE):
        self._probe: EngineProbe = engine_probe or adapter_not_built_probe
        self._engines: Dict[str, Any] = dict(engines or {})
        self.default_engine = default_engine

    # -- probes ------------------------------------------------------------------

    @staticmethod
    def _safe(probe: Callable[[], Any], key: Optional[str]) -> EngineStatus:
        """A probe's answer; a probe that raises or answers the wrong type reports an
        `error` engine, never an available one."""
        try:
            status = probe()
        except Exception as exc:
            logger.warning("music: the engine probe failed", exc_info=True)
            return EngineStatus(state="error", reason=f"engine probe failed: {exc}", key=key)
        if not isinstance(status, EngineStatus):
            return EngineStatus(state="error", reason=f"engine probe answered {type(status).__name__}", key=key)
        return status

    def _probe_of(self, key: str) -> Callable[[], Any]:
        engine = self._engines.get(key)
        if engine is None:
            return self._probe
        return engine.status if hasattr(engine, "status") else engine

    def engine_status(self, key: Optional[str] = None) -> EngineStatus:
        """The status of one engine (the default when `key` is None)."""
        key = key or self.default_engine
        if self._engines and key not in self._engines:
            return EngineStatus(state="not_built", reason=f"no {key} engine in this DPC", key=key)
        return self._safe(self._probe_of(key), key)

    def engine_statuses(self) -> Dict[str, EngineStatus]:
        """Every engine's status; a single probe is read once for all of them."""
        if not self._engines:
            status = self._safe(self._probe, None)
            return {k: status for k in js.FROM_TEXT_ENGINES}
        return {k: self.engine_status(k) for k in js.FROM_TEXT_ENGINES}

    def _form_info(self, key: str) -> Dict[str, Any]:
        engine = self._engines.get(key)
        info = getattr(engine, "form_info", None)
        if not callable(info):
            return {}
        try:
            return dict(info())
        except Exception:
            logger.warning("music: reading the %s form info failed", key, exc_info=True)
            return {}

    # -- job types -------------------------------------------------------------------

    @staticmethod
    def _from_text_status(engine: EngineStatus) -> Dict[str, str]:
        if engine.available:
            return {"status": SUPPORTED, "reason": f"{engine.name}: {engine.reason}"}
        return {"status": UNSUPPORTED, "reason": engine.reason}

    def _from_text_any(self, statuses: Mapping[str, EngineStatus]) -> Dict[str, str]:
        """Supported when an engine is available (the default one first in the reason)."""
        if not self._engines:  # one probe answers for every engine: its own words
            return self._from_text_status(next(iter(statuses.values())))
        order = [self.default_engine] + [k for k in statuses if k != self.default_engine]
        for k in order:
            if k in statuses and statuses[k].available:
                return self._from_text_status(statuses[k])
        reasons = "; ".join(f"{k}: {statuses[k].reason}" for k in order if k in statuses)
        return {"status": UNSUPPORTED, "reason": reasons or "no music engine"}

    def job_type_status(self, job_type: str, engine: Optional[EngineStatus] = None,
                        params: Optional[Mapping[str, Any]] = None) -> Dict[str, str]:
        """`{status, reason}` of one job type; for `generate_from_text` with `params`, of
        the engine they name."""
        if job_type == js.GENERATE_FROM_TEXT:
            if engine is not None:
                return self._from_text_status(engine)
            if params is not None:
                return self._from_text_status(self.engine_status(js.engine_of(params)))
            return self._from_text_any(self.engine_statuses())
        if job_type in _NOT_BUILT_JOB_TYPES:
            return dict(_NOT_BUILT_JOB_TYPES[job_type])
        return {"status": UNSUPPORTED, "reason": f"{job_type!r} is not a job type"}

    def unsupported_reason(self, job_type: str, params: Optional[Mapping[str, Any]] = None) -> Optional[str]:
        """Why a job of this type (with these params) cannot run now, or None when it can
        be queued."""
        st = self.job_type_status(job_type, params=params)
        if st["status"] == SUPPORTED:
            return None
        if st["status"] == PLANNED:
            return f"planned, not built: {st['reason']}"
        return st["reason"]

    def check_params(self, job_type: str, params: Mapping[str, Any]) -> None:
        """Raise `job_schemas.ParamsError` when normalized `generate_from_text` params name a
        YuE2 backbone whose file is missing or an adapter the adapters folder does not hold.
        Without a supervisor that reports them (a test stand-in), or while the engine is not
        installed, nothing is checked."""
        if job_type != js.GENERATE_FROM_TEXT or js.engine_of(params) != js.ENGINE_YUE2:
            return
        if not self.engine_status(js.ENGINE_YUE2).available:
            return  # the job is `unsupported` with the engine's own reason
        info = self._form_info(js.ENGINE_YUE2)
        if not info:
            return
        backbones = info.get("backbones") or {}
        choice = params.get("backbone")
        if choice in backbones and not backbones[choice].get("present"):
            raise js.ParamsError("backbone", f"backbone {choice!r}: {backbones[choice].get('reason') or 'its file is missing'}")
        found = {a.get("name") for a in info.get("adapters") or []}
        for a in params.get("adapters") or []:
            if a.get("name") not in found:
                where = info.get("adapters_dir") or "the adapters folder"
                raise js.ParamsError("adapters", f"adapter {a.get('name')!r} is not in {where}"
                                                 + ("" if found else " (no adapters found there)"))

    # -- the report --------------------------------------------------------------------

    def _engine_limits(self, key: str) -> Dict[str, Any]:
        dmin, dmax, ddefault = js.DURATION_LIMITS[key]
        out: Dict[str, Any] = {"duration_s": {"min": dmin, "max": dmax, "default": ddefault}}
        if key == js.ENGINE_ACESTEP:
            out.update({"dit_model": js.FROM_TEXT_DIT_MODEL, "dit_quant": js.FROM_TEXT_DIT_QUANT,
                        "inference_steps": js.FROM_TEXT_INFERENCE_STEPS, "synth_batch": "variants in one request"})
        else:
            out.update({
                "duration_is_cap": True,
                "backbones": list(js.YUE2_BACKBONES), "backbone_default": js.YUE2_BACKBONE_DEFAULT,
                "cot": list(js.YUE2_COT_MODES), "cot_default": js.YUE2_COT_DEFAULT,
                "steps": js.YUE2_STEPS,
                "adapters_max": js.YUE2_ADAPTERS_MAX,
                "adapter_scale": {"min": js.YUE2_ADAPTER_SCALE_MIN, "max": js.YUE2_ADAPTER_SCALE_MAX,
                                  "music_default": js.YUE2_ADAPTER_MUSIC_DEFAULT,
                                  "sound_default": js.YUE2_ADAPTER_SOUND_DEFAULT},
                "synth_batch": "1 per variant, seed and lm_seed + variant index",
                "short_cap_estimate": {"seconds_per_line": js.YUE2_SECONDS_PER_LINE, "intro_s": js.YUE2_INTRO_S},
            })
        return out

    def report(self) -> Dict[str, Any]:
        """The `mp.capabilities/1` report."""
        statuses = self.engine_statuses()
        default = statuses.get(self.default_engine) or self.engine_status()
        from_text = self._from_text_any(statuses)
        features = copy.deepcopy(_FEATURES)
        features["text2music"] = dict(from_text)
        engines: Dict[str, Any] = {}
        for k, st in statuses.items():
            engines[k] = {**st.as_dict(), "key": k, "default": k == self.default_engine,
                          "limits": self._engine_limits(k), **self._form_info(k)}
        dmin, dmax, ddefault = js.DURATION_LIMITS[self.default_engine]
        job_types = {t: dict(from_text) if t == js.GENERATE_FROM_TEXT else self.job_type_status(t)
                     for t in js.JOB_TYPES}
        return {
            "schema": js.CAPABILITIES,
            "backend": {"id": BACKEND_ID, "connected": True, "simulated": False},
            "engine": default.as_dict(),
            "engines": engines,
            "default_engine": self.default_engine,
            "job_types": job_types,
            "actions": copy.deepcopy(_EDIT_ACTIONS),
            "limits": {
                "max_region_s": 30,
                "max_track_s": max(js.DURATION_MAX_S, js.YUE2_DURATION_MAX_S),
                "takes_per_job": 2,
                "generate_from_text": {
                    "variants": {"min": js.VARIANTS_MIN, "max": js.VARIANTS_MAX, "default": js.VARIANTS_DEFAULT},
                    "duration_s": {"min": dmin, "max": dmax, "default": ddefault},
                    "seed": {"min": js.SEED_MIN, "max": js.SEED_MAX, "null": "the runner draws one"},
                    "engine": self.default_engine,
                    "engine_choices": list(js.FROM_TEXT_ENGINES),
                    "engines": {k: self._engine_limits(k) for k in js.FROM_TEXT_ENGINES},
                    "engine_output": js.ENGINE_OUTPUT_FORMAT,
                    "library_format": js.LIBRARY_FORMAT,
                },
            },
            "qa": {"tool": "ffmpeg", "fields": list(QA_FIELDS),
                   "field_status": {k: dict(v) for k, v in QA_FIELD_STATUS.items()}, "advisory": list(QA_ADVISORY),
                   "status": PLANNED, "reason": "stage-1 QA through ffmpeg is not built in DPC"},
            "features": features,
            "take_decisions": {
                "actions": ["keep", "discard", "none_of_these"],
                "discard_needs_reason": True,
                "failure_classes": [{"id": c, "meaning": js.FAILURE_CLASS_MEANINGS[c]} for c in js.FAILURE_CLASSES],
                "max_reason_text": js.MAX_REASON_TEXT,
            },
        }
