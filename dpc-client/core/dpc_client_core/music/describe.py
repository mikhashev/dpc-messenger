"""The source's description by an audio model, for a same-style song (Mike 2026-11-26, option 3 "both": Music
Flamingo listens to the source and writes its style tags, and a section of the source's own codes opens the song;
`similar.py`, ADR-051 row B5).

**What it is for.** Describing, never ranking: the 2026-11-26 live probe found Music Flamingo's descriptions of
takes sensible and its scores flat (10 of 10 on every take, ai-studio `measurements/2026-11-26-live-gpu-probe.md`),
so nothing here scores, orders or judges. The answer fills what the person did not type: the style when the caption
is empty, and each Keep box checked with no value (`params.keep_from_source`). A typed value always wins.

**The describer** is the alias `[music] describe_alias` names (`DPC_MUSIC_DESCRIBE_ALIAS`): by default
`music_flamingo` when that alias is a loaded provider, else none; `none` (or `off`) turns it off. It must be an
audio-capable llama-server alias (`supports_audio`, `generate_with_audio`), the judge's engine (`qc.py`); its first
call starts the child through the card lease like any local engine, so it waits for a card a running engine holds.

**One call, held to a schema.** The audio goes as one `input_audio` part with a fixed prompt (no text of the
person's or of the track's goes into it) and `response_format` holding `describe_schema()`, which llama-server turns
into a grammar (the judge's machinery: b12110 tools/server/server-common.cpp:1384-1396). The whole answer is parsed
and checked again key for key (`parse_describe_answer`); an answer that does not parse is a failed description with
the reason, never a guess.

**The answer is data.** It is the model's text about a recording anyone could have made, so it is treated like the
person's own caption: every string is cleaned before it is stored or sent (`clean_text`: no control characters, no
line breaks, no brackets of any kind, no marker brackets, white space made single, cut to its bound). It reaches
yue2.cpp only as the `style` line (a tag list, never an instruction), and a judge that later reads the job's style
fences it as data (`qc._quoted`).

**How much audio fits.** Music Flamingo costs about 25 prompt tokens per audio second (measured 2026-11-26: 1915
prompt tokens for a 30 s take, 4157 for 120 s, through the same llama-server b12110 with the alias's 16384-token
context). A source whose tokens, the prompt and the answer fit the context is heard whole; a longer one is heard in
`MAX_WINDOWS` windows of `WINDOW_S` (its start, middle and end) and the answers are merged (`merge_answers`): the
tags in order of first mention, the tempo the median, the rest the most common answer (the first window's on a
tie). The result names the windows.

**Where it is kept.** By the source's audio hash, in `<DPC home>/music/describe/<hash>.json` (written once per
hash, read by the "Describe source" button and by every job on the same audio), and in each job as its write-once
payload `describe.result.json`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import statistics
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import job_schemas as js
from .store import music_dir_of

logger = logging.getLogger(__name__)

DESCRIBE_SCHEMA = "dpc.music-describe/1"
DESCRIBE_PAYLOAD = "describe.result.json"
DESCRIBE_DIR_NAME = "describe"
DEFAULT_DESCRIBE_ALIAS = "music_flamingo"
DESCRIBE_OFF_WORDS = ("", "none", "off", "false", "0")
DESCRIBE_SCHEMA_NAME = "music_source_description"
DONE, FAILED, SKIPPED = "done", "failed", "skipped"
CAVEAT = ("Music Flamingo describes the source; it does not rank takes (it scored every take 10/10 in the "
          "2026-11-26 probe), and it can confuse a key with its relative major or minor.")

VOCALS = ("none", "male", "female", "mixed", "choir", "unknown")
BPM_MIN, BPM_MAX = 30, 300
TAGS_MAX = 8
TAG_MAX_CHARS = 40
GENRE_MAX_CHARS = 80
KEY_MAX_CHARS = 20
TIMESIG_MAX_CHARS = 8
MOOD_MAX_CHARS = 80
STYLE_MAX_CHARS = 300
DESCRIBE_MAX_TOKENS = 512          # the answer's bounded size (every string and list is bounded by the schema)
# Measured on 2026-11-26 (module docstring): about 25 prompt tokens per audio second, and ~1165 for the template.
AUDIO_TOKENS_PER_S = 25
PROMPT_RESERVE_TOKENS = 1500
DEFAULT_CONTEXT = 16384
WINDOW_S = 120.0
MAX_WINDOWS = 3
DECODE_RATE = 16000                # what the audio encoder hears (a Whisper-type encoder at 16 kHz mono)
DECODE_TIMEOUT_S = 300.0

_SAFE_HASH = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_BRACKETS = str.maketrans({c: " " for c in "[](){}<>"})


class DescribeError(Exception):
    """A description that could not be made; `code` names why (`no_describer`, `describer_not_found`, `no_audio`,
    `decode_failed`, `failed`)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# -- the answer ------------------------------------------------------------------------------


