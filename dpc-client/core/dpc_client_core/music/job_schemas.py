"""The shapes of music jobs and takes, checked in code (no schema library).

The schemas are owned by the music player SPEC (`ai-studio/experiments/music-player/
SPEC.md` §4, §5): `mp.job/1` and `mp.take/1`. This module checks the subset the core
writes: required keys, no undeclared keys, types, enums, id patterns, the conditional
rules (`allOf`) for the job types the store creates, and the job state machine of §5.

**The state machine** (SPEC §5)::

    queued ──► running ──► review ──► accepted
       │          │          └──────► rejected
       ├──────────┴─► cancelled   (by the person)
       │          └─► failed      (reason required)
       └─► unsupported            (reason required; terminal)

A job never returns to an earlier state. Waiting for the GPU card is not a state: the job
stays `queued` with `state_reason` "waiting for the card: X holds it" (SPEC §5, §12
decision 32).

**`generate_from_text` engines** (SPEC §12 decision 40, Mike #198 2026-11-21: YuE2 is the
default engine). `params.engine` is `"yue2"` (the default) or `"acestep"`. Records written
before 2026-11-21 carry the constant `"ace-step-1.5:text2music"` (`LEGACY_ACESTEP_ENGINE`),
which still loads and means acestep; a record with no `engine` at all is read as acestep
too, since every job before YuE2 ran there. The stored value is kept verbatim (the store
compares a record with its normalized params), so read the engine with `engine_of`.

**Limits per engine.** Both: variants 1-4 (default 2); seed `[0, UINT32_MAX]` (acestep.cpp's
`src/request.h`) or null (the runner draws one); the engine answers wav24, which DPC
converts to FLAC.

- acestep (SPEC §12 decisions 33-34): duration 10-600 s (default 120), ACE-Step turbo Q8,
  8 steps.
- yue2 [P]: `duration_s` is a cap, not a length: the model ends the song on its own, and a
  30 s cap gave an intro without vocals in the trial, so the default is 180 s, from 30 to
  360 (the engine's own ceiling). `backbone` `stock` (default) or `rs-tw`; `cot` `full`
  (default), `melody` or `off`; `adapters` `[{name, music, sound}]`, at most 4, each
  strength 0-2, default music 0.45 / sound 0.35 (Ruach's voice README); `{name, scale}` is
  short for both halves at `scale`. yue2.cpp takes one strength per adapter file, so DPC
  splits a two-half adapter into a music (AR) and a sound (NAR) file and stacks both
  (`music/yue2.py`). Whether a named adapter exists
  is the capabilities' check at propose time (`MusicCapabilities.check_params`), since it
  depends on the files present. Batch 1 per variant (the CUDA flash-attention mask bug at
  batch > 1 in ggml ef4555b7): variant i runs alone with seed and lm_seed `seed + i`.
- `estimate_yue2_seconds` is the short-cap warning's estimate (the form shows it): about
  `YUE2_SECONDS_PER_LINE` per sung line plus `YUE2_INTRO_S`. An estimate, never a refusal.

Keys that are not in the SPEC yet are declared here as extensions, all optional so a
record without them is valid against both shapes:

- `mp.job/1`: `idempotency_key` (SPEC §6 `propose_job` takes one; the job did not
  record it); `payloads`, the request bodies written for the engine, each
  `{name, sha256, size_bytes, written_at, endpoint}` (SPEC §3 "show and store the
  payload", PROVENANCE payload rule); `card_wait`, `{holders: [{alias, engine}], since}`
  while a queued job waits for the card (a running one too since ADR-052 S4: its engine child
  ended and its next start asks for the card again), so the job card can offer "Stop X" by alias.
- `mp.job/1`: `engine_choice`, null or `{rule, engine, adapters, fallback, reason, at,
  fell_back}`: the engine and adapters a rule chose for the job from what the job is, and
  why (Mike, 2026-11-24, group #75/#76: the per-job choice is built general; `instrumental`
  is its one rule today). `engine` is the engine the params name; `adapters` the adapter
  names the rule put into `params.adapters` (or found there); `fallback` the engine the job
  moves to when they do not load (`fallback_params_from_text`), null when there is none
  left; `fell_back`, null or `{from, adapters, why, at}`, set when the job did move.
  `reason` is the sentence the job card shows. No paths in it: it is copied into the take's
  provenance record.
- `mp.job/1`: `requested_by`, `{kind: "person"|"agent", agent_id?, conversation_id?, turn_id?}`:
  who proposed the job (music tools, part 1, 2026-11-24). A job from the UI carries
  `{kind: "person"}`; one an agent proposed carries `kind` "agent" and its `agent_id` (required
  for an agent), and the chat and turn it came from when known. The queue limits, the agent's
  own-only cancel and the cancel of a stopped turn's jobs read it. A record written before the
  field has none and is read as the person's (`requested_by_of`). It is provenance of the
  request, not a credential: nothing here proves who called, the door that calls the store does.
- `mp.take/1`: `seed`, `synth_batch_size`, `synth_batch_index` (a take is reproducible
  from codes, seed, params, batch size and batch index on the same build: PROVENANCE
  §2, measured 2026-11-21) and `payload` (the name of the payload whose bytes produced
  it); `duration_requested_s` (SPEC §4: requested and received duration are two
  fields; received is `file.duration_s`); `library_track_id` (the library entry
  `register_generated` made for the file); `reason_text`, `reason_class`, `decided_at`
  (Discard needs a reason, Mike 2026-11-21: free text, a failure class, or both).
- `mp.take/1.qa` is null when stage-1 QA could not fill every required `mp.qa/1` field
  (ffmpeg missing, a silent file with no true peak, a measurement that failed: `qa.py`), and
  on takes written before it existed. When it is set it is exactly `mp.qa/1` (`check_qa`):
  every field present, no extras. A take is never given invented numbers to make it valid.
- `mp.take/1`: `qa_report`, an object or null: the stage-1 gates beside `qa`
  (`check_qa_report`, `qa.py`). `mp.qa/1` has no place for a pass/fail, a duration or a silence
  length (`additionalProperties: false`), so they live here: `{stage: 1, verdict, gates:
  {length, clipping, true_peak, silence}, advisory: {seams, lyrics_wer}, measurements,
  thresholds, measured_by, measured_at}`. A gate is `{status: pass|fail|not_measured, detail,
  ...}` and the verdict is `failed` when a gate failed, `passed` when all four passed and
  `not_measured` otherwise (a gate that could not be measured is never a pass). The advisory
  entries never gate (decision 29). A report always exists on a take made by the runner; a
  failed QA run is a report whose verdict is `not_measured` with the reason.
- `mp.take/1.qc`: the take's `mp.qc/1` (SPEC §8.1), or absent on a take written before it
  (`check_qc`, `qc.py`). DPC's record keeps the SPEC's keys and declares these extensions:
  `judge` carries `status` (`absent`, `done` or `failed`), `alias`, `error` and `judged_at`
  beside `model`, `runtime` and `rubric`, and `model`/`runtime` are null while no judge ran;
  `scores` and `confidence` are null unless the judge is `done`; `stage1` is the stage-1
  summary (`qa.qa_summary`); `lyrics_wer` the advisory WER entry (`status` measured or
  not_measured, never a gate); `caveat` the line the UI shows; `assembled_at`. QC never accepts
  or rejects a take: no key here is a decision.
- `mp.take/1`: `loudness`, `{lufs_i, lra, true_peak_dbtp, measured_by, measured_at}` or
  null: the ffmpeg ebur128 measurement the level-matched A/B needs (decision 37,
  `loudness.py`). It is not `qa`, because a loudness-only record is not a valid `mp.qa/1`
  (every field required, no extras). Advisory, never a gate (decision 29).
- `mp.take/1`: `engine` (the engine id that made it, `yue2.cpp` or `acestep.cpp`) and
  `lm_seed` (the seed of the token draw, which yue2.cpp takes apart from the noise seed),
  both optional, so takes written before 2026-11-21 still load.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .schemas import SchemaError

JOB = "mp.job/1"
TAKE = "mp.take/1"
CAPABILITIES = "mp.capabilities/1"

JOB_ID_RE = re.compile(r"^job_[0-9a-f]{16}$")
TAKE_ID_RE = re.compile(r"^tak_[0-9a-f]{16}$")

JOB_TYPES = (
    "edit", "generate_similar", "cover", "generate_from_lyrics", "transcribe", "qc",
    "generate_from_melody", "voice_clone", "generate_from_text",
)
GENERATE_FROM_TEXT = "generate_from_text"
# Job types whose results are new-track takes chosen by the person's A/B (SPEC §5).
NEW_TRACK_TYPES = ("generate_similar", "generate_from_lyrics", "cover", "generate_from_melody",
                   "voice_clone", "generate_from_text")

QUEUED, RUNNING, REVIEW = "queued", "running", "review"
ACCEPTED, REJECTED, FAILED, CANCELLED, UNSUPPORTED = "accepted", "rejected", "failed", "cancelled", "unsupported"
JOB_STATES = (QUEUED, RUNNING, REVIEW, ACCEPTED, REJECTED, FAILED, CANCELLED, UNSUPPORTED)
TERMINAL_STATES = frozenset({ACCEPTED, REJECTED, FAILED, CANCELLED, UNSUPPORTED})
TRANSITIONS: Dict[str, frozenset] = {
    QUEUED: frozenset({RUNNING, CANCELLED, UNSUPPORTED}),
    RUNNING: frozenset({REVIEW, CANCELLED, FAILED}),
    REVIEW: frozenset({ACCEPTED, REJECTED}),
    ACCEPTED: frozenset(), REJECTED: frozenset(), FAILED: frozenset(),
    CANCELLED: frozenset(), UNSUPPORTED: frozenset(),
}
REASON_REQUIRED = frozenset({FAILED, UNSUPPORTED})

EDIT_ACTIONS = ("note", "regenerate_region", "extend_ending", "fix_seam", "remove_region")

TAKE_KINDS = ("edit", "new_track")
PENDING = "pending"
TAKE_DECISIONS = (PENDING, ACCEPTED, REJECTED)

# Failure classes of a Discard (EVOLUTION §6, Ark; Mike 2026-11-21: the Discard reason may
# be picked from this list). The meanings are CC's reading, still to confirm with Ark.
FAILURE_CLASSES = ("prep", "sel", "struct", "opt", "transport", "listen")
FAILURE_CLASS_MEANINGS = {
    "prep": "the prepared input was wrong (lyrics or caption preparation)",
    "sel": "a bad setting was selected",
    "struct": "a structural defect of the audio (length, seam, silence, wrong language)",
    "opt": "a property of the model or engine itself, after settings are ruled out",
    "transport": "damage in conversion, tags or the hash chain",
    "listen": "the listening instrument (player, level, a start that was not loaded)",
}
MAX_REASON_TEXT = 2000

# generate_from_text engines (SPEC §12 decision 40).
ENGINE_YUE2, ENGINE_ACESTEP = "yue2", "acestep"
FROM_TEXT_ENGINES = (ENGINE_YUE2, ENGINE_ACESTEP)
DEFAULT_FROM_TEXT_ENGINE = ENGINE_YUE2             # Mike #198, 2026-11-21
LEGACY_ACESTEP_ENGINE = "ace-step-1.5:text2music"  # the one value before 2026-11-21
FROM_TEXT_ENGINE = LEGACY_ACESTEP_ENGINE           # the old name, kept for callers
ENGINE_IDS = {ENGINE_YUE2: "yue2.cpp", ENGINE_ACESTEP: "acestep.cpp"}

# Both engines.
VARIANTS_MIN, VARIANTS_MAX, VARIANTS_DEFAULT = 1, 4, 2
SEED_MIN, SEED_MAX = 0, 2 ** 32 - 1
ENGINE_OUTPUT_FORMAT = "wav24"
LIBRARY_FORMAT = "flac"
SYNTH_BATCH_SIZE_MAX = 16  # a sanity bound on a recorded value, not an engine limit
LM_SEED_MAX = 2 ** 63 - 1  # yue2.cpp's int64 seeds; acestep.cpp's lm_seed is within it

# acestep.cpp (SPEC §12 decisions 33-34).
DURATION_MIN_S, DURATION_MAX_S, DURATION_DEFAULT_S = 10, 600, 120
FROM_TEXT_DIT_MODEL = "turbo"
FROM_TEXT_DIT_QUANT = "Q8"
FROM_TEXT_INFERENCE_STEPS = 8

# yue2.cpp [P] (decision 40): the duration is a cap.
YUE2_DURATION_MIN_S, YUE2_DURATION_MAX_S, YUE2_DURATION_DEFAULT_S = 30, 360, 180
YUE2_BACKBONES = ("stock", "rs-tw")
YUE2_BACKBONE_DEFAULT = "stock"
YUE2_COT_MODES = ("full", "melody", "off")
YUE2_COT_DEFAULT = "full"
YUE2_STEPS = 32
YUE2_PEAK_CLIP = 10
YUE2_ADAPTERS_MAX = 4
# The lyrics and the style of a text job together, in UTF-8 bytes. Both go into the engine request
# that is written to disk before it is sent (`jobs.MAX_PAYLOAD_BYTES`, 8 MiB); JSON turns a control
# character into six bytes, so a text under this bound always fits (6 x 1 MiB is under 8 MiB). A real
# song is a few thousand characters: the door refuses an absurd one before a job exists.
MAX_SONG_TEXT_BYTES = 1024 * 1024
YUE2_ADAPTER_SCALE_MIN, YUE2_ADAPTER_SCALE_MAX = 0.0, 2.0
# Per half: Ruach's voice README begins near music 0.45 / sound 0.35, and with a planned
# score keeps the music half at 0.5 or less.
YUE2_ADAPTER_MUSIC_DEFAULT = 0.45
YUE2_ADAPTER_SOUND_DEFAULT = 0.35
YUE2_ADAPTER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,127}$")
# The short-cap estimate (the trial: six sung lines needed about 70 s; a 30 s cap was all
# intro). An estimate for a warning, never a refusal.
YUE2_SECONDS_PER_LINE = 10.0
YUE2_INTRO_S = 20.0

DURATION_LIMITS = {
    ENGINE_ACESTEP: (DURATION_MIN_S, DURATION_MAX_S, DURATION_DEFAULT_S),
    ENGINE_YUE2: (YUE2_DURATION_MIN_S, YUE2_DURATION_MAX_S, YUE2_DURATION_DEFAULT_S),
}

_PARAMS_FROM_TEXT_KEYS = ("lyrics", "style", "variants", "duration_s", "seed", "lyrics_prep", "engine",
                          "backbone", "cot", "adapters")
_YUE2_ONLY_KEYS = ("backbone", "cot", "adapters")

_JOB_REQUIRED = (
    "schema", "id", "type", "track_id", "base_version_id", "comment_id", "action", "region",
    "params", "state", "created_at",
)
_JOB_KEYS = set(_JOB_REQUIRED) | {
    "prompt", "state_reason", "progress", "take_ids", "result_text", "result", "backend",
    "history", "updated_at",
    # extensions, see the module docstring
    "idempotency_key", "payloads", "card_wait", "engine_choice", "requested_by",
}
REQUESTER_PERSON, REQUESTER_AGENT = "person", "agent"
REQUESTER_KINDS = (REQUESTER_PERSON, REQUESTER_AGENT)
_REQUESTED_BY_KEYS = {"kind", "agent_id", "conversation_id", "turn_id"}
MAX_REQUESTER_ID_LEN = 200
ENGINE_CHOICE_RULES = ("instrumental",)
_ENGINE_CHOICE_KEYS = {"rule", "engine", "adapters", "fallback", "reason", "at", "fell_back"}
_FELL_BACK_KEYS = {"from", "adapters", "why", "at"}
_PAYLOAD_KEYS = {"name", "sha256", "size_bytes", "written_at", "endpoint"}
PAYLOAD_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

_TAKE_REQUIRED = ("schema", "id", "kind", "job_id", "track_id", "base_version_id", "region", "file", "qa", "decision")
_TAKE_KEYS = set(_TAKE_REQUIRED) | {
    "variant", "source_track_id", "title", "qc", "simulated", "created_at",
    # extensions, see the module docstring
    "seed", "synth_batch_size", "synth_batch_index", "payload", "duration_requested_s",
    "library_track_id", "reason_text", "reason_class", "decided_at", "loudness",
    "engine", "lm_seed", "engine_window_s", "length_check", "length_delta_s", "qa_report",
}
_LOUDNESS_KEYS = {"lufs_i", "lra", "true_peak_dbtp", "measured_by", "measured_at"}

# mp.qa/1 (SPEC §8), exactly: every key required, no others.
QA_KEYS = ("integrated_lufs", "true_peak_dbtp", "loudness_delta_lu", "seam_start_jump_db", "seam_end_jump_db",
           "seam_click_score", "clipped_samples", "lyrics_wer", "measured_by")
_QA_OPTIONAL = ("simulated",)
# The stage-1 gates (SPEC §8 "What gates a take", decision 29) and the advisory entries.
QA_GATES = ("length", "clipping", "true_peak", "silence")
QA_ADVISORY_ENTRIES = ("seams", "lyrics_wer")
GATE_PASS, GATE_FAIL, GATE_NOT_MEASURED = "pass", "fail", "not_measured"
GATE_STATUSES = (GATE_PASS, GATE_FAIL, GATE_NOT_MEASURED)
QA_PASSED, QA_FAILED, QA_NOT_MEASURED = "passed", "failed", "not_measured"
QA_VERDICTS = (QA_PASSED, QA_FAILED, QA_NOT_MEASURED)
_QA_REPORT_KEYS = {"stage", "verdict", "gates", "advisory", "measurements", "thresholds", "measured_by", "measured_at",
                   "note", "unmeasurable"}
# `qa_report.unmeasurable` (follow-up 16, Zcode #188 finding 6): written by `backfill_music_qa` when a measurement
# still ends `not_measured`: `{reason, attempts, last_attempt_at, file_hash}`. A later backfill skips such a take
# unless `force` is set or the file's hash differs from `file_hash`.
_QA_UNMEASURABLE_KEYS = {"reason", "attempts", "last_attempt_at", "file_hash"}
_FILE_KEYS = {"uri", "content_hash", "sample_rate", "channels", "duration_s"}

# mp.qc/1 (SPEC §8.1) as DPC writes it; the extensions are in the module docstring.
QC = "mp.qc/1"
QC_FLOWS = ("edit", "same_style", "cover", "lyrics", "melody", "voice_clone")
QC_SCORE_KEYS = ("style_match", "mood", "vocal_clarity", "artifacts", "structure", "overall")
JUDGE_ABSENT, JUDGE_DONE, JUDGE_FAILED = "absent", "done", "failed"
JUDGE_STATUSES = (JUDGE_ABSENT, JUDGE_DONE, JUDGE_FAILED)
_QC_REQUIRED = ("schema", "take_id", "flow", "deterministic", "judge", "scores", "issues", "reasons", "confidence")
_QC_KEYS = set(_QC_REQUIRED) | {"simulated", "stage1", "lyrics_wer", "caveat", "assembled_at", "last_attempt"}
_QC_ATTEMPT_KEYS = {"status", "error", "at", "alias"}
_QC_JUDGE_KEYS = {"status", "model", "runtime", "rubric", "alias", "error", "judged_at"}
_QC_ISSUE_KEYS = {"t_start", "t_end", "kind", "note", "expected_pronunciation"}
_QC_WER_KEYS = {"status", "value", "raw", "reason", "advisory", "transcription"}


class ParamsError(ValueError):
    """The params of a job request break a rule; `field` names the param."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field
        self.message = message


