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
- `generate_similar` on yue2.cpp, the default (Mike 2026-11-26, option 3 "both"), `_same_style_on_yue2`: while the
  job is queued, the describer (`describe.py`, Music Flamingo by default) listens to the source and its answer
  fills what the person did not type (the style when the caption is empty, each Keep box checked with no value;
  typed values win), written as payload `describe.result.json` and kept by the audio's hash; skipped, with the
  reason recorded, when nothing is asked of it. On the card, yue2.cpp's audio tokenizer (`POST /tokenize`, served
  when the server runs with `--tokenizer`) turns the source into its semantic codes (25 a second), written as
  payload `tokenize.result.json` and kept by the audio's hash. Each variant's `/synth` carries the merged style,
  the lyrics as on the from-text path, the codes as `source_tokens` with the section as `source_start` /
  `source_end` (`params.source_section`, the first 30 s by default), `cot` off (a continuation without a score
  renders without one) and `duration` = section + the new part (yue2.cpp counts the section in the length,
  src/pipeline.h:378-385). The section opens each take and is rendered in it in the model's own sound, so it is
  cut off sample-exactly before the take is stored (`SongDress.prompt_cut_s`: the first section x sample-rate
  frames, then a `PROMPT_CUT_FADE_MS` fade-in), and stage-1 QA measures the cut take against the new part's
  length (a cap, as every yue2 take). Whether the source's sound carries into the continuation is not verified
  (re-rendering a recording's codes comes out in the model's own voice: ai-studio research-singing-voice.md).
- `cover` on acestep.cpp: `task_type: cover`, the source as the `audio` part of one `/synth` with every
  variant, no LM (`run_cover`: the source is VAE-encoded and degraded by an FSQ round trip, the caption
  steers the style); `cover_strength` is `audio_cover_strength`. The take is as long as the source.
- `cover` on yue2.cpp, the default (Mike 2026-11-26), Ruach Studio's cover chain (`cover_score.py`):
  the source is heard levelled when it is too loud for the transcriber; SheetSage2 transcribes it to an
  ABC score (`POST /transcribe`, served when the server runs with `--transcriber`; `melody_only` for
  `cot` melody, the default), written as payload `transcribe.result.json`; `vocal_only` keeps the
  score's Vocal voice; the style's tempo and key are made to agree with the score; the lyrics' blocks
  take the score's section tags by the track's line times (Match sections); each variant's `/synth`
  carries the score as `abc`, and the source's length as `duration` plus a semantic `min_tokens` at the
  source's own length (`yue2_cover_length`; Ruach's is a second under it), through the from-text yue2
  path. QA measures every cover against its source's length (mode `same`, `COVER_SAME_TOLERANCE_S`).
