"""Re-tag existing generated files with the full provenance record (PROVENANCE §6; the CLI is
`tools/retag_music_provenance.py`).

Today's trial files carry older tags (WAV `ICMT` JSON) and a sidecar `<name>.json` (the trial's
`request_body` + `info`, or a `dpc.take-sidecar/1`). This builds the best `mp.provenance/1`
record each file's own evidence allows and writes it into the file:

- **WAV** (the trial files): `LIST/INFO` item `DPCP`. The choice is recorded in PROVENANCE §3: a
  custom top-level RIFF chunk is invisible to ffprobe and the library, while an INFO item is
  returned by ffprobe as the tag `DPCP` and read directly by `audio_tags.wav_read_info`; players
  ignore it. The WAV's other chunks, the PCM included, are copied byte for byte.
- **FLAC** (`slice1-smoke*`): Vorbis comment `DPC_PROVENANCE` plus `LYRICS` / `UNSYNCEDLYRICS`.

**Never guessed** (rule 7). A field the evidence does not hold is `not_stored`: the text sent to
`/lm` (the trial stored the LM's answer, not the request), the engine binaries' hashes, the
weights' hashes of the acestep trials (the files installed today are not proof of the files used
then), the GPU and driver. A genotype field that is `not_stored` means no genome or render id.
What *is* stored is copied exactly: the codes, the seeds, the sampling settings, the batch size
and index, the yue2 weights' sha256 and adapters' sha256.

**Safety.** The PCM md5 of the file is computed before and after, and compared with the sidecar's
own `audio_pcm_md5_*` when it has one; a mismatch before writing refuses the file. The writers in
`audio_tags.py` verify the audio region themselves and replace the file atomically. A file that
already has a record is left alone. `dry_run` builds every record and writes nothing.
The sidecar gets the record under `mp_provenance` (its own keys are kept).

**Derived files** (`level-matched-*`, `*_m18lufs.wav`: a gain-changed copy for the loudness A/B)
are left alone: their source carries the record, they have no sidecar, and a second copy of a
6-60 KB record per listening copy would only make 51 more things to keep in step.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from . import provenance as prov
from .audio_tags import (WAV_INFO_PROVENANCE_KEY, AlreadyTagged, TagError, flac_add_comments, flac_read_comments,
                         wav_add_info, wav_read_info)
from .identity import MediaTools, file_sha256, read_flac_streaminfo
from .runner import pcm_md5_and_sha256, read_wav_info
from .store import atomic_write_text

logger = logging.getLogger(__name__)

TOOL = "retag_music_provenance.py"
NS = prov.NOT_STORED
DERIVED_DIR_PREFIXES = ("level-matched",)
DERIVED_SUFFIXES = ("_m18lufs",)


@dataclass
class Format:
    sample_rate: Optional[int]
    channels: Optional[int]
    bits: Optional[int]
    ffmpeg_md5: Optional[str] = None   # the sidecar's `MD5=...` (ffmpeg -f md5 over the decoded samples)


@dataclass
class FileResult:
    file: str
    container: str
    status: str            # tagged | would_tag | already | derived | error
    reason: str = ""
    record_bytes: int = 0
    by_hash_only: List[str] = field(default_factory=list)
    not_stored: List[str] = field(default_factory=list)
    genome: bool = False
    pcm_md5: str = ""


# -- reading the evidence -----------------------------------------------------------------------


def _md5_of(text: Any) -> Optional[str]:
    if isinstance(text, str):
        m = re.fullmatch(r"(?:MD5=)?([0-9a-fA-F]{32})", text.strip())
        return m.group(1).lower() if m else None
    return None


def _load_json(path: Path, limit: int = 4 * 1024 * 1024) -> Any:
    try:
        if not path.is_file() or path.stat().st_size > limit:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _json_bytes(raw: Optional[bytes]) -> Any:
    try:
        return json.loads(raw.decode("utf-8")) if raw else None
    except (ValueError, UnicodeDecodeError):
        return None


def _basename(text: Any) -> Optional[str]:
    return re.split(r"[\\/]", text)[-1] if isinstance(text, str) and text else None


def _commit_from_engine(text: Any) -> Optional[str]:
    m = re.search(r"\b([0-9a-f]{7,40})\b", text) if isinstance(text, str) else None
    return m.group(1) if m else None


def _replay_tokens(info: Mapping[str, Any], body: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The yue2 trial's replay request (the score and the semantic tokens), read from where the trial
    left it, accepted only when its seeds, lyrics and style are those of the sidecar's request."""
    raw = info.get("replay_request_with_tokens")
    if not isinstance(raw, str) or not raw:
        return None
    replay = _load_json(Path(raw))
    if not isinstance(replay, dict):
        return None
    if (replay.get("seed") != body.get("seed") or replay.get("lm_seed") != body.get("lm_seed")
            or replay.get("lyrics") != body.get("lyrics") or replay.get("style") != body.get("style")):
        return None
    tokens = replay.get("semantic_tokens")
    if not (isinstance(tokens, str) and re.fullmatch(r"[0-9, ]+", tokens)):
        return None
    return replay


