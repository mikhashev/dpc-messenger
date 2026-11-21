"""Waveform peaks (`music/peaks.py`, `get_music_track_peaks`): ffmpeg decodes in the core,
the reduction is plain bucket math, the cache is keyed by the content hash, and the
command resolves the file from the library by id."""

import asyncio
import json
import shutil
import subprocess
import sys
from array import array
from pathlib import Path

import pytest

from dpc_client_core import local_api
from dpc_client_core.music import commands, peaks
from dpc_client_core.music.commands import MUSIC_COMMANDS, MusicCommandsMixin
from dpc_client_core.music.identity import MediaTools

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def _pcm(values):
    a = array("h", values)
    if sys.byteorder == "big":
        a.byteswap()
    return a.tobytes()


def _fake_tools(pcm=b"", rc=0, err=b"", calls=None):
    def runner(cmd, *, timeout):
        if calls is not None:
            calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, rc, pcm, err)

    return MediaTools(ffmpeg="ffmpeg", ffprobe=None, runner=runner)


def test_reduce_pcm_gives_the_min_and_max_of_each_bucket():
    samples = array("h", [0, 100, -200, 300, 32767, -32768, 0, 0])
    out = peaks.reduce_pcm(samples, 4)
    assert len(out) == 4
    assert out[0] == [0.0, round(100 / 32767, 3)]
    assert out[1] == [round(-200 / 32768, 3), round(300 / 32767, 3)]
    assert out[2] == [-1.0, 1.0]
    assert out[3] == [0.0, 0.0]


def test_reduce_pcm_gives_fewer_buckets_than_samples_and_none_for_silence_of_no_length():
    assert len(peaks.reduce_pcm(array("h", [1, 2, 3]), 10)) == 3
    assert peaks.reduce_pcm(array("h"), 10) == []


def test_the_bucket_count_is_clamped_and_must_be_an_integer():
    assert peaks.clamp_buckets(None) == peaks.DEFAULT_BUCKETS
    assert peaks.clamp_buckets(1) == peaks.MIN_BUCKETS
    assert peaks.clamp_buckets(10**9) == peaks.MAX_BUCKETS
    for bad in ("5", 2.5, True):
        with pytest.raises(peaks.PeaksError) as e:
            peaks.clamp_buckets(bad)
        assert e.value.code == "invalid_params"


def test_peaks_come_from_the_decoded_pcm_with_the_documented_command_line(tmp_path):
    audio = tmp_path / "a.flac"
    audio.write_bytes(b"x")
    calls = []
    tools = _fake_tools(_pcm([1000, -1000] * 4000), calls=calls)
    doc = peaks.get_peaks(audio, None, tmp_path / "home", 100, tools)
    assert doc["cached"] is False and doc["n"] == 100 and len(doc["peaks"]) == 100
    assert doc["duration_s"] == 1.0 and doc["sample_rate_used"] == 8000
    cmd = calls[0]
    assert cmd[cmd.index("-ac") + 1] == "1" and cmd[cmd.index("-ar") + 1] == "8000"
    assert cmd[-3:] == ["-f", "s16le", "-"]
    assert not (tmp_path / "home").exists()  # no hash, no cache


def test_the_cache_is_written_by_hash_hit_again_and_survives_a_renamed_file(tmp_path):
    home = tmp_path / "home"
    first = tmp_path / "a.flac"
    first.write_bytes(b"x")
    calls = []
    tools = _fake_tools(_pcm(list(range(-5000, 5000))), calls=calls)
    one = peaks.get_peaks(first, "sha256:abc123", home, 100, tools)
    assert one["cached"] is False
    cache = peaks.cache_path(home, "sha256:abc123")
    assert cache == home / "music" / "peaks" / "sha256-abc123.json" and cache.is_file()
    assert not list(cache.parent.glob("*.tmp"))
    saved = json.loads(cache.read_text(encoding="utf-8"))
    assert {"version", "n", "duration_s", "sample_rate_used", "peaks"} <= set(saved)

    first.rename(tmp_path / "renamed.flac")  # the file is gone from the old path; the hash is the same
    two = peaks.get_peaks(None, "sha256:abc123", home, 100, tools)
    assert two["cached"] is True and two["peaks"] == one["peaks"] and len(calls) == 1


