"""A small reader for the ID3v2 SYLT frame (synchronised lyrics), standard library only.

ffprobe does not surface SYLT (checked with ffmpeg 8.1.2 on a tag holding both a USLT
and a SYLT frame: only the USLT came back), so the lyrics chain's tags step reads it
here. Read only; the file is opened `rb`.

Supported: ID3v2.2 (frame `SLT`), v2.3 and v2.4 tags; the tag-wide unsynchronisation of
v2.2/v2.3 and the per-frame one of v2.4; a v2.3/v2.4 extended header; the data-length
indicator and grouping byte of a frame. Text encodings 0 (Latin-1), 1 (UTF-16 with BOM),
2 (UTF-16BE) and 3 (UTF-8). Timestamp format 2 (milliseconds) becomes seconds.

Timestamp format 1 (absolute time in MPEG frames) is **not converted**: the time of a
frame depends on the stream's sample rate and layer, which this reader does not parse,
and a guess would shift every line. Such a frame is reported in `skipped` and the chain
falls through to the next tag/step (USLT and the rest). Compressed or encrypted frames
are skipped the same way.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

# A tag with a large cover picture is a few MB; anything over this is not read.
MAX_TAG_BYTES = 32 * 1024 * 1024


@dataclass
class SyltResult:
    lines: List[dict] = field(default_factory=list)
    language: Optional[str] = None
    skipped: List[str] = field(default_factory=list)


def _synchsafe(b: bytes) -> int:
    return (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]


def _ununsync(data: bytes) -> bytes:
    return data.replace(b"\xff\x00", b"\xff")


def _split_terminated(data: bytes, pos: int, encoding: int) -> Tuple[bytes, int]:
    """`(raw text, position after the terminator)`; the terminator is one NUL for
    encodings 0 and 3 and a two-byte NUL on an even offset for 1 and 2."""
    if encoding in (1, 2):
        i = pos
        while i + 1 < len(data):
            if data[i] == 0 and data[i + 1] == 0:
                return data[pos:i], i + 2
            i += 2
        return data[pos:], len(data)
    i = data.find(b"\x00", pos)
    if i < 0:
        return data[pos:], len(data)
    return data[pos:i], i + 1


def _decode(raw: bytes, encoding: int) -> str:
    if encoding == 0:
        return raw.decode("latin-1")
    if encoding == 1:
        if raw.startswith(b"\xff\xfe"):
            return raw[2:].decode("utf-16-le", "replace")
        if raw.startswith(b"\xfe\xff"):
            return raw[2:].decode("utf-16-be", "replace")
        return raw.decode("utf-16-le", "replace")  # no BOM: the common writer's choice
    if encoding == 2:
        return raw.decode("utf-16-be", "replace")
    return raw.decode("utf-8", "replace")


def parse_sylt_frame(body: bytes) -> Tuple[Optional[dict], Optional[str]]:
    """`({"language", "content_type", "lines"}, None)` of one SYLT frame body, or
    `(None, reason)` when it cannot be used."""
    if len(body) < 7:
        return None, "SYLT frame is too short"
    encoding, language, fmt, content_type = body[0], body[1:4], body[4], body[5]
    if encoding > 3:
        return None, f"SYLT text encoding {encoding} is unknown"
    if fmt == 1:
        return None, "SYLT timestamps are in MPEG frames (format 1), which is not converted to time"
    if fmt != 2:
        return None, f"SYLT timestamp format {fmt} is unknown"
    _, pos = _split_terminated(body, 6, encoding)  # the content descriptor
    lines: List[dict] = []
    while pos < len(body):
        raw, pos = _split_terminated(body, pos, encoding)
        if pos + 4 > len(body):
            break
        ms = struct.unpack(">I", body[pos:pos + 4])[0]
        pos += 4
        text = " ".join(_decode(raw, encoding).split())
        if text:
            lines.append({"t_start": round(ms / 1000.0, 3), "text": text})
    lines.sort(key=lambda ln: ln["t_start"])  # sorted() is stable, the tag's order breaks ties
    lang = language.decode("latin-1").strip("\x00 ").lower()
    return {"language": lang if len(lang) == 3 and lang.isalpha() and lang != "xxx" else None,
            "content_type": content_type, "lines": lines}, None


def parse_id3_sylt(data: bytes) -> Optional[SyltResult]:
    """The SYLT lyrics of an ID3v2 tag at the start of `data`, or None when `data` has no
    ID3v2 tag or no SYLT frame. Prefers a content-type-1 (lyrics) frame, then the one
    with the most lines."""
    if len(data) < 10 or data[:3] != b"ID3" or data[3] not in (2, 3, 4):
        return None
    version, flags = data[3], data[5]
    if any(b & 0x80 for b in data[6:10]):
        return None
    size = _synchsafe(data[6:10])
    tag = data[10:10 + size]
    if flags & 0x80 and version < 4:
        tag = _ununsync(tag)
    pos = 0
    if flags & 0x40 and version >= 3 and len(tag) >= 4:  # extended header
        ext = _synchsafe(tag[:4]) if version == 4 else struct.unpack(">I", tag[:4])[0] + 4
        pos = min(ext, len(tag))
    result = SyltResult()
    candidates: List[dict] = []
    wanted = b"SLT" if version == 2 else b"SYLT"
    head = 6 if version == 2 else 10
    while pos + head <= len(tag):
        frame_id = tag[pos:pos + (3 if version == 2 else 4)]
        if frame_id[0] == 0:  # padding
            break
        if version == 2:
            fsize, fflags = int.from_bytes(tag[pos + 3:pos + 6], "big"), 0
        elif version == 3:
            fsize, fflags = struct.unpack(">I", tag[pos + 4:pos + 8])[0], struct.unpack(">H", tag[pos + 8:pos + 10])[0]
        else:
            fsize, fflags = _synchsafe(tag[pos + 4:pos + 8]), struct.unpack(">H", tag[pos + 8:pos + 10])[0]
        body = tag[pos + head:pos + head + fsize]
        pos += head + fsize
        if frame_id != wanted:
            continue
        if version == 3:
            compressed, encrypted, grouped, indicator, unsync = fflags & 0x80, fflags & 0x40, fflags & 0x20, False, False
        elif version == 4:
            compressed, encrypted, grouped = fflags & 0x08, fflags & 0x04, fflags & 0x40
            indicator, unsync = bool(fflags & 0x01), bool(fflags & 0x02)
        else:
            compressed = encrypted = grouped = 0
            indicator = unsync = False
        if compressed or encrypted:
            result.skipped.append("SYLT frame is compressed or encrypted")
            continue
        if grouped:
            body = body[1:]
        if indicator:
            body = body[4:]
        if unsync:
            body = _ununsync(body)
        frame, reason = parse_sylt_frame(body)
        if frame is None:
            if reason:
                result.skipped.append(reason)
            continue
        if frame["lines"]:
            candidates.append(frame)
    if not candidates and not result.skipped:
        return None
    if candidates:
        best = max(candidates, key=lambda f: (f["content_type"] == 1, len(f["lines"])))
        result.lines, result.language = best["lines"], best["language"]
    return result


def read_id3_sylt(path: Path) -> Optional[SyltResult]:
    """`parse_id3_sylt` over the start of the file at `path`. Blocking. Raises `OSError`
    when the file cannot be read."""
    with open(path, "rb") as f:
        head = f.read(10)
        if len(head) < 10 or head[:3] != b"ID3" or any(b & 0x80 for b in head[6:10]):
            return None
        size = _synchsafe(head[6:10])
        if size > MAX_TAG_BYTES:
            return None
        return parse_id3_sylt(head + f.read(size))
