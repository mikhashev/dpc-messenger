"""MP3 export (`music/export.py`, Mike #210): one root, flat portable names, one file per
(source content hash, format, preset), playlists as one level of numbered links, a -1 dBTP
limiter on the export only, and the provenance inside the MP3's own ID3v2.4 tag."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from dpc_client_core.music import export as ex
from dpc_client_core.music.export import (
    ExportError, ExportSource, MusicExporter, SkippedTrack, build_id3v24, pick_name, read_export_marker,
    read_id3_txxx, sanitize_component, track_stem, txxx_frame,
)
from dpc_client_core.music.identity import MediaTools

HAVE_LAME = bool(shutil.which("ffmpeg")) and "libmp3lame" in (
    subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    if shutil.which("ffmpeg") else "")


# -- a fake ffmpeg ---------------------------------------------------------------------------


class FakeFfmpeg:
    """Answers `-encoders` and `-version`, and "encodes" by writing a few bytes to the output."""

    def __init__(self, *, rc=0, lame=True):
        self.encodes = []
        self.rc = rc
        self.lame = lame

    def __call__(self, cmd, *, timeout):
        cmd = list(cmd)
        if "-encoders" in cmd:
            out = b" A....D libmp3lame  libmp3lame MP3\n" if self.lame else b" A....D flac\n"
            return subprocess.CompletedProcess(cmd, 0, out, b"")
        if "-version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, b"ffmpeg version 9.9-test Copyright (c) x\n", b"")
        self.encodes.append(cmd)
        Path(cmd[-1]).write_bytes(b"\xff\xfb\x90\x00" + b"audio" * 50)
        err = b"boom: the encoder failed" if self.rc else b""
        return subprocess.CompletedProcess(cmd, self.rc, b"", err)


class Peaks:
    """The measure: the source's true peak, then one value per encode."""

    def __init__(self, *outs, source=None):
        self.outs = list(outs)
        self.source = source
        self.calls = []

    def __call__(self, path):
        self.calls.append(Path(path).name)
        if str(path).endswith(".audio.part"):
            value = self.outs.pop(0) if len(self.outs) > 1 else self.outs[0]
        else:
            value = self.source
        return {"true_peak_dbtp": value}


def _exporter(root, fake=None, peaks=None):
    fake = fake or FakeFfmpeg()
    tools = MediaTools(ffmpeg="ffmpeg", ffprobe=None, runner=fake)
    return MusicExporter(root, tools=tools, measure=peaks or Peaks(-1.5)), fake


def _source(tmp_path, name="a.flac", content_hash="md5:" + "ab12cd" * 5 + "ef", title="Song", artist="Band",
            tp=-0.5, **kw):
    path = tmp_path / "lib" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(b"source bytes " + name.encode())
    return ExportSource(track_id="trk_" + name.split(".")[0], path=path, content_hash=content_hash, title=title,
                        artist=artist, true_peak_dbtp=tp, true_peak_basis="test", **kw)


# -- names ---------------------------------------------------------------------------------


def test_names_are_portable_on_every_platform():
    assert sanitize_component('AC/DC: Live? <best> | "x" *') == "AC_DC - Live _best_ _ 'x'"
    assert sanitize_component("  ..hidden. ") == "hidden"
    assert sanitize_component("trailing dots...") == "trailing dots"
    assert sanitize_component("tab\there\nnewline") == "tab here newline"
    assert sanitize_component("CON") == "_CON"
    assert sanitize_component("nul.txt") == "_nul.txt"
    assert sanitize_component("") == "Untitled"
    assert sanitize_component("???") == "Untitled"
    long = sanitize_component("я" * 200, 120)
    assert len(long.encode("utf-8")) <= 120 and long == "я" * 60  # cut on a character boundary