def describe_schema() -> Dict[str, Any]:
    """The JSON schema of the describer's answer: every key required, every string and list bounded, no key beyond
    these. The tempo is an integer (a grammar holds an integer's range, not a number's)."""
    def text(n: int) -> Dict[str, Any]:
        return {"type": "string", "maxLength": n}
    return {
        "type": "object",
        "properties": {
            "genre": text(GENRE_MAX_CHARS),
            "tags": {"type": "array", "maxItems": TAGS_MAX, "items": text(TAG_MAX_CHARS)},
            "instruments": {"type": "array", "maxItems": TAGS_MAX, "items": text(TAG_MAX_CHARS)},
            "vocals": {"type": "string", "enum": list(VOCALS)},
            "bpm": {"type": "integer", "minimum": BPM_MIN, "maximum": BPM_MAX},
            "key": text(KEY_MAX_CHARS),
            "time_signature": text(TIMESIG_MAX_CHARS),
            "mood": text(MOOD_MAX_CHARS),
            "style": text(STYLE_MAX_CHARS),
        },
        "required": ["genre", "tags", "instruments", "vocals", "bpm", "key", "time_signature", "mood", "style"],
        "additionalProperties": False,
    }


def describe_response_format() -> Dict[str, Any]:
    """`response_format` for llama-server (the judge's shape, `qc.judge_response_format`)."""
    return {"type": "json_schema",
            "json_schema": {"name": DESCRIBE_SCHEMA_NAME, "strict": True, "schema": describe_schema()}}


def describe_prompt(window: Optional[Tuple[float, float]] = None) -> str:
    """The fixed instruction; nothing in it comes from the person or the track (module docstring)."""
    part = ("This is an excerpt of a song, from {:.0f} s to {:.0f} s. ".format(*window) if window else
            "This is a whole song. ")
    return (part + "Describe its musical style as one JSON object with exactly these keys: genre (the main genre), "
            "tags (up to 8 short style tags, each a few words), instruments (up to 8, the ones you hear), vocals "
            "(none, male, female, mixed, choir or unknown), bpm (the tempo, a whole number), key (for example "
            "'E minor'), time_signature (for example '4/4'), mood (a few words), style (one comma-separated line of "
            "tags a music generator would take as its style prompt: genre, mood, instruments, vocals, tempo). "
            "Describe only what you hear; write no lyrics and nothing else.")


def clean_text(value: Any, limit: int) -> str:
    """Model text as data (module docstring): printable characters only, no brackets or marker brackets, white
    space made single, at most `limit` characters."""
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFC", value)
    text = "".join(ch if unicodedata.category(ch)[0] != "C" else " " for ch in text)
    text = text.replace("<<<", " ").replace(">>>", " ").translate(_BRACKETS)
    text = " ".join(text.split())
    return text[:limit].strip(" ,;")


def _clean_list(value: Any) -> List[str]:
    out: List[str] = []
    for item in value if isinstance(value, list) else []:
        t = clean_text(item, TAG_MAX_CHARS)
        if t and t.lower() not in (x.lower() for x in out):
            out.append(t)
    return out[:TAGS_MAX]


