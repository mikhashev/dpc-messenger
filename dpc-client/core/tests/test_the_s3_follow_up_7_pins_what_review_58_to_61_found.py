"""ADR-052 S3 follow-up 7: what the group-chat reviews #58 and #61 (Ark), #59 (Johnny) and #60 (Forge)
found in S3d (`c4590d19`) and the commits after it.

**A load figure per device and model** (Ark #61-1). Follow-up 6 made "a load has run" per device; a
second model on a device that has loaded another one read the reload figure (0.8 s on cuda) for a load
that reads weights no load has read. The key is now (device, model): the twin and a configured
`device: cpu` alias of the same model share it, another model does not.

**The twin checks the RAM in its own load** (Ark #61-3, the S3d note's "[unverified] ... not guarded"):
a twin seen warm at L12's seam and unloaded by its idle timer before the call loads cold; the check now
runs where the load runs, so the snapshot at the seam is no longer the only one.

**The INFO line says what L12 compared** (Ark #58-2, #58-3; Johnny #59-1, #59-2): behind a local engine
that stays on the card, the GPU's wait is not estimated (item 1a), so the line says "not compared"
rather than reading as a measurement; a contention factor of 1 says it applied no penalty.

**`let_peers_use_cpu_twin: 1`** (Ark #61-6): a JSON 1 turns it on, as the string "1" did.

**The agent tool's `queue_wait_s` default is the seed table's** (Johnny #59): one value in two
carriers, held by a comment until now.

Real supervisors faked at their seams, a real Whisper provider on a fake GPU and its real twin with a
faked model, as in tests 30-33; no torch, no model.
"""

import asyncio
import logging

import pytest

from dpc_client_core.dpc_agent.tools import transcribe as transcribe_tool
from dpc_client_core.providers import whisper_provider as wp
from dpc_client_core.providers.whisper_provider import (
    _START_FIGURES, LocalWhisperProvider, RoutedToCpu, transcribe_with_route)
from dpc_client_core.settings import _SEEDED_KEYS

from tests._card_lease_helpers import BOUND, fast, holding, nothing_left, until  # noqa: F401
from tests._whisper_lease_helpers import cpu_twin, gpu_whisper, host_ram, l12_settings
from tests.test_a_busy_strata_lets_short_files_on_the_cpu_with_the_contention_factor import a_turn_on, ask
from tests.test_music_swaps_out_an_idle_supervisor_when_nothing_waits import quick, running_strata  # noqa: F401
from tests.test_one_local_engine_holds_the_card import card  # noqa: F401

CUDA, CPU = _START_FIGURES["cuda"], _START_FIGURES["cpu"]
TURBO, SMALL = "openai/whisper-large-v3-turbo", "openai/whisper-small"


def _provider(device="cuda", model=TURBO, alias="w"):
    return LocalWhisperProvider(alias, {"type": "local_whisper", "device": device, "model": model})


# --- Ark #61-1: a load figure per device and model ---------------------------------------------------


class TestALoadFigurePerDeviceAndModel:
    def test_another_model_on_a_loaded_device_reads_its_first_load(self):
        turbo, small = _provider(model=TURBO), _provider(model=SMALL, alias="s")
        turbo._note_load("cuda", 12.0)
        assert turbo.speed_figures("cuda")["load_s"] == CUDA["load_reload_s"]
        assert small.speed_figures("cuda")["load_s"] == CUDA["load_first_s"], \
            "no load has read the small model's weights in this process"
        small._note_load("cuda", 3.0)
        assert small._measured["cuda"]["load_first_s"] == 3.0, "recorded as its own first load"

    def test_the_same_model_on_the_same_device_is_a_reload_for_any_object(self):
        first, again = _provider(), _provider(alias="again")  # a providers reload builds a new object
        first._note_load("cuda", 12.0)
        assert again.speed_figures("cuda")["load_s"] == CUDA["load_reload_s"]

    def test_the_twin_and_a_configured_cpu_alias_of_one_model_share_the_key(self):
        twin = _provider("cuda")._the_cpu_twin()
        configured = _provider("cpu", alias="on-cpu")
        twin._note_load("cpu", 9.0)
        assert configured.speed_figures("cpu")["load_s"] == CPU["load_reload_s"]
        assert _provider("cpu", model=SMALL, alias="small-cpu").speed_figures("cpu")["load_s"] == \
            CPU["load_first_s"]