# -- building the record ------------------------------------------------------------------------


def _loudness_of(info: Mapping[str, Any]) -> Any:
    loud = info.get("loudness")
    if isinstance(loud, Mapping) and isinstance(loud.get("lufs_i"), (int, float)):
        return {"lufs_i": loud.get("lufs_i"), "lra": loud.get("lra_lu", loud.get("lra")),
                "true_peak_dbtp": loud.get("true_peak_dbtp"), "measured_by": "trial measurement kept in the sidecar",
                "measured_at": NS}
    return NS


def _chain(container: str, wav_sha: Optional[str], md5: str, fmt: Format, now: str) -> Dict[str, Any]:
    return {
        "engine_output": {"format": "wav24" if container == "wav" else NS, "sha256": wav_sha or NS, "pcm_md5": md5,
                          "sample_rate": fmt.sample_rate, "channels": fmt.channels, "bits": fmt.bits,
                          "latent_sha256": NS, "kept": container == "wav",
                          "sha256_basis": "the file's bytes before this retag",
                          **({"ffmpeg_decoded_md5": fmt.ffmpeg_md5,
                              "ffmpeg_decoded_md5_note": "the trial's own check (ffmpeg -f md5, samples decoded to "
                                                         "32 bit), verified against the file before this retag"}
                             if fmt.ffmpeg_md5 else {})},
        "dpc_postprocess": {"step": "retag", "tool": TOOL, "tool_version": NS, "retagged_at": now,
                            "note": "the record was written into an existing file from its sidecar and tags; "
                                    "fields the evidence did not hold are not_stored"},
        "output": {"container": container, "pcm_md5": md5, "pcm_md5_basis": prov.PCM_MD5_BASIS,
                   "sample_rate": fmt.sample_rate, "channels": fmt.channels, "bits": fmt.bits},
        "pcm_md5": md5,
    }