def _whole_object(text: str) -> Any:
    inner = (text or "").strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", inner, re.DOTALL)
    if fence:
        inner = fence.group(1).strip()
    if not inner:
        raise DescribeError("failed", "the describer's answer is empty")
    try:
        return json.loads(inner)
    except json.JSONDecodeError as exc:
        raise DescribeError("failed", f"the describer's JSON does not parse: {exc.msg} at char {exc.pos}") from None


def parse_describe_answer(text: str) -> Dict[str, Any]:
    """The answer checked against `describe_schema` key for key and cleaned (`clean_text`); `DescribeError` for
    anything else (a missing or extra key, a wrong type, a tempo out of range, a vocals word not in the list)."""
    obj = _whole_object(text)
    want = set(describe_schema()["required"])
    if not isinstance(obj, dict) or set(obj) != want:
        got = sorted(obj) if isinstance(obj, dict) else type(obj).__name__
        raise DescribeError("failed", f"the describer's answer has keys {got}, not {sorted(want)}")
    for k in ("genre", "key", "time_signature", "mood", "style"):
        if not isinstance(obj[k], str):
            raise DescribeError("failed", f"the describer's {k} is not text")
    for k in ("tags", "instruments"):
        if not isinstance(obj[k], list) or not all(isinstance(x, str) for x in obj[k]):
            raise DescribeError("failed", f"the describer's {k} is not a list of text")
    if obj["vocals"] not in VOCALS:
        raise DescribeError("failed", f"the describer's vocals is {obj['vocals']!r}, not one of {', '.join(VOCALS)}")
    bpm = obj["bpm"]
    if not isinstance(bpm, int) or isinstance(bpm, bool) or not BPM_MIN <= bpm <= BPM_MAX:
        raise DescribeError("failed", f"the describer's bpm is {bpm!r}, not a whole number from {BPM_MIN} to {BPM_MAX}")
    return {"genre": clean_text(obj["genre"], GENRE_MAX_CHARS), "tags": _clean_list(obj["tags"]),
            "instruments": _clean_list(obj["instruments"]), "vocals": obj["vocals"], "bpm": bpm,
            "key": clean_text(obj["key"], KEY_MAX_CHARS),
            "time_signature": clean_text(obj["time_signature"], TIMESIG_MAX_CHARS),
            "mood": clean_text(obj["mood"], MOOD_MAX_CHARS), "style": clean_text(obj["style"], STYLE_MAX_CHARS)}


def _most_common(values: Sequence[str]) -> str:
    seen = [v for v in values if v]
    if not seen:
        return ""
    counts: Dict[str, int] = {}
    for v in seen:
        counts[v.lower()] = counts.get(v.lower(), 0) + 1
    best = max(counts.values())
    return next(v for v in seen if counts[v.lower()] == best)


