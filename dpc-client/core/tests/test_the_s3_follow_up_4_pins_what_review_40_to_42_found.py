"""ADR-052 S3 follow-up 4: what the group-chat reviews #40 (Ark), #41 (Johnny) and #42 (Forge) found.

**The first call's penalty by length band** (Johnny #41-2, Forge #42-3). `penalty_s` was one figure
per device, so a long first call (300 s, whose first-call cost is within run-to-run spread, ≈0) set
it to ≈0 for every length, and a cold 10 s clip (≈+0.9 s in `gpu_run1.log`) lost its penalty. It is
now kept by length band, as `warm` is: 1.0 s in every band on cuda, 0 on the cpu, and a first call
moves its own band only.

**A holder that is loading has an ETA** (Ark #40-1, Johnny #41-5). During a load the lease is
`starting` (busy) and its `eta_s` was only the open calls' `expected_s - elapsed`: nothing for a
preload, and a call whose estimate was made while the model was loaded (no load counted) ran over
into unknown. It is now the load's remaining time (from the load's start and the current load
figure) plus the expected work of the calls waiting for it, and None when any of that work is unknown.

**Two start load figures** (Forge #42-4). The 12.8 s of a cold load is a fresh process's (imports
included); a reload in a running process is 0.6-0.9 s. `load_first_s` counts until a load has run
in this process (any provider object: a providers reload builds new ones), `load_reload_s` after;
a measured load of each kind wins over its start figure.

**The dead length probe is gone** (Ark #40-3): `_get_audio_duration` had no caller.

The pipeline, librosa and torch are fakes; no model is loaded.
"""

import asyncio
import math
import threading

import pytest

from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.card_lease import PEER_TRIP_S, LeaseRefusedForPeer, seconds_from_ratio
from dpc_client_core.managers.llama_server_supervisor import serving_a_peer
from dpc_client_core.providers import whisper_provider as wp
from dpc_client_core.providers.whisper_provider import _LENGTH_BANDS, _START_FIGURES, LocalWhisperProvider

from tests._card_lease_helpers import BOUND, fast, until, waiting, whisper  # noqa: F401
from tests._whisper_lease_helpers import gpu_whisper, lease_of

CUDA, CPU = _START_FIGURES["cuda"], _START_FIGURES["cpu"]


def _provider(device="cuda", model="openai/whisper-large-v3-turbo"):
    return LocalWhisperProvider("w", {"type": "local_whisper", "device": device, "model": model})


def _close(a, b, tol=1e-9):
    return a is not None and math.isclose(a, b, rel_tol=tol)


# --- item 1: the penalty by length band ----------------------------------------------------------


class TestThePenaltyByLengthBand:
    def test_a_long_first_call_leaves_a_short_clips_penalty(self):
        # Johnny's falsifier: the first call after a load is 300 s and costs nothing beyond warm.
        p, untouched = _provider("cuda"), _provider("cuda")
        p._note_load("cuda", 0.8)
        p._note_run("cuda", 300.0, seconds_from_ratio(300, CUDA["warm"][">180"]), first=True)
        # The next 10 s clip after a load still pays the start penalty of its own band: the same
        # estimate beyond the load as on an object that measured nothing (both just loaded).
        assert p._calls_since_load == 0 and untouched._calls_since_load == 0
        assert _close(p.expected_seconds(10, loaded=True), untouched.expected_seconds(10, loaded=True))
        # (A 10 s clip reads the short-clip first-call pair, 0.7 s + 10 / 15.3 s: follow-up 13.)
        assert p.expected_seconds(10, loaded=True) > seconds_from_ratio(10, CUDA["warm"]["<=60"]) + 0.8
        # And the 300 s band itself took the measured 0.
        assert _close(p.expected_seconds(300, loaded=True), seconds_from_ratio(300, CUDA["warm"][">180"]))

    def test_the_start_penalty_is_one_second_in_every_band_on_cuda_and_zero_on_the_cpu(self):
        assert CUDA["penalty_s"] == {band: 1.0 for band in _LENGTH_BANDS}
        assert CPU["penalty_s"] == {band: 0.0 for band in _LENGTH_BANDS}

    def test_a_first_call_moves_only_its_own_band(self):
        p = _provider("cuda")
        warm = CUDA["warm"]["<=60"]
        p._note_run("cuda", 10.0, 10.0 / warm + 2.5, first=True)
        penalty = p.speed_figures("cuda")["penalty_s"]
        assert _close(penalty["<=60"], 2.5, 1e-6)
        for band in ("<=120", "<=180", ">180"):
            assert penalty[band] == CUDA["penalty_s"][band]
        assert set(p._measured["cuda"]["penalty_s"]) == {"<=60"}

    @pytest.mark.asyncio
    async def test_the_real_first_call_records_in_its_band(self, monkeypatch):
        p = gpu_whisper(monkeypatch, device="cuda")
        p._note_load("cuda", 0.5)
        p.model_loaded = True
        p._record_run(300.0, 300.0 / 15.9)
        assert set(p._measured["cuda"]["penalty_s"]) == {">180"}

    def test_a_reload_carries_the_penalty_band_by_band_and_keeps_the_new_objects_own(self):
        old, new = _provider("cuda"), _provider("cuda")
        old._note_run("cuda", 10.0, 10.0 / CUDA["warm"]["<=60"] + 3.0, first=True)
        old._note_run("cuda", 300.0, 300.0 / CUDA["warm"][">180"] + 0.5, first=True)
        new._note_run("cuda", 10.0, 10.0 / CUDA["warm"]["<=60"] + 2.0, first=True)
        old.hand_over_to(new)
        penalty = new.speed_figures("cuda")["penalty_s"]
        assert _close(penalty["<=60"], 2.0, 1e-6), "the new object's own measurement is kept"
        assert _close(penalty[">180"], 0.5, 1e-6), "the old object's other band is carried"
        assert penalty["<=120"] == CUDA["penalty_s"]["<=120"]

    def test_a_band_with_no_penalty_figure_is_unknown(self):
        p = _provider("cuda")
        p._measured["mps"] = {"warm": {"<=60": 20.0}}
        assert p.expected_seconds(10, device="mps", loaded=True) is None


