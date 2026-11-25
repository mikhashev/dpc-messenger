"""Zcode's review #195 of the YuE2 cover (follow-up 18): the context a cover shares with its score and text, the
transcribe timeout, the level on a partial reading, the sweep of the `/synth` temp files, and the level measured
on the mono mix that is sent.

- Finding 1: the door holds a cover's length to what is left of yue2.cpp's context after a reserve for the score and
  the text; after the transcription the real score is counted and a take that cannot reach its length is refused
  (`context_exceeded`) before any `/synth` is sent.
- Finding 2: `/transcribe` is given time that grows with the source.
- Finding 3: a reading with no LUFS is leveled on its peak; a failed measurement on the mix's own sample peak.
- Finding 4: `synth.v<n>.multipart.part` files are swept at start, and only that exact shape.
- Finding 5: the loudness is measured on the mono downmix that goes to the transcriber.
"""

import asyncio
import json
import math
import types
from pathlib import Path

import numpy as np
import pytest

from dpc_client_core.music import cover_score as cs
from dpc_client_core.music import job_schemas as js
from dpc_client_core.music import region_edit as re_
from dpc_client_core.music import similar
from tests._acestep_helpers import fake_qa, free_env, install_fake_engine
from tests._yue2_helpers import DualRig, install_fake_yue2
from tests.test_a_song_from_a_library_track_is_a_cover_a_same_style_song_or_a_song_from_its_lyrics import (
    DONE, LOCAL, TRACK, VERSION, fake_decoder, write_base)

RATE = 48000


# -- finding 1: the door ------------------------------------------------------------------------------


def _door(seconds, **params):
    return js.normalize_params_song("cover", {"caption": "x", "lyrics": ["la la la"], **params}, track_id=TRACK,
                                    source=dict(LOCAL, duration_s=seconds))


def test_the_door_leaves_room_in_the_context_for_the_score_and_the_text():
    top = js.yue2_cover_door_max_s(len("x") + len("la la la"))
    assert top < js.YUE2_COVER_MAX_S and 500 < top < 600           # 982 s was frames only
    assert _door(top)["duration_s"] == top
    with pytest.raises(js.SongParamsError) as err:
        _door(top + 2)
    assert err.value.code == "source_too_long" and err.value.field == "track_id"
    assert "score" in err.value.message and "shared" in err.value.message
    # At the ceiling the three parts add up to the context: the frames, a score at the reserved rate, the text.
    frames = js.yue2_cover_frames(top)
    score = top * js.YUE2_SCORE_TOKENS_PER_S
    text = js.YUE2_PROMPT_FIXED_TOKENS + math.ceil(8 * js.YUE2_TEXT_TOKENS_PER_BYTE)
    assert frames + score + text + 1 <= js.YUE2_CONTEXT
    frames, score = js.yue2_cover_frames(top + 2), (top + 2) * js.YUE2_SCORE_TOKENS_PER_S
    assert frames + score + text + 1 > js.YUE2_CONTEXT


def test_a_long_text_shortens_the_ceiling_and_a_text_that_leaves_no_room_is_context_exceeded():
    short = js.yue2_cover_door_max_s(100)
    long_text = js.yue2_cover_door_max_s(8000)
    assert long_text < short
    lines = ["la " * 100] * 80                                     # about 24 kB
    assert js.yue2_cover_door_max_s(sum(len(x) for x in lines)) < long_text
    with pytest.raises(js.SongParamsError) as err:
        _door(js.yue2_cover_door_max_s(9001) + 2, lyrics=["a" * 9000])
    assert err.value.code == "source_too_long"
    with pytest.raises(js.SongParamsError) as err:
        _door(30.0, lyrics=["a" * 50000])
    assert err.value.code == "context_exceeded" and err.value.field == "lyrics"
    assert _door(30.0, lyrics=lines[:2])["duration_s"] == 30.0     # a short text is unaffected