def merge_answers(answers: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """One description from the windows' answers (module docstring)."""
    if len(answers) == 1:
        return dict(answers[0])

    def union(key: str) -> List[str]:
        out: List[str] = []
        for a in answers:
            for t in a[key]:
                if t.lower() not in (x.lower() for x in out):
                    out.append(t)
        return out[:TAGS_MAX]
    vocals = _most_common([a["vocals"] for a in answers if a["vocals"] != "unknown"]) or "unknown"
    return {"genre": _most_common([a["genre"] for a in answers]), "tags": union("tags"),
            "instruments": union("instruments"), "vocals": vocals,
            "bpm": int(round(statistics.median(a["bpm"] for a in answers))),
            "key": _most_common([a["key"] for a in answers]),
            "time_signature": _most_common([a["time_signature"] for a in answers]),
            "mood": _most_common([a["mood"] for a in answers]), "style": answers[0]["style"]}


def plan_windows(source_s: float, context: Optional[int]) -> List[Tuple[float, float]]:
    """The whole source when its audio, the prompt and the answer fit `context`, else `MAX_WINDOWS` windows of
    `WINDOW_S` at its start, middle and end (fewer when they would overlap)."""
    ctx = int(context) if isinstance(context, int) and context > 0 else DEFAULT_CONTEXT
    room_s = (ctx - PROMPT_RESERVE_TOKENS - DESCRIBE_MAX_TOKENS) / AUDIO_TOKENS_PER_S
    if source_s <= room_s:
        return [(0.0, round(source_s, 3))]
    w = min(WINDOW_S, max(10.0, room_s))
    starts = [0.0, max(0.0, source_s / 2 - w / 2), max(0.0, source_s - w)]
    out: List[Tuple[float, float]] = []
    for s in starts[:MAX_WINDOWS]:
        if not out or s >= out[-1][1]:
            out.append((round(s, 3), round(min(source_s, s + w), 3)))
    return out


# -- what the description fills ------------------------------------------------------------------

_VOCALS_WORDS = {"none": "instrumental", "male": "male vocals", "female": "female vocals", "mixed": "mixed vocals",
                 "choir": "choir"}


def keep_value(key: str, answer: Mapping[str, Any]) -> Any:
    """The value the description gives a kept attribute (`job_schemas.KEEP_FROM_SOURCE_KEYS`), in `keep`'s own
    shape (bpm an integer, vocals `vocals` or `instrumental`), or None when it gives none."""
    if key == "genre":
        return answer.get("genre") or None
    if key == "bpm":
        return answer.get("bpm")
    if key == "key":
        return answer.get("key") or None
    if key == "timesig":
        return answer.get("time_signature") or None
    if key == "mood":
        return answer.get("mood") or None
    if key == "vocals":
        v = answer.get("vocals")
        return None if v in (None, "unknown") else ("instrumental" if v == "none" else "vocals")
    return None


def _keep_words(key: str, value: Any, answer: Mapping[str, Any]) -> str:
    """A filled kept value as the style words the form builds from Keep (musicSimilar.ts `captionFromKeep`)."""
    if key == "bpm":
        return f"{value} BPM"
    if key == "timesig":
        return f"{value} time"
    if key == "vocals":
        return _VOCALS_WORDS.get(str(answer.get("vocals")), "with vocals")
    return str(value)


def merged_style(params: Mapping[str, Any], answer: Optional[Mapping[str, Any]]) -> Tuple[str, Dict[str, Any]]:
    """The style a same-style song on yue2.cpp sends, and what was done: the person's caption when typed, else
    the description's style line; then each `keep_from_source` value the description gives, as the form's Keep
    words, unless the style already names it. The caption as sent (`job_schemas.song_caption_sent`, the theme of
    new lyrics with it) stays first and whole. When the style is the description's own line, its genre and mood
    are already in it in other words (the live probe: "Acoustic folk, singer-songwriter, ..." then "Acoustic Folk /
    Singer-Songwriter" again), so only the tempo, key, time signature and vocals are added. `said` records the
    filled values and what had none."""
    base = js.song_caption_sent(params.get("caption") or "", params.get("lyrics_theme"))
    from_describer = not (params.get("caption") or "").strip()
    filled: Dict[str, Any] = {}
    missing: List[str] = []
    parts: List[str] = []
    line = ""
    if from_describer:
        line = (answer or {}).get("style") or ""
        if not line and answer:
            line = ", ".join(x for x in [answer.get("genre") or "", *(answer.get("tags") or [])] if x)
        base = js.song_caption_sent(line, params.get("lyrics_theme")) if line else base
    for key in params.get("keep_from_source") or []:
        value = keep_value(key, answer or {})
        if value is None:
            missing.append(key)
            continue
        filled[key] = value
        if line and key in ("genre", "mood"):
            continue
        words = _keep_words(key, value, answer or {})
        if words.lower() not in base.lower() and words.lower() not in ", ".join(parts).lower():
            parts.append(words)
    style = ", ".join(x for x in [base.strip(" ,"), *parts] if x)
    said = {"style_from": "describer" if from_describer else "typed", "keep_filled": filled,
            "keep_without_value": missing}
    return style, said


def needs_describer(params: Mapping[str, Any]) -> bool:
    """Whether a same-style job asks anything of the describer: an empty caption or a Keep box with no value."""
    return not (params.get("caption") or "").strip() or bool(params.get("keep_from_source"))


# -- the cache by audio hash ------------------------------------------------------------------


def cache_path(dpc_home: Path, content_hash: str) -> Optional[Path]:
    """`<music>/describe/<hash>.json` (the hash's colon a dash), or None for a hash that is not a safe name."""
    name = str(content_hash or "").replace(":", "-")
    if not _SAFE_HASH.match(name):
        return None
    return music_dir_of(dpc_home) / DESCRIBE_DIR_NAME / f"{name}.json"


def read_cached(dpc_home: Path, content_hash: Optional[str]) -> Optional[Dict[str, Any]]:
    """A kept `done` description of this audio, or None."""
    path = cache_path(dpc_home, content_hash) if content_hash else None
    if path is None:
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (not isinstance(doc, dict) or doc.get("schema") != DESCRIBE_SCHEMA or doc.get("status") != DONE
            or doc.get("source_content_hash") != content_hash or not isinstance(doc.get("answer"), dict)):
        return None
    return doc


def write_cached(dpc_home: Path, doc: Mapping[str, Any]) -> None:
    """Keep a `done` description by its audio hash (atomically; an existing one is replaced only by `refresh`)."""
    path = cache_path(dpc_home, doc.get("source_content_hash") or "")
    if path is None or doc.get("status") != DONE:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.stem + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def answer_sha256(doc: Mapping[str, Any]) -> str:
    """The hash of a description's answer as stored (the take's provenance names it)."""
    raw = json.dumps(doc.get("answer") or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


# -- the call ------------------------------------------------------------------------------------

Decoder = Callable[[Path, Path, Optional[float], Optional[float]], None]


def ffmpeg_window_decoder(ffmpeg: Optional[str] = None) -> Decoder:
    """Decode `src` (or `[start, start + length)` of it) to a 16 kHz mono 16-bit WAV at `dest` with ffmpeg: what the
    audio encoder hears, a small upload. Blocking; `DescribeError("decode_failed")` on a failure."""
    import shutil
    import subprocess

    def decode(src: Path, dest: Path, start: Optional[float], length: Optional[float]) -> None:
        exe = ffmpeg or shutil.which("ffmpeg")
        if not exe:
            raise DescribeError("decode_failed", "ffmpeg is not on PATH; the source is decoded with it for the "
                                                 "describer (install ffmpeg)")
        cmd = [exe, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
        if start:
            cmd += ["-ss", f"{start:.3f}"]
        cmd += ["-i", str(src)]
        if length:
            cmd += ["-t", f"{length:.3f}"]
        cmd += ["-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(DECODE_RATE), "-c:a", "pcm_s16le", "-f", "wav",
                str(dest)]
        kwargs: Dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=DECODE_TIMEOUT_S, **kwargs)
        except subprocess.TimeoutExpired:
            raise DescribeError("decode_failed", f"decoding the source took longer than {DECODE_TIMEOUT_S:g} s")
        except OSError as exc:
            raise DescribeError("decode_failed", f"ffmpeg could not be run: {exc}")
        if result.returncode != 0 or not dest.is_file():
            err = (result.stderr or b"").decode("utf-8", errors="replace").strip()[-300:]
            raise DescribeError("decode_failed", f"ffmpeg could not decode the source (rc {result.returncode}): {err}")
    return decode


def _model_of(provider: Any, alias: str) -> str:
    model = getattr(provider, "model", None)
    if isinstance(model, str) and model:
        return model
    gguf = (getattr(provider, "config", None) or {}).get("gguf_path")
    return Path(gguf).stem if gguf else alias


def _context_of(provider: Any) -> Optional[int]:
    ask = getattr(provider, "server_window", None)
    try:
        n = ask() if callable(ask) else None
    except Exception:
        n = None
    return n if isinstance(n, int) and n > 0 else None


def _usage(provider: Any) -> Dict[str, Any]:
    get = getattr(provider, "get_last_usage", None)
    try:
        usage = get() if callable(get) else None
    except Exception:
        usage = None
    if not isinstance(usage, Mapping):
        return {}
    keep = ("prompt_tokens", "completion_tokens", "finish_reason", "elapsed_s")
    return {k: usage.get(k) for k in keep if k in usage}


async def describe_audio(provider: Any, alias: str, source: Path, *, source_s: float, content_hash: Optional[str],
                         work_dir: Path, decode: Optional[Decoder] = None,
                         to_thread: Optional[Callable[..., Awaitable[Any]]] = None) -> Dict[str, Any]:
    """Describe the audio file `source` (`source_s` seconds long) with `provider` and answer the record (module
    docstring): `{schema, status: done, source_content_hash, alias, model, described_at, whole, windows, usage,
    answer, caveat}`. `DescribeError` for a provider that takes no audio, a decode that failed, or an answer that is
    not the schema's (with the reason)."""
    import asyncio
    run = to_thread or asyncio.to_thread
    supports = getattr(provider, "supports_audio", None)
    generate = getattr(provider, "generate_with_audio", None)
    if not callable(supports) or not callable(generate) or not supports():
        raise DescribeError("no_audio", f"alias {alias!r} takes no audio (a llama-server alias with mmproj_kind audio "
                                        "is the describer's engine)")
    decode = decode or ffmpeg_window_decoder()
    windows = plan_windows(float(source_s), _context_of(provider))
    whole = len(windows) == 1 and windows[0][0] == 0.0
    work_dir.mkdir(parents=True, exist_ok=True)
    answers: List[Dict[str, Any]] = []
    usage: List[Dict[str, Any]] = []
    for i, (start, end) in enumerate(windows):
        wav = work_dir / f"describe.w{i}.wav.part"
        try:
            await run(decode, source, wav, None if whole else start, None if whole else round(end - start, 3))
            try:
                text = await generate(describe_prompt(None if whole else (start, end)), wav,
                                      max_tokens=DESCRIBE_MAX_TOKENS, temperature=0.0,
                                      response_format=describe_response_format())
            except Exception as exc:
                raise DescribeError("failed", f"{type(exc).__name__}: {exc}"[:500]) from exc
        finally:
            try:
                wav.unlink()
            except OSError:
                pass
        try:
            answers.append(parse_describe_answer(text))
        except DescribeError as exc:
            cut = _usage(provider).get("finish_reason") == "length"
            raise DescribeError("failed", ("the answer was cut at the token ceiling; " if cut else "") + exc.message)
        usage.append(_usage(provider))
    return {"schema": DESCRIBE_SCHEMA, "status": DONE, "source_content_hash": content_hash, "alias": alias,
            "model": _model_of(provider, alias), "described_at": _now(), "whole": whole,
            "windows": [{"start_s": s, "end_s": e} for s, e in windows], "source_s": round(float(source_s), 3),
            "usage": usage, "answer": merge_answers(answers), "caveat": CAVEAT}


def skipped(reason: str, content_hash: Optional[str] = None) -> Dict[str, Any]:
    """The record of a description that did not run, with why (shown on the job and kept in its payload)."""
    return {"schema": DESCRIBE_SCHEMA, "status": SKIPPED, "source_content_hash": content_hash, "reason": reason,
            "answer": None, "described_at": _now()}


def failed(reason: str, alias: Optional[str], content_hash: Optional[str] = None) -> Dict[str, Any]:
    """The record of a description that ran and failed (never cached)."""
    return {"schema": DESCRIBE_SCHEMA, "status": FAILED, "source_content_hash": content_hash, "alias": alias,
            "reason": reason[:500], "answer": None, "described_at": _now()}


def seconds_or_none(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) \
        and value > 0 else None
