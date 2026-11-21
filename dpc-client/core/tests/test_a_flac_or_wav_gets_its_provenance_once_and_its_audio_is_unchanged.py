"""`music/audio_tags.py` (PROVENANCE rules 1, 6, 7): a FLAC Vorbis comment or a WAV `LIST/INFO`
item is added without touching the audio (proved by the writer and again here), written once,
read back with a size cap, and readable by ffprobe when it is installed."""

import hashlib
import json
import shutil
import struct
import subprocess
import wave
from pathlib import Path

import pytest

from dpc_client_core.music import audio_tags as at
from dpc_client_core.music.identity import identify, MediaTools, read_flac_streaminfo
from dpc_client_core.music.runner import pcm_md5_and_sha256, read_wav_info

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
BIG = json.dumps({"schema": "mp.provenance/1", "engine": "x", "audio_codes": ",".join(["12345"] * 9000),
                  "lyrics": "Тысячи умов\n" * 50}, ensure_ascii=False)       # about 60 KB


def make_wav(path: Path, seconds=0.5, rate=48000, info: dict = None) -> Path:
    frames = b"".join(struct.pack("<hh", int(8000 * ((i % 100) - 50) / 50), int(6000 * ((i % 70) - 35) / 35))
                      for i in range(int(seconds * rate)))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    if info:
        # add a LIST/INFO chunk with the given items before `data`, like the trial WAVs have
        raw = path.read_bytes()
        items = b"INFO" + b"".join(k.encode() + struct.pack("<I", len(v) + 1) + v.encode() + b"\0" +
                                   (b"\0" if (len(v) + 1) & 1 else b"") for k, v in info.items())
        chunk = b"LIST" + struct.pack("<I", len(items)) + items
        at_data = raw.index(b"data")
        raw = raw[:at_data] + chunk + raw[at_data:]
        raw = raw[:4] + struct.pack("<I", len(raw) - 8) + raw[8:]
        path.write_bytes(raw)
    return path


def make_flac(path: Path, wav: Path) -> Path:
    subprocess.run([FFMPEG, "-nostdin", "-v", "error", "-y", "-i", str(wav), "-map_metadata", "-1", "-c:a", "flac",
                    "-metadata", "TITLE=Song", str(path)], check=True)
    return path