def test_the_stem_is_artist_dash_title_or_the_title_and_names_the_preset():
    assert track_stem("Band", "Song") == "Band - Song"
    assert track_stem(None, "Song") == "Song"
    assert track_stem("  ", "Song") == "Song"
    assert track_stem("Band", "Song", "mp3-320") == "Band - Song (320 kbps)"
    stem = track_stem("A" * 300, "B" * 300)
    assert len((stem + " (abcdef).mp3").encode("utf-8")) <= ex.STEM_MAX_BYTES + 4


def test_a_taken_name_gets_a_suffix_from_the_source_hash_compared_without_case():
    taken = {ex.name_key("band - song.mp3")}
    assert pick_name("Band - Song", ".mp3", taken, "md5:a1b2c3d4e5f6a7b8") == "Band - Song (a1b2c3).mp3"
    taken.add(ex.name_key("Band - Song (A1B2C3).mp3"))
    assert pick_name("Band - Song", ".mp3", taken, "md5:a1b2c3d4e5f6a7b8") == "Band - Song (a1b2c3d4e5f6).mp3"
    assert pick_name("Band - Song", ".mp3", set(), "sha256:ff") == "Band - Song.mp3"


# -- the ID3 tag -----------------------------------------------------------------------------


def test_the_tag_reader_reads_back_what_the_writer_wrote(tmp_path):
    f = tmp_path / "x.mp3"
    f.write_bytes(build_id3v24([txxx_frame("DPC_EXPORT_OF", "md5:abc"), txxx_frame("DPC_EXPORT_PRESET", "mp3-320"),
                                txxx_frame("note", "ünïcode")]) + b"\xff\xfbaudio")
    assert read_id3_txxx(f) == {"DPC_EXPORT_OF": "md5:abc", "DPC_EXPORT_PRESET": "mp3-320", "note": "ünïcode"}
    assert read_export_marker(f) == ("md5:abc", "mp3-320")
    plain = tmp_path / "plain.mp3"
    plain.write_bytes(b"\xff\xfbno tag at all")
    assert read_export_marker(plain) is None
    assert read_export_marker(tmp_path / "gone.mp3") is None
    assert read_export_marker(tmp_path / "x.flac") is None


def test_a_v23_tag_with_utf16_text_is_read_too(tmp_path):
    def frame23(fid, payload):
        return fid + len(payload).to_bytes(4, "big") + b"\x00\x00" + payload
    payload = b"\x01" + "DPC_EXPORT_OF".encode("utf-16") + b"\x00\x00" + "md5:def".encode("utf-16")
    body = frame23(b"TXXX", payload) + b"\x00" * 16
    f = tmp_path / "v23.mp3"
    f.write_bytes(b"ID3\x03\x00\x00" + ex._syncsafe(len(body)) + body + b"\xff\xfb")
    assert read_export_marker(f) == ("md5:def", None)


# -- one track -------------------------------------------------------------------------------


def test_a_second_export_of_the_same_source_and_preset_answers_the_existing_file(tmp_path):
    exporter, fake = _exporter(tmp_path / "out")
    src = _source(tmp_path)
    first = exporter.export_track(src)
    assert first["already_exported"] is False and first["name"] == "Band - Song.mp3"
    assert Path(first["path"]).parent == tmp_path / "out"
    second = exporter.export_track(src, "mp3-v0")
    assert second["already_exported"] is True and second["path"] == first["path"]
    assert len(fake.encodes) == 1
    # a fresh exporter over the same root reads the index
    again, fake2 = _exporter(tmp_path / "out")
    assert again.export_track(src)["already_exported"] is True and not fake2.encodes
    index = json.loads((tmp_path / "out" / ".dpc-exports.json").read_text(encoding="utf-8"))
    [record] = index["exports"]
    assert record["source_hash"] == src.content_hash and record["format"] == "mp3" and record["preset"] == "mp3-v0"
    assert record["path"] == "Band - Song.mp3" and record["sha256"].startswith("sha256:") and record["exported_at"]


