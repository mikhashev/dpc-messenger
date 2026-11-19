"""When the folder root is reachable but a subfolder fails during the walk (a
permission error, a share lost half-way), the folder is `unavailable` for that scan and
none of its tracks is marked missing (SPEC §6: `unavailable` is a folder property).
"""

import os

import pytest

from dpc_client_core.music.identity import MediaTools
from dpc_client_core.music.library import MusicLibrary, path_key


@pytest.mark.asyncio
async def test_a_subfolder_that_cannot_be_listed_leaves_its_tracks_present(tmp_path, monkeypatch):
    music = tmp_path / "Music"
    (music / "Sub").mkdir(parents=True)
    (music / "a.mp3").write_bytes(os.urandom(256))
    (music / "Sub" / "b.mp3").write_bytes(os.urandom(256))
    lib = MusicLibrary(tmp_path / "dpc", tools=MediaTools(ffmpeg=None, ffprobe=None), save_delay_s=0.01)
    await lib.open()
    try:
        folder = await lib.add_folder(str(music), scan=False)
        await lib.rescan_folder(folder["id"])
        await lib.wait_hashed()
        before = sorted(e["id"] for e in lib.list_entries())
        full = lib.list_folders()[0]

        real_scandir = os.scandir
        refused = path_key(str(music / "Sub"))

        def scandir(path="."):
            if path_key(str(path)) == refused:
                raise PermissionError(13, "Permission denied", str(path))
            return real_scandir(path)

        monkeypatch.setattr(os, "scandir", scandir)
        (music / "a.mp3").unlink()  # gone too, but the walk is partial: no conclusion
        partial = await lib.rescan_folder(folder["id"])
        entries = lib.list_entries()
        monkeypatch.undo()

        whole = await lib.rescan_folder(folder["id"])
        after = {e["path"]: e["missing"] for e in lib.list_entries()}
    finally:
        await lib.stop()

    assert len(before) == 2
    assert partial["watch_state"] == "unavailable"
    assert "Sub" in partial["state_reason"] and "missing" in partial["state_reason"]
    assert partial["file_count"] == full["file_count"] and partial["last_scan_at"] == full["last_scan_at"]
    assert sorted(e["id"] for e in entries) == before
    assert all(e["missing"] is False for e in entries)
    # A complete walk afterwards draws the conclusions again.
    assert whole["watch_state"] == "polling"
    assert after == {str(music / "a.mp3"): True, str(music / "Sub" / "b.mp3"): False}