def _fail(schema: str, message: str) -> None:
    raise SchemaError(f"{schema}: {message}")


def _expect(schema: str, ok: bool, message: str) -> None:
    if not ok:
        _fail(schema, message)


def _is_str(value: Any) -> bool:
    return isinstance(value, str)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def check_requested_by(value: Any) -> dict:
    """`requested_by` as a clean dict, or `ParamsError` (field `requested_by`): `kind` is
    `person` or `agent`; an agent has a non-empty `agent_id`; `conversation_id` and `turn_id`
    are optional non-empty strings (null counts as absent); nothing else is declared."""
    if not isinstance(value, Mapping):
        raise ParamsError("requested_by", "requested_by must be an object")
    extra = sorted(set(value) - _REQUESTED_BY_KEYS)
    if extra:
        raise ParamsError("requested_by", f"requested_by has undeclared key(s): {', '.join(extra)}")
    kind = value.get("kind")
    if kind not in REQUESTER_KINDS:
        raise ParamsError("requested_by", f"requested_by.kind must be one of {REQUESTER_KINDS}")
    out: Dict[str, Any] = {"kind": kind}
    for key in ("agent_id", "conversation_id", "turn_id"):
        item = value.get(key)
        if item is None:
            continue
        if not _is_str(item) or not item.strip() or len(item) > MAX_REQUESTER_ID_LEN:
            raise ParamsError("requested_by", f"requested_by.{key} must be a non-empty string "
                                              f"of at most {MAX_REQUESTER_ID_LEN} characters")
        out[key] = item
    if kind == REQUESTER_AGENT and "agent_id" not in out:
        raise ParamsError("requested_by", "requested_by.agent_id is required when kind is agent")
    if kind == REQUESTER_PERSON and len(out) > 1:
        raise ParamsError("requested_by", "a person's requested_by carries only kind")
    return out


def requested_by_of(job: Mapping[str, Any]) -> dict:
    """Who asked for `job`: its `requested_by`, or the person for a record written before the
    field existed."""
    rb = job.get("requested_by")
    return dict(rb) if isinstance(rb, Mapping) else {"kind": REQUESTER_PERSON}


def _check_keys(schema: str, record: Any, required: Iterable[str], allowed: set) -> None:
    if not isinstance(record, Mapping):
        _fail(schema, f"expected an object, got {type(record).__name__}")
    missing = [k for k in required if k not in record]
    if missing:
        _fail(schema, f"missing required key(s): {', '.join(missing)}")
    extra = sorted(set(record) - allowed)
    if extra:
        _fail(schema, f"undeclared key(s): {', '.join(extra)}")
    if record.get("schema") != schema:
        _fail(schema, f"'schema' must be {schema!r}, got {record.get('schema')!r}")


def _check_range(schema: str, value: Any, where: str) -> None:
    _expect(schema, isinstance(value, Mapping), f"'{where}' must be an object")
    extra = sorted(set(value) - {"t_start", "t_end"})
    _expect(schema, not extra, f"'{where}' has undeclared key(s): {', '.join(extra)}")
    for k in ("t_start", "t_end"):
        _expect(schema, k in value and _is_number(value[k]) and value[k] >= 0, f"'{where}.{k}' must be a number >= 0")


# -- params ------------------------------------------------------------------------


def engine_of(params: Any) -> str:
    """The engine word (`yue2` or `acestep`) of a `generate_from_text` job's params. The
    legacy constant and a missing `engine` (a record from before YuE2) are acestep."""
    raw = params.get("engine") if isinstance(params, Mapping) else None
    if raw is None or raw == LEGACY_ACESTEP_ENGINE:
        return ENGINE_ACESTEP
    return raw if raw in FROM_TEXT_ENGINES else ENGINE_ACESTEP


def estimate_yue2_seconds(lyrics: Optional[str]) -> float:
    """About how long YuE2 needs to sing these lyrics: `YUE2_INTRO_S` plus
    `YUE2_SECONDS_PER_LINE` per sung line (section tags such as [verse] and blank lines do
    not count). An instrumental (None) is the intro alone: that is the short-cap warning's figure (an
    instrumental gets no warning); the queue's ETA for an instrumental counts the nominal length
    instead (`music_expected_s`). An estimate, never a refusal."""
    if not lyrics:
        return YUE2_INTRO_S
    lines = [ln.strip() for ln in lyrics.splitlines()]
    sung = [ln for ln in lines if ln and not (ln.startswith("[") and ln.endswith("]"))]
    return YUE2_INTRO_S + YUE2_SECONDS_PER_LINE * len(sung)


# YuE2's wall time for a song: a fixed overhead plus the audio divided by a rate (ADR-052 L10; slice S4
# records it): `yue2_wall_s = YUE2_OVERHEAD_S + audio_s / YUE2_AUDIO_S_PER_WALL_S`. The job's wall time is
# not proportional to the song: it has a fixed cost (model load, prompt, codec) that dominates a short song,
# so the old "audio seconds per wall second" factor that fell with length (3.5 at 30 s to 3.0 at 240 s, from
# the trial card) had the wrong shape: the measured factor RISES with length.
# Five live points, job wall seconds (queued -> running -> review) for the song's audio seconds:
#   30 s -> 16.48, 120 s -> 33.73, 162.7 s -> 43.11 (2026-11-24 ADR-052 live run, measurements section 4d)
#   30 s -> 18 (instrumental, 2026-11-25 04:25:52 -> 04:26:10), 60 s -> 21 (instrumental, 04:29:42 -> 04:30:03)
# Least-squares line through them: 10.76 s + audio / 5.13 (slope 0.195 wall s per audio s). Residuals
# (measured minus fit, s): -0.13, -0.43, +0.62, +1.39, -1.46: all within 1.5 s. A start value, tuned from
# operation (Mike #11). An instrumental counts its duration cap as the audio (`music_expected_s`), so what
# is shown for YuE2 is an upper bound ("up to"), not a promise.
YUE2_OVERHEAD_S = 10.76
YUE2_AUDIO_S_PER_WALL_S = 5.13


def yue2_wall_s(audio_s: float) -> float:
    """Wall seconds YuE2 takes for `audio_s` seconds of song: `YUE2_OVERHEAD_S` plus the audio seconds
    divided by `YUE2_AUDIO_S_PER_WALL_S` (audio seconds per wall second, so above 1 is faster than real
    time)."""
    return YUE2_OVERHEAD_S + audio_s / YUE2_AUDIO_S_PER_WALL_S


def music_expected_s(params: Any) -> Optional[float]:
    """How long a `generate_from_text` job that has not started will hold the card, in wall seconds,
    an estimate, or None for unknown (ADR-052 L10, "a job that has not started"): YuE2's text estimate
    per variant (`estimate_yue2_seconds`, never more than the duration cap, which the song cannot
    outrun), as wall time by `yue2_wall_s` (a fixed overhead plus the audio over a rate), times the variants.
    An instrumental (no lyrics) has no lines to count: its song is the nominal length, the cap the job
    carries (`duration_s`, the form's value, `YUE2_DURATION_DEFAULT_S` = 180 s when absent; the
    instrumental adapter's README says "3-5 minutes"), not the intro alone (Mike, 2026-11-24, group #33).
    It routes and informs, never refuses. acestep.cpp has no measured factor: unknown."""
    if not isinstance(params, Mapping) or engine_of(params) != ENGINE_YUE2:
        return None
    try:
        variants = int(params.get("variants") or VARIANTS_DEFAULT)
        cap = float(params.get("duration_s") or YUE2_DURATION_DEFAULT_S)
    except (TypeError, ValueError):
        return None
    lyrics = params.get("lyrics")
    if isinstance(lyrics, list):   # a song job's lines (`normalize_params_song`)
        lyrics = "\n".join(str(x) for x in lyrics)
    audio_s = min(estimate_yue2_seconds(lyrics), cap) if lyrics else cap
    return yue2_wall_s(audio_s) * variants


