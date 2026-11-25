"""`music/audio_tags.py` (PROVENANCE rules 1, 6, 7): a FLAC Vorbis comment or a WAV `id3 ` chunk
(`TXXX:DPC_PROVENANCE`, the key every format uses; the old WAV `LIST/INFO` item `DPCP` is still read)
is added without touching the audio (proved by the writer and again here), written once, read back
with a size cap, and readable by ffprobe when it is installed."""

import hashlib
import json
import shutil
import struct
import subprocess
import wave
from pathlib import Path

import pytest

from dpc_client_core.music import audio_tags as at
from dpc_client_core.music.id3 import build_id3v24, text_frame, txxx_frame
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
@pytest.mark.parametrize("legacy", [False, True])
def test_ffprobe_returns_the_record_from_both_containers_and_the_library_identity_does_not_move(tmp_path, legacy):
    wav = make_wav(tmp_path / "a.wav", info={"INAM": "Song"})
    flac = make_flac(tmp_path / "a.flac", wav)
    tools = MediaTools.discover()
    before = {p.name: identify(p, tools) for p in (wav, flac)}
    record = '{"schema":"mp.provenance/1","engine":"acestep.cpp","seed":5}'
    if legacy:                                                          # a WAV written before 2026-11-26
        at.wav_add_info(wav, "DPCP", record)
    else:
        at.wav_add_provenance(wav, record)
    at.flac_add_comments(flac, {"DPC_PROVENANCE": '{"schema":"mp.provenance/1","engine":"yue2.cpp","seed":6}'})
    for p in (wav, flac):
        out = subprocess.run([FFPROBE, "-v", "error", "-show_format", "-of", "json", str(p)], capture_output=True)
        tags = json.loads(out.stdout)["format"]["tags"]
        assert any(k.upper() in ("DPCP", "DPC_PROVENANCE") for k in tags), tags
        if p == wav:                                                    # one key for the new form, the old key for the old
            assert ("DPCP" in tags) is legacy and ("DPC_PROVENANCE" in tags) is not legacy, tags
        after = identify(p, tools)
        # The library keys a WAV on the ffmpeg audio-stream hash and a FLAC on STREAMINFO MD5: tags are
        # outside both, so the same track keeps its id, comments and hidden flag.
        assert (after.hash_kind, after.content_hash) == (before[p.name].hash_kind, before[p.name].content_hash)
        assert after.title == before[p.name].title and after.provenance is not None
    assert identify(wav, tools).provenance["engine"] == "acestep.cpp" and identify(flac, tools).provenance["seed"] == 6


# -- the WAV record is `TXXX:DPC_PROVENANCE` in an `id3 ` chunk, like the MP3 (Mike 2026-11-26) ------------------


def chunks_of(path: Path):
    raw = path.read_bytes()
    out, pos = [], 12
    while pos + 8 <= len(raw):
        cid, n = struct.unpack("<4sI", raw[pos:pos + 8])
        out.append((cid, raw[pos + 8:pos + 8 + n]))
        pos += 8 + n + (n & 1)
    assert pos == len(raw) and struct.unpack("<I", raw[4:8])[0] == len(raw) - 8   # whole chunks, right RIFF size
    return out


def pcm_md5(path: Path) -> str:
    return pcm_md5_and_sha256(path, read_wav_info(path))[0]


def id3_chunk(cid: bytes, frames, version=4) -> bytes:
    tag = build_id3v24(frames)
    if version == 3:
        tag = b"ID3\x03\x00\x00" + tag[6:]
    return cid + struct.pack("<I", len(tag)) + tag + (b"\0" if len(tag) & 1 else b"")


def put_before_data(path: Path, chunk: bytes) -> Path:
    raw = path.read_bytes()
    i = raw.index(b"data")
    raw = raw[:i] + chunk + raw[i:]
    path.write_bytes(raw[:4] + struct.pack("<I", len(raw) - 8) + raw[8:])
    return path


