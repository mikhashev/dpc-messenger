"""The provenance record `mp.provenance/1`: what a generated track carries inside itself
(`ai-studio/experiments/music-player/docs/PROVENANCE.md` §2-§4; Mike approved writing it
2026-11-21).

The goal: anyone holding only the audio file can tell how it was made and, with the same
engine and weights, make the same audio again. So the record holds the **genotype** (what is
inherited: text, what the LM chose, the `audio_codes` or the YuE2 score and semantic tokens),
the **substrate** (engine, binaries and weights by name + sha256 + size, sampling settings,
the hidden genes `synth_batch_size` and `synth_batch_index`), and the **phenotype** (lengths,
loudness, the decoded-PCM MD5), plus the lineage ids and the output chain. It is one flat JSON
object; the bucket of each field is in `GENOTYPE_KEYS` / `SUBSTRATE_KEYS`.

Where it goes: `audio_tags.py` writes it into the file (FLAC Vorbis comment `DPC_PROVENANCE`,
WAV `LIST/INFO` item `DPCP`); the sidecar `<name>.json` carries the same record (plus the
local ids the store assigns after the file exists); `export.py` carries it into MP3's
`TXXX:DPC_PROVENANCE` as `source_record`; `library.py` reads it back on a scan.

**Rules this module enforces**

- *Exact, not paraphrased* (rule 2): lyrics, captions and numbers are what was sent / chosen /
  used. A field the engine was not told is `engine_default`; a field nobody stored is
  `not_stored`. Nothing is guessed.
- *Privacy* (rule 3): `check_privacy` scans the serialized record for absolute paths, the home
  folder, the user name, IP addresses and tokens, and the writer refuses to write a record that
  has any. The caller passes file names, never paths.
- *Size* (rule 4): the compact JSON is at most `MAX_BYTES` (64 KB). If the codes, tokens or the
  score push it over, they are replaced by their sha256 + count + a payload reference, and
  `omitted_for_size` says so. The genotype hashes are computed first, so the ids do not change.
- *Reading* (rule 6): `parse_embedded` takes untrusted text, enforces the size cap and a strict
  shape, and returns a small summary; a bad record is `None` with a reason, never an exception,
  and nothing in it is used as a path.

**Ids** (EVOLUTION v3 section 3, group decision 4:1 of 2026-11-21). Every hash is the sha256 of
canonical JSON (sorted keys, no spaces, UTF-8).

- `genome_sha256` is the hash of the engine's **inheritable unit only**, with the engine id inside
  the hash so genomes of two engines can never be compared by mistake. The unit, named by
  `genome_basis`: acestep.cpp `audio_codes` (hashed as `audio_codes_sha256`); yue2.cpp
  `semantic_tokens` (as `semantic_tokens_sha256`), and `semantic_tokens+score_abc` for a cover
  (`task_type == "cover"`). The hash input is `{"engine", "genome_basis", <the sha fields of the
  unit>}`. The text, `lm_seed` and `dpc_lyrics_prep` are not in it: the codes already hold their
  effect, and changing the preparation is a `mutate` edge, not another genome. A `not_stored`
  unit means no genome id (`genome_sha256` is `not_stored`, `genome_basis` still names the unit);
  a `not_stored` text field no longer does.
- `intent_sha256` is the hash of the text bucket: `caption_sent`, `lyrics`, `bpm_sent`,
  `keyscale_sent`, `timesignature_sent`, `vocal_language_sent` (all as sent; `None` when the
  engine was not told) and `duration_requested_s`. It is `not_stored` when any of them is, which
  is allowed on old files: the intent node is then unnamed and the take keeps its genome.
- `render_sha256` is the hash of the genome id plus the substrate (the batch index is in it), so
  it says "repeatable exactly like this". Group by genome, never by render.

**Licence** (Mike, 2026-11-24, group #32; ADR-051 D17). `licence` is `{key, non_commercial,
components}`, written by `licence_record`: each part the take was made with (engine code, weights,
each adapter) with its licence key, the take's `key` the sorted distinct component keys joined by `+`
(`"cc-by-nc-4.0+mit+unknown"`), and `non_commercial` true when any component is a non-commercial
licence, false when every component is known and none is, null when one is unknown and none is known
non-commercial. A licence this module has no source for is `unknown`, never a guess. It sits outside the
genotype and the substrate, so no id changes. `summarize` carries `licence_key` and `non_commercial`
into the library entry, so takes that must stay non-commercial can be told apart without opening files.

**Legacy records.** Until 2026-11-22 `genome_sha256` was the hash of the whole genotype bucket
(the wide definition), and 55 re-tagged files carry that value. A record **without
`genome_basis` is legacy**: its stored `genome_sha256` / `render_sha256` mean the wide
definition and are never compared with the narrow ones. `summarize` therefore reports a legacy
record's ids as `legacy_genome_sha256` / `legacy_render_sha256` and leaves `genome_sha256` out
of the summary, so nothing groups old and new by accident. Stored files are not rewritten (the
file's record is written once); `derive_ids(record)` computes the narrow ids of any record,
legacy ones included, from the hash fields it already holds, for a caller that wants to regroup
them.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import logging
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

SCHEMA = "mp.provenance/1"
TAG = "DPC_PROVENANCE"
MAX_BYTES = 64 * 1024
NOT_STORED = "not_stored"
PCM_MD5_BASIS = ("md5 of the packed little-endian samples at the file's bit depth, the FLAC STREAMINFO MD5; "
                 "ffmpeg -f md5 differs for 24-bit audio because it decodes to 32 bit")
ENGINE_DEFAULT = "engine_default"
_SHA_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
CODES_HZ = 5.0

GENOTYPE_KEYS = (
    "task_type",
    "caption_sent", "bpm_sent", "keyscale_sent", "timesignature_sent", "vocal_language_sent",
    "caption_chosen", "bpm_chosen", "keyscale_chosen", "timesignature_chosen", "vocal_language_chosen",
    "lyrics", "instrumental", "lyrics_prep", "pronunciation",
    "audio_codes_sha256", "codes_count",
    "score_abc_sha256", "semantic_tokens_sha256", "semantic_tokens_count",
)
SUBSTRATE_KEYS = (
    "engine", "engine_version", "engine_commit", "engine_binaries", "models", "backbone", "adapters",
    "seed", "variant_seed", "inference_steps", "guidance_scale", "shift", "solver",
    "lm_model", "lm_mode", "use_cot_caption", "synth_model", "vae", "cot", "peak_clip", "lm_batch_size",
    "synth_batch_size", "synth_batch_index",
)
# Fields that may be replaced by a hash reference when the record is over the size cap, in the
# order they give way: (field, its sha256 field, its count field or None, the payload that holds it).
_BIG_FIELDS = (
    ("audio_codes", "audio_codes_sha256", "codes_count", "lm.result.json"),
    ("semantic_tokens", "semantic_tokens_sha256", "semantic_tokens_count", None),
    ("score_abc", "score_abc_sha256", None, None),
)

# -- licences (module docstring, "Licence") ------------------------------------------------------

LICENCE_UNKNOWN = "unknown"
NON_COMMERCIAL_LICENCES = frozenset({"cc-by-nc-4.0", "cc-by-nc-sa-4.0"})
# Where each figure comes from: acestep.cpp and yue2.cpp code, and acestep.cpp's GGUF weights, are MIT
# (ai-studio experiments/music-player/engine/acestep-cpp/BUILD.md "License", yue2-cpp/BUILD.md "Repo");
# the stock YuE2 backbone is m-a-p/YuE2-3B, cc-by-nc-4.0 (yue2-cpp/BUILD.md "Official weights"), the RS-TW
# backbone is Ruach Studio's finetune, CC BY-NC-SA 4.0 (yue2-cpp/trial.md), the instrumental adapter
# `ar_lora_inst_v3abc` is CC BY-NC 4.0 (its README, ADR-051 D17; `yue2.py`). Nothing else has a source here:
# the VAE and the Ruach voice adapters are `unknown`.
_ENGINE_CODE_LICENCE = {"acestep.cpp": "mit", "yue2.cpp": "mit"}
_ACESTEP_WEIGHTS_LICENCE = "mit"
_YUE2_BACKBONE_LICENCE = {"stock": "cc-by-nc-4.0", "rs-tw": "cc-by-nc-sa-4.0"}
INSTRUMENTAL_ADAPTER_NAME = "YuE2-instrumental-cot-full-loras"
INSTRUMENTAL_ADAPTER_SOURCE_PREFIX = "ar_lora_inst_v3abc"
_LICENCE_KEY_RE = re.compile(r"^[a-z0-9.+-]{1,96}$")


def _adapter_licence(adapter: Mapping[str, Any]) -> str:
    name = str(adapter.get("name") or "")
    source = adapter.get("source") if isinstance(adapter.get("source"), Mapping) else {}
    file = str(source.get("file") or "")
    if name.lower() == INSTRUMENTAL_ADAPTER_NAME.lower() or file.lower().startswith(INSTRUMENTAL_ADAPTER_SOURCE_PREFIX):
        return "cc-by-nc-4.0"
    return LICENCE_UNKNOWN


def licence_record(engine: str, *, backbone: Optional[str] = None, adapters: Any = ()) -> Dict[str, Any]:
    """The take's `licence` (module docstring): `{key, non_commercial, components}` for what it was made
    with. `engine` is the record's engine id; `backbone` and `adapters` (the record's adapter list, each
    `{name, source: {file}}`) apply to yue2.cpp. An engine this module does not know is `unknown`."""
    parts: List[Dict[str, str]] = [
        {"role": "engine", "name": engine, "licence": _ENGINE_CODE_LICENCE.get(engine, LICENCE_UNKNOWN)}]
    if engine == "acestep.cpp":
        parts.append({"role": "weights", "name": "acestep.cpp models", "licence": _ACESTEP_WEIGHTS_LICENCE})
    elif engine == "yue2.cpp":
        parts.append({"role": "backbone", "name": f"YuE2 backbone {backbone}",
                      "licence": _YUE2_BACKBONE_LICENCE.get(str(backbone), LICENCE_UNKNOWN)})
        parts.append({"role": "vae", "name": "YuE2 VAE", "licence": LICENCE_UNKNOWN})
        for adapter in adapters or ():
            if isinstance(adapter, Mapping):
                parts.append({"role": "adapter", "name": str(adapter.get("name")), "licence": _adapter_licence(adapter)})
    else:
        parts.append({"role": "weights", "name": f"{engine} models", "licence": LICENCE_UNKNOWN})
    keys = {p["licence"] for p in parts}
    if keys & NON_COMMERCIAL_LICENCES:
        non_commercial: Optional[bool] = True
    else:
        non_commercial = None if LICENCE_UNKNOWN in keys else False
    return {"key": "+".join(sorted(keys)), "non_commercial": non_commercial, "components": parts}


REPRODUCIBLE_WITHIN = {
    "acestep.cpp": ("same binary build, same GPU backend and driver, same synth_batch_size and batch index; "
                    "holds across server restarts (measured 2026-11-21); cross-machine unverified"),
    "yue2.cpp": ("same binary build, same GPU backend and driver, batch 1; not yet measured across server "
                 "restarts or machines"),
}


class ProvenanceError(Exception):
    """The record cannot be built or written as it is."""


class PrivacyError(ProvenanceError):
    """The serialized record holds something that must not travel inside a shared file."""


# -- canonical hashes ---------------------------------------------------------------------------


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compact_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def sha256_of(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def text_sha256(text: str) -> str:
    return sha256_of(text.encode("utf-8"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# -- privacy guard ------------------------------------------------------------------------------

_WIN_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]")
_UNC_PATH = re.compile(r"\\\\[A-Za-z0-9_.$-]+\\")
_POSIX_PATH = re.compile(
    r"(?:^|[\s\"'(=,\[])/(?:home|Users|root|tmp|var|mnt|opt|private|etc|usr|srv|media|Volumes|proc|dev)/")
_IPV4 = re.compile(r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])")
_IPV6 = re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}(?![0-9A-Fa-f:])")
_TOKEN = re.compile(
    r"(?:\bBearer\s+[A-Za-z0-9._~+/=-]{12,}|\bsk-[A-Za-z0-9_-]{16,}|\bghp_[A-Za-z0-9]{20,}|\bxox[abp]-[A-Za-z0-9-]{10,}"
    r"|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"
    r"|(?i:\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|secret|password|passwd)\b)\"?\s*[:=]\s*\"?[^\s\",]{6,})")


def _private_words() -> List[str]:
    words: List[str] = []
    try:
        home = str(Path.home())
        if len(home) >= 5:
            words += [home, home.replace("\\", "/"), home.replace("/", "\\")]
    except (RuntimeError, OSError):
        pass
    try:
        user = getpass.getuser()
        if user and len(user) >= 4:
            words.append(user)
    except Exception:
        pass
    return list(dict.fromkeys(words))


def check_privacy(text: str, *, extra_words: Sequence[str] = ()) -> None:
    """Raise `PrivacyError` when `text` (the serialized record) holds an absolute path, the home
    folder, the user name, an IP address or a token. The message names the kind, never the match."""
    lowered = text.casefold()
    for word in [*_private_words(), *extra_words]:
        w = word.casefold()
        if not w:
            continue
        if len(w) < 6 and not any(c in w for c in "\\/"):
            if re.search(r"(?<![0-9a-zа-я_])" + re.escape(w) + r"(?![0-9a-zа-я_])", lowered):
                raise PrivacyError("the record contains the user name")
        elif w in lowered:
            raise PrivacyError("the record contains the home folder or the user name")
    for kind, rx in (("an absolute Windows path", _WIN_PATH), ("a network share path", _UNC_PATH),
                     ("an absolute POSIX path", _POSIX_PATH), ("an IPv4 address", _IPV4),
                     ("an IPv6 address", _IPV6), ("a token or secret", _TOKEN)):
        if rx.search(text):
            raise PrivacyError(f"the record contains {kind}")


# -- helpers for the builders -------------------------------------------------------------------


def _ns(value: Any) -> Any:
    """`not_stored` for a missing value."""
    return NOT_STORED if value is None else value


def _sent_or_default(body: Mapping[str, Any], key: str) -> Any:
    return body[key] if key in body else ENGINE_DEFAULT


def _commit_of(version: Optional[str]) -> Optional[str]:
    if not version:
        return None
    m = re.match(r"\s*([0-9a-f]{7,40})\b", version)
    return m.group(1) if m else None


def count_codes(text: str) -> int:
    return len([c for c in text.split(",") if c.strip()])


def stress_marks(lyrics: Optional[str]) -> Dict[str, Any]:
    """PROVENANCE §2 `pronunciation`: what the author sent, never what a tag lost."""
    if not isinstance(lyrics, str):
        return {"stress_marks": "none_sent", "count": 0}
    n = lyrics.count("\u0301")
    return {"stress_marks": "present" if n else "none_sent", "count": n}


def _models_list(models: Optional[Mapping[str, Any]]) -> Any:
    if not isinstance(models, Mapping):
        return NOT_STORED
    out = []
    for role, m in models.items():
        m = m if isinstance(m, Mapping) else {}
        out.append({"role": role, "file": m.get("file"), "sha256": _ns(m.get("sha256")), "bytes": _ns(m.get("bytes")),
                    **({"choice": m["choice"]} if m.get("choice") else {})})
    return out


def _binaries_list(binaries: Any) -> Any:
    if not isinstance(binaries, list):
        return NOT_STORED
    return [{"file": b.get("file"), "sha256": _ns(b.get("sha256")), "bytes": _ns(b.get("bytes"))}
            for b in binaries if isinstance(b, Mapping)]


def _adapters_list(adapters: Any) -> List[Dict[str, Any]]:
    out = []
    for a in adapters or []:
        if not isinstance(a, Mapping):
            continue
        item: Dict[str, Any] = {"name": a.get("name"), "source": a.get("source")}
        for half in ("music", "sound"):
            if isinstance(a.get(half), Mapping):
                item[half] = {k: a[half].get(k) for k in ("file", "bytes", "sha256", "scale")}
        out.append(item)
    return out


def cmd_as_sent(argv: Sequence[str], names: Mapping[str, str]) -> List[str]:
    """The argv as it was run, with only the given paths reduced to the given names (rule 3)."""
    return [names.get(a, a) for a in argv]


def flac_command(ffmpeg: str, wav: str, flac: str, tags: Mapping[str, str]) -> List[str]:
    """The ffmpeg argv that turns the engine's WAV into the library FLAC (`runner.ffmpeg_converter`
    runs exactly this; the record keeps it with the paths reduced to file names)."""
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "wav", "-i", wav, "-map", "0:a:0", "-map_metadata", "-1",
           "-c:a", "flac", "-fflags", "+bitexact", "-flags:a", "+bitexact"]
    for k, v in tags.items():
        cmd += ["-metadata", f"{k}={v}"]
    return cmd + ["-f", "flac", flac]


# -- device (cheap, cached) ---------------------------------------------------------------------

_DEVICE_CACHE: Optional[Dict[str, Any]] = None
_DEVICE_LOCK = threading.Lock()


def device_identity(runner: Optional[Callable[..., Any]] = None, *, refresh: bool = False) -> Dict[str, Any]:
    """`{gpu, driver, source}` from `nvidia-smi` when it is there, else `{gpu: not_stored, ...}`.
    One short probe per process, through the process registry like the other probes."""
    global _DEVICE_CACHE
    with _DEVICE_LOCK:
        if _DEVICE_CACHE is not None and not refresh and runner is None:
            return dict(_DEVICE_CACHE)
        out: Dict[str, Any] = {"gpu": NOT_STORED, "driver": NOT_STORED, "source": "nvidia-smi not available"}
        exe = shutil.which("nvidia-smi")
        if exe:
            try:
                if runner is None:
                    from .identity import default_runner as runner  # imported at the call: identity imports us
                done = runner([exe, "--query-gpu=name,driver_version", "--format=csv,noheader"], timeout=8.0)
                text = done.stdout.decode("utf-8", "replace") if isinstance(done.stdout, bytes) else (done.stdout or "")
                line = text.strip().splitlines()[0] if done.returncode == 0 and text.strip() else ""
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 2 and parts[0] and parts[1]:
                    out = {"gpu": parts[0], "driver": parts[1], "source": "nvidia-smi"}
            except Exception as exc:
                logger.info("music: the GPU probe failed: %s", exc)
        if runner is None or _DEVICE_CACHE is None:
            _DEVICE_CACHE = dict(out)
        return out


# -- the builders -------------------------------------------------------------------------------


def _base(*, engine: str, job_id: Optional[str], variant: Optional[int], created_at: str, identity: Mapping[str, Any],
          device: Optional[Mapping[str, Any]], payloads: Optional[Mapping[str, str]]) -> Dict[str, Any]:
    version = identity.get("version")
    server_args = identity.get("server_args") if isinstance(identity.get("server_args"), Mapping) else {}
    return {
        "schema": SCHEMA,
        "job_id": job_id,
        "variant": variant,
        "created_at": created_at,
        "engine": engine,
        "engine_version": _ns(version),
        "engine_commit": _ns(identity.get("commit") or _commit_of(version)),
        "engine_binaries": _binaries_list(identity.get("binaries")),
        "build": NOT_STORED,
        "device": dict(device) if device else {"gpu": NOT_STORED, "driver": NOT_STORED},
        "backend": server_args.get("device") or NOT_STORED,
        "reproducible_within": REPRODUCIBLE_WITHIN.get(engine, NOT_STORED),
        "models": _models_list(identity.get("models")),
        "payloads": dict(payloads or {}),
    }


def _chain(engine_output: Mapping[str, Any], postprocess: Mapping[str, Any], output: Mapping[str, Any]) -> Dict[str, Any]:
    return {"engine_output": dict(engine_output), "dpc_postprocess": dict(postprocess),
            "output": {**dict(output), "pcm_md5_basis": PCM_MD5_BASIS}, "pcm_md5": output.get("pcm_md5")}


def _add_engine_choice(rec: Dict[str, Any], choice: Optional[Mapping[str, Any]]) -> None:
    """The job's `engine_choice` (`job_schemas`: which rule chose the engine and adapters, and
    a fall-back when there was one), kept as it is but without its timestamps. Outside the
    genotype and the substrate: the adapters themselves are in `adapters` by sha256."""
    if not isinstance(choice, Mapping):
        return
    out = {k: choice.get(k) for k in ("rule", "engine", "adapters", "fallback", "reason")}
    fb = choice.get("fell_back")
    out["fell_back"] = {k: fb.get(k) for k in ("from", "adapters", "why")} if isinstance(fb, Mapping) else None
    rec["engine_choice"] = out


def build_acestep_record(*, job_id: str, variant: int, created_at: str, params: Mapping[str, Any],
                         identity: Mapping[str, Any], lm_request: Mapping[str, Any], lm_result: Mapping[str, Any],
                         synth_request: Mapping[str, Any], seed: int, batch_size: int, batch_index: int,
                         duration_received_s: Optional[float], window_s: Optional[float],
                         loudness: Optional[Mapping[str, Any]], engine_output: Mapping[str, Any],
                         postprocess: Mapping[str, Any], output: Mapping[str, Any],
                         payloads: Optional[Mapping[str, str]] = None,
                         device: Optional[Mapping[str, Any]] = None,
                         engine_choice: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The `mp.provenance/1` record of one acestep.cpp take, from what the runner holds: the
    `/lm` request as sent, the LM's answer, the `/synth` body as sent, the engine identity, and
    the job's engine choice when a rule made one."""
    rec = _base(engine="acestep.cpp", job_id=job_id, variant=variant, created_at=created_at, identity=identity,
                device=device, payloads=payloads)
    _add_engine_choice(rec, engine_choice)
    rec["licence"] = licence_record("acestep.cpp")
    codes = str(lm_result.get("audio_codes") or "")
    lyrics = lm_request.get("lyrics")
    chosen_lyrics = lm_result.get("lyrics")
    rec.update({
        "task_type": synth_request.get("task_type") or lm_result.get("task_type") or "text2music",
        "caption_sent": lm_request.get("caption"),
        "bpm_sent": lm_request.get("bpm"), "keyscale_sent": lm_request.get("keyscale"),
        "timesignature_sent": lm_request.get("timesignature"),
        "vocal_language_sent": lm_request.get("vocal_language"),
        "caption_chosen": lm_result.get("caption"), "bpm_chosen": lm_result.get("bpm"),
        "keyscale_chosen": lm_result.get("keyscale"), "timesignature_chosen": lm_result.get("timesignature"),
        "vocal_language_chosen": lm_result.get("vocal_language"),
        "lyrics": lyrics,
        "instrumental": params.get("lyrics") is None,
        "lyrics_prep": chosen_lyrics if isinstance(chosen_lyrics, str) and chosen_lyrics != lyrics else None,
        "dpc_lyrics_prep": params.get("lyrics_prep") or {},
        "pronunciation": stress_marks(params.get("lyrics")),
        "audio_codes": codes,
        "audio_codes_sha256": text_sha256(codes),
        "codes_count": count_codes(codes),
        "lm_seed": lm_result.get("lm_seed"),
        "seed": seed, "variant_seed": seed + batch_index,
        "inference_steps": _sent_or_default(synth_request, "inference_steps"),
        "guidance_scale": _sent_or_default(synth_request, "guidance_scale"),
        "shift": _sent_or_default(synth_request, "shift"),
        "solver": _sent_or_default(synth_request, "solver"),
        "lm_model": lm_request.get("lm_model") or NOT_STORED,
        "lm_mode": _sent_or_default(synth_request, "lm_mode"),
        "use_cot_caption": _sent_or_default(synth_request, "use_cot_caption"),
        "synth_model": synth_request.get("synth_model") or NOT_STORED,
        "vae": synth_request.get("vae") or NOT_STORED,
        "synth_batch_size": batch_size, "synth_batch_index": batch_index,
        "duration_requested_s": params.get("duration_s"),
        "duration_received_s": duration_received_s,
        "engine_window_s": window_s,
        "loudness": dict(loudness) if loudness else NOT_STORED,
        "relation": "song_from_own_text", "parents": [],
        **_chain(engine_output, postprocess, output),
    })
    return rec


