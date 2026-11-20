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
async def test_delete_is_not_offered_for_a_watched_folder_track(tmp_path):
    home, music = tmp_path / "dpc", tmp_path / "Music"
    lib = library(home)
    await lib.open()
    try:
        _, ids = await folder_with_tracks(lib, music, ("a.mp3",))
        with pytest.raises(MusicLibraryError) as exc:
            await lib.delete_generated_file(ids["a.mp3"])
    finally:
        await lib.stop()
    assert exc.value.code == "not_owned" and (music / "a.mp3").exists()
