"""Lossy export of library tracks: MP3 (Mike, AI Studio group #210, 2026-11-22: "export or save to
mp3 is missing; think how to avoid duplicates and not turn the folder and its subfolders into
organisational chaos") and Opus for sending to messengers (Mike, group #171, 2026-11-25). The
design is CC's proposal [P]; the asks and the true-peak policy below are Mike's.

**Presets** (`PRESETS`): `mp3-v0` (default), `mp3-320` and `opus-160`. The two MP3 presets are
libmp3lame in an MP3 file with an ID3v2.4 tag. `opus-160` is Opus at 160 kbps (libopus, VBR) in an
**Ogg container with the `.opus` extension** (RFC 7845, "Ogg Opus"): every desktop webview
and every messenger plays it, ffmpeg and ffprobe read and write it everywhere, and its Vorbis
comments hold what ID3 holds for MP3 (see Tags). The Opus file is always 48 kHz; a source with
more than two channels is mixed to two.

**One export root, outside every watched folder.** The default is `<DPC home>/exports/music/`.
The library refuses to watch anything inside the DPC home except `<DPC home>/music/`
(`library._check_watchable`), so nothing written here is ever scanned in as a second track.
`[music] export_dir` (`DPC_MUSIC_EXPORT_DIR`) points it elsewhere, `~/Music` say; when that
place is inside a watched folder the export marker below still keeps the copies out of the
library, and `get_music_export_info` says so for the UI to warn.

**Flat and predictable.** A single track is `<root>/<Artist> - <Title>.mp3` (`<Title>.mp3`
without an artist; `.opus` for the Opus preset), with ` (320 kbps)` added for the 320 preset and
` (no limit)` for an export made with the true-peak limit switched off; a name another source already
holds gets a short suffix from this source's content hash, `Title (a1b2c3).mp3`. The only
subfolders are playlists, one level: `<root>/<Playlist>/NN - <Artist> - <Title>.mp3`. Names are
portable on Windows, Linux and macOS: no `<>:"/\\|?*` or control characters (`:` reads as
` -`, `?` and `*` are dropped, `"` becomes `'`, the rest `_`), no leading dots
(hidden on Unix), no trailing dots or spaces, no reserved Windows device names, at most 120
UTF-8 bytes for a file stem and 60 for a folder (the whole path stays under Windows' 260), and
collisions are compared case-insensitively after NFC (macOS hands out NFD names).

**No duplicates, by identity.** `<root>/.dpc-exports.json` (written atomically) maps
`(source content hash, format, preset, true-peak limit on or off)` to the file's path relative to
the root, its sha256 and `exported_at`; the limit is part of the identity because an unlimited
file is another file (a record without `true_peak_limit` is a limited one). The source is the library's own content hash (`md5:<pcm md5>` for a FLAC
take), so a renamed or moved library file is still the same source.

- The same source with the same preset is answered with the existing file
  (`already_exported`), never encoded again.
- When the indexed file is gone, the root (flat, not its playlist folders) is searched for
  a file of the preset's container whose ID3 `TXXX` (Opus: Vorbis comment) `DPC_EXPORT_OF`,
  `DPC_EXPORT_PRESET` and, for an unlimited export, `DPC_EXPORT_TP_LIMIT=off` name this source,
  preset and limit: a file the person renamed by hand is found that way and the index follows it
  (`relinked`). Reading the tag is a few KB per file, in pure Python. Only when no such file
  exists is it encoded again, under the expected name.
- A playlist export runs the same rule per track and then hard-links the single file into
  the playlist folder (no second encode, no second copy of the bytes); where a hard link is
  refused (another volume, FAT/exFAT, a share) the file is copied. Each run of the same
  playlist and preset replaces the folder's previous run: files it no longer lists are
  removed when they still carry this source's marker, a renamed playlist renames its folder,
  and a file the person put there is never touched.

**The scanner** (`library.py`) never takes an export in as a track: a file the index lists is
skipped, and any file whose tags carry `DPC_EXPORT_OF` is dropped from the scan with a log line,
wherever it was copied. The whole export root is not skipped by path: with `export_dir` set to
the person's own music folder that would hide their own music.

**The encode** goes to `<name>.<ext>.audio.part` (ffmpeg, no tags), is measured, and is written
with its tags to `<name>.<ext>.part` (MP3: the ID3v2.4 tag in front; Opus: the `OpusTags` packet
replaced in the Ogg stream, see Tags), which is moved to `<name>.<ext>` only when complete and
never over an existing file. `.part` is not an audio extension, so the
library never sees a half file, and a failure removes both parts.

**Tags.** ffmpeg 8.1.2's ID3 muxer writes `comment` and `lyrics` as `TXXX` frames, not `COMM`
and `USLT` (checked on this machine), so the tag is built here, by hand, as PROVENANCE §3 asks:
ID3v2.4, UTF-8, `TIT2` `TPE1` `TALB` `TCON` `TDRC` `COMM` `USLT` `TSSE`, and `TXXX` frames
`DPC_EXPORT_OF`, `DPC_EXPORT_PRESET` and `DPC_PROVENANCE`. Writing the tag after the encode
also keeps a 20 KB provenance record off the command line (Windows caps it at 32 767 chars).

**Tags of the Opus file.** `OpusTags` holds Vorbis comments, written here by hand the same way: the
ffmpeg Ogg muxer writes only a tiny tag packet, and a 20 KB provenance record must stay off the
command line. `TITLE` `ARTIST` `ALBUM` `GENRE` `DATE` `COMMENT` `LYRICS` (unsynchronised, as MP3's
`USLT`), `ENCODER`, and `DPC_EXPORT_OF`, `DPC_EXPORT_PRESET`, `DPC_PROVENANCE` (and
`DPC_EXPORT_TP_LIMIT` when off). The tag packet replaces ffmpeg's: the first Ogg pages after
`OpusHead` are rewritten, the audio pages are copied byte for byte (renumbered and re-checksummed
only when the tag packet needs another page count). Opus decoders expect the tag packet under
about 60 KB, so the provenance record is shortened to fit (the omitted parts are named by hash).

**Provenance** (`TXXX:DPC_PROVENANCE`, `dpc.export-provenance/1`): the source's identity (track
id, content hash, file sha256 and PCM MD5), the source file's own `DPC_PROVENANCE` carried over
unchanged when it has one (PROVENANCE rule 7), the take's sidecar (`dpc.take-sidecar/1`) for a
generated take, and the post step: tool and version, encoder, preset and settings, the limiter,
and `cmd_as_sent`, the ffmpeg argv of the encode that made the audio (Forge #209 R-F3). Rule 3
forbids local paths inside a file that may be shared, so exactly three elements of that argv
are reduced to file names: the ffmpeg binary, the source and the output; the record says so.

**The source's own record** (PROVENANCE §3, Mike 2026-11-21): a track made since then carries a
full `mp.provenance/1` inside its FLAC or WAV (a Vorbis comment `DPC_PROVENANCE`, or the same
`TXXX:DPC_PROVENANCE` frame in the WAV's `id3 ` chunk; a WAV written before 2026-11-26 holds it in a
`LIST/INFO` item `DPCP`, which is still read), read straight from the file when ffprobe is absent.
It is strictly validated (`provenance.parse_embedded`) and carried over whole
as `source_record`; the sidecar, which is the same record, then adds only its ids to `take`. The
record's `node_id` (the node's public id) is replaced by `withheld_on_export` in both: the node id is a
tracking identifier, so it stays in the library's copy and does not travel in an export. When
the library has no lyrics for the track, `USLT` is written from the file's own lyrics tag, else
from the record's `lyrics` (never for an instrumental).

**True peak** (EVOLUTION §11, Forge #209 R-F3; decided by Mike, group #171, 2026-11-25): a lossy
export keeps the -1 dBTP true-peak limit by default, with a switch in the export dialog
(`limit_true_peak=False`) to turn it off for one export; the library file is never touched. An
export with the limit off is encoded once, as it comes, and is measured and recorded all the
same (`limiter.enabled` false, `reached` says whether it happens to be under -1 dBTP). When the source's true peak is known and at or below -1 dBTP (the take's
`loudness.true_peak_dbtp`, else a measurement of the source) there is no limiter. Otherwise the
chain is `aresample=4x, alimiter (sample peak, -1.5 dBFS, lookahead), aresample back`. The
decoded MP3 is then measured with ebur128: an MP3 can overshoot the limiter (measured on this
machine: dense clipped pink noise limited to -1.2 dBTP came back at +0.5 dBTP from V0), so
while the result is above -1 dBTP it is encoded again with the limit lowered by the overshoot,
at most `MAX_ENCODES` times in all. An unlimited encode that lands above -1 dBTP is redone with
the limiter (not when the limit is off). The answer carries the measured result.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import stat as stat_module
import subprocess
import threading
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from . import provenance as mp_provenance
from .audio_tags import (WAV_INFO_PROVENANCE_KEY, WAV_RECORD_READ_MAX, _build_vorbis, _parse_vorbis,
                         flac_read_comments, wav_read_provenance)
from .id3 import (ID3_PADDING, ID3_READ_MAX_BYTES, _clean, _frame, _syncsafe, _unsyncsafe,  # noqa: F401 (re-exported)
                  build_id3v24, comm_frame, read_id3_txxx, text_frame, txxx_frame, uslt_frame)
from .identity import MediaTools, file_sha256, read_flac_streaminfo
from .loudness import LoudnessError, measure_loudness
from .store import atomic_write_text

logger = logging.getLogger(__name__)

INDEX_FILE = ".dpc-exports.json"
INDEX_SCHEMA = "dpc.music-exports/1"
PROVENANCE_SCHEMA = "dpc.export-provenance/1"
TAKE_SIDECAR_SCHEMA = "dpc.take-sidecar/1"
MARKER_TAG = "DPC_EXPORT_OF"
PRESET_TAG = "DPC_EXPORT_PRESET"
LIMIT_TAG = "DPC_EXPORT_TP_LIMIT"          # written only as "off": no tag means the limit was on
PROVENANCE_TAG = "DPC_PROVENANCE"
DEFAULT_EXPORT_SUBDIR = ("exports", "music")

FORMAT_MP3 = "mp3"
FORMAT_OPUS = "opus"
DEFAULT_PRESET = "mp3-v0"
NO_LIMIT_SUFFIX = " (no limit)"          # in the name of a single and of a playlist folder made without the limit
_MP3_MUX = ["-id3v2_version", "0", "-write_id3v1", "0", "-f", "mp3"]
# `ext` is the file extension, `ffmpeg_encoder` what `-encoders` must list, `mux` the output flags
# before the file name, `folder_suffix` what a playlist folder of this preset adds to its name.
PRESETS: Dict[str, Dict[str, Any]] = {
    "mp3-v0": {
        "format": FORMAT_MP3, "label": "MP3 V0 (VBR, about 245 kbps)", "name_suffix": "", "folder_suffix": "",
        "ext": ".mp3", "ffmpeg_encoder": "libmp3lame", "mux": _MP3_MUX,
        "args": ["-c:a", "libmp3lame", "-q:a", "0"],
        "settings": {"encoder": "libmp3lame", "mode": "vbr", "vbr_quality": 0},
    },
    "mp3-320": {
        "format": FORMAT_MP3, "label": "MP3 320 kbps (CBR)", "name_suffix": " (320 kbps)", "folder_suffix": " (320 kbps)",
        "ext": ".mp3", "ffmpeg_encoder": "libmp3lame", "mux": _MP3_MUX,
        "args": ["-c:a", "libmp3lame", "-b:a", "320k"],
        "settings": {"encoder": "libmp3lame", "mode": "cbr", "bitrate_kbps": 320},
    },
    "opus-160": {
        "format": FORMAT_OPUS, "label": "Opus 160 kbps (VBR, for messengers)", "name_suffix": "",
        "folder_suffix": " (Opus 160)",
        "ext": ".opus", "ffmpeg_encoder": "libopus", "mux": ["-f", "ogg"],
        "args": ["-c:a", "libopus", "-b:a", "160k", "-vbr", "on", "-application", "audio"],
        "settings": {"encoder": "libopus", "mode": "vbr", "bitrate_kbps": 160, "application": "audio",
                     "container": "ogg", "sample_rate": 48000},
    },
}
OPUS_SAMPLE_RATE = 48000
OPUS_TAGS_MAX_BYTES = 60 * 1024          # decoders expect the tag packet under about 60 KB
OPUS_PROVENANCE_MAX_BYTES = 40 * 1024
OGG_READ_MAX_BYTES = 1024 * 1024

TRUE_PEAK_CEILING_DBTP = -1.0
LIMITER_START_DBFS = -1.5
LIMITER_FLOOR_DBFS = -24.0          # alimiter's lowest limit (0.0625)
LIMITER_STEP_MARGIN_DB = 0.2
MAX_ENCODES = 4
OVERSAMPLE = 4
MP3_SAMPLE_RATES = frozenset({8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000})

ENCODE_TIMEOUT_S = 600.0
PROBE_TIMEOUT_S = 15.0
STEM_MAX_BYTES = 120
FOLDER_MAX_BYTES = 60
HASH_SUFFIX_RESERVE = 9              # " (abcdef)"
PROVENANCE_MAX_BYTES = 64 * 1024
SIDECAR_MAX_BYTES = 256 * 1024
_COPY_CHUNK = 1024 * 1024

_FORBIDDEN = frozenset('<>:"/\\|?*')
# How a forbidden character reads in a name: `Title: Part` -> `Title - Part`, `Why?` -> `Why`.
_REPLACE = {":": " -", '"': "'", "?": "", "*": "", "<": "_", ">": "_", "|": "_", "/": "_", "\\": "_"}
_RESERVED = frozenset({"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                       *(f"LPT{i}" for i in range(1, 10))})
_DATE_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")

Measure = Callable[[Path], Dict[str, Any]]


class ExportError(Exception):
    """An export the exporter refuses or cannot make; `code` is a short machine word."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def default_export_root(dpc_home: Path) -> Path:
    return Path(dpc_home).joinpath(*DEFAULT_EXPORT_SUBDIR)


