"""A track keeps its id across a move, a rename and a tag edit (ADR-051 Q4).

The id is stable and random; a new path whose audio hash equals a missing entry's
relinks it. With ffmpeg the hash is over the audio packets, so a `-c copy` retag keeps
it; without ffmpeg the fallback is a whole-file hash marked `hash_kind: "file"`. A FLAC
is identified by its STREAMINFO MD5 without any tool.
"""

import os
import shutil
import subprocess

import pytest

from dpc_client_core.music import identity
from dpc_client_core.music.identity import MediaTools, read_flac_streaminfo
from dpc_client_core.music.library import MusicLibrary

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not on PATH, so no audio can be generated or stream-hashed")


def _library(home, tools=None):
    return MusicLibrary(home, tools=tools or MediaTools(ffmpeg=None, ffprobe=None), save_delay_s=0.01)


async def _scan(lib, folder_id):
    await lib.rescan_folder(folder_id)
    await lib.wait_hashed()


def _ffmpeg(*args):
    subprocess.run([FFMPEG, "-nostdin", "-v", "error", "-y", *args], check=True, timeout=60,
                   stdin=subprocess.DEVNULL, capture_output=True)


@pytest.mark.asyncio
async def test_a_moved_and_renamed_file_relinks_to_the_same_track(tmp_path):
    music = tmp_path / "Music"
    (music / "Old").mkdir(parents=True)
    (music / "New").mkdir()
    (music / "Old" / "song.mp3").write_bytes(os.urandom(4096))
    lib = _library(tmp_path / "dpc")
    await lib.open()
    try:
        folder = await lib.add_folder(str(music), scan=False)
        await _scan(lib, folder["id"])
        [before] = lib.list_entries()
        playlist = await lib.create_playlist("Keep", [before["id"]])

        os.replace(music / "Old" / "song.mp3", music / "New" / "renamed.mp3")
        await _scan(lib, folder["id"])
        after = lib.list_entries()
        lists = lib.list_playlists()
    finally:
        await lib.stop()

    assert [e["id"] for e in after] == [before["id"]]
    assert after[0]["path"] == str(music / "New" / "renamed.mp3")
    assert after[0]["missing"] is False
    assert after[0]["content_hash"] == before["content_hash"]
    assert [p for p in lists if p["id"] == playlist["id"]][0]["track_ids"] == [before["id"]]