def _trial_acestep(side: Mapping[str, Any], md5: str, container: str, wav_sha: Optional[str], fmt: Format,
                   now: str) -> Dict[str, Any]:
    body, meta = side["request_body"], side["info"]
    codes = str(body.get("audio_codes") or "")
    commit = _commit_from_engine(meta.get("engine"))
    batch_index = meta.get("synth_batch_index", 0)
    seed = meta.get("seed", body.get("seed"))
    rec: Dict[str, Any] = {
        "schema": prov.SCHEMA, "job_id": None, "variant": batch_index + 1 if isinstance(batch_index, int) else None,
        "created_at": meta.get("date") or NS,
        "engine": "acestep.cpp", "engine_version": commit or NS, "engine_commit": commit or NS,
        "engine_binaries": NS, "build": NS, "device": {"gpu": NS, "driver": NS}, "backend": NS,
        "reproducible_within": prov.REPRODUCIBLE_WITHIN["acestep.cpp"],
        "models": [
            {"role": "lm", "file": meta.get("lm_model") or NS, "sha256": NS, "bytes": NS},
            {"role": "dit", "file": meta.get("dit_model_file") or body.get("synth_model") or NS, "sha256": NS,
             "bytes": NS},
            {"role": "text_encoder", "file": NS, "sha256": NS, "bytes": NS},
            {"role": "vae", "file": NS, "sha256": NS, "bytes": NS}],
        "payloads": {},
        "task_type": body.get("task_type") or NS,
        "caption_sent": NS, "bpm_sent": NS, "keyscale_sent": NS, "timesignature_sent": NS, "vocal_language_sent": NS,
        "caption_chosen": body.get("caption", NS), "bpm_chosen": body.get("bpm", NS),
        "keyscale_chosen": body.get("keyscale", NS), "timesignature_chosen": body.get("timesignature", NS),
        "vocal_language_chosen": body.get("vocal_language", NS),
        "lyrics": body.get("lyrics", NS), "instrumental": NS, "lyrics_prep": NS,
        "lyrics_basis": "the /synth request body (the LM answer's lyrics); the text sent to /lm was not stored",
        "pronunciation": {"stress_marks": NS, "count": str(body.get("lyrics") or "").count("́"),
                          "note": "the author's text was not stored, so 'none sent' cannot be told from 'lost'"},
        "audio_codes": codes, "audio_codes_sha256": prov.text_sha256(codes), "codes_count": prov.count_codes(codes),
        "lm_seed": body.get("lm_seed", meta.get("lm_seed")),
        "seed": seed,
        "variant_seed": seed + batch_index if isinstance(seed, int) and isinstance(batch_index, int) else NS,
        "inference_steps": body.get("inference_steps", meta.get("steps", NS)),
        "guidance_scale": body.get("guidance_scale", meta.get("guidance_scale", prov.ENGINE_DEFAULT)),
        "shift": body.get("shift", meta.get("shift", prov.ENGINE_DEFAULT)),
        "solver": body.get("solver", meta.get("solver", NS)),
        "lm_model": meta.get("lm_model") or NS, "lm_mode": body.get("lm_mode", NS),
        "use_cot_caption": NS, "synth_model": body.get("synth_model") or meta.get("dit_model_file") or NS, "vae": NS,
        "synth_batch_size": body.get("synth_batch_size", meta.get("synth_batch_size", NS)),
        "synth_batch_index": batch_index,
        "duration_requested_s": meta.get("duration_requested_s", body.get("duration")),
        "duration_received_s": meta.get("duration_received_s", NS),
        "engine_window_s": round(prov.count_codes(codes) / prov.CODES_HZ, 3) if codes else NS,
        "loudness": NS,
        "relation": "song_from_own_text", "parents": [],
    }
    for key in ("dcw_mode", "dcw_scaler", "dcw_high_scaler"):
        if key in body:
            rec[key] = body[key]
    rec.update(_chain(container, wav_sha, md5, fmt, now))
    return rec