def test_another_bucket_count_is_a_resample_of_the_cached_canonical_peaks_not_a_second_decode(tmp_path):
    home = tmp_path / "home"
    audio = tmp_path / "a.flac"
    audio.write_bytes(b"x")
    calls = []
    # a ramp: bucket maxima rise, minima fall, so pooling is easy to check
    pcm = list(range(-12000, 12000))
    tools = _fake_tools(_pcm(pcm), calls=calls)
    first = peaks.get_peaks(audio, "sha256:h1", home, 100, tools)
    assert first["cached"] is False and len(first["peaks"]) == 100
    canon = peaks.get_peaks(None, "sha256:h1", home, None, tools)
    wide = peaks.get_peaks(None, "sha256:h1", home, 500, tools)
    narrow = peaks.get_peaks(None, "sha256:h1", home, 100, tools)
    assert len(calls) == 1 and canon["cached"] and wide["cached"] and narrow["cached"]
    assert len(canon["peaks"]) == peaks.DEFAULT_BUCKETS and canon["n"] == peaks.DEFAULT_BUCKETS
    assert len(wide["peaks"]) == 500 and narrow["peaks"] == first["peaks"]
    saved = json.loads(peaks.cache_path(home, "sha256:h1").read_text(encoding="utf-8"))
    assert saved["n"] == peaks.DEFAULT_BUCKETS
    # each narrow bucket is the min of the mins and the max of the maxes of the canonical ones it covers
    step = peaks.DEFAULT_BUCKETS // 100
    for i, (lo, hi) in enumerate(narrow["peaks"]):
        group = canon["peaks"][i * step:(i + 1) * step]
        assert lo == min(g[0] for g in group) and hi == max(g[1] for g in group)


def test_resample_never_invents_detail():
    src = [[-0.5, 0.5], [-0.2, 0.9], [-0.7, 0.1]]
    assert peaks.resample_peaks(src, 10) == src
    assert peaks.resample_peaks(src, 3) == src
    assert peaks.resample_peaks(src, 1) == [[-0.7, 0.9]]
    assert peaks.resample_peaks(src, 2) == [[-0.5, 0.5], [-0.7, 0.9]]


def test_the_cache_is_recomputed_for_another_hash_or_a_bad_version(tmp_path):
    home = tmp_path / "home"
    audio = tmp_path / "a.flac"
    audio.write_bytes(b"x")
    calls = []
    tools = _fake_tools(_pcm([5, -5] * 3000), calls=calls)
    peaks.get_peaks(audio, "sha256:h1", home, 100, tools)
    assert peaks.get_peaks(audio, "sha256:h2", home, 100, tools)["cached"] is False      # another content
    assert peaks.get_peaks(audio, "sha256:h1", home, 200, tools)["cached"] is True       # another n: a resample
    cache = peaks.cache_path(home, "sha256:h1")
    doc = json.loads(cache.read_text(encoding="utf-8"))
    doc["version"] = 0
    cache.write_text(json.dumps(doc), encoding="utf-8")
    assert peaks.get_peaks(audio, "sha256:h1", home, 200, tools)["cached"] is False      # old version
    cache.write_text("{not json", encoding="utf-8")
    assert peaks.get_peaks(audio, "sha256:h1", home, 200, tools)["cached"] is False      # corrupt
    assert len(calls) == 4


def test_an_unsafe_hash_is_never_a_path(tmp_path):
    audio = tmp_path / "a.flac"
    audio.write_bytes(b"x")
    tools = _fake_tools(_pcm([1, 2, 3] * 100))
    doc = peaks.get_peaks(audio, "../../evil", tmp_path / "home", 100, tools)
    assert doc["cached"] is False and not (tmp_path / "home").exists()


