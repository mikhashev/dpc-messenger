"""ID3 SYLT (synchronised lyrics) is read directly by `music/id3_sylt.py` because ffprobe
does not surface it; the lyrics chain's tags step prefers it over USLT. The MP3 fixtures
are built here, byte by byte. No network, no ffmpeg."""

import json
import struct
from types import SimpleNamespace

import pytest

from dpc_client_core.music.id3_sylt import parse_id3_sylt
from dpc_client_core.music.identity import MediaTools
from dpc_client_core.music.library import MusicLibraryError
from dpc_client_core.music.lyrics import MusicLyrics

TRACK_ID = "trk_0123456789abcdef"
MPEG = b"\xff\xfb" * 300


def _sylt(encoding, entries, *, fmt=2, content_type=1, lang=b"eng", descriptor=b""):
    term = b"\x00\x00" if encoding in (1, 2) else b"\x00"
    out = bytes([encoding]) + lang + bytes([fmt, content_type]) + descriptor + term
    for text, ms in entries:
        out += text + term + struct.pack(">I", ms)
    return out


def _synchsafe(n):
    return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])


def _tag(frames, version=4):
    if version == 2:
        body = b"".join(fid + len(d).to_bytes(3, "big") + d for fid, d in frames)
        return b"ID3\x02\x00\x00" + _synchsafe(len(body)) + body
    enc = _synchsafe if version == 4 else (lambda n: struct.pack(">I", n))
    body = b"".join(fid + enc(len(d)) + b"\x00\x00" + d for fid, d in frames)
    return b"ID3" + bytes([version, 0, 0]) + _synchsafe(len(body)) + body


@pytest.mark.parametrize("encoding,text,codec", [
    (0, "café".encode("latin-1"), "latin-1"),
    (1, b"\xff\xfe" + "Пр".encode("utf-16-le"), "utf-16"),
    (2, "Пр".encode("utf-16-be"), "utf-16-be"),
    (3, "Пр".encode("utf-8"), "utf-8"),
])
@pytest.mark.parametrize("version", [3, 4])
def test_text_encodings_0_to_3_decode_and_ms_become_sorted_seconds(encoding, text, codec, version):
    res = parse_id3_sylt(_tag([(b"SYLT", _sylt(encoding, [(text, 2500), (text, 500)]))], version))
    expected = text.decode(codec)
    assert res.lines == [{"t_start": 0.5, "text": expected}, {"t_start": 2.5, "text": expected}]
    assert res.language == "eng"


def test_a_content_descriptor_is_skipped_in_utf16():
    body = _sylt(1, [(b"\xff\xfe" + "a".encode("utf-16-le"), 1000)],
                 descriptor=b"\xff\xfe" + "d".encode("utf-16-le"))
    assert parse_id3_sylt(_tag([(b"SYLT", body)])).lines == [{"t_start": 1.0, "text": "a"}]


def test_id3v22_slt_frame_is_read():
    res = parse_id3_sylt(_tag([(b"SLT", _sylt(0, [(b"one", 1000)]))], version=2))
    assert res.lines == [{"t_start": 1.0, "text": "one"}]


def test_mpeg_frame_timestamps_are_not_converted_and_are_reported():
    res = parse_id3_sylt(_tag([(b"SYLT", _sylt(3, [(b"one", 100)], fmt=1))]))
    assert res.lines == [] and "MPEG frames" in res.skipped[0]


def test_the_lyrics_content_type_beats_another_one():
    other = _sylt(3, [(b"a", 1000), (b"b", 2000)], content_type=2)
    lyrics = _sylt(3, [(b"x", 1000)], content_type=1)
    assert parse_id3_sylt(_tag([(b"SYLT", other), (b"SYLT", lyrics)])).lines == [{"t_start": 1.0, "text": "x"}]


def test_no_id3_or_no_sylt_gives_none():
    assert parse_id3_sylt(_tag([(b"USLT", b"\x03eng\x00text")])) is None
    assert parse_id3_sylt(MPEG) is None


class _Library:
    def __init__(self, path):
        self.path = path

    def get_entry(self, track_id):
        if track_id != TRACK_ID:
            raise MusicLibraryError("unknown_track", "no")
        return {"id": TRACK_ID, "title": "Song", "artist": "Band", "album": None, "duration_s": 215,
                "source_hash": "sha256:aa", "source": {"kind": "local", "name": self.path.name, "uri": self.path.as_uri()}}

    def get_track_path(self, track_id):
        return self.path


def _service(tmp_path, audio, *, tags=None, ffprobe=True):
    def runner(cmd, *, timeout):
        out = {"format": {"duration": "215.4", "tags": dict(tags or {})}, "streams": [{"codec_type": "audio"}]}
        return SimpleNamespace(returncode=0, stdout=json.dumps(out).encode(), stderr=b"")

    tools = MediaTools(ffmpeg=None, ffprobe="ffprobe" if ffprobe else None, runner=runner)
    return MusicLyrics(tmp_path / "dpc", _Library(audio), lrclib_enabled=lambda: False, tools=tools)


@pytest.mark.asyncio
async def test_a_sylt_only_file_shows_timed_lyrics_from_tags_even_without_ffprobe(tmp_path):
    audio = tmp_path / "Band - Song.mp3"
    audio.write_bytes(_tag([(b"SYLT", _sylt(3, [(b"first", 1000), (b"second", 4000)]))]) + MPEG)
    answer = await _service(tmp_path, audio, ffprobe=False).get(TRACK_ID)
    assert answer["lyrics"]["source"] == "tags" and "synced" not in answer["lyrics"]
    assert answer["lyrics"]["lines"] == [{"t_start": 1.0, "text": "first"}, {"t_start": 4.0, "text": "second"}]
    assert answer["chain"][1]["state"] == "found" and answer["chain"][1]["detail"].startswith("tag SYLT")


@pytest.mark.asyncio
async def test_sylt_beats_the_uslt_text_ffprobe_returns(tmp_path):
    audio = tmp_path / "Band - Song.mp3"
    audio.write_bytes(_tag([(b"SYLT", _sylt(3, [(b"from sylt", 1000)]))]) + MPEG)
    answer = await _service(tmp_path, audio, tags={"lyrics-eng": "[00:02.00] from uslt"}).get(TRACK_ID)
    assert answer["lyrics"]["lines"] == [{"t_start": 1.0, "text": "from sylt"}]


@pytest.mark.asyncio
async def test_an_mpeg_frames_sylt_falls_through_to_uslt_and_is_named_when_nothing_else_is_found(tmp_path):
    audio = tmp_path / "Band - Song.mp3"
    audio.write_bytes(_tag([(b"SYLT", _sylt(3, [(b"x", 100)], fmt=1))]) + MPEG)
    answer = await _service(tmp_path, audio, tags={"lyrics-eng": "[00:02.00] from uslt"}).get(TRACK_ID)
    assert answer["lyrics"]["lines"] == [{"t_start": 2.0, "text": "from uslt"}]
    answer = await _service(tmp_path / "b", audio).get(TRACK_ID)
    assert answer["lyrics"] is None and "MPEG frames" in answer["chain"][1]["detail"]