@pytest.mark.asyncio
async def test_without_ffmpeg_the_hash_is_of_the_whole_file_and_says_so(tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    (music / "a.ogg").write_bytes(b"OggS fake")
    lib = _library(tmp_path / "dpc")
    await lib.open()
    try:
        folder = await lib.add_folder(str(music), scan=False)
        await _scan(lib, folder["id"])
        [entry] = lib.list_entries()
    finally:
        await lib.stop()
    assert entry["hash_kind"] == "file"
    assert entry["content_hash"] == "sha256:" + identity.file_sha256(music / "a.ogg")


def _fake_flac(md5: bytes, sample_rate=44100, channels=2, total_samples=441000):
    bits = (sample_rate << 44) | ((channels - 1) << 41) | (15 << 36) | total_samples
    streaminfo = (4096).to_bytes(2, "big") * 2 + b"\x00" * 6 + bits.to_bytes(8, "big") + md5
    assert len(streaminfo) == 34
    return b"fLaC" + bytes([0x80]) + (34).to_bytes(3, "big") + streaminfo + b"\xff\xf8 frames"


def test_flac_streaminfo_is_read_in_pure_python(tmp_path):
    path = tmp_path / "x.flac"
    path.write_bytes(_fake_flac(bytes(range(16))))
    info = read_flac_streaminfo(path)
    assert info.md5 == bytes(range(16)).hex()
    assert (info.sample_rate, info.channels, info.total_samples) == (44100, 2, 441000)
    assert info.duration_s == pytest.approx(10.0)
    # Behind a leading ID3v2 tag, as some taggers write it.
    tagged = tmp_path / "tagged.flac"
    tagged.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x05" + b"12345" + _fake_flac(bytes(range(16))))
    assert read_flac_streaminfo(tagged).md5 == info.md5
    assert read_flac_streaminfo(tmp_path / "x.flac") is not None
    (tmp_path / "not.flac").write_bytes(b"RIFF....")
    assert read_flac_streaminfo(tmp_path / "not.flac") is None


@pytest.mark.asyncio
async def test_a_flac_keeps_its_id_when_only_its_tags_change_even_without_ffmpeg(tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    md5 = bytes.fromhex("00112233445566778899aabbccddeeff")
    (music / "a.flac").write_bytes(_fake_flac(md5))
    lib = _library(tmp_path / "dpc")
    await lib.open()
    try:
        folder = await lib.add_folder(str(music), scan=False)
        await _scan(lib, folder["id"])
        [before] = lib.list_entries()
        # A tag edit: different bytes and size, same STREAMINFO.
        (music / "a.flac").write_bytes(_fake_flac(md5) + b" PADDING-AND-VORBIS-COMMENT")
        await _scan(lib, folder["id"])
        after = lib.list_entries()
    finally:
        await lib.stop()
    assert before["hash_kind"] == "flac_streaminfo" and before["content_hash"] == f"md5:{md5.hex()}"
    assert [e["id"] for e in after] == [before["id"]]
    assert after[0]["file_stat"] != before["file_stat"]
    assert after[0]["duration_s"] == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_a_zero_flac_md5_falls_back_to_another_hash(tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    (music / "a.flac").write_bytes(_fake_flac(b"\x00" * 16))
    ident = identity.identify(music / "a.flac", MediaTools(ffmpeg=None, ffprobe=None))
    assert ident.hash_kind == "file"


@needs_ffmpeg
@pytest.mark.asyncio
async def test_with_ffmpeg_a_retag_keeps_the_audio_hash_and_the_track(tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    original = music / "song.mp3"
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-metadata", "title=First",
            "-c:a", "libmp3lame", "-b:a", "64k", str(original))
    lib = _library(tmp_path / "dpc", tools=MediaTools.discover())
    await lib.open()
    try:
        folder = await lib.add_folder(str(music), scan=False)
        await _scan(lib, folder["id"])
        [before] = lib.list_entries()
        whole_before = identity.file_sha256(original)

        retagged = tmp_path / "retagged.mp3"
        _ffmpeg("-i", str(original), "-map", "0", "-c", "copy", "-metadata", "title=Second",
                "-metadata", "artist=Someone", str(retagged))
        os.replace(retagged, original)
        assert identity.file_sha256(original) != whole_before  # the bytes did change

        await _scan(lib, folder["id"])
        after = lib.list_entries()
    finally:
        await lib.stop()

    assert before["hash_kind"] == "audio_stream"
    assert [e["id"] for e in after] == [before["id"]]
    assert after[0]["content_hash"] == before["content_hash"]
    if shutil.which("ffprobe"):
        assert before["title"] == "First"
        assert after[0]["title"] == "Second" and after[0]["artist"] == "Someone"
        assert after[0]["duration_s"] == pytest.approx(1.0, abs=0.1)


@needs_ffmpeg
@pytest.mark.asyncio
async def test_with_ffmpeg_a_retagged_copy_elsewhere_relinks_a_missing_track(tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    original = music / "a.flac"
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=220:duration=1", str(original))
    assert read_flac_streaminfo(original).md5_is_set  # ffmpeg's encoder writes the MD5
    lib = _library(tmp_path / "dpc", tools=MediaTools.discover())
    await lib.open()
    try:
        folder = await lib.add_folder(str(music), scan=False)
        await _scan(lib, folder["id"])
        [before] = lib.list_entries()
        _ffmpeg("-i", str(original), "-map", "0", "-c", "copy", "-metadata", "title=Moved", str(music / "b.flac"))
        original.unlink()
        await _scan(lib, folder["id"])
        after = lib.list_entries()
    finally:
        await lib.stop()
    assert before["hash_kind"] == "flac_streaminfo"
    assert [e["id"] for e in after] == [before["id"]]
    assert after[0]["path"] == str(music / "b.flac")


@pytest.mark.asyncio
async def test_new_audio_at_the_same_path_is_a_new_track_and_the_old_one_is_kept(tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"first recording")
    lib = _library(tmp_path / "dpc")
    await lib.open()
    try:
        folder = await lib.add_folder(str(music), scan=False)
        await _scan(lib, folder["id"])
        [old] = lib.list_entries()
        (music / "a.mp3").write_bytes(b"an entirely different take")
        await _scan(lib, folder["id"])
        entries = {e["id"]: e for e in lib.list_entries()}
    finally:
        await lib.stop()
    assert len(entries) == 2
    assert entries[old["id"]]["missing"] is True
    [new] = [e for e in entries.values() if e["id"] != old["id"]]
    assert new["path"] == str(music / "a.mp3") and new["missing"] is False