def _normalize_adapters(raw: Any) -> list:
    if not isinstance(raw, list):
        raise ParamsError("adapters", "adapters must be a list of {name, music, sound}")
    if len(raw) > YUE2_ADAPTERS_MAX:
        raise ParamsError("adapters", f"at most {YUE2_ADAPTERS_MAX} adapters, got {len(raw)}")
    out, seen = [], set()
    for i, item in enumerate(raw):
        if not isinstance(item, Mapping) or not set(item) <= {"name", "music", "sound", "scale"} \
                or "name" not in item or ("scale" in item and ({"music", "sound"} & set(item))):
            raise ParamsError("adapters", f"adapters[{i}] must be {{name, music, sound}} or {{name, scale}}")
        name = item["name"]
        if not _is_str(name) or not YUE2_ADAPTER_NAME_RE.match(name) or name.strip(".") == "":
            raise ParamsError("adapters", f"adapters[{i}].name {name!r} is not an adapter name (a file or folder "
                                          "name in the adapters folder, no path)")
        if name in seen:
            raise ParamsError("adapters", f"adapter {name!r} is named twice")
        seen.add(name)
        if "scale" in item:
            halves = {"music": item["scale"], "sound": item["scale"]}
        else:
            halves = {"music": item.get("music", YUE2_ADAPTER_MUSIC_DEFAULT),
                      "sound": item.get("sound", YUE2_ADAPTER_SOUND_DEFAULT)}
        for half, value in halves.items():
            if not _is_number(value) or not YUE2_ADAPTER_SCALE_MIN <= value <= YUE2_ADAPTER_SCALE_MAX:
                raise ParamsError("adapters", f"adapters[{i}].{half} must be a number from "
                                              f"{YUE2_ADAPTER_SCALE_MIN:g} to {YUE2_ADAPTER_SCALE_MAX:g}, got {value!r}")
        out.append({"name": name, "music": halves["music"], "sound": halves["sound"]})
    return out


