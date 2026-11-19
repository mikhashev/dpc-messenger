"""Follow-ups to the music library's folder checks and saves:

- a watched root replaced after `add_folder` (a symlink into the DPC home) is refused
  on the next scan, marked `unavailable` with the code, and nothing is marked missing;
- a drive whose type `GetDriveTypeW` cannot read is refused, not let through;
- a hand-edited `folders.json` with more than `MAX_FOLDERS` folders keeps the first
  `MAX_FOLDERS` by `added_at` active and the rest `unavailable`, kept, not scanned;
- missing entries whose hash a present entry holds are merged into it, and their
  playlist positions move to it;
- `save_now()` writes under the write lock, and an older snapshot never lands after it.
"""

import asyncio
import json
import os
import shutil
import sys
import threading
import time
import types
from pathlib import Path, PureWindowsPath

import pytest

from dpc_client_core.music import library as library_module
from dpc_client_core.music import schemas
from dpc_client_core.music.identity import MediaTools
from dpc_client_core.music.library import MAX_FOLDERS, MusicLibrary, MusicLibraryError


def _library(home, **kwargs):
    return MusicLibrary(home, tools=MediaTools(ffmpeg=None, ffprobe=None), save_delay_s=0.01, **kwargs)


def _link_dir(link: Path, target: Path) -> str:
    """Make `link` point at the folder `target`: a symlink, or on Windows without the
    symlink privilege a junction (no privilege needed). Skips when neither works."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return "symlink"
    except (OSError, NotImplementedError) as exc:
        if os.name != "nt":
            pytest.skip(f"cannot create a directory symlink here: {exc}")
    try:
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
        return "junction"
    except (OSError, ImportError, AttributeError) as exc:
        pytest.skip(f"a directory symlink needs a privilege on Windows and a junction failed: {exc}")


def _unlink_dir(link: Path) -> None:
    """Remove the link itself, never what it points at."""
    if os.path.islink(link) or getattr(os.path, "isjunction", lambda _p: False)(link):
        try:
            os.unlink(link)
        except OSError:
            os.rmdir(link)  # a directory symlink or a junction on Windows


# -- 1. the root is checked again before every scan ---------------------------------


@pytest.mark.asyncio
async def test_a_root_replaced_by_a_link_to_the_dpc_home_is_refused_on_rescan(tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    (music / "song.mp3").write_bytes(os.urandom(600))
    dpc = tmp_path / "dpc"
    lib = _library(dpc)
    await lib.open()
    try:
        folder = await lib.add_folder(str(music), scan=False)
        await lib.rescan_folder(folder["id"])
        await lib.wait_hashed()
        [entry] = lib.list_entries()
        assert entry["hash_state"] == "done" and entry["missing"] is False

        shutil.rmtree(music)
        (dpc / "secret.mp3").write_bytes(os.urandom(600))  # must never be indexed
        _link_dir(music, dpc)

        after = await lib.rescan_folder(folder["id"])
        entries = lib.list_entries()
    finally:
        await lib.stop()
        _unlink_dir(music)
    assert after["watch_state"] == "unavailable"
    assert "protected_folder" in after["state_reason"]
    assert [e["id"] for e in entries] == [entry["id"]]
    assert entries[0]["missing"] is False


@pytest.mark.asyncio
async def test_polling_refuses_a_folder_that_now_fails_the_checks(tmp_path, monkeypatch):
    music = tmp_path / "Music"
    music.mkdir()
    (music / "song.mp3").write_bytes(os.urandom(600))
    lib = _library(tmp_path / "dpc", poll_interval_s=0.02)
    await lib.open()
    try:
        folder = await lib.add_folder(str(music), scan=False)
        await lib.rescan_folder(folder["id"])
        await lib.wait_hashed()

        def refuse(_path):
            raise MusicLibraryError("network_path", "now a network drive")

        monkeypatch.setattr(library_module, "_is_network_drive", refuse)
        (music / "song.mp3").unlink()  # a scan would mark it missing; a refused one must not
        await lib.start()
        for _ in range(200):
            if lib.list_folders()[0]["watch_state"] == "unavailable":
                break
            await asyncio.sleep(0.02)
        after = lib.list_folders()[0]
        entries = lib.list_entries()
    finally:
        await lib.stop()
    assert after["watch_state"] == "unavailable"
    assert "network_path" in after["state_reason"]
    assert [e["missing"] for e in entries] == [False]


# -- 2. GetDriveTypeW fails closed ---------------------------------------------------


def test_a_drive_whose_type_cannot_be_read_is_refused(monkeypatch):
    def broken(_root):
        raise OSError("GetDriveTypeW is not reachable")

    fake_ctypes = types.SimpleNamespace(
        windll=types.SimpleNamespace(kernel32=types.SimpleNamespace(GetDriveTypeW=broken)))
    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)
    with monkeypatch.context() as m:
        if os.name != "nt":
            m.setattr(library_module.os, "name", "nt")
        with pytest.raises(MusicLibraryError) as err:
            library_module._is_network_drive(PureWindowsPath("Z:/Music"))
    assert err.value.code == "network_path"
    assert "Z:" in err.value.message


@pytest.mark.skipif(os.name != "nt", reason="GetDriveTypeW is called on Windows only")
@pytest.mark.asyncio
async def test_add_folder_refuses_when_get_drive_type_raises(tmp_path, monkeypatch):
    import ctypes

    def broken(_root):
        raise OSError("GetDriveTypeW is not reachable")

    music = tmp_path / "Music"
    music.mkdir()
    lib = _library(tmp_path / "dpc")
    await lib.open()
    monkeypatch.setattr(ctypes.windll, "kernel32", types.SimpleNamespace(GetDriveTypeW=broken))
    try:
        with pytest.raises(MusicLibraryError) as err:
            await lib.add_folder(str(music), scan=False)
    finally:
        monkeypatch.undo()
        await lib.stop()
    assert err.value.code == "network_path"
    assert lib.list_folders() == []


# -- 3. MAX_FOLDERS on load ----------------------------------------------------------


def _folder_record(path: Path, i: int) -> dict:
    return {
        "schema": schemas.MUSIC_FOLDER, "id": f"mf_{i:016x}", "path": str(path),
        "added_at": f"2026-01-01T00:00:{i:02d}.000Z", "last_scan_at": None, "watch": True,
        "file_count": 0, "hashed_count": 0, "watch_state": "polling", "state_reason": None,
    }


@pytest.mark.asyncio
async def test_forty_folders_on_load_leave_thirty_two_active_and_eight_unavailable(tmp_path):
    dpc = tmp_path / "dpc"
    records = []
    for i in range(40):
        folder = tmp_path / f"M{i:02d}"
        folder.mkdir()
        records.append(_folder_record(folder, i))
    lib = _library(dpc)
    folders_file = lib.folders_path
    folders_file.parent.mkdir(parents=True, exist_ok=True)
    folders_file.write_text(json.dumps(list(reversed(records))), encoding="utf-8")

    await lib.open()
    try:
        await lib.rescan_all()
        folders = {f["id"]: f for f in lib.list_folders()}
    finally:
        await lib.stop()

    first, rest = {r["id"] for r in records[:MAX_FOLDERS]}, {r["id"] for r in records[MAX_FOLDERS:]}
    active = {i for i, f in folders.items() if f["watch_state"] == "polling"}
    unavailable = {i for i, f in folders.items() if f["watch_state"] == "unavailable"}
    assert (len(folders), len(active), len(unavailable)) == (40, 32, 8)
    assert active == first and unavailable == rest
    assert all(f"limit of {MAX_FOLDERS}" in folders[i]["state_reason"] for i in rest)
    assert all(folders[i]["last_scan_at"] is None for i in rest)
    assert all(folders[i]["last_scan_at"] is not None for i in first)
    saved = json.loads(folders_file.read_text(encoding="utf-8"))
    assert len(saved) == 40


@pytest.mark.asyncio
async def test_removing_an_active_folder_lets_the_next_one_in(tmp_path):
    dpc = tmp_path / "dpc"
    records = []
    for i in range(MAX_FOLDERS + 1):
        folder = tmp_path / f"M{i:02d}"
        folder.mkdir()
        records.append(_folder_record(folder, i))
    lib = _library(dpc)
    lib.folders_path.parent.mkdir(parents=True, exist_ok=True)
    lib.folders_path.write_text(json.dumps(records), encoding="utf-8")
    await lib.open()
    try:
        await lib.remove_folder(records[0]["id"])
        last = await lib.rescan_folder(records[-1]["id"])
    finally:
        await lib.stop()
    assert last["watch_state"] == "polling" and last["last_scan_at"] is not None


# -- 4. missing twins beside a present twin ------------------------------------------


def _hashed(lib, path: str, folder_id: str, digest: str, *, missing: bool, added_at: str) -> dict:
    e = lib._new_folder_entry(path, folder_id, 100, 1_700_000_000_000_000_000)
    e.update({"hash_state": "done", "hash_kind": "file", "content_hash": digest, "source_hash": digest,
              "missing": missing, "added_at": added_at})
    return e


def _playlist(pl_id: str, track_ids):
    return {"schema": schemas.PLAYLIST, "id": pl_id, "name": pl_id, "track_ids": list(track_ids),
            "created_at": "2026-01-01T00:00:00.000Z", "updated_at": "2026-01-01T00:00:00.000Z", "kind": "user"}


@pytest.mark.asyncio
async def test_missing_twins_on_load_merge_into_the_present_twin_and_playlists_move(tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    dpc = tmp_path / "dpc"
    first = _library(dpc)
    first.load()
    fid = "mf_0000000000000001"
    first._folders[fid] = _folder_record(music, 1)
    present = _hashed(first, str(music / "now.mp3"), fid, "aa" * 32, missing=False, added_at="2026-01-02T00:00:00.000Z")
    gone1 = _hashed(first, str(music / "old1.mp3"), fid, "aa" * 32, missing=True, added_at="2026-01-01T00:00:00.000Z")
    gone2 = _hashed(first, str(music / "old2.mp3"), fid, "aa" * 32, missing=True, added_at="2026-01-03T00:00:00.000Z")
    other = _hashed(first, str(music / "other.mp3"), fid, "bb" * 32, missing=False, added_at="2026-01-01T00:00:00.000Z")
    first._playlists["pl_a"] = _playlist("pl_a", [gone1["id"], other["id"], present["id"]])
    first._playlists["pl_b"] = _playlist("pl_b", [gone2["id"], other["id"]])
    first.save_now()

    lib = _library(dpc)
    await lib.open()
    try:
        ids = {e["id"] for e in lib.list_entries()}
        playlists = {p["id"]: p["track_ids"] for p in lib.list_playlists() if p["kind"] == "user"}
        await lib.flush()
    finally:
        await lib.stop()
    assert ids == {present["id"], other["id"]}
    assert playlists["pl_a"] == [other["id"], present["id"]]  # deduped: it already held the twin
    assert playlists["pl_b"] == [present["id"], other["id"]]  # moved in place
    reloaded = _library(dpc)
    reloaded.load()
    assert set(reloaded._entries) == {present["id"], other["id"]}


@pytest.mark.asyncio
async def test_a_new_duplicate_merges_the_missing_twins_of_its_present_twin(tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    audio = os.urandom(800)
    (music / "a.mp3").write_bytes(audio)
    lib = _library(tmp_path / "dpc")
    await lib.open()
    try:
        folder = await lib.add_folder(str(music), scan=False)
        await lib.rescan_folder(folder["id"])
        await lib.wait_hashed()
        [present] = lib.list_entries()
        ghost = _hashed(lib, str(tmp_path / "elsewhere" / "a.mp3"), folder["id"], present["content_hash"],
                        missing=True, added_at="2020-01-01T00:00:00.000Z")
        ghost["hash_kind"] = present["hash_kind"]
        lib._index_add(ghost)
        pl = await lib.create_playlist("mix", [ghost["id"]])

        (music / "b.mp3").write_bytes(audio)
        await lib.rescan_folder(folder["id"])
        await lib.wait_hashed()
        entries = lib.list_entries()
        track_ids = next(p["track_ids"] for p in lib.list_playlists() if p["id"] == pl["id"])
    finally:
        await lib.stop()
    assert [e["id"] for e in entries] == [present["id"]]
    assert [Path(p).name for p in entries[0]["alt_paths"]] == ["b.mp3"]
    assert track_ids == [present["id"]]


# -- 5. save_now under the lock ------------------------------------------------------


def test_save_now_waits_for_the_write_lock(tmp_path):
    lib = _library(tmp_path / "dpc")
    lib.load()
    done = threading.Event()
    lib._disk_lock.acquire()
    try:
        worker = threading.Thread(target=lambda: (lib.save_now(), done.set()))
        worker.start()
        time.sleep(0.2)
        assert not done.is_set()
        assert not lib.library_path.exists()
    finally:
        lib._disk_lock.release()
    worker.join(5)
    assert done.is_set() and lib.library_path.exists()


@pytest.mark.asyncio
async def test_an_older_snapshot_does_not_land_after_save_now(tmp_path):
    lib = _library(tmp_path / "dpc")
    await lib.open()
    try:
        seq, old_texts = lib._take_snapshot()  # a flush's snapshot, its thread not yet writing
        await lib.create_playlist("newer")  # flushes itself
        lib.save_now()
        lib._write_snapshot(seq, old_texts)  # the late thread: skipped
        saved = json.loads(lib.playlists_path.read_text(encoding="utf-8"))
    finally:
        await lib.stop()
    assert [p["name"] for p in saved] == ["newer"]
