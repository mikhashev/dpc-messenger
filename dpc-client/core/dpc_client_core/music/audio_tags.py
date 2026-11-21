"""Writing and reading embedded tags in FLAC and WAV files, in pure Python (PROVENANCE §3, §4).

Why not ffmpeg: the provenance record is 5-60 KB of JSON, and Windows caps a command line at
32 767 characters (the MP3 export hit the same wall and writes its ID3 tag by hand). These
helpers copy the audio bytes verbatim and only rebuild the metadata around them, so **tags
never change the sound** (PROVENANCE rule 1) by construction, and each writer also proves it:
it hashes the audio region of the source and of the result and refuses to replace the file
unless they are equal.

- **FLAC.** A `VORBIS_COMMENT` block (type 4) carries `NAME=value` comments. The writer keeps
  every other metadata block, keeps the comments already there and adds new ones. The
  audio frames are copied byte for byte, so the STREAMINFO MD5 (the decoded-PCM hash the
  library keys a FLAC on) is the same.
- **WAV.** The full record goes in a sub-chunk of `LIST/INFO` with the four-character key
  `DPCP` (see PROVENANCE §3: ffprobe returns it as the tag `DPCP`; a custom top-level RIFF
  chunk is not read by ffprobe). Every other chunk is copied byte for byte, `data` included,
  so the library's audio-stream hash (an ffmpeg stream copy of the PCM packets) is the same.

**Write once** (PROVENANCE rule 7): a file that already holds the key is never changed;
`AlreadyTagged` is raised and the file is left as it was. The new file is written next to the
old one under a name that is not an audio extension and moved over it only when complete and
verified.

Reading is defensive (rule 6): a block or sub-chunk larger than the caller's cap is skipped,
never loaded, and a malformed file gives `{}`, not an exception.
"""

from __future__ import annotations

import hashlib
import os
import struct
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

_COPY_CHUNK = 4 * 1024 * 1024
FLAC_VORBIS_COMMENT = 4
WAV_INFO_PROVENANCE_KEY = "DPCP"


class AlreadyTagged(Exception):
    """The file already holds this key; the first writer wins (rule 7)."""


class TagError(Exception):
    """The file is not one the writer can rebuild, or the rebuilt audio did not verify."""


# -- FLAC ---------------------------------------------------------------------------------------


def _flac_blocks(f, *, limit: int) -> Tuple[List[Tuple[int, bytes, int]], int]:
    """`([(type, body or b'' when over limit, length)], offset of the first audio frame)`.
    Raises `TagError` when the file is not FLAC."""
    f.seek(0)
    head = f.read(10)
    skip = 0
    if len(head) >= 10 and head[:3] == b"ID3":
        skip = 10 + ((head[6] & 0x7F) << 21 | (head[7] & 0x7F) << 14 | (head[8] & 0x7F) << 7 | (head[9] & 0x7F))
    f.seek(skip)
    if f.read(4) != b"fLaC":
        raise TagError("not a FLAC file")
    blocks: List[Tuple[int, bytes, int]] = []
    while True:
        h = f.read(4)
        if len(h) != 4:
            raise TagError("a FLAC metadata block header is cut off")
        last, kind, length = bool(h[0] & 0x80), h[0] & 0x7F, int.from_bytes(h[1:4], "big")
        if kind == 127:
            raise TagError("invalid FLAC block type")
        if length <= limit:
            body = f.read(length)
            if len(body) != length:
                raise TagError("a FLAC metadata block is cut off")
        else:
            f.seek(length, 1)
            body = b""
        blocks.append((kind, body, length))
        if last:
            return blocks, f.tell()


def _parse_vorbis(body: bytes) -> Optional[Tuple[bytes, List[Tuple[str, str]]]]:
    try:
        vlen = struct.unpack_from("<I", body, 0)[0]
        vendor = body[4:4 + vlen]
        pos = 4 + vlen
        count = struct.unpack_from("<I", body, pos)[0]
        pos += 4
        out: List[Tuple[str, str]] = []
        for _ in range(count):
            n = struct.unpack_from("<I", body, pos)[0]
            pos += 4
            raw = body[pos:pos + n]
            if len(raw) != n:
                return None
            pos += n
            text = raw.decode("utf-8", errors="replace")
            key, sep, value = text.partition("=")
            if sep:
                out.append((key, value))
        return vendor, out
    except (struct.error, ValueError):
        return None


def _build_vorbis(vendor: bytes, comments: List[Tuple[str, str]]) -> bytes:
    parts = [struct.pack("<I", len(vendor)), vendor, struct.pack("<I", len(comments))]
    for key, value in comments:
        raw = f"{key}={value}".encode("utf-8")
        parts.append(struct.pack("<I", len(raw)))
        parts.append(raw)
    return b"".join(parts)


