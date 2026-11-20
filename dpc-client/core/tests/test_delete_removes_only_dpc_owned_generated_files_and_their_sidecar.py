"""Delete file: only inside <DPC home>/music/generated/ (resolved, so `..` and symlinks
cannot escape), never a watched folder's file; it removes the audio and its sidecar json,
leaves siblings alone, and drops the entry and its playlist positions."""

import os

import pytest

from tests._music_hide_helpers import folder_with_tracks, generated_dir, library, take
from dpc_client_core.music.library import MusicLibraryError


@pytest.mark.asyncio
async def test_delete_removes_the_wav_and_its_json_and_drops_the_entry(tmp_path):
    home = tmp_path / "dpc"
    lib = library(home)
    await lib.open()
    try:
        folder = generated_dir(home)
        wav = take(folder, "take1.wav")
        other = take(folder, "take1.latent.pt")
        sidecar = folder / "take1.json"
        sidecar.write_text("{}")
        entry = await lib.register_generated(str(wav), job_id="job_1", lineage={"m": 1})
        pl = await lib.create_playlist("Mix", [entry["id"]])
        answer = await lib.delete_generated_file(entry["id"])
        playlist_ids = [p for p in lib.list_playlists() if p["id"] == pl["id"]][0]["track_ids"]
        entries = lib.list_entries()
    finally:
        await lib.stop()
    assert not wav.exists() and not sidecar.exists() and other.exists()
    assert len(answer["deleted"]) == 2
    assert entries == [] and playlist_ids == []


@pytest.mark.asyncio
async def test_delete_refuses_a_file_outside_generated_including_dotdot(tmp_path):
    home = tmp_path / "dpc"
    lib = library(home)
    await lib.open()
    try:
        outside = take(tmp_path / "elsewhere", "song.wav")
        gen = generated_dir(home)
        gen.mkdir(parents=True)
        sneaky = os.path.join(str(gen), "..", "..", "..", "..", "elsewhere", "song.wav")
        entry = await lib.register_generated(str(outside), job_id="job_1", lineage={})
        refused = []
        for path in (str(outside), sneaky):
            lib._entries[entry["id"]]["path"] = path
            with pytest.raises(MusicLibraryError) as exc:
                await lib.delete_generated_file(entry["id"])
            refused.append(exc.value.code)
        still = lib.get_entry(entry["id"])["id"]
    finally:
        await lib.stop()
    assert refused == ["not_owned", "not_owned"]
    assert outside.exists() and still == entry["id"]


@pytest.mark.asyncio
async def test_delete_refuses_a_symlink_that_points_out_of_generated(tmp_path):
    home = tmp_path / "dpc"
    lib = library(home)
    await lib.open()
    try:
        outside = take(tmp_path / "elsewhere", "song.wav")
        gen = generated_dir(home)
        gen.mkdir(parents=True)
        link = gen / "link.wav"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are not permitted here")
        entry = await lib.register_generated(str(outside), job_id="job_1", lineage={})
        lib._entries[entry["id"]]["path"] = str(link)
        with pytest.raises(MusicLibraryError) as exc:
            await lib.delete_generated_file(entry["id"])
    finally:
        await lib.stop()
    assert exc.value.code == "not_owned" and outside.exists()


@pytest.mark.asyncio
async def test_a_generated_file_seen_through_a_watched_music_dir_is_deletable_and_stays_gone(tmp_path):
    home = tmp_path / "dpc"
    music = home / "music"
    lib = library(home)
    await lib.open()
    try:
        wav = take(generated_dir(home), "take1.wav")
        song = take(music / "files", "mine.mp3")
        folder = await lib.add_folder(str(music), scan=False)
        await lib.rescan_folder(folder["id"])
        await lib.wait_hashed()
        ids = {e["source"]["name"]: e for e in lib.list_entries()}
        gen_entry, own_entry = ids["take1.wav"], ids["mine.mp3"]
        flags = (gen_entry["folder_id"] is not None, gen_entry["deletable"], own_entry["deletable"])
        with pytest.raises(MusicLibraryError) as refused:
            await lib.delete_generated_file(own_entry["id"])
        await lib.delete_generated_file(gen_entry["id"])
        await lib.rescan_folder(folder["id"])
        await lib.wait_hashed()
        names = sorted(e["source"]["name"] for e in lib.list_entries())
        count = lib.list_folders()[0]["file_count"]
    finally:
        await lib.stop()
    assert flags == (True, True, False)
    assert refused.value.code == "not_owned" and song.exists()
    assert not wav.exists() and names == ["mine.mp3"] and count == 1


@pytest.mark.asyncio
async def test_a_track_in_an_ordinary_watched_folder_is_not_deletable(tmp_path):
    home, music = tmp_path / "dpc", tmp_path / "Music"
    lib = library(home)
    await lib.open()
    try:
        _, ids = await folder_with_tracks(lib, music, ("a.mp3",))
        flag = lib.get_entry(ids["a.mp3"])["deletable"]
        with pytest.raises(MusicLibraryError) as exc:
            await lib.delete_generated_file(ids["a.mp3"])
    finally:
        await lib.stop()
    assert flag is False and exc.value.code == "not_owned" and (music / "a.mp3").exists()
