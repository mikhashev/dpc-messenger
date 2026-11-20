"""Remove from library drops a missing entry for good, with its playlist positions; a
present entry is refused; Remove all missing drops every missing one."""

import pytest

from tests._music_hide_helpers import folder_with_tracks, library
from dpc_client_core.music.library import MusicLibraryError


def _playlist_ids(lib, playlist_id):
    return [p for p in lib.list_playlists() if p["id"] == playlist_id][0]["track_ids"]


@pytest.mark.asyncio
async def test_remove_refuses_a_present_entry_and_drops_missing_ones(tmp_path):
    home, music = tmp_path / "dpc", tmp_path / "Music"
    lib = library(home)
    await lib.open()
    try:
        folder, ids = await folder_with_tracks(lib, music, ("a.mp3", "b.mp3", "c.mp3"))
        a, b, c = ids["a.mp3"], ids["b.mp3"], ids["c.mp3"]
        pl = await lib.create_playlist("Mix", [a, b, c])
        with pytest.raises(MusicLibraryError) as present:
            await lib.remove_missing(a)
        (music / "a.mp3").unlink()
        (music / "b.mp3").unlink()
        await lib.rescan_folder(folder["id"])
        assert lib.get_entry(a)["missing"] and lib.get_entry(b)["missing"]
        removed_one = await lib.remove_missing(a)
        after_one = _playlist_ids(lib, pl["id"])
        removed_rest = await lib.remove_missing()
        after_all = _playlist_ids(lib, pl["id"])
        left = [e["id"] for e in lib.list_entries()]
        nothing = await lib.remove_missing()
    finally:
        await lib.stop()
    assert present.value.code == "not_missing"
    assert removed_one == [a] and after_one == [b, c]
    assert removed_rest == [b] and after_all == [c]
    assert left == [c] and nothing == []