def test_another_preset_is_another_file_and_another_source_with_the_same_name_gets_a_suffix(tmp_path):
    exporter, fake = _exporter(tmp_path / "out")
    a = _source(tmp_path, "a.flac", content_hash="md5:aaaaaa11")
    b = _source(tmp_path, "b.flac", content_hash="md5:bbbbbb22")
    assert exporter.export_track(a)["name"] == "Band - Song.mp3"
    assert exporter.export_track(a, "mp3-320")["name"] == "Band - Song (320 kbps).mp3"
    assert exporter.export_track(b)["name"] == "Band - Song (bbbbbb).mp3"
    assert len(fake.encodes) == 3
    assert "-b:a" in fake.encodes[1] and "320k" in fake.encodes[1] and "-q:a" in fake.encodes[0]
    with pytest.raises(ExportError) as e:
        exporter.export_track(a, "opus-128")
    assert e.value.code == "invalid_preset"


def test_a_file_deleted_by_hand_is_made_again_under_the_same_name(tmp_path):
    exporter, fake = _exporter(tmp_path / "out")
    src = _source(tmp_path)
    path = Path(exporter.export_track(src)["path"])
    path.unlink()
    again = exporter.export_track(src)
    assert again["already_exported"] is False and again["path"] == str(path) and path.is_file()
    assert len(fake.encodes) == 2
    assert len(json.loads((tmp_path / "out" / ".dpc-exports.json").read_text(encoding="utf-8"))["exports"]) == 1


def test_a_file_renamed_by_hand_or_a_lost_index_is_found_by_its_tag_not_encoded_again(tmp_path):
    exporter, fake = _exporter(tmp_path / "out")
    src = _source(tmp_path)
    path = Path(exporter.export_track(src)["path"])
    renamed = path.with_name("my favourite.mp3")
    path.rename(renamed)
    found = exporter.export_track(src)
    assert found["already_exported"] is True and found["relinked"] is True and found["path"] == str(renamed)
    (tmp_path / "out" / ".dpc-exports.json").unlink()
    fresh, fake2 = _exporter(tmp_path / "out")
    assert fresh.export_track(src)["path"] == str(renamed) and not fake2.encodes
    assert len(fake.encodes) == 1


def test_a_failed_encode_leaves_no_part_file_and_no_index_entry(tmp_path):
    exporter, fake = _exporter(tmp_path / "out", FakeFfmpeg(rc=1))
    with pytest.raises(ExportError) as e:
        exporter.export_track(_source(tmp_path))
    assert e.value.code == "encode_failed" and "boom" in e.value.message
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == []


def test_no_ffmpeg_or_no_lame_is_a_clear_error(tmp_path):
    exporter = MusicExporter(tmp_path / "out", tools=MediaTools(ffmpeg=None, ffprobe=None), measure=Peaks(-2))
    with pytest.raises(ExportError) as e:
        exporter.export_track(_source(tmp_path))
    assert e.value.code == "no_ffmpeg" and "ffmpeg" in e.value.message
    exporter, _ = _exporter(tmp_path / "out", FakeFfmpeg(lame=False))
    with pytest.raises(ExportError) as e:
        exporter.export_track(_source(tmp_path))
    assert e.value.code == "no_mp3_encoder" and "libmp3lame" in e.value.message


# -- the limiter -----------------------------------------------------------------------------


def _filters(fake):
    return [c[c.index("-af") + 1] if "-af" in c else None for c in fake.encodes]


def test_no_limiter_when_the_measured_true_peak_is_already_at_or_below_minus_one(tmp_path):
    exporter, fake = _exporter(tmp_path / "out", peaks=Peaks(-3.2))
    answer = exporter.export_track(_source(tmp_path, tp=-1.0))
    assert _filters(fake) == [None] and answer["limited_db"] is None and answer["true_peak_out_dbtp"] == -3.2


