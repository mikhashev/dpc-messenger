"""ADR-052 S3 follow-up 6: what the group-chat reviews #47 (Johnny), #48 (Forge) and #49 (Ark) found
in follow-ups 3 to 5.

**A reload figure only for a device that has loaded** (Forge #48-1, Ark #49-1). One process-wide flag,
set by any load on any device, picked `load_reload_s` for every device: after the CPU twin loaded,
the GPU's first load read 0.8 s instead of 12.8 s, and after 0.8 s its ETA ran into "unknown" for the
rest of a ~12 s load (an owner saw "time unknown", a peer was refused). It is now the set of devices
that have loaded in this process. These tests build a CPU and a GPU provider in one test: the
conftest clears the set per test, so a carry-over between the two is invisible otherwise. A reload
that overruns its figure now falls back to the device's first-load figure before it is unknown.

**The ETA during a load reads the device L12 routed on** (Ark #49-2): the route's device is carried
into the load, never read again there, and a model that loaded elsewhere is logged at WARNING.

**The detection never runs on the loop** (Forge #48-4): the route's device is detected in a thread.

**The warm figure learns from the decoded length** (Ark #49-3): the batch follows the estimate's
length, the record the decoded one; a difference over 1 s is logged.

**The unknown-length branch of the loading ETA adds nothing twice** (Forge #48-5, read with the code).

The pipeline, librosa and torch are fakes; no model is loaded.
"""

import asyncio
import logging
import sys
import threading
import time
import types

import pytest

from dpc_client_core.managers.card_lease import seconds_from_ratio
from dpc_client_core.providers import whisper_provider as wp
from dpc_client_core.providers.whisper_provider import _START_FIGURES, LocalWhisperProvider, length_band

from tests._card_lease_helpers import BOUND, fast, until  # noqa: F401
from tests._whisper_lease_helpers import cpu_twin, gpu_whisper, lease_of

CUDA, CPU = _START_FIGURES["cuda"], _START_FIGURES["cpu"]

pytestmark = pytest.mark.usefixtures("fast")


def _provider(device="cuda"):
    return LocalWhisperProvider("w", {"type": "local_whisper", "device": device,
                                      "model": "openai/whisper-large-v3-turbo"})


def _gated_load(p, gate: threading.Event, load_on: str = "cuda", load_s: float = 0.1):
    """The fake load waits in its thread until `gate` is set; it reports `load_on` as the device."""

    def load():
        p.loads += 1
        assert gate.wait(BOUND), "the test never let the load finish"
        p.pipeline = object()
        p.model_loaded = True
        p._note_load(load_on, load_s)

    p._load_model = load


async def _loading(p):
    await until(lambda: p.loads == 1 and lease_of(p) is not None and lease_of(p).state == "starting")


# --- item 1: a reload figure only for a device that has loaded ------------------------------------


class TestAReloadFigureOnlyForADeviceThatHasLoaded:
    def test_a_cpu_twins_load_leaves_the_gpus_first_load(self):
        gpu, twin = _provider("cuda"), _provider("cpu")
        cold_before = gpu.expected_seconds(30)
        twin._note_load("cpu", 9.0)
        assert twin.speed_figures("cpu")["load_s"] == CPU["load_reload_s"], "the twin's next load is a reload"
        assert gpu.speed_figures("cuda")["load_s"] == CUDA["load_first_s"], "the GPU has not loaded yet"
        assert gpu.expected_seconds(30) == cold_before

    def test_a_gpu_load_leaves_the_twins_first_load(self):
        gpu, twin = _provider("cuda"), _provider("cpu")
        gpu._note_load("cuda", 12.0)
        assert twin.speed_figures("cpu")["load_s"] == CPU["load_first_s"]
        twin._note_load("cpu", 9.5)
        assert twin._measured["cpu"]["load_first_s"] == 9.5, "recorded as the twin's own first load"

    def test_each_device_turns_to_its_reload_after_its_own_load(self):
        gpu, other_gpu = _provider("cuda"), _provider("cuda")
        other_gpu._note_load("cuda", 12.0)  # another object, the same device: a providers reload
        assert gpu.speed_figures("cuda")["load_s"] == CUDA["load_reload_s"]

    @pytest.mark.asyncio
    async def test_the_gpus_first_load_after_the_twin_loaded_has_the_first_loads_eta(self, monkeypatch):
        p = gpu_whisper(monkeypatch)
        twin = cpu_twin(monkeypatch, p)
        twin._load_model()  # the twin's fake load: `_note_load("cpu", ...)`
        assert twin.model_loaded
        gate = threading.Event()
        _gated_load(p, gate)
        preload = asyncio.ensure_future(p.ensure_model_loaded())
        try:
            await _loading(p)
            eta = lease_of(p)._eta()
            assert eta is not None and CUDA["load_first_s"] - 1.0 < eta <= CUDA["load_first_s"], (
                f"Forge #48-1: the GPU's first load reads 12.8 s, not the 0.8 s reload ({eta})")
        finally:
            gate.set()
            await asyncio.wait_for(preload, BOUND)

    @pytest.mark.asyncio
    async def test_a_reload_past_its_figure_falls_back_to_the_first_load(self, monkeypatch):
        _provider("cuda")._note_load("cuda", 12.0)  # the GPU has loaded in this process
        monkeypatch.setitem(wp._START_FIGURES["cuda"], "load_reload_s", 0.01)
        p = gpu_whisper(monkeypatch)
        gate = threading.Event()
        _gated_load(p, gate)
        preload = asyncio.ensure_future(p.ensure_model_loaded())
        try:
            await _loading(p)
            started = time.monotonic()
            await until(lambda: time.monotonic() - started > 0.05)
            eta = lease_of(p)._eta()
            first = p.speed_figures("cuda")["load_first_s"]
            assert eta is not None and first - 1.0 < eta < first, (
                f"a reload over its 0.01 s reads the first load's 12.0 s, not unknown ({eta})")
        finally:
            gate.set()
            await asyncio.wait_for(preload, BOUND)

    def test_past_the_first_load_too_it_is_unknown(self):
        figures = _provider("cuda").speed_figures("cuda")
        assert wp.LocalWhisperProvider._load_left(figures, CUDA["load_first_s"] + 0.1) is None
        reload = {**figures, "load_s": figures["load_reload_s"]}
        assert wp.LocalWhisperProvider._load_left(reload, CUDA["load_first_s"] + 0.1) is None
        assert wp.LocalWhisperProvider._load_left(reload, 0.5) == pytest.approx(CUDA["load_reload_s"] - 0.5)