def build_yue2_record(*, job_id: str, variant: int, created_at: str, params: Mapping[str, Any],
                      identity: Mapping[str, Any], sent: Mapping[str, Any], replay: Mapping[str, Any],
                      seed: int, variant_seed: int, lm_seed: int, backbone: str, duration_received_s: Optional[float],
                      loudness: Optional[Mapping[str, Any]], engine_output: Mapping[str, Any],
                      postprocess: Mapping[str, Any], output: Mapping[str, Any],
                      payloads: Optional[Mapping[str, str]] = None,
                      device: Optional[Mapping[str, Any]] = None,
                      engine_choice: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The record of one yue2.cpp take: the `/synth` body as sent and the engine's replay request
    (the score as ABC and the semantic tokens), which is what makes the song repeatable; the
    job's engine choice when a rule made one."""
    rec = _base(engine="yue2.cpp", job_id=job_id, variant=variant, created_at=created_at, identity=identity,
                device=device, payloads=payloads)
    _add_engine_choice(rec, engine_choice)
    rec["licence"] = licence_record("yue2.cpp", backbone=backbone, adapters=identity.get("adapters"))
    tokens = str(replay.get("semantic_tokens") or "")
    abc = str(replay.get("abc") or "")
    lyrics = sent.get("lyrics")
    rec.update({
        "task_type": "text2music",
        "caption_sent": sent.get("style"),
        "caption_chosen": replay.get("style") if isinstance(replay.get("style"), str) else NOT_STORED,
        "lyrics": lyrics,
        "instrumental": params.get("lyrics") is None,
        "lyrics_prep": None,
        "dpc_lyrics_prep": params.get("lyrics_prep") or {},
        "pronunciation": stress_marks(params.get("lyrics")),
        "score_abc": abc, "score_abc_sha256": text_sha256(abc) if abc else NOT_STORED,
        "semantic_tokens": tokens,
        "semantic_tokens_sha256": text_sha256(tokens) if tokens else NOT_STORED,
        "semantic_tokens_count": count_codes(tokens) if tokens else NOT_STORED,
        "lm_seed": lm_seed,
        "backbone": backbone, "adapters": _adapters_list(identity.get("adapters")),
        "seed": seed, "variant_seed": variant_seed,
        "inference_steps": _sent_or_default(sent, "steps"),
        "cot": _sent_or_default(sent, "cot"),
        "peak_clip": _sent_or_default(sent, "peak_clip"),
        "lm_batch_size": sent.get("lm_batch_size", 1),
        "synth_batch_size": 1, "synth_batch_index": 0,
        "duration_requested_s": params.get("duration_s"),
        "duration_received_s": duration_received_s,
        "loudness": dict(loudness) if loudness else NOT_STORED,
        "relation": "song_from_own_text", "parents": [],
        **_chain(engine_output, postprocess, output),
    })
    if not tokens:
        rec["record_notes"] = ["the engine's replay request had no semantic_tokens: the song cannot be replayed from the file"]
    return rec


EDIT_ENGINE_LOCAL = "dpc"   # a removal is DPC's own sample work, no engine


def _with_parent_licence(licence: Dict[str, Any], parent_key: Optional[str]) -> Dict[str, Any]:
    """An edit inherits its base's licence: each part of the base's key becomes a `parent` component
    (`unknown` when the base has no record), and the key and `non_commercial` are recomputed."""
    parts = list(licence["components"])
    keys = [k for k in str(parent_key).split("+") if k] if isinstance(parent_key, str) and \
        _LICENCE_KEY_RE.match(parent_key) else [LICENCE_UNKNOWN]
    for k in keys:
        parts.append({"role": "parent", "name": "the edited track", "licence": k})
    found = {p["licence"] for p in parts}
    nc: Optional[bool] = True if found & NON_COMMERCIAL_LICENCES else (None if LICENCE_UNKNOWN in found else False)
    return {"key": "+".join(sorted(found)), "non_commercial": nc, "components": parts}


def build_edit_record(*, job_id: str, variant: int, created_at: str, action: str, task_type: str,
                      identity: Mapping[str, Any], parent: Mapping[str, Any], edit: Mapping[str, Any],
                      duration_requested_s: float, duration_received_s: Optional[float],
                      loudness: Optional[Mapping[str, Any]], engine_output: Mapping[str, Any],
                      postprocess: Mapping[str, Any], output: Mapping[str, Any],
                      request: Optional[Mapping[str, Any]] = None, seed: Optional[int] = None,
                      batch_size: Optional[int] = None, batch_index: Optional[int] = None,
                      payloads: Optional[Mapping[str, str]] = None,
                      device: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The `mp.provenance/1` record of one region-edit take (`region_edit`): what was edited (`parents`:
    the base track's library id, audio hash, version and the take it came from when it is one), how
    (`edit`: the action, the three zones, the engine window, the seams and their shapes), the repaint
    request as sent when the engine made it, and the output chain. The genome stays `not_stored`: a
    repaint has no `audio_codes`; what makes it repeatable is the parent plus the request and seed."""
    engine = str(identity.get("engine") or EDIT_ENGINE_LOCAL)
    rec = _base(engine=engine, job_id=job_id, variant=variant, created_at=created_at, identity=identity,
                device=device, payloads=payloads)
    rec["licence"] = _with_parent_licence(licence_record(engine), parent.get("licence_key"))
    req = dict(request or {})
    rec.update({
        "task_type": task_type,
        "edit_action": action,
        "caption_sent": req.get("caption"),
        "bpm_sent": None, "keyscale_sent": None, "timesignature_sent": None, "vocal_language_sent": None,
        "lyrics": req.get("lyrics"),
        "instrumental": None,
        "seed": seed if seed is not None else NOT_STORED,
        "variant_seed": (seed + (batch_index or 0)) if seed is not None else NOT_STORED,
        "inference_steps": _sent_or_default(req, "inference_steps") if req else NOT_STORED,
        "synth_model": req.get("synth_model") or NOT_STORED,
        "vae": req.get("vae") or NOT_STORED,
        "synth_batch_size": batch_size if batch_size is not None else NOT_STORED,
        "synth_batch_index": batch_index if batch_index is not None else NOT_STORED,
        "duration_requested_s": duration_requested_s,
        "duration_received_s": duration_received_s,
        "loudness": dict(loudness) if loudness else NOT_STORED,
        "relation": "region_edit",
        "parents": [{k: parent.get(k) for k in ("role", "library_track_id", "content_hash", "version_id",
                                                 "take_id", "job_id", "comment_id", "licence_key")}],
        "edit": dict(edit),
        **_chain(engine_output, postprocess, output),
    })
    return rec


# -- ids, size, serialization -------------------------------------------------------------------


_INTENT_KEYS = ("caption_sent", "lyrics", "bpm_sent", "keyscale_sent", "timesignature_sent",
                "vocal_language_sent", "duration_requested_s")
_EMPTY_SHA = sha256_of(b"")
_GENOME_UNITS = {
    # engine -> (genome_basis, the hash fields hashed)
    "acestep.cpp": ("audio_codes", ("audio_codes_sha256",)),
    "yue2.cpp": ("semantic_tokens", ("semantic_tokens_sha256",)),
}
_COVER_UNIT = ("semantic_tokens+score_abc", ("semantic_tokens_sha256", "score_abc_sha256"))


def _genome_unit(rec: Mapping[str, Any]) -> Optional[Tuple[str, Tuple[str, ...]]]:
    engine = rec.get("engine")
    if engine == "yue2.cpp" and rec.get("task_type") == "cover":
        return _COVER_UNIT
    return _GENOME_UNITS.get(engine) if isinstance(engine, str) else None


def derive_ids(rec: Mapping[str, Any]) -> Dict[str, Any]:
    """The narrow ids of a record (see the module docstring), from its own fields; the record is
    not changed. Keys: `genome_basis`, `genome_sha256`, `intent_sha256`, `render_sha256` (each
    `not_stored` when what it hashes is not known) and `ids_note` when something is missing."""
    notes: List[str] = []
    unit = _genome_unit(rec)
    out: Dict[str, Any] = {"genome_basis": unit[0] if unit else NOT_STORED}
    genome = NOT_STORED
    if unit is None:
        notes.append("the engine has no known inheritable unit, so the take has no genome or render id")
    else:
        basis, fields = unit
        values = {f: rec.get(f) for f in fields}
        if all(isinstance(v, str) and _SHA_RE.match(v) and v != _EMPTY_SHA for v in values.values()):
            genome = sha256_of(canonical_json({"engine": rec["engine"], "genome_basis": basis, **values}))
        else:
            notes.append(f"the engine's inheritable unit ({basis}) is not_stored, so the take has no genome or render id")
    out["genome_sha256"] = genome
    intent = {k: rec.get(k) for k in _INTENT_KEYS}
    if any(v == NOT_STORED for v in intent.values()):
        out["intent_sha256"] = NOT_STORED
        notes.append("a field of the intent (the text) is not_stored, so the take has no intent id")
    else:
        out["intent_sha256"] = sha256_of(canonical_json(intent))
    if genome == NOT_STORED:
        out["render_sha256"] = NOT_STORED
    else:
        substrate = {k: rec[k] for k in SUBSTRATE_KEYS if k in rec}
        out["render_sha256"] = sha256_of(canonical_json({"genome_sha256": genome, "substrate": substrate}))
    if notes:
        out["ids_note"] = "; ".join(notes)
    return out


def add_ids(rec: Dict[str, Any]) -> None:
    """`genome_basis`, `genome_sha256`, `intent_sha256`, `render_sha256` (see the module docstring)."""
    rec.pop("ids_note", None)
    rec.update(derive_ids(rec))


def serialize(rec: Mapping[str, Any]) -> str:
    return compact_json(rec)


def fit_size(rec: Dict[str, Any], max_bytes: int = MAX_BYTES) -> None:
    """Replace the big fields by hash references until the compact JSON fits `max_bytes`
    (PROVENANCE rule 4). Raises `ProvenanceError` when even that is not enough."""
    omitted: Dict[str, Any] = {}
    for field, sha_field, count_field, payload in _BIG_FIELDS:
        if len(serialize(rec).encode("utf-8")) <= max_bytes:
            break
        if not isinstance(rec.get(field), str) or not rec[field]:
            continue
        ref: Dict[str, Any] = {"sha256": rec.get(sha_field), "bytes": len(rec[field].encode("utf-8")),
                               "count": rec.get(count_field) if count_field else None,
                               "reason": f"the record was over {max_bytes} bytes"}
        name = payload
        if name is None:
            name = next((n for n in (rec.get("payloads") or {}) if n.endswith(".result.json")), None)
        if name and name in (rec.get("payloads") or {}):
            ref["payload"] = name
            ref["payload_sha256"] = rec["payloads"][name]
        omitted[field] = ref
        del rec[field]
        rec["omitted_for_size"] = dict(omitted)
    size = len(serialize(rec).encode("utf-8"))
    if size > max_bytes:
        raise ProvenanceError(f"the provenance record is {size} bytes, over the {max_bytes}-byte limit even with the "
                              "codes, tokens and score replaced by hashes")


def finalize(rec: Dict[str, Any], *, extra_private: Sequence[str] = ()) -> Tuple[Dict[str, Any], str]:
    """Add the ids, apply the size limit, run the privacy guard; return `(record, compact JSON)`.
    Raises `PrivacyError` / `ProvenanceError` (the caller then writes no record)."""
    add_ids(rec)
    fit_size(rec)
    text = serialize(rec)
    check_privacy(text, extra_words=extra_private)
    return rec, text


# -- reading (untrusted) ------------------------------------------------------------------------

_ENGINE_RE = re.compile(r"^[A-Za-z0-9._+ -]{1,48}$")
_CODES_RE = re.compile(r"^[0-9, ]*$")
_BASIS_RE = re.compile(r"^[a-z_+]{1,48}$")


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def validate_embedded(rec: Any) -> None:
    """Strict shape of a record read from a file. Raises `ValueError` with the reason."""
    if not isinstance(rec, dict):
        raise ValueError("not a JSON object")
    if rec.get("schema") != SCHEMA:
        raise ValueError(f"schema is {str(rec.get('schema'))[:40]!r}, not {SCHEMA}")
    engine = rec.get("engine")
    if not isinstance(engine, str) or not _ENGINE_RE.match(engine):
        raise ValueError("engine is missing or not a plain name")
    seed = rec.get("seed")
    if not (_is_int(seed) or seed in (None, NOT_STORED)):
        raise ValueError("seed is not an integer")
    for key in ("audio_codes", "semantic_tokens"):
        v = rec.get(key)
        if v is not None and not (isinstance(v, str) and _CODES_RE.match(v)):
            raise ValueError(f"{key} is not a list of integers")
    for key in ("audio_codes_sha256", "semantic_tokens_sha256", "score_abc_sha256",
                "genome_sha256", "intent_sha256", "render_sha256"):
        v = rec.get(key)
        if v is not None and v != NOT_STORED and not (isinstance(v, str) and _SHA_RE.match(v)):
            raise ValueError(f"{key} is not a sha256")
    basis = rec.get("genome_basis")
    if basis is not None and basis != NOT_STORED and not (isinstance(basis, str) and _BASIS_RE.match(basis)):
        raise ValueError("genome_basis is not a plain name")
    for key in ("synth_batch_size", "synth_batch_index", "codes_count"):
        v = rec.get(key)
        if v is not None and v != NOT_STORED and not (_is_int(v) and v >= 0):
            raise ValueError(f"{key} is not a count")
    for key in ("models", "engine_binaries", "parents"):
        v = rec.get(key)
        if v is not None and v != NOT_STORED and not isinstance(v, list):
            raise ValueError(f"{key} is not a list")
    for key in ("engine_output", "dpc_postprocess", "output", "payloads", "device"):
        v = rec.get(key)
        if v is not None and not isinstance(v, dict):
            raise ValueError(f"{key} is not an object")


def summarize(rec: Mapping[str, Any]) -> Dict[str, Any]:
    """The small, path-free summary the library keeps on an entry."""
    codes = rec.get("audio_codes") or rec.get("semantic_tokens")
    refs = rec.get("omitted_for_size") if isinstance(rec.get("omitted_for_size"), dict) else {}
    out: Dict[str, Any] = {
        "schema": SCHEMA,
        "engine": rec["engine"],
        "seed": rec.get("seed") if _is_int(rec.get("seed")) else None,
        "has_codes": bool(isinstance(codes, str) and codes.strip()),
        "codes_by_hash_only": bool(set(refs) & {"audio_codes", "semantic_tokens"}),
    }
    licence = rec.get("licence")
    if isinstance(licence, dict):
        key = licence.get("key")
        if isinstance(key, str) and _LICENCE_KEY_RE.match(key):
            out["licence_key"] = key
            nc = licence.get("non_commercial")
            out["non_commercial"] = nc if isinstance(nc, bool) else None
    basis = rec.get("genome_basis")
    narrow = isinstance(basis, str) and basis != NOT_STORED
    if narrow:
        out["genome_basis"] = basis
    for key in ("genome_sha256", "intent_sha256", "render_sha256"):
        v = rec.get(key)
        if isinstance(v, str) and _SHA_RE.match(v):
            # A record without genome_basis is legacy: its ids are the wide definition and must not
            # be grouped with the narrow ones (module docstring).
            out[key if narrow or key == "intent_sha256" else f"legacy_{key}"] = v
    return out


def parse_embedded(raw: Any, *, max_bytes: int = MAX_BYTES) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """`(record, None)` for a good record, `(None, reason)` for an absent, oversized, malformed or
    wrong-schema one. Never raises: the text comes from a file nobody vouches for."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, None
    if not isinstance(raw, (str, bytes)):
        return None, "not text"
    size = len(raw.encode("utf-8", "replace")) if isinstance(raw, str) else len(raw)
    if size > max_bytes:
        return None, f"{size} bytes, over the {max_bytes}-byte limit"
    try:
        rec = json.loads(raw)
        validate_embedded(rec)
    except (ValueError, RecursionError) as exc:
        return None, str(exc)[:120]
    return rec, None


def read_summary(raw: Any, *, where: str = "a file") -> Optional[Dict[str, Any]]:
    """The entry summary of an embedded record, or None; a bad record is logged and ignored."""
    rec, problem = parse_embedded(raw)
    if rec is None:
        if problem:
            logger.info("music: ignoring the provenance record in %s: %s", where, problem)
        return None
    return summarize(rec)