- A web track's audio comes through `web_sources.fetch_audio` (the streaming integration's one place),
  which today raises `web_source_pending_integration`; the door says so first.

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
take's when its audio or its own stored lyrics (`lyrics_source: stored`) went in. DPC's temp files of a
job (`TEMP_PARTS`) are removed when it ends and swept at runner start (`sweep_temp_parts`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from . import cover_score as cs
from . import describe
from . import job_schemas as js
from . import provenance as prov
from . import web_sources
from .acestep import MODEL_FILES
from .engine_base import MusicEngineError
from .jobs import MusicJobError

logger = logging.getLogger(__name__)

SONG_PAYLOAD = "song.request.json"
SONG_SCHEMA = "dpc.song-job/1"
TRANSCRIBE_PAYLOAD = "transcribe.result.json"
TOKENIZE_PAYLOAD = "tokenize.result.json"
TOKENS_DIR_NAME = "tokens"
# The cut of a same-style take's opening section: a short fade-in at the cut, so the take does not start mid-wave.
PROMPT_CUT_FADE_MS = 10.0
# The least new music a continuation is asked to write after its section (`semantic_sampling.min_tokens`, which
# yue2.cpp counts after the section and holds under its budget, src/pipeline.h:386-387): the end token right after
# the section would leave nothing once it is cut.
SIMILAR_MIN_NEW_S = 10.0
SIMILAR_UNVERIFIED = ("the source's own codes open the song; whether its sound carries into the continuation is not "
                      "verified (re-rendering a recording's codes comes out in the model's own voice)")
SOURCE_PART = "source.wav.part"
LEVELLED_PART = "transcribe.wav.part"
MULTIPART_PART = "cover.multipart.part"
# DPC's own temp files of a song job in `generated/<job>/`: removed when the job ends, swept at runner start
# (`sweep_temp_parts`) after a crash.
TEMP_PARTS = (SOURCE_PART, LEVELLED_PART, MULTIPART_PART)
# One yue2.cpp `/synth` answer on disk while it is split into its take (`MusicJobRunner._produce_yue2`); a hard kill
# in the middle leaves it, and it is large. Matched by this exact shape only, never by `*.part`.
SYNTH_VARIANT_PART = re.compile(r"synth\.v[0-9]+\.multipart\.part")
SOURCE_FORMAT = "wav, PCM 24-bit, 48 kHz, stereo (DPC's ffmpeg decode of the source)"
INSTRUMENTAL_LYRICS = "[Instrumental]"
TITLE_PREFIX = {js.GENERATE_SIMILAR: "Same style as", js.COVER: "Cover of",
                js.GENERATE_FROM_LYRICS: "From the lyrics of"}
TASK_TYPE = {js.GENERATE_SIMILAR: "text2music", js.COVER: "cover", js.GENERATE_FROM_LYRICS: "text2music"}
ENGINE_IDS = {js.ENGINE_ACESTEP: "acestep.cpp", js.ENGINE_YUE2: "yue2.cpp"}
# acestep.cpp's LM metadata a kept attribute fills (src/request.h: bpm, keyscale, timesignature).
KEEP_TO_LM = {"bpm": "bpm", "key": "keyscale", "timesig": "timesignature"}
# yue2.cpp's semantic preset caps the music at 9000 frames (e646d52 src/sampling.h:47).
YUE2_SEMANTIC_MAX_TOKENS = 9000
# How far a cover may land from its source's length before QA flags it (length mode `same`): acestep.cpp
# renders whole 1920-sample latent frames of the source; yue2.cpp is held between the source's length and a
# second over it (`yue2_cover_length`), so two seconds covers the rounding up of the cap.
COVER_SAME_TOLERANCE_S = {js.ENGINE_ACESTEP: 0.05, js.ENGINE_YUE2: 2.0}
# `/transcribe` is given time that grows with the source, the way `/synth` is (`MusicJobRunner._produce_yue2`: six
# seconds per audio second and five minutes), and never less than a job's flat engine timeout.
TRANSCRIBE_S_PER_SOURCE_S = 6.0
TRANSCRIBE_SLACK_S = 300.0


def transcribe_timeout_s(flat_s: float, source_s: float) -> float:
    """The seconds `/transcribe` may take for a source of `source_s` seconds."""
    return max(float(flat_s), float(source_s) * TRANSCRIBE_S_PER_SOURCE_S + TRANSCRIBE_SLACK_S)


def yue2_cover_length(source_s: float) -> Dict[str, Any]:
    """The yue2.cpp request fields that make a cover as long as its source: `duration` the source's seconds
    rounded up plus one (the cap the semantic stage stops at, src/pipeline.h:538-547, as Ruach Studio's rule for
    a song over a backing sets it, reimplemented here) and `semantic_sampling.min_tokens` the source's
    own frame count rounded up (the end token is banned until then, src/sampling.h:96), with `max_tokens`
    raised past the preset's 9000 only when the source needs it.

    Not Ruach's floor: Ruach bans the end token only until a second under the source, `floor((s - 1) x 25)`,
    and the model ended right there in the live probe (step 3007 of a 121.319 s source, 1.04 s short), so
    that floor set the length. `ceil(s x 25)` lets the take reach the source and stays under the cap of
    `(ceil(s) + 1) x 25` frames, so the semantic budget and the context check are unchanged."""
    floor = max(0, math.ceil(source_s * js.YUE2_FRAME_RATE))
    need = js.yue2_cover_frames(source_s)
    sampling: Dict[str, int] = {"min_tokens": floor}
    if need > YUE2_SEMANTIC_MAX_TOKENS:
        sampling["max_tokens"] = need
    return {"duration": float(math.ceil(source_s) + 1), "semantic_sampling": sampling}


# -- what is sent ------------------------------------------------------------------------------


def lyrics_text(params: Mapping[str, Any]) -> Optional[str]:
    """The kept or given lyrics as one text, or None when the job has none."""
    lines = params.get("lyrics")
    return "\n".join(lines) if lines else None


def caption_sent(params: Mapping[str, Any]) -> str:
    """The caption the engine gets: the person's, plus the theme of agent-free new lyrics."""
    return js.song_caption_sent(params["caption"], params.get("lyrics_theme"))


def engine_lyrics(params: Mapping[str, Any]) -> str:
    """The lyrics field as sent: the lines; acestep's `[Instrumental]` for none; "" for new lyrics
    without lines (acestep.cpp's LM writes them: docs/ARCHITECTURE.md, "Caption only")."""
    text = lyrics_text(params)
    if text is not None:
        return text
    if params["lyrics_mode"] == "none":
        return INSTRUMENTAL_LYRICS if params["engine"] == js.ENGINE_ACESTEP else ""
    return ""


def section_s_of(params: Mapping[str, Any]) -> float:
    """The length of a same-style song's opening section on yue2.cpp (`params.source_section`), 0 for any other."""
    sec = params.get("source_section")
    return round(float(sec["end_s"]) - float(sec["start_s"]), 3) if isinstance(sec, Mapping) else 0.0


def text_view(params: Mapping[str, Any], style: Optional[str] = None) -> Dict[str, Any]:
    """The job's params in the shape the from-text producers read (`style`, `lyrics`, ...). A same-style song on
    yue2.cpp asks for its section and its new part together (`duration_s` is their sum here), with the merged
    `style` when the describer filled it."""
    view: Dict[str, Any] = {"style": style if style is not None else caption_sent(params),
                            "lyrics": lyrics_text(params), "variants": params["variants"],
                            "duration_s": round(params["duration_s"] + section_s_of(params), 3),
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


def build_yue2_song_body(params: Mapping[str, Any], seed: int, abc: Optional[str] = None, *,
                         style: Optional[str] = None, lyrics: Optional[str] = None,
                         source_s: Optional[float] = None, source_tokens: Optional[str] = None) -> bytes:
    """One variant's yue2.cpp `/synth` body: the from-text body; a cover's score as `abc`, its style and lyrics
    as prepared against the score (`_cover_on_yue2`), and its length from the source (`yue2_cover_length`); a
    same-style song's codes as `source_tokens` with its section and the new part's floor (`_same_style_on_yue2`)."""
    from .runner import build_yue2_body
    req = json.loads(build_yue2_body(text_view(params, style), seed).decode("utf-8"))
    if abc is not None:
        req["abc"] = abc
    if style is not None:
        req["style"] = style
    if lyrics is not None:
        req["lyrics"] = lyrics
    if source_s is not None:
        req.update(yue2_cover_length(source_s))
    if source_tokens is not None:
        sec = params["source_section"]
        req.update({"source_tokens": source_tokens, "source_start": float(sec["start_s"]),
                    "source_end": float(sec["end_s"]), "cot": "off",
                    "semantic_sampling": {"min_tokens": int(min(SIMILAR_MIN_NEW_S, params["duration_s"])
                                                            * js.YUE2_FRAME_RATE)}})
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
    # A cover's QA terms (`runner._measure_qa`): the source's length, mode `same`, the engine's tolerance.
    qa_override: Optional[Dict[str, Any]] = None
    # A same-style take on yue2.cpp: the seconds of its opening section, cut off before it is stored
    # (`runner._store_take`, `cut_head`).
    prompt_cut_s: Optional[float] = None
    # The style the engine got when it is not the caption (a same-style song's merged style): the take's title,
    # lineage and file tags follow it.
    style_sent: Optional[str] = None
    request_title: Optional[str] = None
    variants: int = 1

    @property
    def relation(self) -> str:
        return js.SONG_RELATIONS[self.job_type]

    def title(self, variant: int) -> str:
        if self.request_title:
            return self.request_title if self.variants <= 1 else f"{self.request_title} (v{variant})"
        return f"{TITLE_PREFIX[self.job_type]} {self.params['source_title'][:60]} (v{variant})"

    def lineage(self, variant: int) -> Dict[str, Any]:
        return {"relation": self.relation, "variant": variant, "source_track_id": self.params["source_track_id"],
                "source_name": self.params["source_title"],
                "caption": self.style_sent if self.style_sent is not None else caption_sent(self.params),
                "engine": ENGINE_IDS[self.params["engine"]]}

    def patch(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        rec["relation"] = self.relation
        rec["parents"] = [dict(self.parent)]
        rec["task_type"] = TASK_TYPE[self.job_type]
        if self.prompt_cut_s:
            # The file is the new part: the length asked of it is the new part's, and the replay codes are the
            # whole render, its opening section included.
            rec["duration_requested_s"] = self.params["duration_s"]
            rec["record_notes"] = list(rec.get("record_notes") or []) + [
                f"the file is the take with its {self.prompt_cut_s:g} s opening section (the source's own codes) cut "
                "off; semantic_tokens replay the whole render, the section included"]
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
    style: Optional[str] = None          # a same-style song's merged style (`describe.merged_style`)


def sweep_temp_parts(generated_root: Path, active: Optional[set] = None) -> List[Path]:
    """Remove song jobs' temp files (`TEMP_PARTS` and a yue2 `synth.v<n>.multipart.part`, by name only) left in
    `generated/<job>/` by a crash, except under the jobs in `active`; the removed paths."""
    removed: List[Path] = []
    try:
        folders = [d for d in Path(generated_root).iterdir() if d.is_dir()]
    except OSError:
        return removed
    for folder in folders:
        if active and folder.name in active:
            continue
        try:
            names = [n for n in os.listdir(folder) if n in TEMP_PARTS or SYNTH_VARIANT_PART.fullmatch(n)]
        except OSError:
            continue
        for name in names:
            p = folder / name
            try:
                p.unlink()
                removed.append(p)
            except FileNotFoundError:
                pass
            except OSError:
                logger.warning("music: could not remove the stale %s", p, exc_info=True)
    return removed


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
            runner._clear_deferred_align(job_id)
            logger.info("music: %s job %s cancelled while it waited for the card", job["type"], job_id)
        finally:
            engine.card_lease.release_when_empty("its job ended with no engine running", holds=engine.alive)
        await runner._finalize_deferred(job_id)
    finally:
        runner._clear_deferred_align(job_id)
        runner._song_dress.pop(job_id, None)
        for p in (out_dir / name for name in TEMP_PARTS):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                logger.warning("music: could not remove %s", p, exc_info=True)


async def _prepare(runner: Any, job: dict, out_dir: Path, source_wav: Path) -> _Prepared:
    """The source, the decoded audio and the payloads, while the job is still `queued`."""
    from .runner import LM_PAYLOAD, SYNTH_PAYLOAD, _JobFailure, _file_sha256, job_request_title, read_wav_info, \
        variant_seed, yue2_request_name
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
    path: Any = None
    if js.song_needs_audio(job_type, params):
        if (entry.get("source") or {}).get("kind") == "web":
            try:
                path = await asyncio.to_thread(web_sources.fetch_audio, entry, out_dir)
            except web_sources.WebSourceNotAvailable as exc:
                raise _JobFailure(f"{exc.code}: {exc.message}")
        else:
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
        # yue2.cpp's same style takes the source through `/tokenize` on the card, not as a part of `/synth`.
        if not (job_type == js.GENERATE_SIMILAR and params["engine"] == js.ENGINE_YUE2):
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
    elif key == js.ENGINE_YUE2 and job_type == js.GENERATE_SIMILAR:
        pass        # its bodies wait for the codes, on the card (`_same_style_on_yue2`)
    elif key == js.ENGINE_YUE2 and job_type != js.COVER:
        check_song_context(params)
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
    for k in ("kept", "melody_source", "cover_strength", "cot", "vocal_only", "lyrics_source"):
        if k in params:
            song[k] = params[k]
    if job_type != js.COVER and params["reference"] == "audio" and key == js.ENGINE_ACESTEP:
        song["reference_role"] = "acestep.cpp ref_audio: a timbre reference; style steering not verified"
    style: Optional[str] = None
    if job_type == js.GENERATE_SIMILAR and key == js.ENGINE_YUE2:
        song["reference_role"] = f"yue2.cpp source_tokens: {SIMILAR_UNVERIFIED}"
        for k in ("source_section", "keep_from_source"):
            song[k] = params[k]
        doc = await _describe_for_job(runner, job, have, Path(path), audio, entry)
        style, merged = describe.merged_style(params, doc.get("answer") if doc.get("status") == describe.DONE
                                              else None)
        if not style.strip():
            raise _JobFailure(f"no_style: the style was left to the describer, and {_describe_reason(doc)}; type a "
                              "style description and propose the job again")
        song["describe"] = _describe_said(doc)
        song["style_sent"] = style
        song.update(merged)
        check_song_context(params, style)
    # The source's licence follows its audio, or its own stored lyrics; lines the caller typed under
    # `reuse` are not the source's (Zcode #188, finding 1).
    dress = SongDress(job_type, params, parent, song, song_payload_sha=song_sha,
                      inherit_licence=audio is not None or params.get("lyrics_source") == "stored",
                      request_title=job_request_title(job),
                      variants=int(params.get("variants") or 1))
    if style is not None:
        dress.style_sent = style
        dress.prompt_cut_s = section_s_of(params)
        # The cut take against the new part's length: a cap, as every yue2 take (`runner._measure_qa`).
        dress.qa_override = {"requested_s": float(params["duration_s"]), "length_mode": "cap"}
        now = await runner._get(job_id)
        sha = next((p["sha256"] for p in (now or {}).get("payloads") or [] if p["name"] == describe.DESCRIBE_PAYLOAD),
                   None)
        if sha:
            dress.extra_payloads[describe.DESCRIBE_PAYLOAD] = sha
    if job_type == js.COVER and audio is not None:
        dress.qa_override = {"requested_s": audio["frames"] / audio["rate"], "length_mode": "same",
                             "same_tolerance_s": COVER_SAME_TOLERANCE_S[key]}
        # The source's peaks beside the take's (`qa.compare_with_source`): a cover keeps its own verdict.
        peaks = await runner._measure_source(source_wav, job_id, "the source", audio["sha256"])
        if peaks is not None:
            dress.qa_override["source"] = {"relation": "cover", "peaks": peaks}
    return _Prepared(seed=seed, dress=dress, files=files, lm_body=lm_body,
                     source_wav=source_wav if audio is not None else None, style=style)


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
        elif job_type == js.GENERATE_SIMILAR and key == js.ENGINE_YUE2:
            await _same_style_on_yue2(runner, job, prepared, engine, engine_job)
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
    from .runner import SYNTH_PAYLOAD, _JobFailure, _range_sha256, job_request_title, multipart_boundary, slug_of, \
        split_multipart_file
    job_id, params = job["id"], job["params"]
    variants = int(params["variants"])
    view = text_view(params)
    await runner._progress(job_id, 0.05)
    await runner._start_engine(job_id, js.ENGINE_ACESTEP, engine)
    await runner._progress(job_id, 0.1)
    synth_body = await runner._call(runner.store.read_payload, job_id, SYNTH_PAYLOAD)
    out_dir = runner.generated_root / job_id
    multipart = out_dir / MULTIPART_PART
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
    source_job = now or job
    payloads = {p["name"]: p["sha256"] for p in source_job.get("payloads") or []
                if p["name"] in (SONG_PAYLOAD, SYNTH_PAYLOAD)}
    seed = prepared.seed
    req_title = job_request_title(source_job)
    slug = slug_of(req_title or f"{params['source_title']}-cover")
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
                payloads=payloads, requested_by=source_job.get("requested_by"),
                job_request=source_job.get("request"), **facts)

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
    await runner._complete_generation(job_id)


async def _transcribe(runner: Any, job_id: str, engine: Any, wav: Path, melody_only: bool,
                      engine_job: List[Optional[str]], source_s: float = 0.0) -> bytes:
    """yue2.cpp `POST /transcribe` (the source as the `audio` part, `melody_only` as a field), polled to
    its end like any engine job; the answer's bytes (`{"abc": ...}`). The whole call (the upload, the polling and
    the fetch of the answer) is under one deadline that grows with the source (`transcribe_timeout_s`; follow-up
    20, Zcode #201: the deadline had bounded the polling alone). On expiry the engine job is cancelled
    (`POST /job?id=N&cancel=1`, yue-server.cpp) and the job fails with "did not finish"."""
    from .runner import _JobFailure
    await runner._check(job_id)
    timeout_s = transcribe_timeout_s(runner.engine_job_timeout_s, source_s)
    fields = {"melody_only": "1"} if melody_only else None

    async def call() -> bytes:
        eid = await engine.submit_form("/transcribe", b"{}", [("audio", wav, "audio/wav")], fields=fields)
        engine_job[0] = eid
        while True:
            await runner._check(job_id)
            status = await engine.job_status(eid)
            if status == "done":
                break
            if status in ("failed", "cancelled"):
                raise _JobFailure(engine.with_log_tail(f"{engine.DISPLAY} /transcribe job {eid} {status}"))
            await asyncio.sleep(runner.engine_poll_s)
        _ctype, raw = await engine.fetch_result(eid, None)
        engine_job[0] = None
        return raw or b""

    try:
        return await asyncio.wait_for(call(), timeout_s)
    except asyncio.TimeoutError:
        eid = engine_job[0]
        if eid:
            await engine.cancel(eid)
        raise _JobFailure(f"{engine.DISPLAY} /transcribe job {eid or '(not yet accepted)'} did not finish within "
                          f"{timeout_s:g} s")


async def _level_for_transcriber(runner: Any, source_wav: Path, out_dir: Path) -> Tuple[Path, Dict[str, Any]]:
    """The file SheetSage2 hears and what was done to it. The loudness is measured on the mono mix, the one
    that is sent (Zcode #195: a stereo reading is up to 6 LU hotter than its mono mix, so one gain from it
    left a fully correlated source 3 LU under target). Too loud for it (the rule of
    `cover_score.transcriber_gain_db`): one gain on that mono mix, written as `transcribe.wav.part`; else the
    source as it is. A reading with no LUFS is leveled on its peak alone; one with nothing in it (or a failed
    measurement) on the mix's own sample peak, which this counts. `said["path"]` names which ran:
    `measured`, `peak_only` (no LUFS), `lufs_sample_peak` (no peak, the sample peak stands in) or
    `sample_peak_only` (nothing was measured), `silent_no_gain` (a loudness but no peak of any kind: the mix is
    digital silence, so it gets no gain); `unreadable` leaves the source as it is."""
    from . import region_edit as re_
    dest = out_dir / LEVELLED_PART

    def downmix() -> Tuple[Any, int, float]:
        pcm = re_.read_pcm(source_wav)
        mono = pcm.data.mean(axis=1, keepdims=True)
        re_.write_wav_s24(dest, [mono], rate=pcm.rate, channels=1)
        peak = float(abs(mono).max()) if mono.size else 0.0
        return mono, pcm.rate, (20.0 * math.log10(peak) if peak > 0 else -math.inf)

    try:
        mono, rate, sample_peak = await asyncio.to_thread(downmix)
    except Exception as exc:
        return source_wav, {"gain_db": None, "measured": False, "path": "unreadable",
                            "reason": f"the source could not be mixed to mono: {exc}"[:200]}
    loud: Dict[str, Any] = {}
    reason = None
    try:
        loud = dict(await asyncio.to_thread(runner._measure, dest))
    except Exception as exc:
        reason = f"not measured: {exc}"[:200]

    def number(v: Any) -> Optional[float]:
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None

    lufs, peak = number(loud.get("lufs_i")), number(loud.get("true_peak_dbtp"))
    peak_from = "true_peak" if peak is not None else None
    if peak is None and math.isfinite(sample_peak):          # -inf (digital silence) is no peak: unknown
        peak, peak_from = round(sample_peak, 2), "sample_peak"
    path = ("measured" if lufs is not None and peak_from == "true_peak" else
            "peak_only" if lufs is None and peak_from == "true_peak" else
            "lufs_sample_peak" if lufs is not None and peak is not None else
            "silent_no_gain" if lufs is not None else "sample_peak_only")
    gain = cs.transcriber_gain_db(lufs, peak, mix_peak_db=sample_peak)
    said: Dict[str, Any] = {"measured": lufs is not None or loud.get("true_peak_dbtp") is not None, "path": path,
                            "lufs_i": lufs, "true_peak_dbtp": peak, "peak_from": peak_from, "gain_db": gain,
                            "measured_on": "the mono mix that is sent",
                            "rule": "above -14 LUFS or -1 dBFS peak: one gain to -16 LUFS, peak at most -1.5 dBFS "
                                    "(Ruach Studio); with no LUFS, the peak to -1.5 dBFS"}
    if reason:
        said["reason"] = reason
    if gain is None:
        try:
            dest.unlink()
        except OSError:
            pass
        return source_wav, said

    def write() -> Path:
        re_.write_wav_s24(dest, [mono * mono.dtype.type(10.0 ** (gain / 20.0))], rate=rate, channels=1)
        return dest

    return await asyncio.to_thread(write), said


def _timed_rows(lines: List[str], times: List[Optional[float]]) -> List[Dict[str, Any]]:
    """The sung lines of the lyrics with their start in the source (`cover_score.match_sections` rows)."""
    rows = []
    for line, t in zip(lines, times):
        s = line.strip()
        if s and s[0] not in "([":
            rows.append({"text": s, "start": t})
    return rows


def _lyrics_against_score(params: Mapping[str, Any], abc: str, *,
                          copy_bodies: bool = True) -> Tuple[Optional[str], Dict[str, Any]]:
    """The lyrics as sent with the score (Match sections, `cover_score`), or None to send them as they are; and
    what was done, for the take's `song` block. `copy_bodies` False gives an unsung section its tag alone
    (`cover_score.match_sections`)."""
    lines, times = params.get("lyrics") or [], params.get("lyrics_times")
    if not lines:
        return None, {"status": "not_run", "reason": "no lyrics"}
    if not times or all(t is None for t in times):
        return None, {"status": "not_run", "reason": "the track's lyrics have no line times (not synced)"}
    tagged = cs.pre_tag(lines, times, abc)
    try:
        match = cs.match_sections(abc, tagged or lines, _timed_rows(lines, times), copy_bodies=copy_bodies)
    except ValueError as exc:
        return None, {"status": "not_run", "reason": str(exc)}
    said = {"status": "unchanged" if match["unchanged"] else "applied", "pre_tagged": tagged is not None,
            "blocks": cs.counts_of(match["blocks"]), "sections_added": len(match["filled"]),
            "sections_copied": sum(1 for f in match["filled"] if f.get("copied_from") is not None),
            "by": "Match sections, after Ruach Studio's approach, reimplemented in DPC"}
    return (None if match["unchanged"] else match["lyrics"]), said


def cover_context(params: Mapping[str, Any], source_s: float, style: Optional[str], lyrics: Optional[str],
                  score: str) -> Dict[str, int]:
    """The context numbers of a cover's `/synth`: the text as sent, the score and the semantic budget its body
    carries (`yue2_cover_length`); `left` negative when it does not fit."""
    text = js.song_text_sent(style if style is not None else caption_sent(params),
                             lyrics if lyrics is not None else lyrics_text(params))
    text_bytes, abc_bytes = len(text.encode("utf-8")), len(score.encode("utf-8"))
    budget = js.yue2_semantic_budget(yue2_cover_length(source_s))
    prompt = js.yue2_prompt_tokens(text_bytes, abc_bytes)
    return {"context": js.YUE2_CONTEXT, "prompt_tokens_est": prompt, "budget_tokens": budget,
            "left": js.yue2_context_left(budget, prompt), "text_bytes": text_bytes, "score_bytes": abc_bytes}


def check_cover_context(params: Mapping[str, Any], source_s: float, style: Optional[str], lyrics: Optional[str],
                        score: str) -> Dict[str, int]:
    """The door's context budget again, now that the score is known (follow-up 18, Zcode #195): prefix (the text
    as it will be sent and the score) + the semantic budget the request will carry + the end token must fit
    yue2.cpp's context (src/generate.h:77-83), else the named failure `context_exceeded` with the numbers and no
    `/synth` is sent. The counts are conservative estimates (`job_schemas.yue2_prompt_tokens`); the answer is the
    numbers for the take's `song.length.context`."""
    from .runner import _JobFailure
    numbers = cover_context(params, source_s, style, lyrics, score)
    left, budget, prompt = numbers["left"], numbers["budget_tokens"], numbers["prompt_tokens_est"]
    if left < 0:
        raise _JobFailure(f"context_exceeded: the source is {source_s:g} s, which asks for {budget} music positions "
                          f"({js.YUE2_FRAME_RATE} a second), and the score ({numbers['score_bytes']} bytes) and the text "
                          f"({numbers['text_bytes']} bytes) take about {prompt} more; with the end token that is "
                          f"{prompt + budget + 1} of the {js.YUE2_CONTEXT} yue2.cpp holds, {-left} over. The take would "
                          "stop short of its source; shorten the lyrics, or cover on acestep")
    return numbers


def check_song_context(params: Mapping[str, Any], style: Optional[str] = None) -> Dict[str, int]:
    """The door's context check of a new song on yue2.cpp (`generate_from_lyrics`, `generate_similar`) again
    before its `/synth` bodies are written: the text as the body carries it (`text_view`, with a same-style song's
    merged `style`), the frames its `duration_s` cap lets run (a same-style song's section with its new part: the
    section is in the prompt and the rest is the budget) and the end token, no score. `context_exceeded` with the
    numbers otherwise."""
    from .runner import _JobFailure
    view = text_view(params, style)
    sent = len(js.song_text_sent(view["style"], view["lyrics"]).encode("utf-8"))
    numbers = js.yue2_song_context(sent, float(view["duration_s"]))
    if numbers["left"] < 0:
        raise _JobFailure(f"context_exceeded: the text is {sent} bytes, about {numbers['prompt_tokens_est']} tokens, and "
                          f"the {params['duration_s']:g} s cap asks for {numbers['budget_tokens']} music positions; with "
                          f"the end token that is {-numbers['left']} over the {js.YUE2_CONTEXT} yue2.cpp holds (shorten "
                          "the lyrics or the cap, or use acestep)")
    return numbers


def _fit_without_copies(params: Mapping[str, Any], source_s: float, style: str, score: str,
                        lyrics: Optional[str], matched: Dict[str, Any]) -> Tuple[Optional[str], Dict[str, Any]]:
    """The lyrics and their record once more without the section copies when only those make the cover not fit:
    Match sections fills an unsung section with a whole earlier block (a chorus), which the door cannot know
    before the score; their tags stay. Unchanged when it fits as it is, or does not fit even without them."""
    if not matched.get("sections_copied") or cover_context(params, source_s, style, lyrics, score)["left"] >= 0:
        return lyrics, matched
    bare, said = _lyrics_against_score(params, score, copy_bodies=False)
    if cover_context(params, source_s, style, bare, score)["left"] < 0:
        return lyrics, matched
    said["sections_copied"] = 0
    said["copies_dropped"] = {"sections": matched["sections_copied"],
                              "bytes": len((lyrics or "").encode("utf-8")) - len((bare or "").encode("utf-8")),
                              "reason": "with the copied sections the cover did not fit yue2.cpp's context; their "
                                        "tags stay"}
    return bare, said


async def _cover_on_yue2(runner: Any, job: dict, prepared: _Prepared, engine: Any,
                         engine_job: List[Optional[str]]) -> None:
    """SheetSage2's score of the source (heard levelled when it is too loud), the Vocal voice alone when asked,
    the style made to agree with the score and the lyrics matched to its sections, then the from-text yue2 path
    with the score as each variant's `abc` and the source's length asked for (`yue2_cover_length`)."""
    from .runner import _JobFailure, variant_seed, yue2_request_name
    job_id, params = job["id"], job["params"]
    backbone = params.get("backbone") or js.YUE2_BACKBONE_DEFAULT
    out_dir = runner.generated_root / job_id
    await runner._progress(job_id, 0.02)
    await runner._start_engine(job_id, js.ENGINE_YUE2, engine, backbone=backbone, adapters=[])
    problem = getattr(engine, "transcriber_problem", lambda: "the supervisor does not report a transcriber")()
    if problem:
        raise _JobFailure(f"yue2.cpp cannot transcribe the source: {problem}. A cover on acestep.cpp runs without "
                          "it (engine acestep)")
    have = {p["name"] for p in (await runner._get(job_id) or {}).get("payloads") or []}
    source_s = prepared.dress.qa_override["requested_s"] if prepared.dress.qa_override else float(params["duration_s"])
    heard, level = await _level_for_transcriber(runner, prepared.source_wav, out_dir)
    if TRANSCRIBE_PAYLOAD in have:
        raw = await runner._call(runner.store.read_payload, job_id, TRANSCRIBE_PAYLOAD)
    else:
        raw = await _transcribe(runner, job_id, engine, heard, params.get("cot") == "melody", engine_job, source_s)
        await runner._call(runner.store.write_payload, job_id, TRANSCRIBE_PAYLOAD, raw, endpoint="/transcribe")
    try:
        abc = json.loads(raw.decode("utf-8")).get("abc")
    except (ValueError, UnicodeDecodeError, AttributeError) as exc:
        raise _JobFailure(f"the /transcribe answer is not the JSON yue2.cpp returns: {exc}")
    if not isinstance(abc, str) or not abc.strip():
        raise _JobFailure("SheetSage2 heard no score in the source; a cover on yue2.cpp needs one (try acestep)")
    score = cs.keep_vocal_voice(abc) if params.get("vocal_only") else abc
    style, style_changes = cs.agree_style(caption_sent(params), score)
    lyrics, matched = _lyrics_against_score(params, score)
    lyrics, matched = _fit_without_copies(params, source_s, style, score, lyrics, matched)
    context = check_cover_context(params, source_s, style, lyrics, score)
    for index in range(int(params["variants"])):
        name = yue2_request_name(index + 1)
        if name not in have:
            body = build_yue2_song_body(params, variant_seed(prepared.seed, index), score, style=style,
                                        lyrics=lyrics, source_s=source_s)
            await runner._call(runner.store.write_payload, job_id, name, body, endpoint="/synth")
    now = await runner._get(job_id)
    sha = next((p["sha256"] for p in (now or {}).get("payloads") or [] if p["name"] == TRANSCRIBE_PAYLOAD), None)
    if sha:
        prepared.dress.extra_payloads[TRANSCRIBE_PAYLOAD] = sha
    song = prepared.dress.song
    song["score_abc_sha256"] = prov.text_sha256(abc)
    if score != abc:
        song["score_sent_sha256"] = prov.text_sha256(score)
    song["transcribe_level"] = level
    song["style_sent"] = style
    song["style_changes"] = style_changes
    song["section_match"] = matched
    song["length"] = {"source_s": round(source_s, 3), **yue2_cover_length(source_s),
                      "by": "the cap after Ruach Studio's rule for a song over a backing, reimplemented in DPC; "
                            "the floor at the source's own length, not a second under it",
                      "context": context}
    await runner._progress(job_id, 0.1)
    await runner._produce_yue2(job_id, text_view(params), prepared.seed, engine, engine_job)


# -- same style on yue2.cpp: the describer, the codes, the section ---------------------------------------


def _describe_reason(doc: Mapping[str, Any]) -> str:
    return str(doc.get("reason") or "the describer gave no answer")


def _describe_said(doc: Mapping[str, Any]) -> Dict[str, Any]:
    """The take's `song.describe`: what the describer was and did, by name and hash (the answer is the payload)."""
    out: Dict[str, Any] = {"status": doc.get("status"), "payload": describe.DESCRIBE_PAYLOAD}
    if doc.get("status") == describe.DONE:
        out.update({"alias": doc.get("alias"), "model": doc.get("model"), "answer_sha256": describe.answer_sha256(doc),
                    "whole": doc.get("whole"), "windows": doc.get("windows"), "cached": bool(doc.get("cached")),
                    "role": "describes only; never ranks (it scored every take 10/10 on 2026-11-26)"})
    else:
        out["reason"] = _describe_reason(doc)
    return out


async def _describe_for_job(runner: Any, job: dict, have: set, source: Path, audio: Optional[Mapping[str, Any]],
                            entry: Mapping[str, Any]) -> Dict[str, Any]:
    """A same-style job's description (`describe.py`), once per job: the payload when the job already has one, else
    a skipped record when nothing is asked of the describer, else the runner's describer (which reuses one kept by
    the audio's hash). Written as the payload `describe.result.json` before the card; never raises for the
    describer's own failure (the record says why)."""
    job_id, params = job["id"], job["params"]
    if describe.DESCRIBE_PAYLOAD in have:
        raw = await runner._call(runner.store.read_payload, job_id, describe.DESCRIBE_PAYLOAD)
        try:
            doc = json.loads(raw.decode("utf-8"))
            if isinstance(doc, dict):
                return doc
        except (ValueError, UnicodeDecodeError):
            pass
        return describe.failed("the stored description is not JSON", None)
    content_hash = params.get("source_content_hash") or entry.get("content_hash")
    if not describe.needs_describer(params):
        doc = describe.skipped("not asked: the style is typed and no Keep box waits for a value", content_hash)
    elif getattr(runner, "_describer", None) is None:
        doc = describe.skipped("this DPC runs no describer", content_hash)
    else:
        source_s = (audio["frames"] / audio["rate"]) if audio else float(params.get("source_duration_s") or 0)
        try:
            doc = await runner._describer(source, content_hash, source_s, runner.generated_root / job_id)
        except Exception as exc:       # the describer is advice: its failure is recorded, never the job's
            logger.warning("music: job %s: the describer failed", job_id, exc_info=True)
            doc = describe.failed(f"{type(exc).__name__}: {exc}", None, content_hash)
    body = json.dumps(doc, ensure_ascii=False, indent=1).encode("utf-8")
    await runner._call(runner.store.write_payload, job_id, describe.DESCRIBE_PAYLOAD, body, endpoint=None)
    have.add(describe.DESCRIBE_PAYLOAD)
    return doc


def cut_head(source: Path, start: int, end: int, dest: Path, cut_s: float, fade_ms: float = PROMPT_CUT_FADE_MS) -> Dict[str, Any]:
    """Write the WAV at `source[start:end]` (one audio part of an engine answer on disk) to `dest` as 24-bit PCM
    without its first `cut_s` seconds: exactly `round(cut_s x rate)` frames go, then the first `fade_ms` of what is
    left rise from silence (a linear fade, so the take does not open mid-wave). Blocking. ValueError when less than
    a second would be left: the model ended with the section and wrote nothing new."""
    from . import region_edit as re_
    import numpy as np
    pcm = re_.read_pcm(source, start, end)
    cut = int(round(float(cut_s) * pcm.rate))
    left = pcm.frames - cut
    if left < pcm.rate:
        raise ValueError(f"the take is {pcm.frames / pcm.rate:.2f} s with its {cut_s:g} s opening section; less than a "
                         "second of new music is left after the cut (the model ended with the section)")
    body = np.array(pcm.data[cut:], dtype=np.float32)
    fade = min(left, int(round(float(fade_ms) * pcm.rate / 1000.0)))
    if fade > 0:
        body[:fade] *= (np.arange(fade, dtype=np.float32) / np.float32(fade))[:, None]
    try:
        re_.write_wav_s24(dest, [body], rate=pcm.rate, channels=pcm.channels)
    except BaseException:
        try:
            Path(dest).unlink()
        except OSError:
            pass
        raise
    return {"frames_cut": cut, "frames_kept": left, "rate": pcm.rate, "fade_frames": fade}


def tokens_cache_path(generated_root: Path, sha256: str) -> Optional[Path]:
    """`<music>/tokens/<sha256 of the decoded source>.json`: the codes of one decoded audio, kept across jobs."""
    name = str(sha256 or "").replace(":", "-")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name):
        return None
    return Path(generated_root).parent / TOKENS_DIR_NAME / f"{name}.json"


def parse_codes(raw: bytes) -> List[int]:
    """The codes of a `/tokenize` answer (`{"codes": "1,2,3"}`, yue-server.cpp `run_tokenize`); ValueError when it is
    not that."""
    doc = json.loads(raw.decode("utf-8"))
    text = doc.get("codes") if isinstance(doc, dict) else None
    if not isinstance(text, str):
        raise ValueError("the answer has no codes")
    codes = [int(c) for c in re.split(r"[,\s]+", text) if c]
    if not codes or any(c < 0 for c in codes):
        raise ValueError("the codes are empty or negative")
    return codes


async def _tokenize(runner: Any, job_id: str, engine: Any, wav: Path, engine_job: List[Optional[str]],
                    source_s: float) -> bytes:
    """yue2.cpp `POST /tokenize` (the source as the `audio` part), polled to its end under one deadline that grows
    with the source like `/transcribe`'s; the answer's bytes (`{"codes": ...}`)."""
    from .runner import _JobFailure
    await runner._check(job_id)
    timeout_s = transcribe_timeout_s(runner.engine_job_timeout_s, source_s)

    async def call() -> bytes:
        eid = await engine.submit_form("/tokenize", b"{}", [("audio", wav, "audio/wav")])
        engine_job[0] = eid
        while True:
            await runner._check(job_id)
            status = await engine.job_status(eid)
            if status == "done":
                break
            if status in ("failed", "cancelled"):
                raise _JobFailure(engine.with_log_tail(f"{engine.DISPLAY} /tokenize job {eid} {status}"))
            await asyncio.sleep(runner.engine_poll_s)
        _ctype, raw = await engine.fetch_result(eid, None)
        engine_job[0] = None
        return raw or b""

    try:
        return await asyncio.wait_for(call(), timeout_s)
    except asyncio.TimeoutError:
        eid = engine_job[0]
        if eid:
            await engine.cancel(eid)
        raise _JobFailure(f"{engine.DISPLAY} /tokenize job {eid or '(not yet accepted)'} did not finish within "
                          f"{timeout_s:g} s")


async def _source_codes(runner: Any, job: dict, prepared: _Prepared, engine: Any,
                        engine_job: List[Optional[str]]) -> Tuple[bytes, bool]:
    """The source's codes for a same-style job: its payload, else the ones kept for the same decoded audio, else
    `/tokenize`; written as the payload `tokenize.result.json` and kept by the audio's sha256. `(raw, cached)`."""
    from .runner import _JobFailure
    job_id = job["id"]
    have = {p["name"] for p in (await runner._get(job_id) or {}).get("payloads") or []}
    if TOKENIZE_PAYLOAD in have:
        return await runner._call(runner.store.read_payload, job_id, TOKENIZE_PAYLOAD), True
    audio = prepared.dress.song.get("source_audio") or {}
    cache = tokens_cache_path(runner.generated_root, audio.get("sha256") or "")
    raw: Optional[bytes] = None
    cached = False
    if cache is not None:
        try:
            raw = await asyncio.to_thread(cache.read_bytes)
            parse_codes(raw)
            cached = True
        except (OSError, ValueError):
            raw = None
    if raw is None:
        source_s = (audio.get("frames") or 0) / (audio.get("rate") or 1)
        raw = await _tokenize(runner, job_id, engine, prepared.source_wav, engine_job, source_s)
        try:
            parse_codes(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise _JobFailure(f"the /tokenize answer is not the codes yue2.cpp returns: {exc}")
        if cache is not None:
            def keep() -> None:
                cache.parent.mkdir(parents=True, exist_ok=True)
                from .store import atomic_write_text
                atomic_write_text(cache, raw.decode("utf-8"))
            try:
                await asyncio.to_thread(keep)
            except OSError:
                logger.warning("music: the codes of job %s were not kept", job_id, exc_info=True)
    await runner._call(runner.store.write_payload, job_id, TOKENIZE_PAYLOAD, raw, endpoint="/tokenize")
    return raw, cached


async def _same_style_on_yue2(runner: Any, job: dict, prepared: _Prepared, engine: Any,
                              engine_job: List[Optional[str]]) -> None:
    """The source's codes (`/tokenize`, once per decoded audio), each variant's `/synth` opening on the section with
    the merged style, then the from-text yue2 path; `_store_take` cuts the section off each take (module
    docstring)."""
    from .runner import _JobFailure, variant_seed, yue2_request_name
    job_id, params = job["id"], job["params"]
    backbone = params.get("backbone") or js.YUE2_BACKBONE_DEFAULT
    await runner._progress(job_id, 0.02)
    await runner._start_engine(job_id, js.ENGINE_YUE2, engine, backbone=backbone, adapters=[])
    problem = getattr(engine, "tokenizer_problem", lambda: "the supervisor does not report an audio tokenizer")()
    if problem:
        raise _JobFailure(f"yue2.cpp cannot read the source's codes: {problem}. Same style on acestep.cpp runs "
                          "without it (engine acestep)")
    raw, cached = await _source_codes(runner, job, prepared, engine, engine_job)
    try:
        codes = parse_codes(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise _JobFailure(f"the stored codes of the source do not parse: {exc}")
    sec = params["source_section"]
    end_frame = round(float(sec["end_s"]) * js.YUE2_FRAME_RATE)
    if end_frame > len(codes):
        raise _JobFailure(f"the section ends at {sec['end_s']:g} s, past the source's {len(codes)} codes "
                          f"({len(codes) / js.YUE2_FRAME_RATE:g} s at {js.YUE2_FRAME_RATE} a second)")
    tokens = ",".join(str(c) for c in codes)
    style = prepared.style if prepared.style is not None else caption_sent(params)
    context = check_song_context(params, style)
    have = {p["name"] for p in (await runner._get(job_id) or {}).get("payloads") or []}
    for index in range(int(params["variants"])):
        name = yue2_request_name(index + 1)
        if name not in have:
            body = build_yue2_song_body(params, variant_seed(prepared.seed, index), style=style,
                                        source_tokens=tokens)
            await runner._call(runner.store.write_payload, job_id, name, body, endpoint="/synth")
    now = await runner._get(job_id)
    sha = next((p["sha256"] for p in (now or {}).get("payloads") or [] if p["name"] == TOKENIZE_PAYLOAD), None)
    if sha:
        prepared.dress.extra_payloads[TOKENIZE_PAYLOAD] = sha
    song = prepared.dress.song
    song["codes"] = {"payload": TOKENIZE_PAYLOAD, "count": len(codes), "frame_rate": js.YUE2_FRAME_RATE,
                     "kept_across_jobs": cached, "by": "yue2.cpp POST /tokenize (the audio tokenizer, --tokenizer)"}
    song["prompt_cut"] = {"cut": True, "section_s": section_s_of(params), "fade_in_ms": PROMPT_CUT_FADE_MS,
                          "at": "the section's length x the take's sample rate, in whole samples"}
    song["length"] = {"section_s": section_s_of(params), "new_part_s": float(params["duration_s"]),
                      "duration_sent_s": round(float(params["duration_s"]) + section_s_of(params), 3),
                      "context": context}
    await runner._progress(job_id, 0.1)
    await runner._produce_yue2(job_id, text_view(params, style), prepared.seed, engine, engine_job)
