"""Follow-up 20 (Zcode #201 on the YuE2 cover): the three P4 findings, each pinned.

1. A mix of digital silence has no sample peak (-inf). With a loudness and no true peak the gain used to follow
   the loudness alone while `said.path` claimed the protected path; now the gain is capped by the mix's own
   sample peak (-1.5 dBFS ceiling), a silent mix gets none, and the path says `silent_no_gain`.
2. `/transcribe` (upload, polling, fetch of the answer) is under one deadline; on expiry the engine job is
   cancelled and the job fails with "did not finish".
3. The door counts the text the synth request carries (`job_schemas.song_caption_sent`, the helper the run
   uses), so a request the door takes does not fail on its text after the transcription.
"""

import asyncio
import types
from pathlib import Path

import numpy as np
import pytest

from dpc_client_core.music import cover_score as cs
from dpc_client_core.music import job_schemas as js
from dpc_client_core.music import region_edit as re_
from dpc_client_core.music import similar
from tests.test_a_song_from_a_library_track_is_a_cover_a_same_style_song_or_a_song_from_its_lyrics import (
    LOCAL, TRACK)

RATE = 48000


# -- finding 1: the level -----------------------------------------------------------------------------


def _wav(path: Path, amplitude: float, seconds: float = 2.0) -> Path:
    t = np.arange(int(seconds * RATE)) / RATE
    x = amplitude * np.sin(2 * np.pi * 220 * t)
    re_.write_wav_s24(path, [np.stack([x, x], axis=1).astype(np.float32)], rate=RATE, channels=2)
    return path


def _level(tmp_path, meter, amplitude):
    src = _wav(tmp_path / "source.wav.part", amplitude)
    runner = types.SimpleNamespace(_measure=meter)
    return src, *asyncio.run(similar._level_for_transcriber(runner, src, tmp_path))


def test_a_silent_mix_with_a_loudness_and_no_peak_gets_no_gain_and_says_so(tmp_path):
    src, heard, said = _level(tmp_path, lambda p: {"lufs_i": -9.0, "true_peak_dbtp": None}, 0.0)
    assert said["path"] == "silent_no_gain" and said["gain_db"] is None
    assert said["peak_from"] is None and said["true_peak_dbtp"] is None
    assert heard == src and not (tmp_path / similar.LEVELLED_PART).exists()


def test_a_loudness_with_no_peak_never_lifts_the_mixs_sample_peak_over_the_ceiling(tmp_path):
    src, heard, said = _level(tmp_path, lambda p: {"lufs_i": -9.0, "true_peak_dbtp": None}, 0.9)
    assert said["path"] == "lufs_sample_peak" and said["peak_from"] == "sample_peak"
    after = float(abs(re_.read_pcm(heard).data).max())
    assert 20 * np.log10(after) <= cs.LEVEL_PEAK_CEILING_DB + 0.05
    # A mix whose loudness asks for more gain than its peak has room for: the gain stops at the ceiling.
    # (a float mix can peak over full scale: here +2 dBFS, so -3 LU would leave it at -1 dBFS, over the ceiling)
    gain = cs.transcriber_gain_db(-13.0, None, mix_peak_db=2.0)
    assert gain == -3.5 and 2.0 + gain <= cs.LEVEL_PEAK_CEILING_DB


def test_the_gain_rule_with_a_mix_peak_in_cover_score():
    assert cs.transcriber_gain_db(-9.0, None, mix_peak_db=-30.0) == -7.0      # the loudness decides: the peak has room
    assert cs.transcriber_gain_db(-9.0, None, mix_peak_db=float("-inf")) is None
    assert cs.transcriber_gain_db(-9.0, float("-inf"), mix_peak_db=float("-inf")) is None   # -inf is no peak
    assert cs.transcriber_gain_db(-9.0, float("-inf")) == -7.0                # no mix peak given: loudness alone
    assert cs.transcriber_gain_db(-9.0, -3.0, mix_peak_db=-0.1) == -7.0       # a peak that is known wins


# -- finding 2: one deadline over the whole call ------------------------------------------------------


class _Engine:
    DISPLAY = "yue2.cpp"

    def __init__(self, *, hang_submit=False, hang_fetch=False):
        self.hang_submit, self.hang_fetch = hang_submit, hang_fetch
        self.cancelled = []

    async def submit_form(self, *a, **k):
        if self.hang_submit:
            await asyncio.sleep(3600)
        return "e1"

    async def job_status(self, eid):
        return "done"

    async def fetch_result(self, eid, _dest):
        if self.hang_fetch:
            await asyncio.sleep(3600)
        return "application/json", b'{"abc": "X"}'

    async def cancel(self, eid):
        self.cancelled.append(eid)

    def with_log_tail(self, s):
        return s


async def _noop(job_id):
    return None


