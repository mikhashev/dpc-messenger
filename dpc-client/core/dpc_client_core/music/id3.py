"""ID3v2.4 tags, written and read by hand, shared by every container that carries one.

One tag format holds DPC's text records in three containers: an MP3 file (a leading tag), a WAV
file (a RIFF `id3 ` chunk, `audio_tags.py`) and, for the export marker, nothing else. The
provenance record is always the text frame `TXXX:DPC_PROVENANCE` (PROVENANCE §3). Pure Python so a
60 KB record never meets the 32 767 character command-line limit of Windows.

Only text frames are built (`TIT2` and friends, `TXXX`, `COMM`, `USLT`), always UTF-8 (encoding
byte 3), never unsynchronised, no extended header. Reading takes ID3v2.3 and 2.4, skips a
compressed, encrypted or unsynchronised frame, and never raises.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ID3_READ_MAX_BYTES = 1024 * 1024
ID3_PADDING = 1024


def _syncsafe(n: int) -> bytes:
    return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])


def _unsyncsafe(b: bytes) -> Optional[int]:
    if len(b) != 4 or any(x & 0x80 for x in b):
        return None
    return (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]


def _clean(text: Any) -> str:
    return str(text).replace("\x00", "")


def _frame(fid: str, payload: bytes) -> bytes:
    return fid.encode("ascii") + _syncsafe(len(payload)) + b"\x00\x00" + payload


def text_frame(fid: str, text: str) -> bytes:
    return _frame(fid, b"\x03" + _clean(text).encode("utf-8"))


def txxx_frame(desc: str, value: str) -> bytes:
    return _frame("TXXX", b"\x03" + _clean(desc).encode("utf-8") + b"\x00" + _clean(value).encode("utf-8"))


def _lang3(language: Optional[str]) -> bytes:
    lang = (language or "").strip().lower()
    return lang.encode("ascii") if len(lang) == 3 and lang.isascii() and lang.isalpha() else b"xxx"


def comm_frame(text: str, language: Optional[str] = None) -> bytes:
    return _frame("COMM", b"\x03" + _lang3(language) + b"\x00" + _clean(text).encode("utf-8"))


def uslt_frame(text: str, language: Optional[str] = None) -> bytes:
    return _frame("USLT", b"\x03" + _lang3(language) + b"\x00" + _clean(text).encode("utf-8"))


def build_id3v24(frames: Sequence[bytes], padding: int = ID3_PADDING) -> bytes:
    """An ID3v2.4 tag: no unsynchronisation, no extended header, `padding` zero bytes so an
    editor can change it in place."""
    body = b"".join(frames) + b"\x00" * padding
    return b"ID3\x04\x00\x00" + _syncsafe(len(body)) + body


def _split_encoded(enc: int, data: bytes) -> Tuple[str, str]:
    if enc in (1, 2):
        cut = next((i for i in range(0, len(data) - 1, 2) if data[i:i + 2] == b"\x00\x00"), len(data))
        codec = "utf-16" if enc == 1 else "utf-16-be"
        head, tail = data[:cut], data[cut + 2:]
        return (head.decode(codec, errors="replace").strip("\x00"),
                tail.decode(codec, errors="replace").strip("\x00"))
    cut = data.find(b"\x00")
    head, tail = (data, b"") if cut < 0 else (data[:cut], data[cut + 1:])
    codec = "utf-8" if enc == 3 else "latin-1"
    return head.decode(codec, errors="replace"), tail.decode(codec, errors="replace").strip("\x00")


def parse_id3_txxx(head: bytes, data: bytes) -> Dict[str, str]:
    """The `TXXX` frames (description -> value) of an ID3v2.3 or 2.4 tag given as its 10-byte header
    and its body (all of it, or a leading part: a frame cut short by the end of `data` is dropped,
    never half-read). {} when the tag is not one this can read safely."""
    if len(head) < 10 or head[:3] != b"ID3" or head[3] not in (3, 4):
        return {}
    flags = head[5]
    if flags & 0x80:  # tag-wide unsynchronisation: not ours, not parsed
        return {}
    major = head[3]
    pos = 0
    if flags & 0x40 and len(data) >= 4:  # an extended header
        if major == 4:
            ext = _unsyncsafe(data[:4])
            pos = ext if ext is not None else len(data)
        else:
            pos = int.from_bytes(data[:4], "big") + 4
    out: Dict[str, str] = {}
    while pos + 10 <= len(data):
        fid = data[pos:pos + 4]
        if fid[0] == 0 or not all(48 <= c <= 57 or 65 <= c <= 90 for c in fid):
            break
        raw_size = data[pos + 4:pos + 8]
        fsize = _unsyncsafe(raw_size) if major == 4 else int.from_bytes(raw_size, "big")
        if fsize is None or pos + 10 + fsize > len(data):
            break
        fflags = data[pos + 9]
        payload = data[pos + 10:pos + 10 + fsize]
        pos += 10 + fsize
        if fid != b"TXXX" or len(payload) < 2:
            continue
        if (major == 4 and fflags & 0x0F) or (major == 3 and fflags & 0xC0):
            continue  # compressed, encrypted or unsynchronised frame
        desc, value = _split_encoded(payload[0], payload[1:])
        out[desc] = value
    return out


def read_id3_txxx(path: Path, max_bytes: int = ID3_READ_MAX_BYTES) -> Dict[str, str]:
    """The `TXXX` frames (description -> value) of a leading ID3v2.3 or 2.4 tag; {} when
    there is none or it cannot be read safely. Reads at most `max_bytes` of the tag."""
    try:
        with open(path, "rb") as f:
            head = f.read(10)
            if len(head) < 10 or head[:3] != b"ID3" or head[3] not in (3, 4):
                return {}
            size = _unsyncsafe(head[6:10])
            if size is None:
                return {}
            data = f.read(min(size, max_bytes))
    except OSError:
        return {}
    return parse_id3_txxx(head, data)


def id3v24_frames(tag: bytes) -> Optional[List[bytes]]:
    """The raw frames (header and payload, byte for byte) of a complete ID3v2.4 tag, padding
    dropped, so a writer can add one and keep the rest. None when the tag is not a plain 2.4 tag
    (another version, unsynchronised, extended header, footer, or a frame that does not fit)."""
    if len(tag) < 10 or tag[:3] != b"ID3" or tag[3] != 4 or tag[5] & 0xBF:
        return None
    size = _unsyncsafe(tag[6:10])
    if size is None or 10 + size > len(tag):
        return None
    body = tag[10:10 + size]
    frames: List[bytes] = []
    pos = 0
    while pos + 10 <= len(body):
        fid = body[pos:pos + 4]
        if fid[0] == 0:
            if any(body[pos:]):
                return None  # something that is not padding after the last frame
            break
        if not all(48 <= c <= 57 or 65 <= c <= 90 for c in fid):
            return None
        fsize = _unsyncsafe(body[pos + 4:pos + 8])
        if fsize is None or pos + 10 + fsize > len(body):
            return None
        frames.append(body[pos:pos + 10 + fsize])
        pos += 10 + fsize
    return frames