def decoded_md5(path: Path) -> str:
    out = subprocess.run([FFMPEG, "-nostdin", "-v", "error", "-i", str(path), "-map", "0:a:0", "-f", "md5", "-"],
                         capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout.strip()
    return out


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg is not installed")
def test_a_flac_keeps_its_decoded_audio_and_its_other_tags_and_reads_the_record_back(tmp_path):
    flac = make_flac(tmp_path / "a.flac", make_wav(tmp_path / "a.wav"))
    before_md5, before_info = decoded_md5(flac), read_flac_streaminfo(flac)
    at.flac_add_comments(flac, {"DPC_PROVENANCE": BIG, "LYRICS": "Тысячи умов", "EMPTY": ""})
    assert decoded_md5(flac) == before_md5                              # rule 1, by an independent decoder
    assert read_flac_streaminfo(flac).md5 == before_info.md5
    tags = at.flac_read_comments(flac)
    assert tags["DPC_PROVENANCE"] == BIG and tags["TITLE"] == "Song" and tags["LYRICS"] == "Тысячи умов"
    assert "EMPTY" not in tags
    assert not list(tmp_path.glob("*.tagging"))


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg is not installed")
def test_a_flac_record_is_written_once_and_a_second_write_changes_nothing(tmp_path):
    flac = make_flac(tmp_path / "a.flac", make_wav(tmp_path / "a.wav"))
    at.flac_add_comments(flac, {"DPC_PROVENANCE": '{"first":1}'})
    first = flac.read_bytes()
    with pytest.raises(at.AlreadyTagged):
        at.flac_add_comments(flac, {"DPC_PROVENANCE": '{"second":2}', "LYRICS": "x"})
    assert flac.read_bytes() == first and at.flac_read_comments(flac)["DPC_PROVENANCE"] == '{"first":1}'
    # a name that is not the once-key keeps its first value too
    at.flac_add_comments(flac, {"LYRICS": "one"})
    at.flac_add_comments(flac, {"LYRICS": "two"})
    assert at.flac_read_comments(flac)["LYRICS"] == "one"


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg is not installed")
def test_a_flac_comment_block_over_the_readers_cap_is_skipped_not_loaded(tmp_path):
    flac = make_flac(tmp_path / "a.flac", make_wav(tmp_path / "a.wav"))
    at.flac_add_comments(flac, {"DPC_PROVENANCE": BIG})
    assert at.flac_read_comments(flac, max_block_bytes=1000) == {}
    assert at.flac_read_comments(tmp_path / "missing.flac") == {}
    (tmp_path / "junk.flac").write_bytes(b"fLaC" + b"\x00" * 3)
    assert at.flac_read_comments(tmp_path / "junk.flac") == {}


def test_a_header_only_flac_the_tests_use_takes_the_comment_as_its_last_block(tmp_path):
    from tests._acestep_helpers import fake_flac
    wav = make_wav(tmp_path / "a.wav")
    flac = tmp_path / "a.flac"
    fake_flac(wav, flac, {})
    before = read_flac_streaminfo(flac)
    at.flac_add_comments(flac, {"DPC_PROVENANCE": "{}"})
    assert read_flac_streaminfo(flac).md5 == before.md5 and at.flac_read_comments(flac)["DPC_PROVENANCE"] == "{}"


def test_a_wav_keeps_every_chunk_and_its_pcm_and_gets_one_info_item(tmp_path):
    wav = make_wav(tmp_path / "a.wav", info={"INAM": "Song", "ICMT": '{"old":true}'})
    info = read_wav_info(wav)
    md5_before, _ = pcm_md5_and_sha256(wav, info)
    original = wav.read_bytes()
    at.wav_add_info(wav, "DPCP", BIG)
    assert pcm_md5_and_sha256(wav, read_wav_info(wav))[0] == md5_before
    items = at.wav_read_info(wav)
    assert items["DPCP"] == BIG and items["INAM"] == "Song" and items["ICMT"] == '{"old":true}'
    new = wav.read_bytes()
    assert struct.unpack("<I", new[4:8])[0] == len(new) - 8             # the RIFF size is right
    assert original[original.index(b"data"):] == new[new.index(b"data"):]   # the data chunk, byte for byte
    first = new
    with pytest.raises(at.AlreadyTagged):
        at.wav_add_info(wav, "DPCP", "second")
    assert wav.read_bytes() == first
    assert not list(tmp_path.glob("*.tagging"))


def test_a_wav_with_no_info_chunk_gets_one_before_the_data(tmp_path):
    wav = make_wav(tmp_path / "a.wav")
    at.wav_add_info(wav, "DPCP", '{"a":1}')
    raw = wav.read_bytes()
    assert raw.index(b"LIST") < raw.index(b"data") and at.wav_read_info(wav) == {"DPCP": '{"a":1}'}
    with wave.open(str(wav)) as w:                                      # still a WAV the standard library reads
        assert w.getnframes() == 24000


def test_a_file_that_is_not_the_right_container_is_refused_and_left_alone(tmp_path):
    junk = tmp_path / "a.wav"
    junk.write_bytes(b"not a wav at all, just bytes")
    with pytest.raises(at.TagError):
        at.wav_add_info(junk, "DPCP", "x")
    assert junk.read_bytes() == b"not a wav at all, just bytes" and at.wav_read_info(junk) == {}
    flac = tmp_path / "a.flac"
    flac.write_bytes(b"RIFF....")
    with pytest.raises(at.TagError):
        at.flac_add_comments(flac, {"DPC_PROVENANCE": "x"})
    with pytest.raises(at.TagError):
        at.wav_add_info(junk, "TOOLONG", "x")


@pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg and ffprobe are not installed")
def test_ffprobe_returns_the_record_from_both_containers_and_the_library_identity_does_not_move(tmp_path):
    wav = make_wav(tmp_path / "a.wav", info={"INAM": "Song"})
    flac = make_flac(tmp_path / "a.flac", wav)
    tools = MediaTools.discover()
    before = {p.name: identify(p, tools) for p in (wav, flac)}
    at.wav_add_info(wav, "DPCP", '{"schema":"mp.provenance/1","engine":"acestep.cpp","seed":5}')
    at.flac_add_comments(flac, {"DPC_PROVENANCE": '{"schema":"mp.provenance/1","engine":"yue2.cpp","seed":6}'})
    for p in (wav, flac):
        out = subprocess.run([FFPROBE, "-v", "error", "-show_format", "-of", "json", str(p)], capture_output=True)
        tags = json.loads(out.stdout)["format"]["tags"]
        assert any(k.upper() in ("DPCP", "DPC_PROVENANCE") for k in tags), tags
        after = identify(p, tools)
        # The library keys a WAV on the ffmpeg audio-stream hash and a FLAC on STREAMINFO MD5: tags are
        # outside both, so the same track keeps its id, comments and hidden flag.
        assert (after.hash_kind, after.content_hash) == (before[p.name].hash_kind, before[p.name].content_hash)
        assert after.title == before[p.name].title and after.provenance is not None
    assert identify(wav, tools).provenance["engine"] == "acestep.cpp" and identify(flac, tools).provenance["seed"] == 6