def test_the_limiter_runs_when_the_true_peak_is_above_minus_one_and_lowers_its_limit_on_overshoot(tmp_path):
    exporter, fake = _exporter(tmp_path / "out", peaks=Peaks(0.3, -1.1))
    answer = exporter.export_track(_source(tmp_path, tp=1.4))
    first, second = _filters(fake)
    assert "alimiter=limit=0.84140" in first and first.startswith("aresample=") and "level=false" in first
    # overshoot 1.3 dB over the ceiling: the next limit is -1.5 - 1.3 - 0.2 = -3.0 dBFS
    assert "alimiter=limit=0.70795" in second
    assert answer["limited_db"] == -1.0 and answer["true_peak_out_dbtp"] == -1.1


def test_an_unlimited_encode_that_lands_above_minus_one_is_redone_with_the_limiter(tmp_path):
    exporter, fake = _exporter(tmp_path / "out", peaks=Peaks(-0.6, -1.4))
    answer = exporter.export_track(_source(tmp_path, tp=-1.2))
    assert _filters(fake)[0] is None and "alimiter" in _filters(fake)[1]
    assert answer["limited_db"] == -1.0


def test_an_unknown_true_peak_is_measured_on_the_source_first(tmp_path):
    peaks = Peaks(-2.0, source=-4.0)
    exporter, fake = _exporter(tmp_path / "out", peaks=peaks)
    exporter.export_track(_source(tmp_path, tp=None))
    assert peaks.calls[0] == "a.flac" and _filters(fake) == [None]


def test_the_limiter_gives_up_after_the_encode_limit_and_says_so(tmp_path):
    exporter, fake = _exporter(tmp_path / "out", peaks=Peaks(0.5))
    exporter.export_track(_source(tmp_path, tp=2.0))
    assert len(fake.encodes) == ex.MAX_ENCODES
    record = json.loads(read_id3_txxx(next((tmp_path / "out").glob("*.mp3")))["DPC_PROVENANCE"])
    limiter = record["dpc_postprocess"]["limiter"]
    assert limiter["applied"] is True and limiter["reached"] is False and limiter["encodes"] == ex.MAX_ENCODES


# -- provenance --------------------------------------------------------------------------------


def test_the_mp3_carries_its_tags_and_the_post_step_with_the_command_as_sent_and_no_local_path(tmp_path):
    sidecar = {"schema": "dpc.take-sidecar/1", "job_id": "job_1", "seed": 42}
    src = _source(tmp_path, lyrics="first line\nвторая строка", lyrics_language="rus", take_record=sidecar,
                  origin="generated")
    exporter, fake = _exporter(tmp_path / "out")
    path = Path(exporter.export_track(src)["path"])
    raw = path.read_bytes()
    assert raw[:4] == b"ID3\x04"
    for frame in (b"TIT2", b"TPE1", b"USLT", b"TSSE"):
        assert frame in raw[:4096]
    assert "вторая строка".encode("utf-8") in raw and b"rus" in raw
    tags = read_id3_txxx(path)
    assert tags["DPC_EXPORT_OF"] == src.content_hash and tags["DPC_EXPORT_PRESET"] == "mp3-v0"
    record = json.loads(tags["DPC_PROVENANCE"])
    assert record["schema"] == "dpc.export-provenance/1"
    assert record["export_of"]["content_hash"] == src.content_hash
    assert record["export_of"]["sha256"].startswith("sha256:") and record["export_of"]["file"] == "a.flac"
    assert record["take"] == sidecar
    post = record["dpc_postprocess"]
    assert post["encoder"] == "libmp3lame" and post["preset"] == "mp3-v0" and post["tool_version"] == "ffmpeg version 9.9-test"
    cmd = post["cmd_as_sent"]
    sent = fake.encodes[-1]
    assert len(cmd) == len(sent) and cmd[0] == "ffmpeg" and cmd[cmd.index("-i") + 1] == "a.flac"
    assert cmd[-1] == "Band - Song.mp3.audio.part"
    assert [a for a, b in zip(cmd, sent) if a != b] == ["a.flac", "Band - Song.mp3.audio.part"]
    assert str(tmp_path) not in tags["DPC_PROVENANCE"]
    # the tag sits in front of the audio bytes the encoder wrote, unchanged
    assert raw.endswith(b"\xff\xfb\x90\x00" + b"audio" * 50)


