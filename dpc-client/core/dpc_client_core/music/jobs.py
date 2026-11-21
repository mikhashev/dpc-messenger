"""The music job store: `mp.job/1` jobs and their `mp.take/1` takes on disk (SPEC §4-§6).

One instance per backend. The `local_api` commands and the future `music_*` agent tools
call the same methods (ADR-051 D11). The store keeps the state machine, the take decisions
and the payload files. Engine calls, QA and the library entry are the runner's work; the
runner reports its results here.

```
<DPC home>/music/jobs/
  <job_id>/
    job.json            {"job": mp.job/1, "takes": [mp.take/1]}
    payload/<name>      the exact request bytes for the engine, written once
```

**Persistence** follows the library (`store.py`): `job.json` is replaced whole in one step
(a temp file in the same folder, fsynced, then `os.replace`). A `job.json` that does not
parse is moved aside as `job.json.corrupt-<UTC time>`, and one that fails its schema as
`job.json.rejected-<UTC time>`. Either way the job is skipped and nothing is deleted.

**Restart.** At `open()`, a job left `running` has lost its runner. With no take it
becomes `failed` with "interrupted by restart". With takes it goes to `review` with that
reason, because SPEC §4 fails a job only when no take of it is usable, and the finished
variants are already files. A `queued` job stays queued; its card wait is cleared, since
the holder named before the restart may be gone.

**Payloads** (SPEC §3 "show and store the payload before an expensive job runs";
PROVENANCE payload rule). The body the engine will receive is written before it is sent:
the exact bytes, under a name such as `lm.request.json`. A name is written once and never
replaced. Each is recorded with its sha256 and size, and `verify_payload` re-reads it. A
job cannot start (`queued -> running`) before its first payload is on disk. Later payloads
are written while it runs, such as the `/synth` body, which exists only once `/lm` has
answered.

**Takes and decisions** (SPEC §5, §12 decisions 24 and 35; Mike 2026-11-21). The runner
adds takes while the job runs, one per variant, and never two for one variant or one file.
Each `generate_from_text` take carries its seed, `synth_batch_size`, `synth_batch_index`
and the payload that produced it. From `review` on, the person decides: **Keep** marks a
take `accepted`, and the job becomes `accepted`. **Discard** marks it `rejected` with a
reason (text, a failure class, or both; never neither). **None of these** rejects every
pending take with one reason. When every take is rejected, the job becomes `rejected`. A
take is decided once. Agents get no decision method of their own (ADR-051 Q1); the door
that calls these methods must be the person's.

**Unsupported.** `create_job` asks the capabilities (`capabilities.py`) whether the type
can run now, on the engine its params name (`generate_from_text`: yue2 by default, or
acestep). When it cannot, the job is created `unsupported` with the reason, the normal
answer of SPEC §5. A YuE2 backbone whose file is missing or an adapter the adapters folder
does not hold is refused as `invalid_params` (`MusicCapabilities.check_params`).

**Threading.** Every public method is blocking and thread-safe (one re-entrant lock).
`job.json` is written before the method returns. Call it through `asyncio.to_thread` from
the event loop. Every method returns copies.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from . import job_schemas as js
from .capabilities import MusicCapabilities
from .schemas import SchemaError
from .store import atomic_write_text, music_dir_of

logger = logging.getLogger(__name__)

JOBS_DIR = "jobs"
JOB_FILE = "job.json"
PAYLOAD_DIR = "payload"
REASONS_FILE = "reasons.json"
RESTART_REASON = "interrupted by restart"
CANCEL_REASON = "cancelled by the person"
WAITING_FOR_CARD = "waiting for the card"
MAX_PAYLOAD_BYTES = 8 * 1024 * 1024

_REPLACE_TRIES = 20
_REPLACE_RETRY_S = 0.05


class MusicJobError(Exception):
    """A request the job store refuses. `code` is a short machine word, and `field` names
    the param at fault when there is one."""

    def __init__(self, code: str, message: str, field: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _new_id(prefix: str) -> str:
    return f"{prefix}{secrets.token_hex(8)}"


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Replace `path` with exactly `data` in one step (binary, no newline translation)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(_REPLACE_TRIES):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == _REPLACE_TRIES - 1:
                    raise
                time.sleep(_REPLACE_RETRY_S)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _holder_text(holders: List[Dict[str, Any]]) -> str:
    names = [h["alias"] for h in holders]
    if len(names) == 1:
        return f"{WAITING_FOR_CARD}: {names[0]} holds it"
    return f"{WAITING_FOR_CARD}: {', '.join(names)} hold it"


class MusicJobStore:
    """Jobs and takes under `<DPC home>/music/jobs/`."""

    def __init__(
        self,
        dpc_home_dir: Path,
        *,
        capabilities: Optional[MusicCapabilities] = None,
        now: Optional[Callable[[], str]] = None,
    ):
        self.dpc_home = Path(dpc_home_dir)
        self.root = music_dir_of(dpc_home_dir) / JOBS_DIR
        self.capabilities = capabilities or MusicCapabilities()
        self._now = now or _now
        self._lock = threading.RLock()
        self._jobs: Dict[str, dict] = {}
        self._takes: Dict[str, List[dict]] = {}
        self._take_job: Dict[str, str] = {}
        self._by_key: Dict[str, str] = {}
        self._reasons: Dict[str, dict] = {}
        self._loaded = False

    # -- storage -----------------------------------------------------------------

    def job_dir(self, job_id: str) -> Path:
        return self.root / job_id

    def job_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / JOB_FILE

    def payload_path(self, job_id: str, name: str) -> Path:
        self._check_payload_name(name)
        return self.job_dir(job_id) / PAYLOAD_DIR / name

    def _set_aside(self, path: Path, kind: str) -> None:
        aside = path.with_name(f"{path.name}.{kind}-{_stamp()}")
        try:
            os.replace(path, aside)
        except OSError:
            logger.warning("music: could not move %s aside", path, exc_info=True)

    def _load_one(self, folder: Path) -> Optional[tuple]:
        path = folder / JOB_FILE
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError, OSError) as exc:
            logger.error("music: %s does not parse (%s); moved aside", path, exc)
            self._set_aside(path, "corrupt")
            return None
        try:
            if not isinstance(data, dict) or set(data) != {"job", "takes"} or not isinstance(data["takes"], list):
                raise SchemaError("job.json must be {\"job\": ..., \"takes\": [...]}")
            job, takes = data["job"], data["takes"]
            js.validate_job(job)
            if job["id"] != folder.name:
                raise SchemaError(f"job id {job['id']!r} is not its folder name {folder.name!r}")
            for t in takes:
                js.validate_take(t)
                if t["job_id"] != job["id"]:
                    raise SchemaError(f"take {t['id']} names another job")
            if [t["id"] for t in takes] != list(job.get("take_ids") or []):
                raise SchemaError("take_ids do not match the takes")
        except SchemaError as exc:
            logger.warning("music: %s fails its schema and is kept aside: %s", path, exc)
            self._set_aside(path, "rejected")
            return None
        return job, takes

    def _snapshot_text(self, job_id: str) -> str:
        job = self._jobs[job_id]
        takes = self._takes.get(job_id, [])
        js.validate_job(job)
        for t in takes:
            js.validate_take(t)
        return json.dumps({"job": job, "takes": takes}, ensure_ascii=False, indent=1, allow_nan=False)

    def _save(self, job_id: str) -> None:
        atomic_write_text(self.job_path(job_id), self._snapshot_text(job_id))

    def _commit(self, job_id: str, before: dict, before_takes: List[dict]) -> None:
        """Write a job after a change; on a failed write put the memory back, so memory
        never runs ahead of the disk."""
        try:
            self._save(job_id)
        except BaseException:
            self._jobs[job_id] = before
            self._takes[job_id] = before_takes
            self._reindex_takes()
            raise

    def _reindex_takes(self) -> None:
        self._take_job = {t["id"]: jid for jid, takes in self._takes.items() for t in takes}

    def open(self) -> None:
        """Load every job and apply the restart rule. Blocking; idempotent."""
        with self._lock:
            if self._loaded:
                return
            self._jobs, self._takes, self._by_key = {}, {}, {}
            if self.root.is_dir():
                for folder in sorted(p for p in self.root.iterdir() if p.is_dir()):
                    loaded = self._load_one(folder)
                    if loaded is None:
                        continue
                    job, takes = loaded
                    self._jobs[job["id"]] = job
                    self._takes[job["id"]] = takes
                    if job.get("idempotency_key"):
                        self._by_key[job["idempotency_key"]] = job["id"]
            self._reindex_takes()
            self._reasons = self._load_reasons()
            self._loaded = True
            for job_id in list(self._jobs):
                self._after_restart(job_id)

    # -- the Discard-reason dictionary (SPEC 35, ADR-051 D32) ----------------------

    @property
    def reasons_path(self) -> Path:
        return self.root / REASONS_FILE

    def _load_reasons(self) -> Dict[str, dict]:
        path = self.reasons_path
        if not path.is_file():
            return {}
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(doc, dict) or doc.get("schema") != js.REASONS_SCHEMA \
                    or not isinstance(doc.get("entries"), list):
                raise SchemaError("mp.reasons/1", "not an mp.reasons/1 document")
            out: Dict[str, dict] = {}
            for entry in doc["entries"]:
                entry.setdefault("reason_class", None)
                js.validate_reason_entry(entry)
                out[entry["key"]] = entry
            return out
        except (OSError, ValueError, SchemaError, TypeError, AttributeError):
            logger.warning("music: %s is unreadable; set aside, the dictionary starts empty", path, exc_info=True)
            self._set_aside(path, "corrupt")
            return {}

    def _save_reasons(self) -> None:
        doc = {"schema": js.REASONS_SCHEMA, "entries": list(self._reasons.values())}
        atomic_write_text(self.reasons_path, json.dumps(doc, ensure_ascii=False, indent=2) + "\n")

    def _reason_class_for(self, text: Optional[str], cls: Optional[str]) -> Optional[str]:
        """The class a take gets: the one the call gave, else the one the typed reason's
        dictionary entry carries."""
        if cls is not None or text is None:
            return cls
        entry = self._reasons.get(js.reason_key(text))
        return entry["reason_class"] if entry else None

    def _note_reason(self, text: Optional[str], cls: Optional[str]) -> None:
        """Record or bump the entry of a typed reason. A class the call gives is attached
        to an entry that has none (once); an entry's class is never overwritten here."""
        if text is None:
            return
        key = js.reason_key(text)
        at = self._now()
        entry = self._reasons.get(key)
        if entry is None:
            entry = {"text": " ".join(text.split()), "key": key, "uses": 0,
                     "first_used_at": at, "last_used_at": at, "reason_class": None}
            self._reasons[key] = entry
        entry["uses"] += 1
        entry["last_used_at"] = at
        if entry["reason_class"] is None and cls is not None:
            entry["reason_class"] = cls
        try:
            self._save_reasons()
        except OSError:
            logger.warning("music: could not save %s; the dictionary is kept in memory", self.reasons_path,
                           exc_info=True)

    def list_reasons(self, prefix: Optional[str] = None, limit: int = 50) -> List[dict]:
        """The dictionary, most used first, then most recently used. `prefix` is matched
        case-insensitively against the normalized key."""
        if prefix is not None and not isinstance(prefix, str):
            raise MusicJobError("invalid_params", "prefix must be text", field="prefix")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise MusicJobError("invalid_params", "limit must be a positive integer", field="limit")
        with self._lock:
            self._ready()
            want = js.reason_key(prefix) if prefix is not None else ""
            rows = [e for e in self._reasons.values() if e["key"].startswith(want)]
            rows.sort(key=lambda e: e["last_used_at"], reverse=True)
            rows.sort(key=lambda e: e["uses"], reverse=True)
            return copy.deepcopy(rows[:limit])

    def set_reason_class(self, key: str, reason_class: Optional[str]) -> dict:
        """Attach a failure class to an entry, or clear it with None. Takes already
        decided keep the class they were given."""
        try:
            cls = js.check_reason_class(reason_class)
        except js.ParamsError as exc:
            raise MusicJobError("invalid_params", exc.message, field=exc.field)
        with self._lock:
            self._ready()
            entry = self._reasons.get(js.reason_key(key)) if isinstance(key, str) else None
            if entry is None:
                raise MusicJobError("not_found", f"no discard reason {key!r}", field="key")
            entry["reason_class"] = cls
            self._save_reasons()
            return copy.deepcopy(entry)

    def _after_restart(self, job_id: str) -> None:
        job = self._jobs[job_id]
        before, before_takes = copy.deepcopy(job), copy.deepcopy(self._takes[job_id])
        if job["state"] == js.RUNNING:
            takes = self._takes[job_id]
            if takes:
                wanted = job["params"].get("variants") if job["type"] == js.GENERATE_FROM_TEXT else None
                of = f" of {wanted}" if wanted else ""
                self._move(job, js.REVIEW, f"{RESTART_REASON} after {len(takes)}{of} variant(s)")
            else:
                self._move(job, js.FAILED, RESTART_REASON)
        elif job["state"] == js.QUEUED and (job.get("card_wait") or job.get("state_reason")):
            job["card_wait"] = None
            job["state_reason"] = None
            job["updated_at"] = self._now()
        else:
            return
        try:
            self._commit(job_id, before, before_takes)
        except OSError:
            logger.exception("music: could not save job %s after the restart rule", job_id)

    def _ready(self) -> None:
        if not self._loaded:
            self.open()

    # -- jobs --------------------------------------------------------------------

    def _job(self, job_id: Any) -> dict:
        if not isinstance(job_id, str) or job_id not in self._jobs:
            raise MusicJobError("not_found", f"no music job {job_id!r}")
        return self._jobs[job_id]

    def _move(self, job: dict, state: str, reason: Optional[str] = None) -> None:
        """Apply one SPEC §5 transition to `job` in memory, or raise."""
        why = js.describe_transition(job["state"], state)
        if why is not None:
            raise MusicJobError("illegal_transition", f"job {job['id']}: {why}")
        if reason is not None and (not isinstance(reason, str) or not reason.strip()):
            reason = None
        if js.job_reason_needed(state) and reason is None:
            raise MusicJobError("reason_required", f"a {state} job needs a reason")
        at = self._now()
        job["state"] = state
        job["state_reason"] = reason
        job["history"].append({"state": state, "at": at, "reason": reason})
        job["updated_at"] = at
        if state != js.QUEUED:
            job["card_wait"] = None

    def _new_job(self, job_type: str, params: dict, *, track_id, base_version_id, comment_id, action, region,
                 prompt, idempotency_key) -> dict:
        at = self._now()
        return {
            "schema": js.JOB,
            "id": _new_id("job_"),
            "type": job_type,
            "track_id": track_id,
            "base_version_id": base_version_id,
            "comment_id": comment_id,
            "action": action,
            "region": region,
            "prompt": prompt,
            "params": params,
            "state": js.QUEUED,
            "state_reason": None,
            "progress": 0,
            "take_ids": [],
            "result_text": None,
            "history": [{"state": js.QUEUED, "at": at, "reason": None}],
            "created_at": at,
            "updated_at": at,
            "idempotency_key": idempotency_key,
            "payloads": [],
            "card_wait": None,
        }

    def create_job(
        self,
        job_type: str,
        params: Any,
        *,
        track_id: Optional[str] = None,
        base_version_id: Optional[str] = None,
        comment_id: Optional[str] = None,
        action: Optional[str] = None,
        region: Optional[Mapping[str, float]] = None,
        prompt: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict:
        """Propose a job (SPEC §6 `propose_job`) and return it: `queued` when its type can
        run now, else `unsupported` with the reason.

        `generate_from_text` params are checked and completed by
        `job_schemas.normalize_params_from_text`, and its `prompt` is the style. Other
        types are recorded as `unsupported` (none is built), with their source fields
        checked for shape only. The same `idempotency_key` returns the same job. The same
        key with another type or other params is refused (`idempotency_conflict`).

        Raises `MusicJobError`: `invalid_type`, `invalid_params` (with `field`),
        `invalid_source`, `idempotency_conflict`."""
        with self._lock:
            self._ready()
            if job_type not in js.JOB_TYPES:
                raise MusicJobError("invalid_type", f"{job_type!r} is not a job type; one of {', '.join(js.JOB_TYPES)}",
                                    field="type")
            if idempotency_key is not None and (not isinstance(idempotency_key, str) or not idempotency_key.strip()):
                raise MusicJobError("invalid_params", "idempotency_key must be non-empty text", field="idempotency_key")
            if job_type == js.GENERATE_FROM_TEXT:
                if any(v is not None for v in (track_id, base_version_id, comment_id, action, region)):
                    raise MusicJobError("invalid_source", "a generate_from_text job has no source track, version, "
                                                          "comment or region")
                try:
                    params = js.normalize_params_from_text(params)
                    self.capabilities.check_params(job_type, params)
                except js.ParamsError as exc:
                    raise MusicJobError("invalid_params", exc.message, field=exc.field)
                prompt = params["style"]
            else:
                params = self._check_other_source(job_type, params, track_id, base_version_id, comment_id, action, region)
                if prompt is not None and not isinstance(prompt, str):
                    raise MusicJobError("invalid_params", "prompt must be text", field="prompt")
                region = dict(region) if region is not None else None

            if idempotency_key is not None and idempotency_key in self._by_key:
                old = self._jobs[self._by_key[idempotency_key]]
                if old["type"] != job_type or old["params"] != params or old["track_id"] != track_id:
                    raise MusicJobError("idempotency_conflict",
                                        f"idempotency_key {idempotency_key!r} belongs to job {old['id']} with other params")
                return copy.deepcopy(old)

            job = self._new_job(job_type, params, track_id=track_id, base_version_id=base_version_id,
                                comment_id=comment_id, action=action, region=region, prompt=prompt,
                                idempotency_key=idempotency_key)
            reason = self.capabilities.unsupported_reason(
                job_type, params if job_type == js.GENERATE_FROM_TEXT else None)
            if reason is not None:
                self._move(job, js.UNSUPPORTED, reason)
            self._jobs[job["id"]] = job
            self._takes[job["id"]] = []
            try:
                self._save(job["id"])
            except BaseException:
                del self._jobs[job["id"]]
                del self._takes[job["id"]]
                raise
            if idempotency_key is not None:
                self._by_key[idempotency_key] = job["id"]
            return copy.deepcopy(job)

    @staticmethod
    def _check_other_source(job_type, params, track_id, base_version_id, comment_id, action, region) -> dict:
        if not isinstance(params, Mapping):
            raise MusicJobError("invalid_params", "params must be an object", field="params")
        try:
            params = json.loads(json.dumps(params, ensure_ascii=False, allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise MusicJobError("invalid_params", f"params must be plain JSON: {exc}", field="params")
        if not (isinstance(track_id, str) and track_id and isinstance(base_version_id, str) and base_version_id):
            raise MusicJobError("invalid_source", f"a {job_type} job needs track_id and base_version_id")
        if job_type == "edit":
            if not isinstance(comment_id, str) or not comment_id:
                raise MusicJobError("invalid_source", "an edit job needs comment_id")
            if action not in js.EDIT_ACTIONS:
                raise MusicJobError("invalid_source", f"an edit job needs an action, one of {', '.join(js.EDIT_ACTIONS)}")
            ok = isinstance(region, Mapping) and set(region) == {"t_start", "t_end"} and all(
                isinstance(region[k], (int, float)) and not isinstance(region[k], bool) and region[k] >= 0
                for k in ("t_start", "t_end"))
            if not ok:
                raise MusicJobError("invalid_source", "an edit job needs a region {t_start, t_end}")
        elif any(v is not None for v in (comment_id, action, region)):
            raise MusicJobError("invalid_source", "comment_id, action and region are for edit jobs only")
        return params

    def get_job(self, job_id: str) -> dict:
        with self._lock:
            self._ready()
            return copy.deepcopy(self._job(job_id))

    def list_jobs(self, *, states: Optional[Iterable[str]] = None, job_type: Optional[str] = None) -> List[dict]:
        """Jobs oldest first (the queue order), optionally only some states or one type."""
        with self._lock:
            self._ready()
            wanted = set(states) if states is not None else None
            if wanted is not None and not wanted <= set(js.JOB_STATES):
                raise MusicJobError("invalid_params", f"unknown state(s): {', '.join(sorted(wanted - set(js.JOB_STATES)))}",
                                    field="states")
            out = [j for j in self._jobs.values()
                   if (wanted is None or j["state"] in wanted) and (job_type is None or j["type"] == job_type)]
            out.sort(key=lambda j: (j["created_at"], j["id"]))
            return copy.deepcopy(out)

    def _change(self, job_id: str, change: Callable[[dict], None]) -> dict:
        with self._lock:
            self._ready()
            job = self._job(job_id)
            before, before_takes = copy.deepcopy(job), copy.deepcopy(self._takes[job_id])
            try:
                change(job)
            except BaseException:
                self._jobs[job_id] = before
                self._takes[job_id] = before_takes
                self._reindex_takes()
                raise
            self._commit(job_id, before, before_takes)
            return copy.deepcopy(self._jobs[job_id])

    def cancel_job(self, job_id: str, reason: Optional[str] = None) -> dict:
        """The person's Cancel: a queued or running job becomes `cancelled`; a cancelled
        job is returned as it is (idempotent, SPEC §6). The runner sees the state and stops."""
        with self._lock:
            self._ready()
            if self._job(job_id)["state"] == js.CANCELLED:
                return copy.deepcopy(self._jobs[job_id])
            return self._change(job_id, lambda j: self._move(j, js.CANCELLED, reason or CANCEL_REASON))

    def start_job(self, job_id: str) -> dict:
        """The runner takes a queued job: `queued -> running`. Refused (`no_payload`) until
        the first payload is written."""
        def change(job: dict) -> None:
            if job["state"] == js.QUEUED and not job.get("payloads"):
                raise MusicJobError("no_payload", f"job {job_id}: write the payload before the run starts")
            self._move(job, js.RUNNING)
        return self._change(job_id, change)

    def finish_job(self, job_id: str, reason: Optional[str] = None) -> dict:
        """The runner is done: `running -> review`. A new-track job needs a take first."""
        def change(job: dict) -> None:
            if job["type"] in js.NEW_TRACK_TYPES and job["state"] == js.RUNNING and not self._takes[job_id]:
                raise MusicJobError("no_takes", f"job {job_id} has no take to review; fail it instead")
            self._move(job, js.REVIEW, reason)
            job["progress"] = 1
        return self._change(job_id, change)

    def refuse_job(self, job_id: str, reason: str) -> dict:
        """The runner cannot build a queued job's request any more (an adapter it names
        went missing after the proposal): `queued -> unsupported`, with the reason."""
        return self._change(job_id, lambda j: self._move(j, js.UNSUPPORTED, reason))

    def fail_job(self, job_id: str, reason: str) -> dict:
        """The runner gives up: `running -> failed`, with the reason (required)."""
        return self._change(job_id, lambda j: self._move(j, js.FAILED, reason))

    def set_progress(self, job_id: str, progress: float) -> dict:
        def change(job: dict) -> None:
            if job["state"] != js.RUNNING:
                raise MusicJobError("illegal_transition", f"job {job_id} is {job['state']}, not running")
            if isinstance(progress, bool) or not isinstance(progress, (int, float)) or not 0 <= progress <= 1:
                raise MusicJobError("invalid_params", "progress must be from 0 to 1", field="progress")
            job["progress"] = progress
            job["updated_at"] = self._now()
        return self._change(job_id, change)

    def set_waiting_for_card(self, job_id: str, holders: Iterable[Mapping[str, Any]]) -> dict:
        """Record that a queued job waits for the GPU card (SPEC §5, decision 32).
        `holders` is `card_holders()`'s list; only `alias` and `engine` are kept. The
        reason is "waiting for the card: X holds it". An empty list clears the wait.
        Nothing is stopped here: "Stop X" is the person's action."""
        kept = []
        for h in holders:
            alias = h.get("alias") if isinstance(h, Mapping) else None
            if not isinstance(alias, str) or not alias:
                raise MusicJobError("invalid_params", "each card holder needs an alias", field="holders")
            engine = h.get("engine")
            kept.append({"alias": alias, "engine": engine if isinstance(engine, str) else None})

        def change(job: dict) -> None:
            if job["state"] != js.QUEUED:
                raise MusicJobError("illegal_transition", f"job {job_id} is {job['state']}; only a queued job waits")
            if not kept:
                job["card_wait"] = None
                job["state_reason"] = None
            else:
                old = job.get("card_wait")
                since = old["since"] if old else self._now()
                job["card_wait"] = {"holders": kept, "since": since}
                job["state_reason"] = _holder_text(kept)
            job["updated_at"] = self._now()
        return self._change(job_id, change)

    def clear_waiting_for_card(self, job_id: str) -> dict:
        return self.set_waiting_for_card(job_id, [])

    # -- payloads ----------------------------------------------------------------

    @staticmethod
    def _check_payload_name(name: Any) -> None:
        if not isinstance(name, str) or not js.PAYLOAD_NAME_RE.match(name):
            raise MusicJobError("invalid_params", f"payload name {name!r}: lowercase letters, digits, '.', '_', '-'; "
                                                  "at most 64", field="name")

    def write_payload(self, job_id: str, name: str, body: bytes, *, endpoint: Optional[str] = None) -> dict:
        """Write the exact bytes the engine will receive, before they are sent, and record
        them on the job. Allowed while the job is queued or running. A name is written once
        (`payload_exists`). Returns the record `{name, sha256, size_bytes, written_at,
        endpoint}`."""
        self._check_payload_name(name)
        if not isinstance(body, (bytes, bytearray)):
            raise MusicJobError("invalid_params", "a payload is bytes, exactly as sent", field="body")
        body = bytes(body)
        if len(body) > MAX_PAYLOAD_BYTES:
            raise MusicJobError("invalid_params", f"a payload is at most {MAX_PAYLOAD_BYTES} bytes", field="body")
        if endpoint is not None and not isinstance(endpoint, str):
            raise MusicJobError("invalid_params", "endpoint must be text", field="endpoint")
        with self._lock:
            self._ready()
            job = self._job(job_id)
            if job["state"] not in (js.QUEUED, js.RUNNING):
                raise MusicJobError("illegal_transition", f"job {job_id} is {job['state']}; payloads are written "
                                                          "before or during the run")
            path = self.payload_path(job_id, name)
            if any(p["name"] == name for p in job["payloads"]) or path.exists():
                raise MusicJobError("payload_exists", f"payload {name!r} of job {job_id} is already written")
            atomic_write_bytes(path, body)
            record = {"name": name, "sha256": _sha256(body), "size_bytes": len(body),
                      "written_at": self._now(), "endpoint": endpoint}

            def change(j: dict) -> None:
                j["payloads"].append(record)
                j["updated_at"] = record["written_at"]
            try:
                self._change(job_id, change)
            except BaseException:
                # Not recorded, so not sent: remove the file, or a retry would be refused.
                try:
                    path.unlink()
                except OSError:
                    logger.warning("music: could not remove the unrecorded payload %s", path, exc_info=True)
                raise
            return dict(record)

    def read_payload(self, job_id: str, name: str) -> bytes:
        with self._lock:
            self._ready()
            job = self._job(job_id)
            if not any(p["name"] == name for p in job["payloads"]):
                raise MusicJobError("not_found", f"job {job_id} has no payload {name!r}")
            return self.payload_path(job_id, name).read_bytes()

    def verify_payload(self, job_id: str, name: str) -> bool:
        """Whether the payload file still holds the bytes recorded when it was written."""
        with self._lock:
            self._ready()
            job = self._job(job_id)
            record = next((p for p in job["payloads"] if p["name"] == name), None)
            if record is None:
                raise MusicJobError("not_found", f"job {job_id} has no payload {name!r}")
            try:
                data = self.payload_path(job_id, name).read_bytes()
            except OSError:
                return False
            return len(data) == record["size_bytes"] and _sha256(data) == record["sha256"]

    # -- takes -------------------------------------------------------------------

    def add_take(
        self,
        job_id: str,
        *,
        variant: int,
        file_uri: str,
        content_hash: Optional[str],
        title: str,
        seed: int,
        synth_batch_size: int,
        synth_batch_index: int,
        payload: str,
        sample_rate: Optional[int] = None,
        channels: Optional[int] = None,
        duration_s: Optional[float] = None,
        library_track_id: Optional[str] = None,
        simulated: bool = False,
        loudness: Optional[Mapping[str, Any]] = None,
        engine: Optional[str] = None,
        lm_seed: Optional[int] = None,
    ) -> dict:
        """Record a finished take of a running `generate_from_text` job: a `new_track` take,
        `pending`, `qa` null until QA runs, `loudness` the runner's ebur128 measurement or
        null (`loudness.py`), `engine` the engine id that made it and `lm_seed` its token
        seed when the engine reports one. The file is written and registered by the runner
        (`register_generated`). One take per variant and per file (decision 24:
        `duplicate_take`). `payload` must name a payload of this job. Raises
        `MusicJobError`."""
        with self._lock:
            self._ready()
            job = self._job(job_id)
            if job["state"] != js.RUNNING:
                raise MusicJobError("illegal_transition", f"job {job_id} is {job['state']}; takes arrive while it runs")
            if job["type"] != js.GENERATE_FROM_TEXT:
                raise MusicJobError("invalid_type", f"takes of {job['type']} jobs are not built")
            takes = self._takes[job_id]
            wanted = job["params"]["variants"]
            if isinstance(variant, bool) or not isinstance(variant, int) or not 1 <= variant <= wanted:
                raise MusicJobError("invalid_params", f"variant must be from 1 to {wanted}", field="variant")
            if any(t["variant"] == variant for t in takes):
                raise MusicJobError("duplicate_take", f"job {job_id} already has variant {variant}")
            if any(t["file"]["uri"] == file_uri for t in takes):
                raise MusicJobError("duplicate_take", f"job {job_id} already has a take for {file_uri}")
            if not any(p["name"] == payload for p in job["payloads"]):
                raise MusicJobError("invalid_params", f"job {job_id} has no payload {payload!r}", field="payload")
            file: Dict[str, Any] = {"uri": file_uri, "content_hash": content_hash, "sample_rate": sample_rate,
                                    "channels": channels, "duration_s": duration_s}
            take = {
                "schema": js.TAKE,
                "id": _new_id("tak_"),
                "kind": "new_track",
                "variant": variant,
                "job_id": job_id,
                "track_id": None,
                "base_version_id": None,
                "source_track_id": None,
                "title": title,
                "region": None,
                "file": file,
                "qa": None,
                "decision": js.PENDING,
                "simulated": simulated,
                "created_at": self._now(),
                "seed": seed,
                "synth_batch_size": synth_batch_size,
                "synth_batch_index": synth_batch_index,
                "payload": payload,
                "duration_requested_s": job["params"]["duration_s"],
                "library_track_id": library_track_id,
                "reason_text": None,
                "reason_class": None,
                "decided_at": None,
                "loudness": dict(loudness) if loudness is not None else None,
                "engine": engine,
                "lm_seed": lm_seed,
            }
            try:
                js.validate_take(take)
            except SchemaError as exc:
                raise MusicJobError("invalid_params", str(exc))

            def change(j: dict) -> None:
                self._takes[job_id].append(take)
                j["take_ids"].append(take["id"])
                j["updated_at"] = take["created_at"]
            self._change(job_id, change)
            self._take_job[take["id"]] = job_id
            return copy.deepcopy(take)

    def set_take_loudness(self, take_id: str, loudness: Mapping[str, Any]) -> dict:
        """Record a take's loudness measurement (`loudness.py`), in any job state and for
        any decision: it is a measurement of the file, not a decision. A later measurement
        replaces an earlier one. Raises `MusicJobError` (`not_found`, `invalid_params`)."""
        try:
            js.check_loudness(loudness)
        except SchemaError as exc:
            raise MusicJobError("invalid_params", str(exc), field="loudness")
        with self._lock:
            self._ready()
            job_id, _ = self._take(take_id)

            def change(job: dict) -> None:
                take = next(t for t in self._takes[job_id] if t["id"] == take_id)
                take["loudness"] = dict(loudness)
            self._change(job_id, change)
            return copy.deepcopy(self._take(take_id)[1])

    def list_takes(self, job_id: str) -> List[dict]:
        with self._lock:
            self._ready()
            self._job(job_id)
            return copy.deepcopy(sorted(self._takes[job_id], key=lambda t: t.get("variant") or 0))

    def get_take(self, take_id: str) -> dict:
        with self._lock:
            self._ready()
            return copy.deepcopy(self._take(take_id)[1])

    def _take(self, take_id: Any) -> tuple:
        job_id = self._take_job.get(take_id) if isinstance(take_id, str) else None
        if job_id is None:
            raise MusicJobError("not_found", f"no music take {take_id!r}")
        take = next(t for t in self._takes[job_id] if t["id"] == take_id)
        return job_id, take

    @staticmethod
    def _check_decidable(job: dict, take: dict) -> None:
        if job["state"] not in (js.REVIEW, js.ACCEPTED):
            raise MusicJobError("illegal_transition", f"job {job['id']} is {job['state']}; takes are decided from review on")
        if take["decision"] != js.PENDING:
            raise MusicJobError("already_decided", f"take {take['id']} is already {take['decision']}")

    def _settle_job(self, job: dict, takes: List[dict]) -> None:
        """Move a job in review to accepted or rejected from its takes' decisions."""
        if job["state"] != js.REVIEW:
            return
        kept = [t for t in takes if t["decision"] == js.ACCEPTED]
        if kept:
            self._move(job, js.ACCEPTED, f"kept take {kept[0]['id']} (variant {kept[0].get('variant')})")
        elif takes and all(t["decision"] == js.REJECTED for t in takes):
            self._move(job, js.REJECTED, "every take discarded")

    def keep_take(self, take_id: str) -> dict:
        """The person's Keep: the take becomes `accepted`, and its job `accepted`."""
        with self._lock:
            self._ready()
            job_id, _ = self._take(take_id)

            def change(job: dict) -> None:
                take = next(t for t in self._takes[job_id] if t["id"] == take_id)
                self._check_decidable(job, take)
                take["decision"] = js.ACCEPTED
                take["decided_at"] = self._now()
                job["updated_at"] = take["decided_at"]
                self._settle_job(job, self._takes[job_id])
            self._change(job_id, change)
            return copy.deepcopy(self._take(take_id)[1])

    def discard_take(self, take_id: str, *, reason_text: Optional[str] = None,
                     reason_class: Optional[str] = None) -> dict:
        """The person's Discard: the take becomes `rejected` with a reason (free text, a
        failure class, or both; `reason_required` when neither). The record stays."""
        text, cls = self._reason(reason_text, reason_class)
        with self._lock:
            self._ready()
            job_id, _ = self._take(take_id)
            eff = self._reason_class_for(text, cls)

            def change(job: dict) -> None:
                take = next(t for t in self._takes[job_id] if t["id"] == take_id)
                self._check_decidable(job, take)
                self._reject(take, text, eff)
                job["updated_at"] = take["decided_at"]
                self._settle_job(job, self._takes[job_id])
            self._change(job_id, change)
            self._note_reason(text, cls)
            return copy.deepcopy(self._take(take_id)[1])

    def reject_all_takes(self, job_id: str, *, reason_text: Optional[str] = None,
                         reason_class: Optional[str] = None) -> dict:
        """The person's "None of these": every pending take is rejected with one reason,
        and the job becomes `rejected`. Only in `review`: once a take is kept the job is
        `accepted`, and the other takes are discarded one by one."""
        text, cls = self._reason(reason_text, reason_class)
        with self._lock:
            self._ready()
            eff = self._reason_class_for(text, cls)

            def change(job: dict) -> None:
                takes = self._takes[job_id]
                if job["state"] != js.REVIEW:
                    raise MusicJobError("illegal_transition", f"job {job_id} is {job['state']}, not in review")
                for t in takes:
                    if t["decision"] == js.PENDING:
                        self._reject(t, text, eff)
                job["updated_at"] = self._now()
                self._settle_job(job, takes)
            job = self._change(job_id, change)
            self._note_reason(text, cls)
            return job

    @staticmethod
    def _reason(reason_text: Any, reason_class: Any) -> tuple:
        try:
            return js.check_reason(reason_text, reason_class)
        except js.ParamsError as exc:
            code = "reason_required" if exc.field == "reason" else "invalid_params"
            raise MusicJobError(code, exc.message, field=exc.field)

    def _reject(self, take: dict, text: Optional[str], cls: Optional[str]) -> None:
        take["decision"] = js.REJECTED
        take["reason_text"] = text
        take["reason_class"] = cls
        take["decided_at"] = self._now()