# --- item 2: the ETA during a load reads the device L12 routed on ----------------------------------


class TestTheLoadReadsTheRoutesDevice:
    @pytest.mark.asyncio
    async def test_the_loading_eta_reads_the_device_the_route_chose(self, monkeypatch):
        p = gpu_whisper(monkeypatch, device="auto", load_on="cuda")
        reads = []

        def route_device(remember=True):  # the first read says cuda, any later one another device
            reads.append(remember)
            return "cuda" if len(reads) == 1 else "mps"

        monkeypatch.setattr(p, "_route_device", route_device)
        gate = threading.Event()
        _gated_load(p, gate)
        preload = asyncio.ensure_future(p.ensure_model_loaded())
        try:
            await _loading(p)
            assert p._card_load["device"] == "cuda"
            eta = lease_of(p)._eta()
            assert eta is not None and eta > CUDA["load_first_s"] - 1.0, (
                "Ark #49-2: the ETA reads cuda, the route's device, never a second read (mps, unknown)")
        finally:
            gate.set()
            await asyncio.wait_for(preload, BOUND)

    @pytest.mark.asyncio
    async def test_a_model_that_loaded_on_another_device_is_logged(self, monkeypatch, caplog):
        p = gpu_whisper(monkeypatch, device="cuda", load_on="mps")
        with caplog.at_level(logging.WARNING, logger=wp.logger.name):
            await asyncio.wait_for(p.ensure_model_loaded(), BOUND)
        assert any("estimated on cuda and ran on mps" in r.getMessage() and r.levelno == logging.WARNING
                   for r in caplog.records)

    @pytest.mark.asyncio
    async def test_a_model_that_loaded_where_it_was_estimated_logs_nothing(self, monkeypatch, caplog):
        p = gpu_whisper(monkeypatch, device="cuda")
        with caplog.at_level(logging.WARNING, logger=wp.logger.name):
            await asyncio.wait_for(p.ensure_model_loaded(), BOUND)
        assert not any("was estimated on" in r.getMessage() for r in caplog.records)


# --- item 6: the detection never runs on the loop ---------------------------------------------------


class TestTheDetectionRunsOffTheLoop:
    @staticmethod
    def _recording_detection(monkeypatch, p):
        threads = []

        def detect():
            threads.append(threading.get_ident())
            return "cuda"

        monkeypatch.setattr(p, "_detect_device", detect)
        return threads

    @pytest.mark.asyncio
    async def test_the_preload_detects_in_a_thread(self, monkeypatch):
        p = gpu_whisper(monkeypatch, device="auto", load_on="cuda")
        threads = self._recording_detection(monkeypatch, p)
        loop_thread = threading.get_ident()
        await asyncio.wait_for(p.ensure_model_loaded(), BOUND)
        assert p.model_loaded and p.loads == 1
        assert threads and all(t != loop_thread for t in threads), "Forge #48-4: never on the loop"
        assert len(threads) == 1, "detected once, then kept"

    @pytest.mark.asyncio
    async def test_a_transcription_detects_in_a_thread(self, monkeypatch):
        p = gpu_whisper(monkeypatch, device="auto", load_on="cuda")
        threads = self._recording_detection(monkeypatch, p)
        loop_thread = threading.get_ident()
        await asyncio.wait_for(p.transcribe("a.wav"), BOUND)
        assert threads and all(t != loop_thread for t in threads)


# --- item 3: the warm figure learns from the decoded length ------------------------------------------