def test_the_take_sidecar_is_read_only_when_it_is_one(tmp_path):
    flac = tmp_path / "take_v1.flac"
    flac.write_bytes(b"x")
    assert ex.read_take_sidecar(flac) is None
    (tmp_path / "take_v1.json").write_text(json.dumps({"schema": "other"}), encoding="utf-8")
    assert ex.read_take_sidecar(flac) is None
    (tmp_path / "take_v1.json").write_text(json.dumps({"schema": "dpc.take-sidecar/1", "seed": 1}), encoding="utf-8")
    assert ex.read_take_sidecar(flac)["seed"] == 1


# -- playlists -----------------------------------------------------------------------------------


def test_a_playlist_is_one_folder_of_numbered_links_and_an_exported_track_is_not_encoded_again(tmp_path):
    root = tmp_path / "out"
    exporter, fake = _exporter(root)
    a = _source(tmp_path, "a.flac", content_hash="md5:aaaa", title="First")
    b = _source(tmp_path, "b.flac", content_hash="md5:bbbb", title="Second", artist=None)
    exporter.export_track(a)
    assert len(fake.encodes) == 1
    items = [a, SkippedTrack("trk_x", "Gone", "missing", "no_file"), b]
    answer = exporter.export_playlist("pl_0123456789abcdef", "Road: trip", items)
    assert len(fake.encodes) == 2  # only b was encoded
    folder = root / "Road - trip"
    assert answer["folder"] == str(folder)
    assert sorted(p.name for p in folder.iterdir()) == ["01 - Band - First.mp3", "02 - Second.mp3"]
    statuses = [(t["track_id"], t["status"]) for t in answer["tracks"]]
    assert statuses == [("trk_a", "ok"), ("trk_x", "skipped"), ("trk_b", "ok")]
    assert answer["tracks"][0]["already_exported"] is True and answer["tracks"][2]["already_exported"] is False
    assert answer["tracks"][0]["placed"] in ("hardlink", "copy")
    assert (folder / "01 - Band - First.mp3").read_bytes() == (root / "Band - First.mp3").read_bytes()
    # only the playlist level exists under the root
    assert sorted(p.name for p in root.iterdir() if p.is_dir()) == ["Road - trip"]

    again = exporter.export_playlist("pl_0123456789abcdef", "Road: trip", items)
    assert len(fake.encodes) == 2 and {t.get("placed") for t in again["tracks"] if t["status"] == "ok"} == {"unchanged"}


def test_a_reordered_or_renamed_playlist_replaces_its_last_run_and_keeps_the_persons_files(tmp_path):
    root = tmp_path / "out"
    exporter, fake = _exporter(root)
    a = _source(tmp_path, "a.flac", content_hash="md5:aaaa", title="First")
    b = _source(tmp_path, "b.flac", content_hash="md5:bbbb", title="Second")
    exporter.export_playlist("pl_1", "Mix", [a, b])
    (root / "Mix" / "notes.txt").write_text("mine", encoding="utf-8")
    answer = exporter.export_playlist("pl_1", "Mix", [b, a])
    assert sorted(p.name for p in (root / "Mix").iterdir()) == [
        "01 - Band - Second.mp3", "02 - Band - First.mp3", "notes.txt"]
    assert sorted(answer["removed"]) == ["01 - Band - First.mp3", "02 - Band - Second.mp3"]
    renamed = exporter.export_playlist("pl_1", "Evening mix", [b, a])
    assert not (root / "Mix").exists() and renamed["folder"] == str(root / "Evening mix")
    assert (root / "Evening mix" / "notes.txt").is_file()
    assert len(fake.encodes) == 2
    # a rename that changes only the case renames the one folder (NTFS, APFS, ext4 alike)
    cased = exporter.export_playlist("pl_1", "Evening Mix", [b, a])
    assert cased["folder"] == str(root / "Evening Mix")
    assert [p.name for p in root.iterdir() if p.is_dir()] == ["Evening Mix"]
    exporter.export_playlist("pl_1", "Evening mix", [b, a])
    # a different playlist with the same name gets its own folder
    other = exporter.export_playlist("pl_2bcdef", "Evening mix", [a])
    assert other["folder"] == str(root / "Evening mix (2bcdef)")