@pytest.fixture(autouse=True)
def _no_slack(monkeypatch):
    """The timeout is the flat one: no five minutes of slack, no seconds per source second."""
    monkeypatch.setattr(similar, "TRANSCRIBE_SLACK_S", 0.0)
    monkeypatch.setattr(similar, "TRANSCRIBE_S_PER_SOURCE_S", 0.0)


def _runner(timeout_s):
    return types.SimpleNamespace(engine_job_timeout_s=timeout_s, engine_poll_s=0.01, _check=_noop)


@pytest.mark.asyncio
async def test_a_fetch_that_hangs_runs_out_of_the_deadline_and_cancels_the_engine_job():
    from dpc_client_core.music.runner import _JobFailure
    engine, job = _Engine(hang_fetch=True), [None]
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(_JobFailure) as err:
        await similar._transcribe(_runner(0.2), "j", engine, Path("x.wav"), True, job, 0.0)
    assert loop.time() - started < 5.0
    assert "did not finish within 0.2 s" in str(err.value) and "e1" in str(err.value)
    assert engine.cancelled == ["e1"]


@pytest.mark.asyncio
async def test_an_upload_that_hangs_runs_out_of_the_deadline_with_no_job_to_cancel():
    from dpc_client_core.music.runner import _JobFailure
    engine, job = _Engine(hang_submit=True), [None]
    with pytest.raises(_JobFailure) as err:
        await similar._transcribe(_runner(0.2), "j", engine, Path("x.wav"), True, job, 0.0)
    assert "did not finish" in str(err.value) and engine.cancelled == []


@pytest.mark.asyncio
async def test_a_transcription_within_the_deadline_returns_the_answer_and_clears_the_job_slot():
    engine, job = _Engine(), [None]
    assert await similar._transcribe(_runner(5.0), "j", engine, Path("x.wav"), True, job, 0.0) == b'{"abc": "X"}'
    assert job == [None] and engine.cancelled == []


# -- finding 3: the door and the check count the same text --------------------------------------------


def _door(seconds, **params):
    return js.normalize_params_song("cover", params, track_id=TRACK, source=dict(LOCAL, duration_s=seconds))


def test_the_caption_is_composed_in_one_place():
    assert js.song_caption_sent("  jazz \n  piano ", "the sea") == "jazz piano, lyrics about the sea"
    assert js.song_caption_sent("jazz", None) == "jazz"
    params = {"caption": "  jazz \n  piano ", "lyrics_theme": "the sea"}
    assert similar.caption_sent(params) == js.song_caption_sent(params["caption"], params["lyrics_theme"])


def test_a_request_the_door_takes_at_its_ceiling_does_not_fail_on_its_text_after_the_transcription():
    """For every text length, take the longest source the door allows for it, then run the run's own check with
    a score of the length the door reserved: the text part is never over what the door counted. (A cover takes
    no theme, `lyrics_mode new` is no cover mode, so the text is the caption, a newline and the lines.)"""
    for n in range(1, 400):
        caption = "warm  jazz\npiano" + " " * (n % 7)                  # white space the run makes single
        lines = ["la la la", "", "[chorus]", "y" * n]
        sent = js.song_text_sent(js.song_caption_sent(caption, None), "\n".join(lines))
        counted = len(sent.encode("utf-8")) + js.YUE2_COVER_TEMPO_RESERVE_BYTES
        top = js.yue2_cover_door_max_s(counted)
        while top > js.yue2_cover_door_max_s(counted + js.yue2_cover_tag_reserve_bytes(top)):
            top -= 1                                                    # the section-tag reserve (follow-up 21)
        normalized = _door(top, caption=caption, lyrics_mode="reuse", lyrics=lines)
        assert normalized["duration_s"] == top
        score = "A" * int(top * js.YUE2_SCORE_TOKENS_PER_S / js.YUE2_ABC_TOKENS_PER_BYTE)
        # The style the run sends is the caption plus, at most, the tempo `agree_style` appends.
        style = similar.caption_sent(normalized) + ", 9999 BPM"
        numbers = similar.check_cover_context(normalized, top, style, "\n".join(normalized["lyrics"]), score)
        assert numbers["left"] >= 0
        assert numbers["text_bytes"] <= counted


def test_the_door_counts_the_theme_and_the_separators_the_old_raw_sum_missed():
    theme = "x" * 200
    old = len("jazz") + len(theme)                                    # the raw sum the door used to count
    sent = len(js.song_text_sent(js.song_caption_sent("jazz", theme), None))
    assert sent - old == len(", lyrics about ") + 1                  # 16 bytes the door could not see
    assert js.yue2_cover_door_max_s(sent + js.YUE2_COVER_TEMPO_RESERVE_BYTES) <= js.yue2_cover_door_max_s(old)