def _trial_yue2(side: Mapping[str, Any], md5: str, container: str, wav_sha: Optional[str], fmt: Format, now: str,
                use_replay_files: bool) -> Dict[str, Any]:
    body, meta = side["request_body"], side["info"]
    replay = _replay_tokens(meta, body) if use_replay_files else None
    tokens = str((replay or {}).get("semantic_tokens") or "")
    abc = str((replay or {}).get("abc") or "")
    commit = _commit_from_engine(meta.get("engine"))
    adapters: List[Dict[str, Any]] = []
    if isinstance(body.get("adapters"), list) and body["adapters"]:
        split = meta.get("adapter_split_files") or {}
        source = meta.get("adapter_source") or {}
        names = [a.get("name") for a in body["adapters"] if isinstance(a, Mapping)]
        adapters.append({
            "name": meta.get("voice") or NS,
            "source": {"file": _basename(source.get("path")) or NS, "bytes": NS, "sha256": source.get("sha256") or NS},
            **{half: {"file": nm, "bytes": NS, "sha256": (split.get(half) or {}).get("sha256") or NS,
                      "scale": (split.get(half) or {}).get("scale")}
               for half, nm in zip(("music", "sound"), names)}})
    variant = meta.get("variant")
    rec: Dict[str, Any] = {
        "schema": prov.SCHEMA, "job_id": None, "variant": variant + 1 if isinstance(variant, int) else None,
        "created_at": meta.get("date") or NS,
        "engine": "yue2.cpp", "engine_version": commit or NS, "engine_commit": commit or NS,
        "engine_binaries": NS, "build": NS, "device": {"gpu": NS, "driver": NS}, "backend": NS,
        "reproducible_within": prov.REPRODUCIBLE_WITHIN["yue2.cpp"],
        "models": [
            {"role": "backbone", "file": meta.get("model_file") or NS,
             "sha256": ("sha256:" + meta["model_sha256"]) if meta.get("model_sha256") else NS, "bytes": NS},
            {"role": "vae", "file": meta.get("vae_file") or NS,
             "sha256": ("sha256:" + meta["vae_sha256"]) if meta.get("vae_sha256") else NS, "bytes": NS}],
        "payloads": {},
        "task_type": "text2music",
        "caption_sent": body.get("style", NS), "caption_chosen": (replay or {}).get("style", NS),
        "lyrics": body.get("lyrics", NS), "instrumental": not bool(body.get("lyrics")), "lyrics_prep": None,
        "pronunciation": prov.stress_marks(body.get("lyrics")),
        "score_abc": abc, "score_abc_sha256": prov.text_sha256(abc) if abc else NS,
        "semantic_tokens": tokens, "semantic_tokens_sha256": prov.text_sha256(tokens) if tokens else NS,
        "semantic_tokens_count": prov.count_codes(tokens) if tokens else NS,
        "lm_seed": body.get("lm_seed", NS),
        "backbone": "stock", "adapters": adapters,
        "seed": body.get("seed", NS), "variant_seed": body.get("seed", NS),
        "inference_steps": body.get("steps", NS), "cot": body.get("cot", prov.ENGINE_DEFAULT),
        "peak_clip": body.get("peak_clip", prov.ENGINE_DEFAULT), "lm_batch_size": body.get("lm_batch_size", NS),
        "synth_batch_size": body.get("synth_batch_size", NS), "synth_batch_index": 0,
        "duration_requested_s": meta.get("duration_req_s", body.get("duration")),
        "duration_received_s": meta.get("duration_rcv_s", NS),
        "loudness": _loudness_of(meta),
        "relation": "song_from_own_text", "parents": [],
    }
    if meta.get("model_sha256") and "own conversion" in str(meta.get("model") or ""):
        rec["models"][0]["note"] = "own conversion of m-a-p/YuE2-3B (the trial's own words)"
    if not tokens:
        rec["record_notes"] = ["the trial's replay request with the semantic tokens was not found or did not match "
                               "this file's request: the song cannot be replayed from the file"]
    rec.update(_chain(container, wav_sha, md5, fmt, now))
    return rec


