"""What a music file is: its audio-payload hash and its metadata (ADR-051 Q4).

Layer 3 of the decided track identity. Computed only for a new path or a changed
file (layer 2, path + size + mtime, is the library's job), always off the event
loop (the library calls these through `asyncio.to_thread`).

- **FLAC**: the MD5 the encoder stores in STREAMINFO over the decoded audio, read
  from the header in pure Python; used when it is not all zeroes (an encoder may
  leave it unset).
- **Everything else**: SHA-256 over an ffmpeg stream copy of the audio packets,
  `ffmpeg -i f -map 0:a -c:a copy -f hash -hash sha256 -`. Tags, cover art and the
  container's own headers are outside it, so a retag keeps the hash.
- **Without ffmpeg** (or when ffmpeg cannot read the file): SHA-256 over the whole
  file, marked `hash_kind: "file"`. A retag changes that hash.

Metadata (title, artist, album, duration, sample rate, channels) comes from
`ffprobe -show_format -show_streams -of json` when ffprobe is present; otherwise
the title is the file name and FLAC still gives its duration from STREAMINFO.

Every child goes through `run_probe` and so through the process registry
(ADR-046 point 5), with a timeout. It is entered as `device_probe`, the registry's
word for a short read-only probe bounded by its own timeout; a music-specific
subsystem name would need `process_registry.SUBSYSTEMS` to grow (a question for
the owner of the vocabulary, see the report).

Nothing here opens a file for writing: tags in watched folders are never written. It does read
the `DPC_PROVENANCE` record inside a FLAC or WAV file (`embedded_provenance`), defensively.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from . import provenance as _provenance
from .audio_tags import WAV_INFO_PROVENANCE_KEY, WAV_RECORD_READ_MAX, flac_read_comments, wav_read_provenance
from .schemas import HASH_KIND_AUDIO, HASH_KIND_FILE, HASH_KIND_FLAC

logger = logging.getLogger(__name__)

# The registry launcher the probes run as (see the module docstring).
PROBE_LAUNCHER = "device_probe"
FFMPEG_HASH_TIMEOUT_S = 120.0
FFPROBE_TIMEOUT_S = 15.0
READ_CHUNK = 1024 * 1024

AUDIO_EXTENSIONS = frozenset({".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wav"})
MIME_BY_EXTENSION = {
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".wav": "audio/wav",
}

Runner = Callable[..., "subprocess.CompletedProcess"]


def default_runner(cmd: Sequence[str], *, timeout: float) -> "subprocess.CompletedProcess":
    """Run a short command through the process registry (ADR-046). Imported at the
    call: the tools package loads the whole agent package with it."""
    from ..dpc_agent.tools.process import run_probe

    return run_probe(cmd, launcher=PROBE_LAUNCHER, timeout=timeout)


@dataclass
class MediaTools:
    """Where ffmpeg and ffprobe are, or None when absent. `discover()` reads PATH."""

    ffmpeg: Optional[str] = None
    ffprobe: Optional[str] = None
    runner: Runner = field(default=default_runner)

    @classmethod
    def discover(cls, runner: Optional[Runner] = None) -> "MediaTools":
        return cls(
            ffmpeg=shutil.which("ffmpeg"),
            ffprobe=shutil.which("ffprobe"),
            runner=runner or default_runner,
        )


@dataclass
class FlacStreamInfo:
    md5: str               # 32 hex characters; all zeroes when the encoder left it unset
    sample_rate: int
    channels: int
    total_samples: int
    bits_per_sample: int = 0

    @property
    def duration_s(self) -> Optional[float]:
        if self.sample_rate <= 0 or self.total_samples <= 0:
            return None
        return self.total_samples / self.sample_rate

    @property
    def md5_is_set(self) -> bool:
        return self.md5.strip("0") != ""


@dataclass
class Identity:
    """The result of identifying one file."""

    hash_kind: str
    content_hash: str
    size_bytes: int
    mtime_ns: int
    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    duration_s: Optional[float] = None
    sample_rate: Optional[int] = None
    channels: Optional[int] = None
    notes: list = field(default_factory=list)
    # The source content hash in a `DPC_EXPORT_OF` tag: the file is one of DPC's own MP3
    # exports (`export.py`), which the library never takes in as a track.
    export_of: Optional[str] = None
    # The small summary of the file's own `DPC_PROVENANCE` record (`provenance.read_summary`): engine,
    # seed, has_codes. None when the file has none or its record is bad (ignored, with a log line).
    provenance: Optional[Dict[str, Any]] = None


def _skip_id3v2(head: bytes) -> int:
    """The offset after a leading ID3v2 tag (some taggers put one before `fLaC`)."""
    if len(head) >= 10 and head[:3] == b"ID3":
        size = (head[6] & 0x7F) << 21 | (head[7] & 0x7F) << 14 | (head[8] & 0x7F) << 7 | (head[9] & 0x7F)
        footer = 10 if head[5] & 0x10 else 0
        return 10 + size + footer
    return 0


def read_flac_streaminfo(path: Path) -> Optional[FlacStreamInfo]:
    """STREAMINFO of a FLAC file, or None when the file is not FLAC.

    The layout (FLAC format, "METADATA_BLOCK_STREAMINFO"): after `fLaC`, a 4-byte
    block header (last-block flag + 7-bit type, 24-bit length); type 0 is
    STREAMINFO, 34 bytes, whose last 16 bytes are the MD5 of the decoded audio.
    """
    with open(path, "rb") as f:
        head = f.read(10)
        offset = _skip_id3v2(head)
        f.seek(offset)
        if f.read(4) != b"fLaC":
            return None
        header = f.read(4)
        if len(header) != 4 or (header[0] & 0x7F) != 0:
            return None
        length = int.from_bytes(header[1:4], "big")
        if length < 34:
            return None
        block = f.read(34)
    if len(block) != 34:
        return None
    # Bytes 10..17: sample rate (20 bits), channels - 1 (3), bits - 1 (5), total samples (36).
    bits = int.from_bytes(block[10:18], "big")
    sample_rate = bits >> 44
    channels = ((bits >> 41) & 0x7) + 1
    bits_per_sample = ((bits >> 36) & 0x1F) + 1
    total_samples = bits & 0xFFFFFFFFF
    return FlacStreamInfo(
        md5=block[18:34].hex(), sample_rate=sample_rate, channels=channels, total_samples=total_samples,
        bits_per_sample=bits_per_sample,
    )


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(READ_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_ffmpeg_hash(stdout: str) -> Optional[str]:
    for line in stdout.splitlines():
        line = line.strip()
        if line.upper().startswith("SHA256="):
            value = line.split("=", 1)[1].strip().lower()
            if len(value) == 64 and all(c in "0123456789abcdef" for c in value):
                return value
    return None


def audio_stream_sha256(path: Path, tools: MediaTools) -> Optional[str]:
    """SHA-256 of the audio packets by an ffmpeg stream copy, or None on any failure."""
    if not tools.ffmpeg:
        return None
    cmd = [tools.ffmpeg, "-nostdin", "-v", "error", "-i", str(path),
           "-map", "0:a", "-c:a", "copy", "-f", "hash", "-hash", "sha256", "-"]
    try:
        done = tools.runner(cmd, timeout=FFMPEG_HASH_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        logger.warning("music: ffmpeg hash timed out after %.0f s on %s", FFMPEG_HASH_TIMEOUT_S, path)
        return None
    except Exception as exc:  # a missing binary, a refused spawn at shutdown
        logger.warning("music: ffmpeg hash could not run on %s: %s", path, exc)
        return None
    out = done.stdout.decode("utf-8", "replace") if isinstance(done.stdout, bytes) else (done.stdout or "")
    if done.returncode != 0:
        err = done.stderr.decode("utf-8", "replace") if isinstance(done.stderr, bytes) else (done.stderr or "")
        logger.info("music: ffmpeg hash exit %s on %s: %s", done.returncode, path, err.strip()[-300:])
        return None
    return _parse_ffmpeg_hash(out)


def _tag(tags: Mapping[str, Any], name: str) -> Optional[str]:
    for key, value in tags.items():
        if isinstance(key, str) and key.casefold() == name and isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _as_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def probe_metadata(path: Path, tools: MediaTools) -> Dict[str, Any]:
    """Title, artist, album, duration, sample rate and channels from ffprobe; {} without it."""
    if not tools.ffprobe:
        return {}
    cmd = [tools.ffprobe, "-v", "error", "-show_format", "-show_streams",
           "-select_streams", "a:0", "-of", "json", str(path)]
    try:
        done = tools.runner(cmd, timeout=FFPROBE_TIMEOUT_S)
    except Exception as exc:
        logger.info("music: ffprobe could not run on %s: %s", path, exc)
        return {}
    if done.returncode != 0:
        return {}
    raw = done.stdout.decode("utf-8", "replace") if isinstance(done.stdout, bytes) else (done.stdout or "")
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    stream = streams[0] if streams and isinstance(streams[0], dict) else {}
    # MP3/MP4 keep tags on the format; Ogg and Opus keep them on the stream.
    tags: Dict[str, Any] = {}
    tags.update(stream.get("tags") or {})
    tags.update(fmt.get("tags") or {})
    result: Dict[str, Any] = {
        "title": _tag(tags, "title"),
        "artist": _tag(tags, "artist"),
        "album": _tag(tags, "album"),
        "duration_s": _as_float(fmt.get("duration")) or _as_float(stream.get("duration")),
        "sample_rate": _as_int(stream.get("sample_rate")),
        "channels": _as_int(stream.get("channels")),
        "export_of": _tag(tags, "dpc_export_of"),
    }
    # a FLAC / WAV `id3 ` chunk / MP3 all give `DPC_PROVENANCE`; a WAV written before 2026-11-26 gives `DPCP`
    raw = _tag(tags, _provenance.TAG.casefold()) or _tag(tags, WAV_INFO_PROVENANCE_KEY.casefold())
    if raw:
        result["provenance"] = _provenance.read_summary(raw, where=Path(path).name)
    return {k: v for k, v in result.items() if v is not None}


def embedded_provenance(path: Path) -> Optional[Dict[str, Any]]:
    """The summary of the `mp.provenance/1` record inside a FLAC or WAV file, read straight from
    the file with no ffprobe (PROVENANCE rules 5 and 6: a size cap, strict validation, a bad record
    ignored with a log line, never a crash). None for any other format, or no record."""
    suffix = Path(path).suffix.lower()
    try:
        if suffix == ".flac":
            raw = flac_read_comments(path, max_block_bytes=2 * 1024 * 1024).get(_provenance.TAG)
        elif suffix == ".wav":
            raw = wav_read_provenance(path, max_item_bytes=WAV_RECORD_READ_MAX)
        else:
            return None
        return _provenance.read_summary(raw, where=Path(path).name) if raw else None
    except Exception:  # a scan must never fail on a tag
        logger.info("music: could not read the provenance record of %s", Path(path).name, exc_info=True)
        return None


def identify(path: Path, tools: MediaTools) -> Identity:
    """Hash and probe one file. Blocking: call it through `asyncio.to_thread`.

    Raises `OSError` when the file cannot be read (gone, locked, unreachable)."""
    stat = path.stat()
    notes: list = []
    hash_kind: Optional[str] = None
    content_hash: Optional[str] = None
    flac = read_flac_streaminfo(path) if path.suffix.lower() == ".flac" else None
    if flac is not None and flac.md5_is_set:
        hash_kind, content_hash = HASH_KIND_FLAC, f"md5:{flac.md5}"
    if content_hash is None:
        digest = audio_stream_sha256(path, tools)
        if digest is not None:
            hash_kind, content_hash = HASH_KIND_AUDIO, f"sha256:{digest}"
        else:
            notes.append("ffmpeg absent" if not tools.ffmpeg else "ffmpeg could not read the audio")
    if content_hash is None:
        hash_kind, content_hash = HASH_KIND_FILE, f"sha256:{file_sha256(path)}"
    meta = probe_metadata(path, tools)
    embedded = embedded_provenance(path)
    if embedded is not None:
        meta["provenance"] = embedded
    ident = Identity(
        hash_kind=hash_kind, content_hash=content_hash,
        size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns, notes=notes, **meta,
    )
    if flac is not None:
        ident.duration_s = ident.duration_s if ident.duration_s is not None else flac.duration_s
        ident.sample_rate = ident.sample_rate or flac.sample_rate or None
        ident.channels = ident.channels or flac.channels or None
    return ident