def _broken(exporter):
    exporter._tools = MediaTools(ffmpeg=None, ffprobe=None)


def test_a_run_where_every_track_fails_prunes_nothing_and_keeps_the_earlier_files_owned(tmp_path):
    root = tmp_path / "out"
    exporter, _ = _exporter(root)
    a = _source(tmp_path, "a.flac", content_hash="md5:aaaa", title="First")
    exporter.export_playlist("pl_1", "Mix", [a])
    exporter.export_playlist("pl_1", "Mix", [a])
    first = root / "Mix" / "01 - Band - First.mp3"
    assert first.is_file()
    # no ffmpeg and a new track: every track of this run fails
    _broken(exporter)
    c = _source(tmp_path, "c.flac", content_hash="md5:cccc", title="Third")
    answer = exporter.export_playlist("pl_1", "Mix", [c])
    assert answer["tracks"][0]["status"] == "error" and answer["removed"] == []
    assert first.is_file()
    # the record still owns the earlier file, so a later clean run prunes it
    exporter2, _ = _exporter(root)
    later = exporter2.export_playlist("pl_1", "Mix", [c])
    assert later["removed"] == ["01 - Band - First.mp3"] and not first.exists()


def test_a_partly_failed_run_prunes_nothing_and_a_clean_run_after_it_does(tmp_path):
    root = tmp_path / "out"
    exporter, _ = _exporter(root)
    a = _source(tmp_path, "a.flac", content_hash="md5:aaaa", title="First")
    b = _source(tmp_path, "b.flac", content_hash="md5:bbbb", title="Second")
    exporter.export_playlist("pl_1", "Mix", [a, b])
    folder = root / "Mix"
    # b leaves the playlist, a stays, a new track fails to encode
    exporter, _ = _exporter(root, FakeFfmpeg(rc=1))
    c = _source(tmp_path, "c.flac", content_hash="md5:cccc", title="Third")
    answer = exporter.export_playlist("pl_1", "Mix", [a, c])
    assert [t["status"] for t in answer["tracks"]] == ["ok", "error"]
    assert answer["removed"] == [] and answer["kept"] == []
    assert sorted(p.name for p in folder.iterdir()) == ["01 - Band - First.mp3", "02 - Band - Second.mp3"]
    # the clean run prunes b, which is no longer in the playlist
    exporter, _ = _exporter(root)
    clean = exporter.export_playlist("pl_1", "Mix", [a, c])
    assert clean["removed"] == ["02 - Band - Second.mp3"]
    assert sorted(p.name for p in folder.iterdir()) == ["01 - Band - First.mp3", "02 - Band - Third.mp3"]