def _take_sidecar_record(side: Mapping[str, Any], md5: str, container: str, wav_sha: Optional[str], fmt: Format,
                         now: str, payload: Callable[[str], Optional[bytes]]) -> Dict[str, Any]:
    """A `dpc.take-sidecar/1` (a take made by a DPC job before the full record): its identity,
    seeds and hashes are exact; the request text and the codes come back only from the job's
    payload files, and only when their sha256 is the one the sidecar recorded."""
    engine = side.get("engine") if isinstance(side.get("engine"), Mapping) else {}
    key = engine.get("engine") or NS
    ident = {"version": engine.get("version"), "commit": engine.get("commit"), "binaries": engine.get("binaries"),
             "models": side.get("models"), "server_args": engine.get("server_args"), "adapters": side.get("adapters")}
    rec = prov._base(engine=key, job_id=side.get("job_id"), variant=side.get("variant"),
                     created_at=side.get("created_at") or NS, identity=ident, device=None,
                     payloads=side.get("payloads"))
    rec.update({
        "task_type": side.get("task_type") or NS,
        "lm_seed": side.get("lm_seed", NS), "seed": side.get("seed", NS), "variant_seed": side.get("variant_seed", NS),
        "synth_batch_size": side.get("synth_batch_size", NS), "synth_batch_index": side.get("synth_batch_index", NS),
        "duration_requested_s": side.get("duration_requested_s", side.get("duration_cap_s", NS)),
        "duration_received_s": side.get("duration_received_s", NS), "loudness": NS,
        "relation": "song_from_own_text", "parents": [], "instrumental": NS, "lyrics_prep": NS,
    })
    if key == "acestep.cpp":
        req = _json_bytes(payload("lm.request.json"))
        res = _json_bytes(payload("lm.result.json"))
        res0 = res[0] if isinstance(res, list) and res and isinstance(res[0], dict) else {}
        req = req if isinstance(req, dict) else {}
        codes = str(res0.get("audio_codes") or "")
        count = side.get("codes_count")
        rec.update({
            "caption_sent": req.get("caption", NS), "bpm_sent": req.get("bpm"), "keyscale_sent": req.get("keyscale"),
            "timesignature_sent": req.get("timesignature"), "vocal_language_sent": req.get("vocal_language"),
            "caption_chosen": res0.get("caption", NS), "bpm_chosen": res0.get("bpm", NS),
            "keyscale_chosen": res0.get("keyscale", NS), "timesignature_chosen": res0.get("timesignature", NS),
            "vocal_language_chosen": res0.get("vocal_language", NS),
            "lyrics": req.get("lyrics", NS),
            "pronunciation": prov.stress_marks(req["lyrics"]) if "lyrics" in req else {"stress_marks": NS, "count": 0},
            **({"instrumental": req["lyrics"] == "[Instrumental]",
                "lyrics_prep": res0["lyrics"] if res0.get("lyrics") not in (None, req["lyrics"]) else None}
               if "lyrics" in req and res0 else {}),
            "audio_codes_sha256": prov.text_sha256(codes) if codes else side.get("audio_codes_sha256") or NS,
            "codes_count": prov.count_codes(codes) if codes else count if count is not None else NS,
            "inference_steps": side.get("inference_steps", NS), "guidance_scale": prov.ENGINE_DEFAULT,
            "shift": prov.ENGINE_DEFAULT, "solver": side.get("solver") or NS,
            "lm_model": side.get("lm_model") or NS, "synth_model": side.get("synth_model") or NS,
            "engine_window_s": round(count / prov.CODES_HZ, 3) if isinstance(count, int) else NS,
        })
        if codes:
            rec["audio_codes"] = codes
    else:
        n = side.get("variant") or 1
        req = _json_bytes(payload(f"synth.v{n}.request.json"))
        res = _json_bytes(payload(f"synth.v{n}.result.json"))
        req = req if isinstance(req, dict) else {}
        res = res if isinstance(res, dict) else {}
        tokens = str(res.get("semantic_tokens") or "")
        abc = str(res.get("abc") or "")
        replay = side.get("replay") if isinstance(side.get("replay"), Mapping) else {}
        rec.update({
            "caption_sent": req.get("style", NS), "caption_chosen": res.get("style", NS),
            "lyrics": req.get("lyrics", NS),
            "pronunciation": prov.stress_marks(req["lyrics"]) if "lyrics" in req else {"stress_marks": NS, "count": 0},
            **({"instrumental": not req["lyrics"], "lyrics_prep": None} if "lyrics" in req else {}),
            "backbone": side.get("backbone") or NS, "adapters": prov._adapters_list(side.get("adapters")),
            "inference_steps": side.get("steps", NS), "cot": side.get("cot", NS),
            "peak_clip": req.get("peak_clip", prov.ENGINE_DEFAULT), "lm_batch_size": side.get("lm_batch_size", NS),
            "score_abc_sha256": prov.text_sha256(abc) if abc else replay.get("abc_sha256") or NS,
            "semantic_tokens_sha256": prov.text_sha256(tokens) if tokens else NS,
            "semantic_tokens_count": prov.count_codes(tokens) if tokens else replay.get("semantic_tokens_count", NS),
        })
        if abc:
            rec["score_abc"] = abc
        if tokens:
            rec["semantic_tokens"] = tokens
    rec["record_notes"] = ["built from the take's older sidecar; the request text and the codes are present only "
                           "where the job's payload files still matched the recorded sha256"]
    rec.update(_chain(container, wav_sha, md5, fmt, now))
    return rec


