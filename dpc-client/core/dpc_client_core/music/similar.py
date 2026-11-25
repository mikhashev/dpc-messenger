"""Song jobs from a source track: Same style (`generate_similar`), Cover (`cover`) and a new song from a
track's lyrics (`generate_from_lyrics`); SPEC flows 10, 15, 17 and 21, ADR-051 row B5.

Each makes `new_track` takes, like `generate_from_text`: one per variant, registered in the library as
`generated` entries with a lineage back to the source (so they join the Generated list), the source's
versions untouched. The params are `job_schemas.normalize_params_song`; the door refuses what cannot
run (`listen_only_source`, `track_not_ready`, `missing_lyrics`).

**What each engine does** (read in the engines' source, ADR-051 B5):

- `generate_similar` and `generate_from_lyrics` on acestep.cpp: text2music, `/lm` then `/synth`, the
  from-text path (`MusicJobRunner._produce`). The caption is the person's (built from the kept
  attributes); a kept BPM, key and time signature go to the LM as its metadata (`bpm`, `keyscale`,
  `timesignature`), which it never overwrites. Lyrics: reuse sends the lines, none `[Instrumental]`, new
  the given lines or "" (the LM writes them, steered by `lyrics_theme` in the caption). With `reference:
  audio` the source is sent to `/synth` as `ref_audio`, acestep.cpp's timbre reference
  (`src/pipeline-synth.cpp` `run_text2music`, `ops_encode_timbre`): whether it steers the style beyond
  timbre is not verified.
- `generate_from_lyrics` on yue2.cpp: one `/synth` per variant with the lines, the from-text path
  (`MusicJobRunner._produce_yue2`).
- `cover` on acestep.cpp: `task_type: cover`, the source as the `audio` part of one `/synth` with every
  variant, no LM (`run_cover`: the source is VAE-encoded and degraded by an FSQ round trip, the caption
  steers the style); `cover_strength` is `audio_cover_strength`. The take is as long as the source.
- `cover` on yue2.cpp: SheetSage2 transcribes the source to an ABC score (`POST /transcribe`, served
  when the server runs with `--transcriber`; `melody_only` for `cot` melody), written as payload
  `transcribe.result.json`; then each variant's `/synth` carries the score as `abc`, the kept lyrics and
  the target style, through the from-text yue2 path.

**One job.** Before the card: the source is looked up in the library (gone, or its audio hash changed
since the proposal: `unsupported` with the reason); a job that sends audio decodes the source with
ffmpeg to a 48 kHz stereo 24-bit WAV (`generated/<job>/source.wav.part`, the edit control's decoder);
payload `song.request.json` (the type, the relation, the source's ids and the decoded audio's sha256,
the caption and lyrics as sent, the seed) is written, then the engine payloads. A resumed job must
decode to the same audio. On the card the job holds the engine's lease like any music job. Takes go
through the runner's shared take path (`_store_take`: FLAC, lossless check, loudness, stage-1 QA,
`mp.qc/1`, provenance, library) dressed by `SongDress`: the title ("Cover of ..."), the library
lineage (SPEC §4: relation, source_track_id, source_name, caption, engine), and the provenance record's
`relation`, `parents` (the source, like an edit's base) and `song` block; the source's licence joins the
take's when its audio or its lyrics went in.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from . import job_schemas as js
from . import provenance as prov
from .acestep import MODEL_FILES
from .engine_base import MusicEngineError
from .jobs import MusicJobError

logger = logging.getLogger(__name__)

SONG_PAYLOAD = "song.request.json"
SONG_SCHEMA = "dpc.song-job/1"
TRANSCRIBE_PAYLOAD = "transcribe.result.json"
SOURCE_PART = "source.wav.part"
SOURCE_FORMAT = "wav, PCM 24-bit, 48 kHz, stereo (DPC's ffmpeg decode of the source)"
INSTRUMENTAL_LYRICS = "[Instrumental]"
TITLE_PREFIX = {js.GENERATE_SIMILAR: "Same style as", js.COVER: "Cover of",
                js.GENERATE_FROM_LYRICS: "From the lyrics of"}
TASK_TYPE = {js.GENERATE_SIMILAR: "text2music", js.COVER: "cover", js.GENERATE_FROM_LYRICS: "text2music"}
ENGINE_IDS = {js.ENGINE_ACESTEP: "acestep.cpp", js.ENGINE_YUE2: "yue2.cpp"}
# acestep.cpp's LM metadata a kept attribute fills (src/request.h: bpm, keyscale, timesignature).
KEEP_TO_LM = {"bpm": "bpm", "key": "keyscale", "timesig": "timesignature"}


# -- what is sent ------------------------------------------------------------------------------


def lyrics_text(params: Mapping[str, Any]) -> Optional[str]:
    """The kept or given lyrics as one text, or None when the job has none."""
    lines = params.get("lyrics")
    return "\n".join(lines) if lines else None


def caption_sent(params: Mapping[str, Any]) -> str:
    """The caption the engine gets: the person's, plus the theme of agent-free new lyrics."""
    caption = " ".join(str(params["caption"]).split())
    theme = params.get("lyrics_theme")
    return f"{caption}, lyrics about {theme}" if theme else caption