@pytest.fixture
def decoding(monkeypatch):
    """librosa reads the length from the file name ("a270.wav" is 270 s); torch has no CUDA."""
    librosa = types.ModuleType("librosa")
    librosa.load = lambda path, sr=16000: (range(int(sr * float(path[1:-4]))), sr)
    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: False, empty_cache=lambda: None)
    torch.backends = types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: False))
    monkeypatch.setitem(sys.modules, "librosa", librosa)
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setattr(wp, "_idle_unload_source", None)


def _warm_provider(monkeypatch):
    p = _provider("cuda")
    p.batches = []
    p.runs = []

    def pipe(audio, batch_size=None, return_timestamps=None, **kwargs):
        time.sleep(0.03)
        p.batches.append(batch_size)
        return {"text": "t", "chunks": []}

    p.pipeline = pipe
    p.model_loaded = True
    p._calls_since_load = 1  # not the first call: the run records a warm figure
    real = p._note_run

    def note_run(device, audio_s, elapsed, *, first):
        p.runs.append((audio_s, elapsed))
        real(device, audio_s, elapsed, first=first)

    monkeypatch.setattr(p, "_note_run", note_run)
    return p


@pytest.mark.usefixtures("decoding")
class TestTheWarmFigureLearnsFromTheDecodedLength:
    @pytest.mark.asyncio
    async def test_a_300_s_estimate_decoded_at_270_s_records_270_over_its_time(self, monkeypatch, caplog):
        p = _warm_provider(monkeypatch)
        with caplog.at_level(logging.WARNING, logger=wp.logger.name):
            await asyncio.wait_for(p.transcribe("a270.wav", audio_s=300.0), BOUND)
        (audio_s, elapsed), = p.runs
        assert audio_s == 270.0, "Ark #49-3: the measurement is the decoded length"
        assert p._measured["cuda"]["warm"][">180"] == pytest.approx(270.0 / elapsed)
        assert p.batches == [max(1, p.batch_size // 8)], "the batch is the estimate's (over 180 s)"
        assert any("estimate read 300.0 s" in r.getMessage() and "decode gives 270.0 s" in r.getMessage()
                   for r in caplog.records)

    @pytest.mark.asyncio
    async def test_a_difference_within_a_second_is_not_logged(self, monkeypatch, caplog):
        p = _warm_provider(monkeypatch)
        with caplog.at_level(logging.WARNING, logger=wp.logger.name):
            await asyncio.wait_for(p.transcribe("a29.5.wav", audio_s=30.0), BOUND)
        assert p.runs[0][0] == 29.5
        assert not any("decode gives" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_across_a_band_boundary_the_batch_is_the_estimates_and_the_record_the_decodes(
            self, monkeypatch):
        p = _warm_provider(monkeypatch)
        await asyncio.wait_for(p.transcribe("a59.6.wav", audio_s=60.4), BOUND)
        assert p.batches == [max(1, p.batch_size // 2)], "batch 8: the estimate's band, <=120"
        assert set(p._measured["cuda"]["warm"]) == {length_band(59.6)} == {"<=60"}


# --- item 7: the unknown-length branch of the loading ETA adds nothing twice --------------------------


class TestTheLoadingEtaOfACallOfUnknownLength:
    @pytest.mark.asyncio
    async def test_its_expected_time_holds_the_load_and_is_not_added_to_it(self, monkeypatch):
        # A door's `expected_s` for an unloaded model holds the load (`expected_seconds`). With no
        # length the ETA is max(load left, expected - elapsed), never load left + expected.
        p = gpu_whisper(monkeypatch)
        expected = p.expected_seconds(30)  # load 12.8 + work + penalty
        assert expected > CUDA["load_first_s"]
        gate = threading.Event()
        _gated_load(p, gate)
        call = asyncio.ensure_future(p.transcribe("a.wav", expected_s=expected))
        try:
            await _loading(p)
            eta = lease_of(p)._eta()
            assert eta is not None and expected - 1.0 < eta <= expected, (
                f"Forge #48-5: {eta} against {expected}; a double count would read "
                f"{CUDA['load_first_s'] + expected:.1f}")
        finally:
            gate.set()
            await asyncio.wait_for(call, BOUND)

    @pytest.mark.asyncio
    async def test_with_the_length_known_it_is_the_load_left_plus_the_first_calls_work(self, monkeypatch):
        p = gpu_whisper(monkeypatch)
        work = seconds_from_ratio(30, CUDA["warm"]["<=60"]) + CUDA["penalty_s"]["<=60"]
        gate = threading.Event()
        _gated_load(p, gate)
        call = asyncio.ensure_future(p.transcribe("a.wav", expected_s=p.expected_seconds(30), audio_s=30.0))
        try:
            await _loading(p)
            eta = lease_of(p)._eta()
            total = CUDA["load_first_s"] + work
            assert eta is not None and total - 1.0 < eta <= total
        finally:
            gate.set()
            await asyncio.wait_for(call, BOUND)
