"""The music job runner: runs `generate_from_text` jobs on the DPC-supervised music engines,
one at a time, oldest first (SPEC §5, §6; §12 decisions 31-35, 40; ADR-051 D30-D32, D35).

A job runs on the engine its params name (`job_schemas.engine_of`): yue2.cpp by default
(Mike #198), or acestep.cpp. Both share the steps below except where each engine's own
requests and answers differ; "yue2.cpp" at the end says how it differs. Before an engine
starts, the other music engine is made to leave the card (`MusicEngines.exclusive`: an idle
one is stopped, a busy one is waited for).

One asyncio task per backend. It takes the oldest `queued` job, runs it to `review`,
`failed` or `cancelled`, and takes the next; at start it resumes whatever is queued (a job
left `running` by a restart is settled by `MusicJobStore.open`). Each step reports to the
job store, which keeps the state machine; every store call goes through
`asyncio.to_thread`.

**One job**, in order:

1. **The LM payload.** The `/lm` body is built from the params and a seed (the job's, or
   one drawn here) and written as payload `lm.request.json` before anything is sent. A job
   resumed after a restart reuses the bytes already on disk, so its seed does not change.
2. **The card** (decision 32). `card_holders()` names the DPC engines (Strata,
   llama-server) holding the GPU card. While one does, the job stays `queued` with "waiting
   for the card: X holds it" (`set_waiting_for_card`), re-checked every `card_poll_s` and on
   every `wake()`. Nothing is stopped here: the job card's "Stop X" is the person's press,
   through the existing `stop_local_engine` command with the holder's alias.
3. `queued -> running`; the server is started when it is not running (`AceStepServer.start`,
   one try; a failure fails the job with the engine's reason and log tail).
4. **`POST /lm`**, polled through `/job` until done. The answer (the JSON with
   `audio_codes`, the only thing that makes the song repeatable: PROVENANCE §1) is written
   as payload `lm.result.json`.
5. **`POST /synth`, once, with `synth_batch_size` = the job's variants.** This is what the
   trial client did (`run_trial.py`: one request, `synth_batch_size` 2, the variants read
   in wire order). Batch size changes the audio even of variant 0 (measured 2026-11-21), so
   it is part of each take's identity: every take records `synth_batch_size` and its
   `synth_batch_index`, and the engine's seed of variant i is `seed + i` (acestep.cpp
   `synth_worker`). The body is the LM answer with `output_format: wav24`,
   `synth_batch_size`, `seed`, `inference_steps: 8` and the model names set, written as
   payload `synth.request.json` before it is sent.
6. **Files.** The multipart answer is streamed to `<DPC home>/music/generated/<job_id>/
   synth.multipart.part`; each audio part becomes `<slug>_v<n>.wav.part`, is converted to
   `<slug>_v<n>.flac.part` by ffmpeg (FLAC, 24-bit, lossless), checked lossless (the FLAC
   STREAMINFO MD5 must equal the MD5 of the WAV's PCM data) and renamed to
   `<slug>_v<n>.flac`. The WAV is not kept: FLAC holds the same samples bit for bit (that
   check), decision 34 makes FLAC the library format, and a `.wav` beside the `.flac` in
   `generated/<job>/` would be a second file of the same take for a folder scan to find
   (`<DPC home>/music` may be watched) and for `delete_generated_music_file` to leave
   behind. Its sha256 and PCM MD5 stay in the sidecar. `.part` is not an audio extension,
   so the library never sees a half-written file.
7. **Record.** Basic Vorbis tags (title, artist, genre, date, comment, `DPC_*` ids) are written by
   ffmpeg. Then the full provenance record `mp.provenance/1` (`provenance.py`, PROVENANCE §2:
   genotype with the `audio_codes` or the YuE2 score and semantic tokens, substrate with the
   weights and binaries by sha256 and the hidden genes batch size and index, phenotype with the
   lengths, loudness and PCM md5, the output chain and the lineage ids) is built from the
   payloads and the engine identity, checked by the privacy guard and the 64 KB limit, and
   written once into the staged FLAC as the Vorbis comment `DPC_PROVENANCE` by `audio_tags.py`,
   with `LYRICS` / `UNSYNCEDLYRICS` when there are lyrics. The audio is copied byte for byte; the
   STREAMINFO MD5 is checked against the engine's PCM again after the write (rule 1). The sidecar
   `<slug>_v<n>.json` (PROVENANCE §3) holds the same record plus the store's `take_id` and
   `library_track_id`. A record the guard refuses or that cannot be built is logged, the file gets
   no record, and the sidecar falls back to the older `dpc.take-sidecar/1` subset: a take is never
   lost to its metadata.
8. **Loudness.** The staged FLAC is measured with ffmpeg `ebur128` (integrated LUFS, LRA, true
   peak; `loudness.py`) before the record is built, for the record and for the review's
   level-matched A/B (decision 37). A failed
   measurement is logged and leaves the take's `loudness` null: it is advisory (decision
   29), never a reason to fail the job, and `measure_music_take_loudness` can retry it.
9. `register_generated(flac, job_id=...)`, then `add_take(... library_track_id=entry id,
   loudness=...)`.
10. `finish_job` (`review`), or `fail_job` with the reason.

**Cancel.** The person's Cancel moves the job to `cancelled` in the store; the runner reads
the state between steps and on every engine poll, asks the engine to cancel its job
(`POST /job?id=N&cancel=1`), and stops. Files already written stay (decision 35: files are
never deleted by these actions).

**Progress.** `set_progress` after each step (0.05 started, 0.1 engine up, 0.4 codes, 0.8
audio, then per take). `on_job_change(job)` is called after every change the runner makes;
the service turns it into a `music_job_updated` broadcast.

**ffmpeg** is found on PATH (`MediaTools.discover`) and run through the process registry
(`run_probe`, as the library's probes). Without it a job fails with that reason.

**yue2.cpp** (decision 40). No `/lm` pass: each variant is one `/synth` request at batch 1
(`lm_batch_size` and `synth_batch_size` 1; the CUDA flash-attention mask bug at batch > 1),
with seed and lm_seed `seed + i` (`variant_seed`). Every variant's body is written as
payload `synth.v<n>.request.json` before the card wait, so a resumed job keeps its seeds.
The answer is multipart: the engine's replay request (the score, the semantic codes and
the seeds it consumed; written as payload `synth.v<n>.result.json`, what makes the song
repeatable), the wav24 audio, and the latents. Each take records `synth_batch_size` 1,
index 0, its own seed and lm_seed and engine `yue2.cpp`; the sidecar adds the commit, the
backbone and its sha256, the adapters with the source and both split sha256s and the
strengths, `cot`, and the duration cap beside the received length. Cancel stops between
variants and cancels the engine's job in flight. A job's engine-job limit grows with the
cap (about 3.5x real time on the trial card).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mmap
import os
import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Tuple

from . import job_schemas as js
from . import provenance as prov
from .acestep import MODEL_FILES
from .audio_tags import AlreadyTagged, TagError, flac_add_comments
from .engine_base import MusicEngineError
from .engines import MusicEngines
from .identity import MediaTools, read_flac_streaminfo
from .jobs import MusicJobError, MusicJobStore
from .loudness import LoudnessError, LoudnessMeter, ffmpeg_loudness_meter
from .store import atomic_write_text, music_dir_of

logger = logging.getLogger(__name__)

LM_PAYLOAD = "lm.request.json"
LM_RESULT_PAYLOAD = "lm.result.json"
SYNTH_PAYLOAD = "synth.request.json"
SIDECAR_SCHEMA = "dpc.take-sidecar/1"
INSTRUMENTAL_LYRICS = "[Instrumental]"   # acestep.cpp's instrumental marker (request.cpp)
CODES_HZ = 5.0                            # audio_codes run at 5 Hz: received length = codes x 0.2 s
FFMPEG_TIMEOUT_S = 600.0
YUE2_BATCH_NOTE = ("batch 1 per variant: the CUDA flash-attention mask bug at batch > 1 is unfixed in ggml "
                   "ef4555b7 (ai-studio engine/yue2-cpp/BUILD.md)")
DEFAULT_CARD_POLL_S = 3.0
DEFAULT_ENGINE_POLL_S = 0.5
DEFAULT_ENGINE_JOB_TIMEOUT_S = 1800.0
_COPY_CHUNK = 4 * 1024 * 1024

Converter = Callable[[Path, Path, Mapping[str, str]], None]
CardHolders = Callable[[], List[Dict[str, Any]]]


class _Cancelled(Exception):
    """The person cancelled the job; the runner stops without touching its state."""


class _JobFailure(Exception):
    """A step failed; the message is the job's `failed` reason."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def default_card_holders() -> List[Dict[str, Any]]:
    from ..managers.llama_server_supervisor import card_holders
    return card_holders()