def engine_lyrics(params: Mapping[str, Any]) -> str:
    """The lyrics field as sent: the lines; acestep's `[Instrumental]` for none; "" for new lyrics
    without lines (acestep.cpp's LM writes them: docs/ARCHITECTURE.md, "Caption only")."""
    text = lyrics_text(params)
    if text is not None:
        return text
    if params["lyrics_mode"] == "none":
        return INSTRUMENTAL_LYRICS if params["engine"] == js.ENGINE_ACESTEP else ""
    return ""


def text_view(params: Mapping[str, Any]) -> Dict[str, Any]:
    """The job's params in the shape the from-text producers read (`style`, `lyrics`, ...)."""
    view: Dict[str, Any] = {"style": caption_sent(params), "lyrics": lyrics_text(params),
                            "variants": params["variants"], "duration_s": params["duration_s"],
                            "seed": params.get("seed"), "lyrics_prep": {}, "engine": params["engine"]}
    if params["engine"] == js.ENGINE_YUE2:
        view.update({"backbone": params.get("backbone") or js.YUE2_BACKBONE_DEFAULT,
                     "cot": params.get("cot") or js.YUE2_COT_DEFAULT, "adapters": []})
    return view


def _keep_metadata(params: Mapping[str, Any]) -> Dict[str, Any]:
    keep = params.get("keep") or {}
    return {lm_key: keep[k] for k, lm_key in KEEP_TO_LM.items() if k in keep}


def build_song_lm_body(params: Mapping[str, Any], seed: int) -> bytes:
    """acestep.cpp's `/lm` body for a text2music song job: the from-text body with the lyrics as sent
    and the kept metadata."""
    from .runner import build_lm_body
    req = json.loads(build_lm_body(text_view(params), seed).decode("utf-8"))
    req["lyrics"] = engine_lyrics(params)
    req.update(_keep_metadata(params))
    return json.dumps(req, ensure_ascii=False).encode("utf-8")


def build_cover_body(params: Mapping[str, Any], seed: int) -> bytes:
    """acestep.cpp's `/synth` request of a cover (the source goes beside it as the `audio` part)."""
    req: Dict[str, Any] = {
        "task_type": "cover",
        "caption": caption_sent(params),
        "lyrics": lyrics_text(params) or "",
        "audio_cover_strength": params["cover_strength"],
        **_keep_metadata(params),
        "seed": seed,
        "synth_batch_size": params["variants"],
        "inference_steps": js.FROM_TEXT_INFERENCE_STEPS,
        "output_format": js.ENGINE_OUTPUT_FORMAT,
        "synth_model": MODEL_FILES["dit"],
        "vae": MODEL_FILES["vae"],
    }
    return json.dumps(req, ensure_ascii=False).encode("utf-8")


def build_yue2_song_body(params: Mapping[str, Any], seed: int, abc: Optional[str] = None) -> bytes:
    """One variant's yue2.cpp `/synth` body: the from-text body, with the score of a cover as `abc`."""
    from .runner import build_yue2_body
    req = json.loads(build_yue2_body(text_view(params), seed).decode("utf-8"))
    if abc is not None:
        req["abc"] = abc
    return json.dumps(req, ensure_ascii=False).encode("utf-8")


