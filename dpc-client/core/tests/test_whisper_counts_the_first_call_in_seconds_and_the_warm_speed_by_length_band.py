"""ADR-052 S3 follow-up 2, Johnny #24-1 and #24-2, Zcode #22-2 and #22-3: the speed model.

**The first call costs seconds, not a ratio** (Johnny #24-1). `gpu_run1.log` says the first call after
a load costs about +0.9 s on a 10 s clip and is within run-to-run spread from 118 s up; dividing the
whole audio by a "cold ratio" (8.8x, the 10 s clip's first run) made a cold 300 s file 12.8 + 300/8.8
= 46.9 s against the ~31.7 s measured (12.8 s load + 18.93 s). The estimate is now
`load_s (not loaded) + audio / warm[band] + penalty_s (first call after a load)`, and a measured
first call updates `penalty_s` (what it took beyond `audio / warm` of its band, at least 0).

**The warm figure is kept by length band** (Johnny #24-2), on the batch descent's thresholds: up to
60 s (batch 16), up to 120 s (8), up to 180 s (4), over 180 s (2). A 10 s clip at 43x no longer
makes a 300 s file look like 7 s: a measurement moves only its own band.

**The figures follow the model** across a providers reload, band by band, and a load that finishes on
the old object after the hand-over keeps its load time (Zcode #22-2). The start figures come from
`_START_FIGURES`, never from literals here (Zcode #22-3).

The pipeline, librosa and torch are fakes; no model is loaded.
"""

import math
import sys
import types

import pytest

from dpc_client_core.managers.card_lease import seconds_from_ratio
from dpc_client_core.providers import whisper_provider
from dpc_client_core.providers.whisper_provider import (
    _BATCH_DESCENT,
    _START_FIGURES,
    LocalWhisperProvider,
    length_band,
)

CUDA = _START_FIGURES["cuda"]


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    librosa = types.ModuleType("librosa")
    # The audio's length is in the file name: "a30.wav" is 30 s.
    librosa.load = lambda path, sr=16000: ([0.0] * int(sr * float(path[1:-4])), sr)
    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: False, empty_cache=lambda: None)
    torch.backends = types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: False))
    monkeypatch.setitem(sys.modules, "librosa", librosa)
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setattr(whisper_provider, "_idle_unload_source", None)


def _provider(device="cuda", loaded=False, model="openai/whisper-large-v3-turbo"):
    p = LocalWhisperProvider("w", {"type": "local_whisper", "device": device, "model": model})
    p.batches = []

    def pipe(audio, batch_size=None, return_timestamps=None, **kwargs):
        import time
        time.sleep(0.03)  # a wall time the loop's clock can see on every platform
        p.batches.append(batch_size)
        return {"text": "t", "chunks": []}

    p.pipeline = pipe
    if loaded:
        p.model_loaded = True
    return p


def _close(a, b, tol=1e-9):
    return a is not None and math.isclose(a, b, rel_tol=tol)


class TestTheFirstCallIsSecondsAdded:
    def test_a_cold_300_s_file_is_the_load_the_warm_work_and_the_penalty(self):
        p = _provider("cuda")
        expected = CUDA["load_first_s"] + seconds_from_ratio(300, CUDA["warm"][">180"]) + CUDA["penalty_s"][">180"]
        assert _close(p.expected_seconds(300), expected)
        # The falsifier: the old cold ratio said 12.8 + 300 / 8.8 = 46.9 s; measured was ~31.7 s.
        assert p.expected_seconds(300) < 40

    def test_loaded_but_unused_pays_the_penalty_and_no_load(self):
        p = _provider("cuda", loaded=True)
        assert _close(p.expected_seconds(30), seconds_from_ratio(30, CUDA["warm"]["<=60"]) + CUDA["penalty_s"]["<=60"])

    def test_the_start_penalty_is_the_logs_first_call_rounded_up(self):
        # gpu_run1.log: ru_10 run0 1.14 s against the 0.23 s median, +0.91 s.
        assert 0.91 <= CUDA["penalty_s"]["<=60"] <= 1.5

    @pytest.mark.asyncio
    async def test_a_measured_first_call_sets_the_penalty_in_seconds(self, monkeypatch):
        p = _provider("cuda", loaded=True)
        p._note_load("cuda", 0.8)
        warm = CUDA["warm"]["<=60"]
        p._note_run("cuda", 10.0, 10.0 / warm + 2.5, first=True)
        figures = p.speed_figures("cuda")
        assert _close(figures["penalty_s"]["<=60"], 2.5, 1e-6)
        assert figures["warm"]["<=60"] == warm, "a first call never moves a warm figure"
        # Still seconds added, never a ratio: 50 s on a just-loaded model, the same band.
        assert _close(p.expected_seconds(50, loaded=True),
                      seconds_from_ratio(50, CUDA["warm"]["<=60"]) + 2.5, 1e-6)

    def test_a_first_call_faster_than_warm_gives_a_penalty_of_zero(self):
        p = _provider("cuda")
        p._note_run("cuda", 60.0, 0.5, first=True)
        assert p.speed_figures("cuda")["penalty_s"]["<=60"] == 0.0

    @pytest.mark.asyncio
    async def test_the_first_real_call_after_a_load_is_the_one_recorded_as_the_penalty(self):
        p = _provider("cpu", loaded=True)
        p._note_load("cpu", 0.8)
        await p.transcribe("a10.wav")
        assert "penalty_s" in p._measured["cpu"] and "warm" not in p._measured["cpu"]
        await p.transcribe("a10.wav")
        assert "<=60" in p._measured["cpu"]["warm"]

    def test_the_cpu_keeps_l12s_cold_row_in_its_load(self):
        cpu = _START_FIGURES["cpu"]
        assert set(cpu["penalty_s"].values()) == {0.0} and cpu["load_first_s"] == 9.0
        # L12's table: 10 s of audio, 11 s warm, about 20 s cold.
        assert _close(_provider("cpu").expected_seconds(10), 9.0 + 11.0, 1e-6)