def test_the_capabilities_report_the_door_ceiling_with_no_text_not_the_frames_only_one():
    from dpc_client_core.music.capabilities import MusicCapabilities
    report = MusicCapabilities().report()
    seconds = json.dumps(report, default=str)
    assert '"cover_source_s"' in seconds and f'"max": {js.yue2_cover_door_max_s(0)}' in seconds
    assert js.yue2_cover_door_max_s(0) < js.YUE2_COVER_MAX_S


# -- finding 1: after the transcription -------------------------------------------------------------------


def test_the_semantic_budget_is_the_engines_own_clamp_of_max_tokens_to_the_duration():
    assert js.yue2_semantic_budget({"duration": 7.0, "semantic_sampling": {"min_tokens": 125}}) == 175
    assert js.yue2_semantic_budget({"duration": 410.0, "semantic_sampling": {"min_tokens": 9000, "max_tokens": 10025}}) \
        == 10025
    assert js.yue2_semantic_budget({"semantic_sampling": {}}) == 9000


def test_a_score_that_leaves_no_room_for_the_music_is_refused_with_the_numbers():
    params = {"caption": "rock", "lyrics": ["la la"], "engine": "yue2", "lyrics_mode": "given"}
    ok = similar.check_cover_context(params, 300.0, "rock", "la la", "X:1\nK:C\n" + "|C D E F|" * 500)
    assert ok["left"] > 0 and ok["budget_tokens"] == js.yue2_cover_frames(300.0)
    from dpc_client_core.music.runner import _JobFailure
    with pytest.raises(_JobFailure) as err:
        similar.check_cover_context(params, 500.0, "rock", "la la", "X:1\nK:C\n" + "|C D E F|" * 2200)
    message = str(err.value)
    assert message.startswith("context_exceeded:") and "500 s" in message and str(js.YUE2_CONTEXT) in message
    assert "over" in message and "acestep" in message


@pytest.mark.asyncio
async def test_a_take_whose_score_cannot_fit_never_reaches_synth(tmp_path, monkeypatch):
    record = tmp_path / "posts"
    monkeypatch.setenv("FAKE_YUE2_MODE", "big_abc")
    monkeypatch.setenv("FAKE_YUE2_RECORD", str(record))
    free_env(monkeypatch)
    models = install_fake_yue2(tmp_path, voices=())
    install_fake_engine(tmp_path)
    for rel in ("serveurperso/SheetSage2-Q8_0.gguf", "serveurperso/MERT-v2-FullSong-Q8_0.gguf"):
        (models / rel).write_bytes(b"gguf")
    rig = DualRig(tmp_path)
    rig.store.capabilities._ffmpeg_probe = lambda: True
    rig.runner._decode_control = fake_decoder
    rig.runner._qa_measure = fake_qa
    async with rig:
        path = write_base(tmp_path / "music" / "Night Drive.wav", seconds=6.0)
        entry = await rig.library.register_generated(str(path), job_id="job_" + "2" * 16, lineage={"variant": 1})
        source = {"kind": "local", "content_hash": entry["content_hash"], "duration_s": 6.0, "title": "Night Drive"}
        job = rig.store.create_job("cover", {"caption": "rock", "lyrics": ["la la"], "variants": 1, "seed": 5},
                                   track_id=entry["id"], base_version_id=VERSION, source=source)
        rig.runner.wake()
        done = await rig.wait_for(job["id"], DONE)
        assert done["state"] == js.FAILED
        assert done["state_reason"].startswith("context_exceeded:"), done["state_reason"]
        assert list(record.glob("*-transcribe.json")) and not list(record.glob("*-synth.json"))
        names = {p["name"] for p in rig.store.get_job(job["id"])["payloads"]}
        assert similar.TRANSCRIBE_PAYLOAD in names and "synth.v1.request.json" not in names


# -- finding 2: the transcribe timeout ----------------------------------------------------------------