_WINDOWS_RESERVED = frozenset({"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                               *(f"lpt{i}" for i in range(1, 10))})


def slug_of(text: str, limit: int = 40) -> str:
    """A file-name stem from the style: Unicode letters and digits joined by '-', lower case.
    Everything else (the characters Windows, macOS and Linux forbid in a name, control
    characters, spaces and punctuation, so also trailing dots and spaces) becomes a separator.
    A reserved Windows device name (CON, NUL, COM1 ...) gets a "take-" prefix; the length is
    capped at `limit` characters; nothing left gives "take"."""
    t = unicodedata.normalize("NFC", text or "").lower()
    t = "".join(ch if (ch.isalnum() or unicodedata.category(ch).startswith("M")) else "-" for ch in t)
    t = re.sub(r"-+", "-", t).strip("-")[:limit].strip("-")
    if not t:
        return "take"
    if t.split(".")[0] in _WINDOWS_RESERVED:
        t = f"take-{t}"
    return t


# -- the request bodies --------------------------------------------------------------


def build_lm_body(params: Mapping[str, Any], seed: int) -> bytes:
    """The `/lm` body, in the shape of the trial's `request_lm.json`, plus the model
    names (the server's folder holds more than one DiT)."""
    lyrics = params.get("lyrics")
    req: Dict[str, Any] = {
        "caption": params["style"],
        "lyrics": lyrics if lyrics is not None else INSTRUMENTAL_LYRICS,
        "duration": params["duration_s"],
    }
    prep = params.get("lyrics_prep") or {}
    lang = prep.get("vocal_language") if isinstance(prep, Mapping) else None
    if isinstance(lang, str) and lang.strip():
        req["vocal_language"] = lang.strip()
    req.update({
        "seed": seed,
        "inference_steps": js.FROM_TEXT_INFERENCE_STEPS,
        "output_format": js.ENGINE_OUTPUT_FORMAT,
        "synth_batch_size": params["variants"],
        "lm_model": MODEL_FILES["lm"],
        "synth_model": MODEL_FILES["dit"],
        "vae": MODEL_FILES["vae"],
    })
    return json.dumps(req, ensure_ascii=False).encode("utf-8")


def yue2_request_name(variant: int) -> str:
    return f"synth.v{variant}.request.json"


def yue2_result_name(variant: int) -> str:
    return f"synth.v{variant}.result.json"


def variant_seed(seed: int, index: int) -> int:
    """The seed of variant `index` (0-based) of a yue2 job: `seed + index`, kept in the
    seed range."""
    return (seed + index) % (js.SEED_MAX + 1)


def build_yue2_body(params: Mapping[str, Any], seed: int, adapters: Optional[List[Dict[str, Any]]] = None) -> bytes:
    """One variant's yue2.cpp `/synth` body, in the shape of the trial's requests
    (`engine/yue2-cpp/scripts/mkreq.py`): batch 1, the token and noise seeds both `seed`,
    wav24. `adapters` is the request form, one entry per split half
    (`Yue2Server.request_adapters`). An instrumental sends empty lyrics: the model then
    composes on its own (ARCHITECTURE.md, `lyrics`)."""
    lyrics = params.get("lyrics")
    req: Dict[str, Any] = {
        "style": params["style"],
        "lyrics": lyrics if lyrics is not None else "",
        "cot": params.get("cot") or js.YUE2_COT_DEFAULT,
        "duration": float(params["duration_s"]),
        "lm_seed": seed,
        "seed": seed,
        "steps": js.YUE2_STEPS,
        "lm_batch_size": 1,
        "synth_batch_size": 1,
        "output_format": js.ENGINE_OUTPUT_FORMAT,
        "peak_clip": js.YUE2_PEAK_CLIP,
    }
    if adapters:
        req["adapters"] = [{"name": a["name"], "scale": a["scale"]} for a in adapters]
    return json.dumps(req, ensure_ascii=False).encode("utf-8")


def build_synth_body(lm_answer: Dict[str, Any], *, seed: int, batch_size: int) -> bytes:
    """The `/synth` body: the LM's answer with the trial's overrides (`run_trial.py`)."""
    r0 = dict(lm_answer)
    r0["output_format"] = js.ENGINE_OUTPUT_FORMAT
    r0["synth_batch_size"] = batch_size
    r0["seed"] = seed
    r0["inference_steps"] = js.FROM_TEXT_INFERENCE_STEPS
    r0["synth_model"] = MODEL_FILES["dit"]
    r0["vae"] = MODEL_FILES["vae"]
    return json.dumps(r0, ensure_ascii=False).encode("utf-8")


# -- WAV, multipart, FLAC --------------------------------------------------------------


@dataclass
class WavInfo:
    format_tag: int
    channels: int
    sample_rate: int
    bits: int
    data_offset: int
    data_size: int

    @property
    def duration_s(self) -> Optional[float]:
        frame = self.channels * ((self.bits + 7) // 8)
        if not frame or not self.sample_rate:
            return None
        return self.data_size / frame / self.sample_rate


def read_wav_info(path: Path) -> WavInfo:
    """The `fmt ` and `data` chunks of a RIFF/WAVE file (PCM or WAVE_FORMAT_EXTENSIBLE)."""
    with open(path, "rb") as f:
        head = f.read(12)
        if len(head) != 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            raise ValueError("not a RIFF/WAVE file")
        fmt = None
        while True:
            ch = f.read(8)
            if len(ch) < 8:
                break
            cid, size = ch[:4], int.from_bytes(ch[4:8], "little")
            if cid == b"fmt ":
                body = f.read(size)
                tag = int.from_bytes(body[0:2], "little")
                if tag == 0xFFFE and len(body) >= 26:
                    tag = int.from_bytes(body[24:26], "little")  # the sub-format GUID's first bytes
                fmt = (tag, int.from_bytes(body[2:4], "little"), int.from_bytes(body[4:8], "little"),
                       int.from_bytes(body[14:16], "little"))
                if size % 2:
                    f.seek(1, os.SEEK_CUR)
            elif cid == b"data":
                if fmt is None:
                    raise ValueError("data chunk before fmt chunk")
                offset = f.tell()
                end = f.seek(0, os.SEEK_END)
                data_size = min(size, end - offset)
                return WavInfo(fmt[0], fmt[1], fmt[2], fmt[3], offset, data_size)
            else:
                f.seek(size + (size % 2), os.SEEK_CUR)
    raise ValueError("no data chunk")


def pcm_md5_and_sha256(path: Path, info: WavInfo) -> Tuple[str, str]:
    """(MD5 of the PCM data, sha256 of the whole file): the first is what a FLAC encoder
    stores in STREAMINFO for the same samples."""
    md5, sha = hashlib.md5(), hashlib.sha256()
    with open(path, "rb") as f:
        pos = 0
        while True:
            chunk = f.read(_COPY_CHUNK)
            if not chunk:
                break
            sha.update(chunk)
            lo, hi = max(pos, info.data_offset), min(pos + len(chunk), info.data_offset + info.data_size)
            if lo < hi:
                md5.update(chunk[lo - pos:hi - pos])
            pos += len(chunk)
    return md5.hexdigest(), "sha256:" + sha.hexdigest()


def multipart_boundary(content_type: str) -> str:
    m = re.search(r'boundary="?([^";]+)"?', content_type or "")
    if not m:
        raise _JobFailure(f"the synth answer is not multipart (Content-Type {content_type!r})")
    return m.group(1)


def split_multipart_file(path: Path, boundary: str) -> List[Tuple[str, int, int]]:
    """`(headers, start, end)` of each part of a multipart body on disk, read through mmap
    so a body of hundreds of MB is never held in memory."""
    delim = b"--" + boundary.encode("latin-1")
    parts: List[Tuple[str, int, int]] = []
    if path.stat().st_size == 0:
        return parts
    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
        pos = m.find(delim)
        while pos != -1:
            after = pos + len(delim)
            if m[after:after + 2] == b"--":
                break
            hstart = after + 2 if m[after:after + 2] == b"\r\n" else after
            hend = m.find(b"\r\n\r\n", hstart)
            if hend == -1:
                raise _JobFailure("the synth answer has a part without a header end")
            start = hend + 4
            nxt = m.find(b"\r\n" + delim, start)
            if nxt == -1:
                raise _JobFailure("the synth answer ends inside a part")
            parts.append((m[hstart:hend].decode("latin-1"), start, nxt))
            pos = nxt + 2
    return parts


def copy_range(src: Path, start: int, end: int, dest: Path) -> None:
    with open(src, "rb") as fi, open(dest, "wb") as fo:
        fi.seek(start)
        left = end - start
        while left > 0:
            chunk = fi.read(min(_COPY_CHUNK, left))
            if not chunk:
                raise _JobFailure("the synth answer is shorter than its parts say")
            fo.write(chunk)
            left -= len(chunk)
        fo.flush()
        os.fsync(fo.fileno())


def ffmpeg_converter(tools: Optional[MediaTools] = None) -> Converter:
    """A converter running ffmpeg through the process registry: WAV in, 24-bit FLAC out
    (ffmpeg keeps the input's 24 bits), with the given Vorbis tags."""
    def convert(wav: Path, flac: Path, tags: Mapping[str, str]) -> None:
        found = tools or MediaTools.discover()
        if not found.ffmpeg:
            raise _JobFailure("ffmpeg is not on PATH; DPC converts the engine's wav24 to FLAC with it "
                              "(install ffmpeg and run the job again)")
        cmd = prov.flac_command(found.ffmpeg, str(wav), str(flac), tags)
        result = found.runner(cmd, timeout=FFMPEG_TIMEOUT_S)
        if result.returncode != 0:
            err = (result.stderr or b"").decode("utf-8", errors="replace").strip()[-400:]
            raise _JobFailure(f"ffmpeg could not convert {wav.name} to FLAC (rc {result.returncode}): {err}")
    return convert


# -- the runner ----------------------------------------------------------------------


class MusicJobRunner:
    """One worker task running queued `generate_from_text` jobs in FIFO order, each on the
    engine its params name."""

    def __init__(
        self,
        store: MusicJobStore,
        engines: Any,
        library: Callable[[], Awaitable[Any]],
        *,
        dpc_home: Path,
        card_holders: Optional[CardHolders] = None,
        converter: Optional[Converter] = None,
        on_job_change: Optional[Callable[[dict], Any]] = None,
        card_poll_s: float = DEFAULT_CARD_POLL_S,
        engine_poll_s: float = DEFAULT_ENGINE_POLL_S,
        engine_job_timeout_s: float = DEFAULT_ENGINE_JOB_TIMEOUT_S,
        draw_seed: Optional[Callable[[], int]] = None,
        loudness_meter: Optional[LoudnessMeter] = None,
        device_probe: Optional[Callable[[], Dict[str, Any]]] = None,
    ):
        self.store = store
        # A `MusicEngines`, a mapping of engine word to supervisor, or one supervisor.
        self.engines = MusicEngines.of(engines)
        self._library = library
        self.generated_root = music_dir_of(dpc_home) / "generated"
        self._card_holders = card_holders or default_card_holders
        self._convert = converter or ffmpeg_converter()
        self._measure = loudness_meter or ffmpeg_loudness_meter()
        self._device_probe = device_probe or prov.device_identity
        self._ffmpeg_version: Optional[str] = None
        self._ffmpeg_version_known = False
        self.on_job_change = on_job_change
        self.card_poll_s = card_poll_s
        self.engine_poll_s = engine_poll_s
        self.engine_job_timeout_s = engine_job_timeout_s
        self._draw_seed = draw_seed or (lambda: secrets.randbelow(js.SEED_MAX + 1 - js.VARIANTS_MAX))
        self._task: Optional[asyncio.Task] = None
        self._wake: Optional[asyncio.Event] = None
        self._skipped: Dict[str, str] = {}
        self.current_job_id: Optional[str] = None
        self.current_engine: Optional[str] = None

    @property
    def engine(self) -> Any:
        """The default engine's supervisor (the only one when the runner was given one)."""
        return self.engines.get()

    # -- the worker ---------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._wake = asyncio.Event()
        self._task = asyncio.ensure_future(self._work())

    def wake(self) -> None:
        """A job was proposed or cancelled, or the card may be free: look again now."""
        self._skipped.clear()  # an engine may be installed or started since a job was skipped
        if self._wake is not None:
            self._wake.set()

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    def _next_queued(self) -> Optional[str]:
        for job in self.store.list_jobs(states=[js.QUEUED], job_type=js.GENERATE_FROM_TEXT):
            if job["id"] not in self._skipped:
                return job["id"]
        return None

    async def _work(self) -> None:
        assert self._wake is not None
        while True:
            job_id = await asyncio.to_thread(self._next_queued)
            if job_id is None:
                self._wake.clear()
                if await asyncio.to_thread(self._next_queued) is None:
                    await self._wake.wait()
                continue
            self.current_job_id = job_id
            try:
                await self.run_job(job_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("music: the runner failed on job %s", job_id)
                await self._fail(job_id, f"internal error in the runner: {type(exc).__name__}: {exc}")
            finally:
                self.current_job_id = None
                self.current_engine = None
            job = await self._get(job_id)
            if job is not None and job["state"] == js.QUEUED:
                # Still queued after a run: something refused before the start. Skip it in
                # this process, or the loop would spin on it; a restart tries it again.
                self._skipped[job_id] = job.get("state_reason") or "the runner could not start it"
                logger.error("music: job %s stays queued and is skipped until a restart", job_id)

    # -- store calls ----------------------------------------------------------------

    async def _call(self, fn, *args, **kwargs):
        return await asyncio.to_thread(lambda: fn(*args, **kwargs))

    async def _get(self, job_id: str) -> Optional[dict]:
        try:
            return await self._call(self.store.get_job, job_id)
        except MusicJobError:
            return None

    def _emit(self, job: Optional[dict]) -> None:
        if job is None or self.on_job_change is None:
            return
        try:
            result = self.on_job_change(job)
            if asyncio.iscoroutine(result):
                asyncio.ensure_future(result)
        except Exception:
            logger.exception("music: the job listener failed")

    async def _change(self, fn, *args, **kwargs) -> dict:
        job = await self._call(fn, *args, **kwargs)
        self._emit(job)
        return job

    async def _cancelled(self, job_id: str) -> bool:
        job = await self._get(job_id)
        return job is None or job["state"] == js.CANCELLED

    async def _check(self, job_id: str) -> None:
        if await self._cancelled(job_id):
            raise _Cancelled()

    async def _progress(self, job_id: str, value: float) -> None:
        try:
            await self._change(self.store.set_progress, job_id, value)
        except MusicJobError:
            await self._check(job_id)
            raise

    async def _refuse(self, job_id: str, reason: str) -> None:
        """`queued -> unsupported` with the reason, so the card shows why and the job does
        not sit in `queued` for ever."""
        try:
            await self._change(self.store.refuse_job, job_id, reason[:2000])
        except MusicJobError:
            logger.warning("music: could not refuse job %s: %s", job_id, reason)

    async def _fail(self, job_id: str, reason: str) -> None:
        job = await self._get(job_id)
        if job is None or job["state"] != js.RUNNING:
            return
        try:
            await self._change(self.store.fail_job, job_id, reason[:2000])
        except MusicJobError:
            logger.warning("music: could not fail job %s: %s", job_id, reason)

    # -- one job ----------------------------------------------------------------------

    async def run_job(self, job_id: str) -> None:
        job = await self._get(job_id)
        if job is None or job["state"] != js.QUEUED or job["type"] != js.GENERATE_FROM_TEXT:
            return
        key = js.engine_of(job["params"])
        engine = self.engines.find(key)
        if engine is None:
            logger.error("music: job %s names engine %s, which this DPC has not set up", job_id, key)
            await self._refuse(job_id, f"engine {key!r} is not set up in this DPC; install or configure it and "
                                       "propose the job again")
            return
        if key == js.ENGINE_YUE2:
            try:
                seed = await self._yue2_payloads(job)
            except (MusicEngineError, MusicJobError) as exc:
                # The adapter went missing between the proposal and the run, or a payload
                # cannot be written (too large, or an unrecorded earlier file that differs).
                await self._refuse(job_id, exc.message)
                return
            lm_body = b""
        else:
            try:
                lm_body, seed = await self._lm_payload(job)
            except MusicJobError as exc:
                await self._refuse(job_id, exc.message)
                return
        if not await self._wait_for_card(job_id):
            return
        try:
            await self._change(self.store.start_job, job_id)
        except MusicJobError:
            if await self._cancelled(job_id):
                return
            raise
        self.current_engine = key
        async with engine.in_use():
            engine_job: List[Optional[str]] = [None]
            try:
                if key == js.ENGINE_YUE2:
                    await self._produce_yue2(job_id, job["params"], seed, engine, engine_job)
                else:
                    await self._produce(job_id, job["params"], lm_body, seed, engine_job, engine)
            except _Cancelled:
                if engine_job[0] is not None and engine.alive():
                    await engine.cancel(engine_job[0])
                logger.info("music: job %s cancelled", job_id)
            except (_JobFailure, MusicEngineError) as exc:
                reason = exc.message if isinstance(exc, MusicEngineError) else str(exc)
                logger.warning("music: job %s failed: %s", job_id, reason)
                if await self._cancelled(job_id):
                    return
                await self._fail(job_id, reason)

    async def _lm_payload(self, job: dict) -> Tuple[bytes, int]:
        """The acestep `/lm` body on disk, written now or reused from an earlier start."""
        job_id = job["id"]
        if any(p["name"] == LM_PAYLOAD for p in job.get("payloads") or []):
            body = await self._call(self.store.read_payload, job_id, LM_PAYLOAD)
            return body, int(json.loads(body.decode("utf-8"))["seed"])
        params = job["params"]
        seed = params["seed"] if params.get("seed") is not None else int(self._draw_seed())
        body = build_lm_body(params, seed)
        await self._call(self.store.write_payload, job_id, LM_PAYLOAD, body, endpoint="/lm")
        self._emit(await self._get(job_id))
        return body, seed

    async def _yue2_payloads(self, job: dict) -> int:
        """Every variant's yue2.cpp `/synth` body on disk before anything is sent (one per
        variant, batch 1, seed and lm_seed `seed + i`), written now or reused from an
        earlier start so the seeds do not change. Returns the job's seed."""
        job_id, params = job["id"], job["params"]
        names = [yue2_request_name(v) for v in range(1, int(params["variants"]) + 1)]
        have = {p["name"] for p in job.get("payloads") or []}
        if names[0] in have:
            first = await self._call(self.store.read_payload, job_id, names[0])
            seed = int(json.loads(first.decode("utf-8"))["seed"])
        else:
            seed = params["seed"] if params.get("seed") is not None else int(self._draw_seed())
        engine = self.engines.get(js.ENGINE_YUE2)
        request_adapters = engine.request_adapters(params.get("adapters") or [])
        wrote = False
        for index, name in enumerate(names):
            if name in have:
                continue
            body = build_yue2_body(params, variant_seed(seed, index), request_adapters)
            await self._call(self.store.write_payload, job_id, name, body, endpoint="/synth")
            wrote = True
        if wrote:
            self._emit(await self._get(job_id))
        return seed

    async def _wait_for_card(self, job_id: str) -> bool:
        """Wait while a DPC engine holds the card; False when the job was cancelled."""
        shown: Optional[List[Tuple[str, Optional[str]]]] = None
        while True:
            job = await self._get(job_id)
            if job is None or job["state"] != js.QUEUED:
                return False
            try:
                holders = list(self._card_holders() or [])
            except Exception:
                logger.exception("music: reading the card holders failed; treating the card as free")
                holders = []
            if not holders:
                if job.get("card_wait"):
                    await self._change(self.store.clear_waiting_for_card, job_id)
                return True
            names = [(h.get("alias"), h.get("engine")) for h in holders]
            if names != shown:
                try:
                    await self._change(self.store.set_waiting_for_card, job_id, holders)
                except MusicJobError:
                    if await self._cancelled(job_id):
                        return False
                    raise
                shown = names
                logger.info("music: job %s waits for the card (%s)", job_id,
                            ", ".join(str(a) for a, _ in names))
            await self._sleep_or_wake(self.card_poll_s)

    async def _sleep_or_wake(self, seconds: float) -> None:
        if self._wake is None:
            await asyncio.sleep(seconds)
            return
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    async def _start_engine(self, job_id: str, key: str, engine: Any, **start_kw) -> None:
        """Start the job's engine with the card free of the other music engine: an idle one
        is stopped, a busy one is waited for (`MusicEngines.exclusive`)."""
        async def check() -> None:
            await self._check(job_id)
        async with self.engines.exclusive(key, check=check):
            await engine.start(**start_kw)

    async def _engine_job(self, job_id: str, engine: Any, path: str, body: bytes,
                          engine_job: List[Optional[str]], dest: Optional[Path] = None,
                          timeout_s: Optional[float] = None) -> Tuple[str, Optional[bytes]]:
        """POST a body, poll `/job` until it ends (watching for the person's Cancel),
        and fetch the result."""
        await self._check(job_id)
        eid = await engine.submit(path, body)
        engine_job[0] = eid
        limit = timeout_s if timeout_s is not None else self.engine_job_timeout_s
        loop = asyncio.get_running_loop()
        deadline = loop.time() + limit
        while True:
            await self._check(job_id)
            status = await engine.job_status(eid)
            if status == "done":
                break
            if status in ("failed", "cancelled"):
                raise _JobFailure(engine.with_log_tail(f"{engine.DISPLAY} {path} job {eid} {status}"))
            if loop.time() > deadline:
                await engine.cancel(eid)
                raise _JobFailure(f"{engine.DISPLAY} {path} job {eid} did not finish within {limit:g} s")
            await asyncio.sleep(self.engine_poll_s)
        result = await engine.fetch_result(eid, dest)
        engine_job[0] = None
        return result

    # -- acestep.cpp: one /lm, then one /synth with every variant -------------------------

    async def _produce(self, job_id: str, params: Mapping[str, Any], lm_body: bytes, seed: int,
                       engine_job: List[Optional[str]], engine: Any) -> None:
        variants = int(params["variants"])
        await self._progress(job_id, 0.05)
        await self._check(job_id)
        await self._start_engine(job_id, js.ENGINE_ACESTEP, engine)
        await self._progress(job_id, 0.1)

        # LM: codes.
        _ctype, lm_raw = await self._engine_job(job_id, engine, "/lm", lm_body, engine_job)
        assert lm_raw is not None
        await self._check(job_id)
        await self._call(self.store.write_payload, job_id, LM_RESULT_PAYLOAD, lm_raw, endpoint="/lm")
        try:
            lm_answer = json.loads(lm_raw.decode("utf-8"))
            lm0 = lm_answer[0] if isinstance(lm_answer, list) else lm_answer
            if not isinstance(lm0, dict):
                raise ValueError("not an object")
        except (ValueError, IndexError, UnicodeDecodeError) as exc:
            raise _JobFailure(f"the /lm answer is not the request JSON acestep.cpp returns: {exc}")
        codes = str(lm0.get("audio_codes") or "")
        if not codes.strip():
            raise _JobFailure("the /lm answer has no audio_codes")
        await self._progress(job_id, 0.4)

        # Synth: one batch of all variants.
        synth_body = build_synth_body(lm0, seed=seed, batch_size=variants)
        await self._check(job_id)
        await self._call(self.store.write_payload, job_id, SYNTH_PAYLOAD, synth_body, endpoint="/synth")
        out_dir = self.generated_root / job_id
        out_dir.mkdir(parents=True, exist_ok=True)
        multipart = out_dir / "synth.multipart.part"
        try:
            ctype, _ = await self._engine_job(job_id, engine, "/synth", synth_body, engine_job, dest=multipart)
            await self._check(job_id)
            parts = await asyncio.to_thread(split_multipart_file, multipart, multipart_boundary(ctype))
            audio = [(h, s, e) for h, s, e in parts if "audio/" in h.lower()]
            latents = [(h, s, e) for h, s, e in parts if "audio/" not in h.lower()]
            if len(audio) != variants:
                raise _JobFailure(f"acestep.cpp returned {len(audio)} audio part(s) for synth_batch_size {variants}; "
                                  "without all of them the batch index of each cannot be told")
            await self._progress(job_id, 0.8)
            job = await self._get(job_id)
            payloads = {p["name"]: p["sha256"] for p in (job or {}).get("payloads") or []}
            identity = await asyncio.to_thread(engine.identity)
            slug = slug_of(params.get("style") or "")
            for index, (_h, start, end) in enumerate(audio):
                await self._check(job_id)
                latent_sha = None
                if index < len(latents):
                    latent_sha = await asyncio.to_thread(_range_sha256, multipart, latents[index][1], latents[index][2])
                await self._one_take(job_id, params, out_dir, slug, multipart, start, end, index, variants, seed,
                                     lm0, codes, payloads, identity, latent_sha, lm_body, synth_body)
                await self._progress(job_id, round(0.8 + 0.2 * (index + 1) / variants, 4))
        finally:
            try:
                multipart.unlink()
            except OSError:
                pass
        await self._check(job_id)
        await self._change(self.store.finish_job, job_id)

    async def _one_take(self, job_id: str, params: Mapping[str, Any], out_dir: Path, slug: str, multipart: Path,
                        start: int, end: int, index: int, batch_size: int, seed: int, lm0: Mapping[str, Any],
                        codes: str, payloads: Dict[str, str], identity: Dict[str, Any],
                        latent_sha: Optional[str], lm_body: bytes, synth_body: bytes) -> None:
        variant = index + 1
        lm_request = json.loads(lm_body.decode("utf-8"))
        synth_request = json.loads(synth_body.decode("utf-8"))
        version = identity.get("version") or ""
        lm_seed = lm0.get("lm_seed")
        codes_count = len([c for c in codes.split(",") if c.strip()])
        window = round(codes_count * (1.0 / CODES_HZ), 3)
        def make_record(facts: Dict[str, Any]) -> Dict[str, Any]:
            return prov.build_acestep_record(
                job_id=job_id, variant=variant, params=params, identity=identity, lm_request=lm_request,
                lm_result=lm0, synth_request=synth_request, seed=seed, batch_size=batch_size, batch_index=index,
                window_s=window, payloads=payloads, **facts)

        await self._store_take(
            job_id, params, out_dir, slug, variant, multipart, start, end, latent_sha=latent_sha,
            window_s=window, provenance=make_record,
            artist="DPC (acestep.cpp)",
            comment=(f"Generated by DPC with acestep.cpp {version}; job {job_id}, variant {variant}, seed {seed}, "
                     f"synth batch {index}/{batch_size}").replace("  ", " "),
            tags={"DPC_SEED": str(seed), "DPC_SYNTH_BATCH_SIZE": str(batch_size),
                  "DPC_SYNTH_BATCH_INDEX": str(index)},
            record={
                "task_type": lm0.get("task_type") or "text2music",
                "engine": {k: identity.get(k) for k in ("engine", "version", "binaries", "server_args")},
                "models": identity.get("models"),
                "payloads": payloads,
                "seed": seed,
                "variant_seed": seed + index,
                "synth_batch_size": batch_size,
                "synth_batch_index": index,
                "inference_steps": lm0.get("inference_steps", js.FROM_TEXT_INFERENCE_STEPS),
                "synth_model": MODEL_FILES["dit"],
                "lm_model": MODEL_FILES["lm"],
                "solver": lm0.get("solver"),
                "lm_seed": lm_seed,
                "audio_codes_sha256": "sha256:" + hashlib.sha256(codes.encode("utf-8")).hexdigest(),
                "codes_count": codes_count,
            },
            take={"seed": seed, "synth_batch_size": batch_size, "synth_batch_index": index, "payload": SYNTH_PAYLOAD,
                  "engine": identity.get("engine") or "acestep.cpp",
                  "lm_seed": lm_seed if isinstance(lm_seed, int) and not isinstance(lm_seed, bool)
                  and 0 <= lm_seed <= js.LM_SEED_MAX else None})

    # -- yue2.cpp: one /synth per variant, batch 1 ----------------------------------------

    async def _produce_yue2(self, job_id: str, params: Mapping[str, Any], seed: int, engine: Any,
                            engine_job: List[Optional[str]]) -> None:
        variants = int(params["variants"])
        backbone = params.get("backbone") or js.YUE2_BACKBONE_DEFAULT
        await self._progress(job_id, 0.05)
        await self._check(job_id)
        await self._start_engine(job_id, js.ENGINE_YUE2, engine, backbone=backbone,
                                 adapters=params.get("adapters") or [])
        await self._progress(job_id, 0.1)
        out_dir = self.generated_root / job_id
        out_dir.mkdir(parents=True, exist_ok=True)
        identity = await asyncio.to_thread(lambda: engine.identity(backbone=backbone,
                                                                   adapters=params.get("adapters") or []))
        slug = slug_of(params.get("style") or "")
        # About 3.5x real time on the trial card: a 360 s cap must not hit the default limit.
        timeout_s = max(self.engine_job_timeout_s, float(params["duration_s"]) * 6 + 300)
        for index in range(variants):
            variant = index + 1
            await self._check(job_id)
            name = yue2_request_name(variant)
            body = await self._call(self.store.read_payload, job_id, name)
            multipart = out_dir / f"synth.v{variant}.multipart.part"
            try:
                ctype, _ = await self._engine_job(job_id, engine, "/synth", body, engine_job, dest=multipart,
                                                  timeout_s=timeout_s)
                await self._check(job_id)
                parts = await asyncio.to_thread(split_multipart_file, multipart, multipart_boundary(ctype))
                replays = [(h, s, e) for h, s, e in parts if "application/json" in h.lower()]
                audio = [(h, s, e) for h, s, e in parts if "audio/" in h.lower()]
                latents = [(h, s, e) for h, s, e in parts if "application/octet-stream" in h.lower()]
                if len(audio) != 1:
                    raise _JobFailure(f"yue2.cpp returned {len(audio)} audio part(s) for one song at batch 1 "
                                      f"(variant {variant})")
                replay_raw = b""
                replay: Dict[str, Any] = {}
                if replays:
                    replay_raw = await asyncio.to_thread(_read_range, multipart, replays[0][1], replays[0][2])
                    try:
                        loaded = json.loads(replay_raw.decode("utf-8"))
                        replay = loaded if isinstance(loaded, dict) else {}
                    except (ValueError, UnicodeDecodeError):
                        logger.warning("music: the replay request of variant %s of job %s is not JSON",
                                       variant, job_id)
                    await self._call(self.store.write_payload, job_id, yue2_result_name(variant), replay_raw,
                                     endpoint="/synth")
                latent_sha = None
                if latents:
                    latent_sha = await asyncio.to_thread(_range_sha256, multipart, latents[0][1], latents[0][2])
                job = await self._get(job_id)
                payloads = {p["name"]: p["sha256"] for p in (job or {}).get("payloads") or []}
                sent = json.loads(body.decode("utf-8"))
                await self._yue2_take(job_id, params, out_dir, slug, multipart, audio[0][1], audio[0][2], variant,
                                      seed, sent, replay, replay_raw, payloads, identity, latent_sha)
            finally:
                try:
                    multipart.unlink()
                except OSError:
                    pass
            await self._progress(job_id, round(0.1 + 0.9 * variant / variants, 4))
        await self._check(job_id)
        await self._change(self.store.finish_job, job_id)

    async def _yue2_take(self, job_id: str, params: Mapping[str, Any], out_dir: Path, slug: str, multipart: Path,
                         start: int, end: int, variant: int, seed: int, sent: Mapping[str, Any],
                         replay: Mapping[str, Any], replay_raw: bytes, payloads: Dict[str, str],
                         identity: Dict[str, Any], latent_sha: Optional[str]) -> None:
        # The engine's replay request carries the seeds it consumed; ours were explicit, so
        # they agree, but the record keeps what the engine says it used.
        v_seed = _int_or(replay.get("seed"), sent["seed"])
        v_lm_seed = _int_or(replay.get("lm_seed"), sent["lm_seed"])
        if (v_seed, v_lm_seed) != (sent["seed"], sent["lm_seed"]):
            logger.warning("music: yue2.cpp used seeds %s/%s for variant %s of job %s, not the sent %s/%s",
                           v_lm_seed, v_seed, variant, job_id, sent["lm_seed"], sent["seed"])
        tokens = str(replay.get("semantic_tokens") or "")
        abc = str(replay.get("abc") or "")
        version = identity.get("version") or ""
        backbone = params.get("backbone") or js.YUE2_BACKBONE_DEFAULT
        adapters = identity.get("adapters") or []
        adapter_text = ", ".join(f"{a.get('name')} music {(a.get('music') or {}).get('scale')} "
                                 f"sound {(a.get('sound') or {}).get('scale')}" for a in adapters) or "none"
        def make_record(facts: Dict[str, Any]) -> Dict[str, Any]:
            return prov.build_yue2_record(
                job_id=job_id, variant=variant, params=params, identity=identity, sent=sent, replay=replay,
                seed=seed, variant_seed=v_seed, lm_seed=v_lm_seed, backbone=backbone, payloads=payloads,
                **facts)

        await self._store_take(
            job_id, params, out_dir, slug, variant, multipart, start, end, latent_sha=latent_sha,
            provenance=make_record,
            artist="DPC (yue2.cpp)",
            comment=(f"Generated by DPC with yue2.cpp {version}; job {job_id}, variant {variant}, seed {v_seed}, "
                     f"lm_seed {v_lm_seed}, backbone {backbone}, adapters {adapter_text}, batch 1").replace("  ", " "),
            tags={"DPC_ENGINE": "yue2.cpp", "DPC_SEED": str(v_seed), "DPC_LM_SEED": str(v_lm_seed),
                  "DPC_BACKBONE": backbone, "DPC_SYNTH_BATCH_SIZE": "1", "DPC_SYNTH_BATCH_INDEX": "0"},
            record={
                "task_type": "text2music",
                "engine": {k: identity.get(k) for k in ("engine", "version", "commit", "binaries", "server_args")},
                "models": identity.get("models"),
                "backbone": backbone,
                "adapters": adapters,
                "payloads": payloads,
                "seed": seed,
                "variant_seed": v_seed,
                "lm_seed": v_lm_seed,
                "lm_batch_size": 1,
                "synth_batch_size": 1,
                "synth_batch_index": 0,
                "batch_note": YUE2_BATCH_NOTE,
                "cot": sent.get("cot"),
                "steps": sent.get("steps"),
                "duration_cap_s": params.get("duration_s"),
                "replay": {"payload": yue2_result_name(variant) if replay_raw else None,
                           "sha256": ("sha256:" + hashlib.sha256(replay_raw).hexdigest()) if replay_raw else None,
                           "semantic_tokens_count": len([c for c in tokens.split(",") if c.strip()]),
                           "abc_sha256": ("sha256:" + hashlib.sha256(abc.encode("utf-8")).hexdigest()) if abc else None},
            },
            take={"seed": v_seed, "synth_batch_size": 1, "synth_batch_index": 0, "payload": yue2_request_name(variant),
                  "engine": identity.get("engine") or "yue2.cpp",
                  "lm_seed": v_lm_seed if 0 <= v_lm_seed <= js.LM_SEED_MAX else None})

    # -- a take, whichever engine made it ---------------------------------------------------

    async def _store_take(self, job_id: str, params: Mapping[str, Any], out_dir: Path, slug: str, variant: int,
                          source: Path, start: int, end: int, *, latent_sha: Optional[str], artist: str,
                          comment: str, tags: Mapping[str, str], record: Mapping[str, Any],
                          take: Mapping[str, Any], window_s: Optional[float] = None,
                          provenance: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None) -> None:
        """The engine's WAV bytes (`source[start:end]`) to a verified FLAC, a sidecar, a
        loudness measurement, a library entry and a take."""
        stem = f"{slug}_v{variant}"
        wav_part = out_dir / f"{stem}.wav.part"
        flac_part = out_dir / f"{stem}.flac.part"
        flac = out_dir / f"{stem}.flac"
        sidecar = out_dir / f"{stem}.json"
        style = " ".join(str(params.get("style") or "").split())
        title = f"{style[:60]} (v{variant})" if style else f"Generated take v{variant}"

        length_take: Dict[str, Any] = {}
        await asyncio.to_thread(copy_range, source, start, end, wav_part)
        try:
            try:
                wav = await asyncio.to_thread(read_wav_info, wav_part)
            except ValueError as exc:
                raise _JobFailure(f"variant {variant}: the engine's audio is not a WAV file: {exc}")
            wav_md5, wav_sha = await asyncio.to_thread(pcm_md5_and_sha256, wav_part, wav)
            all_tags = {
                "TITLE": title,
                "ARTIST": artist,
                "GENRE": style[:120],
                "DATE": _now()[:10],
                "COMMENT": comment,
                "DPC_JOB_ID": job_id,
                "DPC_VARIANT": str(variant),
                **tags,
            }
            await asyncio.to_thread(self._convert, wav_part, flac_part, all_tags)
            info = await asyncio.to_thread(read_flac_streaminfo, flac_part)
            if info is None:
                raise _JobFailure(f"variant {variant}: the converter did not write a FLAC file")
            if info.md5 != wav_md5:
                raise _JobFailure(f"variant {variant}: the FLAC's decoded audio (md5 {info.md5}) is not the engine's "
                                  f"PCM (md5 {wav_md5}); the conversion was not lossless")
            # Advisory only (decision 29): the received length against what the engine's codes
            # promise. Never fails the job.
            if window_s is not None and info.duration_s is not None:
                length_take = length_check_of(window_s, info.duration_s)
                if length_take["length_check"] == "mismatch":
                    logger.warning("music: variant %s of job %s is %.2f s long, its codes promise %.2f s",
                                   variant, job_id, info.duration_s, window_s)
            # Loudness of the staged FLAC: advisory (decision 29), and the provenance record holds it.
            loudness = await self._measure_loudness(flac_part, job_id, variant)
            created_at = _now()
            record_obj, record_text = await self._take_record(
                provenance, job_id, variant, flac_part, wav_part, all_tags, loudness, created_at, wav, wav_md5,
                wav_sha, latent_sha, info, length_take)
            embedded = await asyncio.to_thread(self._embed, flac_part, record_obj, record_text, wav_md5, job_id,
                                               variant)
            os.replace(flac_part, flac)
        finally:
            for p in (wav_part, flac_part):
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    logger.warning("music: could not remove %s", p, exc_info=True)

        if record_obj is not None:
            # The same record the file carries (PROVENANCE §3), plus the ids the store assigns once
            # the file exists (the file's own copy is immutable and cannot hold them).
            sidecar_record: Dict[str, Any] = {**record_obj, "take_id": None, "library_track_id": None,
                                              "embedded_in_file": embedded}
        else:
            sidecar_record = {
                "schema": SIDECAR_SCHEMA,
                "note": "the mp.provenance/1 record could not be built for this take (see the log); this is the "
                        "older subset",
                "job_id": job_id,
                "take_id": None,
                "library_track_id": None,
                "variant": variant,
                "created_at": created_at,
                **record,
                **({"engine_window_s": window_s, **length_take} if length_take else {}),
                "duration_requested_s": params.get("duration_s"),
                "duration_received_s": info.duration_s,
                "engine_output": {"format": js.ENGINE_OUTPUT_FORMAT, "sha256": wav_sha, "pcm_md5": wav_md5,
                                  "sample_rate": wav.sample_rate, "channels": wav.channels, "bits": wav.bits,
                                  "latent_sha256": latent_sha, "kept": False},
                "dpc_postprocess": {"tool": "ffmpeg", "codec": "flac",
                                    "lossless_check": "flac streaminfo md5 == wav pcm md5"},
                "output": {"container": js.LIBRARY_FORMAT, "file": flac.name, "pcm_md5": info.md5,
                           "sample_rate": info.sample_rate, "channels": info.channels,
                           "bits": info.bits_per_sample},
            }
        await asyncio.to_thread(atomic_write_text, sidecar, json.dumps(sidecar_record, ensure_ascii=False, indent=1))

        library_id: Optional[str] = None
        content_hash = f"md5:{info.md5}"
        try:
            library = await self._library()
            entry = await library.register_generated(str(flac), job_id=job_id,
                                                     lineage={"relation": "song_from_own_text", "variant": variant})
            library_id = entry.get("id")
            content_hash = entry.get("content_hash") or content_hash
        except Exception as exc:
            logger.warning("music: variant %s of job %s is not in the library: %s", variant, job_id, exc)

        await self._check(job_id)
        try:
            added = await self._call(
                self.store.add_take, job_id, variant=variant, file_uri=flac.as_uri(), content_hash=content_hash,
                title=title, sample_rate=info.sample_rate or None, channels=info.channels or None,
                duration_s=info.duration_s, library_track_id=library_id, loudness=loudness,
                **({"engine_window_s": window_s, **length_take} if length_take else {}), **dict(take))
        except MusicJobError:
            await self._check(job_id)
            raise
        self._emit(await self._get(job_id))
        sidecar_record["take_id"], sidecar_record["library_track_id"] = added["id"], library_id
        await asyncio.to_thread(atomic_write_text, sidecar, json.dumps(sidecar_record, ensure_ascii=False, indent=1))


    async def _measure_loudness(self, path: Path, job_id: str, variant: int) -> Optional[Dict[str, Any]]:
        """The take's ebur128 loudness, or None: advisory (decision 29), never a reason to fail."""
        try:
            loudness = dict(await asyncio.to_thread(self._measure, path))
            js.check_loudness(loudness)
            return loudness
        except LoudnessError as exc:
            logger.warning("music: variant %s of job %s has no loudness measurement: %s", variant, job_id, exc)
        except Exception:
            logger.exception("music: measuring the loudness of variant %s of job %s failed", variant, job_id)
        return None

    async def _ffmpeg_version_text(self) -> Optional[str]:
        """ffmpeg's version line for the record's post step; asked once, None when it cannot be read."""
        if not self._ffmpeg_version_known:
            self._ffmpeg_version_known = True

            def ask() -> Optional[str]:
                tools = MediaTools.discover()
                if not tools.ffmpeg:
                    return None
                done = tools.runner([tools.ffmpeg, "-version"], timeout=15.0)
                text = done.stdout.decode("utf-8", "replace") if isinstance(done.stdout, bytes) else (done.stdout or "")
                first = (text.splitlines() or [""])[0]
                return first.split(" Copyright")[0].strip() or None
            try:
                self._ffmpeg_version = await asyncio.to_thread(ask)
            except Exception:
                self._ffmpeg_version = None
        return self._ffmpeg_version

    async def _take_record(self, make: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]], job_id: str,
                           variant: int, flac_part: Path, wav_part: Path, tags: Mapping[str, str],
                           loudness: Optional[Dict[str, Any]], created_at: str, wav: WavInfo, wav_md5: str,
                           wav_sha: str, latent_sha: Optional[str], info: Any,
                           length_take: Mapping[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """`(record, compact JSON)` of the take's `mp.provenance/1`, or `(None, None)` when it cannot
        be built or the privacy guard refuses it: the take is kept either way, with the older
        sidecar, and the reason is logged."""
        if make is None:
            return None, None
        try:
            device = await asyncio.to_thread(self._device_probe)
            version = await self._ffmpeg_version_text()
            cmd = prov.flac_command("ffmpeg", wav_part.name, flac_part.name, tags)
            facts = {
                "created_at": created_at,
                "duration_received_s": info.duration_s,
                "loudness": loudness,
                "device": device,
                "engine_output": {"format": js.ENGINE_OUTPUT_FORMAT, "sha256": wav_sha, "pcm_md5": wav_md5,
                                  "sample_rate": wav.sample_rate, "channels": wav.channels, "bits": wav.bits,
                                  "latent_sha256": latent_sha, "kept": False},
                "postprocess": {"step": "wav24_to_flac", "tool": "ffmpeg", "tool_version": version or prov.NOT_STORED,
                                "codec": "flac", "cmd_as_sent": cmd,
                                "cmd_paths": "exact argv; the engine file and the output are file names, ffmpeg is "
                                             "its bare name (PROVENANCE rule 3)",
                                "lossless_check": "flac streaminfo md5 == wav pcm md5",
                                "tags_written_by_dpc_after": [prov.TAG, "LYRICS", "UNSYNCEDLYRICS"]},
                "output": {"container": js.LIBRARY_FORMAT, "pcm_md5": info.md5, "sample_rate": info.sample_rate,
                           "channels": info.channels, "bits": info.bits_per_sample},
            }
            record = make(facts)
            record.update(length_take)   # length_check, length_delta_s (advisory, decision 29)
            record, text = prov.finalize(record)
            return record, text
        except prov.ProvenanceError as exc:
            logger.warning("music: variant %s of job %s gets no provenance record: %s", variant, job_id, exc)
        except Exception:
            logger.exception("music: building the provenance record of variant %s of job %s failed", variant, job_id)
        return None, None

    def _embed(self, flac_part: Path, record: Optional[Dict[str, Any]], text: Optional[str], wav_md5: str,
               job_id: str, variant: int) -> bool:
        """Write `DPC_PROVENANCE` (and the lyrics tags) into the staged FLAC, once, and prove the
        decoded audio did not change. A failure to tag is logged and leaves the file as it was: the
        sidecar still holds the record (`embedded_in_file` says which). True when it is in the file."""
        if record is None or text is None:
            return False
        comments = {prov.TAG: text}
        lyrics = record.get("lyrics")
        if isinstance(lyrics, str) and lyrics.strip() and not record.get("instrumental"):
            comments["LYRICS"] = lyrics
            comments["UNSYNCEDLYRICS"] = lyrics
        try:
            flac_add_comments(flac_part, comments)
        except (AlreadyTagged, TagError, OSError) as exc:
            logger.warning("music: variant %s of job %s: the provenance record is not in the file: %s",
                           variant, job_id, exc)
            return False
        after = read_flac_streaminfo(flac_part)
        if after is None or after.md5 != wav_md5:
            raise _JobFailure(f"variant {variant}: tagging changed the FLAC's decoded audio (rule 1); the take is not kept")
        return True


LENGTH_TOLERANCE_S = 0.5


def length_check_of(window_s: float, received_s: float) -> Dict[str, Any]:
    """`{length_check: ok | mismatch, length_delta_s}`: received length minus the window the
    codes promise (`codes x 0.2 s`). Advisory."""
    delta = round(float(received_s) - float(window_s), 3)
    return {"length_check": "ok" if abs(delta) <= LENGTH_TOLERANCE_S else "mismatch", "length_delta_s": delta}


def _int_or(value: Any, fallback: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else fallback


def _read_range(path: Path, start: int, end: int) -> bytes:
    with open(path, "rb") as f:
        f.seek(start)
        return f.read(end - start)


def _range_sha256(path: Path, start: int, end: int) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        f.seek(start)
        left = end - start
        while left > 0:
            chunk = f.read(min(_COPY_CHUNK, left))
            if not chunk:
                break
            h.update(chunk)
            left -= len(chunk)
    return "sha256:" + h.hexdigest()