def test_a_wav_gets_its_record_as_an_id3_txxx_and_the_pcm_and_every_other_chunk_are_bit_exact(tmp_path):
    wav = make_wav(tmp_path / "a.wav", info={"INAM": "Song", "ICMT": '{"old":true}'})
    md5_before, original = pcm_md5(wav), chunks_of(wav)
    ffmpeg_before = decoded_md5(wav) if FFMPEG else None
    at.wav_add_provenance(wav, BIG)
    assert pcm_md5(wav) == md5_before                                   # the PCM, packed-sample md5 (the library's)
    if FFMPEG:
        assert decoded_md5(wav) == ffmpeg_before                        # and ffmpeg's decoder agrees
    new = chunks_of(wav)
    assert [c for c, _ in new if c != b"id3 "] == [c for c, _ in original]         # nothing else added or lost
    assert dict(new)[b"data"] == dict(original)[b"data"] and dict(new)[b"fmt "] == dict(original)[b"fmt "]
    assert dict(new)[b"LIST"] == dict(original)[b"LIST"]                # the INFO text items stay as they were
    ids = [c for c, _ in new]
    assert ids.index(b"id3 ") < ids.index(b"data")                      # lowercase on write, before the audio
    assert at.wav_read_id3(wav)["DPC_PROVENANCE"] == BIG and at.wav_read_provenance(wav) == BIG
    assert "DPCP" not in at.wav_read_info(wav) and at.wav_read_info(wav)["INAM"] == "Song"
    with wave.open(str(wav)) as w:                                      # still a WAV the standard library reads
        assert w.getnframes() == 24000
    assert not list(tmp_path.glob("*.tagging"))


def test_the_wav_tag_is_the_same_id3_tag_the_mp3_carries(tmp_path):
    from dpc_client_core.music.export import build_id3v24 as export_builder, read_id3_txxx, txxx_frame as export_txxx
    wav = make_wav(tmp_path / "a.wav")
    at.wav_add_provenance(wav, BIG)
    body = dict(chunks_of(wav))[b"id3 "]
    assert body == export_builder([export_txxx("DPC_PROVENANCE", BIG)])  # one builder, one frame, one reader
    mp3 = tmp_path / "a.mp3"
    mp3.write_bytes(body + b"\xff\xfb")
    assert read_id3_txxx(mp3) == {"DPC_PROVENANCE": BIG}


@pytest.mark.parametrize("n", range(1, 7))
def test_a_chunk_of_odd_or_even_size_is_padded_and_the_riff_size_stays_right(tmp_path, n):
    wav = make_wav(tmp_path / "a.wav")
    data = dict(chunks_of(wav))[b"data"]
    text = "x" * n
    at.wav_add_provenance(wav, text)
    raw = wav.read_bytes()
    at_id3 = raw.index(b"id3 ")
    size = struct.unpack("<I", raw[at_id3 + 4:at_id3 + 8])[0]
    after = raw[at_id3 + 8 + size:]
    assert after.startswith(b"\0data") if size & 1 else after.startswith(b"data")      # a pad byte on an odd size only
    assert dict(chunks_of(wav))[b"data"] == data and at.wav_read_provenance(wav) == text and len(raw) % 2 == 0


def test_both_parities_of_the_tag_are_exercised(tmp_path):
    sizes = set()
    for n in (1, 2):
        wav = make_wav(tmp_path / f"a{n}.wav")
        at.wav_add_provenance(wav, "x" * n)
        sizes.add(len(dict(chunks_of(wav))[b"id3 "]) & 1)
    assert sizes == {0, 1}


def test_a_wav_with_the_old_info_item_is_still_read_and_the_new_form_wins_when_both_exist(tmp_path):
    wav = make_wav(tmp_path / "a.wav", info={"INAM": "Song"})
    at.wav_add_info(wav, "DPCP", '{"old":1}')
    assert at.wav_read_provenance(wav) == '{"old":1}' and at.wav_read_id3(wav) == {}
    put_before_data(wav, id3_chunk(b"id3 ", [txxx_frame("DPC_PROVENANCE", '{"new":1}')]))
    assert at.wav_read_provenance(wav) == '{"new":1}'
    assert at.wav_read_info(wav)["DPCP"] == '{"old":1}'


def test_writing_the_new_form_to_an_old_wav_drops_the_old_item_and_keeps_the_other_info_text(tmp_path):
    wav = make_wav(tmp_path / "a.wav", info={"INAM": "Song", "ICMT": "note"})
    at.wav_add_info(wav, "DPCP", '{"old":1}')
    md5 = pcm_md5(wav)
    at.wav_add_provenance(wav, '{"new":2}')
    info = at.wav_read_info(wav)
    assert "DPCP" not in info and info["INAM"] == "Song" and info["ICMT"] == "note"
    assert at.wav_read_provenance(wav) == '{"new":2}' and pcm_md5(wav) == md5
    chunks_of(wav)


def test_an_old_wav_whose_info_held_only_the_record_loses_the_empty_list_chunk(tmp_path):
    wav = make_wav(tmp_path / "a.wav")
    at.wav_add_info(wav, "DPCP", '{"old":1}')
    assert at.wav_migrate_provenance(wav) is True
    ids = [c for c, _ in chunks_of(wav)]
    assert b"LIST" not in ids and b"id3 " in ids and at.wav_read_provenance(wav) == '{"old":1}'


