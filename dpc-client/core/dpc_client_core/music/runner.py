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
2. **The card** (decision 32; ADR-052 slice S4, section 3.2). The job holds its engine's card
   lease (`music:<key>`) through `engine.in_use(...)`, one use from before `start_job` to the
   end of the job, waiting in the lease line with no deadline of its own: until the person
   cancels (`check`, read every `card_poll_s` and on every `wake()`). While it waits the job
   stays `queued` with "waiting for the card: X holds it" (`set_waiting_for_card`, the holders
   the lease names, `card_lease.holders_in_the_way`). The lease does what the matrix says for a
   music start (2.1): an idle Whisper or the idle other music engine is asked to let go, a busy
   one is waited for; an idle local LLM engine is swapped out only with `swap_when_idle` on
   (Mike #70), else waited for. The job card's "Stop X" is the person's press, through the
   existing `stop_local_engine` command with the holder's alias. The job's expected time
   (`job_schemas.music_expected_s`) is what the waiters behind it count.
3. `queued -> running`; the server is started when it is not running (`AceStepServer.start`,
   one try; a failure fails the job with the engine's reason and log tail). A child that ended
   under the job's use let the card go (L8): the start asks for it again and the job waits
   `running`, its `card_wait` shown the same way (the runner's second gate: it ends only on a
   real cancel, never on the job's state).
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
9. **Stage-1 QA** (`qa.py`, SPEC §8 "What gates a take"). The same staged FLAC goes through one
   ffmpeg pass (`astats` for the clipped samples, `silencedetect` for the silence; the loudness
   record above gives the integrated loudness and true peak, so ebur128 is not run twice) and the
   take gets a valid `mp.qa/1` as `qa` and the four gates and a verdict as `qa_report` (length
   against the request, clipping, true peak, silence). It runs off the event loop; a failed run is
   logged and gives a `qa_report` whose verdict is `not_measured` with the reason and `qa` null.
   It never fails the job and never rejects a take: it flags.
10. `register_generated(flac, job_id=...)`, then `add_take(... library_track_id=entry id,
   loudness=..., qa=..., qa_report=...)`.
11. `finish_job` (`review`), or `fail_job` with the reason.

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

**Region edits** (`edit` jobs with a built action, `region_edit.py`; ADR-051 row B4). The same queue,
oldest first. Before the card: the base track is looked up in the library (gone, missing, or its audio
hash changed since the proposal: `unsupported` with the reason, SPEC §6 `hash_mismatch`), decoded by
ffmpeg to the **control** (48 kHz stereo 24-bit WAV, `generated/<job>/control.wav.part`), and the
payload `edit.request.json` (the action, the zones, the crossfade, the seed, the base's ids and the
control's sha256 and frames) is written; a resumed job must decode to the same control. The style
caption and lyrics a repaint sends are the params', else the base file's own provenance record's, else
(lyrics only) the track's stored lyrics; a repaint with no caption anywhere is refused, since acestep.cpp
requires one. `regenerate_region` and `fix_seam` then hold acestep.cpp's card like a from-text job, write
`synth.request.json` (the repaint request) and send it with the control as one multipart `/synth`
(`submit_form`); every audio part (wav32) is spliced into the control (`region_edit.splice`). A
`remove_region` needs no engine and no card. Each take is written as a 24-bit WAV and goes the same way
as a from-text take (FLAC, lossless check, loudness, stage-1 QA with the edit zone and the seam numbers,
the `mp.provenance/1` record of `build_edit_record` naming the parent, the library with lineage
`region_edit`, an `edit` take of the job). A take the splice cannot make the control's length is not
kept (`length_mismatch`, decision 23); the job fails only when no take is usable. A `cut_with_shift`
removal re-times the base's stored lyrics onto the take (decision 27).
"""

from __future__ import annotations

import asyncio
import contextlib
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
from . import region_edit as re_
from .acestep import MODEL_FILES
from .audio_tags import AlreadyTagged, TagError, flac_add_comments, flac_read_comments
from .engine_base import MusicEngineError
from .engines import MusicEngines
from .identity import MediaTools, read_flac_streaminfo
from .jobs import MusicJobError, MusicJobStore
from .loudness import LoudnessError, LoudnessMeter, ffmpeg_loudness_meter
from .qa import LENGTH_CAP, LENGTH_EXACT, LENGTH_SAME, QaError, QaMeter, ffmpeg_qa_meter, not_measured_record
from .store import atomic_write_text, music_dir_of

logger = logging.getLogger(__name__)

LM_PAYLOAD = "lm.request.json"
LM_RESULT_PAYLOAD = "lm.result.json"
SYNTH_PAYLOAD = "synth.request.json"
EDIT_PAYLOAD = "edit.request.json"
EDIT_SCHEMA = "dpc.region-edit/1"
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
ControlDecoder = Callable[[Path, Path], None]
LyricsGet = Callable[[str], Awaitable[Optional[Dict[str, Any]]]]
LyricsSave = Callable[[str, str], Awaitable[Any]]


class _Cancelled(Exception):
    """The person cancelled the job; the runner stops without touching its state."""


class _JobFailure(Exception):
    """A step failed; the message is the job's `failed` reason."""


class _FallBack(Exception):
    """The adapters the job's engine choice picked did not load: the job moves to the choice's
    fallback engine (`MusicJobStore.fall_back_job`). `why` has no paths; `detail` is the engine's
    own message, for the log and for the failure when there is no fallback after all."""

    def __init__(self, why: str, detail: str):
        super().__init__(why)
        self.why = why
        self.detail = detail


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class _CardWait:
    """What a job shows while its engine's card lease waits (ADR-052 section 3.2): the lease's
    `on_wait` (synchronous, on the owner loop) notes the holders in the way, and one task at a time
    writes the latest to the job store (`set_waiting_for_card`, through a thread), so a burst of
    changes ends with the last one written. `settled()` waits for that task and clears what was
    shown, once the grant came."""

    def __init__(self, runner: "MusicJobRunner", job_id: str, lease: Any):
        self.runner, self.job_id, self.lease = runner, job_id, lease
        self.wanted: Optional[List[Dict[str, Any]]] = None
        self.shown: Optional[List[Dict[str, Any]]] = None
        self.task: Optional[asyncio.Future] = None

    def __call__(self, estimate: Dict[str, Any]) -> None:
        from ..managers.card_lease import holders_in_the_way
        holders = holders_in_the_way(self.lease)
        if not holders:
            return
        self.wanted = holders
        if self.task is None or self.task.done():
            self.task = asyncio.ensure_future(self._write())

    async def _write(self) -> None:
        while self.wanted is not None and self.wanted != self.shown:
            holders = self.wanted
            try:
                await self.runner._change(self.runner.store.set_waiting_for_card, self.job_id, holders)
            except MusicJobError as exc:
                # Cancelled meanwhile: the lease's check ends the wait at its next round.
                logger.info("music: job %s: its wait for the card was not shown (%s)", self.job_id, exc.message)
                return
            self.shown = holders
            logger.info("music: job %s waits for the card (%s)", self.job_id,
                        ", ".join(str(h.get("alias")) for h in holders))

    async def settled(self) -> None:
        self.wanted = None
        if self.task is not None:
            with contextlib.suppress(Exception):
                await self.task
        if self.shown is not None:
            self.shown = None
            try:
                await self.runner._change(self.runner.store.clear_waiting_for_card, self.job_id)
            except MusicJobError:
                pass  # the job moved on (cancelled): its wait went with it


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


def build_yue2_body(params: Mapping[str, Any], seed: int, adapters: Optional[List[Dict[str, Any]]] = None,
                    instrumental_lyrics: Optional[str] = None) -> bytes:
    """One variant's yue2.cpp `/synth` body, in the shape of the trial's requests
    (`engine/yue2-cpp/scripts/mkreq.py`): batch 1, the token and noise seeds both `seed`,
    wav24. `adapters` is the request form, one entry per split half
    (`Yue2Server.request_adapters`). An instrumental sends `instrumental_lyrics` (the
    instrumental adapter's tag, `Yue2Server.instrumental_lyrics_for`), else empty lyrics: the
    model then composes on its own (ARCHITECTURE.md, `lyrics`)."""
    lyrics = params.get("lyrics")
    req: Dict[str, Any] = {
        "style": params["style"],
        "lyrics": lyrics if lyrics is not None else (instrumental_lyrics or ""),
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


def ffmpeg_control_decoder(tools: Optional[MediaTools] = None) -> ControlDecoder:
    """A decoder running ffmpeg through the process registry: any audio in, the control out (48 kHz,
    stereo, 24-bit PCM WAV, the rate acestep.cpp works at)."""
    def decode(source: Path, dest: Path) -> None:
        found = tools or MediaTools.discover()
        if not found.ffmpeg:
            raise _JobFailure("ffmpeg is not on PATH; an edit decodes the track with it (install ffmpeg)")
        cmd = [found.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
               "-map", "0:a:0", "-ac", str(re_.CHANNELS), "-ar", str(re_.RATE), "-c:a", "pcm_s24le", "-f", "wav",
               str(dest)]
        result = found.runner(cmd, timeout=FFMPEG_TIMEOUT_S)
        if result.returncode != 0:
            err = (result.stderr or b"").decode("utf-8", errors="replace").strip()[-400:]
            raise _JobFailure(f"ffmpeg could not decode the track (rc {result.returncode}): {err}")
    return decode


def _base_record(path: Path) -> Dict[str, Any]:
    """The base file's own `mp.provenance/1` (a DPC take's FLAC), or {}: read as untrusted text."""
    if Path(path).suffix.lower() != ".flac":
        return {}
    try:
        raw = flac_read_comments(Path(path), max_block_bytes=2 * 1024 * 1024).get(prov.TAG)
    except Exception:
        return {}
    rec, _problem = prov.parse_embedded(raw) if raw else (None, None)
    return rec or {}


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_COPY_CHUNK), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def _lines_text(lyrics: Optional[Mapping[str, Any]]) -> str:
    lines = (lyrics or {}).get("lines") if isinstance(lyrics, Mapping) else None
    return "\n".join(str(l.get("text") or "") for l in lines or [] if isinstance(l, Mapping)).strip()


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
        converter: Optional[Converter] = None,
        on_job_change: Optional[Callable[[dict], Any]] = None,
        card_poll_s: float = DEFAULT_CARD_POLL_S,
        engine_poll_s: float = DEFAULT_ENGINE_POLL_S,
        engine_job_timeout_s: float = DEFAULT_ENGINE_JOB_TIMEOUT_S,
        draw_seed: Optional[Callable[[], int]] = None,
        loudness_meter: Optional[LoudnessMeter] = None,
        qa_meter: Optional[QaMeter] = None,
        device_probe: Optional[Callable[[], Dict[str, Any]]] = None,
        control_decoder: Optional[ControlDecoder] = None,
        lyrics_get: Optional[LyricsGet] = None,
        lyrics_save: Optional[LyricsSave] = None,
    ):
        self.store = store
        # A `MusicEngines`, a mapping of engine word to supervisor, or one supervisor.
        self.engines = MusicEngines.of(engines)
        self._library = library
        self.generated_root = music_dir_of(dpc_home) / "generated"
        self._convert = converter or ffmpeg_converter()
        self._measure = loudness_meter or ffmpeg_loudness_meter()
        self._qa_measure = qa_meter or ffmpeg_qa_meter()
        self._device_probe = device_probe or prov.device_identity
        self._decode_control = control_decoder or ffmpeg_control_decoder()
        self._lyrics_get = lyrics_get
        self._lyrics_save = lyrics_save
        self._ffmpeg_version: Optional[str] = None
        self._ffmpeg_version_known = False
        self.on_job_change = on_job_change
        # How often a job waiting for the card reads whether it was cancelled (a `wake()` reads at once).
        self.card_poll_s = card_poll_s
        self.engine_poll_s = engine_poll_s
        self.engine_job_timeout_s = engine_job_timeout_s
        self._draw_seed = draw_seed or (lambda: secrets.randbelow(js.SEED_MAX + 1 - js.VARIANTS_MAX))
        self._task: Optional[asyncio.Task] = None
        self._wake: Optional[asyncio.Event] = None
        self._skipped: Dict[str, str] = {}
        self._wakes = 0
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
        self._wakes += 1  # a job waiting for the card reads its state now (a cancel)
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

    @staticmethod
    def runs(job: Mapping[str, Any]) -> bool:
        """Whether this runner runs a job of this kind: `generate_from_text`, and an `edit` with a
        built action (`region_edit.BUILT_ACTIONS`)."""
        return job.get("type") == js.GENERATE_FROM_TEXT or (
            job.get("type") == "edit" and job.get("action") in re_.BUILT_ACTIONS)

    def _next_queued(self) -> Optional[str]:
        for job in self.store.list_jobs(states=[js.QUEUED]):
            if self.runs(job) and job["id"] not in self._skipped:
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
        if job is None or job["state"] != js.QUEUED or not self.runs(job):
            return
        if job["type"] == "edit":
            await self._run_edit(job)
            return
        started = False
        while True:
            fell: Optional[_FallBack] = None
            key = js.engine_of(job["params"])
            engine = self.engines.find(key)
            if engine is None:
                logger.error("music: job %s names engine %s, which this DPC has not set up", job_id, key)
                reason = f"engine {key!r} is not set up in this DPC; install or configure it and propose the job again"
                await (self._fail(job_id, reason) if started else self._refuse(job_id, reason))
                return
            if key == js.ENGINE_YUE2:
                try:
                    seed = await self._yue2_payloads(job)
                except (MusicEngineError, MusicJobError) as exc:
                    # The adapter went missing between the proposal and the run, or a payload
                    # cannot be written (too large, or an unrecorded earlier file that differs).
                    why = self._fall_back_why(job, exc, "is no longer offered by the adapters folders")
                    moved = await self._fall_back(job_id, why, exc.message) if why else None
                    if moved is None:
                        await self._refuse(job_id, exc.message)
                        return
                    job = moved
                    continue
                lm_body = b""
            else:
                try:
                    lm_body, seed = await self._lm_payload(job)
                except MusicJobError as exc:
                    await (self._fail(job_id, exc.message) if started else self._refuse(job_id, exc.message))
                    return
            shown = _CardWait(self, job_id, engine.card_lease)
            try:
                # The card first (ADR-052 section 3.2): one lease use for the whole job, asked for while
                # the job is queued; no deadline of its own, only the person's cancel ends the wait.
                async with engine.in_use(f"music job {job_id}", wait_s=None, check=self._card_check(job_id),
                                         on_wait=shown, expected_s=js.music_expected_s(job["params"])):
                    await shown.settled()
                    await self._run_on_the_card(job_id, job, key, engine, lm_body, seed, started=started)
            except _Cancelled:
                # Cancelled while it waited in the lease line: the wait left the line (`_leaves`).
                await shown.settled()
                logger.info("music: job %s cancelled while it waited for the card", job_id)
            except _FallBack as exc:
                fell = exc   # handled once this engine's lease use has ended
            finally:
                # A grant with nothing on the card (the job ended before its engine came up) goes back.
                engine.card_lease.release_when_empty("its job ended with no engine running", holds=engine.alive)
            if fell is None:
                return
            # The running job moves to the fallback engine, which asks for the card through its own
            # lease use; the idle engine left behind is let go by the usual exclusive start.
            moved = None if await self._cancelled(job_id) else await self._fall_back(job_id, fell.why, fell.detail)
            if moved is None:
                if not await self._cancelled(job_id):
                    await self._fail(job_id, fell.detail)
                return
            job, started = moved, True

    @staticmethod
    def _fall_back_why(job: Mapping[str, Any], exc: Exception, what: str) -> Optional[str]:
        """The path-free reason a job falls back, when `exc` says that an adapter its engine choice
        picked is missing or did not load and the choice has a fallback; else None."""
        choice = job.get("engine_choice") or {}
        if not isinstance(exc, MusicEngineError) or exc.code != "invalid_adapter" or not choice.get("fallback"):
            return None
        hit = [n for n in choice.get("adapters") or [] if n in exc.message]
        if not hit:
            return None
        return f"the {choice['rule']} adapter {', '.join(hit)} {what}"

    async def _fall_back(self, job_id: str, why: str, detail: str) -> Optional[dict]:
        """`MusicJobStore.fall_back_job`, logged; None when the store refuses it."""
        try:
            job = await self._change(self.store.fall_back_job, job_id, why)
        except MusicJobError as exc:
            logger.warning("music: job %s could not fall back (%s): %s", job_id, exc.message, detail)
            return None
        logger.warning("music: job %s falls back to %s: %s (%s)", job_id, js.engine_of(job["params"]), why, detail)
        return job

    def _card_check(self, job_id: str) -> Callable[[], Awaitable[None]]:
        """The lease's `check` for a job waiting for the card: the store is read every `card_poll_s`
        and after every `wake()` (the person's cancel wakes the runner), not on every round of the
        line; `_Cancelled` ends the wait."""
        loop = asyncio.get_running_loop()
        state = {"next_at": 0.0, "wakes": self._wakes}

        async def check() -> None:
            now = loop.time()
            if now < state["next_at"] and self._wakes == state["wakes"]:
                return
            state["next_at"], state["wakes"] = now + self.card_poll_s, self._wakes
            await self._check(job_id)
        return check

    async def _run_on_the_card(self, job_id: str, job: dict, key: str, engine: Any, lm_body: bytes,
                               seed: int, *, started: bool = False) -> None:
        """Run the job on its engine; `started` when it is already `running` (it fell back to this
        engine from another). Raises `_FallBack` when the adapters its engine choice picked do
        not load."""
        if not started:
            try:
                await self._change(self.store.start_job, job_id)
            except MusicJobError:
                if await self._cancelled(job_id):
                    return
                raise
        self.current_engine = key
        engine_job: List[Optional[str]] = [None]
        try:
            if key == js.ENGINE_YUE2:
                try:
                    await self._produce_yue2(job_id, job["params"], seed, engine, engine_job)
                except MusicEngineError as exc:
                    why = self._fall_back_why(job, exc, "did not load in yue2.cpp")
                    if why is None:
                        raise
                    raise _FallBack(why, exc.message) from exc
            else:
                await self._produce(job_id, job["params"], lm_body, seed, engine_job, engine)
        except _Cancelled:
            if engine_job[0] is not None and engine.alive():
                await engine.cancel(engine_job[0])
            logger.info("music: job %s cancelled", job_id)
        except (_JobFailure, MusicEngineError, MusicJobError) as exc:
            # A MusicJobError here is the store refusing a write of this run (a payload over the limit
            # or an earlier file that differs): a named failure, not "internal error".
            reason = exc.message if isinstance(exc, (MusicEngineError, MusicJobError)) else str(exc)
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
        instrumental_lyrics = engine.instrumental_lyrics_for(params.get("adapters") or [])
        wrote = False
        for index, name in enumerate(names):
            if name in have:
                continue
            body = build_yue2_body(params, variant_seed(seed, index), request_adapters, instrumental_lyrics)
            await self._call(self.store.write_payload, job_id, name, body, endpoint="/synth")
            wrote = True
        if wrote:
            self._emit(await self._get(job_id))
        return seed

    async def _start_engine(self, job_id: str, key: str, engine: Any, **start_kw) -> None:
        """Start the job's engine on the job's grant, with the card free of the other music engine:
        an idle one is stopped, a busy one is waited for (`MusicEngines.exclusive`, which agrees
        with the leases: the other engine's lease is let go with its stop).

        A grant the job no longer has (its engine's child ended under the job's use and let the
        card go, ADR-052 L8) is asked for again here, before any lock, with a fresh arrival, while
        the job is `running` (section 3.2, test 29): the job shows its `card_wait`, and the wait
        ends only when the card comes or the person cancels (`_Cancelled`), never on the job's
        state."""
        if not engine.card_lease.granted:
            shown = _CardWait(self, job_id, engine.card_lease)
            try:
                await engine.card_lease.acquire(f"music job {job_id}", wait_s=None, check=self._card_check(job_id),
                                                on_wait=shown)
            finally:
                await shown.settled()

        async def check() -> None:
            await self._check(job_id)
        async with self.engines.exclusive(key, check=check):
            await engine.start(**start_kw)

    async def _engine_job(self, job_id: str, engine: Any, path: str, body: bytes,
                          engine_job: List[Optional[str]], dest: Optional[Path] = None,
                          timeout_s: Optional[float] = None,
                          files: Optional[List[Tuple[str, Path, str]]] = None) -> Tuple[str, Optional[bytes]]:
        """POST a body (with `files`, a multipart form whose `request` part is the body), poll `/job`
        until it ends (watching for the person's Cancel), and fetch the result."""
        await self._check(job_id)
        eid = await (engine.submit_form(path, body, files) if files else engine.submit(path, body))
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
            # Only the requests and the answer acestep.cpp took part in: a job that fell back from YuE2
            # holds `synth.vN.request.json` files that were written and never sent (Zcode, follow-up 10
            # review, P2), and a record names only what was sent.
            sent_here = (LM_PAYLOAD, LM_RESULT_PAYLOAD, SYNTH_PAYLOAD)
            payloads = {p["name"]: p["sha256"] for p in (job or {}).get("payloads") or []
                        if p["name"] in sent_here}
            identity = await asyncio.to_thread(engine.identity)
            slug = slug_of(params.get("style") or "")
            for index, (_h, start, end) in enumerate(audio):
                await self._check(job_id)
                latent_sha = None
                if index < len(latents):
                    latent_sha = await asyncio.to_thread(_range_sha256, multipart, latents[index][1], latents[index][2])
                await self._one_take(job_id, params, out_dir, slug, multipart, start, end, index, variants, seed,
                                     lm0, codes, payloads, identity, latent_sha, lm_body, synth_body,
                                     engine_choice=(job or {}).get("engine_choice"))
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
                        latent_sha: Optional[str], lm_body: bytes, synth_body: bytes,
                        engine_choice: Optional[Mapping[str, Any]] = None) -> None:
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
                window_s=window, payloads=payloads, engine_choice=engine_choice, **facts)

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
                "engine_choice": engine_choice,
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
        # A deliberately generous safety net, not an estimate: six seconds per audio second plus five
        # minutes, far above what the trial card measured, so it only ever catches a stuck engine. It is
        # independent of the ETA the queue shows (`job_schemas.yue2_wall_s`); do not tighten it to that.
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
                                      seed, sent, replay, replay_raw, payloads, identity, latent_sha,
                                      engine_choice=(job or {}).get("engine_choice"))
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
                         identity: Dict[str, Any], latent_sha: Optional[str],
                         engine_choice: Optional[Mapping[str, Any]] = None) -> None:
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
                engine_choice=engine_choice, **facts)

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
                "engine_choice": engine_choice,
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

    # -- region edits: regenerate_region, fix_seam (acestep.cpp repaint), remove_region ---------------

    async def _run_edit(self, job: dict) -> None:
        """One edit job, from `queued` (module docstring, "Region edits")."""
        job_id, action = job["id"], job["action"]
        out_dir = self.generated_root / job_id
        control = out_dir / "control.wav.part"
        try:
            try:
                prepared = await self._edit_prepare(job, out_dir, control)
            except (_JobFailure, re_.EditError, MusicJobError, MusicEngineError) as exc:
                reason = exc.message if hasattr(exc, "message") else str(exc)
                logger.warning("music: edit job %s cannot run: %s", job_id, reason)
                if not await self._cancelled(job_id):
                    await self._refuse(job_id, reason)
                return
            if action == re_.REMOVE_REGION:
                await self._edit_on_its_own(job, prepared)
                return
            engine = self.engines.find(js.ENGINE_ACESTEP)
            if engine is None:
                await self._refuse(job_id, "acestep.cpp is not set up in this DPC; a repaint needs it")
                return
            shown = _CardWait(self, job_id, engine.card_lease)
            try:
                async with engine.in_use(f"music job {job_id}", wait_s=None, check=self._card_check(job_id),
                                         on_wait=shown, expected_s=None):
                    await shown.settled()
                    await self._edit_on_the_card(job, prepared, engine)
            except _Cancelled:
                await shown.settled()
                logger.info("music: edit job %s cancelled while it waited for the card", job_id)
            finally:
                engine.card_lease.release_when_empty("its job ended with no engine running", holds=engine.alive)
        finally:
            for p in (control, out_dir / "repaint.multipart.part"):
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    logger.warning("music: could not remove %s", p, exc_info=True)

    async def _edit_prepare(self, job: dict, out_dir: Path, control: Path) -> Dict[str, Any]:
        """The base track, the control and the payloads, while the job is still `queued`."""
        job_id, action, params = job["id"], job["action"], job["params"]
        library = await self._library()
        try:
            entry = library.get_entry(job["track_id"])
        except Exception:
            raise _JobFailure(f"the track {job['track_id']} is no longer in the library")
        path = library.get_track_path(job["track_id"])
        if path is None or not Path(path).is_file():
            raise _JobFailure("the track's file is missing; relink or rescan it and propose the edit again")
        want = params.get("source_content_hash")
        if want and entry.get("content_hash") and entry["content_hash"] != want:
            raise _JobFailure("hash_mismatch: the track's file changed since the edit was proposed; propose it again")
        # The library's hash can lag the file (a rewrite it has not polled yet): the bytes are hashed again now,
        # by the library's own identity function, before anything is decoded.
        expected = want or entry.get("content_hash")
        if expected and hasattr(library, "content_hash_of"):
            try:
                now = await library.content_hash_of(Path(path))
            except OSError as exc:
                raise _JobFailure(f"the track's file cannot be read to check its hash: {exc}")
            if now != expected:
                raise _JobFailure("hash_mismatch: the track's file holds other audio than when the edit was proposed "
                                  "(its bytes were hashed again at run start); propose it again")
        out_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._decode_control, Path(path), control)
        pcm = await asyncio.to_thread(re_.read_pcm, control)
        if pcm.rate != re_.RATE or pcm.channels != re_.CHANNELS:
            raise _JobFailure(f"the control is {pcm.rate} Hz x {pcm.channels}, not {re_.RATE} Hz x {re_.CHANNELS}")
        control_sha = await asyncio.to_thread(_file_sha256, control)
        zones = re_.zones_of(job["region"], params)
        base_rec = await asyncio.to_thread(_base_record, Path(path))
        parent_take = await self._call(self.store.find_take_by_library_track, job["track_id"])
        parent = {"role": "base", "library_track_id": job["track_id"], "content_hash": entry.get("content_hash"),
                  "version_id": job["base_version_id"], "take_id": (parent_take or {}).get("id"),
                  "job_id": (parent_take or {}).get("job_id") or ((entry.get("source") or {}).get("lineage") or {}).get("job_id"),
                  "comment_id": job.get("comment_id"),
                  "licence_key": (entry.get("provenance") or {}).get("licence_key")}
        have = {p["name"] for p in job.get("payloads") or []}
        if EDIT_PAYLOAD in have:
            body = await self._call(self.store.read_payload, job_id, EDIT_PAYLOAD)
            request = json.loads(body.decode("utf-8"))
            if request.get("control", {}).get("sha256") != control_sha:
                raise _JobFailure("the track decodes to another control than the one recorded when the job was "
                                  "first prepared; propose the edit again")
        else:
            request = {
                "schema": EDIT_SCHEMA, "action": action, "region": dict(job["region"]),
                "zones": {k: {"t_start": v[0], "t_end": v[1]} for k, v in zones.items()},
                "crossfade_ms": params["crossfade_ms"], "cut_with_shift": bool(params.get("cut_with_shift")),
                "takes": params["takes"], "engine": params["engine"],
                "base": {k: parent[k] for k in ("library_track_id", "content_hash", "version_id", "take_id")},
                "control": {"format": re_.CONTROL_FORMAT, "frames": pcm.frames, "rate": pcm.rate,
                            "channels": pcm.channels, "sha256": control_sha},
            }
            if action in re_.REPAINT_ACTIONS:
                caption, cap_from = params.get("caption"), "params"
                if not caption:
                    caption, cap_from = base_rec.get("caption_sent") or base_rec.get("caption_chosen"), "the base's record"
                if not isinstance(caption, str) or not caption.strip():
                    raise _JobFailure("no style caption for the repaint: the track carries no DPC record of its style; "
                                      "propose the edit with params.caption (the track's style)")
                lyrics, lyr_from = params.get("lyrics"), "params"
                if lyrics is None:
                    if isinstance(base_rec.get("lyrics"), str):
                        lyrics, lyr_from = base_rec["lyrics"], "the base's record"
                    elif base_rec.get("instrumental") is True:
                        lyrics, lyr_from = INSTRUMENTAL_LYRICS, "the base's record (instrumental)"
                    elif self._lyrics_get is not None:
                        try:
                            text = _lines_text(await self._lyrics_get(job["track_id"]))
                        except Exception:
                            text = ""
                        if text:
                            lyrics, lyr_from = text, "the track's stored lyrics"
                if lyrics is None:
                    lyrics, lyr_from = "", "none: the track has no known lyrics"
                seed = params["seed"] if params.get("seed") is not None else int(self._draw_seed())
                window = re_.engine_window(zones["allowed"], pcm.frames, pcm.rate)
                request.update({"seed": seed, "caption": caption.strip(), "caption_from": cap_from,
                                "lyrics": lyrics, "lyrics_from": lyr_from, "engine_window": window.as_dict()})
            body = json.dumps(request, ensure_ascii=False).encode("utf-8")
            await self._call(self.store.write_payload, job_id, EDIT_PAYLOAD, body, endpoint=None)
            if action in re_.REPAINT_ACTIONS:
                synth = re_.build_repaint_request(
                    caption=request["caption"], lyrics=request["lyrics"], window=window, seed=request["seed"],
                    takes=params["takes"], inference_steps=js.FROM_TEXT_INFERENCE_STEPS,
                    synth_model=MODEL_FILES["dit"], vae=MODEL_FILES["vae"])
                await self._call(self.store.write_payload, job_id, SYNTH_PAYLOAD, synth, endpoint="/synth")
            self._emit(await self._get(job_id))
        return {"entry": entry, "path": Path(path), "control": control, "pcm": pcm, "control_sha": control_sha,
                "zones": zones, "request": request, "parent": parent}

    async def _edit_start(self, job_id: str) -> bool:
        try:
            await self._change(self.store.start_job, job_id)
        except MusicJobError:
            if await self._cancelled(job_id):
                return False
            raise
        return True

    async def _edit_on_its_own(self, job: dict, prepared: Dict[str, Any]) -> None:
        """remove_region: no engine, no card."""
        job_id = job["id"]
        if not await self._edit_start(job_id):
            return
        try:
            await self._progress(job_id, 0.3)
            params = job["params"]
            zones = prepared["zones"]
            result = re_.remove(prepared["pcm"], zones["intent"], params["crossfade_ms"],
                                cut_with_shift=bool(params.get("cut_with_shift")))
            await self._check(job_id)
            rejected: List[str] = []
            await self._edit_take(job, prepared, 1, result, rejected, identity={"engine": prov.EDIT_ENGINE_LOCAL})
            await self._edit_finish(job_id, rejected)
        except _Cancelled:
            logger.info("music: edit job %s cancelled", job_id)
        except (_JobFailure, re_.EditError, MusicEngineError) as exc:
            await self._edit_failed(job_id, exc)

    async def _edit_on_the_card(self, job: dict, prepared: Dict[str, Any], engine: Any) -> None:
        """regenerate_region / fix_seam: one multipart `/synth` repaint with every take in its batch."""
        job_id, params = job["id"], job["params"]
        if not await self._edit_start(job_id):
            return
        self.current_engine = js.ENGINE_ACESTEP
        engine_job: List[Optional[str]] = [None]
        out_dir = self.generated_root / job_id
        multipart = out_dir / "repaint.multipart.part"
        try:
            await self._progress(job_id, 0.05)
            await self._start_engine(job_id, js.ENGINE_ACESTEP, engine)
            await self._progress(job_id, 0.1)
            synth_body = await self._call(self.store.read_payload, job_id, SYNTH_PAYLOAD)
            ctype, _ = await self._engine_job(job_id, engine, "/synth", synth_body, engine_job, dest=multipart,
                                              files=[("audio", prepared["control"], "audio/wav")])
            await self._check(job_id)
            parts = await asyncio.to_thread(split_multipart_file, multipart, multipart_boundary(ctype))
            audio = [(h, a, b) for h, a, b in parts if "audio/" in h.lower()]
            takes = int(params["takes"])
            if len(audio) != takes:
                raise _JobFailure(f"acestep.cpp returned {len(audio)} audio part(s) for a repaint of {takes} takes")
            await self._progress(job_id, 0.8)
            identity = await asyncio.to_thread(engine.identity)
            request = json.loads(synth_body.decode("utf-8"))
            rejected: List[str] = []
            for index, (_h, a, b) in enumerate(audio):
                await self._check(job_id)
                variant = index + 1
                try:
                    engine_pcm = await asyncio.to_thread(re_.read_pcm, multipart, a, b)
                    result = await asyncio.to_thread(re_.splice, prepared["pcm"], engine_pcm,
                                                     prepared["zones"]["splice"], params["crossfade_ms"])
                except re_.EditError as exc:
                    logger.warning("music: variant %s of edit job %s is not kept: %s", variant, job_id, exc.message)
                    rejected.append(f"variant {variant} rejected: {exc.message}")
                    continue
                raw_sha = await asyncio.to_thread(_range_sha256, multipart, a, b)
                await self._edit_take(job, prepared, variant, result, rejected, identity=identity, request=request,
                                      batch=(takes, index), engine_raw={"format": re_.ENGINE_OUTPUT_FORMAT,
                                                                        "sha256": raw_sha,
                                                                        "frames": engine_pcm.frames})
                await self._progress(job_id, round(0.8 + 0.2 * variant / takes, 4))
            await self._edit_finish(job_id, rejected)
        except _Cancelled:
            if engine_job[0] is not None and engine.alive():
                await engine.cancel(engine_job[0])
            logger.info("music: edit job %s cancelled", job_id)
        except (_JobFailure, re_.EditError, MusicEngineError) as exc:
            await self._edit_failed(job_id, exc)

    async def _edit_failed(self, job_id: str, exc: Exception) -> None:
        reason = exc.message if hasattr(exc, "message") else str(exc)
        logger.warning("music: edit job %s failed: %s", job_id, reason)
        if not await self._cancelled(job_id):
            await self._fail(job_id, reason)

    async def _edit_finish(self, job_id: str, rejected: List[str]) -> None:
        await self._check(job_id)
        job = await self._get(job_id)
        if not (job or {}).get("take_ids"):
            raise _JobFailure("no take is usable: " + ("; ".join(rejected) or "none was made"))
        await self._change(self.store.finish_job, job_id, "; ".join(rejected) or None)

    async def _edit_take(self, job: dict, prepared: Dict[str, Any], variant: int, result: "re_.SpliceResult",
                         rejected: List[str], *, identity: Dict[str, Any], request: Optional[Dict[str, Any]] = None,
                         batch: Optional[Tuple[int, int]] = None, engine_raw: Optional[Dict[str, Any]] = None) -> None:
        """One edit take: the spliced samples to a 24-bit WAV, the seam numbers, then the shared take path."""
        job_id, action, params = job["id"], job["action"], job["params"]
        pcm, zones, entry = prepared["pcm"], prepared["zones"], prepared["entry"]
        out_dir = self.generated_root / job_id
        spliced = out_dir / f"take_v{variant}.splice.part"
        cut = bool(params.get("cut_with_shift"))
        expected = pcm.frames - ((result.splice_frames[1] - result.splice_frames[0]) if cut else 0)
        try:
            frames = await asyncio.to_thread(re_.write_wav_s24, spliced, result.pieces, rate=pcm.rate,
                                             channels=pcm.channels)
            if frames != expected or result.frames != expected:
                rejected.append(f"variant {variant} rejected: length_mismatch ({frames} frames, the control "
                                f"{'minus the cut ' if cut else ''}is {expected})")
                logger.warning("music: %s (job %s)", rejected[-1], job_id)
                return
            window = None
            if request is not None and isinstance(prepared["request"].get("engine_window"), Mapping):
                w = prepared["request"]["engine_window"]
                window = re_.EngineWindow(int(round(w["t_start"] * re_.GRID_HZ)), int(round(w["t_end"] * re_.GRID_HZ)),
                                          w["repainting_start"], w["repainting_end"])
            seams = await asyncio.to_thread(re_.seams_entry, action, result, pcm.rate,
                                            crossfade_ms=params["crossfade_ms"], window=window)
            control_s = round(expected / pcm.rate, 6)
            zone = None if cut else (zones["intent"] if action == re_.REMOVE_REGION else zones["splice"])
            qa_override = {"requested_s": control_s, "length_mode": LENGTH_SAME,
                           "edit": {"action": action, "zone": zone, "expects_silence": action == re_.REMOVE_REGION,
                                    "seams": seams}}
            base_title = " ".join(str(entry.get("title") or "track").split())[:60]
            words = re_.region_words(action, job["region"])
            title = f"{base_title} ({words}, v{variant})"
            slug = slug_of(f"{base_title}-{action}")
            seed = (request or {}).get("seed")
            edit_rec = {
                "action": action, "region": dict(job["region"]),
                "zones": {k: {"t_start": v[0], "t_end": v[1]} for k, v in zones.items()},
                "engine_window": prepared["request"].get("engine_window"),
                "crossfade_ms": params["crossfade_ms"], "cut_with_shift": cut,
                "seams": [s.as_dict(pcm.rate) for s in result.seams], "notes": list(result.notes),
                "coherence_linear_min": re_.COHERENCE_LINEAR_MIN,
                "control": dict(prepared["request"]["control"]),
                "caption_from": prepared["request"].get("caption_from"),
                "lyrics_from": prepared["request"].get("lyrics_from"),
                "engine_raw_output": engine_raw,
                "length_rule": ("cut with shift: control minus the region (decision 27)" if cut
                                else "the control's length (decision 23)"),
            }
            job_now = await self._get(job_id)
            payloads = {p["name"]: p["sha256"] for p in (job_now or {}).get("payloads") or []}

            def make_record(facts: Dict[str, Any]) -> Dict[str, Any]:
                return prov.build_edit_record(
                    job_id=job_id, variant=variant, action=action,
                    task_type="repaint" if action in re_.REPAINT_ACTIONS else action,
                    identity=identity, parent=prepared["parent"], edit=edit_rec, duration_requested_s=control_s,
                    request=request, seed=seed, batch_size=batch[0] if batch else None,
                    batch_index=batch[1] if batch else None, payloads=payloads,
                    **{k: facts[k] for k in ("created_at", "duration_received_s", "loudness", "device",
                                             "engine_output", "postprocess", "output")})

            after = None
            if cut and self._lyrics_get is not None and self._lyrics_save is not None:
                after = self._retime_lyrics_after(job["track_id"], zones["intent"])
            elif cut:
                after = _lyrics_not_retimed
            await self._store_take(
                job_id, {"style": request.get("caption") if request else base_title, "duration_s": control_s},
                out_dir, slug, variant, spliced, 0, spliced.stat().st_size, latent_sha=None,
                provenance=make_record, artist="DPC (region edit)",
                comment=(f"Region edit by DPC ({words}) of library track {job['track_id']}; job {job_id}, "
                         f"variant {variant}" + (f", seed {seed}" if seed is not None else "")),
                tags={"DPC_EDIT_ACTION": action, "DPC_EDIT_OF": str(entry.get("content_hash") or ""),
                      "DPC_EDIT_REGION": f"{job['region']['t_start']}-{job['region']['t_end']}",
                      **({"DPC_SEED": str(seed)} if seed is not None else {})},
                record={"task_type": "repaint" if action in re_.REPAINT_ACTIONS else action,
                        "relation": "region_edit", "parent": prepared["parent"], "edit": edit_rec,
                        "payloads": payloads, "seed": seed},
                take={"seed": seed + batch[1] if seed is not None and batch else None,
                      "synth_batch_size": batch[0] if batch else None,
                      "synth_batch_index": batch[1] if batch else None,
                      "payload": SYNTH_PAYLOAD if request is not None else EDIT_PAYLOAD,
                      "engine": identity.get("engine") or prov.EDIT_ENGINE_LOCAL,
                      "duration_requested_s": control_s},
                title=title,
                lineage={"relation": "region_edit", "variant": variant, "action": action,
                         "base_track_id": job["track_id"], "region": dict(job["region"])},
                qa_override=qa_override, after_register=after)
        finally:
            try:
                spliced.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                logger.warning("music: could not remove %s", spliced, exc_info=True)

    def _retime_lyrics_after(self, base_track_id: str, region_s: Tuple[float, float]):
        """The hook that gives a cut-with-shift take the base's lyrics re-timed (decision 27)."""
        async def hook(library_id: Optional[str]) -> Dict[str, Any]:
            if not library_id:
                return {"lyrics_retimed": {"saved": False, "reason": "the take has no library entry"}}
            answer = await self._lyrics_get(base_track_id)
            lyrics = answer if isinstance(answer, Mapping) else None
            lines = list((lyrics or {}).get("lines") or [])
            if not lines:
                return {"lyrics_retimed": {"saved": False, "reason": "the edited track has no stored lyrics"}}
            moved = re_.retime_lines(lines, region_s)
            from .lyrics import format_lrc
            try:
                await self._lyrics_save(library_id, format_lrc({**lyrics, "lines": moved}))
            except Exception as exc:
                logger.warning("music: the re-timed lyrics of %s were not saved: %s", library_id, exc)
                return {"lyrics_retimed": {"saved": False, "reason": f"saving the re-timed lyrics failed: {exc}"}}
            return {"lyrics_retimed": {"saved": True, "lines_before": len(lines), "lines_after": len(moved),
                                       "cut_s": [region_s[0], region_s[1]]}}
        return hook

    # -- a take, whichever engine made it ---------------------------------------------------

    async def _store_take(self, job_id: str, params: Mapping[str, Any], out_dir: Path, slug: str, variant: int,
                          source: Path, start: int, end: int, *, latent_sha: Optional[str], artist: str,
                          comment: str, tags: Mapping[str, str], record: Mapping[str, Any],
                          take: Mapping[str, Any], window_s: Optional[float] = None,
                          provenance: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
                          title: Optional[str] = None, lineage: Optional[Dict[str, Any]] = None,
                          qa_override: Optional[Dict[str, Any]] = None,
                          after_register: Optional[Callable[[Optional[str]], Awaitable[Dict[str, Any]]]] = None) -> None:
        """The engine's WAV bytes (`source[start:end]`) to a verified FLAC, a sidecar, a
        loudness measurement, a library entry and a take. An edit take passes its `title`, its
        library `lineage`, its QA terms (`qa_override`: `requested_s`, `length_mode`, `edit`) and a
        hook run once the library entry exists, whose answer joins the sidecar."""
        stem = f"{slug}_v{variant}"
        wav_part = out_dir / f"{stem}.wav.part"
        flac_part = out_dir / f"{stem}.flac.part"
        flac = out_dir / f"{stem}.flac"
        sidecar = out_dir / f"{stem}.json"
        style = " ".join(str(params.get("style") or "").split())
        if title is None:
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
            qa_record = await self._measure_qa(flac_part, job_id, variant, params, take, info.duration_s, loudness,
                                               override=qa_override)
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
            entry = await library.register_generated(
                str(flac), job_id=job_id, lineage=lineage or {"relation": "song_from_own_text", "variant": variant})
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
                qa=qa_record["qa"], qa_report=qa_record["qa_report"],
                **({"engine_window_s": window_s, **length_take} if length_take else {}), **dict(take))
        except MusicJobError:
            await self._check(job_id)
            raise
        self._emit(await self._get(job_id))
        sidecar_record["take_id"], sidecar_record["library_track_id"] = added["id"], library_id
        if after_register is not None:
            try:
                sidecar_record.update(await after_register(library_id))
            except Exception:
                logger.exception("music: the after-take step of variant %s of job %s failed", variant, job_id)
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

    async def _measure_qa(self, path: Path, job_id: str, variant: int, params: Mapping[str, Any],
                          take: Mapping[str, Any], duration_s: Optional[float],
                          loudness: Optional[Dict[str, Any]],
                          override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """`{qa, qa_report}` of the take, never an exception: stage-1 QA flags, it does not fail
        the job. A measurement that cannot run gives `not_measured`, never a pass. YuE2's
        `duration_s` is a cap (the model ends the song itself), acestep's is the length asked for;
        an edit take passes its own terms (`override`: the control's length, mode `same`, `edit`)."""
        requested = params.get("duration_s")
        requested = float(requested) if isinstance(requested, (int, float)) and not isinstance(requested, bool) else None
        mode = LENGTH_CAP if str(take.get("engine") or "").startswith("yue2") else LENGTH_EXACT
        extra: Dict[str, Any] = {}
        if override:
            requested, mode = override.get("requested_s", requested), override.get("length_mode", mode)
            if override.get("edit") is not None:
                extra["edit"] = override["edit"]
        try:
            version = await self._ffmpeg_version_text()
            record = await asyncio.to_thread(
                self._qa_measure, path, requested_s=requested, length_mode=mode, duration_s=duration_s,
                loudness=loudness, tool_version=version, **extra)
            js.check_qa_report(record["qa_report"])
            if record["qa"] is not None:
                js.check_qa(record["qa"])
            if record["qa"] is None:
                logger.info("music: variant %s of job %s has no mp.qa/1 (not every required number was measured)",
                            variant, job_id)
            return {"qa": record["qa"], "qa_report": record["qa_report"]}
        except Exception as exc:
            if isinstance(exc, QaError):
                logger.warning("music: variant %s of job %s has no QA record: %s", variant, job_id, exc)
            else:
                logger.exception("music: measuring the QA of variant %s of job %s failed", variant, job_id)
            return not_measured_record(f"stage-1 QA could not run: {exc}"[:300], requested_s=requested,
                                       length_mode=mode)

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


async def _lyrics_not_retimed(library_id: Optional[str]) -> Dict[str, Any]:
    """A cut-with-shift take made by a runner with no lyrics store wired: the skip is recorded, not silent."""
    return {"lyrics_retimed": {"saved": False, "reason": "lyrics_hooks_not_wired: this runner has no lyrics store, "
                                                         "so the base's lyrics were not re-timed for the cut"}}


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