def normalize_params_from_text(params: Any, *, missing_engine: str = DEFAULT_FROM_TEXT_ENGINE) -> Dict[str, Any]:
    """The params of a `generate_from_text` job with the defaults filled in, or
    `ParamsError` naming the first param that breaks a rule.

    Defaults: `engine` `yue2` (Mike #198; `missing_engine` is what a missing `engine`
    means: the store reads old records with `acestep`), `variants` 2, `duration_s` the
    engine's (yue2 180, acestep 120), `seed` null (the runner draws one), `lyrics_prep` {}
    (the generate dialog fills it); for yue2 also `backbone` `stock`, `cot` `full`,
    `adapters` []. The yue2 keys are refused on an acestep job. `lyrics` and `style` must
    be given. The lyrics are kept verbatim: nothing is stripped or normalised, so U+0301
    stress marks and the person's line breaks stay. The engine value is kept as given, so
    the legacy constant stays the legacy constant."""
    if not isinstance(params, Mapping):
        raise ParamsError("params", "params must be an object")
    extra = sorted(set(params) - set(_PARAMS_FROM_TEXT_KEYS))
    if extra:
        raise ParamsError(extra[0], f"unknown param(s) for generate_from_text: {', '.join(extra)}")
    out: Dict[str, Any] = {}

    engine = params.get("engine", missing_engine)
    if engine not in FROM_TEXT_ENGINES and engine != LEGACY_ACESTEP_ENGINE:
        raise ParamsError("engine", f"engine must be one of {', '.join(FROM_TEXT_ENGINES)}, got {engine!r}")
    key = ENGINE_ACESTEP if engine == LEGACY_ACESTEP_ENGINE else engine

    if "lyrics" not in params:
        raise ParamsError("lyrics", "lyrics is required: the song's text, or null for an instrumental")
    lyrics = params["lyrics"]
    if lyrics is not None:
        if not _is_str(lyrics):
            raise ParamsError("lyrics", "lyrics must be text, or null for an instrumental")
        if not lyrics.strip():
            raise ParamsError("lyrics", "lyrics are empty; send null for an instrumental")
    out["lyrics"] = lyrics

    style = params.get("style")
    if not _is_str(style) or not style.strip():
        raise ParamsError("style", "style is required: a non-empty description of the sound")
    out["style"] = style
    try:
        text_bytes = len((lyrics or "").encode("utf-8")) + len(style.encode("utf-8"))
    except UnicodeEncodeError:
        raise ParamsError("lyrics", "the lyrics or the style hold a character that is not valid text (a lone surrogate)")
    if text_bytes > MAX_SONG_TEXT_BYTES:
        raise ParamsError("lyrics", f"the lyrics and style are {text_bytes} bytes together; the limit is "
                                    f"{MAX_SONG_TEXT_BYTES} bytes (about a megabyte of text): shorten them")

    variants = params.get("variants", VARIANTS_DEFAULT)
    if not _is_int(variants) or not VARIANTS_MIN <= variants <= VARIANTS_MAX:
        raise ParamsError("variants", f"variants must be a whole number from {VARIANTS_MIN} to {VARIANTS_MAX}, "
                                      f"got {variants!r}")
    out["variants"] = variants

    dmin, dmax, ddefault = DURATION_LIMITS[key]
    duration = params.get("duration_s", ddefault)
    if not _is_number(duration) or not dmin <= duration <= dmax:
        what = "a cap in seconds" if key == ENGINE_YUE2 else "a number of seconds"
        raise ParamsError("duration_s", f"duration_s must be {what} from {dmin} to {dmax} for {key}, "
                                        f"got {duration!r}")
    out["duration_s"] = duration

    seed = params.get("seed")
    if seed is not None and (not _is_int(seed) or not SEED_MIN <= seed <= SEED_MAX):
        raise ParamsError("seed", f"seed must be a whole number from {SEED_MIN} to {SEED_MAX}, or null to draw one, "
                                  f"got {seed!r}")
    out["seed"] = seed

    lyrics_prep = params.get("lyrics_prep", {})
    if not isinstance(lyrics_prep, Mapping):
        raise ParamsError("lyrics_prep", "lyrics_prep must be an object")
    try:
        lyrics_prep = json.loads(json.dumps(lyrics_prep, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ParamsError("lyrics_prep", f"lyrics_prep must be plain JSON: {exc}")
    out["lyrics_prep"] = lyrics_prep
    out["engine"] = engine

    if key == ENGINE_ACESTEP:
        given = [k for k in _YUE2_ONLY_KEYS if k in params]
        if given:
            raise ParamsError(given[0], f"{given[0]} is a yue2 param; acestep takes none of "
                                        f"{', '.join(_YUE2_ONLY_KEYS)}")
        return out

    backbone = params.get("backbone", YUE2_BACKBONE_DEFAULT)
    if backbone not in YUE2_BACKBONES:
        raise ParamsError("backbone", f"backbone must be one of {', '.join(YUE2_BACKBONES)}, got {backbone!r}")
    out["backbone"] = backbone
    cot = params.get("cot", YUE2_COT_DEFAULT)
    if cot not in YUE2_COT_MODES:
        raise ParamsError("cot", f"cot must be one of {', '.join(YUE2_COT_MODES)}, got {cot!r}")
    out["cot"] = cot
    out["adapters"] = _normalize_adapters(params.get("adapters", []))
    # The context is shared with the frames the cap lets run: the same budget a song job gets
    # (`normalize_params_song`), on the text as the body will carry it. The run checks it again.
    # An instrumental (lyrics null) runs with the adapter's tag as its lyrics (`runner.build_yue2_body`): the door
    # counts the same tag, so a job at the ceiling is not refused only at the run (follow-up 23, Zcode #214).
    why = yue2_text_overflow(style, lyrics if lyrics is not None else YUE2_INSTRUMENTAL_LYRICS, duration)
    if why is not None:
        raise SongParamsError("context_exceeded", "lyrics", why)
    return out


def fallback_params_from_text(params: Mapping[str, Any], engine: str) -> Dict[str, Any]:
    """Normalized params of a `generate_from_text` job moved to `engine` (an engine choice's
    fallback): the text, style, variants, seed and lyrics preparation kept, the engine's own
    keys dropped (backbone, cot, adapters for acestep), the duration clamped into the new
    engine's range. `ParamsError` when the result breaks a rule."""
    if engine not in FROM_TEXT_ENGINES:
        raise ParamsError("engine", f"engine must be one of {', '.join(FROM_TEXT_ENGINES)}, got {engine!r}")
    dmin, dmax, _default = DURATION_LIMITS[engine]
    kept = {k: params[k] for k in ("lyrics", "style", "variants", "seed", "lyrics_prep") if k in params}
    duration = params.get("duration_s")
    if _is_number(duration):
        kept["duration_s"] = min(max(duration, dmin), dmax)
    kept["engine"] = engine
    return normalize_params_from_text(kept)


# -- song jobs from a source track (SPEC §4 `params_song`; engines in music/similar.py) --------------
# DPC adds, filled at the door from the library: `source_title`, `source_content_hash` (null for a web
# track), `source_duration_s`; `seed`; per engine `cover_strength` (acestep cover) or `backbone`, `cot`
# (yue2). Engine words are DPC's (`acestep`, `yue2`); `reference_uri` is `dpc-library:<id>`, never a path.

GENERATE_SIMILAR, COVER, GENERATE_FROM_LYRICS = "generate_similar", "cover", "generate_from_lyrics"
SONG_TYPES = (GENERATE_SIMILAR, COVER, GENERATE_FROM_LYRICS)
SONG_ENGINES = {GENERATE_SIMILAR: (ENGINE_YUE2, ENGINE_ACESTEP), GENERATE_FROM_LYRICS: (ENGINE_YUE2, ENGINE_ACESTEP),
                COVER: (ENGINE_YUE2, ENGINE_ACESTEP)}
# Mike 2026-11-26: a cover runs on yue2.cpp's score chain by default; acestep.cpp `cover` is the second choice. The
# same day, for Same style too: "For Cover or Same style add the choice of engine. We have two and YuE2 is the
# default" (a continuation of a section of the source, music/similar.py; acestep.cpp text2music is the second).
SONG_DEFAULT_ENGINE = {GENERATE_SIMILAR: ENGINE_YUE2, GENERATE_FROM_LYRICS: ENGINE_YUE2, COVER: ENGINE_YUE2}
SONG_RELATIONS = {GENERATE_SIMILAR: "same style as", COVER: "cover of",
                  GENERATE_FROM_LYRICS: "new song from the lyrics of"}
SONG_DEFAULT_LYRICS_MODE = {GENERATE_SIMILAR: "none", GENERATE_FROM_LYRICS: "reuse", COVER: "reuse"}
LYRICS_MODES = ("none", "new", "reuse")
SONG_LYRICS_MODES = {GENERATE_SIMILAR: LYRICS_MODES, GENERATE_FROM_LYRICS: ("reuse",), COVER: ("reuse", "none")}
REFERENCES = ("description", "audio")
KEEP_KEYS = ("genre", "bpm", "key", "timesig", "mood", "structure", "vocals")
KEEP_VOCALS = ("vocals", "instrumental")
KEEP_BPM_MAX = 400
MELODY_SOURCES = {ENGINE_ACESTEP: "source_audio", ENGINE_YUE2: "sheetsage2:abc"}
COVER_STRENGTH_DEFAULT = 1.0     # acestep.cpp's `audio_cover_strength` default (src/request.h)
COVER_COT_MODES = ("full", "melody")
# CC, following Ruach (build/tools/console/index.html:1250-1254, app.js:11046): the lead melody by default.
COVER_COT_DEFAULT = "melody"
# A cover is as long as its source; each engine's real floor and ceiling, read in its source (ADR-051 B5):
# acestep.cpp 973517b src/pipeline-synth-ops.cpp:152,316 (1920-sample latent frames at 48 kHz); yue2.cpp
# e646d52 src/sheetsage.h:1094 (1025 samples at 24 kHz), src/prompt.h:29 and src/generate.h:80 (the context
# holds the semantic frames plus the end token; the score and lyrics share it, so a long one may stop sooner).
ACESTEP_COVER_MIN_S = 1920 / 48000
YUE2_COVER_MIN_S = 1025 / 24000
YUE2_CONTEXT = 24576
YUE2_FRAME_RATE = 25
YUE2_COVER_MAX_S = (YUE2_CONTEXT - 1) / YUE2_FRAME_RATE - 1.0
# The context is shared (follow-up 18, Zcode #195 finding 1): yue2.cpp refuses a request unless
# `prefix + max_tokens + 1 <= context` (src/generate.h:77-83 at e646d52), and the prefix is the instruction, the
# style, the lyrics, the score and four special tokens (src/prompt.h:81-96). The frames-only cap above leaves
# nothing for them, so a cover's length is held to the context that is left after the score and the text.
# Where the numbers come from:
# - the score: a planned score is 10.3 to 11.0 tokens per source second (Score song 1857 tokens for the 4223
#   frames = 169 s of rurock_seed42, 2208 for 5349 frames = 214 s of rurock_seed43; the yue2.cpp trial logs, ai-studio
#   `engine/yue2-cpp/trial.md`). No real SheetSage2 score was at hand, so the door reserves 16 a second, a half more.
# - a score's tokens per byte of ABC, 0.74 to 0.79 on the same trial (809 tokens for 1088 bytes, 1857 for 2358, 2208
#   for 2785); the post-transcription check takes 0.85. A byte-level BPE gives at most one token per byte of ASCII.
# - the text: 425 tokens for 1582 bytes of Russian lyrics and style, 134 for 407 bytes (the same trial), at most 0.27
#   a byte; the door and the check take 0.5, with 64 for the instruction, the tags and the special tokens.
# Ruach Studio holds a backing sung over to 470 s (build/tools/console/app.js:11721-11727) and reckons a long
# prompt at 6000 rows, the 95th percentile of 407 songs (app.js:1436-1440); its 470 s leaves 12.8k rows for them.
YUE2_SCORE_TOKENS_PER_S = 16
YUE2_ABC_TOKENS_PER_BYTE = 0.85
YUE2_TEXT_TOKENS_PER_BYTE = 0.5
YUE2_PROMPT_FIXED_TOKENS = 64
YUE2_SEMANTIC_DEFAULT_MAX_TOKENS = 9000     # the semantic preset's cap (src/sampling.h:47)


def yue2_cover_frames(source_s: float) -> int:
    """The frames a cover's music stage is let run: the source's seconds and one more (Ruach Studio, app.js:11721)."""
    return math.ceil((source_s + 1.0) * YUE2_FRAME_RATE)


def yue2_semantic_budget(body: Mapping[str, Any]) -> int:
    """The semantic stage's budget as yue2.cpp takes it from a `/synth` body: the preset's `max_tokens` unless
    the body sets one, clamped to `duration` frames (src/pipeline.h:538-547)."""
    sampling = body.get("semantic_sampling") or {}
    budget = sampling.get("max_tokens", YUE2_SEMANTIC_DEFAULT_MAX_TOKENS)
    frames = int(float(body.get("duration") or 0) * YUE2_FRAME_RATE)
    return frames if 0 < frames < budget else int(budget)


def yue2_prompt_tokens(text_bytes: int, abc_bytes: int) -> int:
    """A conservative count of the tokens the prompt takes: the text (style, lyrics) and the score."""
    return (YUE2_PROMPT_FIXED_TOKENS + math.ceil(text_bytes * YUE2_TEXT_TOKENS_PER_BYTE)
            + math.ceil(abc_bytes * YUE2_ABC_TOKENS_PER_BYTE))


def song_caption_sent(caption: Any, theme: Optional[str]) -> str:
    """The caption a song engine gets: the person's with its white space made single, plus the theme of
    agent-free new lyrics. The one composition the door (`normalize_params_song`) and the run
    (`similar.caption_sent`) both count (follow-up 20, Zcode #201)."""
    text = " ".join(str(caption).split())
    return f"{text}, lyrics about {theme}" if theme else text


# What a yue2.cpp request sends as its lyrics for an Instrumental job that runs the instrumental adapter (its README's
# bare form: the model picks the structure). One source: `yue2.INSTRUMENTAL_LYRICS` (the request body) and the door's
# count (`yue2_instrumental_text_overflow`) both read it, so they cannot drift (follow-up 23, Zcode #214).
YUE2_INSTRUMENTAL_LYRICS = "[instrumental]"


def song_text_sent(style: str, lyrics: Optional[str]) -> str:
    """The text part of a yue2.cpp prompt as the run counts it: the style, a newline, the lyrics."""
    return style + "\n" + (lyrics or "")


def yue2_context_left(budget: int, prompt_tokens: int) -> int:
    """What is left of the context after the prompt, the budget and the end token; negative when it is not enough."""
    return YUE2_CONTEXT - (prompt_tokens + budget + 1)


def yue2_cover_door_max_s(text_bytes: int) -> float:
    """The longest source a cover on yue2.cpp is taken at the door, with the context shared with the text and
    with a score of `YUE2_SCORE_TOKENS_PER_S` a second (a score is not known before it is transcribed):
    frames (`yue2_cover_frames`) + score + text + the end token fit the context."""
    fixed = YUE2_CONTEXT - 1 - YUE2_FRAME_RATE - 1 - YUE2_PROMPT_FIXED_TOKENS - math.ceil(
        text_bytes * YUE2_TEXT_TOKENS_PER_BYTE)
    # one frame second is for the ceiling of the frames, one of them is `yue2_cover_frames`' second over the source
    return min(YUE2_COVER_MAX_S, math.floor((fixed - YUE2_FRAME_RATE) / (YUE2_FRAME_RATE + YUE2_SCORE_TOKENS_PER_S)))


# Match sections (`cover_score.match_sections`) adds a tag line for each of the score's sections the lyrics do not
# cover; a section copy's body is dropped at run time when it would not fit (`similar._cover_on_yue2`), so the door
# reserves only the tags. One tag line, `\n\n[pre-chorus]` and the like, is at most 16 bytes; a section is taken
# to last at least 12 s (six bars of 4/4 at 120 BPM), an estimate on the side of more sections.
YUE2_SECTION_TAG_BYTES = 16
YUE2_SECTION_MIN_S = 12.0


def yue2_cover_tag_reserve_bytes(source_s: float) -> int:
    """The bytes the door keeps for the section tags Match sections may add to a cover's lyrics."""
    return math.ceil(max(0.0, float(source_s)) / YUE2_SECTION_MIN_S) * YUE2_SECTION_TAG_BYTES


def yue2_song_context(sent_bytes: int, duration_s: float, abc_bytes: int = 0) -> Dict[str, int]:
    """The context numbers of a yue2.cpp `/synth` whose text is `sent_bytes` and whose `duration` is
    `duration_s` (the semantic budget the body carries, `yue2_semantic_budget`): `left` is negative when the
    prompt, the budget and the end token do not fit (src/generate.h:77-83)."""
    budget = yue2_semantic_budget({"duration": duration_s})
    prompt = yue2_prompt_tokens(sent_bytes, abc_bytes)
    return {"context": YUE2_CONTEXT, "prompt_tokens_est": prompt, "budget_tokens": budget,
            "left": yue2_context_left(budget, prompt), "text_bytes": sent_bytes, "score_bytes": abc_bytes}


def yue2_text_overflow(style: str, lyrics: Optional[str], duration_s: float) -> Optional[str]:
    """Why the text of a `generate_from_text` job on yue2.cpp does not fit the context with the frames its
    `duration_s` cap lets run and the end token (no score), or None when it fits. The text counted is the exact
    text the `/synth` body carries (`song_text_sent` of the style and the lyrics, nothing collapsed); the door
    (`normalize_params_from_text`) and the run (`runner._yue2_payloads`) both ask this one function (follow-up 22,
    Zcode #211)."""
    sent = len(song_text_sent(style, lyrics).encode("utf-8"))
    room = yue2_song_context(sent, duration_s)
    if room["left"] >= 0:
        return None
    return (f"the style and the lyrics are {sent} bytes as sent, about {room['prompt_tokens_est']} tokens; with the "
            f"{room['budget_tokens']} music positions a {duration_s:g} s cap asks for and the end token that is "
            f"{-room['left']} over yue2.cpp's {YUE2_CONTEXT}-position context (shorten the lyrics or the cap, or "
            "use acestep)")


# The longest source whose audio a song job decodes and uploads as a timbre reference (acestep.cpp `ref_audio`,
# same style or from lyrics): the engine's longest song (acestep.cpp clamps a duration to 600 s, 973517b
# src/metadata-fsm.h:669) plus a margin. A cover's own ceiling is its engine's (`_song_duration`).
SOURCE_AUDIO_MARGIN_S = 30.0
REFERENCE_AUDIO_MAX_S = DURATION_MAX_S + SOURCE_AUDIO_MARGIN_S
LYRICS_SOURCES = ("stored", "given")
SOURCE_URI_PREFIX = "dpc-library:"
SONG_TEXT_MAX_CHARS = 4000          # one keep value, the caption basis, the source caption, the theme
_SONG_KEYS = ("source_track_id", "source_kind", "source_title", "source_content_hash", "source_duration_s",
              "keep", "caption", "caption_basis", "source_caption", "lyrics_mode", "lyrics", "lyrics_source",
              "lyrics_theme",
              "reference", "reference_uri", "variants", "duration_s", "engine", "seed")
_SONG_COVER_KEYS = ("kept", "melody_source", "cover_strength", "vocal_only", "lyrics_times")
# Same style on yue2.cpp (Mike 2026-11-26, option 3 "both"): Music Flamingo describes the source and a section of the
# source's own semantic codes opens the song (yue2.cpp `source_tokens`, `source_start`, `source_end`; e646d52
# src/pipeline.h:352-411). `source_section` `{start_s, end_s}` is that section, on whole frames (25 a second, the
# engine rounds the bounds to frames), 10 to 60 s long, the source's first 30 s by default. The section opens the
# take and is rendered in it, and the requested length counts it (the engine's own rule), so the run asks for
# section + `duration_s` and cuts the section off each take (`similar._same_style_on_yue2`); `duration_s` is the
# length of the new part. `keep_from_source` names the kept attributes whose value the describer fills (a Keep box
# checked with no value typed). The section and the target together stay under yue2.cpp's 9000-frame semantic cap.
_SONG_SIMILAR_YUE2_KEYS = ("source_section", "keep_from_source")
SIMILAR_SECTION_MIN_S = 10.0
SIMILAR_SECTION_MAX_S = 60.0
SIMILAR_SECTION_DEFAULT_S = 30.0
# The cap of a take with its section: the semantic preset's `max_tokens` (src/sampling.h:47), which the section
# shares (src/pipeline.h:378-385); `YUE2_DURATION_MAX_S` x 25 is the same 9000.
SIMILAR_YUE2_MAX_TOTAL_S = YUE2_DURATION_MAX_S
# The keep attributes the describer can fill (`describe.py`); `structure` it does not hear.
KEEP_FROM_SOURCE_KEYS = ("genre", "bpm", "key", "timesig", "mood", "vocals")
_SONG_SOURCE_KINDS = ("local", "web", "generated")


class SongParamsError(ParamsError):
    """Song-job params refused with a code of their own (`missing_lyrics`, `track_not_ready`)."""

    def __init__(self, code: str, field: str, message: str):
        super().__init__(field, message)
        self.code = code


def song_source_uri(track_id: str) -> str:
    return f"{SOURCE_URI_PREFIX}{track_id}"


def song_needs_audio(job_type: str, params: Mapping[str, Any]) -> bool:
    """Whether a song job sends the source's audio to the engine: every cover, and a reference."""
    return job_type == COVER or params.get("reference") == "audio"


def _short_text(params: Mapping[str, Any], key: str, default: Optional[str]) -> Optional[str]:
    value = params.get(key, default)
    if value is None:
        return None
    if not _is_str(value) or len(value) > SONG_TEXT_MAX_CHARS:
        raise ParamsError(key, f"{key} must be text of at most {SONG_TEXT_MAX_CHARS} characters")
    return value


def _normalize_keep(raw: Any) -> Dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ParamsError("keep", "keep must be an object of the kept attributes and their values")
    extra = sorted(set(raw) - set(KEEP_KEYS))
    if extra:
        raise ParamsError("keep", f"keep has unknown attribute(s) {', '.join(extra)}; one of {', '.join(KEEP_KEYS)}")
    out: Dict[str, Any] = {}
    for k in KEEP_KEYS:
        if k not in raw:
            continue
        v = raw[k]
        if k == "bpm":
            if not _is_int(v) or not 1 <= v <= KEEP_BPM_MAX:
                raise ParamsError("keep", f"keep.bpm must be a whole number from 1 to {KEEP_BPM_MAX}, got {v!r}")
        elif k == "vocals":
            if v not in KEEP_VOCALS:
                raise ParamsError("keep", f"keep.vocals must be one of {', '.join(KEEP_VOCALS)}, got {v!r}")
        elif not _is_str(v) or not v.strip() or len(v) > SONG_TEXT_MAX_CHARS:
            raise ParamsError("keep", f"keep.{k} must be non-empty text")
        out[k] = v
    return out


def _normalize_lines(raw: Any) -> Optional[list]:
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = raw.splitlines()
    if not isinstance(raw, list) or not all(_is_str(x) for x in raw):
        raise ParamsError("lyrics", "lyrics must be a list of line texts (no timings), or null")
    lines = [x.rstrip("\r\n") for x in raw]
    while lines and not lines[-1].strip():
        lines.pop()
    while lines and not lines[0].strip():
        lines.pop(0)
    return lines or None


def _song_source(params: Mapping[str, Any], track_id: Optional[str],
                 source: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    tid = params.get("source_track_id", track_id)
    if track_id is not None and tid != track_id:
        raise ParamsError("source_track_id", "source_track_id must be the job's track_id")
    if not _is_str(tid) or not tid:
        raise ParamsError("source_track_id", "a song job needs its source track (track_id)")
    facts = dict(source) if isinstance(source, Mapping) else {
        "kind": params.get("source_kind"), "content_hash": params.get("source_content_hash"),
        "duration_s": params.get("source_duration_s"), "title": params.get("source_title")}
    kind = facts.get("kind")
    if kind not in _SONG_SOURCE_KINDS:
        raise ParamsError("source_kind", f"the source's kind must be one of {', '.join(_SONG_SOURCE_KINDS)}")
    if source is not None and params.get("source_kind") not in (None, kind):
        raise ParamsError("source_kind", f"the source track is a {kind} track, not {params.get('source_kind')!r}")
    title, ch, dur = facts.get("title"), facts.get("content_hash"), facts.get("duration_s")
    return {"source_track_id": tid, "source_kind": kind,
            "source_title": " ".join(title.split())[:200] if _is_str(title) and title.strip() else tid,
            "source_content_hash": ch if _is_str(ch) and ch else None,
            "source_duration_s": float(dur) if _is_number(dur) and dur > 0 else None}


def _song_lyrics(job_type: str, engine: str, params: Mapping[str, Any]) -> Dict[str, Any]:
    mode = params.get("lyrics_mode", SONG_DEFAULT_LYRICS_MODE[job_type])
    if mode not in SONG_LYRICS_MODES[job_type]:
        raise ParamsError("lyrics_mode", f"lyrics_mode of a {job_type} job must be one of "
                                         f"{', '.join(SONG_LYRICS_MODES[job_type])}, got {mode!r}")
    lines = _normalize_lines(params.get("lyrics"))
    theme = _short_text(params, "lyrics_theme", None)
    if mode == "none":
        if lines is not None:
            raise ParamsError("lyrics", "lyrics_mode none sends no lyrics; leave lyrics null")
        if job_type == COVER and engine == ENGINE_YUE2:
            raise SongParamsError("missing_lyrics", "lyrics", "a cover on yue2.cpp sings the source's melody "
                                  "from its score with the original lyrics: without them it cannot run (a technical "
                                  "limit; a cover on acestep.cpp works without lyrics)")
    elif mode == "reuse" and lines is None:
        if job_type == COVER and engine == ENGINE_YUE2:
            raise SongParamsError("missing_lyrics", "lyrics", "a cover on yue2.cpp sings the source's melody with its "
                                  "lyrics, and the track has none stored (a technical limit; a cover on acestep.cpp "
                                  "works without lyrics)")
        raise ParamsError("lyrics", "lyrics_mode reuse needs the track's lyrics lines; the track has none stored")
    if mode != "new" and theme is not None:
        raise ParamsError("lyrics_theme", "lyrics_theme goes with lyrics_mode new only")
    # `stored`: the lines are the track's own stored lyrics, filled in or checked by the command door; only
    # then does the take inherit the source's licence for them (music/similar.py). Lines given any other way
    # are `given`.
    source = params.get("lyrics_source", "given" if lines is not None else None)
    if lines is None and source is not None:
        raise ParamsError("lyrics_source", "lyrics_source names where the lyrics came from; there are none")
    if lines is not None and source not in LYRICS_SOURCES:
        raise ParamsError("lyrics_source", f"lyrics_source must be one of {', '.join(LYRICS_SOURCES)}, got {source!r}")
    return {"lyrics_mode": mode, "lyrics": lines, "lyrics_source": source,
            "lyrics_theme": (theme.strip() or None) if theme else None}


# What the run may add to the style once the score is known: `cover_score.agree_style` appends `, <n> BPM` when the
# style names no tempo (a four-digit tempo at most). The door counts it, so a request it takes does not fail on
# its text after the transcription (follow-up 20).
YUE2_COVER_TEMPO_RESERVE_BYTES = len(", 9999 BPM")


def _song_duration(job_type: str, engine: str, params: Mapping[str, Any], src_s: Optional[float],
                   text_bytes: int = 0, *, section_s: float = 0.0) -> float:
    dmin, dmax, ddefault = DURATION_LIMITS[engine]
    if section_s:
        # Same style on yue2.cpp: `duration_s` is the new part, asked for with the section (`_similar_section`).
        return _similar_duration(params, src_s, section_s, dmin)
    if job_type == COVER:
        # acestep.cpp takes the length from the source (docs/ARCHITECTURE.md, Cover); yue2.cpp is asked for it
        # (music/similar.py `yue2_cover_length`).
        if src_s is None:
            raise SongParamsError("track_not_ready", "track_id", "the source's length is not known yet; propose the "
                                  "cover again once the track is read")
        floor = ACESTEP_COVER_MIN_S if engine == ENGINE_ACESTEP else YUE2_COVER_MIN_S
        if src_s < floor:
            why = ("one 1920-sample latent frame at 48 kHz" if engine == ENGINE_ACESTEP
                   else "the 1025 samples at 24 kHz SheetSage2 needs to write a score")
            raise SongParamsError("source_too_short", "track_id", f"the source is {src_s:g} s; a cover on "
                                  f"{ENGINE_IDS[engine]} is as long as its source and needs at least {floor:.4f} s ({why})")
        if engine == ENGINE_YUE2:
            text_bytes += yue2_cover_tag_reserve_bytes(src_s)   # the tags Match sections may add
        ceiling = dmax if engine == ENGINE_ACESTEP else yue2_cover_door_max_s(text_bytes)
        if engine == ENGINE_YUE2 and ceiling < YUE2_COVER_MIN_S:
            raise SongParamsError("context_exceeded", "lyrics",
                                  f"the caption and the lyrics, with what the run may add (the tempo, section tags), "
                                  f"are {text_bytes} bytes; with the score a cover on yue2.cpp needs, they leave no "
                                  f"room in its {YUE2_CONTEXT}-position context for the music (shorten the text, or "
                                  "cover on acestep)")
        if src_s > ceiling:
            why = (f"DPC's {dmax} s bound for acestep.cpp" if engine == ENGINE_ACESTEP else
                   f"yue2.cpp's {YUE2_CONTEXT}-position context is shared: {YUE2_FRAME_RATE} frames a second of music, "
                   f"{YUE2_SCORE_TOKENS_PER_S} tokens a second reserved for the score and about "
                   f"{YUE2_PROMPT_FIXED_TOKENS + math.ceil(text_bytes * YUE2_TEXT_TOKENS_PER_BYTE)} for the text")
            raise SongParamsError("source_too_long", "track_id", f"the source is {src_s:g} s; a cover on "
                                  f"{ENGINE_IDS[engine]} is as long as its source and takes at most {ceiling:g} s ({why})")
        return round(src_s, 3)
    fallback = min(max(math.ceil(src_s), dmin), dmax) if src_s is not None else ddefault
    duration = params.get("duration_s", fallback)
    if not _is_number(duration) or not dmin <= duration <= dmax:
        what = "a cap in seconds" if engine == ENGINE_YUE2 else "a number of seconds"
        raise ParamsError("duration_s", f"duration_s must be {what} from {dmin} to {dmax} for {engine}, "
                                        f"got {duration!r}")
    return duration


def _similar_duration(params: Mapping[str, Any], src_s: Optional[float], section_s: float, dmin: float) -> float:
    """The new part's length of a same-style song on yue2.cpp: from `dmin` up to what the semantic cap leaves after
    the section (`SIMILAR_YUE2_MAX_TOTAL_S`); by default the source's length, held to that."""
    dmax = SIMILAR_YUE2_MAX_TOTAL_S - section_s
    fallback = min(max(math.ceil(src_s or 0), dmin), math.floor(dmax))
    duration = params.get("duration_s", fallback)
    if not _is_number(duration) or not dmin <= duration <= dmax:
        raise ParamsError("duration_s", f"duration_s (the new part, after the {section_s:g} s section that opens it) "
                                        f"must be from {dmin:g} to {dmax:g} s: the section and the new part together "
                                        f"stay within yue2.cpp's {SIMILAR_YUE2_MAX_TOTAL_S:g} s, got {duration!r}")
    return duration


def _song_cover(engine: str, mode: str, params: Mapping[str, Any]) -> Dict[str, Any]:
    kept = ["melody", "lyrics"] if mode == "reuse" else ["melody"]
    if params.get("kept") not in (None, kept):
        raise ParamsError("kept", f"a cover with lyrics_mode {mode} keeps {kept}")
    melody = MELODY_SOURCES[engine]
    if params.get("melody_source") not in (None, melody):
        raise ParamsError("melody_source", f"a cover on {engine} takes its melody from {melody}")
    out: Dict[str, Any] = {"kept": kept, "melody_source": melody}
    if engine == ENGINE_YUE2:
        out.update(_cover_score_choice(params))
    if engine == ENGINE_ACESTEP:
        strength = params.get("cover_strength", COVER_STRENGTH_DEFAULT)
        if not _is_number(strength) or not 0.0 <= strength <= 1.0:
            raise ParamsError("cover_strength", "cover_strength must be a number from 0 to 1 (the share of "
                                                "the engine's steps that hear the source)")
        out["cover_strength"] = strength
    return out


def _cover_score_choice(params: Mapping[str, Any]) -> Dict[str, Any]:
    """A yue2 cover's `vocal_only` (the score's Vocal voice alone; with `cot` melody) and `lyrics_times` (each
    lyrics line's start in the source, seconds or null, from the track's synced lyrics; the cover matches the
    lyrics' blocks to the score's sections with them, music/cover_score.py)."""
    vocal_only = params.get("vocal_only", False)
    if not isinstance(vocal_only, bool):
        raise ParamsError("vocal_only", "vocal_only must be true or false")
    if vocal_only and params.get("cot", COVER_COT_DEFAULT) != "melody":
        raise ParamsError("vocal_only", "vocal_only keeps the Vocal voice of a melody score: use it with cot melody")
    times = params.get("lyrics_times")
    if times is not None:
        lines = _normalize_lines(params.get("lyrics"))
        if not isinstance(times, list) or lines is None or len(times) != len(lines) or not all(
                t is None or (_is_number(t) and t >= 0) for t in times):
            raise ParamsError("lyrics_times", "lyrics_times must be one start time in seconds (or null) per lyrics "
                                              "line, or null")
    return {"vocal_only": vocal_only, "lyrics_times": times}


def _song_yue2(job_type: str, params: Mapping[str, Any]) -> Dict[str, Any]:
    backbone = params.get("backbone", YUE2_BACKBONE_DEFAULT)
    if backbone not in YUE2_BACKBONES:
        raise ParamsError("backbone", f"backbone must be one of {', '.join(YUE2_BACKBONES)}, got {backbone!r}")
    if job_type == GENERATE_SIMILAR:
        # A continuation sent without a score renders without one whatever the mode (e646d52 src/pipeline.h:461-466:
        # a score planned now is not the one the codes follow), so `off` is what runs and the only value taken.
        cot = params.get("cot", "off")
        if cot != "off":
            raise ParamsError("cot", f"same style on yue2.cpp continues the source's codes with no score: cot is off, "
                                     f"got {cot!r}")
        return {"backbone": backbone, "cot": cot}
    modes = COVER_COT_MODES if job_type == COVER else YUE2_COT_MODES
    cot = params.get("cot", COVER_COT_DEFAULT if job_type == COVER else YUE2_COT_DEFAULT)
    if cot not in modes:
        raise ParamsError("cot", f"cot must be one of {', '.join(modes)}, got {cot!r}")
    return {"backbone": backbone, "cot": cot}


def _on_frames(seconds: float) -> float:
    """Seconds on whole frames of yue2.cpp's 25 a second (the engine rounds a bound to the nearest frame)."""
    return round(round(float(seconds) * YUE2_FRAME_RATE) / YUE2_FRAME_RATE, 3)


def _similar_section(raw: Any, src_s: Optional[float]) -> Dict[str, float]:
    """The section of the source that opens a same-style song on yue2.cpp, `{start_s, end_s}` on whole frames:
    the first `SIMILAR_SECTION_DEFAULT_S` by default, a given one within the source and `SIMILAR_SECTION_MIN_S` to
    `SIMILAR_SECTION_MAX_S` long. `source_too_short` for a source shorter than the shortest section."""
    if src_s is None:
        raise SongParamsError("track_not_ready", "track_id", "the source's length is not known yet; same style on "
                              "yue2.cpp opens on a section of it once the track is read")
    if src_s < SIMILAR_SECTION_MIN_S:
        raise SongParamsError("source_too_short", "track_id", f"the source is {src_s:g} s; same style on yue2.cpp opens "
                              f"on a section of at least {SIMILAR_SECTION_MIN_S:g} s of it (use acestep)")
    whole = math.floor(src_s * YUE2_FRAME_RATE) / YUE2_FRAME_RATE
    if raw is None:
        return {"start_s": 0.0, "end_s": _on_frames(min(SIMILAR_SECTION_DEFAULT_S, whole))}
    if not isinstance(raw, Mapping) or set(raw) != {"start_s", "end_s"} or not all(
            _is_number(raw[k]) for k in ("start_s", "end_s")):
        raise ParamsError("source_section", "source_section must be {start_s, end_s} in seconds, or null for the "
                                            f"first {SIMILAR_SECTION_DEFAULT_S:g} s")
    start, end = _on_frames(raw["start_s"]), _on_frames(raw["end_s"])
    if start < 0 or end > _on_frames(whole):
        raise ParamsError("source_section", f"source_section must lie within the source, 0 to {whole:g} s")
    length = round(end - start, 3)
    if not SIMILAR_SECTION_MIN_S <= length <= SIMILAR_SECTION_MAX_S:
        raise ParamsError("source_section", f"source_section must be {SIMILAR_SECTION_MIN_S:g} to "
                                            f"{SIMILAR_SECTION_MAX_S:g} s long, got {length:g} s")
    return {"start_s": start, "end_s": end}


def _keep_from_source(raw: Any, keep: Mapping[str, Any]) -> List[str]:
    """The kept attributes whose value the describer fills, in `KEEP_KEYS` order: each one the describer hears
    (`KEEP_FROM_SOURCE_KEYS`) and not typed in `keep` (a typed value wins and is not asked again)."""
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(_is_str(k) for k in raw):
        raise ParamsError("keep_from_source", "keep_from_source must be a list of kept attribute names")
    unknown = sorted(set(raw) - set(KEEP_FROM_SOURCE_KEYS))
    if unknown:
        raise ParamsError("keep_from_source", f"keep_from_source has {', '.join(unknown)}; the describer fills "
                                              f"{', '.join(KEEP_FROM_SOURCE_KEYS)}")
    typed = sorted(set(raw) & set(keep))
    if typed:
        raise ParamsError("keep_from_source", f"{', '.join(typed)} has a typed value in keep; the typed value is kept")
    return [k for k in KEEP_KEYS if k in raw]


def normalize_params_song(job_type: str, params: Any, *, track_id: Optional[str] = None,
                          source: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The params of a `generate_similar`, `cover` or `generate_from_lyrics` job with every key filled
    in, or `ParamsError` naming the first param that breaks a rule (`SongParamsError` for
    `missing_lyrics` and `track_not_ready`). `source` is the library's `{kind, content_hash,
    duration_s, title}` of the track (the door looks it up); without it the params' own `source_*`
    keys are read, so a normalized record normalizes to itself. Per type, the engines are
    `SONG_ENGINES`, the lyrics modes `SONG_LYRICS_MODES`; a cover always uses the source's audio, and
    on acestep it is as long as its source."""
    if job_type not in SONG_TYPES:
        raise ParamsError("type", f"{job_type!r} is not a song job type")
    if not isinstance(params, Mapping):
        raise ParamsError("params", "params must be an object")
    extra = sorted(set(params) - set(_SONG_KEYS) - set(_SONG_COVER_KEYS) - set(_YUE2_ONLY_KEYS)
                   - set(_SONG_SIMILAR_YUE2_KEYS))
    if extra:
        raise ParamsError(extra[0], f"unknown param(s) for {job_type}: {', '.join(extra)}")
    out = _song_source(params, track_id, source)
    out["keep"] = _normalize_keep(params.get("keep"))
    engine = params.get("engine", SONG_DEFAULT_ENGINE[job_type])
    if engine not in SONG_ENGINES[job_type]:
        raise ParamsError("engine", f"a {job_type} job runs on {' or '.join(SONG_ENGINES[job_type])}, got {engine!r}")
    similar_yue2 = job_type == GENERATE_SIMILAR and engine == ENGINE_YUE2
    caption = params.get("caption", "" if similar_yue2 else None)
    # Same style on yue2.cpp may leave the style to the describer (`describe.py`): an empty caption is its
    # `caption_basis` `describer`, and the run fails with the reason when no description comes.
    if not _is_str(caption) or (not caption.strip() and not (similar_yue2 and caption == "")):
        what = "the target style" if job_type == COVER else "the style description"
        raise ParamsError("caption", f"caption is required: {what}")
    out["caption"] = caption
    basis_default = "describer" if similar_yue2 and not caption else "typed"
    out["caption_basis"] = _short_text(params, "caption_basis", basis_default) or basis_default
    out["source_caption"] = _short_text(params, "source_caption", "") or ""
    out["engine"] = engine
    out.update(_song_lyrics(job_type, engine, params))
    # The text the synth request will carry (`song_text_sent`), joiners and the theme's words included: both the
    # size bound (`MAX_SONG_TEXT_BYTES`, as a text job's) and the context count it, not the parts raw.
    try:
        sent_bytes = len(song_text_sent(song_caption_sent(caption, out["lyrics_theme"]),
                                        "\n".join(out["lyrics"]) if out["lyrics"] else None).encode("utf-8"))
    except UnicodeEncodeError:
        raise ParamsError("lyrics", "the caption or the lyrics hold a character that is not valid text (a lone surrogate)")
    if sent_bytes > MAX_SONG_TEXT_BYTES:
        raise ParamsError("lyrics", f"the caption, theme and lyrics come to {sent_bytes} bytes as sent; the limit is "
                                    f"{MAX_SONG_TEXT_BYTES} bytes (about a megabyte of text): shorten them")

    always_audio = job_type == COVER or similar_yue2
    reference = "audio" if always_audio else params.get("reference", "description")
    if job_type == COVER and params.get("reference", "audio") != "audio":
        raise ParamsError("reference", "a cover always uses the source's audio (reference audio)")
    if similar_yue2 and params.get("reference", "audio") != "audio":
        raise ParamsError("reference", "same style on yue2.cpp always opens on a section of the source's own audio "
                                       "codes (reference audio); a description alone runs on acestep")
    if reference not in REFERENCES:
        raise ParamsError("reference", f"reference must be one of {', '.join(REFERENCES)}, got {reference!r}")
    if reference == "audio" and engine == ENGINE_YUE2 and not always_audio:
        raise ParamsError("reference", "yue2.cpp takes no reference audio for a new song; use acestep or "
                                       "reference description")
    out["reference"] = reference
    if reference == "audio" and job_type != COVER:
        src_s = out["source_duration_s"]
        if src_s is None:
            raise SongParamsError("track_not_ready", "track_id", "the source's length is not known yet; its audio "
                                  "goes to the engine only once the track is read")
        if src_s > REFERENCE_AUDIO_MAX_S:
            raise SongParamsError("source_too_long", "track_id", f"the source is {src_s:g} s; a reference audio takes "
                                  f"at most {REFERENCE_AUDIO_MAX_S:g} s (acestep.cpp's longest song, {DURATION_MAX_S} s, "
                                  f"plus {SOURCE_AUDIO_MARGIN_S:g} s)")
    out["reference_uri"] = song_source_uri(out["source_track_id"]) if reference == "audio" else None
    if params.get("reference_uri") not in (None, out["reference_uri"]):
        raise ParamsError("reference_uri", f"reference_uri is the source, {out['reference_uri']}, or null")

    variants = params.get("variants", VARIANTS_DEFAULT)
    if not _is_int(variants) or not VARIANTS_MIN <= variants <= VARIANTS_MAX:
        raise ParamsError("variants", f"variants must be a whole number from {VARIANTS_MIN} to {VARIANTS_MAX}, "
                                      f"got {variants!r}")
    out["variants"] = variants
    section_s = 0.0
    if similar_yue2:
        out["source_section"] = _similar_section(params.get("source_section"), out["source_duration_s"])
        section_s = round(out["source_section"]["end_s"] - out["source_section"]["start_s"], 3)
        out["keep_from_source"] = _keep_from_source(params.get("keep_from_source"), out["keep"])
    out["duration_s"] = _song_duration(job_type, engine, params, out["source_duration_s"],
                                       sent_bytes + YUE2_COVER_TEMPO_RESERVE_BYTES, section_s=section_s)
    if engine == ENGINE_YUE2 and job_type != COVER:
        # A new song on yue2.cpp shares the context too: its text, the frames its duration cap lets run and the
        # end token (no score). The run checks it again before /synth (`similar.check_song_context`). Same style
        # asks for its section and its new part together (the section is in the prompt and counts in the length);
        # its style is not known before the describer answers, so the door counts the caption as typed and the run
        # counts the merged style again before the bodies are written.
        room = yue2_song_context(sent_bytes, out["duration_s"] + section_s)
        if room["left"] < 0:
            raise SongParamsError("context_exceeded", "lyrics", f"the caption and the lyrics are {sent_bytes} bytes as "
                                  f"sent, about {room['prompt_tokens_est']} tokens; with the {room['budget_tokens']} "
                                  f"music positions a {out['duration_s']:g} s cap asks for and the end token that is "
                                  f"{-room['left']} over yue2.cpp's {YUE2_CONTEXT}-position context (shorten the "
                                  "lyrics or the cap, or use acestep)")
    seed = params.get("seed")
    if seed is not None and (not _is_int(seed) or not SEED_MIN <= seed <= SEED_MAX):
        raise ParamsError("seed", f"seed must be a whole number from {SEED_MIN} to {SEED_MAX}, or null to draw one")
    out["seed"] = seed
    if job_type == COVER:
        out.update(_song_cover(engine, out["lyrics_mode"], params))
    for k in _SONG_COVER_KEYS:
        if k not in out and k in params:
            where = {"cover_strength": " on acestep", "vocal_only": " on yue2", "lyrics_times": " on yue2"}.get(k, "")
            raise ParamsError(k, f"{k} is a cover param{where}")
    if not similar_yue2:
        given = [k for k in _SONG_SIMILAR_YUE2_KEYS if k in params]
        if given:
            raise ParamsError(given[0], f"{given[0]} is a param of same style on yue2.cpp")
    if engine == ENGINE_YUE2:
        out.update(_song_yue2(job_type, params))
    else:
        given = [k for k in ("backbone", "cot") if k in params]
        if given:
            raise ParamsError(given[0], f"{given[0]} is a yue2 param")
    if "adapters" in params:
        raise ParamsError("adapters", f"a {job_type} job takes no adapters")
    return out


def check_params_song(job_type: str, params: Any) -> Optional[str]:
    """Why stored song-job params are not a normalized record, or None (`validate_job`)."""
    try:
        normalized = normalize_params_song(job_type, params)
    except ParamsError as exc:
        return exc.message
    return None if normalized == dict(params) else f"params of {job_type} must carry every key, normalized"


def _check_engine_choice(s: str, job: Mapping[str, Any]) -> None:
    ch = job["engine_choice"]
    _expect(s, isinstance(ch, Mapping) and set(ch) == _ENGINE_CHOICE_KEYS,
            f"'engine_choice' must have exactly {sorted(_ENGINE_CHOICE_KEYS)}")
    _expect(s, ch["rule"] in ENGINE_CHOICE_RULES, f"'engine_choice.rule' must be one of {ENGINE_CHOICE_RULES}")
    _expect(s, job["type"] == GENERATE_FROM_TEXT, "only a generate_from_text job carries an engine_choice")
    _expect(s, ch["engine"] == engine_of(job["params"]), "'engine_choice.engine' must be the engine the params name")
    _expect(s, isinstance(ch["adapters"], list) and all(_is_str(a) and YUE2_ADAPTER_NAME_RE.match(a)
                                                         for a in ch["adapters"]),
            "'engine_choice.adapters' must be a list of adapter names")
    named = {a.get("name") for a in job["params"].get("adapters") or [] if isinstance(a, Mapping)}
    _expect(s, set(ch["adapters"]) <= named, "'engine_choice.adapters' must be in params.adapters")
    _expect(s, ch["fallback"] is None or (ch["fallback"] in FROM_TEXT_ENGINES and ch["fallback"] != ch["engine"]),
            "'engine_choice.fallback' must be another engine or null")
    _expect(s, _is_str(ch["reason"]) and bool(ch["reason"].strip()), "'engine_choice.reason' must be text")
    _expect(s, _is_str(ch["at"]), "'engine_choice.at' must be a string")
    fb = ch["fell_back"]
    if fb is not None:
        _expect(s, isinstance(fb, Mapping) and set(fb) == _FELL_BACK_KEYS,
                f"'engine_choice.fell_back' must have exactly {sorted(_FELL_BACK_KEYS)}")
        _expect(s, fb["from"] in FROM_TEXT_ENGINES and fb["from"] != ch["engine"],
                "'engine_choice.fell_back.from' must be the engine the job left")
        _expect(s, isinstance(fb["adapters"], list) and all(_is_str(a) for a in fb["adapters"]),
                "'engine_choice.fell_back.adapters' must be a list of adapter names")
        _expect(s, _is_str(fb["why"]) and bool(fb["why"].strip()) and _is_str(fb["at"]),
                "'engine_choice.fell_back' needs why and at")


# -- records -----------------------------------------------------------------------


def _check_history(schema: str, history: Any) -> None:
    _expect(schema, isinstance(history, list) and history, "'history' must be a non-empty array")
    _expect(schema, history[0].get("state") == QUEUED if isinstance(history[0], Mapping) else False,
            "'history' must start with queued")
    for i, item in enumerate(history):
        _expect(schema, isinstance(item, Mapping), f"history[{i}] must be an object")
        _expect(schema, set(item) == {"state", "at", "reason"}, f"history[{i}] must have exactly state, at, reason")
        _expect(schema, item["state"] in JOB_STATES, f"history[{i}].state {item['state']!r} is not a job state")
        _expect(schema, _is_str(item["at"]), f"history[{i}].at must be a string")
        _expect(schema, item["reason"] is None or _is_str(item["reason"]), f"history[{i}].reason must be a string or null")
        if item["state"] in REASON_REQUIRED:
            _expect(schema, _is_str(item["reason"]) and item["reason"].strip(), f"history[{i}]: {item['state']} needs a reason")
        if i:
            prev = history[i - 1]["state"]
            _expect(schema, item["state"] in TRANSITIONS[prev], f"history[{i}]: {prev} -> {item['state']} is not a transition")


def validate_job(job: Any) -> None:
    """Raise `SchemaError` unless `job` is an `mp.job/1` the core may write."""
    s = JOB
    _check_keys(s, job, _JOB_REQUIRED, _JOB_KEYS)
    _expect(s, _is_str(job["id"]) and bool(JOB_ID_RE.match(job["id"])), "'id' must be job_ + 16 hex")
    _expect(s, job["type"] in JOB_TYPES, f"'type' {job['type']!r} is not a job type")
    _expect(s, job["state"] in JOB_STATES, f"'state' {job['state']!r} is not a job state")
    _expect(s, isinstance(job["params"], Mapping), "'params' must be an object")
    _expect(s, _is_str(job["created_at"]), "'created_at' must be a string")
    if job["type"] == GENERATE_FROM_TEXT:
        _expect(s, job["track_id"] is None and job["base_version_id"] is None,
                "a generate_from_text job has track_id and base_version_id null")
        # A record without `engine` predates YuE2: it ran on acestep and is read so.
        try:
            normalized = normalize_params_from_text(job["params"], missing_engine=ENGINE_ACESTEP)
        except ParamsError as exc:
            _fail(s, f"params: {exc.message}")
        if isinstance(job["params"], Mapping) and "engine" not in job["params"]:
            normalized.pop("engine", None)
        _expect(s, normalized == dict(job["params"]), "params of generate_from_text must carry every key")
    else:
        _expect(s, _is_str(job["track_id"]) and _is_str(job["base_version_id"]),
                f"a {job['type']} job has string track_id and base_version_id")
    if job["type"] in SONG_TYPES:
        problem = check_params_song(job["type"], job["params"])
        _expect(s, problem is None, f"params: {problem}")
        _expect(s, job["params"].get("source_track_id") == job["track_id"],
                "params.source_track_id must be the job's track_id")
    if job["type"] == "edit":
        _expect(s, _is_str(job["comment_id"]), "an edit job has a comment_id")
        _expect(s, job["action"] in EDIT_ACTIONS, f"'action' must be one of {EDIT_ACTIONS}")
        _check_range(s, job["region"], "region")
    else:
        _expect(s, job["comment_id"] is None and job["action"] is None and job["region"] is None,
                "comment_id, action and region are null for every type but edit")
    for k in ("prompt", "state_reason", "result_text", "updated_at", "idempotency_key"):
        if k in job:
            _expect(s, job[k] is None or _is_str(job[k]), f"'{k}' must be a string or null")
    if "progress" in job:
        _expect(s, _is_number(job["progress"]) and 0 <= job["progress"] <= 1, "'progress' must be from 0 to 1")
    if "take_ids" in job:
        ids = job["take_ids"]
        _expect(s, isinstance(ids, list) and all(_is_str(t) and TAKE_ID_RE.match(t) for t in ids),
                "'take_ids' must be an array of take ids")
        _expect(s, len(set(ids)) == len(ids), "'take_ids' has a repeat")
    if "history" in job:
        _check_history(s, job["history"])
        _expect(s, job["history"][-1]["state"] == job["state"], "the last history entry must be the job's state")
    if job["state"] in REASON_REQUIRED:
        _expect(s, _is_str(job.get("state_reason")) and job["state_reason"].strip(), f"a {job['state']} job needs a state_reason")
    if "payloads" in job:
        payloads = job["payloads"]
        _expect(s, isinstance(payloads, list), "'payloads' must be an array")
        names = set()
        for i, p in enumerate(payloads):
            _expect(s, isinstance(p, Mapping) and set(p) == _PAYLOAD_KEYS, f"payloads[{i}] must have exactly {sorted(_PAYLOAD_KEYS)}")
            _expect(s, _is_str(p["name"]) and bool(PAYLOAD_NAME_RE.match(p["name"])), f"payloads[{i}].name is not a payload name")
            _expect(s, p["name"] not in names, f"payloads[{i}].name {p['name']!r} repeats")
            names.add(p["name"])
            _expect(s, _is_str(p["sha256"]) and bool(SHA256_RE.match(p["sha256"])), f"payloads[{i}].sha256 must be sha256:<hex>")
            _expect(s, _is_int(p["size_bytes"]) and p["size_bytes"] >= 0, f"payloads[{i}].size_bytes must be >= 0")
            _expect(s, _is_str(p["written_at"]), f"payloads[{i}].written_at must be a string")
            _expect(s, p["endpoint"] is None or _is_str(p["endpoint"]), f"payloads[{i}].endpoint must be a string or null")
    if "card_wait" in job and job["card_wait"] is not None:
        cw = job["card_wait"]
        # ADR-052 S4 (section 3.2): a running job whose engine child ended waits for the card again.
        _expect(s, job["state"] in (QUEUED, RUNNING), "only a queued or running job waits for the card")
        _expect(s, isinstance(cw, Mapping) and set(cw) == {"holders", "since"}, "'card_wait' must have exactly holders, since")
        _expect(s, isinstance(cw["holders"], list) and cw["holders"], "'card_wait.holders' must be a non-empty array")
        for i, h in enumerate(cw["holders"]):
            _expect(s, isinstance(h, Mapping) and set(h) == {"alias", "engine"}, f"card_wait.holders[{i}] must have exactly alias, engine")
            _expect(s, _is_str(h["alias"]) and h["alias"], f"card_wait.holders[{i}].alias must be a string")
            _expect(s, h["engine"] is None or _is_str(h["engine"]), f"card_wait.holders[{i}].engine must be a string or null")
        _expect(s, _is_str(cw["since"]), "'card_wait.since' must be a string")
    if job.get("engine_choice") is not None:
        _check_engine_choice(s, job)
    if "requested_by" in job:
        try:
            clean = check_requested_by(job["requested_by"])
        except ParamsError as exc:
            _fail(s, exc.message)
        _expect(s, clean == dict(job["requested_by"]), "'requested_by' must not carry null or extra keys")


def check_reason(reason_text: Any, reason_class: Any) -> tuple:
    """The Discard reason as `(text or None, class or None)`, or `ParamsError`: at least
    one is needed (Mike 2026-11-21), the text non-blank, the class from `FAILURE_CLASSES`."""
    text = reason_text
    if text is not None:
        if not _is_str(text):
            raise ParamsError("reason_text", "reason_text must be text")
        if not text.strip():
            text = None
        elif len(text) > MAX_REASON_TEXT:
            raise ParamsError("reason_text", f"reason_text is longer than {MAX_REASON_TEXT} characters")
    cls = reason_class
    if cls is not None and cls not in FAILURE_CLASSES:
        raise ParamsError("reason_class", f"reason_class must be one of {', '.join(FAILURE_CLASSES)}, got {cls!r}")
    if text is None and cls is None:
        raise ParamsError("reason", "a discard needs a reason: a short text, a failure class "
                                    f"({', '.join(FAILURE_CLASSES)}), or both")
    return text, cls


REASONS_SCHEMA = "mp.reasons/1"
_REASON_ENTRY_KEYS = ("text", "key", "uses", "first_used_at", "last_used_at", "reason_class")


def reason_key(text: str) -> str:
    """The dictionary key of a typed Discard reason: whitespace trimmed and collapsed,
    case-folded, so "Muddy  mix " and "muddy mix" are one entry."""
    return " ".join(text.split()).casefold()


def check_reason_class(reason_class: Any) -> Optional[str]:
    """A failure class from `FAILURE_CLASSES`, or None; `ParamsError` otherwise."""
    if reason_class is not None and reason_class not in FAILURE_CLASSES:
        raise ParamsError("reason_class",
                          f"reason_class must be one of {', '.join(FAILURE_CLASSES)} or null, got {reason_class!r}")
    return reason_class


def validate_reason_entry(entry: Any) -> None:
    """Raise `SchemaError` unless `entry` is one dictionary entry of `reasons.json`."""
    s = "mp.reasons/1 entry"
    _expect(s, isinstance(entry, Mapping) and set(entry) <= set(_REASON_ENTRY_KEYS)
            and all(k in entry for k in _REASON_ENTRY_KEYS[:5]), "an entry needs text, key, uses and the two times")
    _expect(s, _is_str(entry["text"]) and bool(entry["text"].strip()), "'text' must be non-blank text")
    _expect(s, entry["key"] == reason_key(entry["text"]), "'key' must be the normalized text")
    _expect(s, isinstance(entry["uses"], int) and not isinstance(entry["uses"], bool) and entry["uses"] >= 1,
            "'uses' must be a positive integer")
    _expect(s, _is_str(entry["first_used_at"]) and _is_str(entry["last_used_at"]), "the times must be strings")
    cls = entry.get("reason_class")
    _expect(s, cls is None or cls in FAILURE_CLASSES, "'reason_class' must be a failure class or null")


def validate_take(take: Any) -> None:
    """Raise `SchemaError` unless `take` is an `mp.take/1` the core may write."""
    s = TAKE
    _check_keys(s, take, _TAKE_REQUIRED, _TAKE_KEYS)
    _expect(s, _is_str(take["id"]) and bool(TAKE_ID_RE.match(take["id"])), "'id' must be tak_ + 16 hex")
    _expect(s, take["kind"] in TAKE_KINDS, f"'kind' must be one of {TAKE_KINDS}")
    _expect(s, _is_str(take["job_id"]) and bool(JOB_ID_RE.match(take["job_id"])), "'job_id' must be a job id")
    _expect(s, take["decision"] in TAKE_DECISIONS, f"'decision' must be one of {TAKE_DECISIONS}")
    f = take["file"]
    _expect(s, isinstance(f, Mapping), "'file' must be an object")
    _expect(s, "uri" in f and "content_hash" in f, "'file' needs uri and content_hash")
    extra = sorted(set(f) - _FILE_KEYS)
    _expect(s, not extra, f"'file' has undeclared key(s): {', '.join(extra)}")
    _expect(s, _is_str(f["uri"]) and f["uri"], "'file.uri' must be a string")
    _expect(s, f["content_hash"] is None or _is_str(f["content_hash"]), "'file.content_hash' must be a string or null")
    for k in ("sample_rate", "channels"):
        if k in f:
            _expect(s, f[k] is None or (_is_int(f[k]) and f[k] > 0), f"'file.{k}' must be a positive integer or null")
    if "duration_s" in f:
        _expect(s, f["duration_s"] is None or (_is_number(f["duration_s"]) and f["duration_s"] >= 0),
                "'file.duration_s' must be a number >= 0 or null")
    if take["qa"] is not None:
        check_qa(take["qa"], s)
    if take.get("qa_report") is not None:
        check_qa_report(take["qa_report"], s)
    if take.get("qc") is not None:
        check_qc(take["qc"], s)
        _expect(s, take["qc"]["take_id"] == take["id"], "'qc.take_id' must be the take's id")
    if take["kind"] == "new_track":
        for k in ("variant", "title", "source_track_id"):
            _expect(s, k in take, f"a new_track take needs '{k}'")
        _expect(s, _is_int(take["variant"]) and take["variant"] >= 1, "'variant' must be an integer >= 1")
        _expect(s, _is_str(take["title"]), "'title' must be a string")
        _expect(s, take["source_track_id"] is None or _is_str(take["source_track_id"]), "'source_track_id' must be a string or null")
        _expect(s, take["track_id"] is None and take["base_version_id"] is None and take["region"] is None,
                "a new_track take has track_id, base_version_id and region null")
    else:
        _expect(s, _is_str(take["track_id"]) and _is_str(take["base_version_id"]), "an edit take has track_id and base_version_id")
        _check_range(s, take["region"], "region")
    if "seed" in take:
        _expect(s, _is_int(take["seed"]) and SEED_MIN <= take["seed"] <= SEED_MAX, f"'seed' must be from {SEED_MIN} to {SEED_MAX}")
    if "synth_batch_size" in take or "synth_batch_index" in take:
        size, index = take.get("synth_batch_size"), take.get("synth_batch_index")
        _expect(s, _is_int(size) and 1 <= size <= SYNTH_BATCH_SIZE_MAX, "'synth_batch_size' must be a positive integer")
        _expect(s, _is_int(index) and 0 <= index < size, "'synth_batch_index' must be from 0 to synth_batch_size - 1")
    if "payload" in take:
        _expect(s, _is_str(take["payload"]) and bool(PAYLOAD_NAME_RE.match(take["payload"])), "'payload' must be a payload name")
    if "engine" in take:
        _expect(s, take["engine"] is None or (_is_str(take["engine"]) and bool(take["engine"])),
                "'engine' must be a string or null")
    if "lm_seed" in take:
        _expect(s, take["lm_seed"] is None or (_is_int(take["lm_seed"]) and 0 <= take["lm_seed"] <= LM_SEED_MAX),
                "'lm_seed' must be a whole number >= 0 or null")
    if "engine_window_s" in take:
        _expect(s, _is_number(take["engine_window_s"]) and take["engine_window_s"] >= 0,
                "'engine_window_s' must be a number >= 0")
        _expect(s, take.get("length_check") in ("ok", "mismatch"), "'length_check' must be 'ok' or 'mismatch'")
        _expect(s, _is_number(take.get("length_delta_s")), "'length_delta_s' must be a number")
    if "duration_requested_s" in take:
        _expect(s, _is_number(take["duration_requested_s"]) and take["duration_requested_s"] > 0,
                "'duration_requested_s' must be a positive number")
    for k in ("library_track_id", "decided_at", "created_at"):
        if k in take:
            _expect(s, take[k] is None or _is_str(take[k]), f"'{k}' must be a string or null")
    if "simulated" in take:
        _expect(s, isinstance(take["simulated"], bool), "'simulated' must be a boolean")
    if take.get("loudness") is not None:
        check_loudness(take["loudness"], s)
    text, cls = take.get("reason_text"), take.get("reason_class")
    _expect(s, text is None or (_is_str(text) and text.strip()), "'reason_text' must be non-blank text or null")
    _expect(s, cls is None or cls in FAILURE_CLASSES, f"'reason_class' must be one of {FAILURE_CLASSES} or null")
    if take["decision"] == REJECTED:
        _expect(s, text is not None or cls is not None, "a rejected take needs reason_text, reason_class or both")
    else:
        _expect(s, text is None and cls is None, "only a rejected take carries a reason")


def check_loudness(loudness: Any, schema: str = TAKE) -> None:
    """Raise `SchemaError` unless `loudness` is a take's loudness record: exactly the
    five keys, the three numbers finite or null, `lufs_i` a number, the two strings set."""
    _expect(schema, isinstance(loudness, Mapping) and set(loudness) == _LOUDNESS_KEYS,
            f"'loudness' must have exactly {sorted(_LOUDNESS_KEYS)}")
    _expect(schema, _is_number(loudness["lufs_i"]), "'loudness.lufs_i' must be a number")
    for k in ("lra", "true_peak_dbtp"):
        _expect(schema, loudness[k] is None or _is_number(loudness[k]), f"'loudness.{k}' must be a number or null")
    for k in ("measured_by", "measured_at"):
        _expect(schema, _is_str(loudness[k]) and bool(loudness[k]), f"'loudness.{k}' must be a string")


def check_qa(qa: Any, schema: str = TAKE) -> None:
    """Raise `SchemaError` unless `qa` is exactly an `mp.qa/1` (SPEC §8): all of `QA_KEYS`
    present, nothing else but `simulated`, `integrated_lufs`, `true_peak_dbtp` numbers,
    `clipped_samples` a whole number >= 0, the loudness delta, the seam fields and the WER
    numbers or null (the seams >= 0, the WER within 0-1), `measured_by` text."""
    _expect(schema, isinstance(qa, Mapping), "'qa' must be an object, or null")
    missing = [k for k in QA_KEYS if k not in qa]
    extra = sorted(set(qa) - set(QA_KEYS) - set(_QA_OPTIONAL))
    _expect(schema, not missing, f"'qa' (mp.qa/1) lacks: {', '.join(missing)}")
    _expect(schema, not extra, f"'qa' (mp.qa/1) has undeclared key(s): {', '.join(extra)}")
    for k in ("integrated_lufs", "true_peak_dbtp"):
        _expect(schema, _is_number(qa[k]), f"'qa.{k}' must be a finite number")
    _expect(schema, _is_int(qa["clipped_samples"]) and qa["clipped_samples"] >= 0,
            "'qa.clipped_samples' must be an integer >= 0")
    _expect(schema, qa["loudness_delta_lu"] is None or _is_number(qa["loudness_delta_lu"]),
            "'qa.loudness_delta_lu' must be a number or null")
    for k in ("seam_start_jump_db", "seam_end_jump_db", "seam_click_score"):
        _expect(schema, qa[k] is None or (_is_number(qa[k]) and qa[k] >= 0), f"'qa.{k}' must be a number >= 0 or null")
    _expect(schema, qa["lyrics_wer"] is None or (_is_number(qa["lyrics_wer"]) and 0 <= qa["lyrics_wer"] <= 1),
            "'qa.lyrics_wer' must be a number from 0 to 1, or null")
    _expect(schema, _is_str(qa["measured_by"]) and bool(qa["measured_by"]), "'qa.measured_by' must be text")
    if "simulated" in qa:
        _expect(schema, isinstance(qa["simulated"], bool), "'qa.simulated' must be a boolean")


def qa_verdict_of(gates: Mapping[str, Mapping[str, Any]]) -> str:
    """The verdict the gate statuses give: `failed` when any gate failed, `passed` only when
    every gate passed, `not_measured` otherwise. An unmeasured gate is never a pass."""
    statuses = [(gates.get(name) or {}).get("status") for name in QA_GATES]
    if GATE_FAIL in statuses:
        return QA_FAILED
    return QA_PASSED if all(st == GATE_PASS for st in statuses) else QA_NOT_MEASURED


def check_qa_report(report: Any, schema: str = TAKE) -> None:
    """Raise `SchemaError` unless `report` is a take's stage-1 `qa_report`: the declared keys,
    each gate with a status and a detail, the advisory entries never `pass`/`fail`, and the
    verdict the one `qa_verdict_of` gives."""
    _expect(schema, isinstance(report, Mapping), "'qa_report' must be an object, or null")
    needed = _QA_REPORT_KEYS - {"note", "unmeasurable"}
    missing = sorted(needed - set(report))
    extra = sorted(set(report) - _QA_REPORT_KEYS)
    _expect(schema, not missing, f"'qa_report' lacks: {', '.join(missing)}")
    _expect(schema, not extra, f"'qa_report' has undeclared key(s): {', '.join(extra)}")
    _expect(schema, report["stage"] == 1, "'qa_report.stage' must be 1")
    gates = report["gates"]
    _expect(schema, isinstance(gates, Mapping) and set(gates) == set(QA_GATES),
            f"'qa_report.gates' must have exactly {list(QA_GATES)}")
    for name, gate in gates.items():
        _expect(schema, isinstance(gate, Mapping) and gate.get("status") in GATE_STATUSES,
                f"'qa_report.gates.{name}.status' must be one of {GATE_STATUSES}")
        _expect(schema, _is_str(gate.get("detail")) and bool(gate["detail"]),
                f"'qa_report.gates.{name}.detail' must be text")
    advisory = report["advisory"]
    _expect(schema, isinstance(advisory, Mapping) and set(advisory) == set(QA_ADVISORY_ENTRIES),
            f"'qa_report.advisory' must have exactly {list(QA_ADVISORY_ENTRIES)}")
    for name, entry in advisory.items():
        _expect(schema, isinstance(entry, Mapping) and entry.get("status") in ("measured", GATE_NOT_MEASURED),
                f"'qa_report.advisory.{name}.status' must be 'measured' or 'not_measured': an advisory entry never gates")
    _expect(schema, report["verdict"] in QA_VERDICTS, f"'qa_report.verdict' must be one of {QA_VERDICTS}")
    _expect(schema, report["verdict"] == qa_verdict_of(gates), "'qa_report.verdict' does not follow from the gates")
    _expect(schema, isinstance(report["measurements"], Mapping) and isinstance(report["thresholds"], Mapping),
            "'qa_report.measurements' and 'thresholds' must be objects")
    for k in ("measured_by", "measured_at"):
        _expect(schema, _is_str(report[k]) and bool(report[k]), f"'qa_report.{k}' must be text")
    if "note" in report:
        _expect(schema, report["note"] is None or _is_str(report["note"]), "'qa_report.note' must be text or null")
    if "unmeasurable" in report:
        mark = report["unmeasurable"]
        _expect(schema, isinstance(mark, Mapping) and set(mark) == _QA_UNMEASURABLE_KEYS,
                f"'qa_report.unmeasurable' must be an object with exactly {sorted(_QA_UNMEASURABLE_KEYS)}")
        _expect(schema, _is_str(mark["reason"]) and bool(mark["reason"]) and _is_str(mark["last_attempt_at"]),
                "'qa_report.unmeasurable' reason and last_attempt_at must be text")
        _expect(schema, isinstance(mark["attempts"], int) and not isinstance(mark["attempts"], bool) and mark["attempts"] >= 1,
                "'qa_report.unmeasurable.attempts' must be an integer >= 1")
        _expect(schema, mark["file_hash"] is None or _is_str(mark["file_hash"]),
                "'qa_report.unmeasurable.file_hash' must be text or null")


def check_qc_issue(issue: Any, schema: str = TAKE, where: str = "qc.issues[]") -> None:
    """Raise `SchemaError` unless `issue` is one `mp.qc/1` issue: `t_start` and `t_end` numbers
    >= 0 in order, `kind` and `note` text, `expected_pronunciation` text and only on a
    `pronunciation` issue."""
    _expect(schema, isinstance(issue, Mapping), f"'{where}' must be an object")
    missing = sorted({"t_start", "t_end", "kind", "note"} - set(issue))
    extra = sorted(set(issue) - _QC_ISSUE_KEYS)
    _expect(schema, not missing, f"'{where}' lacks: {', '.join(missing)}")
    _expect(schema, not extra, f"'{where}' has undeclared key(s): {', '.join(extra)}")
    for k in ("t_start", "t_end"):
        _expect(schema, _is_number(issue[k]) and issue[k] >= 0, f"'{where}.{k}' must be a number >= 0")
    _expect(schema, issue["t_end"] >= issue["t_start"], f"'{where}.t_end' must not be before t_start")
    _expect(schema, _is_str(issue["kind"]) and bool(issue["kind"].strip()), f"'{where}.kind' must be text")
    _expect(schema, _is_str(issue["note"]), f"'{where}.note' must be text")
    if "expected_pronunciation" in issue:
        _expect(schema, issue["kind"] == "pronunciation" and _is_str(issue["expected_pronunciation"]),
                f"'{where}.expected_pronunciation' is text on a pronunciation issue only")


def check_qc_scores(scores: Any, schema: str = TAKE) -> None:
    """Raise `SchemaError` unless `scores` holds exactly the six `mp.qc/1` scores, each 0-10."""
    _expect(schema, isinstance(scores, Mapping) and set(scores) == set(QC_SCORE_KEYS),
            f"'qc.scores' must have exactly {list(QC_SCORE_KEYS)}")
    for k in QC_SCORE_KEYS:
        _expect(schema, _is_number(scores[k]) and 0 <= scores[k] <= 10,
                f"'qc.scores.{k}' must be a number from 0 to 10")


def check_qc(qc: Any, schema: str = TAKE) -> None:
    """Raise `SchemaError` unless `qc` is an `mp.qc/1` as DPC writes it (SPEC §8.1 plus the
    extensions in the module docstring): `deterministic` an `mp.qa/1` or null (no stage-1
    numbers), the judge's status deciding whether `scores` and `confidence` are set, the
    issues valid, the advisory WER never a gate."""
    _check_keys(QC, qc, _QC_REQUIRED, _QC_KEYS)  # also checks 'schema' == mp.qc/1
    _expect(schema, _is_str(qc["take_id"]) and bool(TAKE_ID_RE.match(qc["take_id"])),
            "'qc.take_id' must be a take id")
    _expect(schema, qc["flow"] in QC_FLOWS, f"'qc.flow' must be one of {QC_FLOWS}")
    if qc["deterministic"] is not None:
        check_qa(qc["deterministic"], schema)
    judge = qc["judge"]
    _expect(schema, isinstance(judge, Mapping) and set(judge) == _QC_JUDGE_KEYS,
            f"'qc.judge' must have exactly {sorted(_QC_JUDGE_KEYS)}")
    _expect(schema, judge["status"] in JUDGE_STATUSES, f"'qc.judge.status' must be one of {JUDGE_STATUSES}")
    _expect(schema, _is_str(judge["rubric"]) and bool(judge["rubric"]), "'qc.judge.rubric' must be text")
    for k in ("model", "runtime", "alias", "error", "judged_at"):
        _expect(schema, judge[k] is None or _is_str(judge[k]), f"'qc.judge.{k}' must be text or null")
    done = judge["status"] == JUDGE_DONE
    if done:
        _expect(schema, _is_str(judge["model"]) and bool(judge["model"]), "a done judge names its model")
        check_qc_scores(qc["scores"], schema)
        _expect(schema, _is_number(qc["confidence"]) and 0 <= qc["confidence"] <= 1,
                "'qc.confidence' must be a number from 0 to 1")
    else:
        _expect(schema, qc["scores"] is None and qc["confidence"] is None,
                "'qc.scores' and 'qc.confidence' are null unless the judge is done")
    if judge["status"] == JUDGE_FAILED:
        _expect(schema, _is_str(judge["error"]) and bool(judge["error"]),
                "a failed judge says why ('qc.judge.error')")
    _expect(schema, isinstance(qc["issues"], list), "'qc.issues' must be a list")
    _expect(schema, done or not qc["issues"], "'qc.issues' come from the judge: empty unless it is done")
    for i, issue in enumerate(qc["issues"]):
        check_qc_issue(issue, schema, f"qc.issues[{i}]")
    _expect(schema, _is_str(qc["reasons"]), "'qc.reasons' must be text")
    if "simulated" in qc:
        _expect(schema, isinstance(qc["simulated"], bool), "'qc.simulated' must be a boolean")
    if "last_attempt" in qc:
        att = qc["last_attempt"]
        _expect(schema, done and isinstance(att, Mapping) and set(att) == _QC_ATTEMPT_KEYS,
                f"'qc.last_attempt' sits beside a done judge and has exactly {sorted(_QC_ATTEMPT_KEYS)}")
        _expect(schema, att["status"] == JUDGE_FAILED and _is_str(att["error"]) and bool(att["error"])
                and _is_str(att["at"]) and (att["alias"] is None or _is_str(att["alias"])),
                "'qc.last_attempt' is a failed attempt with its error, time and alias")
    if "stage1" in qc:
        st = qc["stage1"]
        _expect(schema, isinstance(st, Mapping) and st.get("verdict") in QA_VERDICTS,
                f"'qc.stage1.verdict' must be one of {QA_VERDICTS}")
    if "lyrics_wer" in qc:
        wer = qc["lyrics_wer"]
        _expect(schema, isinstance(wer, Mapping) and set(wer) <= _QC_WER_KEYS,
                f"'qc.lyrics_wer' keys must be within {sorted(_QC_WER_KEYS)}")
        _expect(schema, wer.get("status") in ("measured", GATE_NOT_MEASURED),
                "'qc.lyrics_wer.status' must be 'measured' or 'not_measured': it never gates")
        if wer.get("status") == "measured":
            _expect(schema, _is_number(wer.get("value")) and 0 <= wer["value"] <= 1,
                    "'qc.lyrics_wer.value' must be a number from 0 to 1")
    for k in ("caveat", "assembled_at"):
        if k in qc:
            _expect(schema, _is_str(qc[k]) and bool(qc[k]), f"'qc.{k}' must be text")


def job_reason_needed(state: str) -> bool:
    return state in REASON_REQUIRED


def describe_transition(old: str, new: str) -> Optional[str]:
    """Why `old -> new` is refused, or None when it is a transition of SPEC §5."""
    if old not in TRANSITIONS:
        return f"{old!r} is not a job state"
    if new not in JOB_STATES:
        return f"{new!r} is not a job state"
    if new in TRANSITIONS[old]:
        return None
    if old in TERMINAL_STATES:
        return f"the job is {old}, which is final"
    return f"a {old} job cannot become {new}"
