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
  keeps each take's judge result.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from ..local_api import slow_command
from . import qc as qc_
from .commands import _FALSE, _TRUE, _error, _need_str, path_of_file_uri
from .jobs import MusicJobError

logger = logging.getLogger(__name__)

DEFAULT_QC_RANKING = False
QC_COMMANDS = ("judge_music_take", "get_music_job_qc")


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
        """`{job_id, takes: [{take_id, variant, qc}], ranking, order, caveat}`: each take's `qc`
        (null on a take made before QC), the ranking state and the take ids in display order.
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
                    "ranking": ranking, "order": qc_.rank_takes(takes, ranking), "caveat": qc_.CAVEAT}
        except Exception as exc:
            return _error(exc)
