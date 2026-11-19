"""Playlists: create, rename, reorder by setting the full ordered list (SPEC §6), and
delete without touching the library; smart lists are computed and cannot be changed.
"""

import pytest

from dpc_client_core.music.identity import MediaTools
from dpc_client_core.music.library import SMART_ALL_ID, MusicLibrary, MusicLibraryError


@pytest.mark.asyncio
async def test_playlist_create_rename_reorder_delete(tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    for name in ("a", "b", "c"):
        (music / f"{name}.mp3").write_bytes(name.encode() * 10)
    lib = MusicLibrary(tmp_path / "dpc", tools=MediaTools(ffmpeg=None, ffprobe=None), save_delay_s=0.01)
    await lib.open()
    try:
        folder = await lib.add_folder(str(music), scan=False)
        await lib.rescan_folder(folder["id"])
        await lib.wait_hashed()
        ids = {e["source"]["name"]: e["id"] for e in lib.list_entries()}
        a, b, c = ids["a.mp3"], ids["b.mp3"], ids["c.mp3"]

        created = await lib.create_playlist("  Road  ", [a, b])
        assert created["name"] == "Road" and created["track_ids"] == [a, b] and created["kind"] == "user"

        renamed = await lib.update_playlist(created["id"], name="Road trip")
        assert renamed["name"] == "Road trip" and renamed["track_ids"] == [a, b]

        reordered = await lib.update_playlist(created["id"], track_ids=[c, a, b, a])
        assert reordered["track_ids"] == [c, a, b, a]  # a track may appear twice

        with pytest.raises(MusicLibraryError) as unknown:
            await lib.update_playlist(created["id"], track_ids=[a, "trk_ffffffffffffffff"])
        with pytest.raises(MusicLibraryError) as smart:
            await lib.update_playlist(SMART_ALL_ID, name="Mine")
        with pytest.raises(MusicLibraryError) as unnamed:
            await lib.create_playlist("   ")

        all_tracks = [p for p in lib.list_playlists() if p["id"] == SMART_ALL_ID][0]
        assert sorted(all_tracks["track_ids"]) == sorted([a, b, c])

        await lib.delete_playlist(created["id"])
        with pytest.raises(MusicLibraryError) as gone:
            await lib.delete_playlist(created["id"])
        remaining = [p["id"] for p in lib.list_playlists()]
    finally:
        await lib.stop()

    assert unknown.value.code == "unknown_track"
    assert smart.value.code == "smart_list"
    assert unnamed.value.code == "invalid_name"
    assert gone.value.code == "unknown_playlist"
    assert remaining == ["pl_smart_all", "pl_smart_generated"]
    assert len(lib.list_entries()) == 3  # deleting a playlist keeps its tracks


@pytest.mark.asyncio
async def test_a_playlist_position_follows_a_track_merged_into_its_duplicate(tmp_path):
    # A pending (not yet hashed) entry may already be in a playlist; when its hash
    # shows it is a copy of a known track, the position moves to that track.
    music = tmp_path / "Music"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"same audio")
    lib = MusicLibrary(tmp_path / "dpc", tools=MediaTools(ffmpeg=None, ffprobe=None), save_delay_s=0.01)
    await lib.open()
    try:
        folder = await lib.add_folder(str(music), scan=False)
        await lib.rescan_folder(folder["id"])
        await lib.wait_hashed()
        [known] = lib.list_entries()
        (music / "copy.mp3").write_bytes(b"same audio")
        lib._worker.cancel()  # hold the hash so the copy stays pending
        await lib.rescan_folder(folder["id"])
        [pending] = [e for e in lib.list_entries() if e["hash_state"] == "pending"]
        playlist = await lib.create_playlist("P", [pending["id"]])
        await lib.open()  # restarts the worker
        await lib.wait_hashed()
        [merged] = lib.list_entries()
        lists = lib.list_playlists()
    finally:
        await lib.stop()
    assert merged["id"] == known["id"]
    assert [p for p in lists if p["id"] == playlist["id"]][0]["track_ids"] == [known["id"]]