# -- the take's dress ----------------------------------------------------------------------------


@dataclass
class SongDress:
    """What makes a song job's take its own on the runner's shared take path (`_store_take`)."""

    job_type: str
    params: Mapping[str, Any]
    parent: Dict[str, Any]
    song: Dict[str, Any]
    song_payload_sha: Optional[str] = None
    inherit_licence: bool = False
    extra_payloads: Dict[str, str] = field(default_factory=dict)

    @property
    def relation(self) -> str:
        return js.SONG_RELATIONS[self.job_type]

    def title(self, variant: int) -> str:
        return f"{TITLE_PREFIX[self.job_type]} {self.params['source_title'][:60]} (v{variant})"

    def lineage(self, variant: int) -> Dict[str, Any]:
        return {"relation": self.relation, "variant": variant, "source_track_id": self.params["source_track_id"],
                "source_name": self.params["source_title"], "caption": caption_sent(self.params),
                "engine": ENGINE_IDS[self.params["engine"]]}

    def patch(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        rec["relation"] = self.relation
        rec["parents"] = [dict(self.parent)]
        rec["task_type"] = TASK_TYPE[self.job_type]
        rec["song"] = dict(self.song)
        payloads = dict(rec.get("payloads") or {})
        if self.song_payload_sha:
            payloads[SONG_PAYLOAD] = self.song_payload_sha
        payloads.update(self.extra_payloads)
        rec["payloads"] = payloads
        if self.inherit_licence and isinstance(rec.get("licence"), Mapping):
            lic = prov._with_parent_licence(dict(rec["licence"]), self.parent.get("licence_key"))
            for c in lic["components"]:
                if c.get("role") == "parent":
                    c["name"] = "the source track"
            rec["licence"] = lic
        return rec

    def apply(self, variant: int, title: Optional[str], lineage: Optional[Dict[str, Any]],
              provenance: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]],
              record: Mapping[str, Any]) -> Tuple[str, Dict[str, Any], Optional[Callable], Dict[str, Any]]:
        make = None if provenance is None else (lambda facts: self.patch(provenance(facts)))
        side = {**dict(record), "relation": self.relation, "parents": [dict(self.parent)],
                "task_type": TASK_TYPE[self.job_type], "song": dict(self.song)}
        return self.title(variant), self.lineage(variant), make, side


# -- the run ------------------------------------------------------------------------------------


@dataclass
class _Prepared:
    seed: int
    dress: SongDress
    files: List[Tuple[str, Path, str]]
    lm_body: bytes = b""
    source_wav: Optional[Path] = None


async def run_song_job(runner: Any, job: dict) -> None:
    """One song job, from `queued` (module docstring)."""
    from .runner import _CardWait, _Cancelled, _JobFailure
    job_id, params = job["id"], job["params"]
    key = params["engine"]
    out_dir = runner.generated_root / job_id
    source_wav = out_dir / SOURCE_PART
    try:
        try:
            prepared = await _prepare(runner, job, out_dir, source_wav)
        except (_JobFailure, MusicJobError, MusicEngineError) as exc:
            reason = exc.message if hasattr(exc, "message") else str(exc)
            logger.warning("music: %s job %s cannot run: %s", job["type"], job_id, reason)
            if not await runner._cancelled(job_id):
                await runner._refuse(job_id, reason)
            return
        engine = runner.engines.find(key)
        if engine is None:
            await runner._refuse(job_id, f"{ENGINE_IDS[key]} is not set up in this DPC; install or configure it "
                                         "and propose the job again")
            return
        expected = js.music_expected_s(text_view(params)) if key == js.ENGINE_YUE2 else None
        shown = _CardWait(runner, job_id, engine.card_lease)
        try:
            async with engine.in_use(f"music job {job_id}", wait_s=None, check=runner._card_check(job_id),
                                     on_wait=shown, expected_s=expected):
                await shown.settled()
                await _on_the_card(runner, job, prepared, engine)
        except _Cancelled:
            await shown.settled()
            logger.info("music: %s job %s cancelled while it waited for the card", job["type"], job_id)
        finally:
            engine.card_lease.release_when_empty("its job ended with no engine running", holds=engine.alive)
    finally:
        runner._song_dress.pop(job_id, None)
        for p in (source_wav, out_dir / "cover.multipart.part"):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                logger.warning("music: could not remove %s", p, exc_info=True)


