"""`MusicExporter.delete_export` and the `delete_music_export` command (player item 3, Mike
#75): the file and its index entry go together, and nothing outside the export root or outside
the index is ever removed, whatever path, traversal or symlink the caller or a hand-edited
index hands in."""

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from dpc_client_core.music.commands import MUSIC_COMMANDS, MusicCommandsMixin
from dpc_client_core.music.export import (
    INDEX_FILE, INDEX_SCHEMA, ExportError, MusicExporter, export_id_of,
)

HASH = "md5:" + "ab" * 16
OTHER = "md5:" + "cd" * 16


def _record(path, source_hash=HASH, preset="mp3-v0", track_id="trk_a"):
    return {"source_hash": source_hash, "format": "mp3", "preset": preset, "path": path,
            "sha256": "sha256:" + "0" * 64, "size_bytes": 12, "exported_at": "2026-11-24T10:00:00Z",
            "track_id": track_id, "title": "Song", "artist": "Band", "limiter": None, "true_peak_out_dbtp": None}


def _root(tmp_path, *records, files=("Band - Song.mp3",)):
    root = tmp_path / "exports"
    root.mkdir()
    for name in files:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"mp3 bytes")
    (root / INDEX_FILE).write_text(json.dumps(
        {"schema": INDEX_SCHEMA, "exports": list(records), "playlists": []}), encoding="utf-8")
    return root


def _indexed(root):
    return [e["path"] for e in json.loads((root / INDEX_FILE).read_text(encoding="utf-8"))["exports"]]


def _outside(tmp_path):
    victim = tmp_path / "outside.mp3"
    victim.write_bytes(b"not an export")
    return victim


def test_delete_by_id_removes_the_file_and_its_index_entry(tmp_path):
    root = _root(tmp_path, _record("Band - Song.mp3"), _record("Band - Song (320 kbps).mp3", preset="mp3-320"),
                 files=("Band - Song.mp3", "Band - Song (320 kbps).mp3"))
    exporter = MusicExporter(root)
    listed = exporter.exports_for({HASH: ["trk_a"]})["trk_a"]
    assert {e["preset"] for e in listed} == {"mp3-v0", "mp3-320"}
    assert all(e["size_bytes"] == len(b"mp3 bytes") for e in listed)
    target = next(e for e in listed if e["preset"] == "mp3-v0")
    assert target["export_id"] == export_id_of(HASH, "mp3", "mp3-v0")

    answer = exporter.delete_export(export_id=target["export_id"])

    assert answer["deleted"] is True and answer["file_missing"] is False
    assert not (root / "Band - Song.mp3").exists()
    assert (root / "Band - Song (320 kbps).mp3").exists()
    assert _indexed(root) == ["Band - Song (320 kbps).mp3"]
    assert [e["preset"] for e in MusicExporter(root).exports_for({HASH: ["trk_a"]})["trk_a"]] == ["mp3-320"]


@pytest.mark.parametrize("relative", [True, False])
def test_delete_by_path_takes_an_absolute_or_a_root_relative_path(tmp_path, relative):
    root = _root(tmp_path, _record("Band - Song.mp3"))
    exporter = MusicExporter(root)
    exporter.delete_export(path="Band - Song.mp3" if relative else str(root / "Band - Song.mp3"))
    assert not (root / "Band - Song.mp3").exists() and _indexed(root) == []


def test_exactly_one_of_export_id_and_path_is_required(tmp_path):
    root = _root(tmp_path, _record("Band - Song.mp3"))
    exporter = MusicExporter(root)
    for kwargs in ({}, {"export_id": "x", "path": "y"}):
        with pytest.raises(ExportError) as caught:
            exporter.delete_export(**kwargs)
        assert caught.value.code == "invalid_params"
    assert (root / "Band - Song.mp3").exists()


def test_a_path_outside_the_root_is_refused_and_the_file_survives(tmp_path):
    root = _root(tmp_path, _record("Band - Song.mp3"))
    victim = _outside(tmp_path)
    exporter = MusicExporter(root)
    with pytest.raises(ExportError) as caught:
        exporter.delete_export(path=str(victim))
    assert caught.value.code == "outside_export_root"
    assert victim.exists() and (root / "Band - Song.mp3").exists() and _indexed(root) == ["Band - Song.mp3"]


def test_a_traversal_in_the_path_argument_is_refused(tmp_path):
    root = _root(tmp_path, _record("Band - Song.mp3"))
    victim = _outside(tmp_path)
    exporter = MusicExporter(root)
    for raw in (str(root / ".." / "outside.mp3"), "../outside.mp3", "sub/../../outside.mp3"):
        with pytest.raises(ExportError) as caught:
            exporter.delete_export(path=raw)
        assert caught.value.code == "outside_export_root", raw
    assert victim.exists()


def test_a_hand_edited_index_path_that_climbs_out_of_the_root_is_refused(tmp_path):
    victim = _outside(tmp_path)
    root = _root(tmp_path, _record("../outside.mp3"), _record("sub/../../outside.mp3", source_hash=OTHER))
    exporter = MusicExporter(root)
    for source in (HASH, OTHER):
        with pytest.raises(ExportError) as caught:
            exporter.delete_export(export_id=export_id_of(source, "mp3", "mp3-v0"))
        assert caught.value.code == "outside_export_root"
    assert victim.exists() and len(_indexed(root)) == 2


