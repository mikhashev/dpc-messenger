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
    "note": {"status": PLANNED, "reason": "comments are not built in DPC"},
    "regenerate_region": {"status": PLANNED, "reason": "repaint is not built in DPC"},
    "extend_ending": {"status": PLANNED, "reason": "continuation is not built in DPC"},
    "fix_seam": {"status": PLANNED, "reason": "repaint around the seam is not built in DPC"},
    "remove_region": {"status": PLANNED, "reason": "ffmpeg fade to silence, length kept, not built in DPC"},
}

# SPEC §7 / §8: the canonical stage-1 list of mp.qa/1, and the advisory ones (decision 29).
QA_FIELDS = ("integrated_lufs", "true_peak_dbtp", "loudness_delta_lu", "seam_start_jump_db", "seam_end_jump_db",
             "seam_click_score", "clipped_samples", "lyrics_wer")
QA_ADVISORY = ("seam_start_jump_db", "seam_end_jump_db", "seam_click_score", "lyrics_wer")

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
            "id": self.engine_id, "name": self.name, "state": self.state, "available": self.available,
            "reason": self.reason, "supervised": self.supervised, "version": self.version,
            "models": dict(self.models),
        }


EngineProbe = Callable[[], EngineStatus]


def adapter_not_built_probe() -> EngineStatus:
    """The probe until the acestep.cpp adapter exists."""
    return EngineStatus(state="not_built", reason="engine adapter not built")


class MusicCapabilities:
    """The capability report, with the engine part read from `engine_probe` on every call
    (SPEC §7: re-read on connect and before each send)."""

    def __init__(self, engine_probe: Optional[EngineProbe] = None):
        self._probe: EngineProbe = engine_probe or adapter_not_built_probe

    def engine_status(self) -> EngineStatus:
        """The probe's answer; a probe that raises or answers the wrong type reports an
        `error` engine, never an available one."""
        try:
            status = self._probe()
        except Exception as exc:
            logger.warning("music: the engine probe failed", exc_info=True)
            return EngineStatus(state="error", reason=f"engine probe failed: {exc}")
        if not isinstance(status, EngineStatus):
            return EngineStatus(state="error", reason=f"engine probe answered {type(status).__name__}")
        return status

    @staticmethod
    def _from_text_status(engine: EngineStatus) -> Dict[str, str]:
        if engine.available:
            return {"status": SUPPORTED, "reason": f"{engine.name}: {engine.reason}"}
        return {"status": UNSUPPORTED, "reason": engine.reason}

    def job_type_status(self, job_type: str, engine: Optional[EngineStatus] = None) -> Dict[str, str]:
        """`{status, reason}` of one job type."""
        if job_type == js.GENERATE_FROM_TEXT:
            return self._from_text_status(engine or self.engine_status())
        if job_type in _NOT_BUILT_JOB_TYPES:
            return dict(_NOT_BUILT_JOB_TYPES[job_type])
        return {"status": UNSUPPORTED, "reason": f"{job_type!r} is not a job type"}

    def unsupported_reason(self, job_type: str) -> Optional[str]:
        """Why a job of this type cannot run now, or None when it can be queued."""
        st = self.job_type_status(job_type)
        if st["status"] == SUPPORTED:
            return None
        if st["status"] == PLANNED:
            return f"planned, not built: {st['reason']}"
        return st["reason"]

    def report(self) -> Dict[str, Any]:
        """The `mp.capabilities/1` report."""
        engine = self.engine_status()
        from_text = self._from_text_status(engine)
        features = copy.deepcopy(_FEATURES)
        features["text2music"] = dict(from_text)
        return {
            "schema": js.CAPABILITIES,
            "backend": {"id": BACKEND_ID, "connected": True, "simulated": False},
            "engine": engine.as_dict(),
            "job_types": {t: self.job_type_status(t, engine) for t in js.JOB_TYPES},
            "actions": copy.deepcopy(_EDIT_ACTIONS),
            "limits": {
                "max_region_s": 30,
                "max_track_s": js.DURATION_MAX_S,
                "takes_per_job": 2,
                "generate_from_text": {
                    "variants": {"min": js.VARIANTS_MIN, "max": js.VARIANTS_MAX, "default": js.VARIANTS_DEFAULT},
                    "duration_s": {"min": js.DURATION_MIN_S, "max": js.DURATION_MAX_S, "default": js.DURATION_DEFAULT_S},
                    "seed": {"min": js.SEED_MIN, "max": js.SEED_MAX, "null": "the runner draws one"},
                    "engine": js.FROM_TEXT_ENGINE,
                    "dit_model": js.FROM_TEXT_DIT_MODEL,
                    "dit_quant": js.FROM_TEXT_DIT_QUANT,
                    "inference_steps": js.FROM_TEXT_INFERENCE_STEPS,
                    "engine_output": js.ENGINE_OUTPUT_FORMAT,
                    "library_format": js.LIBRARY_FORMAT,
                },
            },
            "qa": {"tool": "ffmpeg", "fields": list(QA_FIELDS), "advisory": list(QA_ADVISORY),
                   "status": PLANNED, "reason": "stage-1 QA through ffmpeg is not built in DPC"},
            "features": features,
            "take_decisions": {
                "actions": ["keep", "discard", "none_of_these"],
                "discard_needs_reason": True,
                "failure_classes": [{"id": c, "meaning": js.FAILURE_CLASS_MEANINGS[c]} for c in js.FAILURE_CLASSES],
                "max_reason_text": js.MAX_REASON_TEXT,
            },
        }