# --- item 3: two start load figures ---------------------------------------------------------------


class TestTheFirstLoadAndTheReload:
    def test_the_start_figures_are_the_fresh_process_and_the_reload(self):
        assert CUDA["load_first_s"] == 12.8 and CUDA["load_reload_s"] == 0.8
        assert CPU["load_first_s"] == 9.0 and CPU["load_reload_s"] == 1.0
        assert "load_s" not in CUDA and "load_s" not in CPU

    def test_before_any_load_in_this_process_a_cold_estimate_counts_the_fresh_process_load(self):
        p = _provider("cuda")
        expected = CUDA["load_first_s"] + seconds_from_ratio(30, CUDA["warm"]["<=60"]) + CUDA["penalty_s"]["<=60"]
        assert _close(p.expected_seconds(30), expected)

    def test_after_a_load_on_any_provider_object_a_cold_estimate_counts_the_reload(self):
        other, p = _provider("cuda"), _provider("cuda")
        cold = p.expected_seconds(30)
        other._note_load("cuda", 11.0)
        assert _close(cold - p.expected_seconds(30), 12.8 - 0.8, 1e-9), "Forge #42-4: the reload, 0.8 s"
        assert p.speed_figures("cuda")["load_s"] == 0.8

    def test_the_first_load_measured_is_not_read_as_a_reload(self):
        p = _provider("cuda")
        p._note_load("cuda", 12.0)
        figures = p.speed_figures("cuda")
        assert figures["load_first_s"] == 12.0
        assert figures["load_reload_s"] == CUDA["load_reload_s"] and figures["load_s"] == CUDA["load_reload_s"]

    def test_a_measured_reload_wins_over_the_start_reload(self):
        p = _provider("cuda")
        p._note_load("cuda", 12.0)
        p._note_load("cuda", 0.65)
        assert p.speed_figures("cuda")["load_s"] == 0.65
        assert _close(p.expected_seconds(30, loaded=False),
                      0.65 + seconds_from_ratio(30, CUDA["warm"]["<=60"]) + CUDA["penalty_s"]["<=60"])

    def test_the_cpu_twins_reload_is_the_backend_logs_second(self):
        p = _provider("cpu")
        assert p.speed_figures("cpu")["load_s"] == CPU["load_first_s"]
        p._note_load("cpu", 9.5)
        assert p.speed_figures("cpu")["load_s"] == CPU["load_reload_s"]

    def test_a_load_that_finishes_on_the_old_object_after_the_hand_over_carries_its_kind(self):
        old, new = _provider("cuda"), _provider("cuda")
        old._note_load("cuda", 12.0)  # the first load of the process, on the old object
        old.hand_over_to(new)
        old.pipeline = object()
        old.model_loaded = True
        old._note_load("cuda", 0.7)  # a reload that finished there after the hand-over
        old._settle_finished_load()
        figures = new.speed_figures("cuda")
        assert figures["load_reload_s"] == 0.7 and figures["load_first_s"] == 12.0


# --- item 2: a holder that is loading has an ETA ---------------------------------------------------


def _gated_load(p, gate: threading.Event, load_s: float = 0.1):
    """The fake load waits in its thread until `gate` is set (the model is "loading" meanwhile)."""
    device = p.device

    def load():
        p.loads += 1
        assert gate.wait(BOUND), "the test never let the load finish"
        p.pipeline = object()
        p.model_loaded = True
        p._note_load(device, load_s)

    p._load_model = load


async def _peer(lease, expected_s):
    with serving_a_peer("node-guest"):
        await lease.acquire("peer node-guest", wait_s=None, expected_s=expected_s)