def test_the_transcribe_timeout_scales_with_the_source_and_is_never_under_the_flat_one():
    assert similar.transcribe_timeout_s(1800.0, 10.0) == 1800.0
    long = similar.transcribe_timeout_s(1800.0, 900.0)
    assert long == 900.0 * similar.TRANSCRIBE_S_PER_SOURCE_S + similar.TRANSCRIBE_SLACK_S > 1800.0


@pytest.mark.asyncio
async def test_transcribe_waits_the_scaled_time_not_the_flat_one(monkeypatch):
    monkeypatch.setattr(similar, "TRANSCRIBE_S_PER_SOURCE_S", 0.05)
    monkeypatch.setattr(similar, "TRANSCRIBE_SLACK_S", 0.0)
    runner = types.SimpleNamespace(engine_job_timeout_s=0.05, engine_poll_s=0.01, _check=lambda job_id: _noop())
    state = {"polls": 0, "cancelled": False}

    class Engine:
        DISPLAY = "yue2.cpp"

        async def submit_form(self, *a, **k):
            return "e1"

        async def job_status(self, eid):
            state["polls"] += 1
            return "done" if state["polls"] > 9 else "running"       # done after about 0.1 s

        async def cancel(self, eid):
            state["cancelled"] = True

        async def fetch_result(self, eid, _x):
            return "application/json", b'{"abc": "X"}'

        def with_log_tail(self, s):
            return s

    from dpc_client_core.music.runner import _JobFailure
    job = [None]
    with pytest.raises(_JobFailure):                                   # flat 0.05 s: a 2 s source gets 0.1 s, not this
        await similar._transcribe(runner, "j", Engine(), Path("x.wav"), True, job, 0.0)
    assert state["cancelled"]
    state.update(polls=0, cancelled=False)
    assert await similar._transcribe(runner, "j", Engine(), Path("x.wav"), True, job, 4.0) == b'{"abc": "X"}'
    assert not state["cancelled"]


async def _noop():
    return None


# -- finding 4: the sweep -----------------------------------------------------------------------------


def test_the_sweep_takes_a_yue2_synth_multipart_part_and_only_that_shape(tmp_path):
    d = tmp_path / "job_a"
    d.mkdir()
    ours = ["synth.v1.multipart.part", "synth.v12.multipart.part", similar.SOURCE_PART]
    theirs = ["synth.vx.multipart.part", "synth.v1.multipart.part.bak", "xsynth.v1.multipart.part", "mine.part",
              "synth.v.multipart.part", "take_v1.flac", "synth.v1.request.json"]
    for name in ours + theirs:
        (d / name).write_bytes(b"x")
    gone = similar.sweep_temp_parts(tmp_path)
    assert sorted(p.name for p in gone) == sorted(ours)
    assert sorted(p.name for p in d.iterdir()) == sorted(theirs)
    (d / "synth.v2.multipart.part").write_bytes(b"x")                  # a running job's folder is left alone
    assert similar.sweep_temp_parts(tmp_path, active={"job_a"}) == []


# -- findings 3 and 5: the level ------------------------------------------------------------------------


def _wav(path: Path, amplitude: float, seconds: float = 2.0, *, stereo: bool = True) -> Path:
    t = np.arange(int(seconds * RATE)) / RATE
    x = amplitude * np.sin(2 * np.pi * 220 * t)
    data = np.stack([x, x], axis=1) if stereo else x[:, None]
    re_.write_wav_s24(path, [data.astype(np.float32)], rate=RATE, channels=data.shape[1])
    return path


def _bs1770_like(path: Path) -> dict:
    """A meter that sums the channels' powers as BS.1770 does: a stereo file reads 3.01 LU over its mono mix."""
    pcm = re_.read_pcm(path)
    power = float((pcm.data.astype(np.float64) ** 2).mean(axis=0).sum())
    peak = float(abs(pcm.data).max())
    return {"lufs_i": round(10 * math.log10(power) - 0.691, 2), "lra": 0.0,
            "true_peak_dbtp": round(20 * math.log10(peak), 2)}