def test_a_migration_moves_the_old_record_verbatim_once_and_does_nothing_for_a_file_without_one(tmp_path):
    wav = make_wav(tmp_path / "a.wav", info={"INAM": "Song"})
    assert at.wav_migrate_provenance(wav) is False and not any(c == b"id3 " for c, _ in chunks_of(wav))
    at.wav_add_info(wav, "DPCP", BIG)
    md5 = pcm_md5(wav)
    assert at.wav_migrate_provenance(wav) is True
    assert at.wav_read_id3(wav)["DPC_PROVENANCE"] == BIG and "DPCP" not in at.wav_read_info(wav) and pcm_md5(wav) == md5
    once = wav.read_bytes()
    assert at.wav_migrate_provenance(wav) is False and wav.read_bytes() == once


def test_the_reader_takes_both_case_forms_of_the_chunk_id_and_the_writer_keeps_the_one_it_finds(tmp_path):
    for cid in (b"id3 ", b"ID3 "):
        wav = put_before_data(make_wav(tmp_path / f"{cid[0]}.wav"),
                              id3_chunk(cid, [txxx_frame("DPC_PROVENANCE", "rec"), txxx_frame("other", "o")]))
        assert at.wav_read_provenance(wav) == "rec" and at.wav_read_id3(wav)["other"] == "o"
    upper = put_before_data(make_wav(tmp_path / "tit.wav"), id3_chunk(b"ID3 ", [text_frame("TIT2", "Title")]))
    at.wav_add_provenance(upper, "rec2")
    ids = [c for c, _ in chunks_of(upper)]
    assert ids.count(b"ID3 ") == 1 and b"id3 " not in ids               # replaced in place, case kept
    assert at.wav_read_provenance(upper) == "rec2" and b"TIT2" in dict(chunks_of(upper))[b"ID3 "]   # a foreign frame survives


def test_a_record_already_in_the_id3_chunk_is_written_once_and_the_file_is_left_alone(tmp_path):
    wav = make_wav(tmp_path / "a.wav")
    at.wav_add_provenance(wav, '{"first":1}')
    first = wav.read_bytes()
    with pytest.raises(at.AlreadyTagged):
        at.wav_add_provenance(wav, '{"second":2}')
    assert wav.read_bytes() == first and at.wav_read_provenance(wav) == '{"first":1}'
    assert not list(tmp_path.glob("*.tagging"))


def test_an_id3_chunk_that_is_not_a_plain_v24_tag_is_refused_and_the_file_is_left_alone(tmp_path):
    wav = put_before_data(make_wav(tmp_path / "a.wav"), id3_chunk(b"id3 ", [text_frame("TIT2", "T")], version=3))
    before = wav.read_bytes()
    with pytest.raises(at.TagError):
        at.wav_add_provenance(wav, "x")
    assert wav.read_bytes() == before and not list(tmp_path.glob("*.tagging"))


def test_the_wav_reader_has_the_same_cap_as_the_info_item_and_a_bad_file_gives_nothing(tmp_path):
    wav = make_wav(tmp_path / "a.wav")
    at.wav_add_provenance(wav, BIG)
    assert at.wav_read_provenance(wav, max_item_bytes=1000) is None and at.wav_read_id3(wav, max_item_bytes=1000) == {}
    assert at.wav_read_provenance(wav, max_item_bytes=len(BIG.encode("utf-8"))) == BIG
    junk = tmp_path / "j.wav"
    junk.write_bytes(b"not a wav")
    assert at.wav_read_provenance(junk) is None and at.wav_read_id3(junk) == {}
    assert at.wav_read_provenance(tmp_path / "missing.wav") is None
    cut = make_wav(tmp_path / "cut.wav")
    at.wav_add_provenance(cut, BIG)
    raw = cut.read_bytes()
    cut.write_bytes(raw[:raw.index(b"id3 ") + 8 + 200])                 # a file cut inside the tag is a bad file, not an exception
    assert at.wav_read_provenance(cut) is None


@pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg and ffprobe are not installed")
@pytest.mark.parametrize("cid", [b"id3 ", b"ID3 "])
def test_ffprobe_reads_the_tag_from_either_case_of_the_chunk_id(tmp_path, cid):
    wav = put_before_data(make_wav(tmp_path / "a.wav"), id3_chunk(cid, [txxx_frame("DPC_PROVENANCE", '{"a":1}')]))
    out = subprocess.run([FFPROBE, "-v", "error", "-show_format", "-of", "json", str(wav)], capture_output=True)
    assert json.loads(out.stdout)["format"]["tags"]["DPC_PROVENANCE"] == '{"a":1}'
