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
- **WAV.** The full record goes in an ID3v2.4 tag in a top-level RIFF `id3 ` chunk, as the text
  frame `TXXX:DPC_PROVENANCE`, the same frame the MP3 export carries (`id3.py`; Mike 2026-11-26:
  one key, in each format's own standard text-tag mechanism). ffmpeg's WAV demuxer reads the chunk
  (`case MKTAG('I','D','3',' '): case MKTAG('i','d','3',' ')`, so both case forms), and so do
  mutagen, foobar2000 and Mp3tag; ffprobe returns it as the tag `DPC_PROVENANCE`. The writer uses
  the lowercase form and keeps the case of a chunk already there; the reader takes both. A WAV
  written before that day holds the record in a `LIST/INFO` item `DPCP` (ffprobe: `DPCP`); the
  reader falls back to it, and writing the new form drops the old item. Every other chunk is
  copied byte for byte, `data` included, so the library's audio-stream hash (an ffmpeg stream copy
  of the PCM packets) is the same. The `LIST/INFO` text items (title and so on) stay as they are.

**Write once** (PROVENANCE rule 7): a file that already holds the key is never changed;
`AlreadyTagged` is raised and the file is left as it was. In a WAV every `id3 ` / `ID3 ` chunk is
checked, not only the first; a WAV with more than one *readable* ID3 chunk is not written at all and
its record reads as `ambiguous` with the reason (`wav_provenance_state`), never as the first chunk's
(Zcode #191). Readable means what the record collector can look into: a chunk within the size caps
(`WAV_RECORD_READ_MAX`, the same for the writer and every reader) that starts with a v2.3 or v2.4 ID3
header (no tag-wide unsynchronisation). A junk, cut-short or oversize `id3 ` / `ID3 ` chunk is not
one (follow-up 19, Zcode #199 finding 2): it does not count toward `ambiguous`, so a real record next
to it reads as present and a write into the one readable chunk (or a new chunk when there is none)
goes ahead, and the junk chunk stays byte for byte as it was, at any size (a chunk over 16 MiB is
streamed through, never loaded). A write is still refused when any chunk up to 16 MiB holds the
record; a bigger one is as unreadable to the writer as to the reader. The new file is written next to the
old one under a name that is not an audio extension and moved over it only when complete and
verified.

Reading is defensive (rule 6): a block or sub-chunk larger than the caller's cap is skipped,
never loaded, and a malformed file gives `{}`, not an exception.
"""

from __future__ import annotations

import hashlib
import logging
import os
import struct
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Tuple

from .id3 import ID3_PADDING, build_id3v24, id3v24_frames, parse_id3_txxx, txxx_frame

logger = logging.getLogger(__name__)

_COPY_CHUNK = 4 * 1024 * 1024
FLAC_VORBIS_COMMENT = 4
PROVENANCE_TAG = "DPC_PROVENANCE"       # the one key, in every format's own text-tag mechanism
WAV_INFO_PROVENANCE_KEY = "DPCP"        # the old WAV form: a LIST/INFO item; still read, never written
WAV_ID3_CHUNK_IDS = (b"id3 ", b"ID3 ")  # the RIFF chunk that holds an ID3v2 tag; ffmpeg reads both cases
WAV_ID3_WRITE_ID = b"id3 "
_WAV_CHUNK_READ_MAX = 16 * 1024 * 1024
_ID3_SLACK = 64 * 1024                  # a tag may be this much bigger than the record it carries
# The one record cap of a WAV's ID3 chunk, for the writer and every reader alike (library scan, migration,
# re-tag, export): a chunk the writer judged junk at one cap and a reader found readable at another made a
# second chunk, and the record just written read as `ambiguous` (Zcode #204).
WAV_RECORD_READ_MAX = 2 * 1024 * 1024


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


def _wav_rewrite(path: Path, plan: Callable[[object, List[Tuple[bytes, int, int]]],
                                              Tuple[Dict[int, Optional[bytes]], Optional[bytes]]]) -> None:
    """Rebuild a WAV next to itself with some chunks replaced or dropped and one chunk added before
    `data`, copying every other chunk byte for byte, then verify the PCM and replace the file.

    `plan(src, chunks)` returns `({chunk index: whole replacement chunk bytes, or None to drop it},
    whole chunk bytes to insert before `data` or None)`. It may raise `AlreadyTagged` or `TagError`;
    the file is then left as it was. A chunk is written with its pad byte when its size is odd and
    the RIFF size is rewritten at the end."""
    path = Path(path)
    size = path.stat().st_size
    before = _wav_data_digest(path)
    tmp = path.with_name(path.name + ".tagging")
    try:
        with open(path, "rb") as src, open(tmp, "wb") as dst:
            chunks = _riff_chunks(src, size)
            replace, insert = plan(src, chunks)
            dst.write(b"RIFF\0\0\0\0WAVE")
            inserted = insert is None
            for i, (c, off, n) in enumerate(chunks):
                if i in replace:
                    if replace[i] is not None:
                        dst.write(replace[i])
                    continue
                if c == b"data" and not inserted:
                    dst.write(insert)
                    inserted = True
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
            if not inserted:
                dst.write(insert)
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


def _riff_chunk(cid: bytes, body: bytes) -> bytes:
    """A whole RIFF chunk: id, size (without the pad), body, and the pad byte when the size is odd."""
    return cid + struct.pack("<I", len(body)) + body + (b"\0" if len(body) & 1 else b"")


def wav_add_info(path: Path, key: str, text: str) -> None:
    """Add one `LIST/INFO` item (`key`: four ASCII characters) to a WAV file, keeping every
    other chunk byte for byte. Raises `AlreadyTagged` when the item is there. The PCM is
    verified equal before the result replaces the file. (The provenance record is not written
    this way any more: see `wav_add_provenance`.)"""
    cid = key.encode("ascii")
    if len(cid) != 4:
        raise TagError("an INFO key is four characters")
    value = text.encode("utf-8") + b"\0"

    def plan(src, chunks):
        info_at = next((i for i, (c, off, n) in enumerate(chunks) if c == b"LIST" and _peek(src, off, 4) == b"INFO"), None)
        if info_at is None:
            return {}, _info_chunk([(cid, value)])
        src.seek(chunks[info_at][1])
        items = _info_items(src.read(chunks[info_at][2]))
        if any(k == cid for k, _ in items):
            raise AlreadyTagged(f"{Path(path).name} already holds INFO {key}")
        return {info_at: _info_chunk(items + [(cid, value)])}, None

    _wav_rewrite(path, plan)


# -- WAV provenance: ID3v2.4 `TXXX:DPC_PROVENANCE` in an `id3 ` chunk, the old INFO `DPCP` as a fallback ---


def _wav_id3_chunks(chunks: List[Tuple[bytes, int, int]]) -> List[int]:
    return [i for i, (c, _o, _n) in enumerate(chunks) if c in WAV_ID3_CHUNK_IDS]


def wav_read_id3(path: Path, max_item_bytes: int = WAV_RECORD_READ_MAX) -> Dict[str, str]:
    """The `TXXX` frames (description -> value) of a WAV's `id3 ` (or `ID3 `) chunk; a value over
    `max_item_bytes` is skipped, and so is a tag more than 64 KB bigger than that. {} for a file that
    is not a readable WAV or has no such chunk."""
    try:
        size = Path(path).stat().st_size
        with open(path, "rb") as f:
            chunks = _riff_chunks(f, size)
            for i in _wav_id3_chunks(chunks):
                _cid, off, csize = chunks[i]
                if csize < 10 or csize > max_item_bytes + _ID3_SLACK or csize > _WAV_CHUNK_READ_MAX:
                    continue
                f.seek(off)
                tag = f.read(csize)
                found = {k: v for k, v in parse_id3_txxx(tag[:10], tag[10:]).items()
                         if len(v.encode("utf-8")) <= max_item_bytes}
                if found:
                    return found
        return {}
    except (OSError, TagError, struct.error):
        return {}


def _id3_chunk_readable(csize: int, head: bytes, max_item_bytes: int) -> bool:
    """Whether an `id3 ` / `ID3 ` chunk is one the record collector can look into: within the size
    caps, and a v2.3 or v2.4 ID3 header without tag-wide unsynchronisation (what `parse_id3_txxx`
    accepts). Anything else is junk to it and counts toward nothing."""
    return (10 <= csize <= max_item_bytes + _ID3_SLACK and csize <= _WAV_CHUNK_READ_MAX
            and len(head) >= 10 and head[:3] == b"ID3" and head[3] in (3, 4) and not head[5] & 0x80)


def _wav_id3_records(path: Path, max_item_bytes: int) -> Tuple[int, List[str]]:
    """`(readable ID3 chunks, records)`: every `id3 ` / `ID3 ` chunk of the WAV is looked at, not only
    the first; only a readable one (`_id3_chunk_readable`) counts, and each non-empty
    `TXXX:DPC_PROVENANCE` within the caps is one record."""
    size = Path(path).stat().st_size
    records: List[str] = []
    readable = 0
    with open(path, "rb") as f:
        chunks = _riff_chunks(f, size)
        for i in _wav_id3_chunks(chunks):
            _cid, off, csize = chunks[i]
            if csize < 10 or csize > max_item_bytes + _ID3_SLACK or csize > _WAV_CHUNK_READ_MAX:
                continue
            f.seek(off)
            tag = f.read(csize)
            if not _id3_chunk_readable(csize, tag[:10], max_item_bytes):
                continue
            readable += 1
            value = parse_id3_txxx(tag[:10], tag[10:]).get(PROVENANCE_TAG)
            if value and value.strip() and len(value.encode("utf-8")) <= max_item_bytes:
                records.append(value)
    return readable, records


def wav_provenance_state(path: Path, max_item_bytes: int = WAV_RECORD_READ_MAX) -> Dict[str, Optional[str]]:
    """`{status, text, where, reason}` for a WAV's provenance record. `status` is `present` (`where`
    is `id3`, or `info` for the old `LIST/INFO` item `DPCP`), `absent`, or `ambiguous`: the WAV has
    more than one readable `id3 ` / `ID3 ` chunk, so which one is the record would be a guess (Zcode
    #191: the reader used to take the first chunk silently, and a second chunk got past write-once).
    A junk or oversize chunk is not readable and is not counted (Zcode #199 finding 2). An ambiguous
    file gives no text: no chunk is chosen. Defensive (rule 6): never raises."""
    try:
        chunks, records = _wav_id3_records(path, max_item_bytes)
    except (OSError, TagError, struct.error):
        return {"status": "absent", "text": None, "where": None, "reason": "not a readable WAV"}
    if chunks > 1:
        return {"status": "ambiguous", "text": None, "where": None,
                "reason": f"the WAV has {chunks} ID3 chunks that can be read, {len(records)} of them holding "
                          f"{PROVENANCE_TAG}; no chunk is chosen"}
    if records:
        return {"status": "present", "text": records[0], "where": "id3", "reason": None}
    legacy = wav_read_info(path, max_item_bytes=max_item_bytes).get(WAV_INFO_PROVENANCE_KEY)
    if legacy and legacy.strip():
        return {"status": "present", "text": legacy, "where": "info", "reason": None}
    return {"status": "absent", "text": None, "where": None, "reason": None}


def wav_read_provenance(path: Path, max_item_bytes: int = WAV_RECORD_READ_MAX) -> Optional[str]:
    """The provenance record text of a WAV: the `TXXX:DPC_PROVENANCE` frame of its `id3 ` chunk, else
    the old `LIST/INFO` item `DPCP`, else None. An ambiguous file (`wav_provenance_state`) gives None
    and a log line with the reason. Defensive (rule 6): never raises."""
    state = wav_provenance_state(path, max_item_bytes)
    if state["status"] == "ambiguous":
        logger.info("music: ignoring the provenance record of %s: %s", Path(path).name, state["reason"])
    return state["text"]


def _info_without_legacy(src, chunks: List[Tuple[bytes, int, int]]) -> Dict[int, Optional[bytes]]:
    """Replacements that remove the `DPCP` item from the `LIST/INFO` chunk (the chunk goes when it
    held nothing else)."""
    out: Dict[int, Optional[bytes]] = {}
    legacy = WAV_INFO_PROVENANCE_KEY.encode("ascii")
    for i, (c, off, n) in enumerate(chunks):
        if c != b"LIST" or _peek(src, off, 4) != b"INFO":
            continue
        src.seek(off)
        items = _info_items(src.read(n))
        kept = [(k, v) for k, v in items if k != legacy]
        if len(kept) != len(items):
            out[i] = _info_chunk(kept) if kept else None
    return out


def wav_add_provenance(path: Path, text: str) -> None:
    """Write the provenance record into a WAV as `TXXX:DPC_PROVENANCE` in an ID3v2.4 tag in a RIFF
    `id3 ` chunk, before `data` (an `id3 ` / `ID3 ` chunk already there keeps its place, its case and
    its other frames). The old `LIST/INFO` item `DPCP` is dropped; the other INFO items stay. Every
    other chunk is copied byte for byte and the PCM is verified equal before the file is replaced
    (atomic, temp then `os.replace`). Raises `AlreadyTagged` when any `id3 ` / `ID3 ` chunk already
    holds the record (rule 7; every chunk is checked, not only the first), `TagError` for a WAV it
    cannot rebuild safely: a readable `id3 ` chunk that is not a plain ID3v2.4 tag, or more than one
    readable ID3 chunk (writing into one would leave a file whose record is ambiguous, Zcode #191).
    Readable is judged at `WAV_RECORD_READ_MAX`, the readers' cap. A junk or oversize chunk the reader
    skips is neither counted nor touched: it stays byte for byte (one over `_WAV_CHUNK_READ_MAX` is not
    even loaded, the rewrite streams it), and with no readable chunk the record goes in a new one
    (Zcode #199 finding 2, #204). The file is left as it was in every refusal."""
    path = Path(path)
    new_frame = txxx_frame(PROVENANCE_TAG, text)

    def plan(src, chunks):
        replace = _info_without_legacy(src, chunks)
        at = _wav_id3_chunks(chunks)
        if not at:
            return replace, _riff_chunk(WAV_ID3_WRITE_ID, build_id3v24([new_frame], ID3_PADDING))
        tags: Dict[int, bytes] = {}
        for i in at:
            _c, off, n = chunks[i]
            if n > _WAV_CHUNK_READ_MAX:
                continue   # never loaded, as the reader skips it; the rewrite streams it through untouched
            src.seek(off)
            tags[i] = src.read(n)
            if parse_id3_txxx(tags[i][:10], tags[i][10:]).get(PROVENANCE_TAG):
                raise AlreadyTagged(f"{path.name} already holds {PROVENANCE_TAG}")
        readable = [i for i in tags if _id3_chunk_readable(chunks[i][2], tags[i][:10], WAV_RECORD_READ_MAX)]
        if not readable:
            # Only chunks the reader skips: they stay as they are and the record gets its own chunk.
            return replace, _riff_chunk(WAV_ID3_WRITE_ID, build_id3v24([new_frame], ID3_PADDING))
        if len(readable) > 1:
            raise TagError(f"the WAV has {len(readable)} ID3 chunks that can be read; a record in one of them "
                           "would be ambiguous, so the file is left as it is")
        target = readable[0]
        cid = chunks[target][0]
        frames = id3v24_frames(tags[target])
        if frames is None:
            raise TagError("the WAV's ID3 chunk is not a plain ID3v2.4 tag; it is left as it is")
        replace[target] = _riff_chunk(cid, build_id3v24(frames + [new_frame], ID3_PADDING))
        return replace, None

    _wav_rewrite(path, plan)


def wav_migrate_provenance(path: Path, max_item_bytes: int = WAV_RECORD_READ_MAX) -> bool:
    """Move a record held in the old `LIST/INFO` item `DPCP` into the `id3 ` chunk, verbatim, and drop
    the old item. True when the file changed; False when there was no old item. When the `id3 ` chunk
    already holds a record the old item is only dropped (the new form wins). Never rewrites a record.
    Raises `TagError` for a WAV whose ID3 record is ambiguous (more than one ID3 chunk)."""
    path = Path(path)
    legacy = wav_read_info(path, max_item_bytes=max_item_bytes).get(WAV_INFO_PROVENANCE_KEY)
    if legacy is None:
        return False
    state = wav_provenance_state(path, max_item_bytes)
    if state["status"] == "ambiguous":
        raise TagError(state["reason"] or "the WAV's provenance record is ambiguous")
    if state["where"] == "id3":
        _wav_rewrite(path, lambda src, chunks: (_info_without_legacy(src, chunks), None))
        return True
    wav_add_provenance(path, legacy)
    return True


def _peek(f, off: int, n: int) -> bytes:
    f.seek(off)
    return f.read(n)