# -- the walk -----------------------------------------------------------------------------------


def _is_derived(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root).parts
    except ValueError:
        rel = path.parts
    return (any(p.startswith(DERIVED_DIR_PREFIXES) for p in rel[:-1])
            or any(path.stem.endswith(s) for s in DERIVED_SUFFIXES))


def _payload_reader(side: Mapping[str, Any], dpc_home: Optional[Path]) -> Callable[[str], Optional[bytes]]:
    sums = side.get("payloads") if isinstance(side.get("payloads"), Mapping) else {}
    job = side.get("job_id")

    def read(name: str) -> Optional[bytes]:
        if dpc_home is None or not isinstance(job, str) or not re.fullmatch(r"job_[0-9a-f]{16}", job):
            return None
        try:
            data = (dpc_home / "music" / "jobs" / job / "payload" / name).read_bytes()
        except OSError:
            return None
        return data if sums.get(name) == prov.sha256_of(data) else None
    return read


def _ffmpeg_md5(path: Path) -> Optional[str]:
    """`ffmpeg -f md5` of the decoded audio (the trials' own check), or None without ffmpeg."""
    tools = MediaTools.discover()
    if not tools.ffmpeg:
        return None
    done = tools.runner([tools.ffmpeg, "-nostdin", "-v", "error", "-i", str(path), "-map", "0:a:0", "-f", "md5", "-"],
                        timeout=600.0)
    out = done.stdout.decode("utf-8", "replace") if isinstance(done.stdout, bytes) else (done.stdout or "")
    return _md5_of(out.strip().splitlines()[-1]) if done.returncode == 0 and out.strip() else None


def _wav_facts(path: Path) -> Tuple[str, str, Format]:
    info = read_wav_info(path)
    md5, _sha = pcm_md5_and_sha256(path, info)
    return md5, "sha256:" + file_sha256(path), Format(info.sample_rate, info.channels, info.bits)


