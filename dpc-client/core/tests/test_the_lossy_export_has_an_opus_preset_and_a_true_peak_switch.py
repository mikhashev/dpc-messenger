"""The lossy export after Mike's decisions of 2026-11-25 (AI Studio group #171): the -1 dBTP true-peak
limit stays the default with a switch to turn it off (`limit_true_peak=False`, a separate export), and
`opus-160` is a third preset, Opus 160 kbps in an Ogg `.opus` file with the same provenance and lyrics
tags as the MP3, written as Vorbis comments by `retag_ogg_opus`."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from dpc_client_core.music import export as ex
from dpc_client_core.music.export import (
    ExportError, ExportSource, MusicExporter, build_opus_tags, export_id_of, ogg_crc, ogg_pages_for_packet,
    read_export_marker, read_export_tags, read_id3_txxx, read_opus_comments, retag_ogg_opus,
)
from dpc_client_core.music.identity import MediaTools

HASH = "md5:" + "ab12cd" * 5 + "ef"


# -- a fake ffmpeg that writes a small Ogg Opus file ------------------------------------------------


def fake_ogg_opus(pages_after_tags=3, tag_pages=1, serial=0x1234):
    """OpusHead, a tag packet on `tag_pages` pages, then audio pages."""
    head = ex._OggPage(0x02, 0, serial, 0, bytes([19]), b"OpusHead\x01\x02\x38\x01\x80\xbb\x00\x00\x00\x00\x00").to_bytes()
    vendor = b"Lavf-test"
    if tag_pages == 1:
        tags = ex.build_opus_tags(vendor, [("ENCODER", "Lavf-test")])
        pages = ogg_pages_for_packet(tags, serial, 1)
    else:
        tags = ex.build_opus_tags(vendor, [("COMMENT", "x" * (255 * 255 + 300))])
        pages = ogg_pages_for_packet(tags, serial, 1)
    assert len(pages) == tag_pages
    out = head + b"".join(p.to_bytes() for p in pages)
    seq = 1 + tag_pages
    for i in range(pages_after_tags):
        body = bytes([i + 1]) * (200 + i)
        eos = 0x04 if i == pages_after_tags - 1 else 0x00
        out += ex._OggPage(eos, 960 * (i + 1), serial, seq + i, bytes([len(body)]), body).to_bytes()
    return out


def pages_of(raw: bytes):
    out, pos = [], 0
    while pos < len(raw):
        assert raw[pos:pos + 4] == b"OggS"
        nseg = raw[pos + 26]
        size = sum(raw[pos + 27:pos + 27 + nseg])
        end = pos + 27 + nseg + size
        out.append(raw[pos:end])
        pos = end
    return out


def assert_valid_ogg(raw: bytes):
    seqs = []
    for page in pages_of(raw):
        stored = int.from_bytes(page[22:26], "little")
        assert ogg_crc(page[:22] + b"\x00\x00\x00\x00" + page[26:]) == stored
        seqs.append(int.from_bytes(page[18:22], "little"))
    assert seqs == list(range(len(seqs)))


class FakeFfmpeg:
    def __init__(self, *, opus=True, lame=True):
        self.encodes = []
        self.opus = opus
        self.lame = lame

    def __call__(self, cmd, *, timeout):
        cmd = list(cmd)
        if "-encoders" in cmd:
            out = (b" A....D libmp3lame  libmp3lame MP3\n" if self.lame else b"") + (
                b" A....D libopus  libopus Opus\n" if self.opus else b"")
            return subprocess.CompletedProcess(cmd, 0, out, b"")
        if "-version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, b"ffmpeg version 9.9-test Copyright (c) x\n", b"")
        self.encodes.append(cmd)
        out = Path(cmd[-1])
        out.write_bytes(fake_ogg_opus() if "libopus" in cmd else b"\xff\xfb\x90\x00" + b"audio" * 50)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")


class Peaks:
    def __init__(self, *outs):
        self.outs = list(outs)

    def __call__(self, path):
        return {"true_peak_dbtp": self.outs.pop(0) if len(self.outs) > 1 else self.outs[0]}


def _exporter(root, fake=None, peaks=None):
    fake = fake or FakeFfmpeg()
    tools = MediaTools(ffmpeg="ffmpeg", ffprobe=None, runner=fake)
    return MusicExporter(root, tools=tools, measure=peaks or Peaks(-1.5)), fake


def _source(tmp_path, name="a.flac", content_hash=HASH, title="Song", artist="Band", tp=-0.5, **kw):
    path = tmp_path / "lib" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(b"source bytes " + name.encode())
    return ExportSource(track_id="trk_" + name.split(".")[0], path=path, content_hash=content_hash, title=title,
                        artist=artist, true_peak_dbtp=tp, true_peak_basis="test", **kw)


# -- the presets ---------------------------------------------------------------------------------


def test_the_presets_are_mp3_v0_mp3_320_and_opus_160():
    assert list(ex.PRESETS) == ["mp3-v0", "mp3-320", "opus-160"]
    assert ex.DEFAULT_PRESET == "mp3-v0"
    opus = ex.PRESETS["opus-160"]
    assert opus["ext"] == ".opus" and opus["format"] == "opus" and opus["ffmpeg_encoder"] == "libopus"
    assert "-b:a" in opus["args"] and "160k" in opus["args"] and opus["mux"] == ["-f", "ogg"]


# -- the Ogg Opus writer and reader ----------------------------------------------------------------


def test_the_crc_is_the_ogg_one():
    assert ogg_crc(b"") == 0
    # the checksum of a page written by this module is the one a page carries, and a bit flip breaks it
    page = ogg_pages_for_packet(b"OpusTags" + b"\x00" * 20, 7, 1)[0].to_bytes()
    assert ogg_crc(page[:22] + b"\x00\x00\x00\x00" + page[26:]) == int.from_bytes(page[22:26], "little")
    flipped = bytearray(page)
    flipped[-1] ^= 1
    assert ogg_crc(bytes(flipped[:22]) + b"\x00\x00\x00\x00" + bytes(flipped[26:])) != int.from_bytes(page[22:26], "little")


def test_a_tag_packet_over_one_page_is_split_with_the_continuation_flag_and_no_granule():
    packet = b"OpusTags" + b"z" * (255 * 255 + 10)
    pages = ogg_pages_for_packet(packet, 5, 1)
    assert len(pages) == 2
    assert pages[0].header_type == 0 and pages[0].granule == 0xFFFFFFFFFFFFFFFF
    assert pages[1].header_type == 1 and pages[1].granule == 0
    assert b"".join(p.body for p in pages) == packet
    assert [p.seq for p in pages] == [1, 2]
    exact = ogg_pages_for_packet(b"y" * 255, 5, 1)  # a packet that is a whole segment gets a closing 0
    assert exact[0].lacing == bytes([255, 0])


def test_retag_replaces_the_tag_packet_and_copies_the_audio_pages_byte_for_byte(tmp_path):
    src = tmp_path / "in.opus"
    src.write_bytes(fake_ogg_opus())
    out = tmp_path / "out.opus"
    sha, size = retag_ogg_opus(src, [("title", "Song ü"), ("DPC_EXPORT_OF", HASH), ("LYRICS", "a\nb")], out)
    raw = out.read_bytes()
    assert size == len(raw) and sha.startswith("sha256:")
    assert_valid_ogg(raw)
    old, new = pages_of(src.read_bytes()), pages_of(raw)
    assert new[0] == old[0] and new[2:] == old[2:]  # OpusHead and every audio page untouched
    assert read_opus_comments(out) == {"TITLE": "Song ü", "DPC_EXPORT_OF": HASH, "LYRICS": "a\nb"}
    vendor = ex.read_opus_tags_packet(out)[8 + 4:8 + 4 + len(b"Lavf-test")]
    assert vendor == b"Lavf-test"  # the encoder's vendor string is kept


def test_retag_renumbers_and_re_checksums_the_audio_when_the_tag_page_count_changes(tmp_path):
    src = tmp_path / "in.opus"
    src.write_bytes(fake_ogg_opus(tag_pages=2))
    out = tmp_path / "out.opus"
    retag_ogg_opus(src, [("TITLE", "short")], out)
    raw = out.read_bytes()
    assert_valid_ogg(raw)
    old, new = pages_of(src.read_bytes()), pages_of(raw)
    assert len(new) == len(old) - 1
    # the audio bodies and granule positions are the same, only the sequence number and checksum moved
    for before, after in zip(old[3:], new[2:]):
        assert before[27:] == after[27:] and before[6:14] == after[6:14]
        assert int.from_bytes(after[18:22], "little") == int.from_bytes(before[18:22], "little") - 1
    assert read_opus_comments(out) == {"TITLE": "short"}
    assert pages_of(raw)[-1][5] & 0x04  # the end-of-stream flag survives


def test_retag_refuses_a_stream_that_is_not_ogg_opus_and_tags_over_the_decoder_limit(tmp_path):
    bad = tmp_path / "bad.opus"
    bad.write_bytes(b"\xff\xfb\x90\x00" + b"x" * 100)
    with pytest.raises(ExportError) as e:
        retag_ogg_opus(bad, [("TITLE", "x")], tmp_path / "o.opus")
    assert e.value.code == "encode_failed"
    good = tmp_path / "good.opus"
    good.write_bytes(fake_ogg_opus())
    with pytest.raises(ExportError) as e:
        retag_ogg_opus(good, [("LYRICS", "x" * (ex.OPUS_TAGS_MAX_BYTES + 10))], tmp_path / "o2.opus")
    assert e.value.code == "encode_failed" and "over the" in e.value.message


def test_the_reader_gives_nothing_for_a_file_that_is_not_ogg_opus(tmp_path):
    assert read_opus_comments(tmp_path / "gone.opus") == {}
    mp3 = tmp_path / "x.opus"
    mp3.write_bytes(b"ID3\x04" + b"\x00" * 50)
    assert read_opus_comments(mp3) == {} and read_export_marker(mp3) is None
    vorbis = tmp_path / "v.ogg"
    vorbis.write_bytes(ex._OggPage(0x02, 0, 1, 0, bytes([7]), b"\x01vorbis").to_bytes())
    assert read_export_marker(vorbis) is None


def test_a_comment_name_is_kept_to_printable_ascii_without_an_equals_sign():
    packet = build_opus_tags(b"v", [("ti=tle", "x"), ("ÄÖ", "dropped"), ("ok", "a\x00b")])
    parsed = ex._parse_vorbis(packet[8:])
    assert parsed[1] == [("TITLE", "x"), ("OK", "ab")]


# -- the Opus export --------------------------------------------------------------------------------


def test_an_opus_export_is_an_ogg_opus_file_with_the_tags_and_provenance_of_the_mp3(tmp_path):
    sidecar = {"schema": "dpc.take-sidecar/1", "job_id": "job_1", "seed": 42}
    src = _source(tmp_path, lyrics="first line\nвторая строка", take_record=sidecar, origin="generated", album="Album")
    exporter, fake = _exporter(tmp_path / "out")
    answer = exporter.export_track(src, "opus-160")
    path = Path(answer["path"])
    assert answer["name"] == "Band - Song.opus" and answer["preset"] == "opus-160" and answer["limit_true_peak"] is True
    raw = path.read_bytes()
    assert raw[:4] == b"OggS" and b"OpusHead" in raw[:64]
    assert_valid_ogg(raw)
    cmd = fake.encodes[-1]
    assert cmd[cmd.index("-c:a") + 1] == "libopus" and cmd[cmd.index("-b:a") + 1] == "160k"
    assert cmd[cmd.index("-ar") + 1] == "48000" and cmd[-3:-1] == ["-f", "ogg"]
    tags = read_opus_comments(path)
    assert tags["TITLE"] == "Song" and tags["ARTIST"] == "Band" and tags["ALBUM"] == "Album"
    assert tags["LYRICS"] == "first line\nвторая строка"
    assert tags["DPC_EXPORT_OF"] == HASH and tags["DPC_EXPORT_PRESET"] == "opus-160"
    assert "DPC_EXPORT_TP_LIMIT" not in tags and "libopus" in tags["ENCODER"]
    record = json.loads(tags["DPC_PROVENANCE"])
    assert record["schema"] == "dpc.export-provenance/1" and record["take"] == sidecar
    post = record["dpc_postprocess"]
    assert post["encoder"] == "libopus" and post["preset"] == "opus-160" and post["settings"]["container"] == "ogg"
    assert post["cmd_as_sent"][-1] == "Band - Song.opus.audio.part" and "Vorbis comments" in post["tags"]
    assert str(tmp_path) not in tags["DPC_PROVENANCE"]
    assert read_export_marker(path) == (HASH, "opus-160")
    assert not list((tmp_path / "out").glob("*.part"))


def test_an_opus_and_an_mp3_of_one_song_sit_side_by_side_and_neither_is_encoded_twice(tmp_path):
    exporter, fake = _exporter(tmp_path / "out")
    src = _source(tmp_path)
    names = [exporter.export_track(src, p)["name"] for p in ("mp3-v0", "opus-160", "mp3-v0", "opus-160")]
    assert names == ["Band - Song.mp3", "Band - Song.opus", "Band - Song.mp3", "Band - Song.opus"]
    assert len(fake.encodes) == 2
    listed = exporter.exports_for({HASH: ["trk_a"]})["trk_a"]
    assert {e["preset"]: e["format"] for e in listed} == {"mp3-v0": "mp3", "opus-160": "opus"}


def test_an_opus_file_renamed_by_hand_or_a_lost_index_is_found_by_its_tag(tmp_path):
    exporter, fake = _exporter(tmp_path / "out")
    src = _source(tmp_path)
    path = Path(exporter.export_track(src, "opus-160")["path"])
    renamed = path.with_name("my favourite.opus")
    path.rename(renamed)
    found = exporter.export_track(src, "opus-160")
    assert found["relinked"] is True and found["path"] == str(renamed)
    (tmp_path / "out" / ".dpc-exports.json").unlink()
    fresh, fake2 = _exporter(tmp_path / "out")
    assert fresh.export_track(src, "opus-160")["path"] == str(renamed) and not fake2.encodes
    assert len(fake.encodes) == 1


def test_a_provenance_record_too_big_for_an_opus_tag_is_shortened_to_fit(tmp_path):
    big = {"schema": "dpc.take-sidecar/1", "notes": "n" * 60_000}
    exporter, _ = _exporter(tmp_path / "out")
    path = Path(exporter.export_track(_source(tmp_path, take_record=big), "opus-160")["path"])
    packet = ex.read_opus_tags_packet(path)
    assert len(packet) <= ex.OPUS_TAGS_MAX_BYTES
    record = json.loads(read_opus_comments(path)["DPC_PROVENANCE"])
    assert record["take"]["omitted"].startswith("over ") and record["take"]["sha256"].startswith("sha256:")


def test_a_missing_libopus_is_a_clear_error_and_does_not_stop_the_mp3(tmp_path):
    exporter, _ = _exporter(tmp_path / "out", FakeFfmpeg(opus=False))
    with pytest.raises(ExportError) as e:
        exporter.export_track(_source(tmp_path), "opus-160")
    assert e.value.code == "no_opus_encoder" and "libopus" in e.value.message
    assert exporter.export_track(_source(tmp_path), "mp3-v0")["name"] == "Band - Song.mp3"


def test_an_opus_playlist_is_its_own_folder_of_numbered_opus_files(tmp_path):
    exporter, fake = _exporter(tmp_path / "out")
    a = _source(tmp_path, "a.flac", content_hash="md5:aaaa", title="First")
    b = _source(tmp_path, "b.flac", content_hash="md5:bbbb", title="Second")
    exporter.export_playlist("pl_1", "Mix", [a, b])
    answer = exporter.export_playlist("pl_1", "Mix", [a, b], "opus-160")
    folder = tmp_path / "out" / "Mix (Opus 160)"
    assert answer["folder"] == str(folder)
    assert sorted(p.name for p in folder.iterdir()) == ["01 - Band - First.opus", "02 - Band - Second.opus"]
    assert sorted(p.name for p in (tmp_path / "out" / "Mix").iterdir()) == ["01 - Band - First.mp3", "02 - Band - Second.mp3"]
    assert read_export_marker(folder / "01 - Band - First.opus") == ("md5:aaaa", "opus-160")
    again = exporter.export_playlist("pl_1", "Mix", [a, b], "opus-160")
    assert {t["placed"] for t in again["tracks"]} == {"unchanged"}
    assert len(fake.encodes) == 4


# -- the true-peak switch ------------------------------------------------------------------------------


def _filters(fake):
    return [c[c.index("-af") + 1] if "-af" in c else None for c in fake.encodes]


@pytest.mark.parametrize("preset,ext", [("mp3-v0", ".mp3"), ("opus-160", ".opus")])
def test_with_the_limit_off_a_loud_source_is_encoded_once_as_it_comes(tmp_path, preset, ext):
    exporter, fake = _exporter(tmp_path / "out", peaks=Peaks(0.4))
    answer = exporter.export_track(_source(tmp_path, tp=1.4), preset, limit_true_peak=False)
    assert _filters(fake) == [None] and len(fake.encodes) == 1
    assert answer["limit_true_peak"] is False and answer["limited_db"] is None and answer["true_peak_out_dbtp"] == 0.4
    assert answer["name"] == "Band - Song (no limit)" + ext
    [record] = json.loads((tmp_path / "out" / ".dpc-exports.json").read_text(encoding="utf-8"))["exports"]
    assert record["true_peak_limit"] is False
    assert record["limiter"]["enabled"] is False and record["limiter"]["applied"] is False
    assert record["limiter"]["reached"] is False and "switched off" in record["limiter"]["policy"]
    tags = read_id3_txxx(Path(answer["path"])) if ext == ".mp3" else read_opus_comments(Path(answer["path"]))
    assert tags["DPC_EXPORT_TP_LIMIT"] == "off"
    assert json.loads(tags["DPC_PROVENANCE"])["dpc_postprocess"]["limiter"]["enabled"] is False


def test_the_limit_is_on_by_default_and_a_quiet_source_needs_no_limiter_either_way(tmp_path):
    exporter, fake = _exporter(tmp_path / "out", peaks=Peaks(0.3, -1.1))
    on = exporter.export_track(_source(tmp_path, tp=1.4))
    assert on["limit_true_peak"] is True and on["limited_db"] == -1.0 and on["name"] == "Band - Song.mp3"
    assert all(f and "alimiter" in f for f in _filters(fake))


def test_the_limited_and_the_unlimited_export_of_one_song_are_two_files_with_two_ids(tmp_path):
    exporter, fake = _exporter(tmp_path / "out", peaks=Peaks(-1.5))
    src = _source(tmp_path, tp=-3.0)
    limited = exporter.export_track(src)
    unlimited = exporter.export_track(src, limit_true_peak=False)
    assert limited["path"] != unlimited["path"] and len(fake.encodes) == 2
    assert exporter.export_track(src)["already_exported"] is True
    assert exporter.export_track(src, limit_true_peak=False)["path"] == unlimited["path"]
    listed = {e["limit_true_peak"]: e for e in exporter.exports_for({HASH: ["trk_a"]})["trk_a"]}
    assert listed[True]["export_id"] == export_id_of(HASH, "mp3", "mp3-v0")
    assert listed[False]["export_id"] == export_id_of(HASH, "mp3", "mp3-v0", False)
    assert listed[True]["export_id"] != listed[False]["export_id"]
    # deleting one leaves the other
    exporter.delete_export(export_id=listed[False]["export_id"])
    assert Path(limited["path"]).exists() and not Path(unlimited["path"]).exists()
    assert [e["limit_true_peak"] for e in exporter.exports_for({HASH: ["trk_a"]})["trk_a"]] == [True]


def test_a_lost_index_finds_each_of_the_two_files_by_its_own_limit_tag(tmp_path):
    exporter, _ = _exporter(tmp_path / "out", peaks=Peaks(-1.5))
    src = _source(tmp_path, tp=-3.0)
    limited = Path(exporter.export_track(src)["path"])
    unlimited = Path(exporter.export_track(src, limit_true_peak=False)["path"])
    assert read_export_tags(limited)["limit"] is True and read_export_tags(unlimited)["limit"] is False
    limited.rename(limited.with_name("a.mp3"))
    unlimited.rename(unlimited.with_name("b.mp3"))
    (tmp_path / "out" / ".dpc-exports.json").unlink()
    fresh, fake = _exporter(tmp_path / "out")
    assert fresh.export_track(src)["path"].endswith("a.mp3")
    assert fresh.export_track(src, limit_true_peak=False)["path"].endswith("b.mp3")
    assert not fake.encodes


def test_an_unlimited_playlist_is_its_own_folder_and_run(tmp_path):
    exporter, _ = _exporter(tmp_path / "out", peaks=Peaks(-1.5))
    a = _source(tmp_path, "a.flac", content_hash="md5:aaaa", title="First", tp=-3.0)
    exporter.export_playlist("pl_1", "Mix", [a])
    answer = exporter.export_playlist("pl_1", "Mix", [a], limit_true_peak=False)
    assert answer["folder"] == str(tmp_path / "out" / "Mix (no limit)")
    assert (tmp_path / "out" / "Mix" / "01 - Band - First.mp3").is_file()
    assert (tmp_path / "out" / "Mix (no limit)" / "01 - Band - First.mp3").is_file()
    plays = json.loads((tmp_path / "out" / ".dpc-exports.json").read_text(encoding="utf-8"))["playlists"]
    assert sorted(p.get("true_peak_limit", True) for p in plays) == [False, True]
    again = exporter.export_playlist("pl_1", "Mix", [a], limit_true_peak=False)
    assert {t["placed"] for t in again["tracks"]} == {"unchanged"}
    # each export's single lists only the playlist copies of its own limit
    by_limit = {e["limit_true_peak"]: e for e in exporter.exports_for({"md5:aaaa": ["trk_a"]})["trk_a"]}
    assert [c["folder"] for c in by_limit[True]["playlists"]] == ["Mix"]
    assert [c["folder"] for c in by_limit[False]["playlists"]] == ["Mix (no limit)"]


# -- the commands ------------------------------------------------------------------------------------------


def test_the_commands_take_the_switch_as_a_boolean_and_default_it_to_on():
    from dpc_client_core.music.commands import _opt_flag
    from dpc_client_core.music.library import MusicLibraryError
    assert _opt_flag(None, "limit_true_peak", True) is True
    assert _opt_flag(False, "limit_true_peak", True) is False
    for bad in ("false", 0, 1, [], {}):
        with pytest.raises(MusicLibraryError) as e:
            _opt_flag(bad, "limit_true_peak", True)
        assert e.value.code == "invalid_params"


# -- a real ffmpeg ----------------------------------------------------------------------------------------------

FFMPEG = shutil.which("ffmpeg")
HAVE_OPUS = bool(FFMPEG) and "libopus" in subprocess.run(
    [FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout if FFMPEG else False


@pytest.mark.skipif(not HAVE_OPUS, reason="needs an ffmpeg with libopus")
def test_a_real_ffmpeg_makes_an_opus_file_that_decodes_and_carries_our_tags(tmp_path):
    wav = tmp_path / "lib" / "loud.wav"
    wav.parent.mkdir(parents=True)
    subprocess.run([FFMPEG, "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-af", "volume=18dB", "-ar", "44100", str(wav)], check=True)
    src = ExportSource(track_id="trk_l", path=wav, content_hash=HASH, title="Loud", artist="Band",
                       lyrics="line one\nline two", true_peak_dbtp=0.0, true_peak_basis="test")
    exporter = MusicExporter(tmp_path / "out")
    limited = exporter.export_track(src, "opus-160")
    path = Path(limited["path"])
    decode = subprocess.run([FFMPEG, "-nostdin", "-loglevel", "error", "-i", str(path), "-f", "null", "-"],
                            capture_output=True, text=True)
    assert decode.returncode == 0 and decode.stderr.strip() == ""
    assert_valid_ogg(path.read_bytes())
    tags = read_opus_comments(path)
    assert tags["TITLE"] == "Loud" and tags["LYRICS"] == "line one\nline two" and tags["DPC_EXPORT_OF"] == HASH
    assert json.loads(tags["DPC_PROVENANCE"])["dpc_postprocess"]["cmd_as_sent"][0] == "ffmpeg"
    assert limited["limited_db"] == -1.0 and limited["true_peak_out_dbtp"] is not None
    assert limited["true_peak_out_dbtp"] <= -0.7
    unlimited = exporter.export_track(src, "opus-160", limit_true_peak=False)
    assert unlimited["limited_db"] is None and unlimited["true_peak_out_dbtp"] > limited["true_peak_out_dbtp"]
    probe = shutil.which("ffprobe")
    if probe:
        shown = subprocess.run([probe, "-v", "error", "-show_entries", "stream_tags=title,lyrics,dpc_export_of",
                                "-of", "json", str(path)], capture_output=True, text=True)
        stream_tags = {k.lower(): v for k, v in (json.loads(shown.stdout)["streams"][0].get("tags") or {}).items()}
        assert stream_tags.get("title") == "Loud" and stream_tags.get("dpc_export_of") == HASH