class TestTheWarmFigureByLengthBand:
    @pytest.mark.parametrize("audio_s, band", [
        (1, "<=60"), (60, "<=60"), (60.01, "<=120"), (120, "<=120"), (120.5, "<=180"),
        (180, "<=180"), (180.01, ">180"), (1800, ">180"), (None, None), (0, None)])
    def test_the_bands_are_the_batch_descents(self, audio_s, band):
        assert length_band(audio_s) == band

    @pytest.mark.asyncio
    @pytest.mark.parametrize("audio_s, band", [(60, "<=60"), (61, "<=120"), (121, "<=180"), (181, ">180")])
    async def test_each_band_is_one_batch_size_of_the_descent(self, audio_s, band):
        p = _provider("cpu", loaded=True)
        await p.transcribe(f"a{audio_s}.wav")
        divisor = dict(zip((">180", "<=180", "<=120"), (d for _, d in _BATCH_DESCENT))).get(band, 1)
        assert p.batches == [max(1, p.batch_size // divisor)]

    def test_the_cuda_start_figures_are_the_logs_slowest_clip_per_band(self):
        # gpu_run1.log SUMMARY: en_30 21.1x, ru_118 27.4x, en_300 15.9x; nothing at batch 4.
        assert CUDA["warm"] == {"<=60": 21.1, "<=120": 27.4, "<=180": 15.9, ">180": 15.9}

    @pytest.mark.asyncio
    async def test_a_fast_short_clip_leaves_a_long_files_estimate_on_its_own_band(self):
        # The falsifier (Johnny #24-2): 10 s warm at 43x, then a 300 s file.
        p = _provider("cuda", loaded=True)
        p._note_load("cuda", 0.8)
        p._calls_since_load = 1
        p._note_run("cuda", 10.0, 10.0 / 43.4, first=False)
        assert _close(p.speed_figures("cuda")["warm"]["<=60"], 43.4)
        assert _close(p.expected_seconds(300), seconds_from_ratio(300, CUDA["warm"][">180"]))
        assert p.expected_seconds(300) > 18.0, "300 s at 15.9x is about 18.9 s, never 6.9 s"

    def test_a_measurement_moves_only_its_own_band(self):
        p = _provider("cuda")
        p._note_run("cuda", 150.0, 5.0, first=False)
        warm = p.speed_figures("cuda")["warm"]
        assert _close(warm["<=180"], 30.0)
        for band in ("<=60", "<=120", ">180"):
            assert warm[band] == CUDA["warm"][band]

    def test_a_device_with_no_start_figures_is_unknown_in_every_band(self):
        p = _provider("mps")
        for audio_s in (10, 100, 150, 300):
            assert p.expected_seconds(audio_s, device="mps") is None


class TestTheFiguresFollowTheModel:
    def test_a_providers_reload_carries_every_band_and_the_penalty(self):
        old, new = _provider("cuda"), _provider("cuda")
        old._note_run("cuda", 30.0, 1.0, first=False)
        old._note_run("cuda", 300.0, 10.0, first=False)
        old._note_run("cuda", 10.0, 3.0, first=True)
        old.hand_over_to(new)
        assert new.speed_figures("cuda") == old.speed_figures("cuda")

    def test_a_reload_keeps_what_the_new_object_measured_itself(self):
        old, new = _provider("cuda"), _provider("cuda")
        old._note_run("cuda", 30.0, 1.0, first=False)
        new._note_run("cuda", 30.0, 3.0, first=False)
        old.hand_over_to(new)
        assert _close(new.speed_figures("cuda")["warm"]["<=60"], 10.0)

    def test_another_models_figures_are_not_carried(self):
        old, new = _provider("cuda"), _provider("cuda", model="openai/whisper-small")
        old._note_run("cuda", 30.0, 1.0, first=False)
        old.hand_over_to(new)
        assert new.speed_figures("cuda") == {**CUDA, "warm": dict(CUDA["warm"]),
                                             "penalty_s": dict(CUDA["penalty_s"]), "load_s": CUDA["load_first_s"]}

    def test_a_load_that_finishes_on_the_old_object_after_the_hand_over_keeps_its_time(self):
        # Zcode #22-2: the reload happened while the old object was loading; its load lands there.
        old, new = _provider("cuda"), _provider("cuda")
        old.hand_over_to(new)
        old.pipeline = object()
        old.model_loaded = True
        old._note_load("cuda", 2.5)
        old._settle_finished_load()
        assert new.model_loaded and new._loaded_device == "cuda"
        # The first load of the process (S3 follow-up 4 keeps it apart from a reload).
        assert new.speed_figures("cuda")["load_first_s"] == 2.5
