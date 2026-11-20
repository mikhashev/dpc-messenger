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

**`generate_from_text` limits** (SPEC §12 decisions 33-34, Mike 2026-11-20/21):
variants 1-4 (default 2), duration 10-600 s (default 120), ACE-Step turbo Q8, 8 steps;
the engine (acestep.cpp) answers wav24, which DPC converts to FLAC. The seed range is
acestep.cpp's own (`src/request.h`: `[0, UINT32_MAX]`, the Python reference's
`random.randint(0, 2**32 - 1)`); null means the runner draws one.

Keys that are not in the SPEC yet are declared here as extensions, all optional so a
record without them is valid against both shapes:

- `mp.job/1`: `idempotency_key` (SPEC §6 `propose_job` takes one; the job did not
  record it); `payloads`, the request bodies written for the engine, each
  `{name, sha256, size_bytes, written_at, endpoint}` (SPEC §3 "show and store the
  payload", PROVENANCE payload rule); `card_wait`, `{holders: [{alias, engine}], since}`
  while a queued job waits for the card, so the job card can offer "Stop X" by alias.
- `mp.take/1`: `seed`, `synth_batch_size`, `synth_batch_index` (a take is reproducible
  from codes, seed, params, batch size and batch index on the same build: PROVENANCE
  §2, measured 2026-11-21) and `payload` (the name of the payload whose bytes produced
  it); `duration_requested_s` (SPEC §4: requested and received duration are two
  fields; received is `file.duration_s`); `library_track_id` (the library entry
  `register_generated` made for the file); `reason_text`, `reason_class`, `decided_at`
  (Discard needs a reason, Mike 2026-11-21: free text, a failure class, or both).
- `mp.take/1.qa` may be null until stage-1 QA has measured the take (slice 1 step 4);
  the SPEC makes `mp.qa/1` required with numeric fields, which nothing can fill before
  the measurement runs.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, Iterable, Mapping, Optional

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

# generate_from_text (SPEC §12 decisions 33-34).
FROM_TEXT_ENGINE = "ace-step-1.5:text2music"
VARIANTS_MIN, VARIANTS_MAX, VARIANTS_DEFAULT = 1, 4, 2
DURATION_MIN_S, DURATION_MAX_S, DURATION_DEFAULT_S = 10, 600, 120
SEED_MIN, SEED_MAX = 0, 2 ** 32 - 1
FROM_TEXT_DIT_MODEL = "turbo"
FROM_TEXT_DIT_QUANT = "Q8"
FROM_TEXT_INFERENCE_STEPS = 8
ENGINE_OUTPUT_FORMAT = "wav24"
LIBRARY_FORMAT = "flac"
SYNTH_BATCH_SIZE_MAX = 16  # a sanity bound on a recorded value, not an engine limit

_PARAMS_FROM_TEXT_KEYS = ("lyrics", "style", "variants", "duration_s", "seed", "lyrics_prep", "engine")

_JOB_REQUIRED = (
    "schema", "id", "type", "track_id", "base_version_id", "comment_id", "action", "region",
    "params", "state", "created_at",
)
_JOB_KEYS = set(_JOB_REQUIRED) | {
    "prompt", "state_reason", "progress", "take_ids", "result_text", "result", "backend",
    "history", "updated_at",
    # extensions, see the module docstring
    "idempotency_key", "payloads", "card_wait",
}
_PAYLOAD_KEYS = {"name", "sha256", "size_bytes", "written_at", "endpoint"}
PAYLOAD_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

_TAKE_REQUIRED = ("schema", "id", "kind", "job_id", "track_id", "base_version_id", "region", "file", "qa", "decision")
_TAKE_KEYS = set(_TAKE_REQUIRED) | {
    "variant", "source_track_id", "title", "qc", "simulated", "created_at",
    # extensions, see the module docstring
    "seed", "synth_batch_size", "synth_batch_index", "payload", "duration_requested_s",
    "library_track_id", "reason_text", "reason_class", "decided_at",
}
_FILE_KEYS = {"uri", "content_hash", "sample_rate", "channels", "duration_s"}


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


def normalize_params_from_text(params: Any) -> Dict[str, Any]:
    """The params of a `generate_from_text` job with the SPEC defaults filled in, or
    `ParamsError` naming the first param that breaks a rule.

    Defaults: `variants` 2, `duration_s` 120, `seed` null (the runner draws one),
    `lyrics_prep` {} (the generate dialog fills it), `engine` the one allowed value.
    `lyrics` and `style` must be given. The lyrics are kept verbatim: nothing is
    stripped or normalised, so U+0301 stress marks and the person's line breaks stay."""
    if not isinstance(params, Mapping):
        raise ParamsError("params", "params must be an object")
    extra = sorted(set(params) - set(_PARAMS_FROM_TEXT_KEYS))
    if extra:
        raise ParamsError(extra[0], f"unknown param(s) for generate_from_text: {', '.join(extra)}")
    out: Dict[str, Any] = {}

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

    variants = params.get("variants", VARIANTS_DEFAULT)
    if not _is_int(variants) or not VARIANTS_MIN <= variants <= VARIANTS_MAX:
        raise ParamsError("variants", f"variants must be a whole number from {VARIANTS_MIN} to {VARIANTS_MAX}, "
                                      f"got {variants!r}")
    out["variants"] = variants

    duration = params.get("duration_s", DURATION_DEFAULT_S)
    if not _is_number(duration) or not DURATION_MIN_S <= duration <= DURATION_MAX_S:
        raise ParamsError("duration_s", f"duration_s must be a number of seconds from {DURATION_MIN_S} to "
                                        f"{DURATION_MAX_S}, got {duration!r}")
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

    engine = params.get("engine", FROM_TEXT_ENGINE)
    if engine != FROM_TEXT_ENGINE:
        raise ParamsError("engine", f"engine must be {FROM_TEXT_ENGINE!r}, got {engine!r}")
    out["engine"] = engine
    return out


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
        try:
            normalized = normalize_params_from_text(job["params"])
        except ParamsError as exc:
            _fail(s, f"params: {exc.message}")
        _expect(s, normalized == dict(job["params"]), "params of generate_from_text must carry every key")
    else:
        _expect(s, _is_str(job["track_id"]) and _is_str(job["base_version_id"]),
                f"a {job['type']} job has string track_id and base_version_id")
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
        _expect(s, job["state"] == QUEUED, "only a queued job waits for the card")
        _expect(s, isinstance(cw, Mapping) and set(cw) == {"holders", "since"}, "'card_wait' must have exactly holders, since")
        _expect(s, isinstance(cw["holders"], list) and cw["holders"], "'card_wait.holders' must be a non-empty array")
        for i, h in enumerate(cw["holders"]):
            _expect(s, isinstance(h, Mapping) and set(h) == {"alias", "engine"}, f"card_wait.holders[{i}] must have exactly alias, engine")
            _expect(s, _is_str(h["alias"]) and h["alias"], f"card_wait.holders[{i}].alias must be a string")
            _expect(s, h["engine"] is None or _is_str(h["engine"]), f"card_wait.holders[{i}].engine must be a string or null")
        _expect(s, _is_str(cw["since"]), "'card_wait.since' must be a string")


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
    _expect(s, take["qa"] is None or isinstance(take["qa"], Mapping), "'qa' must be an object, or null before QA ran")
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
    if "duration_requested_s" in take:
        _expect(s, _is_number(take["duration_requested_s"]) and take["duration_requested_s"] > 0,
                "'duration_requested_s' must be a positive number")
    for k in ("library_track_id", "decided_at", "created_at"):
        if k in take:
            _expect(s, take[k] is None or _is_str(take[k]), f"'{k}' must be a string or null")
    if "simulated" in take:
        _expect(s, isinstance(take["simulated"], bool), "'simulated' must be a boolean")
    text, cls = take.get("reason_text"), take.get("reason_class")
    _expect(s, text is None or (_is_str(text) and text.strip()), "'reason_text' must be non-blank text or null")
    _expect(s, cls is None or cls in FAILURE_CLASSES, f"'reason_class' must be one of {FAILURE_CLASSES} or null")
    if take["decision"] == REJECTED:
        _expect(s, text is not None or cls is not None, "a rejected take needs reason_text, reason_class or both")
    else:
        _expect(s, text is None and cls is None, "only a rejected take carries a reason")


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