def test_a_run_with_an_unavailable_source_prunes_nothing_and_a_hidden_skip_still_prunes(tmp_path):
    root = tmp_path / "out"
    exporter, _ = _exporter(root)
    a = _source(tmp_path, "a.flac", content_hash="md5:aaaa", title="First")
    b = _source(tmp_path, "b.flac", content_hash="md5:bbbb", title="Second")
    exporter.export_playlist("pl_1", "Mix", [a, b])
    folder = root / "Mix"
    both = ["01 - Band - First.mp3", "02 - Band - Second.mp3"]
    # every item skipped (a library drive offline): the playlist is refused, nothing is touched
    with pytest.raises(ExportError):
        exporter.export_playlist("pl_1", "Mix", [SkippedTrack("trk_a", None, "not in the library", "unknown_track"),
                                                 SkippedTrack("trk_b", None, "not in the library", "unknown_track")])
    assert sorted(p.name for p in folder.iterdir()) == both
    # a lost library entry next to a track that still exports: nothing is pruned, files stay owned
    answer = exporter.export_playlist("pl_1", "Mix", [a, SkippedTrack("trk_b", None, "not in the library",
                                                                      "unknown_track")])
    assert answer["removed"] == [] and sorted(p.name for p in folder.iterdir()) == both
    # a source ExportError raised while building the source is a skip that blocks the prune too
    answer = exporter.export_playlist("pl_1", "Mix", [a, SkippedTrack("trk_b", "Second", "file missing",
                                                                      "source_missing")])
    assert answer["removed"] == [] and sorted(p.name for p in folder.iterdir()) == both
    # only a hidden skip: the stale export of b is pruned as before
    answer = exporter.export_playlist("pl_1", "Mix", [a, SkippedTrack("trk_b", "Second", "hidden", "hidden")])
    assert answer["removed"] == ["02 - Band - Second.mp3"]
    assert [p.name for p in folder.iterdir()] == ["01 - Band - First.mp3"]


def test_a_playlist_rename_with_blocked_prune_updates_surviving_file_paths(tmp_path):
    root = tmp_path / "out"
    exporter, _ = _exporter(root)
    a = _source(tmp_path, "a.flac", content_hash="md5:aaaa", title="First")
    b = _source(tmp_path, "b.flac", content_hash="md5:bbbb", title="Second")
    exporter.export_playlist("pl_1", "Mix", [a, b])

    # Rename the playlist to "Evening mix" while b is unavailable so prune is blocked
    # and the earlier "02 - Band - Second.mp3" stays in the renamed folder.
    answer = exporter.export_playlist(
        "pl_1", "Evening mix",
        [a, SkippedTrack("trk_b", "Second", "not in the library", "unknown_track")],
    )
    assert answer["removed"] == []
    surviving = root / "Evening mix" / "02 - Band - Second.mp3"
    assert surviving.is_file()

    index_data = json.loads((root / ".dpc-exports.json").read_text(encoding="utf-8"))
    pl_record = next(p for p in index_data["playlists"] if p["playlist_id"] == "pl_1")
    assert pl_record["folder"] == "Evening mix"
    file_paths = [f["path"] for f in pl_record["files"]]
    assert "Evening mix/02 - Band - Second.mp3" in file_paths
    assert all(not p.startswith("Mix/") for p in file_paths)

    info = exporter.exports_for({"md5:bbbb": ["trk_b"]})
    assert [c["folder"] for c in info["trk_b"][0]["playlists"]] == ["Evening mix"]
    assert exporter.is_export_path(surviving) is not None
    assert exporter.is_export_path(root / "Mix" / "02 - Band - Second.mp3") is None

    # A subsequent clean run still prunes the stale file cleanly.
    clean = exporter.export_playlist("pl_1", "Evening mix", [a])
    assert clean["removed"] == ["02 - Band - Second.mp3"]
    assert not surviving.exists()


def test_where_a_hard_link_is_refused_the_playlist_file_is_copied(tmp_path, monkeypatch):
    root = tmp_path / "out"
    exporter, _ = _exporter(root)
    a = _source(tmp_path, "a.flac", content_hash="md5:aaaa", title="First")
    exporter.export_track(a)

    def refuse(src, dst):
        raise OSError(18, "Invalid cross-device link")
    monkeypatch.setattr(ex.os, "link", refuse)
    answer = exporter.export_playlist("pl_1", "Mix", [a])
    assert answer["tracks"][0]["placed"] == "copy"
    copied = root / "Mix" / "01 - Band - First.mp3"
    assert copied.read_bytes() == (root / "Band - First.mp3").read_bytes()
    assert not os.path.samefile(copied, root / "Band - First.mp3")
    assert not list((root / "Mix").glob("*.part"))
    assert exporter.export_playlist("pl_1", "Mix", [a])["tracks"][0]["placed"] == "unchanged"