def test_a_folder_symlinked_out_of_the_root_is_not_followed(tmp_path):
    outside_dir = tmp_path / "elsewhere"
    outside_dir.mkdir()
    victim = outside_dir / "song.mp3"
    victim.write_bytes(b"not an export")
    root = _root(tmp_path, _record("link/song.mp3"), files=())
    try:
        os.symlink(outside_dir, root / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        junction = subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "link"), str(outside_dir)],
                                  capture_output=True) if os.name == "nt" else None
        if junction is None or junction.returncode != 0:
            pytest.skip("this account cannot create symlinks or junctions")
    exporter = MusicExporter(root)
    with pytest.raises(ExportError) as by_id:
        exporter.delete_export(export_id=export_id_of(HASH, "mp3", "mp3-v0"))
    with pytest.raises(ExportError) as by_path:
        exporter.delete_export(path=str(root / "link" / "song.mp3"))
    assert by_id.value.code == "outside_export_root" and by_path.value.code == "outside_export_root"
    assert victim.exists()


def test_a_file_the_index_does_not_list_is_never_deleted(tmp_path):
    root = _root(tmp_path, _record("Band - Song.mp3"), files=("Band - Song.mp3", "my own mix.mp3"))
    exporter = MusicExporter(root)
    with pytest.raises(ExportError) as caught:
        exporter.delete_export(path="my own mix.mp3")
    assert caught.value.code == "unknown_export"
    with pytest.raises(ExportError) as unknown_id:
        exporter.delete_export(export_id="0" * 20)
    assert unknown_id.value.code == "unknown_export"
    assert (root / "my own mix.mp3").exists()


def test_a_file_deleted_by_hand_leaves_the_listing_and_its_entry_is_dropped_on_delete(tmp_path):
    root = _root(tmp_path, _record("Band - Song.mp3"))
    exporter = MusicExporter(root)
    (root / "Band - Song.mp3").unlink()
    assert exporter.exports_for({HASH: ["trk_a"]}) == {}
    assert _indexed(root) == ["Band - Song.mp3"]

    answer = exporter.delete_export(export_id=export_id_of(HASH, "mp3", "mp3-v0"))

    assert answer["deleted"] is False and answer["file_missing"] is True
    assert _indexed(root) == []


def test_a_file_that_cannot_be_removed_keeps_its_entry_and_says_why(tmp_path, monkeypatch):
    root = _root(tmp_path, _record("Band - Song.mp3"))
    exporter = MusicExporter(root)
    real_unlink = os.unlink

    def refuse(path, *args, **kwargs):
        if Path(path).name == "Band - Song.mp3":
            raise PermissionError(13, "The process cannot access the file because it is being used")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", refuse)
    with pytest.raises(ExportError) as caught:
        exporter.delete_export(export_id=export_id_of(HASH, "mp3", "mp3-v0"))
    assert caught.value.code == ("file_in_use" if os.name == "nt" else "permission_denied")
    assert "Band - Song.mp3" in caught.value.message
    assert (root / "Band - Song.mp3").exists() and _indexed(root) == ["Band - Song.mp3"]


def test_a_folder_named_like_an_export_is_not_removed(tmp_path):
    root = _root(tmp_path, _record("folder.mp3"), files=())
    (root / "folder.mp3").mkdir()
    with pytest.raises(ExportError) as caught:
        MusicExporter(root).delete_export(path="folder.mp3")
    assert caught.value.code == "delete_failed" and (root / "folder.mp3").is_dir()


# -- the command ---------------------------------------------------------------------------


class _Settings:
    def __init__(self, home, export_dir):
        self.dpc_home_dir = home
        self._export_dir = export_dir

    def get(self, section, key, fallback=None):
        if (section, key) == ("music", "export_dir"):
            return str(self._export_dir)
        if (section, key) == ("music", "poll_interval_s"):
            return "60"
        return fallback


class _Service(MusicCommandsMixin):
    def __init__(self, home, export_dir):
        self.settings = _Settings(home, export_dir)
        self.sent = []

    def _music_broadcast(self, event, payload):
        self.sent.append((event, payload))


def test_the_command_is_registered_where_commands_are_registered():
    from dpc_client_core.local_api import ALLOWED_COMMANDS
    assert "delete_music_export" in MUSIC_COMMANDS and "delete_music_export" in ALLOWED_COMMANDS


@pytest.mark.asyncio
async def test_the_command_deletes_answers_ok_and_never_raises_on_a_refusal(tmp_path):
    root = _root(tmp_path, _record("Band - Song.mp3"))
    victim = _outside(tmp_path)
    service = _Service(tmp_path / "dpc", root)
    try:
        refused = await service.delete_music_export(path=str(victim))
        assert refused["status"] == "error" and refused["code"] == "outside_export_root" and victim.exists()
        missing_arg = await service.delete_music_export()
        assert missing_arg["status"] == "error" and missing_arg["code"] == "invalid_params"

        done = await service.delete_music_export(export_id=export_id_of(HASH, "mp3", "mp3-v0"))
        assert done["status"] == "ok" and done["deleted"] is True and done["name"] == "Band - Song.mp3"
        assert not (root / "Band - Song.mp3").exists() and _indexed(root) == []
        await asyncio.sleep(0.8)
        announced = [p for e, p in service.sent if e == "music_library_changed"]
        assert len(announced) == 1 and announced[0]["reasons"] == ["export"] and announced[0]["track_ids"] == ["trk_a"]
    finally:
        await service.stop_music_library()
