"""Hide is a reversible flag on the entry: the file is untouched, the smart lists leave
the track out, and it survives a reload, a rescan, and a relink of a moved file."""

import pytest

from tests._music_hide_helpers import folder_with_tracks, library
from dpc_client_core.music.library import SMART_ALL_ID, MusicLibraryError


def _all(lib):
    return next(p for p in lib.list_playlists() if p["id"] == SMART_ALL_ID)["track_ids"]


@pytest.mark.asyncio
async def test_hidden_persists_across_save_load_and_rescan_and_is_reversible(tmp_path):
    home, music = tmp_path / "dpc", tmp_path / "Music"
    lib = library(home)
    await lib.open()
    try:
        folder, ids = await folder_with_tracks(lib, music)
        a, b = ids["a.mp3"], ids["b.mp3"]
        await lib.set_hidden(a, True)
        assert _all(lib) == [b]
        assert (music / "a.mp3").exists()
        with pytest.raises(MusicLibraryError) as unknown:
            await lib.set_hidden("trk_ffffffffffffffff", True)
        await lib.rescan_folder(folder["id"])
        await lib.wait_hashed()
        still_hidden_after_rescan = lib.get_entry(a)["hidden"]
    finally:
        await lib.stop()
    lib2 = library(home)
    await lib2.open()
    try:
        reloaded = lib2.get_entry(a)["hidden"]
        await lib2.set_hidden(a, False)
        back = sorted(_all(lib2))
    finally:
        await lib2.stop()
    assert unknown.value.code == "unknown_track"
    assert still_hidden_after_rescan is True and reloaded is True
    assert back == sorted([a, b])


@pytest.mark.asyncio
async def test_a_moved_file_relinked_by_hash_keeps_hidden(tmp_path):
    home, music = tmp_path / "dpc", tmp_path / "Music"
    lib = library(home)
    await lib.open()
    try:
        folder, ids = await folder_with_tracks(lib, music)
        a = ids["a.mp3"]
        await lib.set_hidden(a, True)
        (music / "a.mp3").rename(music / "moved.mp3")
        await lib.rescan_folder(folder["id"])
        await lib.wait_hashed()
        entry = lib.get_entry(a)
        count = len(lib.list_entries())
    finally:
        await lib.stop()
    assert entry["hidden"] is True and entry["missing"] is False
    assert entry["path"].endswith("moved.mp3")
    assert count == 2