def test_a_playlist_with_nothing_to_export_is_refused(tmp_path):
    exporter, _ = _exporter(tmp_path / "out")
    with pytest.raises(ExportError) as e:
        exporter.export_playlist("pl_1", "Empty", [SkippedTrack("trk_1", "x", "hidden", "hidden")])
    assert e.value.code == "empty_playlist"


def test_the_index_lists_its_files_for_the_scanner_and_reports_what_exists(tmp_path):
    root = tmp_path / "out"
    exporter, _ = _exporter(root)
    a = _source(tmp_path, "a.flac", content_hash="md5:aaaa", title="First")
    exporter.export_playlist("pl_1", "Mix", [a])
    assert exporter.is_export_path(str(root / "Band - First.mp3"))
    assert exporter.is_export_path(str(root / "Mix" / "01 - Band - First.mp3"))
    assert exporter.is_export_path(str(tmp_path / "lib" / "a.flac")) is None
    fresh, _ = _exporter(root)
    assert fresh.is_export_path(str(root / "Band - First.mp3"))  # read from the index file
    info = fresh.exports_for({"md5:aaaa": ["trk_a"], "md5:zzzz": ["trk_z"]})
    assert list(info) == ["trk_a"] and info["trk_a"][0]["name"] == "Band - First.mp3"
    (root / "Band - First.mp3").unlink()
    assert fresh.exports_for({"md5:aaaa": ["trk_a"]}) == {}


def test_a_corrupt_index_is_moved_aside_not_trusted(tmp_path):
    root = tmp_path / "out"
    root.mkdir()
    (root / ".dpc-exports.json").write_text("{not json", encoding="utf-8")
    exporter, fake = _exporter(root)
    exporter.export_track(_source(tmp_path))
    assert len(fake.encodes) == 1
    assert any(p.name.startswith(".dpc-exports.json.corrupt-") for p in root.iterdir())


# -- real ffmpeg -----------------------------------------------------------------------------------


def _make_flac(path: Path, lavfi: str):
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i", lavfi, "-ac", "2",
                    "-c:a", "flac", "-sample_fmt", "s32", str(path)], check=True)


@pytest.mark.skipif(not HAVE_LAME, reason="ffmpeg with libmp3lame is not installed")
def test_a_real_hot_take_comes_out_at_or_below_minus_one_dbtp_and_a_quiet_one_is_not_limited(tmp_path):
    from dpc_client_core.music.loudness import measure_loudness

    hot = tmp_path / "hot.flac"
    quiet = tmp_path / "quiet.flac"
    _make_flac(hot, "anoisesrc=color=pink:amplitude=1:duration=4:sample_rate=48000,volume=14dB")
    _make_flac(quiet, "sine=frequency=440:duration=3:sample_rate=44100,volume=-12dB")
    assert measure_loudness(hot)["true_peak_dbtp"] > -1.0
    exporter = MusicExporter(tmp_path / "out")
    loud = exporter.export_track(ExportSource("trk_hot", hot, "md5:hot1", "Hot", "DPC", lyrics="la la"))
    soft = exporter.export_track(ExportSource("trk_quiet", quiet, "md5:quiet1", "Quiet", None))
    assert loud["limited_db"] == -1.0 and soft["limited_db"] is None
    assert measure_loudness(Path(loud["path"]))["true_peak_dbtp"] <= -1.0
    assert measure_loudness(Path(soft["path"]))["true_peak_dbtp"] < -6.0
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-of", "json", loud["path"]],
                           capture_output=True, text=True, encoding="utf-8", check=True)
    tags = json.loads(probe.stdout)["format"]["tags"]
    assert tags["title"] == "Hot" and tags["artist"] == "DPC" and tags["DPC_EXPORT_OF"] == "md5:hot1"
    assert any(k.startswith("lyrics") and v == "la la" for k, v in tags.items())
    assert exporter.export_track(ExportSource("trk_hot", hot, "md5:hot1", "Hot", "DPC"))["already_exported"]
    assert not list((tmp_path / "out").glob("*.part"))