def test_the_errors_are_clear(tmp_path):
    audio = tmp_path / "a.flac"
    audio.write_bytes(b"x")
    with pytest.raises(peaks.PeaksError) as e:
        peaks.get_peaks(tmp_path / "gone.flac", None, tmp_path, 100, _fake_tools(b"1234"))
    assert e.value.code == "no_file"
    with pytest.raises(peaks.PeaksError) as e:
        peaks.get_peaks(None, None, tmp_path, 100, _fake_tools(b"1234"))
    assert e.value.code == "no_file"
    with pytest.raises(peaks.PeaksError) as e:
        peaks.get_peaks(audio, None, tmp_path, 100, MediaTools(ffmpeg=None, ffprobe=None))
    assert e.value.code == "no_ffmpeg" and "ffmpeg" in e.value.message
    with pytest.raises(peaks.PeaksError) as e:
        peaks.get_peaks(audio, None, tmp_path, 100, _fake_tools(rc=1, err=b"bad data"))
    assert e.value.code == "decode_failed" and "bad data" in e.value.message
    with pytest.raises(peaks.PeaksError) as e:
        peaks.get_peaks(audio, None, tmp_path, 100, _fake_tools(b""))
    assert e.value.code == "decode_failed"

    def slow(cmd, *, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout)

    with pytest.raises(peaks.PeaksError) as e:
        peaks.get_peaks(audio, None, tmp_path, 100, MediaTools(ffmpeg="ffmpeg", runner=slow))
    assert e.value.code == "timeout"


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg is not installed")
def test_a_real_ffmpeg_run_on_a_generated_tone(tmp_path):
    tone = tmp_path / "tone.wav"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         str(tone)], check=True)
    doc = peaks.get_peaks(tone, "tone-hash", tmp_path / "home", 200)
    assert doc["cached"] is False and len(doc["peaks"]) == 200
    assert abs(doc["duration_s"] - 2.0) < 0.1
    highs = [hi for _, hi in doc["peaks"]]
    lows = [lo for lo, _ in doc["peaks"]]
    assert max(highs) > 0.1 and min(lows) < -0.1 and max(highs) <= 1.0
    assert peaks.get_peaks(None, "tone-hash", tmp_path / "home", 200)["cached"] is True


class _Settings:
    def __init__(self, home):
        self.dpc_home_dir = home

    def get(self, section, key, fallback=None):
        return "0.05" if (section, key) == ("music", "poll_interval_s") else fallback


class _Service(MusicCommandsMixin):
    def __init__(self, home):
        self.settings = _Settings(home)


def test_the_command_is_registered_allowed_and_slow():
    assert "get_music_track_peaks" in MUSIC_COMMANDS and "get_music_track_peaks" in local_api.ALLOWED_COMMANDS
    fn = MusicCommandsMixin.get_music_track_peaks
    assert asyncio.iscoroutinefunction(fn) and fn.dpc_slow is True


@pytest.mark.asyncio
async def test_the_command_resolves_the_file_from_the_library_by_id(tmp_path, monkeypatch):
    original = commands.MusicLibrary.__init__
    tools = _fake_tools(_pcm([300, -300] * 2000))

    def init(self, home, **kw):
        kw.setdefault("tools", tools)
        kw.setdefault("save_delay_s", 0.01)
        original(self, home, **kw)

    monkeypatch.setattr(commands.MusicLibrary, "__init__", init)
    music = tmp_path / "Music"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"audio-a")
    service = _Service(tmp_path / "dpc")
    try:
        await service.add_music_folder(path=str(music))
        await service.rescan_music_folder()
        await service._music_library_instance.wait_hashed()
        [entry] = (await service.list_music_library())["entries"]

        got = await service.get_music_track_peaks(track_id=entry["id"], n=100)
        assert got["status"] == "ok" and got["cached"] is False and len(got["peaks"]) == 100
        again = await service.get_music_track_peaks(track_id=entry["id"], n=100)
        assert again["cached"] is True and again["peaks"] == got["peaks"]

        assert (await service.get_music_track_peaks(track_id="nope"))["code"] == "unknown_track"
        assert (await service.get_music_track_peaks())["code"] == "invalid_params"
        assert (await service.get_music_track_peaks(track_id=entry["id"], n="x"))["code"] == "invalid_params"

        (music / "a.mp3").unlink()
        peaks.cache_path(tmp_path / "dpc", entry["content_hash"]).unlink()
        gone = await service.get_music_track_peaks(track_id=entry["id"], n=100)
        assert gone["status"] == "error" and gone["code"] == "no_file"
    finally:
        await service.stop_music_library()