# --- Ark #61-3: the twin checks the RAM where it loads -----------------------------------------------


def test_a_twin_that_loads_cold_checks_the_ram_in_its_load(monkeypatch):
    twin = _provider("cuda")._the_cpu_twin()
    host_ram(monkeypatch, 3000)
    with pytest.raises(RuntimeError, match="3000 MiB of host RAM available, 5120 MiB needed"):
        twin._load_model()  # raises before any import: no torch in this test
    assert not twin.model_loaded


def test_unreadable_ram_stops_a_twins_load(monkeypatch):
    twin = _provider("cuda")._the_cpu_twin()
    host_ram(monkeypatch, None)
    with pytest.raises(RuntimeError, match="could not be read"):
        twin._load_model()


@pytest.mark.asyncio
async def test_a_twin_unloaded_after_the_seam_is_refused_inside_its_own_turn(monkeypatch):
    """L12 saw the twin warm (no RAM check at the seam); its idle timer unloaded it before the call;
    the load's own check refuses, and section 3.5 turns the twin's failure into the refusal."""
    twin = _provider("cuda")._the_cpu_twin()
    host_ram(monkeypatch, 1000)

    class Seam:
        async def transcribe(self, audio_path, **kw):
            raise RoutedToCpu(twin, {"route": "cpu"}, if_it_fails=lambda reason: PermissionError(reason))

    with pytest.raises(PermissionError, match="1000 MiB of host RAM available, 5120 MiB needed"):
        await asyncio.wait_for(transcribe_with_route(Seam(), "f.wav", audio_s=30.0), BOUND)
    assert not twin.model_loaded


# --- Ark #58-2, #58-3, Johnny #59-1, #59-2: the INFO line says what was compared ---------------------


@pytest.mark.usefixtures("fast", "quick")
class TestTheLineSaysWhatL12Compared:
    @pytest.mark.asyncio
    async def test_behind_an_engine_that_stays_the_gpu_is_not_compared(self, tmp_path, monkeypatch, caplog):
        host_ram(monkeypatch, 64_000)
        await running_strata(tmp_path)
        p = gpu_whisper(monkeypatch)
        cpu_twin(monkeypatch, p)
        over, turn = await a_turn_on("flash")
        with caplog.at_level(logging.INFO, logger=wp.logger.name):
            result = await asyncio.wait_for(transcribe_with_route(p, "f", **ask(p, 120.0)), BOUND)
        assert result["device"] == "cpu"
        route = p.last_route
        assert route["gpu_s"] is None and route["gpu_compared"] is False
        assert "not compared" in route["why"] and "behind a local engine" in route["why"]
        assert "long (unknown)" not in route["why"], "a rule, not a measurement"
        assert any("not compared" in r.getMessage() for r in caplog.records)
        over.set()
        await asyncio.wait_for(turn, BOUND)
        await nothing_left()

    @pytest.mark.asyncio
    async def test_a_factor_of_one_says_it_applied_no_penalty(self, tmp_path, monkeypatch):
        host_ram(monkeypatch, 64_000)
        l12_settings(monkeypatch, cpu_contention_factor=1.0)
        await running_strata(tmp_path)
        p = gpu_whisper(monkeypatch)
        cpu_twin(monkeypatch, p)
        over, turn = await a_turn_on("flash")
        await asyncio.wait_for(transcribe_with_route(p, "f", **ask(p, 60.0)), BOUND)
        why = p.last_route["why"]
        assert "a busy Strata" in why and "cpu_contention_factor 1, no penalty" in why
        assert "x1 for a busy Strata" not in why
        over.set()
        await asyncio.wait_for(turn, BOUND)
        await nothing_left()


# --- Ark #61-6: a JSON 1 turns the peer setting on ---------------------------------------------------


@pytest.mark.parametrize("value, on", [(True, True), (1, True), ("1", True), ("true", True),
                                       (False, False), (0, False), (2, False), (1.0, False), (None, False)])
def test_let_peers_use_cpu_twin_reads_a_json_one(value, on):
    p = _provider()
    p.config["let_peers_use_cpu_twin"] = value
    assert p._lets_peers_use_the_twin() is on


# --- Johnny #59: one queue_wait_s default ------------------------------------------------------------


def test_the_agent_tools_queue_wait_default_is_the_seed_tables():
    assert transcribe_tool._DEFAULT_QUEUE_WAIT_S == float(_SEEDED_KEYS[("local_transcription", "queue_wait_s")][0])
