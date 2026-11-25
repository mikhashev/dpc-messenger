"""The copies list and Delete of an exported copy say which playlist-folder files hold the same
song (`MusicExporter.exports_for` and `delete_export`; backlog
DELETING-AN-EXPORTED-COPY-DOES-NOT-NAME-THE-PLAYLIST-FOLDERS-THAT-HOLD-A-LINK-TO-IT): each
single export lists the playlist files of its source and preset, says whether each is a hard link
or a copy, and Delete leaves them alone and names the ones that stay."""

import os

from dpc_client_core.music import export as ex
from tests.test_an_mp3_export_is_made_once_per_source_and_preset_with_portable_names import _exporter, _source

A_HASH = "md5:aaaa"


def _two_playlists(tmp_path):
    root = tmp_path / "out"
    exporter, fake = _exporter(root)
    a = _source(tmp_path, "a.flac", content_hash=A_HASH, title="First")
    b = _source(tmp_path, "b.flac", content_hash="md5:bbbb", title="Second")
    exporter.export_playlist("pl_road", "Road trip", [a, b])
    exporter.export_playlist("pl_night", "Night", [b, a])
    return root, exporter, fake, a, b


def test_a_single_export_lists_the_playlist_files_of_the_same_song(tmp_path):
    root, exporter, _, _, _ = _two_playlists(tmp_path)
    listed = exporter.exports_for({A_HASH: ["trk_a"]})["trk_a"]
    assert len(listed) == 1
    copies = {(c["playlist_name"], c["folder"], c["name"]) for c in listed[0]["playlists"]}
    assert copies == {("Road trip", "Road trip", "01 - Band - First.mp3"), ("Night", "Night", "02 - Band - First.mp3")}
    one = next(c for c in listed[0]["playlists"] if c["playlist_id"] == "pl_road")
    assert one["path"] == str(root / "Road trip" / "01 - Band - First.mp3")
    assert one["how"] in ("hardlink", "copy")


def test_a_hard_link_is_told_from_a_copy(tmp_path, monkeypatch):
    root = tmp_path / "out"
    exporter, _ = _exporter(root)
    a = _source(tmp_path, "a.flac", content_hash=A_HASH, title="First")
    exporter.export_track(a)
    exporter.export_playlist("pl_one", "Linked", [a])
    monkeypatch.setattr(ex.os, "link", lambda src, dst: (_ for _ in ()).throw(OSError(18, "cross-device")))
    exporter.export_playlist("pl_two", "Copied", [a])
    monkeypatch.undo()
    by_name = {c["playlist_name"]: c["how"] for c in exporter.exports_for({A_HASH: ["trk_a"]})["trk_a"][0]["playlists"]}
    assert by_name["Copied"] == "copy"
    # Where the filesystem has hard links (every CI and desktop volume but FAT/exFAT), the first is one.
    linked_on_disk = os.path.samefile(root / "Linked" / "01 - Band - First.mp3", root / "Band - First.mp3")
    assert by_name["Linked"] == ("hardlink" if linked_on_disk else "copy")


def test_a_song_in_no_playlist_lists_none_and_another_preset_does_not_count(tmp_path):
    root = tmp_path / "out"
    exporter, _ = _exporter(root)
    a = _source(tmp_path, "a.flac", content_hash=A_HASH, title="First")
    exporter.export_track(a)
    assert exporter.exports_for({A_HASH: ["trk_a"]})["trk_a"][0]["playlists"] == []
    exporter.export_playlist("pl_320", "Loud", [a], "mp3-320")
    listed = {e["preset"]: e for e in exporter.exports_for({A_HASH: ["trk_a"]})["trk_a"]}
    assert listed["mp3-v0"]["playlists"] == []
    assert [c["playlist_name"] for c in listed["mp3-320"]["playlists"]] == ["Loud"]


def test_a_playlist_file_that_is_gone_is_not_listed(tmp_path):
    root, exporter, _, _, _ = _two_playlists(tmp_path)
    (root / "Night" / "02 - Band - First.mp3").unlink()
    names = [c["playlist_name"] for c in exporter.exports_for({A_HASH: ["trk_a"]})["trk_a"][0]["playlists"]]
    assert names == ["Road trip"]


def test_delete_leaves_the_playlist_files_and_names_the_ones_that_stay(tmp_path):
    root, exporter, _, _, _ = _two_playlists(tmp_path)
    export_id = exporter.exports_for({A_HASH: ["trk_a"]})["trk_a"][0]["export_id"]
    answer = exporter.delete_export(export_id=export_id)
    assert answer["deleted"] is True
    assert not (root / "Band - First.mp3").exists()
    assert (root / "Road trip" / "01 - Band - First.mp3").is_file()
    assert (root / "Night" / "02 - Band - First.mp3").is_file()
    assert {(c["playlist_name"], c["name"]) for c in answer["playlist_copies"]} == {
        ("Road trip", "01 - Band - First.mp3"), ("Night", "02 - Band - First.mp3")}
    # the other song's playlist file is untouched
    assert (root / "Road trip" / "02 - Band - Second.mp3").is_file()


def test_delete_of_a_song_in_no_playlist_answers_an_empty_list(tmp_path):
    root = tmp_path / "out"
    exporter, _ = _exporter(root)
    a = _source(tmp_path, "a.flac", content_hash=A_HASH, title="First")
    exporter.export_track(a)
    export_id = exporter.exports_for({A_HASH: ["trk_a"]})["trk_a"][0]["export_id"]
    assert exporter.delete_export(export_id=export_id)["playlist_copies"] == []