async def _prepare(runner: Any, job: dict, out_dir: Path, source_wav: Path) -> _Prepared:
    """The source, the decoded audio and the payloads, while the job is still `queued`."""
    from .runner import LM_PAYLOAD, SYNTH_PAYLOAD, _JobFailure, _file_sha256, read_wav_info, variant_seed, \
        yue2_request_name
    job_id, job_type, params = job["id"], job["type"], job["params"]
    track_id = job["track_id"]
    library = await runner._library()
    try:
        entry = library.get_entry(track_id)
    except Exception:
        raise _JobFailure(f"the source track {track_id} is no longer in the library")
    want = params.get("source_content_hash")
    if want and entry.get("content_hash") and entry["content_hash"] != want:
        raise _JobFailure("hash_mismatch: the source track's file changed since the job was proposed; propose it again")
    audio: Optional[Dict[str, Any]] = None
    files: List[Tuple[str, Path, str]] = []
    if js.song_needs_audio(job_type, params):
        path = library.get_track_path(track_id)
        if path is None or not Path(path).is_file():
            raise _JobFailure("the source track's file is missing; relink or rescan it and propose the job again")
        # The library's hash can lag the file: the bytes are hashed again now, by the library's own identity
        # function, before anything is decoded or sent (as a region edit's base is, runner._edit_prepare).
        expected = want or entry.get("content_hash")
        if expected and hasattr(library, "content_hash_of"):
            try:
                now = await library.content_hash_of(Path(path))
            except OSError as exc:
                raise _JobFailure(f"the source track's file cannot be read to check its hash: {exc}")
            if now != expected:
                raise _JobFailure("hash_mismatch: the source track's file holds other audio than when the job was "
                                  "proposed (its bytes were hashed again at run start); propose it again")
        out_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(runner._decode_control, Path(path), source_wav)
        try:
            wav = await asyncio.to_thread(read_wav_info, source_wav)
        except ValueError as exc:
            raise _JobFailure(f"the source did not decode to a WAV file: {exc}")
        block = max(1, wav.channels * wav.bits // 8)
        audio = {"format": SOURCE_FORMAT, "frames": wav.data_size // block, "rate": wav.sample_rate,
                 "channels": wav.channels, "sha256": await asyncio.to_thread(_file_sha256, source_wav)}
        files = [("audio" if job_type == js.COVER else "ref_audio", source_wav, "audio/wav")]

    parent_take = await runner._call(runner.store.find_take_by_library_track, track_id)
    parent = {"role": "source", "library_track_id": track_id, "content_hash": entry.get("content_hash"),
              "version_id": job["base_version_id"], "take_id": (parent_take or {}).get("id"),
              "job_id": (parent_take or {}).get("job_id")
              or ((entry.get("source") or {}).get("lineage") or {}).get("job_id"),
              "comment_id": None, "licence_key": (entry.get("provenance") or {}).get("licence_key")}

    have = {p["name"] for p in job.get("payloads") or []}
    if SONG_PAYLOAD in have:
        request = json.loads((await runner._call(runner.store.read_payload, job_id, SONG_PAYLOAD)).decode("utf-8"))
        if (request.get("source_audio") or {}).get("sha256") != (audio or {}).get("sha256"):
            raise _JobFailure("the source decodes to other audio than the one recorded when the job was first "
                              "prepared; propose the job again")
        seed = int(request["seed"])
    else:
        seed = params["seed"] if params.get("seed") is not None else int(runner._draw_seed())
        request = {
            "schema": SONG_SCHEMA, "type": job_type, "relation": js.SONG_RELATIONS[job_type],
            "engine": params["engine"], "task_type": TASK_TYPE[job_type],
            "source": {k: parent[k] for k in ("library_track_id", "content_hash", "version_id", "take_id", "job_id")},
            "source_title": params["source_title"], "source_audio": audio,
            "caption_sent": caption_sent(params), "lyrics_sent": engine_lyrics(params),
            "lyrics_mode": params["lyrics_mode"], "keep": dict(params.get("keep") or {}),
            "reference": params["reference"], "seed": seed,
        }
        body = json.dumps(request, ensure_ascii=False).encode("utf-8")
        await runner._call(runner.store.write_payload, job_id, SONG_PAYLOAD, body, endpoint=None)
        have.add(SONG_PAYLOAD)

    lm_body = b""
    key = params["engine"]
    if key == js.ENGINE_ACESTEP and job_type != js.COVER:
        if LM_PAYLOAD in have:
            lm_body = await runner._call(runner.store.read_payload, job_id, LM_PAYLOAD)
        else:
            lm_body = build_song_lm_body(params, seed)
            await runner._call(runner.store.write_payload, job_id, LM_PAYLOAD, lm_body, endpoint="/lm")
    elif key == js.ENGINE_ACESTEP and SYNTH_PAYLOAD not in have:
        await runner._call(runner.store.write_payload, job_id, SYNTH_PAYLOAD, build_cover_body(params, seed),
                           endpoint="/synth")
    elif key == js.ENGINE_YUE2 and job_type != js.COVER:
        for index in range(int(params["variants"])):
            name = yue2_request_name(index + 1)
            if name not in have:
                await runner._call(runner.store.write_payload, job_id, name,
                                   build_yue2_song_body(params, variant_seed(seed, index)), endpoint="/synth")
    runner._emit(await runner._get(job_id))

    now = await runner._get(job_id)
    song_sha = next((p["sha256"] for p in (now or {}).get("payloads") or [] if p["name"] == SONG_PAYLOAD), None)
    song = {"type": job_type, "relation": js.SONG_RELATIONS[job_type], "keep": dict(params.get("keep") or {}),
            "lyrics_mode": params["lyrics_mode"], "reference": params["reference"],
            "reference_uri": params.get("reference_uri"), "caption": params["caption"],
            "caption_basis": params.get("caption_basis"), "source_audio": audio}
    for k in ("kept", "melody_source", "cover_strength", "cot"):
        if k in params:
            song[k] = params[k]
    if job_type != js.COVER and params["reference"] == "audio":
        song["reference_role"] = "acestep.cpp ref_audio: a timbre reference; style steering not verified"
    dress = SongDress(job_type, params, parent, song, song_payload_sha=song_sha,
                      inherit_licence=audio is not None or params["lyrics_mode"] == "reuse")
    return _Prepared(seed=seed, dress=dress, files=files, lm_body=lm_body,
                     source_wav=source_wav if audio is not None else None)


async def _on_the_card(runner: Any, job: dict, prepared: _Prepared, engine: Any) -> None:
    from .runner import _Cancelled, _JobFailure
    job_id, params, job_type = job["id"], job["params"], job["type"]
    key = params["engine"]
    try:
        await runner._change(runner.store.start_job, job_id)
    except MusicJobError:
        if await runner._cancelled(job_id):
            return
        raise
    runner.current_engine = key
    runner._song_dress[job_id] = prepared.dress
    engine_job: List[Optional[str]] = [None]
    try:
        if job_type == js.COVER and key == js.ENGINE_ACESTEP:
            await _cover_on_acestep(runner, job, prepared, engine, engine_job)
        elif job_type == js.COVER:
            await _cover_on_yue2(runner, job, prepared, engine, engine_job)
        elif key == js.ENGINE_YUE2:
            await runner._produce_yue2(job_id, text_view(params), prepared.seed, engine, engine_job)
        else:
            await runner._produce(job_id, text_view(params), prepared.lm_body, prepared.seed, engine_job, engine,
                                  synth_files=prepared.files or None)
    except _Cancelled:
        if engine_job[0] is not None and engine.alive():
            await engine.cancel(engine_job[0])
        logger.info("music: %s job %s cancelled", job_type, job_id)
    except (_JobFailure, MusicEngineError, MusicJobError) as exc:
        reason = exc.message if hasattr(exc, "message") else str(exc)
        logger.warning("music: %s job %s failed: %s", job_type, job_id, reason)
        if not await runner._cancelled(job_id):
            await runner._fail(job_id, reason)


async def _cover_on_acestep(runner: Any, job: dict, prepared: _Prepared, engine: Any,
                            engine_job: List[Optional[str]]) -> None:
    """One multipart `/synth` (task cover, the source as `audio`) with every variant in its batch."""
    from .runner import SYNTH_PAYLOAD, _JobFailure, _range_sha256, multipart_boundary, slug_of, \
        split_multipart_file
    job_id, params = job["id"], job["params"]
    variants = int(params["variants"])
    view = text_view(params)
    await runner._progress(job_id, 0.05)
    await runner._start_engine(job_id, js.ENGINE_ACESTEP, engine)
    await runner._progress(job_id, 0.1)
    synth_body = await runner._call(runner.store.read_payload, job_id, SYNTH_PAYLOAD)
    out_dir = runner.generated_root / job_id
    multipart = out_dir / "cover.multipart.part"
    ctype, _ = await runner._engine_job(job_id, engine, "/synth", synth_body, engine_job, dest=multipart,
                                        files=prepared.files)
    await runner._check(job_id)
    parts = await asyncio.to_thread(split_multipart_file, multipart, multipart_boundary(ctype))
    audio = [(h, a, b) for h, a, b in parts if "audio/" in h.lower()]
    latents = [(h, a, b) for h, a, b in parts if "audio/" not in h.lower()]
    if len(audio) != variants:
        raise _JobFailure(f"acestep.cpp returned {len(audio)} audio part(s) for a cover of {variants} variants")
    await runner._progress(job_id, 0.8)
    identity = await asyncio.to_thread(engine.identity)
    request = json.loads(synth_body.decode("utf-8"))
    now = await runner._get(job_id)
    payloads = {p["name"]: p["sha256"] for p in (now or {}).get("payloads") or []
                if p["name"] in (SONG_PAYLOAD, SYNTH_PAYLOAD)}
    seed = prepared.seed
    slug = slug_of(f"{params['source_title']}-cover")
    version = identity.get("version") or ""
    for index, (_h, a, b) in enumerate(audio):
        await runner._check(job_id)
        variant = index + 1
        latent_sha = None
        if index < len(latents):
            latent_sha = await asyncio.to_thread(_range_sha256, multipart, latents[index][1], latents[index][2])

        def make_record(facts: Dict[str, Any], index: int = index, variant: int = variant) -> Dict[str, Any]:
            return prov.build_acestep_record(
                job_id=job_id, variant=variant, params=view, identity=identity, lm_request=request, lm_result={},
                synth_request=request, seed=seed, batch_size=variants, batch_index=index, window_s=None,
                payloads=payloads, **facts)

        await runner._store_take(
            job_id, view, out_dir, slug, variant, multipart, a, b, latent_sha=latent_sha, provenance=make_record,
            artist="DPC (acestep.cpp)",
            comment=(f"Cover by DPC with acestep.cpp {version} of library track {job['track_id']}; job {job_id}, "
                     f"variant {variant}, seed {seed}, synth batch {index}/{variants}").replace("  ", " "),
            tags={"DPC_SEED": str(seed), "DPC_SYNTH_BATCH_SIZE": str(variants), "DPC_SYNTH_BATCH_INDEX": str(index),
                  "DPC_COVER_OF": str(params.get("source_content_hash") or "")},
            record={"engine": {k: identity.get(k) for k in ("engine", "version", "binaries", "server_args")},
                    "models": identity.get("models"), "payloads": payloads, "seed": seed,
                    "variant_seed": seed + index, "synth_batch_size": variants, "synth_batch_index": index,
                    "inference_steps": js.FROM_TEXT_INFERENCE_STEPS, "synth_model": MODEL_FILES["dit"],
                    "audio_cover_strength": params["cover_strength"]},
            take={"seed": seed, "synth_batch_size": variants, "synth_batch_index": index, "payload": SYNTH_PAYLOAD,
                  "engine": identity.get("engine") or "acestep.cpp", "lm_seed": None})
        await runner._progress(job_id, round(0.8 + 0.2 * variant / variants, 4))
    await runner._check(job_id)
    await runner._change(runner.store.finish_job, job_id)


async def _transcribe(runner: Any, job_id: str, engine: Any, wav: Path, melody_only: bool,
                      engine_job: List[Optional[str]]) -> bytes:
    """yue2.cpp `POST /transcribe` (the source as the `audio` part, `melody_only` as a field), polled to
    its end like any engine job; the answer's bytes (`{"abc": ...}`)."""
    from .runner import _JobFailure
    await runner._check(job_id)
    fields = {"melody_only": "1"} if melody_only else None
    eid = await engine.submit_form("/transcribe", b"{}", [("audio", wav, "audio/wav")], fields=fields)
    engine_job[0] = eid
    loop = asyncio.get_running_loop()
    deadline = loop.time() + runner.engine_job_timeout_s
    while True:
        await runner._check(job_id)
        status = await engine.job_status(eid)
        if status == "done":
            break
        if status in ("failed", "cancelled"):
            raise _JobFailure(engine.with_log_tail(f"{engine.DISPLAY} /transcribe job {eid} {status}"))
        if loop.time() > deadline:
            await engine.cancel(eid)
            raise _JobFailure(f"{engine.DISPLAY} /transcribe job {eid} did not finish within "
                              f"{runner.engine_job_timeout_s:g} s")
        await asyncio.sleep(runner.engine_poll_s)
    _ctype, raw = await engine.fetch_result(eid, None)
    engine_job[0] = None
    return raw or b""


async def _cover_on_yue2(runner: Any, job: dict, prepared: _Prepared, engine: Any,
                         engine_job: List[Optional[str]]) -> None:
    """SheetSage2's score of the source, then the from-text yue2 path with the score as each variant's `abc`."""
    from .runner import _JobFailure, variant_seed, yue2_request_name
    job_id, params = job["id"], job["params"]
    backbone = params.get("backbone") or js.YUE2_BACKBONE_DEFAULT
    await runner._progress(job_id, 0.02)
    await runner._start_engine(job_id, js.ENGINE_YUE2, engine, backbone=backbone, adapters=[])
    problem = getattr(engine, "transcriber_problem", lambda: "the supervisor does not report a transcriber")()
    if problem:
        raise _JobFailure(f"yue2.cpp cannot transcribe the source: {problem}")
    have = {p["name"] for p in (await runner._get(job_id) or {}).get("payloads") or []}
    if TRANSCRIBE_PAYLOAD in have:
        raw = await runner._call(runner.store.read_payload, job_id, TRANSCRIBE_PAYLOAD)
    else:
        raw = await _transcribe(runner, job_id, engine, prepared.source_wav, params.get("cot") == "melody", engine_job)
        await runner._call(runner.store.write_payload, job_id, TRANSCRIBE_PAYLOAD, raw, endpoint="/transcribe")
    try:
        abc = json.loads(raw.decode("utf-8")).get("abc")
    except (ValueError, UnicodeDecodeError, AttributeError) as exc:
        raise _JobFailure(f"the /transcribe answer is not the JSON yue2.cpp returns: {exc}")
    if not isinstance(abc, str) or not abc.strip():
        raise _JobFailure("SheetSage2 heard no score in the source; a cover on yue2.cpp needs one (try acestep)")
    for index in range(int(params["variants"])):
        name = yue2_request_name(index + 1)
        if name not in have:
            await runner._call(runner.store.write_payload, job_id, name,
                               build_yue2_song_body(params, variant_seed(prepared.seed, index), abc), endpoint="/synth")
    now = await runner._get(job_id)
    sha = next((p["sha256"] for p in (now or {}).get("payloads") or [] if p["name"] == TRANSCRIBE_PAYLOAD), None)
    if sha:
        prepared.dress.extra_payloads[TRANSCRIBE_PAYLOAD] = sha
    prepared.dress.song["score_abc_sha256"] = prov.text_sha256(abc)
    await runner._progress(job_id, 0.1)
    await runner._produce_yue2(job_id, text_view(params), prepared.seed, engine, engine_job)