def name_key(name: str) -> str:
    """How two names in one folder are compared: NFC, case-folded (NTFS and APFS are
    case-insensitive by default; ext4 is not, but a name that differs only by case would
    still be two exports of one name)."""
    return unicodedata.normalize("NFC", name).casefold()


def _path_key(path: Path) -> str:
    return os.path.normcase(unicodedata.normalize("NFC", os.path.normpath(str(path))))


# -- names ---------------------------------------------------------------------------------


def _truncate_utf8(text: str, max_bytes: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode("utf-8", errors="ignore")


def sanitize_component(text: Any, max_bytes: int = STEM_MAX_BYTES, fallback: str = "Untitled") -> str:
    """One portable file or folder name part (see the module docstring)."""
    s = unicodedata.normalize("NFC", str(text or ""))
    s = "".join(_REPLACE[c] if c in _FORBIDDEN else " " if (ord(c) < 32 or ord(c) == 127) else c for c in s)
    s = " ".join(s.split()).strip(" .")
    s = _truncate_utf8(s, max(1, max_bytes)).rstrip(" .")
    if not s:
        s = fallback
    if s.split(".")[0].rstrip(" ").upper() in _RESERVED:
        s = "_" + s
    return s


def track_stem(artist: Optional[str], title: Optional[str], preset: str = DEFAULT_PRESET,
               max_bytes: int = STEM_MAX_BYTES, limit: bool = True) -> str:
    """`<Artist> - <Title>` (or `<Title>`), sanitized, with the preset's suffix (and ` (no limit)`
    when the true-peak limit is off), short enough for a collision suffix to fit."""
    suffix = PRESETS.get(preset, {}).get("name_suffix", "") + ("" if limit else NO_LIMIT_SUFFIX)
    room = max_bytes - len(suffix.encode("utf-8")) - HASH_SUFFIX_RESERVE
    t = sanitize_component(title, room)
    a = sanitize_component(artist, room, fallback="") if artist and str(artist).strip() else ""
    base = f"{a} - {t}" if a else t
    return sanitize_component(base, room) + suffix


def export_id_of(source_hash: str, fmt: str, preset: str, limit: bool = True) -> str:
    """The stable id of an export: its identity (source content hash, format, preset, and
    `|nolimit` when the true-peak limit was off, so every id made before the switch is unchanged)."""
    return hashlib.sha256(f"{source_hash}|{fmt}|{preset}{'' if limit else '|nolimit'}".encode("utf-8")).hexdigest()[:20]


def record_limit(record: Dict[str, Any]) -> bool:
    """Whether an index record (or playlist record) was made with the true-peak limit on."""
    return record.get("true_peak_limit") is not False


def hash_digest(content_hash: str) -> str:
    """The hex part of a content hash (`md5:ab..` -> `ab..`); letters and digits only."""
    tail = str(content_hash).split(":", 1)[-1]
    return "".join(c for c in tail.lower() if c in "0123456789abcdef") or "0"


def pick_name(stem: str, ext: str, taken: Set[str], content_hash: str) -> str:
    """The first free name: `stem.ext`, then `stem (<6 hex>).ext`, `stem (<12 hex>).ext`,
    then `stem (<6 hex>-n).ext`. `taken` holds `name_key`s."""
    h = hash_digest(content_hash)
    candidates = [stem, f"{stem} ({h[:6]})", f"{stem} ({h[:12]})"]
    candidates += [f"{stem} ({h[:6]}-{n})" for n in range(2, 1000)]
    for cand in candidates:
        name = cand + ext
        if name_key(name) not in taken:
            return name
    raise ExportError("name_taken", f"no free name for {stem}{ext}")


def number_width(count: int) -> int:
    return max(2, len(str(count)))


# -- Ogg Opus, written and read by hand ---------------------------------------------------------


def _ogg_crc_table() -> List[int]:
    table = []
    for i in range(256):
        r = i << 24
        for _ in range(8):
            r = ((r << 1) ^ 0x04C11DB7) if r & 0x80000000 else (r << 1)
        table.append(r & 0xFFFFFFFF)
    return table


_OGG_CRC = _ogg_crc_table()


def ogg_crc(data: bytes) -> int:
    """The Ogg page checksum: CRC-32, polynomial 0x04C11DB7, no reflection, initial value 0."""
    crc = 0
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _OGG_CRC[((crc >> 24) & 0xFF) ^ b]
    return crc


@dataclass
class _OggPage:
    header_type: int
    granule: int
    serial: int
    seq: int
    lacing: bytes
    body: bytes

    def to_bytes(self) -> bytes:
        head = (b"OggS\x00" + bytes([self.header_type]) + self.granule.to_bytes(8, "little")
                + self.serial.to_bytes(4, "little") + self.seq.to_bytes(4, "little") + b"\x00\x00\x00\x00"
                + bytes([len(self.lacing)]) + self.lacing)
        raw = head + self.body
        crc = ogg_crc(raw).to_bytes(4, "little")
        return raw[:22] + crc + raw[26:]


def _read_ogg_page(f) -> Optional[Tuple[_OggPage, bytes]]:
    """The next page of an Ogg stream and its exact bytes, None at the end; `ExportError` when the
    stream is not Ogg."""
    head = f.read(27)
    if not head:
        return None
    if len(head) < 27 or head[:4] != b"OggS" or head[4] != 0:
        raise ExportError("encode_failed", "the Opus encode is not a valid Ogg stream")
    nseg = head[26]
    lacing = f.read(nseg)
    if len(lacing) != nseg:
        raise ExportError("encode_failed", "the Opus encode is cut off inside an Ogg page")
    size = sum(lacing)
    body = f.read(size)
    if len(body) != size:
        raise ExportError("encode_failed", "the Opus encode is cut off inside an Ogg page")
    page = _OggPage(head[5], int.from_bytes(head[6:14], "little"), int.from_bytes(head[14:18], "little"),
                    int.from_bytes(head[18:22], "little"), lacing, body)
    return page, head + lacing + body


def ogg_pages_for_packet(packet: bytes, serial: int, first_seq: int, final_granule: int = 0) -> List[_OggPage]:
    """The pages that carry one packet by itself: 255 segments of up to 255 bytes at most per page,
    the continuation flag on every page after the first, and granule position -1 on a page where the
    packet does not end."""
    lacing = [255] * (len(packet) // 255) + [len(packet) % 255]
    pages: List[_OggPage] = []
    pos = 0
    for start in range(0, len(lacing), 255):
        seg = lacing[start:start + 255]
        body = packet[pos:pos + sum(seg)]
        pos += sum(seg)
        last = start + 255 >= len(lacing)
        pages.append(_OggPage(0x01 if pages else 0x00, final_granule if last else 0xFFFFFFFFFFFFFFFF, serial,
                              first_seq + len(pages), bytes(seg), body))
    return pages


def read_opus_tags_packet(path: Path, max_bytes: int = OGG_READ_MAX_BYTES) -> Optional[bytes]:
    """The `OpusTags` packet of an Ogg Opus file (up to `max_bytes` of it), or None when the file is
    not Ogg Opus, has no tag packet or it is longer than `max_bytes`."""
    try:
        with open(path, "rb") as f:
            first = _read_ogg_page(f)
            if first is None or not first[0].body.startswith(b"OpusHead"):
                return None
            packet = b""
            while True:
                nxt = _read_ogg_page(f)
                if nxt is None:
                    return None
                page = nxt[0]
                packet += page.body
                if len(packet) > max_bytes:
                    return None
                if page.lacing and page.lacing[-1] < 255:
                    return packet if packet.startswith(b"OpusTags") else None
    except (OSError, ExportError):
        return None


def read_opus_comments(path: Path, max_bytes: int = OGG_READ_MAX_BYTES) -> Dict[str, str]:
    """The Vorbis comments of an Ogg Opus file (the first value of each upper-cased name); {} for a
    file that is not Ogg Opus or whose tag packet cannot be read."""
    packet = read_opus_tags_packet(path, max_bytes)
    parsed = _parse_vorbis(packet[8:]) if packet else None
    out: Dict[str, str] = {}
    for key, value in (parsed[1] if parsed else []):
        out.setdefault(key.upper(), value)
    return out


def build_opus_tags(vendor: bytes, comments: Sequence[Tuple[str, str]]) -> bytes:
    """An `OpusTags` packet. A comment name must be printable ASCII without `=`; a value is UTF-8 with
    no NUL."""
    clean = []
    for key, value in comments:
        key = "".join(c for c in str(key) if 0x20 <= ord(c) <= 0x7D and c != "=").upper()
        if key:
            clean.append((key, _clean(value)))
    return b"OpusTags" + _build_vorbis(vendor, clean)


def retag_ogg_opus(src: Path, comments: Sequence[Tuple[str, str]], out: Path) -> Tuple[str, int]:
    """Write `src` (an Ogg Opus file from ffmpeg) to `out` with its tag packet replaced by one that
    holds `comments`; returns `(sha256 of the written file, size)`. The `OpusHead` page and the
    audio pages are copied as they are; the audio pages are renumbered and re-checksummed only when
    the new tag packet needs a different number of pages. The original vendor string is kept."""
    h = hashlib.sha256()
    size = 0
    with open(src, "rb") as fi, open(out, "wb") as fo:
        def put(raw: bytes) -> None:
            nonlocal size
            fo.write(raw)
            h.update(raw)
            size += len(raw)

        first = _read_ogg_page(fi)
        if first is None or not first[0].body.startswith(b"OpusHead") or not first[0].header_type & 0x02:
            raise ExportError("encode_failed", "the Opus encode does not start with an OpusHead page")
        put(first[1])
        serial = first[0].serial
        old_pages = 0
        packet = b""
        while True:
            nxt = _read_ogg_page(fi)
            if nxt is None:
                raise ExportError("encode_failed", "the Opus encode has no tag packet")
            page = nxt[0]
            old_pages += 1
            packet += page.body
            ends = [i for i, n in enumerate(page.lacing) if n < 255]
            if ends:
                if ends[0] != len(page.lacing) - 1:
                    raise ExportError("encode_failed", "the Opus encode puts audio on the page of its tag packet")
                break
        if not packet.startswith(b"OpusTags"):
            raise ExportError("encode_failed", "the Opus encode has no OpusTags packet")
        parsed = _parse_vorbis(packet[8:])
        vendor = parsed[0] if parsed else b"libopus"
        tags = build_opus_tags(vendor, comments)
        if len(tags) > OPUS_TAGS_MAX_BYTES:
            raise ExportError("encode_failed", f"the Opus tags are {len(tags)} bytes, over the {OPUS_TAGS_MAX_BYTES} "
                                               "a decoder is expected to read")
        new_pages = ogg_pages_for_packet(tags, serial, first_seq=1)
        for p in new_pages:
            put(p.to_bytes())
        delta = len(new_pages) - old_pages
        while True:
            nxt = _read_ogg_page(fi)
            if nxt is None:
                break
            page, raw = nxt
            if delta:
                page.seq += delta
                raw = page.to_bytes()
            put(raw)
        fo.flush()
        os.fsync(fo.fileno())
    return f"sha256:{h.hexdigest()}", size


def _tag_value(tags: Dict[str, str], name: str) -> Optional[str]:
    for key, value in tags.items():
        if key.casefold() == name.casefold() and value and value.strip():
            return value.strip()
    return None


def read_export_tags(path: Path) -> Optional[Dict[str, Any]]:
    """`{source, preset, limit}` when `path` is a file DPC exported, by its `DPC_EXPORT_OF` (an MP3's
    `TXXX`, an Ogg Opus file's Vorbis comment); None otherwise. `preset` is None for a file written
    before presets were tagged; `limit` is False only when the true-peak limit was off. Cheap: reads
    the leading tag only."""
    suffix = Path(path).suffix.lower()
    if suffix == ".mp3":
        tags = read_id3_txxx(Path(path))
    elif suffix in (".opus", ".ogg"):
        tags = read_opus_comments(Path(path))
    else:
        return None
    marker = _tag_value(tags, MARKER_TAG)
    if not marker:
        return None
    return {"source": marker, "preset": _tag_value(tags, PRESET_TAG),
            "limit": (_tag_value(tags, LIMIT_TAG) or "").lower() != "off"}


def read_export_marker(path: Path) -> Optional[Tuple[str, Optional[str]]]:
    """`(source content hash, preset or None)` when `path` is a file that DPC exported (MP3 or Ogg
    Opus), by its `DPC_EXPORT_OF`; None otherwise. Cheap: reads the leading tag only."""
    found = read_export_tags(path)
    return (found["source"], found["preset"]) if found else None


# -- the source ---------------------------------------------------------------------------------


@dataclass
class ExportSource:
    """One library track as the exporter sees it; the command layer fills it from the library
    by id (the UI never names a file)."""

    track_id: str
    path: Path
    content_hash: str
    title: str
    artist: Optional[str] = None
    album: Optional[str] = None
    lyrics: Optional[str] = None
    lyrics_language: Optional[str] = None
    true_peak_dbtp: Optional[float] = None
    true_peak_basis: Optional[str] = None
    take_record: Optional[Dict[str, Any]] = None
    origin: Optional[str] = None


@dataclass
class SkippedTrack:
    """A playlist position the exporter does not export, and why."""

    track_id: str
    title: Optional[str]
    reason: str
    code: str = "skipped"


def read_take_sidecar(path: Path) -> Optional[Dict[str, Any]]:
    """The take's `<name>.json` sidecar next to its file, or None: the older `dpc.take-sidecar/1`, or
    the `mp.provenance/1` record a take carries since 2026-11-21 (the same record as in the file)."""
    sidecar = Path(path).with_suffix(".json")
    try:
        if not sidecar.is_file() or sidecar.stat().st_size > SIDECAR_MAX_BYTES:
            return None
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if isinstance(data, dict) and data.get("schema") in (TAKE_SIDECAR_SCHEMA, mp_provenance.SCHEMA):
        return data
    return None


# -- the exporter ---------------------------------------------------------------------------------


@dataclass
class _Encoded:
    cmd_as_sent: List[str]
    limiter: Dict[str, Any]
    true_peak_out_dbtp: Optional[float]


@dataclass
class _Index:
    exports: List[Dict[str, Any]] = field(default_factory=list)
    playlists: List[Dict[str, Any]] = field(default_factory=list)


class MusicExporter:
    """Exports library tracks as MP3 under one root. Blocking: the commands call it through
    `asyncio.to_thread`. One lock serialises every export, so two clicks never write the
    same index or name at once."""

    def __init__(self, root: Path, *, tools: Optional[MediaTools] = None, measure: Optional[Measure] = None):
        self.root = Path(root)
        self._tools = tools
        self._tools_given = tools is not None
        self._measure_fn = measure
        self._lock = threading.RLock()
        self._index: Optional[_Index] = None
        self._export_keys: frozenset = frozenset()
        self._encoders_checked: Set[str] = set()
        self._ffmpeg_version: Optional[str] = None

    # -- tools ----------------------------------------------------------------------------

    @property
    def tools(self) -> MediaTools:
        if self._tools is None:
            self._tools = MediaTools.discover()
        return self._tools

    def _measure(self, path: Path) -> Dict[str, Any]:
        if self._measure_fn is not None:
            return self._measure_fn(path)
        return measure_loudness(path, self.tools)

    def check_encoder(self, preset: Optional[str] = None) -> None:
        """Raise `no_ffmpeg`, `no_mp3_encoder` or `no_opus_encoder` unless ffmpeg with the preset's
        encoder (libmp3lame, libopus) is there. Asked once per encoder and exporter; a missing
        ffmpeg is asked again on the next export."""
        encoder = PRESETS[self.check_preset(preset)]["ffmpeg_encoder"]
        tools = self.tools
        if not tools.ffmpeg and not self._tools_given:
            self._tools = tools = MediaTools.discover()  # installed since the last try?
            self._encoders_checked.clear()
        if not tools.ffmpeg:
            raise ExportError("no_ffmpeg", f"ffmpeg is not on PATH; export encodes with ffmpeg's {encoder} "
                                           "(install ffmpeg and try again)")
        if encoder in self._encoders_checked:
            return
        try:
            done = tools.runner([tools.ffmpeg, "-hide_banner", "-encoders"], timeout=PROBE_TIMEOUT_S)
        except Exception as exc:
            raise ExportError("no_ffmpeg", f"ffmpeg could not run: {exc}")
        out = done.stdout.decode("utf-8", "replace") if isinstance(done.stdout, bytes) else (done.stdout or "")
        if encoder not in out:
            if encoder == "libopus":
                raise ExportError("no_opus_encoder", "this ffmpeg has no libopus encoder; install an ffmpeg build "
                                                     "with libopus (distribution packages and the gyan.dev and "
                                                     "evermeet builds have it) and try again")
            raise ExportError("no_mp3_encoder", "this ffmpeg has no libmp3lame encoder; install an ffmpeg build "
                                                "with LAME (distribution packages and the gyan.dev and "
                                                "evermeet builds have it) and try again")
        try:
            ver = tools.runner([tools.ffmpeg, "-version"], timeout=PROBE_TIMEOUT_S)
            text = ver.stdout.decode("utf-8", "replace") if isinstance(ver.stdout, bytes) else (ver.stdout or "")
            first = (text.splitlines() or [""])[0]
            self._ffmpeg_version = first.split(" Copyright")[0].strip() or None
        except Exception:
            self._ffmpeg_version = None
        self._encoders_checked.add(encoder)

    def _probe(self, path: Path) -> Tuple[Dict[str, str], Optional[int], Optional[int]]:
        """`(tags, sample rate, channels)` of the source from ffprobe; FLAC STREAMINFO
        gives the rate and channels without it."""
        tags: Dict[str, str] = {}
        rate = channels = None
        tools = self.tools
        if tools.ffprobe:
            cmd = [tools.ffprobe, "-v", "error", "-show_format", "-show_streams", "-select_streams", "a:0",
                   "-of", "json", str(path)]
            try:
                done = tools.runner(cmd, timeout=PROBE_TIMEOUT_S)
                raw = done.stdout.decode("utf-8", "replace") if isinstance(done.stdout, bytes) else (done.stdout or "")
                data = json.loads(raw) if done.returncode == 0 else {}
            except Exception:
                data = {}
            streams = data.get("streams") or []
            stream = streams[0] if streams and isinstance(streams[0], dict) else {}
            for src in (stream.get("tags") or {}, (data.get("format") or {}).get("tags") or {}):
                for k, v in src.items():
                    if isinstance(k, str) and isinstance(v, str):
                        tags[k] = v
            try:
                rate = int(stream.get("sample_rate")) if stream.get("sample_rate") else None
                channels = int(stream.get("channels")) if stream.get("channels") else None
            except (TypeError, ValueError):
                rate = channels = None
        if rate is None and path.suffix.lower() == ".flac":
            try:
                info = read_flac_streaminfo(path)
            except OSError:
                info = None
            if info is not None:
                rate, channels = info.sample_rate or None, info.channels or None
        return tags, rate, channels

    # -- the index ----------------------------------------------------------------------------

    @property
    def index_path(self) -> Path:
        return self.root / INDEX_FILE

    def _abs(self, rel: str) -> Path:
        return self.root.joinpath(*[p for p in str(rel).split("/") if p])

    def _rel(self, path: Path) -> str:
        return Path(os.path.relpath(path, self.root)).as_posix()

    def _load(self) -> _Index:
        if self._index is not None:
            return self._index
        index = _Index()
        path = self.index_path
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("not a JSON object")
                index.exports = [e for e in data.get("exports") or [] if self._valid_export(e)]
                index.playlists = [p for p in data.get("playlists") or [] if self._valid_playlist(p)]
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                aside = path.with_name(f"{path.name}.corrupt-{_stamp()}")
                logger.error("music export: %s does not parse (%s); moved aside to %s", path, exc, aside.name)
                try:
                    os.replace(path, aside)
                except OSError:
                    logger.warning("music export: could not move %s aside", path, exc_info=True)
        self._index = index
        self._refresh_keys()
        return index

    @staticmethod
    def _valid_export(e: Any) -> bool:
        return (isinstance(e, dict) and all(isinstance(e.get(k), str) and e.get(k)
                                            for k in ("source_hash", "format", "preset", "path")))

    @staticmethod
    def _valid_playlist(p: Any) -> bool:
        return (isinstance(p, dict) and isinstance(p.get("playlist_id"), str) and isinstance(p.get("preset"), str)
                and isinstance(p.get("folder"), str) and isinstance(p.get("files"), list))

    def _save(self) -> None:
        index = self._load()
        doc = {"schema": INDEX_SCHEMA,
               "note": "DPC's record of its lossy exports; delete it and DPC finds its files again by their tags",
               "exports": index.exports, "playlists": index.playlists}
        atomic_write_text(self.index_path, json.dumps(doc, ensure_ascii=False, indent=1))
        self._refresh_keys()

    def _refresh_keys(self) -> None:
        index = self._index or _Index()
        keys = {_path_key(self._abs(e["path"])) for e in index.exports}
        for p in index.playlists:
            keys.update(_path_key(self._abs(f["path"])) for f in p["files"] if isinstance(f, dict) and f.get("path"))
        self._export_keys = frozenset(keys)

    def is_export_path(self, path: Any) -> Optional[str]:
        """Why the library must not take `path` in as a track (a file this index lists), or
        None. Called by the library's scan on the event loop: the index is read once, then
        kept in memory."""
        try:
            if self._index is None:
                with self._lock:
                    self._load()
            if _path_key(Path(path)) in self._export_keys:
                return f"an export listed in {self.index_path}"
        except Exception:
            logger.debug("music export: the index check failed for %s", path, exc_info=True)
        return None

    def _find_record(self, source_hash: str, preset: str, limit: bool = True) -> Optional[Dict[str, Any]]:
        fmt = PRESETS[preset]["format"]
        for e in self._load().exports:
            if (e["source_hash"] == source_hash and e["format"] == fmt and e["preset"] == preset
                    and record_limit(e) == limit):
                return e
        return None

    def _taken(self, folder: Path, *, skip: Optional[Dict[str, Any]] = None) -> Set[str]:
        """The names in `folder` on disk, and the names the index holds there for other
        exports (a file deleted by hand keeps its name for its own source)."""
        taken: Set[str] = set()
        try:
            for name in os.listdir(folder):
                taken.add(name_key(name))
        except FileNotFoundError:
            pass
        for e in self._load().exports:
            if e is skip:
                continue
            p = self._abs(e["path"])
            if _path_key(p.parent) == _path_key(folder):
                taken.add(name_key(p.name))
        return taken

    def _find_by_marker(self, source_hash: str, preset: str, limit: bool = True) -> Optional[Path]:
        """A file of the preset's container directly in the root whose tag names this source, preset
        and true-peak limit."""
        ext = PRESETS[preset]["ext"]
        try:
            names = sorted(os.listdir(self.root))
        except FileNotFoundError:
            return None
        for name in names:
            if not name.lower().endswith(ext):
                continue
            path = self.root / name
            found = read_export_tags(path)
            if (found and found["source"] == source_hash and (found["preset"] or DEFAULT_PRESET) == preset
                    and found["limit"] == limit and path.is_file()):
                return path
        return None

    # -- one track ----------------------------------------------------------------------------

    def export_track(self, source: ExportSource, preset: Optional[str] = None,
                     limit_true_peak: bool = True) -> Dict[str, Any]:
        """Export one track (see the module docstring); returns `{track_id, path, name,
        already_exported, relinked, preset, limit_true_peak, limited_db, true_peak_out_dbtp,
        exported_at}`. `limit_true_peak=False` makes the unlimited file, which is a separate export
        from the limited one. Raises `ExportError`."""
        preset = self.check_preset(preset)
        limit = bool(limit_true_peak)
        with self._lock:
            index = self._load()
            record = self._find_record(source.content_hash, preset, limit)
            if record is not None and self._abs(record["path"]).is_file():
                return self._answer(source, record, already=True)
            found = self._find_by_marker(source.content_hash, preset, limit)
            if found is not None:
                logger.info("music export: %s found by its tag at %s; the index follows it",
                            source.content_hash, found.name)
                fresh = self._record_for(source, preset, found, previous=record, limit=limit)
                if record is not None:
                    index.exports.remove(record)
                index.exports.append(fresh)
                self._save()
                return self._answer(source, fresh, already=True, relinked=True)
            self.check_encoder(preset)
            if not source.path.is_file():
                raise ExportError("no_file", f"the track's file is not there: {source.path.name}")
            self.root.mkdir(parents=True, exist_ok=True)
            stem = track_stem(source.artist, source.title, preset, limit=limit)
            name = pick_name(stem, PRESETS[preset]["ext"], self._taken(self.root, skip=record), source.content_hash)
            final = self._write_export(source, preset, self.root / name, limit)
            if record is not None:
                index.exports.remove(record)
            record = self._record_for(source, preset, final.path, encoded=final, limit=limit)
            index.exports.append(record)
            self._save()
            return self._answer(source, record, already=False)

    @staticmethod
    def check_preset(preset: Optional[str]) -> str:
        chosen = preset or DEFAULT_PRESET
        if chosen not in PRESETS:
            raise ExportError("invalid_preset", f"preset must be one of {', '.join(PRESETS)}")
        return chosen

    def _record_for(self, source: ExportSource, preset: str, path: Path, *,
                    encoded: Optional["_Written"] = None, previous: Optional[Dict[str, Any]] = None,
                    limit: bool = True) -> Dict[str, Any]:
        if encoded is not None:
            sha, size, limiter, tp_out, exported_at = (encoded.sha256, encoded.size, encoded.limiter,
                                                       encoded.true_peak_out_dbtp, encoded.exported_at)
        else:
            sha, size = f"sha256:{file_sha256(path)}", path.stat().st_size
            limiter = (previous or {}).get("limiter")
            tp_out = (previous or {}).get("true_peak_out_dbtp")
            exported_at = (previous or {}).get("exported_at") or _now()
        record = {"source_hash": source.content_hash, "format": PRESETS[preset]["format"], "preset": preset,
                  "path": self._rel(path), "sha256": sha, "size_bytes": size, "exported_at": exported_at,
                  "track_id": source.track_id, "title": source.title, "artist": source.artist,
                  "limiter": limiter, "true_peak_out_dbtp": tp_out}
        if not limit:
            record["true_peak_limit"] = False  # absent means on, so older records read as limited
        return record

    def _answer(self, source: ExportSource, record: Dict[str, Any], *, already: bool,
                relinked: bool = False) -> Dict[str, Any]:
        path = self._abs(record["path"])
        limiter = record.get("limiter") or {}
        return {"track_id": source.track_id, "path": str(path), "name": path.name,
                "already_exported": already, "relinked": relinked, "preset": record["preset"],
                "limit_true_peak": record_limit(record),
                "limited_db": limiter.get("ceiling_dbtp") if limiter.get("applied") else None,
                "true_peak_out_dbtp": record.get("true_peak_out_dbtp"),
                "exported_at": record.get("exported_at"), "export_root": str(self.root)}

    # -- the encode ---------------------------------------------------------------------------

    def _write_export(self, source: ExportSource, preset: str, final: Path, limit: bool = True) -> "_Written":
        audio_part = final.with_name(final.name + ".audio.part")
        tag_part = final.with_name(final.name + ".part")
        try:
            tags, rate, channels = self._probe(source.path)
            tp_in, basis = source.true_peak_dbtp, source.true_peak_basis
            if tp_in is None:
                try:
                    tp_in = self._measure(source.path).get("true_peak_dbtp")
                    basis = "measured on the source before the export (ffmpeg ebur128)" if tp_in is not None else None
                except LoudnessError as exc:
                    logger.info("music export: the source's true peak is unknown (%s); the limiter is on", exc)
            encoded = self._encode(source.path, audio_part, preset, tp_in, basis, rate, channels, limit)
            if PRESETS[preset]["format"] == FORMAT_OPUS:
                record = self._provenance(source, preset, tags, encoded, OPUS_PROVENANCE_MAX_BYTES)
                sha, size = retag_ogg_opus(audio_part, self._opus_comments(source, preset, tags, record, limit), tag_part)
            else:
                record = self._provenance(source, preset, tags, encoded)
                tag = build_id3v24(self._frames(source, preset, tags, record, limit))
                sha, size = self._join(tag, audio_part, tag_part)
            placed = _move_no_clobber(tag_part, final)
            return _Written(path=placed, sha256=sha, size=size, limiter=encoded.limiter,
                            true_peak_out_dbtp=encoded.true_peak_out_dbtp, exported_at=record["dpc_postprocess"]["exported_at"])
        finally:
            for p in (audio_part, tag_part):
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    logger.warning("music export: could not remove %s", p, exc_info=True)

    @staticmethod
    def _out_rate(rate: Optional[int], preset: str = DEFAULT_PRESET) -> Optional[int]:
        """The `-ar` to ask for, or None to keep the source's rate. Opus is always 48 kHz; MP3 keeps
        any rate it has, else the nearest one it has below."""
        if PRESETS[preset]["format"] == FORMAT_OPUS:
            return None if rate == OPUS_SAMPLE_RATE else OPUS_SAMPLE_RATE
        if rate is None or rate in MP3_SAMPLE_RATES:
            return None
        return 48000 if rate > 48000 else 44100 if rate > 32000 else 32000

    @staticmethod
    def limiter_filter(rate: int, limit_dbfs: float, latency: bool = True) -> str:
        lin = max(0.0625, min(1.0, 10 ** (limit_dbfs / 20.0)))
        opts = f"limit={lin:.5f}:level=false" + (":latency=true" if latency else "")
        return f"aresample={rate * OVERSAMPLE},alimiter={opts},aresample={rate}"

    def _encode_cmd(self, src: Path, out: Path, preset: str, filt: Optional[str], out_rate: Optional[int],
                    channels: Optional[int]) -> List[str]:
        cmd = [self.tools.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
               "-map", "0:a:0", "-map_metadata", "-1", "-map_chapters", "-1"]
        if filt:
            cmd += ["-af", filt]
        if out_rate:
            cmd += ["-ar", str(out_rate)]
        if channels and channels > 2:
            cmd += ["-ac", "2"]
        cmd += list(PRESETS[preset]["args"])
        cmd += list(PRESETS[preset]["mux"]) + [str(out)]
        return cmd

    def _run(self, cmd: List[str]) -> Tuple[int, str]:
        try:
            done = self.tools.runner(cmd, timeout=ENCODE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise ExportError("encode_failed", f"ffmpeg did not finish the MP3 within {ENCODE_TIMEOUT_S:.0f} s")
        except Exception as exc:
            raise ExportError("encode_failed", f"ffmpeg could not run: {exc}")
        err = done.stderr.decode("utf-8", "replace") if isinstance(done.stderr, bytes) else (done.stderr or "")
        return done.returncode, err

    def _encode(self, src: Path, out: Path, preset: str, tp_in: Optional[float], basis: Optional[str],
                rate: Optional[int], channels: Optional[int], limit_on: bool = True) -> _Encoded:
        out_rate = self._out_rate(rate, preset)
        work_rate = out_rate or rate or 48000
        apply = limit_on and (tp_in is None or tp_in > TRUE_PEAK_CEILING_DBTP)
        limit = LIMITER_START_DBFS
        latency = True
        encodes = 0
        tp_out: Optional[float] = None
        cmd: List[str] = []
        while True:
            filt = self.limiter_filter(work_rate, limit, latency) if apply else None
            cmd = self._encode_cmd(src, out, preset, filt, out_rate, channels)
            rc, err = self._run(cmd)
            if rc != 0 and apply and latency and "latency" in err:
                latency = False  # an ffmpeg older than alimiter's latency option
                continue
            encodes += 1
            if rc != 0:
                raise ExportError("encode_failed", f"ffmpeg could not encode the {PRESETS[preset]['ext'].lstrip('.').upper()} "
                                                   f"(rc {rc}): {err.strip()[-400:]}")
            try:
                tp_out = self._measure(out).get("true_peak_dbtp")
            except LoudnessError as exc:
                logger.warning("music export: the MP3's true peak could not be measured: %s", exc)
                tp_out = None
            if not limit_on or tp_out is None or tp_out <= TRUE_PEAK_CEILING_DBTP or encodes >= MAX_ENCODES:
                break
            if not apply:
                apply = True
                continue
            limit = max(LIMITER_FLOOR_DBFS, limit - (tp_out - TRUE_PEAK_CEILING_DBTP) - LIMITER_STEP_MARGIN_DB)
        limiter: Dict[str, Any] = {
            "applied": apply,
            "enabled": limit_on,
            "ceiling_dbtp": TRUE_PEAK_CEILING_DBTP,
            "policy": ("limit to -1 dBTP on lossy export by default, a switch in the export dialog turns it off; "
                       "the library file is never changed (EVOLUTION §11, Forge #209 R-F3; decided by Mike, "
                       "AI Studio group #171, 2026-11-25)" if limit_on else
                       "the true-peak limit was switched off by the person for this export (Mike's switch, "
                       "AI Studio group #171, 2026-11-25); the audio is encoded as it comes and measured"),
            "true_peak_in_dbtp": tp_in,
            "true_peak_in_basis": basis,
            "true_peak_out_dbtp": tp_out,
            "reached": tp_out is not None and tp_out <= TRUE_PEAK_CEILING_DBTP,
            "encodes": encodes,
        }
        if apply:
            limiter.update({"method": f"ffmpeg alimiter (sample peak, lookahead) at {OVERSAMPLE}x oversampling; "
                                      "the decoded MP3 measured with ebur128 and encoded again with a lower limit "
                                      "while above the ceiling",
                            "limit_dbfs": round(limit, 2), "latency_compensated": latency})
        return _Encoded(cmd_as_sent=cmd, limiter=limiter, true_peak_out_dbtp=tp_out)

    def _redact(self, cmd: List[str], src: Path, out: Path) -> List[str]:
        swap = {str(self.tools.ffmpeg): "ffmpeg", str(src): src.name, str(out): out.name}
        return [swap.get(a, a) for a in cmd]

    def _provenance(self, source: ExportSource, preset: str, tags: Dict[str, str], encoded: _Encoded,
                    max_bytes: int = PROVENANCE_MAX_BYTES) -> Dict[str, Any]:
        audio_part = Path(encoded.cmd_as_sent[-1])
        opus = PRESETS[preset]["format"] == FORMAT_OPUS
        flac = read_flac_streaminfo(source.path) if source.path.suffix.lower() == ".flac" else None
        pcm_md5 = f"md5:{flac.md5}" if flac is not None and flac.md5_is_set else None
        record: Dict[str, Any] = {
            "schema": PROVENANCE_SCHEMA,
            "note": "the post step of a lossy export; the source's own record, when it has one, is carried as "
                    "source_record (PROVENANCE rule 7)",
            "export_of": {
                "track_id": source.track_id, "content_hash": source.content_hash, "file": source.path.name,
                "container": source.path.suffix.lower().lstrip("."), "sha256": f"sha256:{file_sha256(source.path)}",
                "pcm_md5": pcm_md5,
                "pcm_md5_basis": "FLAC STREAMINFO MD5 of the decoded audio" if pcm_md5 else "not a FLAC with a stored MD5",
                "origin": source.origin,
            },
            "dpc_postprocess": {
                "step": "lossy_export", "tool": "ffmpeg", "tool_version": self._ffmpeg_version,
                "encoder": PRESETS[preset]["settings"]["encoder"], "preset": preset,
                "settings": dict(PRESETS[preset]["settings"]), "limiter": encoded.limiter,
                "cmd_as_sent": self._redact(encoded.cmd_as_sent, source.path, audio_part),
                "cmd_paths": "exact argv, except the ffmpeg binary, the source and the output, which are reduced to "
                             "file names (PROVENANCE rule 3: no local paths in a file that may be shared)",
                "tags": ("Vorbis comments in the OpusTags packet, written by DPC after the encode (the Ogg pages "
                         "after OpusHead are replaced, the audio pages are copied)" if opus else
                         "ID3v2.4 written by DPC after the encode (ffmpeg writes COMM and USLT as TXXX)"),
                "exported_at": _now(),
            },
        }
        # ffprobe returns the `id3 ` chunk's TXXX as `DPC_PROVENANCE` and a legacy WAV INFO item as `DPCP`
        carried = (_tag_value(tags, PROVENANCE_TAG) or _tag_value(tags, WAV_INFO_PROVENANCE_KEY)
                   or self._embedded_record_text(source.path))
        parsed: Optional[Dict[str, Any]] = None
        if carried:
            parsed, problem = mp_provenance.parse_embedded(carried, max_bytes=PROVENANCE_MAX_BYTES)
            record["source_record"] = mp_provenance.withhold_node_id(parsed) if parsed is not None else None
            if parsed is None:
                record["source_record_note"] = f"the source's DPC_PROVENANCE was not carried over: {problem}"
        if source.take_record is not None:
            take = source.take_record
            if parsed is not None and take.get("schema") == mp_provenance.SCHEMA:
                # The sidecar is the same record as the file's: only what the file cannot hold.
                take = {k: take.get(k) for k in ("schema", "take_id", "library_track_id") if k in take}
            record["take"] = mp_provenance.withhold_node_id(take) if take.get("schema") == mp_provenance.SCHEMA else take
        text = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        if len(text.encode("utf-8")) > max_bytes:
            for key in ("take", "source_record"):
                if record.get(key) is not None:
                    blob = json.dumps(record[key], ensure_ascii=False, sort_keys=True).encode("utf-8")
                    record[key] = {"omitted": f"over {max_bytes} bytes",
                                   "sha256": "sha256:" + hashlib.sha256(blob).hexdigest()}
        return record

    @staticmethod
    def _embedded_record_text(path: Path) -> Optional[str]:
        """The source's own provenance record read straight from a FLAC or WAV file, for when ffprobe
        is absent or does not return it."""
        cap = PROVENANCE_MAX_BYTES * 4
        try:
            if path.suffix.lower() == ".flac":
                return flac_read_comments(path, max_block_bytes=cap).get(PROVENANCE_TAG)
            if path.suffix.lower() == ".wav":
                return wav_read_provenance(path, max_item_bytes=WAV_RECORD_READ_MAX)   # the cap every WAV reader uses
        except Exception:
            logger.info("music export: could not read the embedded record of %s", path.name, exc_info=True)
        return None

    @staticmethod
    def _lyrics_for_tag(source: ExportSource, tags: Dict[str, str], record: Dict[str, Any]) -> Optional[str]:
        """The text of the USLT frame: the library's lyrics, else the source file's own lyrics tag, else
        the lyrics inside its provenance record (an instrumental has none)."""
        if source.lyrics and source.lyrics.strip():
            return source.lyrics
        for name in ("unsyncedlyrics", "lyrics"):
            text = _tag_value(tags, name)
            if text:
                return text
        src = record.get("source_record")
        if isinstance(src, dict) and not src.get("instrumental"):
            lyrics = src.get("lyrics")
            if isinstance(lyrics, str) and lyrics.strip() and lyrics != mp_provenance.NOT_STORED:
                return lyrics
        return None

    def _frames(self, source: ExportSource, preset: str, tags: Dict[str, str], record: Dict[str, Any],
                limit: bool = True) -> List[bytes]:
        frames = [text_frame("TIT2", source.title or source.path.stem)]
        if source.artist:
            frames.append(text_frame("TPE1", source.artist))
        album = source.album or _tag_value(tags, "album")
        if album:
            frames.append(text_frame("TALB", album))
        genre = _tag_value(tags, "genre")
        if genre:
            frames.append(text_frame("TCON", genre))
        date = _tag_value(tags, "date")
        if date and _DATE_RE.match(date):
            frames.append(text_frame("TDRC", date))
        comment = _tag_value(tags, "comment")
        if comment:
            frames.append(comm_frame(comment))
        lyrics = self._lyrics_for_tag(source, tags, record)
        if lyrics:
            frames.append(uslt_frame(lyrics, source.lyrics_language))
        frames.append(text_frame("TSSE", f"DPC MP3 export, {PRESETS[preset]['label']}, "
                                         f"{self._ffmpeg_version or 'ffmpeg'} libmp3lame"))
        frames.append(txxx_frame(MARKER_TAG, source.content_hash))
        frames.append(txxx_frame(PRESET_TAG, preset))
        if not limit:
            frames.append(txxx_frame(LIMIT_TAG, "off"))
        frames.append(txxx_frame(PROVENANCE_TAG, json.dumps(record, ensure_ascii=False, separators=(",", ":"))))
        return frames

    def _opus_comments(self, source: ExportSource, preset: str, tags: Dict[str, str], record: Dict[str, Any],
                       limit: bool = True) -> List[Tuple[str, str]]:
        """The Vorbis comments of the Opus file: what `_frames` writes as ID3, under their Vorbis names."""
        out: List[Tuple[str, str]] = [("TITLE", source.title or source.path.stem)]
        if source.artist:
            out.append(("ARTIST", source.artist))
        album = source.album or _tag_value(tags, "album")
        if album:
            out.append(("ALBUM", album))
        genre = _tag_value(tags, "genre")
        if genre:
            out.append(("GENRE", genre))
        date = _tag_value(tags, "date")
        if date and _DATE_RE.match(date):
            out.append(("DATE", date))
        comment = _tag_value(tags, "comment")
        if comment:
            out.append(("COMMENT", comment))
        lyrics = self._lyrics_for_tag(source, tags, record)
        if lyrics:
            out.append(("LYRICS", lyrics))
        out.append(("ENCODER", f"DPC export, {PRESETS[preset]['label']}, {self._ffmpeg_version or 'ffmpeg'} libopus"))
        out.append((MARKER_TAG, source.content_hash))
        out.append((PRESET_TAG, preset))
        if not limit:
            out.append((LIMIT_TAG, "off"))
        out.append((PROVENANCE_TAG, json.dumps(record, ensure_ascii=False, separators=(",", ":"))))
        return out

    @staticmethod
    def _join(tag: bytes, audio: Path, out: Path) -> Tuple[str, int]:
        h = hashlib.sha256(tag)
        size = len(tag)
        with open(audio, "rb") as fi, open(out, "wb") as fo:
            fo.write(tag)
            for chunk in iter(lambda: fi.read(_COPY_CHUNK), b""):
                fo.write(chunk)
                h.update(chunk)
                size += len(chunk)
            fo.flush()
            os.fsync(fo.fileno())
        return f"sha256:{h.hexdigest()}", size

    # -- a playlist ---------------------------------------------------------------------------

    def export_playlist(self, playlist_id: str, name: str, items: Sequence[Any],
                        preset: Optional[str] = None,
                        on_track: Optional[Callable[[Dict[str, Any]], None]] = None,
                        limit_true_peak: bool = True) -> Dict[str, Any]:
        """Export a playlist into `<root>/<name>/NN - <Artist> - <Title>.<ext>` (see the module
        docstring). `items` are `ExportSource`s and `SkippedTrack`s in playlist order; the
        skipped ones get no number. `on_track` is called with each single track's answer.
        A playlist run is one per (playlist, preset, true-peak limit on or off).
        Returns `{folder, tracks: [...], removed, kept}`."""
        preset = self.check_preset(preset)
        limit = bool(limit_true_peak)
        sources = [i for i in items if isinstance(i, ExportSource)]
        if not sources:
            raise ExportError("empty_playlist", "the playlist has no track that can be exported")
        with self._lock:
            index = self._load()
            self.root.mkdir(parents=True, exist_ok=True)
            previous = next((p for p in index.playlists
                             if p["playlist_id"] == playlist_id and p["preset"] == preset
                             and record_limit(p) == limit), None)
            folder = self._playlist_folder(playlist_id, name, preset, previous, limit)
            folder.mkdir(parents=True, exist_ok=True)
            width = number_width(len(sources))
            owned = {name_key(Path(f["path"]).name): f for f in (previous or {}).get("files", [])
                     if isinstance(f, dict) and f.get("path")}
            results: List[Dict[str, Any]] = []
            planned: List[Dict[str, Any]] = []
            n = 0
            for item in items:
                if isinstance(item, SkippedTrack):
                    results.append({"track_id": item.track_id, "title": item.title, "status": "skipped",
                                    "code": item.code, "reason": item.reason})
                    continue
                n += 1
                try:
                    single = self.export_track(item, preset, limit)
                except ExportError as exc:
                    results.append({"track_id": item.track_id, "title": item.title, "status": "error",
                                    "code": exc.code, "reason": exc.message})
                    continue
                if on_track is not None:
                    on_track(single)
                fname = f"{n:0{width}d} - {track_stem(item.artist, item.title, DEFAULT_PRESET)}{PRESETS[preset]['ext']}"
                dst = folder / fname
                placed = self._place(Path(single["path"]), dst, item.content_hash, owned)
                if placed["status"] == "error":
                    results.append({"track_id": item.track_id, "title": item.title, "status": "error",
                                    "code": placed["code"], "reason": placed["reason"]})
                    continue
                planned.append({"path": self._rel(dst), "source_hash": item.content_hash,
                                "track_id": item.track_id})
                results.append({"track_id": item.track_id, "title": item.title, "status": "ok",
                                "path": str(dst), "name": dst.name, "single_path": single["path"],
                                "already_exported": single["already_exported"], "placed": placed["how"],
                                "limited_db": single["limited_db"]})
            keep_keys = {name_key(Path(p["path"]).name) for p in planned}
            removed, kept = self._prune(folder, owned, keep_keys)
            record = {"playlist_id": playlist_id, "name": name, "preset": preset, "folder": self._rel(folder),
                      "exported_at": _now(), "files": planned}
            if not limit:
                record["true_peak_limit"] = False
            if previous is not None:
                index.playlists.remove(previous)
            index.playlists.append(record)
            self._save()
            return {"folder": str(folder), "folder_name": folder.name, "preset": preset, "tracks": results,
                    "removed": removed, "kept": kept, "export_root": str(self.root)}

    def _playlist_folder(self, playlist_id: str, name: str, preset: str,
                         previous: Optional[Dict[str, Any]], limit: bool = True) -> Path:
        suffix = PRESETS[preset]["folder_suffix"] + ("" if limit else NO_LIMIT_SUFFIX)
        base = sanitize_component(name, FOLDER_MAX_BYTES - len(suffix.encode("utf-8")), fallback="Playlist") + suffix
        others = {name_key(self._abs(p["folder"]).name) for p in self._load().playlists
                  if p["playlist_id"] != playlist_id or p["preset"] != preset or record_limit(p) != limit}
        prev_dir = self._abs(previous["folder"]) if previous else None
        try:
            on_disk = {name_key(n) for n in os.listdir(self.root)}
        except FileNotFoundError:
            on_disk = set()

        def free(folder_name: str) -> bool:
            key = name_key(folder_name)
            if key in others:
                return False
            if prev_dir is not None and key == name_key(prev_dir.name):
                return True  # this playlist's own folder from its last run
            return key not in on_disk

        short = "".join(c for c in playlist_id.lower() if c.isalnum())[-6:] or "list"
        target_name = base if free(base) else f"{base} ({short})"
        target = self.root / target_name
        if prev_dir is not None and prev_dir.is_dir() and prev_dir.name != target_name:
            # The playlist was renamed (or only its case changed): its folder follows.
            same_folder = name_key(prev_dir.name) == name_key(target_name)
            if not target.exists():
                return target if self._rename(prev_dir, target) else prev_dir
            if same_folder:
                # A case-only rename: one folder on NTFS and APFS, but on a case-sensitive
                # filesystem `target` may be another folder that a rename would replace.
                try:
                    one = os.path.samefile(prev_dir, target)
                except OSError:
                    one = False
                if one:
                    return target if self._rename(prev_dir, target) else prev_dir
                return prev_dir
        return target

    @staticmethod
    def _rename(src: Path, dst: Path) -> bool:
        try:
            os.rename(src, dst)
            return True
        except OSError:
            logger.warning("music export: could not rename %s to %s", src, dst.name, exc_info=True)
            return False

    def _place(self, single: Path, dst: Path, source_hash: str, owned: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """Put the single export at `dst`: unchanged when it is already there, else a hard link,
        else a copy. A file in the way is replaced only when an earlier run of this playlist
        wrote it."""
        if dst.exists():
            try:
                if os.path.samefile(single, dst):
                    return {"status": "ok", "how": "unchanged"}
            except OSError:
                pass
            marker = read_export_marker(dst)
            if marker and marker[0] == source_hash and dst.stat().st_size == single.stat().st_size:
                return {"status": "ok", "how": "unchanged"}
            if name_key(dst.name) not in owned:
                return {"status": "error", "code": "name_taken",
                        "reason": f"{dst.name} is already in the folder and DPC did not write it"}
            try:
                dst.unlink()
            except OSError as exc:
                return {"status": "error", "code": "write_failed", "reason": f"could not replace {dst.name}: {exc}"}
        try:
            os.link(single, dst)
            return {"status": "ok", "how": "hardlink"}
        except (OSError, NotImplementedError, AttributeError):
            pass
        part = dst.with_name(dst.name + ".part")
        try:
            shutil.copyfile(single, part)
            os.replace(part, dst)
            return {"status": "ok", "how": "copy"}
        except OSError as exc:
            try:
                part.unlink()
            except OSError:
                pass
            return {"status": "error", "code": "write_failed", "reason": f"could not write {dst.name}: {exc}"}

    @staticmethod
    def _prune(folder: Path, owned: Dict[str, Dict[str, Any]], keep: Set[str]) -> Tuple[List[str], List[str]]:
        """Remove the files an earlier run of this playlist wrote that this run does not list,
        when they still carry the source marker they were written with; keep the others."""
        removed: List[str] = []
        kept: List[str] = []
        for key, f in owned.items():
            if key in keep:
                continue
            path = folder / Path(f["path"]).name
            if not path.exists():
                continue
            marker = read_export_marker(path)
            if marker and marker[0] == f.get("source_hash"):
                try:
                    path.unlink()
                    removed.append(path.name)
                    continue
                except OSError:
                    logger.warning("music export: could not remove the stale %s", path, exc_info=True)
            kept.append(path.name)
        return removed, kept

    # -- what exists ------------------------------------------------------------------------------

    def _playlist_copies(self, record: Dict[str, Any], single: Path) -> List[Dict[str, Any]]:
        """The files in playlist folders that hold this export's song: every file of a playlist
        run of the same preset that lists this source hash and is on disk now. `how` says
        whether the file is a hard link of the single (the same bytes on disk) or a copy.
        Deleting the single touches none of them."""
        out: List[Dict[str, Any]] = []
        for p in self._load().playlists:
            if p["preset"] != record["preset"] or record_limit(p) != record_limit(record):
                continue
            for f in p["files"]:
                if not isinstance(f, dict) or not f.get("path") or f.get("source_hash") != record["source_hash"]:
                    continue
                path = self._abs(f["path"])
                try:
                    if not stat_module.S_ISREG(path.stat().st_mode):
                        continue
                except OSError:
                    continue
                try:
                    linked = os.path.samefile(single, path)
                except OSError:
                    linked = False
                out.append({"playlist_id": p["playlist_id"], "playlist_name": p.get("name") or p["playlist_id"],
                            "folder": path.parent.name, "path": str(path), "name": path.name,
                            "how": "hardlink" if linked else "copy"})
        return out

    def exports_for(self, hashes: Dict[str, List[str]]) -> Dict[str, List[Dict[str, Any]]]:
        """`{track_id: [export, ...]}` for the single exports whose file is there now, for
        the track ids `hashes` maps each content hash to. The index is checked against the
        disk on every call: a file deleted by hand is left out of the answer (its index
        entry stays, so a later export of the same source finds the name free again and
        encodes it anew); `size_bytes` is the file's size now, `export_id` is what
        `delete_export` takes. `playlists` names the playlist-folder files that hold the
        same song (`_playlist_copies`), so a person sees what a delete leaves behind."""
        with self._lock:
            out: Dict[str, List[Dict[str, Any]]] = {}
            for e in self._load().exports:
                ids = hashes.get(e["source_hash"])
                if not ids:
                    continue
                path = self._abs(e["path"])
                try:
                    st = path.stat()
                except OSError:
                    continue
                if not stat_module.S_ISREG(st.st_mode):
                    continue
                limiter = e.get("limiter") or {}
                item = {"export_id": export_id_of(e["source_hash"], e["format"], e["preset"], record_limit(e)),
                        "preset": e["preset"], "format": e["format"], "limit_true_peak": record_limit(e),
                        "path": str(path), "name": path.name,
                        "size_bytes": st.st_size, "exported_at": e.get("exported_at"),
                        "limited_db": limiter.get("ceiling_dbtp") if limiter.get("applied") else None,
                        "true_peak_out_dbtp": e.get("true_peak_out_dbtp"),
                        "playlists": self._playlist_copies(e, path)}
                for tid in ids:
                    out.setdefault(tid, []).append(dict(item))
            return out

    # -- delete one export ---------------------------------------------------------------------

    def _within_root(self, real: Path) -> bool:
        """Whether the already-resolved `real` lies strictly inside the resolved root."""
        root_real = Path(os.path.realpath(self.root))
        return real != root_real and root_real in real.parents

    def _safe_target(self, path: Path) -> Path:
        """The place to unlink for an index path: its directory resolved (symlinks and `..`
        followed) must be inside the resolved root, and the file name must be one plain
        component. The name itself is not resolved, so a symlink standing there is removed
        as a link and its target is never touched."""
        name = path.name
        if name in ("", ".", "..") or "/" in name or "\\" in name:
            raise ExportError("outside_export_root", f"not a file name inside the export folder: {path}")
        parent_real = Path(os.path.realpath(path.parent))
        root_real = Path(os.path.realpath(self.root))
        if parent_real != root_real and root_real not in parent_real.parents:
            raise ExportError("outside_export_root", f"{path} is outside the export folder {self.root}; it is not deleted")
        return parent_real / name

    def _record_by_id_or_path(self, export_id: Optional[str], path: Optional[str]) -> Dict[str, Any]:
        index = self._load()
        if export_id:
            for e in index.exports:
                if export_id_of(e["source_hash"], e["format"], e["preset"], record_limit(e)) == export_id:
                    return e
            raise ExportError("unknown_export", f"no export {export_id}")
        raw = Path(str(path))
        if not raw.is_absolute():
            raw = self.root / raw
        real = Path(os.path.realpath(raw))
        if not self._within_root(real):
            raise ExportError("outside_export_root",
                              f"{path} is outside the export folder {self.root}; it is not deleted")
        key = _path_key(real)
        for e in index.exports:
            if _path_key(Path(os.path.realpath(self._abs(e["path"])))) == key:
                return e
        raise ExportError("unknown_export", f"{real.name} is not a single export DPC lists; it is not deleted")

    def delete_export(self, *, export_id: Optional[str] = None, path: Optional[str] = None) -> Dict[str, Any]:
        """Delete one single export's file and its index entry. Exactly one of `export_id`
        (from `exports_for`) or `path` (absolute, or relative to the root). Only a file the
        index lists can go, and only inside the resolved export root: a traversal, a symlinked
        folder that leads out, or a path the index does not list is refused
        (`outside_export_root`, `unknown_export`). A file already gone drops the entry
        (`file_missing`). A file another program holds open (Windows) or that cannot be
        removed leaves the entry and raises `file_in_use` / `permission_denied` /
        `delete_failed`. Playlist copies are not touched: a playlist run owns them; the answer
        names the ones that stay (`playlist_copies`, as `exports_for` lists them), so the caller
        can say so. A hard-linked copy keeps the audio on disk after the single is gone."""
        if bool(export_id) == bool(path):
            raise ExportError("invalid_params", "give export_id or path, exactly one of them")
        with self._lock:
            record = self._record_by_id_or_path(export_id, path)
            target = self._safe_target(self._abs(record["path"]))
            name = target.name
            staying = self._playlist_copies(record, target)  # read before the unlink: `how` needs the link
            deleted = False
            if os.path.isdir(target) and not os.path.islink(target):
                raise ExportError("delete_failed", f"{name} is a folder, not an export file; it is not deleted")
            try:
                os.unlink(target)
                deleted = True
            except FileNotFoundError:
                pass
            except PermissionError as exc:
                if os.name == "nt":
                    raise ExportError("file_in_use", f"could not delete {name}: Windows refused (a player may have it "
                                                     f"open, or it is read-only). Close it and try again.") from exc
                raise ExportError("permission_denied", f"could not delete {name}: {exc.strerror or exc}") from exc
            except OSError as exc:
                raise ExportError("delete_failed", f"could not delete {name}: {exc.strerror or exc}") from exc
            index = self._load()
            if record in index.exports:
                index.exports.remove(record)
            self._save()
            return {"export_id": export_id_of(record["source_hash"], record["format"], record["preset"],
                                              record_limit(record)),
                    "path": str(target), "name": name, "preset": record["preset"], "format": record["format"],
                    "track_id": record.get("track_id"), "deleted": deleted, "file_missing": not deleted,
                    "playlist_copies": staying}


@dataclass
class _Written:
    path: Path
    sha256: str
    size: int
    limiter: Dict[str, Any]
    true_peak_out_dbtp: Optional[float]
    exported_at: str


def _move_no_clobber(src: Path, dst: Path) -> Path:
    """Move `src` to `dst` without replacing a file that appeared there meanwhile; such a
    file makes it `dst` with a numbered suffix instead. `os.rename` refuses an existing target
    on Windows; on POSIX `os.link` does (where links work), else the check is made first."""
    candidate = dst
    for n in range(1, 100):
        try:
            if os.name == "nt":
                os.rename(src, candidate)
                return candidate
            try:
                os.link(src, candidate)
            except FileExistsError:
                raise
            except OSError:
                if candidate.exists():
                    raise FileExistsError(str(candidate))
                os.rename(src, candidate)
                return candidate
            os.unlink(src)
            return candidate
        except FileExistsError:
            candidate = dst.with_name(f"{dst.stem} ({n + 1}){dst.suffix}")
    raise ExportError("name_taken", f"no free name for {dst.name}")