def retag_file(path: Path, *, dry_run: bool, dpc_home: Optional[Path] = None, now: Optional[str] = None,
               use_replay_files: bool = True) -> FileResult:
    """Re-tag one WAV or FLAC. Never raises: the result says what happened."""
    path = Path(path)
    container = path.suffix.lower().lstrip(".")
    res = FileResult(file=path.name, container=container, status="error")
    now = now or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    try:
        if container == "wav":
            has = bool(wav_read_info(path).get(WAV_INFO_PROVENANCE_KEY))
        elif container == "flac":
            has = bool(flac_read_comments(path, max_block_bytes=8 * 1024 * 1024).get(prov.TAG))
        else:
            res.reason = "not a WAV or FLAC file"
            return res
        if has:
            res.status, res.reason = "already", "the file already holds a record"
            return res
        sidecar_path = path.with_suffix(".json")
        side = _load_json(sidecar_path)
        if not isinstance(side, dict):
            res.reason = "no readable sidecar next to the file"
            return res
        if container == "wav":
            md5, wav_sha, fmt = _wav_facts(path)
            claimed = (_md5_of((side.get("info") or {}).get("audio_pcm_md5_before")) or _md5_of(side.get("pcm_md5_before"))
                       or _md5_of((side.get("engine_output") or {}).get("pcm_md5")))
            if claimed:
                # The trials' md5 is ffmpeg's (24-bit samples decoded to 32 bit), not the packed-sample md5
                # the library uses; check the claim the way it was made.
                now_md5 = _ffmpeg_md5(path)
                if now_md5 is not None and now_md5 != claimed:
                    res.reason = f"the file's decoded md5 {now_md5} is not the sidecar's {claimed}; left alone"
                    return res
                fmt.ffmpeg_md5 = claimed if now_md5 is not None else None
        else:
            si = read_flac_streaminfo(path)
            if si is None or not si.md5_is_set:
                res.reason = "the FLAC has no STREAMINFO md5"
                return res
            md5, wav_sha, fmt = si.md5, None, Format(si.sample_rate, si.channels, si.bits_per_sample)
            claimed = _md5_of((side.get("output") or {}).get("pcm_md5"))
            if claimed and claimed != md5:
                res.reason = f"the file's PCM md5 {md5} is not the sidecar's {claimed}; left alone"
                return res
        res.pcm_md5 = md5

        if side.get("schema") == "dpc.take-sidecar/1":
            rec = _take_sidecar_record(side, md5, container, wav_sha, fmt, now, _payload_reader(side, dpc_home))
        elif isinstance(side.get("request_body"), dict) and isinstance(side.get("info"), dict):
            body = side["request_body"]
            if "audio_codes" in body:
                rec = _trial_acestep(side, md5, container, wav_sha, fmt, now)
            elif "style" in body:
                rec = _trial_yue2(side, md5, container, wav_sha, fmt, now, use_replay_files)
            else:
                res.reason = "the sidecar's request is of a kind this tool does not know"
                return res
        else:
            res.reason = "the sidecar is of a kind this tool does not know"
            return res
        rec["retagged"] = {"tool": TOOL, "at": now, "container": container}
        rec, text = prov.finalize(rec)
        res.record_bytes = len(text.encode("utf-8"))
        res.by_hash_only = sorted(rec.get("omitted_for_size", {}))
        res.not_stored = sorted(k for k, v in rec.items() if v == NS)
        res.genome = isinstance(rec.get("genome_sha256"), str) and rec["genome_sha256"] != NS
        if dry_run:
            res.status = "would_tag"
            return res

        if container == "wav":
            wav_add_info(path, WAV_INFO_PROVENANCE_KEY, text)
            after = _wav_facts(path)[0]
        else:
            comments = {prov.TAG: text}
            lyrics = rec.get("lyrics")
            if isinstance(lyrics, str) and lyrics.strip() and lyrics != NS and rec.get("instrumental") is not True:
                comments.update({"LYRICS": lyrics, "UNSYNCEDLYRICS": lyrics})
            flac_add_comments(path, comments)
            si2 = read_flac_streaminfo(path)
            after = si2.md5 if si2 else ""
        if after != md5:
            res.reason = f"the PCM md5 changed from {md5} to {after}"
            return res  # unreachable in practice: the writers verify the audio first
        side["mp_provenance"] = rec
        atomic_write_text(sidecar_path, json.dumps(side, ensure_ascii=False, indent=1))
        res.status = "tagged"
        return res
    except AlreadyTagged as exc:
        res.status, res.reason = "already", str(exc)
    except prov.ProvenanceError as exc:
        res.reason = f"no record written: {exc}"
    except (TagError, OSError, ValueError) as exc:
        res.reason = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # the batch goes on
        logger.exception("music retag: %s failed", path.name)
        res.reason = f"{type(exc).__name__}: {exc}"
    return res


def retag_tree(roots: List[Path], *, dry_run: bool, dpc_home: Optional[Path] = None, now: Optional[str] = None,
               use_replay_files: bool = True) -> List[FileResult]:
    """Every WAV and FLAC under each root, sorted, derived copies skipped."""
    out: List[FileResult] = []
    for root in roots:
        root = Path(root)
        files = [root] if root.is_file() else sorted(
            p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in (".wav", ".flac"))
        for p in files:
            if _is_derived(p, root if root.is_dir() else p.parent):
                out.append(FileResult(file=p.name, container=p.suffix.lower().lstrip("."), status="derived",
                                      reason="a gain-changed listening copy; its source carries the record"))
                continue
            out.append(retag_file(p, dry_run=dry_run, dpc_home=dpc_home, now=now, use_replay_files=use_replay_files))
    return out


def summarize(results: List[FileResult]) -> Dict[str, Any]:
    counts: Dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    sizes = [r.record_bytes for r in results if r.record_bytes]
    return {"files": len(results), "by_status": counts,
            "record_bytes": ({"min": min(sizes), "max": max(sizes), "mean": round(sum(sizes) / len(sizes))}
                             if sizes else None),
            "by_hash_only": sum(1 for r in results if r.by_hash_only),
            "with_genome_id": sum(1 for r in results if r.genome),
            "errors": [f"{r.file}: {r.reason}" for r in results if r.status == "error"]}
