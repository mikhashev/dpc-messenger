"""The QC commands of the music jobs (`qc.py`, SPEC §8.1), on the `local_api` door.

`MusicQcCommandsMixin` sits beside `MusicCommandsMixin` on `CoreService` and uses its job store,
lyrics and settings helpers; each `async def` without a leading underscore is one command and is
in `local_api.ALLOWED_COMMANDS`. No agent tool calls them (ADR-051 Q1 keeps the take decisions
with the person; QC is advice for that decision).

- `judge_music_take {take_id}`: the stage-2 judge, on demand only. The judge is the alias named
  by `[music] qc_judge_alias` (`DPC_MUSIC_QC_JUDGE_ALIAS`; empty by default: `no_judge`), an
  audio-capable llama-server alias (`mmproj_kind: audio`). Its first call starts the child through
  the card lease like any local engine, so it waits for the card a running engine holds. The
  result, `done` or `failed` with the reason, is stored as the take's `qc` (stage 1 is assembled
  again beside it, with the lyrics WER when a transcription is stored); a failed judge leaves
  stage 1 as it was. Never accepts, rejects or blocks Keep / Discard.
- `get_music_job_qc {job_id, refresh?}`: each take's `qc`, the ranking state and the display
  order (`qc.ranking_state`, `qc.rank_takes`). Ranking is `on` only with a judge alias and
  `[music] qc_ranking` true (default false, SPEC §12 decision 26). `refresh` assembles stage 1
  again for every take (it reads a stored transcription for the WER, never starts Whisper) and
  keeps each take's judge result. `judge_alias` is the configured alias or null: the UI shows a
  Judge button only when it is set.
- `backfill_music_qa {job_id?, take_id?, force?}`: measures stage-1 QA (`qa.py`) for takes made
  before it existed or whose measurement could not run (no `qa_report`, or a `not_measured`
  verdict; with `force`, every take of the scope), one take at a time off the event loop, stores
  `qa` / `qa_report` through the job store and assembles `mp.qc/1` again beside them, keeping a
  stored judge result. A take whose file is gone is `no_file`; the rest go on.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..local_api import slow_command
from . import job_schemas as js
from . import qc as qc_
from . import region_edit as re_
from .qa import LENGTH_CAP, LENGTH_EXACT, LENGTH_SAME, ffmpeg_qa_meter, not_measured_record
from .commands import _FALSE, _TRUE, _error, _need_str, path_of_file_uri
from .jobs import MusicJobError

logger = logging.getLogger(__name__)

DEFAULT_QC_RANKING = False
QC_COMMANDS = ("judge_music_take", "get_music_job_qc", "backfill_music_qa")
BACKFILL_NOTE = "measured after the take was made (backfill_music_qa)"
# A take whose backfill still ends `not_measured` is marked `qa_report.unmeasurable` (follow-up 16, Zcode #188
# finding 6) and is skipped by later runs without `force`: at once when its length cannot be recovered (nothing
# a later run could add), after `UNMEASURABLE_ATTEMPTS` runs for any other reason. A new file hash measures again.
# A meter that raises (ffmpeg could not decode the file) is such a run too (follow-up 17, Zcode #193): the take
# gets the same mark, with the error as its reason, and the attempt counts toward the same limit.
UNMEASURABLE_ATTEMPTS = 3
LENGTH_UNRECOVERABLE = "the requested length of the take cannot be recovered, so the length gate cannot be measured"


def _positive(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _next_mark(report: Any, file_hash: Any, reason: str) -> Dict[str, Any]:
    """The `qa_report.unmeasurable` mark of this run: the attempts of an earlier mark on the same
    file hash plus one (a new file starts at one)."""
    prior = report.get("unmeasurable") if isinstance(report, dict) else None
    same_file = isinstance(prior, dict) and prior.get("file_hash") == file_hash
    return {"reason": (str(reason).strip() or "stage-1 QA could not be measured")[:300],
            "attempts": (int(prior.get("attempts") or 0) if same_file else 0) + 1,
            "last_attempt_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "file_hash": file_hash}


class MusicQcCommandsMixin:
    """QC commands for `CoreService`; needs `MusicCommandsMixin` beside it."""

    def _music_qc_judge_alias(self) -> Optional[str]:
        raw = self._music_config_value("qc_judge_alias", "")
        text = str(raw).strip() if raw is not None else ""
        return text or None

    def _music_qc_ranking_enabled(self) -> bool:
        raw = self._music_config_value("qc_ranking", "true" if DEFAULT_QC_RANKING else "false")
        text = str(raw).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        logger.warning("[music] qc_ranking %r is not a boolean; using %s", raw, DEFAULT_QC_RANKING)
        return DEFAULT_QC_RANKING

    def _music_qc_ranking(self) -> Dict[str, Any]:
        return qc_.ranking_state(self._music_qc_judge_alias(), self._music_qc_ranking_enabled())

    async def _music_qc_wer(self, take: Dict[str, Any], job: Dict[str, Any]) -> Dict[str, Any]:
        """The advisory WER entry from the lyrics already stored for the take's file; reads a file,
        never transcribes."""
        target = qc_.target_lyrics_of(job)
        stored = None
        if target is not None and take.get("library_track_id"):
            try:
                stored = await (await self._music_lyrics()).load_stored(take["library_track_id"])
            except Exception:
                logger.debug("music: stored lyrics of take %s were not read", take["id"], exc_info=True)
        return qc_.lyrics_wer_entry(target, stored, take)

    async def _music_qc_store(self, take: Dict[str, Any], job: Dict[str, Any],
                              judge: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        store = await self._music_jobs()
        record = qc_.assemble(take, job, lyrics_wer=await self._music_qc_wer(take, job), judge=judge)
        return await asyncio.to_thread(store.set_take_qc, take["id"], record)

    @slow_command
    async def judge_music_take(self, take_id: Any = None) -> Dict[str, Any]:
        """Judge one take with the configured audio judge; `{take_id, qc, ranking}`. Errors:
        `invalid_take_id`, `not_found`, `no_judge` (no alias configured), `judge_not_found` (the
        alias is not loaded), `unsupported` (a job type with no rubric), `no_file`. A judge that
        ran and failed is not an error: `qc.judge.status` is `failed` with the reason."""
        try:
            tid = _need_str(take_id, "take_id", "invalid_take_id")
            alias = self._music_qc_judge_alias()
            if alias is None:
                raise MusicJobError("no_judge", "no QC judge is configured: set [music] qc_judge_alias to an "
                                                "audio-capable llama-server alias")
            providers = dict(getattr(getattr(self, "llm_manager", None), "providers", None) or {})
            provider = providers.get(alias)
            if provider is None:
                raise MusicJobError("judge_not_found", f"the QC judge alias {alias!r} is not a loaded provider")
            store = await self._music_jobs()

            def read() -> tuple:
                t = store.get_take(tid)
                return t, store.get_job(t["job_id"])
            take, job = await asyncio.to_thread(read)
            if qc_.FLOW_OF_JOB_TYPE.get(job.get("type")) is None:
                raise MusicJobError("unsupported", f"a {job.get('type')} job has no QC rubric")
            path = path_of_file_uri((take.get("file") or {}).get("uri"))
            if path is None or not await asyncio.to_thread(path.is_file):
                raise MusicJobError("no_file", f"take {tid} has no local file to judge")
            result = await qc_.judge_take(provider, alias, take=take, job=job, audio=path)
            take = await self._music_qc_store(take, job, judge=result)
            self._music_job_changed(await asyncio.to_thread(store.get_job, job["id"]))
            return {"status": "ok", "take_id": tid, "qc": take["qc"], "ranking": self._music_qc_ranking()}
        except qc_.QcError as exc:
            return _error(MusicJobError(exc.code, exc.message))
        except Exception as exc:
            return _error(exc)

    @slow_command
    async def get_music_job_qc(self, job_id: Any = None, refresh: Any = False) -> Dict[str, Any]:
        """`{job_id, takes: [{take_id, variant, qc}], ranking, judge_alias, order, caveat}`: each
        take's `qc` (null on a take made before QC), the ranking state, the configured judge alias
        (null: no judge, so no Judge button) and the take ids in display order.
        `refresh: true` assembles stage 1 again for each take whose job type has a rubric,
        keeping its judge result. Errors: `invalid_job_id`, `not_found`, `invalid_params`."""
        try:
            jid = _need_str(job_id, "job_id", "invalid_job_id")
            if not isinstance(refresh, bool):
                raise MusicJobError("invalid_params", "refresh must be true or false", field="refresh")
            store = await self._music_jobs()
            job = await asyncio.to_thread(store.get_job, jid)
            takes: List[Dict[str, Any]] = await asyncio.to_thread(store.list_takes, jid)
            if refresh and qc_.FLOW_OF_JOB_TYPE.get(job.get("type")) is not None:
                takes = [await self._music_qc_store(t, job) for t in takes]
            ranking = self._music_qc_ranking()
            return {"status": "ok", "job_id": jid,
                    "takes": [{"take_id": t["id"], "variant": t.get("variant"), "qc": t.get("qc")} for t in takes],
                    "ranking": ranking, "judge_alias": self._music_qc_judge_alias(),
                    "order": qc_.rank_takes(takes, ranking), "caveat": qc_.CAVEAT}
        except Exception as exc:
            return _error(exc)

    # -- backfill of stage-1 QA ------------------------------------------------------------

    @staticmethod
    def _backfill_terms(take: Dict[str, Any], job: Dict[str, Any]) -> Dict[str, Any]:
        """The length terms and the edit zone `measure_take_qa` needs, from what the take and its
        job hold; what is missing stays None and the gate that needs it is `not_measured`. An edit
        take keeps the control's length (`same`) and has its zone judged apart; its seam metrics
        were measured from the engine's audio at the time and cannot be had again."""
        params = job.get("params") or {}
        if take.get("kind") == "edit" and job.get("type") == "edit":
            zones = re_.zones_of(job["region"], params)
            action = job.get("action")
            cut = bool(params.get("cut_with_shift"))
            zone = None if cut else (zones["intent"] if action == re_.REMOVE_REGION else zones["splice"])
            requested = take.get("duration_requested_s")
            if not cut and not _positive(requested):
                # An edit keeps the control's length, which is the base track's: the job's params hold it
                # (`source_duration_s`, what `region_edit.normalize_params_edit` read from the library).
                # A cut shortens the take by its splice in whole frames, which nothing stored says, so a
                # cut take with no stored length stays unrecoverable (Zcode #193).
                requested = params.get("source_duration_s")
            return {"requested_s": float(requested) if _positive(requested) else None, "length_mode": LENGTH_SAME,
                    "edit": {"action": action, "zone": zone, "expects_silence": action == re_.REMOVE_REGION,
                             "seams": {}}}
        requested = take.get("duration_requested_s")
        if not isinstance(requested, (int, float)) or isinstance(requested, bool):
            requested = params.get("duration_s")
        if not isinstance(requested, (int, float)) or isinstance(requested, bool):
            requested = None
        mode = LENGTH_CAP if str(take.get("engine") or "").startswith("yue2") else LENGTH_EXACT
        return {"requested_s": float(requested) if requested is not None else None, "length_mode": mode}

    async def _backfill_one(self, store: Any, take: Dict[str, Any], job: Dict[str, Any],
                            force: bool) -> Dict[str, Any]:
        """One take's outcome: `measured`, `skipped` (already measured, no `force`), `no_file` or
        `failed` with `error`. A take that is still `not_measured` after a run is marked
        `qa_report.unmeasurable` and skipped later (see `UNMEASURABLE_ATTEMPTS`)."""
        tid = take["id"]
        entry: Dict[str, Any] = {"take_id": tid, "job_id": job["id"], "variant": take.get("variant")}
        report = take.get("qa_report")
        file_hash = (take.get("file") or {}).get("content_hash")
        if not force and isinstance(report, dict) and report.get("verdict") != js.QA_NOT_MEASURED:
            return {**entry, "outcome": "skipped", "verdict": report.get("verdict")}
        mark = report.get("unmeasurable") if isinstance(report, dict) else None
        if (not force and isinstance(mark, dict) and mark.get("file_hash") == file_hash
                and (mark.get("reason") == LENGTH_UNRECOVERABLE or mark.get("attempts", 0) >= UNMEASURABLE_ATTEMPTS)):
            return {**entry, "outcome": "skipped", "verdict": js.QA_NOT_MEASURED, "unmeasurable": mark["reason"]}
        path = path_of_file_uri((take.get("file") or {}).get("uri"))
        if path is None or not await asyncio.to_thread(path.is_file):
            return {**entry, "outcome": "no_file", "error": f"take {tid} has no local file to measure"}
        terms: Optional[Dict[str, Any]] = None
        try:
            terms = self._backfill_terms(take, job)
            meter = getattr(self, "_music_qa_meter", None) or ffmpeg_qa_meter()
            record = await asyncio.to_thread(
                meter, path, duration_s=(take.get("file") or {}).get("duration_s"),
                loudness=take.get("loudness"), **terms)
        except Exception as exc:
            # The meter itself raised (ffmpeg rc != 0 on a file it cannot decode, a QaError): marked like a
            # run that ended not_measured, so the next run does not measure the whole file again (Zcode #193).
            logger.warning("music: QA backfill of take %s failed", tid, exc_info=True)
            error = f"{type(exc).__name__}: {exc}"[:300]
            out = {**entry, "outcome": "failed", "error": error}
            mark = await self._mark_meter_failure(store, take, job, report, file_hash, terms, error)
            if mark is not None:
                out.update(unmeasurable=mark["reason"], attempts=mark["attempts"])
            return out
        try:
            new_report = dict(record["qa_report"])
            new_report["note"] = BACKFILL_NOTE + ("; the seam metrics of an edit cannot be measured again"
                                                  if "edit" in terms else "")
            if new_report["verdict"] == js.QA_NOT_MEASURED:
                gate = (new_report.get("gates") or {}).get("length") or {}
                new_report["unmeasurable"] = _next_mark(
                    report, file_hash, LENGTH_UNRECOVERABLE if terms["requested_s"] is None
                    else str(gate.get("detail") or new_report.get("note") or "stage-1 QA could not be measured"))
            js.check_qa_report(new_report)
            qc_record = None
            if qc_.FLOW_OF_JOB_TYPE.get(job.get("type")) is not None:
                qc_record = qc_.assemble({**take, "qa": record["qa"], "qa_report": new_report}, job,
                                         lyrics_wer=await self._music_qc_wer(take, job))
            stored = await asyncio.to_thread(store.set_take_qa, tid, record["qa"], new_report, qc_record)
        except Exception as exc:
            logger.warning("music: QA backfill of take %s failed", tid, exc_info=True)
            return {**entry, "outcome": "failed", "error": f"{type(exc).__name__}: {exc}"[:300]}
        return {**entry, "outcome": "measured", "verdict": stored["qa_report"]["verdict"]}

    async def _mark_meter_failure(self, store: Any, take: Dict[str, Any], job: Dict[str, Any],
                                  report: Any, file_hash: Any, terms: Optional[Dict[str, Any]],
                                  error: str) -> Optional[Dict[str, Any]]:
        """Store a `not_measured` report carrying the `unmeasurable` mark for a take whose meter
        raised, and answer the mark; None when nothing was stored. A take that already holds a
        measured report (a `force` run) keeps it: a failed re-measure never replaces a measurement."""
        if isinstance(report, dict) and report.get("verdict") != js.QA_NOT_MEASURED:
            return None
        why = f"the meter failed: {error}"
        try:
            record = not_measured_record(why, requested_s=(terms or {}).get("requested_s"),
                                         length_mode=(terms or {}).get("length_mode") or LENGTH_EXACT)
            new_report = dict(record["qa_report"])
            new_report["note"] = f"{BACKFILL_NOTE}; {why}"[:300]
            new_report["unmeasurable"] = _next_mark(report, file_hash, why)
            js.check_qa_report(new_report)
            qc_record = None
            if qc_.FLOW_OF_JOB_TYPE.get(job.get("type")) is not None:
                qc_record = qc_.assemble({**take, "qa": None, "qa_report": new_report}, job,
                                         lyrics_wer=await self._music_qc_wer(take, job))
            await asyncio.to_thread(store.set_take_qa, take["id"], None, new_report, qc_record)
        except Exception:
            logger.warning("music: the unmeasurable mark of take %s could not be stored", take["id"], exc_info=True)
            return None
        return new_report["unmeasurable"]

    @slow_command
    async def backfill_music_qa(self, job_id: Any = None, take_id: Any = None, force: Any = False) -> Dict[str, Any]:
        """Measure stage-1 QA for takes that have none: `{counts, takes: [{take_id, job_id, variant,
        outcome, verdict?, error?}]}`; outcomes `measured`, `skipped`, `no_file`, `failed`. Scope:
        `take_id`, else `job_id`, else every take of every job. A take counts as unmeasured when
        it has no `qa_report` or its verdict is `not_measured`; `force: true` measures every take
        of the scope again. One take at a time, off the event loop; a job whose takes changed is
        broadcast once. Errors: `invalid_job_id`, `invalid_take_id`, `not_found`, `invalid_params`."""
        try:
            if not isinstance(force, bool):
                raise MusicJobError("invalid_params", "force must be true or false", field="force")
            tid = _need_str(take_id, "take_id", "invalid_take_id") if take_id is not None else None
            jid = _need_str(job_id, "job_id", "invalid_job_id") if job_id is not None else None
            store = await self._music_jobs()

            def scope() -> List[tuple]:
                if tid is not None:
                    t = store.get_take(tid)
                    return [(store.get_job(t["job_id"]), [t])]
                if jid is not None:
                    return [(store.get_job(jid), store.list_takes(jid))]
                return [(j, store.list_takes(j["id"])) for j in store.list_jobs()]
            groups = await asyncio.to_thread(scope)
            lock = self.__dict__.setdefault("_music_qa_backfill_lock", asyncio.Lock())
            results: List[Dict[str, Any]] = []
            async with lock:
                for job, takes in groups:
                    changed = False
                    for take in takes:
                        outcome = await self._backfill_one(store, take, job, force)
                        results.append(outcome)
                        changed = changed or outcome["outcome"] == "measured"
                    if changed:
                        self._music_job_changed(await asyncio.to_thread(store.get_job, job["id"]))
            counts = {k: sum(1 for r in results if r["outcome"] == k)
                      for k in ("measured", "skipped", "no_file", "failed")}
            return {"status": "ok", "counts": counts, "takes": results}
        except Exception as exc:
            return _error(exc)