def _level(tmp_path, meter, amplitude, *, stereo=True):
    src = _wav(tmp_path / "source.wav.part", amplitude, stereo=stereo)
    runner = types.SimpleNamespace(_measure=meter)
    return src, *asyncio.run(similar._level_for_transcriber(runner, src, tmp_path))


def test_the_level_is_measured_on_the_mono_mix_that_is_sent_and_lands_on_target(tmp_path):
    seen = []

    def meter(path):
        seen.append(re_.read_pcm(path).channels)
        return _bs1770_like(path)

    src, heard, said = _level(tmp_path, meter, 0.9)                     # a fully correlated, loud source
    assert seen[0] == 1 and said["measured_on"] == "the mono mix that is sent" and said["path"] == "measured"
    assert heard.name == similar.LEVELLED_PART and re_.read_pcm(heard).channels == 1
    after = _bs1770_like(heard)
    assert abs(after["lufs_i"] - cs.LEVEL_TARGET_LUFS) < 0.15           # not 3 LU under, as a stereo reading gave
    assert after["true_peak_dbtp"] <= cs.LEVEL_PEAK_CEILING_DB + 0.1


def test_a_source_within_bounds_on_its_mono_mix_goes_as_it_is_and_leaves_no_temp_file(tmp_path):
    src, heard, said = _level(tmp_path, _bs1770_like, 0.1)
    assert heard == src and said["gain_db"] is None and said["path"] == "measured"
    assert not (tmp_path / similar.LEVELLED_PART).exists()


def test_a_reading_with_no_lufs_is_leveled_on_its_peak_and_says_so(tmp_path):
    src, heard, said = _level(tmp_path, lambda p: {"lufs_i": None, "true_peak_dbtp": -0.2}, 0.5)
    assert said["path"] == "peak_only" and said["gain_db"] == -1.3 and said["peak_from"] == "true_peak"
    assert heard.name == similar.LEVELLED_PART


def test_a_reading_with_no_peak_takes_the_mixs_own_sample_peak(tmp_path):
    src, heard, said = _level(tmp_path, lambda p: {"lufs_i": -9.0, "true_peak_dbtp": None}, 0.9)
    assert said["path"] == "lufs_sample_peak" and said["peak_from"] == "sample_peak" and said["gain_db"] < 0
    assert _bs1770_like(heard)["true_peak_dbtp"] <= cs.LEVEL_PEAK_CEILING_DB + 0.1


def test_a_failed_measurement_still_brings_a_hot_peak_down_by_the_sample_peak(tmp_path):
    def broken(path):
        raise RuntimeError("ffmpeg is not on PATH")

    src, heard, said = _level(tmp_path, broken, 0.99)                    # -0.09 dBFS
    assert said["path"] == "sample_peak_only" and said["measured"] is False and "not measured" in said["reason"]
    assert said["gain_db"] is not None and -1.6 < said["gain_db"] < -1.3
    assert heard.name == similar.LEVELLED_PART
    assert _bs1770_like(heard)["true_peak_dbtp"] <= cs.LEVEL_PEAK_CEILING_DB + 0.05


def test_a_failed_measurement_of_a_quiet_source_changes_nothing(tmp_path):
    def broken(path):
        raise RuntimeError("no ffmpeg")

    src, heard, said = _level(tmp_path, broken, 0.2)
    assert heard == src and said["gain_db"] is None and said["path"] == "sample_peak_only"
    assert not (tmp_path / similar.LEVELLED_PART).exists()


def test_the_partial_reading_rule_in_cover_score_keeps_the_peak_half():
    assert cs.transcriber_gain_db(None, -0.2) == -1.3
    assert cs.transcriber_gain_db(None, -3.0) is None and cs.transcriber_gain_db(None, None) is None
    assert cs.transcriber_gain_db(-9.0, None) == -7.0