class TestALoadingHolderHasAnEta:
    @pytest.mark.asyncio
    async def test_a_preload_that_loads_has_the_loads_remaining_time(self, monkeypatch, fast):
        p = gpu_whisper(monkeypatch)
        gate = threading.Event()
        _gated_load(p, gate)
        load = p.speed_figures("cuda")["load_s"]  # nothing loaded in this process yet: 12.8 s
        assert load == 12.8
        preload = asyncio.ensure_future(p.ensure_model_loaded())
        try:
            await until(lambda: p.loads == 1 and lease_of(p) is not None and lease_of(p).state == "starting")
            eta = lease_of(p)._eta()
            assert eta is not None and load - 1.0 < eta <= load
        finally:
            gate.set()
            await asyncio.wait_for(preload, BOUND)

    @pytest.mark.asyncio
    async def test_a_peer_behind_a_loading_whisper_with_known_lengths_waits_and_is_granted(self, monkeypatch, fast):
        p = gpu_whisper(monkeypatch)
        gate = threading.Event()
        _gated_load(p, gate)
        preload = asyncio.ensure_future(p.ensure_model_loaded())
        guest = whisper("small")
        peer = None
        try:
            await until(lambda: p.loads == 1 and lease_of(p) is not None and lease_of(p).state == "starting")
            peer = asyncio.ensure_future(_peer(guest.lease, expected_s=5.0))
            await until(lambda: "whisper:small" in waiting())
            row = next(r for r in base_mod.card_queue() if r.get("alias") == "whisper:small")
            assert row["wait"]["known"] and row["wait"]["wait_s"] + 5.0 <= PEER_TRIP_S
            assert row["wait"]["wait_s"] > 12.8 - 1.0
        finally:
            gate.set()
            await asyncio.wait_for(preload, BOUND)
        # Loaded and idle: the swap is on and no owner waits, so the peer asks it to let go.
        await asyncio.wait_for(peer, BOUND)
        assert guest.lease.granted and p.unloads == 1

    @pytest.mark.asyncio
    async def test_a_call_whose_estimate_had_no_load_counts_the_load_while_it_loads(self, monkeypatch, fast):
        p = gpu_whisper(monkeypatch)
        gate = threading.Event()
        _gated_load(p, gate)
        # The door's estimate was made on a loaded model (no load in it); the model went meanwhile.
        warm_s = seconds_from_ratio(30, CUDA["warm"]["<=60"])
        work = p.expected_seconds(30, loaded=True)  # the first call after the load: warm + penalty
        load = p.speed_figures("cuda")["load_s"]
        call = asyncio.ensure_future(p.transcribe("a.wav", expected_s=warm_s, audio_s=30.0))
        try:
            await until(lambda: p.loads == 1)
            eta = lease_of(p)._eta()
            assert eta is not None and load - 1.0 + work < eta <= load + work
        finally:
            gate.set()
            await asyncio.wait_for(call, BOUND)
        assert lease_of(p)._eta() is None, "nothing runs and nothing loads: unknown, never 0"

    @pytest.mark.asyncio
    async def test_after_a_load_in_this_process_the_loading_eta_is_the_reload(self, monkeypatch, fast):
        _provider("cuda")._note_load("cuda", 12.0)
        p = gpu_whisper(monkeypatch)
        gate = threading.Event()
        _gated_load(p, gate)
        preload = asyncio.ensure_future(p.ensure_model_loaded())
        try:
            await until(lambda: p.loads == 1 and lease_of(p) is not None and lease_of(p).state == "starting")
            eta = lease_of(p)._eta()
            assert eta is not None and eta <= 0.8
        finally:
            gate.set()
            await asyncio.wait_for(preload, BOUND)

    @pytest.mark.asyncio
    async def test_a_call_of_unknown_length_leaves_the_loading_eta_unknown_and_a_peer_is_refused(
            self, monkeypatch, fast):
        p = gpu_whisper(monkeypatch)
        gate = threading.Event()
        _gated_load(p, gate)
        call = asyncio.ensure_future(p.transcribe("a.wav"))
        guest = whisper("small")
        try:
            await until(lambda: p.loads == 1)
            assert lease_of(p)._eta() is None
            with pytest.raises(LeaseRefusedForPeer):
                await asyncio.wait_for(_peer(guest.lease, expected_s=5.0), BOUND)
            assert "whisper:small" not in waiting()
        finally:
            gate.set()
            await asyncio.wait_for(call, BOUND)

    @pytest.mark.asyncio
    async def test_a_load_that_overruns_its_figure_is_unknown(self, monkeypatch, fast):
        p = gpu_whisper(monkeypatch)
        gate = threading.Event()
        _gated_load(p, gate)
        monkeypatch.setitem(wp._START_FIGURES["cuda"], "load_first_s", 0.01)
        preload = asyncio.ensure_future(p.ensure_model_loaded())
        try:
            await until(lambda: p.loads == 1 and lease_of(p) is not None and lease_of(p).state == "starting")
            await asyncio.sleep(0.05)
            assert lease_of(p)._eta() is None
        finally:
            gate.set()
            await asyncio.wait_for(preload, BOUND)


# --- item 4: the dead length probe -----------------------------------------------------------------


def test_the_dead_length_probe_is_gone():
    assert not hasattr(LocalWhisperProvider, "_get_audio_duration")