def flac_read_comments(path: Path, max_block_bytes: int = 256 * 1024) -> Dict[str, str]:
    """The Vorbis comments of a FLAC file (the first value of each upper-cased name); {} for a
    file that is not FLAC, has none, or whose comment block is over `max_block_bytes`."""
    try:
        with open(path, "rb") as f:
            blocks, _ = _flac_blocks(f, limit=max_block_bytes)
    except (OSError, TagError):
        return {}
    for kind, body, length in blocks:
        if kind == FLAC_VORBIS_COMMENT:
            parsed = _parse_vorbis(body) if body or length == 0 else None
            if parsed is None:
                return {}
            out: Dict[str, str] = {}
            for key, value in parsed[1]:
                out.setdefault(key.upper(), value)
            return out
    return {}


def _flac_audio_digest(path: Path) -> Tuple[str, str]:
    """`(sha256 of the audio frames, STREAMINFO MD5 hex)`: the audio region starts after the
    last metadata block."""
    with open(path, "rb") as f:
        blocks, start = _flac_blocks(f, limit=64)
        stream = next((b for k, b, _ in blocks if k == 0), b"")
        md5 = stream[18:34].hex() if len(stream) >= 34 else ""
        f.seek(start)
        h = hashlib.sha256()
        for chunk in iter(lambda: f.read(_COPY_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest(), md5


def flac_add_comments(path: Path, comments: Mapping[str, str], *, once_key: str = "DPC_PROVENANCE") -> None:
    """Add Vorbis comments to a FLAC file, keeping everything else byte for byte.

    Raises `AlreadyTagged` when `once_key` is among the names being added and the file already
    has it (write once). Comments whose value is empty are not written. The audio is verified
    equal before the result replaces the file."""
    path = Path(path)
    add = [(k.upper(), v) for k, v in comments.items() if isinstance(v, str) and v != ""]
    if not add:
        return
    with open(path, "rb") as f:
        blocks, start = _flac_blocks(f, limit=16 * 1024 * 1024)
    before = _flac_audio_digest(path)
    vendor, existing, vc_index = b"DPC", [], None
    for i, (kind, body, _length) in enumerate(blocks):
        if kind == FLAC_VORBIS_COMMENT and vc_index is None:
            parsed = _parse_vorbis(body)
            if parsed is None:
                raise TagError("the FLAC's comment block cannot be read; it is left as it is")
            vendor, existing = parsed
            vc_index = i
    names = {k.upper() for k, _ in existing}
    if once_key and once_key.upper() in {k for k, _ in add} and once_key.upper() in names:
        raise AlreadyTagged(f"{path.name} already holds {once_key}")
    # A name already present keeps its value (first writer wins); only new names are added.
    merged = list(existing) + [(k, v) for k, v in add if k not in names]
    body = _build_vorbis(vendor, merged)
    if len(body) >= 1 << 24:
        raise TagError("the comment block would be over 16 MB")
    if vc_index is None:
        vc_index = 1
        blocks = blocks[:1] + [(FLAC_VORBIS_COMMENT, body, len(body))] + blocks[1:]
    else:
        blocks[vc_index] = (FLAC_VORBIS_COMMENT, body, len(body))
    tmp = path.with_name(path.name + ".tagging")
    try:
        with open(path, "rb") as src, open(tmp, "wb") as dst:
            dst.write(b"fLaC")
            for i, (kind, bbody, length) in enumerate(blocks):
                if i != vc_index and not bbody and length:
                    raise TagError("a large metadata block was not loaded; the file is left as it is")
                flag = 0x80 if i == len(blocks) - 1 else 0
                dst.write(bytes([flag | kind]) + length.to_bytes(3, "big") + bbody)
            src.seek(start)
            for chunk in iter(lambda: src.read(_COPY_CHUNK), b""):
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        after = _flac_audio_digest(tmp)
        if after != before:
            raise TagError("the FLAC's audio changed while tagging; the file is left as it is")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


# -- WAV ----------------------------------------------------------------------------------------


def _riff_chunks(f, size: int) -> List[Tuple[bytes, int, int]]:
    """`[(id, body offset, body size)]` of a RIFF/WAVE file. Raises `TagError`."""
    f.seek(0)
    head = f.read(12)
    if len(head) != 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        raise TagError("not a RIFF/WAVE file")
    out: List[Tuple[bytes, int, int]] = []
    pos = 12
    while pos + 8 <= size:
        f.seek(pos)
        h = f.read(8)
        if len(h) != 8:
            break
        cid, csize = struct.unpack("<4sI", h)
        if pos + 8 + csize > size:
            raise TagError("a RIFF chunk runs past the end of the file")
        out.append((cid, pos + 8, csize))
        pos += 8 + csize + (csize & 1)
    return out


def _info_items(body: bytes) -> List[Tuple[bytes, bytes]]:
    items: List[Tuple[bytes, bytes]] = []
    pos = 4  # after 'INFO'
    while pos + 8 <= len(body):
        cid, csize = struct.unpack_from("<4sI", body, pos)
        value = body[pos + 8:pos + 8 + csize]
        items.append((cid, value))
        pos += 8 + csize + (csize & 1)
    return items


def _info_chunk(items: List[Tuple[bytes, bytes]]) -> bytes:
    parts = [b"INFO"]
    for cid, value in items:
        parts.append(cid + struct.pack("<I", len(value)) + value + (b"\0" if len(value) & 1 else b""))
    body = b"".join(parts)
    return b"LIST" + struct.pack("<I", len(body)) + body + (b"\0" if len(body) & 1 else b"")


def wav_read_info(path: Path, max_item_bytes: int = 256 * 1024) -> Dict[str, str]:
    """The `LIST/INFO` items of a WAV file as `{four-character key: text}` (NUL trimmed); an item
    over `max_item_bytes` is skipped. {} for a file that is not a readable WAV."""
    try:
        size = Path(path).stat().st_size
        with open(path, "rb") as f:
            chunks = _riff_chunks(f, size)
            out: Dict[str, str] = {}
            for cid, off, csize in chunks:
                if cid != b"LIST" or csize > 16 * 1024 * 1024:
                    continue
                f.seek(off)
                body = f.read(csize)
                if body[:4] != b"INFO":
                    continue
                pos = 4
                while pos + 8 <= len(body):
                    key, n = struct.unpack_from("<4sI", body, pos)
                    if n <= max_item_bytes:
                        text = body[pos + 8:pos + 8 + n].split(b"\0", 1)[0].decode("utf-8", errors="replace")
                        out.setdefault(key.decode("ascii", errors="replace"), text)
                    pos += 8 + n + (n & 1)
            return out
    except (OSError, TagError, struct.error):
        return {}


def _wav_data_digest(path: Path) -> str:
    """sha256 of the `data` chunk body: the PCM, whatever sits around it."""
    size = path.stat().st_size
    with open(path, "rb") as f:
        for cid, off, csize in _riff_chunks(f, size):
            if cid == b"data":
                f.seek(off)
                h, left = hashlib.sha256(), csize
                while left > 0:
                    chunk = f.read(min(_COPY_CHUNK, left))
                    if not chunk:
                        break
                    h.update(chunk)
                    left -= len(chunk)
                return h.hexdigest()
    raise TagError("the WAV has no data chunk")


def wav_add_info(path: Path, key: str, text: str) -> None:
    """Add one `LIST/INFO` item (`key`: four ASCII characters) to a WAV file, keeping every
    other chunk byte for byte. Raises `AlreadyTagged` when the item is there. The PCM is
    verified equal before the result replaces the file."""
    path = Path(path)
    cid = key.encode("ascii")
    if len(cid) != 4:
        raise TagError("an INFO key is four characters")
    value = text.encode("utf-8") + b"\0"
    size = path.stat().st_size
    before = _wav_data_digest(path)
    tmp = path.with_name(path.name + ".tagging")
    try:
        with open(path, "rb") as src, open(tmp, "wb") as dst:
            chunks = _riff_chunks(src, size)
            info_at = next((i for i, (c, off, n) in enumerate(chunks) if c == b"LIST" and _peek(src, off, 4) == b"INFO"), None)
            dst.write(b"RIFF\0\0\0\0WAVE")
            written_info = False
            for i, (c, off, n) in enumerate(chunks):
                if i == info_at:
                    src.seek(off)
                    items = _info_items(src.read(n))
                    if any(k == cid for k, _ in items):
                        raise AlreadyTagged(f"{path.name} already holds INFO {key}")
                    dst.write(_info_chunk(items + [(cid, value)]))
                    written_info = True
                    continue
                if c == b"data" and info_at is None and not written_info:
                    dst.write(_info_chunk([(cid, value)]))
                    written_info = True
                dst.write(c + struct.pack("<I", n))
                src.seek(off)
                left = n
                while left > 0:
                    chunk = src.read(min(_COPY_CHUNK, left))
                    if not chunk:
                        raise TagError("the WAV is shorter than its chunks say")
                    dst.write(chunk)
                    left -= len(chunk)
                if n & 1:
                    dst.write(b"\0")
            if not written_info:
                dst.write(_info_chunk([(cid, value)]))
            total = dst.tell()
            dst.seek(4)
            dst.write(struct.pack("<I", total - 8))
            dst.flush()
            os.fsync(dst.fileno())
        if _wav_data_digest(tmp) != before:
            raise TagError("the WAV's PCM changed while tagging; the file is left as it is")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _peek(f, off: int, n: int) -> bytes:
    f.seek(off)
    return f.read(n)
