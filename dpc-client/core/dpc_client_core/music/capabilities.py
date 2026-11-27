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

**Engine choice** (Mike, 2026-11-24, group #75/#76: option (c), built general). Before a
proposal is checked, `choose_engine` lets a rule pick the engine and adapters from what the
job is, and the store records why on the job (`engine_choice`, `job_schemas`). One rule
today, `instrumental`: an Instrumental job (lyrics null) on YuE2 gets the instrumental adapter
(`Yue2Server.purpose_adapters`) with acestep.cpp as its fallback, or goes to acestep.cpp when
that adapter is not offered. The report's `engine_choice_rules` says what each rule would
choose now, so the form shows it before the job is sent.

**Statuses** are SPEC §7's `supported | planned | unsupported`. `planned` means on the
roadmap and not built in DPC; `propose_job` answers `unsupported` for it, as SPEC §7 says a
real backend does. `features.lyrics_*` and `features.transcribe` describe what the DPC
lyrics chain already does (`music/lyrics.py`), outside the job store.
"""

from __future__ import annotations

import copy
import logging
import shutil
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from . import job_schemas as js
from . import region_edit as re_
from . import web_sources
from .qa import THRESHOLDS as QA_THRESHOLDS

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
# Status). `generate_from_text` and the song types (`job_schemas.SONG_TYPES`) are missing on purpose:
# their status follows the engines (`song_type_status`).
_NOT_BUILT_JOB_TYPES: Dict[str, Dict[str, str]] = {
    "transcribe": {"status": PLANNED, "reason": "transcribe runs as the lyrics command, not as a job yet"},
    "qc": {"status": PLANNED, "reason": "QC is folded into the take (mp.qc/1, qc.py, judge_music_take); no qc child job"},
    "generate_from_melody": {"status": PLANNED, "reason": "musician mode is not built in DPC"},
    "voice_clone": {"status": UNSUPPORTED, "reason": "no voice clone engine is chosen (research card, flow 26)"},
}

# The edit actions whose status does not follow an engine (`action_statuses` adds the three built ones).
_FIXED_EDIT_ACTIONS: Dict[str, Dict[str, str]] = {
    "note": {"status": SUPPORTED, "reason": "timecoded comments, mp.comment/1 (music/comments.py); a note makes no job"},
    "extend_ending": {"status": PLANNED, "reason": "continuation is not built in DPC (whether acestep.cpp continues a "
                                                   "track is not verified)"},
}
_REPAINT_WORDS = ("acestep.cpp repaint (POST /synth, task_type repaint) of the region, spliced back into the track "
                  "with a crossfade, length kept; built, to be checked live on a real track (decision 28)")
_REMOVE_WORDS = "the region faded to silence in DPC, length kept (decision 23); no model, no card"

# The song job types (music/similar.py): what each engine does for them, in the words the report gives.
ENGINE_WORDS = {"acestep": "acestep.cpp", "yue2": "yue2.cpp"}
SONG_WORDS = {
    ("generate_similar", "acestep"): "acestep.cpp text2music (/lm, then /synth): the caption with the kept attributes; "
                                     "kept BPM, key and time signature go to the LM as metadata",
    ("generate_similar", "yue2"): "yue2.cpp (the default same-style engine): the describer writes the source's style "
                                  "tags when asked, /tokenize reads the source's codes, then /synth opens on a "
                                  "section of them (the first 30 s by default) and writes on in the style; the "
                                  "section is cut off each take",
    ("generate_from_lyrics", "acestep"): "acestep.cpp text2music with the track's lyrics (/lm, then /synth)",
    ("generate_from_lyrics", "yue2"): "yue2.cpp text-to-song with the track's lyrics (one /synth per variant)",
    ("cover", "acestep"): "acestep.cpp task cover: the source's audio and the target caption in one /synth, no LM; "
                          "the take is as long as the source",
    ("cover", "yue2"): "yue2.cpp (the default cover engine): SheetSage2 /transcribe makes the source's ABC score "
                       "(levelled first when too loud), then /synth sings it with the lyrics matched to its sections, "
                       "in the target style agreed with the score's tempo and key, as long as the source (Ruach "
                       "Studio's cover chain)",
}
# What a yue2 cover's refusal offers instead (Mike 2026-11-26: acestep.cpp cover is the fallback).
COVER_FALLBACK_WORDS = "a cover on acestep.cpp runs without it and needs no lyrics (engine acestep)"
AUDIO_REFERENCE_WORDS = ("; the source's audio goes as acestep.cpp's ref_audio, a timbre reference (whether it steers "
                         "the style beyond timbre is not verified)")
# What a yue2 same-style refusal offers instead (Mike 2026-11-26: acestep.cpp text2music is the second choice).
SAME_STYLE_FALLBACK_WORDS = "same style on acestep.cpp runs without it (engine acestep)"
SAME_STYLE_UNVERIFIED_WORDS = ("; whether the source's sound carries into the continuation is not verified (its codes "
                               "re-rendered come out in the model's own voice)")

# SPEC §7 / §8: the canonical stage-1 list of mp.qa/1, and the advisory ones (decision 29).
QA_FIELDS = ("integrated_lufs", "true_peak_dbtp", "loudness_delta_lu", "seam_start_jump_db", "seam_end_jump_db",
             "seam_click_score", "clipped_samples", "lyrics_wer")
QA_ADVISORY = ("seam_start_jump_db", "seam_end_jump_db", "seam_click_score", "lyrics_wer")

MEASURED = "measured"
# What produces each field today. `measured` means a producer exists in DPC (loudness.py:
# integrated LUFS, LRA and true peak through ffmpeg ebur128; qa.py: clipped samples through
# ffmpeg astats, in one stage-1 pass on every take the runner makes); `planned` means nothing
# produces it yet, so a UI must not present it as a measurement.
QA_FIELD_STATUS: Dict[str, Dict[str, str]] = {
    "integrated_lufs": {"status": MEASURED, "reason": "loudness.py, ffmpeg ebur128; copied into qa by qa.py"},
    "true_peak_dbtp": {"status": MEASURED, "reason": "loudness.py, ffmpeg ebur128; copied into qa by qa.py"},
    "loudness_delta_lu": {"status": PLANNED, "reason": "nothing produces it yet"},
    "seam_start_jump_db": {"status": MEASURED, "reason": "region_edit.py on region-edit takes (RMS 20 ms before vs after "
                           "the crossfade); null on a new take and a removed region; advisory, never a gate"},
    "seam_end_jump_db": {"status": MEASURED, "reason": "region_edit.py on region-edit takes; null on a new take and a "
                         "removed region; advisory, never a gate"},
    "seam_click_score": {"status": MEASURED, "reason": "region_edit.py on region-edit takes (largest step near the seam "
                         "over the local median step); null on a new take and a removed region; advisory, never a gate"},
    "clipped_samples": {"status": MEASURED, "reason": "qa.py, ffmpeg astats: samples at the peak when the peak is "
                        "0.999 of full scale or more (a lower bound)"},
    "lyrics_wer": {"status": MEASURED, "reason": "qc.py, in mp.qc/1 only (deterministic.lyrics_wer and lyrics_wer), from a stored unedited transcription of the take against the job's lyrics; advisory, never a gate or a rank; take.qa keeps null"},
}

_FEATURES: Dict[str, Dict[str, str]] = {
    "auto_caption": {"status": PLANNED, "reason": "not built in DPC"},
    "transcribe": {"status": SUPPORTED, "reason": "DPC local Whisper through transcribe_music_lyrics; segment timestamps"},
    "lyrics_tags": {"status": SUPPORTED, "reason": "sidecar .lrc and file tags through ffprobe; ID3 SYLT is not read"},
    "lyrics_lrclib": {"status": SUPPORTED, "reason": "lrclib.net from core, on the person's explicit lookup"},
    "word_timings": {"status": SUPPORTED, "reason": "forced line alignment from Whisper word timestamps "
                                                   "(music/lyrics_align.py, align_music_lyrics)"},
    "qc": {"status": SUPPORTED, "reason": "mp.qc/1 on every take with stage 1 (qc.py, take.qc); the stage-2 audio judge runs only on judge_music_take when [music] qc_judge_alias names an audio llama-server alias; ranking by scores.overall is off by default ([music] qc_ranking, decision 26)"},
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
    # A start by hand waiting for the card (ADR-052 section 3.2): `{text, behind, wait_s, known,
    # upper_bound}`; None when nothing waits. `as_dict` always carries the key, so the UI's merge of
    # a later event clears a finished wait.
    card_wait: Optional[Mapping[str, Any]] = None

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
            "card_wait": dict(self.card_wait) if self.card_wait else None,
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
                 default_engine: str = js.DEFAULT_FROM_TEXT_ENGINE,
                 ffmpeg_probe: Optional[Callable[[], bool]] = None,
                 describer_probe: Optional[Callable[[], Mapping[str, Any]]] = None):
        self._probe: EngineProbe = engine_probe or adapter_not_built_probe
        # The describer of a same-style song (`describe.py`): `{status, alias, reason}`; none without a probe.
        self._describer_probe = describer_probe
        self._engines: Dict[str, Any] = dict(engines or {})
        self.default_engine = default_engine
        # Every edit decodes the track with ffmpeg (the control, region_edit.py).
        self._ffmpeg_probe = ffmpeg_probe or (lambda: bool(shutil.which("ffmpeg")))

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

    def _ffmpeg(self) -> bool:
        try:
            return bool(self._ffmpeg_probe())
        except Exception:
            return False

    def action_statuses(self, acestep: Optional[EngineStatus] = None) -> Dict[str, Dict[str, str]]:
        """`{status, reason}` of each edit action (SPEC §7 `actions`): the repaint pair follows
        acestep.cpp, the removal needs ffmpeg only, and every built edit needs ffmpeg for the control."""
        out = copy.deepcopy(_FIXED_EDIT_ACTIONS)
        ffmpeg = self._ffmpeg()
        no_ffmpeg = "ffmpeg is not on PATH; an edit decodes the track with it (install ffmpeg)"
        ace = acestep or self.engine_status(js.ENGINE_ACESTEP)
        for action in re_.REPAINT_ACTIONS:
            if not ffmpeg:
                out[action] = {"status": UNSUPPORTED, "reason": no_ffmpeg}
            elif ace.available:
                out[action] = {"status": SUPPORTED, "reason": _REPAINT_WORDS}
            else:
                out[action] = {"status": UNSUPPORTED, "reason": f"needs acestep.cpp: {ace.reason}"}
        out[re_.REMOVE_REGION] = ({"status": SUPPORTED, "reason": _REMOVE_WORDS} if ffmpeg
                                  else {"status": UNSUPPORTED, "reason": no_ffmpeg})
        return {a: out[a] for a in js.EDIT_ACTIONS}

    def _edit_status(self, action: Optional[str], actions: Optional[Mapping[str, Dict[str, str]]] = None) -> Dict[str, str]:
        actions = actions or self.action_statuses()
        if action is not None:
            if action == "note":
                return {"status": UNSUPPORTED, "reason": "a note is a comment; it makes no job"}
            return dict(actions.get(action) or {"status": UNSUPPORTED, "reason": f"{action!r} is not an edit action"})
        built = [a for a in re_.BUILT_ACTIONS if actions[a]["status"] == SUPPORTED]
        if built:
            return {"status": SUPPORTED, "reason": f"region edits: {', '.join(built)} (see actions)"}
        return {"status": UNSUPPORTED, "reason": "; ".join(f"{a}: {actions[a]['reason']}" for a in re_.BUILT_ACTIONS)}

    # -- song jobs from a source track (music/similar.py) ---------------------------------------

    def _yue2_transcriber_problem(self) -> Optional[str]:
        """Why yue2.cpp cannot transcribe a source to a score (`--transcriber`), or None. A stand-in
        engine that does not say (a single test probe) cannot."""
        engine = self._engines.get(js.ENGINE_YUE2)
        ask = getattr(engine, "transcriber_problem", None)
        if not callable(ask):
            return "this yue2.cpp supervisor does not report the SheetSage2 transcriber"
        try:
            return ask()
        except Exception as exc:
            return f"checking the SheetSage2 transcriber failed: {exc}"

    def _yue2_tokenizer_problem(self) -> Optional[str]:
        """Why yue2.cpp cannot read a source's codes (`--tokenizer`), or None. A stand-in engine that does not say
        (a single test probe) cannot."""
        engine = self._engines.get(js.ENGINE_YUE2)
        ask = getattr(engine, "tokenizer_problem", None)
        if not callable(ask):
            return "this yue2.cpp supervisor does not report the audio tokenizer"
        try:
            return ask()
        except Exception as exc:
            return f"checking the audio tokenizer failed: {exc}"

    def describer_status(self) -> Dict[str, Any]:
        """`{status, alias, reason}` of the describer (`describe.py`): `supported` when an audio alias is set up. It
        is advice, so a same-style song runs without it, from what the person types."""
        if self._describer_probe is None:
            return {"status": UNSUPPORTED, "alias": None, "reason": "no describer is set up in this DPC"}
        try:
            st = dict(self._describer_probe())
        except Exception as exc:
            return {"status": UNSUPPORTED, "alias": None, "reason": f"checking the describer failed: {exc}"}
        return {"status": st.get("status") if st.get("status") in (SUPPORTED, UNSUPPORTED) else UNSUPPORTED,
                "alias": st.get("alias"), "reason": str(st.get("reason") or "")}

    def song_engine_status(self, job_type: str, key: str, *, needs_audio: bool,
                           statuses: Optional[Mapping[str, EngineStatus]] = None) -> Dict[str, str]:
        """`{status, reason}` of one song job type on one engine (`job_schemas.SONG_ENGINES`)."""
        engine = (statuses or {}).get(key) or self.engine_status(key)
        same_style_yue2 = job_type == js.GENERATE_SIMILAR and key == js.ENGINE_YUE2
        needs_audio = needs_audio or same_style_yue2       # it always reads the source's codes
        if not engine.available:
            return {"status": UNSUPPORTED, "reason": f"{ENGINE_WORDS.get(key, key)}: {engine.reason}"}
        if needs_audio and not self._ffmpeg():
            return {"status": UNSUPPORTED, "reason": "ffmpeg is not on PATH; the source is decoded with it for the "
                                                     "engine (install ffmpeg)"}
        if job_type == js.COVER and key == js.ENGINE_YUE2:
            problem = self._yue2_transcriber_problem()
            if problem:
                return {"status": UNSUPPORTED, "reason": f"yue2.cpp cover: {problem}; {COVER_FALLBACK_WORDS}"}
        if same_style_yue2:
            problem = self._yue2_tokenizer_problem()
            if problem:
                return {"status": UNSUPPORTED, "reason": f"yue2.cpp same style: {problem}; {SAME_STYLE_FALLBACK_WORDS}"}
            d = self.describer_status()
            told = (f"; describer {d['alias']}" if d["status"] == SUPPORTED else
                    f"; no describer ({d['reason']}): the style comes from what is typed")
            return {"status": SUPPORTED, "reason": SONG_WORDS[(job_type, key)] + told + SAME_STYLE_UNVERIFIED_WORDS,
                    "verified": False}
        return {"status": SUPPORTED, "reason": SONG_WORDS[(job_type, key)]
                + (AUDIO_REFERENCE_WORDS if needs_audio and job_type != js.COVER else "")}

    def song_type_status(self, job_type: str, params: Optional[Mapping[str, Any]] = None,
                         statuses: Optional[Mapping[str, EngineStatus]] = None) -> Dict[str, str]:
        """`{status, reason}` of a song job type: with `params`, on the engine they name (and its
        reference); without, supported when one of its engines is."""
        keys = [params["engine"]] if params and params.get("engine") in js.SONG_ENGINES[job_type] \
            else list(js.SONG_ENGINES[job_type])
        needs_audio = job_type == js.COVER or bool(params and params.get("reference") == "audio")
        reasons = []
        for key in keys:
            st = self.song_engine_status(job_type, key, needs_audio=needs_audio, statuses=statuses)
            if st["status"] == SUPPORTED:
                return st
            reasons.append(st["reason"])
        return {"status": UNSUPPORTED, "reason": "; ".join(reasons)}

    def song_features(self, statuses: Optional[Mapping[str, EngineStatus]] = None) -> Dict[str, Dict[str, Any]]:
        """`features.audio_reference` (a same-style song with the source as acestep.cpp's timbre
        reference), `features.cover`, per engine for the cover (`cover_engines`, the default and the fallback
        engine with why), `features.same_style` in the same shape (`same_style_engines`: yue2 needs the audio
        tokenizer, and says whether a describer is set up), `features.describe` (the describer, `describe.py`)
        and `features.web_source_audio` (planned: the streaming integration)."""
        ref = self.song_engine_status(js.GENERATE_SIMILAR, js.ENGINE_ACESTEP, needs_audio=True, statuses=statuses)
        cover = {k: self.song_engine_status(js.COVER, k, needs_audio=True, statuses=statuses)
                 for k in js.SONG_ENGINES[js.COVER]}
        any_cover = next((v for v in cover.values() if v["status"] == SUPPORTED), None)
        default = js.SONG_DEFAULT_ENGINE[js.COVER]
        fallback = None
        if cover[default]["status"] != SUPPORTED:
            fallback = next((k for k in js.SONG_ENGINES[js.COVER] if k != default
                             and cover[k]["status"] == SUPPORTED), None)
        same = {k: self.song_engine_status(js.GENERATE_SIMILAR, k, needs_audio=False, statuses=statuses)
                for k in js.SONG_ENGINES[js.GENERATE_SIMILAR]}
        any_same = next((v for v in same.values() if v["status"] == SUPPORTED), None)
        same_default = js.SONG_DEFAULT_ENGINE[js.GENERATE_SIMILAR]
        same_fallback = None
        if same[same_default]["status"] != SUPPORTED:
            same_fallback = next((k for k in js.SONG_ENGINES[js.GENERATE_SIMILAR] if k != same_default
                                  and same[k]["status"] == SUPPORTED), None)
        return {
            "audio_reference": ref if ref["status"] != SUPPORTED else {
                "status": SUPPORTED, "reason": AUDIO_REFERENCE_WORDS.lstrip("; "), "verified": False},
            "cover": {**(any_cover or {"status": UNSUPPORTED,
                                       "reason": "; ".join(v["reason"] for v in cover.values())}),
                      "cover_engines": cover, "default_engine": default,
                      "fallback_engine": fallback,
                      "fallback_reason": cover[default]["reason"] if fallback else None},
            # Same style per engine (Mike 2026-11-26: yue2 by default, acestep the second), the same shape as cover.
            "same_style": {**(any_same or {"status": UNSUPPORTED,
                                           "reason": "; ".join(v["reason"] for v in same.values())}),
                           "same_style_engines": same, "default_engine": same_default,
                           "fallback_engine": same_fallback,
                           "fallback_reason": same[same_default]["reason"] if same_fallback else None,
                           "section_s": {"min": js.SIMILAR_SECTION_MIN_S, "max": js.SIMILAR_SECTION_MAX_S,
                                         "default": js.SIMILAR_SECTION_DEFAULT_S},
                           "max_total_s": js.SIMILAR_YUE2_MAX_TOTAL_S,
                           "keep_from_source": list(js.KEEP_FROM_SOURCE_KEYS)},
            # The describer (`describe.py`): it describes the source, never ranks a take.
            "describe": self.describer_status(),
            # A web track's audio (a cover, an audio reference): the streaming integration (music/web_sources.py).
            "web_source_audio": web_sources.integration_status(),
        }

    def job_type_status(self, job_type: str, engine: Optional[EngineStatus] = None,
                        params: Optional[Mapping[str, Any]] = None, action: Optional[str] = None) -> Dict[str, str]:
        """`{status, reason}` of one job type; for `generate_from_text` with `params`, of
        the engine they name; for `edit` with `action`, of that action."""
        if job_type == "edit":
            return self._edit_status(action)
        if job_type == js.GENERATE_FROM_TEXT:
            if engine is not None:
                return self._from_text_status(engine)
            if params is not None:
                return self._from_text_status(self.engine_status(js.engine_of(params)))
            return self._from_text_any(self.engine_statuses())
        if job_type in js.SONG_TYPES:
            return self.song_type_status(job_type, params)
        if job_type in _NOT_BUILT_JOB_TYPES:
            return dict(_NOT_BUILT_JOB_TYPES[job_type])
        return {"status": UNSUPPORTED, "reason": f"{job_type!r} is not a job type"}

    def unsupported_reason(self, job_type: str, params: Optional[Mapping[str, Any]] = None,
                           action: Optional[str] = None) -> Optional[str]:
        """Why a job of this type (with these params, or this edit action) cannot run now, or None
        when it can be queued."""
        st = self.job_type_status(job_type, params=params, action=action)
        if st["status"] == SUPPORTED:
            return None
        if st["status"] == PLANNED:
            return f"planned, not built: {st['reason']}"
        return st["reason"]

    def choose_engine(self, job_type: str, params: Mapping[str, Any], *, at: str,
                      yue2_status: Optional[EngineStatus] = None) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        """`(params, engine_choice)` for normalized params: what a rule chooses from what the job
        is (module docstring, "engine choice"), or the params unchanged and None when no rule
        applies. Nothing is chosen while YuE2 is not available (the job is `unsupported` with its
        reason) or without a supervisor that reports its purpose adapters (a test stand-in).
        `ParamsError` when the instrumental adapter would be a fifth adapter. `yue2_status` is
        YuE2's status when the caller has read it already (the report reads each probe once)."""
        out = copy.deepcopy(dict(params))
        if job_type != js.GENERATE_FROM_TEXT or js.engine_of(out) != js.ENGINE_YUE2 or out.get("lyrics") is not None:
            return out, None
        if not (yue2_status or self.engine_status(js.ENGINE_YUE2)).available:
            return out, None
        purpose = (self._form_info(js.ENGINE_YUE2).get("purpose_adapters") or {}).get("instrumental")
        if not isinstance(purpose, Mapping):
            return out, None
        name = purpose.get("adapter")
        if purpose.get("offered") and name:
            adapters = list(out.get("adapters") or [])
            if not any(a.get("name") == name for a in adapters):
                if len(adapters) >= js.YUE2_ADAPTERS_MAX:
                    raise js.ParamsError("adapters", f"an Instrumental job on YuE2 runs with the instrumental adapter "
                                                     f"{name}, and {js.YUE2_ADAPTERS_MAX} adapters are already named")
                scale = purpose.get("scale", 1.0)
                adapters.append({"name": name, "music": scale, "sound": scale})
            out["adapters"] = adapters
            return out, {"rule": "instrumental", "engine": js.ENGINE_YUE2, "adapters": [name],
                         "fallback": js.ENGINE_ACESTEP, "at": at, "fell_back": None,
                         "reason": f"Instrumental: YuE2 with the instrumental adapter {name}; "
                                   "acestep.cpp if the adapter does not load"}
        # No path in the reason: it goes into the take's provenance record.
        why = {"off": "is turned off", "unusable": f"{name} cannot be used"}.get(
            str(purpose.get("state")), f"{name} is not installed" if name else "is not installed")
        dropped = [a.get("name") for a in out.get("adapters") or []]
        moved = js.fallback_params_from_text(out, js.ENGINE_ACESTEP)
        reason = f"Instrumental: acestep.cpp, because the YuE2 instrumental adapter {why}"
        if dropped:
            reason += f"; adapter(s) {', '.join(dropped)} left out with YuE2"
        return moved, {"rule": "instrumental", "engine": js.ENGINE_ACESTEP, "adapters": [], "fallback": None,
                       "at": at, "fell_back": None, "reason": reason}

    def engine_choice_rules(self, yue2_status: Optional[EngineStatus] = None) -> Dict[str, Optional[Dict[str, Any]]]:
        """What each rule would choose now (`choose_engine` on a bare YuE2 job of that kind), for
        the form: `{instrumental: {rule, engine, adapters, fallback, reason} or None}`."""
        try:
            _params, choice = self.choose_engine(
                js.GENERATE_FROM_TEXT, js.normalize_params_from_text({"lyrics": None, "style": "-"}), at="",
                yue2_status=yue2_status)
        except js.ParamsError:
            choice = None
        if choice is not None:
            choice = {k: choice[k] for k in ("rule", "engine", "adapters", "fallback", "reason")}
        return {"instrumental": choice}

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
        features.update(self.song_features(statuses))
        engines: Dict[str, Any] = {}
        for k, st in statuses.items():
            engines[k] = {**st.as_dict(), "key": k, "default": k == self.default_engine,
                          "limits": self._engine_limits(k), **self._form_info(k)}
        dmin, dmax, ddefault = js.DURATION_LIMITS[self.default_engine]
        actions = self.action_statuses(statuses.get(js.ENGINE_ACESTEP))
        job_types = {t: dict(from_text) if t == js.GENERATE_FROM_TEXT
                     else self._edit_status(None, actions) if t == "edit"
                     else self.song_type_status(t, statuses=statuses) if t in js.SONG_TYPES else self.job_type_status(t)
                     for t in js.JOB_TYPES}
        return {
            "schema": js.CAPABILITIES,
            "backend": {"id": BACKEND_ID, "connected": True, "simulated": False},
            "engine": default.as_dict(),
            "engines": engines,
            "default_engine": self.default_engine,
            "engine_choice_rules": self.engine_choice_rules(statuses.get(js.ENGINE_YUE2)),
            "job_types": job_types,
            "actions": actions,
            "limits": {
                "max_region_s": re_.MAX_REGION_S,
                "max_track_s": max(js.DURATION_MAX_S, js.YUE2_DURATION_MAX_S),
                "takes_per_job": re_.TAKES_PER_JOB,
                "edit": {
                    "actions": list(re_.BUILT_ACTIONS),
                    "takes": {a: re_.takes_for(a) for a in re_.BUILT_ACTIONS},
                    "max_region_s": re_.MAX_REGION_S, "max_allowed_s": re_.MAX_ALLOWED_S,
                    "fix_seam_window_s": re_.FIX_SEAM_WINDOW_S,
                    "crossfade_ms": {"min": re_.CROSSFADE_MS_MIN, "max": re_.CROSSFADE_MS_MAX,
                                     "default": re_.CROSSFADE_MS_DEFAULT, "chosen": False},
                    "coherence_linear_min": re_.COHERENCE_LINEAR_MIN,
                    "cut_with_shift_near_end_s": re_.NEAR_END_S,
                    "engine_grid_hz": re_.GRID_HZ,
                    "zones": "region = intent; allowed_zone (what the engine may rework, null = region; fix_seam: at "
                             f"least {re_.FIX_SEAM_WINDOW_S:g} s); splice_bounds (where the take joins, null = allowed)",
                },
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
                "song": {
                    "engines": {t: list(js.SONG_ENGINES[t]) for t in js.SONG_TYPES},
                    "default_engine": dict(js.SONG_DEFAULT_ENGINE),
                    "lyrics_modes": {t: list(js.SONG_LYRICS_MODES[t]) for t in js.SONG_TYPES},
                    "relations": dict(js.SONG_RELATIONS),
                    "keep": list(js.KEEP_KEYS),
                    "variants": {"min": js.VARIANTS_MIN, "max": js.VARIANTS_MAX, "default": js.VARIANTS_DEFAULT},
                    "duration_s": {k: {"min": v[0], "max": v[1]} for k, v in js.DURATION_LIMITS.items()},
                    "cover_strength": {"min": 0.0, "max": 1.0, "default": js.COVER_STRENGTH_DEFAULT},
                    "cover_length": "the source's length on both engines: acestep.cpp takes it from the source, "
                                    "yue2.cpp is asked for it (duration and semantic min_tokens); QA mode same",
                    "cover_source_s": {js.ENGINE_ACESTEP: {"min": js.ACESTEP_COVER_MIN_S, "max": js.DURATION_MAX_S},
                                       js.ENGINE_YUE2: {"min": js.YUE2_COVER_MIN_S, "max": js.yue2_cover_door_max_s(0),
                                                        "note": "with no text; the context is shared with the score "
                                                                "and the lyrics, so a long text shortens it"}},
                    "reference_audio_max_s": js.REFERENCE_AUDIO_MAX_S,
                    "cover_cot_default": js.COVER_COT_DEFAULT,
                },
            },
            "qa": {"tool": "ffmpeg", "fields": list(QA_FIELDS),
                   "field_status": {k: dict(v) for k, v in QA_FIELD_STATUS.items()}, "advisory": list(QA_ADVISORY),
                   "status": SUPPORTED,
                   "reason": "stage 1 is built for takes the runner makes (qa.py): one ffmpeg pass fills qa (mp.qa/1) and "
                             "qa_report; the seam fields are measured on region-edit takes (advisory); loudness delta is planned "
                             "and null; lyrics WER is not in take.qa (null there) but is measured in mp.qc/1 (qc.py), "
                             "advisory; QA flags a take and never rejects it",
                   "gates": {"names": ["length", "clipping", "true_peak", "silence"], "thresholds": dict(QA_THRESHOLDS),
                             "advisory_only": ["seams", "lyrics_wer"]}},
            "features": features,
            "take_decisions": {
                "actions": ["keep", "discard", "none_of_these"],
                "discard_needs_reason": True,
                "failure_classes": [{"id": c, "meaning": js.FAILURE_CLASS_MEANINGS[c]} for c in js.FAILURE_CLASSES],
                "max_reason_text": js.MAX_REASON_TEXT,
            },
        }
