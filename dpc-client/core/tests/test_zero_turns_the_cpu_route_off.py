"""ADR-052 L12, Mike's decision of 2026-11-24 (Q3; Forge #60): `cpu_max_s = 0` or `cpu_max_audio_s =
0` turns the CPU route off, as `idle_unload_s` 0 means never and `queue_wait_s` 0 means do not wait.

- The settings reader and the provider's own defaults path read 0 as 0 (off), never as the default;
  a value below 0, unreadable or not finite still reads as the default. `cpu_contention_factor` is a
  multiplier, not a switch: 0 there still reads as the default.
- The seeded `config.ini` line says "0 = never use the CPU" for both keys.
- With the route off every call that L12 is asked about goes to the line, and the INFO line says
  why ("CPU route off: cpu_max_s is 0"). A free card still gives the GPU at once (point 1).
- An agent's transcription inside its own turn on a local alias (section 3.5) is refused at once,
  in words that say the CPU route is off; no place is taken.
- A peer the owner lets onto the twin (`let_peers_use_cpu_twin`) does not reach it either.

Real supervisors faked at their seams, a real Whisper provider on a fake GPU and its real twin with a
faked model.
"""

import asyncio
import logging
import time

import pytest

from dpc_client_core.managers.card_lease import (
    LeaseRefusedForPeer,
    LeaseWaitedTooLong,
    LeaseWouldWaitOnItself,
)
from dpc_client_core.managers.llama_server_supervisor import serving_a_peer
from dpc_client_core.providers.whisper_provider import cpu_route_settings, transcribe_with_route
from dpc_client_core.settings import _SEEDED_KEYS, Settings

from tests._card_lease_helpers import BOUND, fast, nothing_left, waiting  # noqa: F401
from tests._voice_cascade_helpers import busy_music  # noqa: F401
from tests._whisper_lease_helpers import cpu_twin, gpu_whisper, host_ram, l12_settings
from tests.test_a_busy_strata_lets_short_files_on_the_cpu_with_the_contention_factor import a_turn_on
from tests.test_an_agents_transcription_inside_its_own_turn import in_its_own_turn
from tests.test_music_swaps_out_an_idle_supervisor_when_nothing_waits import quick, running_strata  # noqa: F401
from tests.test_one_local_engine_holds_the_card import card  # noqa: F401

KEYS = {"cpu_max_s": 600.0, "cpu_max_audio_s": 300.0, "cpu_contention_factor": 1.0}


def ask(p, audio_s, **kw):
    return {"purpose": "dictation", "wait_s": BOUND, "expected_s": p.expected_seconds(audio_s),
            "audio_s": audio_s, **kw}


class TestTheSettings:
    @pytest.mark.parametrize("key", ["cpu_max_s", "cpu_max_audio_s"])
    def test_0_in_the_file_reads_as_off_not_as_the_default(self, tmp_path, key):
        settings = Settings(tmp_path)
        settings._config.set("local_transcription", key, "0")
        assert settings.get_local_transcription_cpu_route() == {**KEYS, key: 0.0}

    @pytest.mark.parametrize("raw", ["-5", "nan", "inf", "soon", ""])
    def test_below_0_unreadable_or_not_finite_still_reads_as_the_default(self, tmp_path, raw):
        settings = Settings(tmp_path)
        settings._config.set("local_transcription", "cpu_max_s", raw)
        settings._config.set("local_transcription", "cpu_max_audio_s", raw)
        assert settings.get_local_transcription_cpu_route() == KEYS

    def test_a_contention_factor_of_0_is_not_a_switch(self, tmp_path):
        settings = Settings(tmp_path)
        settings._config.set("local_transcription", "cpu_contention_factor", "0")
        assert settings.get_local_transcription_cpu_contention_factor() == 1.0

    @pytest.mark.parametrize("key", ["cpu_max_s", "cpu_max_audio_s"])
    def test_the_seeded_line_says_0_never_uses_the_cpu(self, tmp_path, key):
        assert "0 = never use the CPU" in _SEEDED_KEYS[("local_transcription", key)][1]
        (tmp_path / "config.ini").write_text("[local_transcription]\nlazy_loading = true\n")
        Settings(tmp_path)
        line = next(line for line in (tmp_path / "config.ini").read_text().splitlines()
                    if "0 = never use the CPU" in line and key in line)
        assert line.startswith(f"{key} = ")

    def test_the_providers_defaults_path_reads_0_as_off(self, monkeypatch):
        l12_settings(monkeypatch, cpu_max_s=0, cpu_max_audio_s="0", cpu_contention_factor=0)
        assert cpu_route_settings() == {**KEYS, "cpu_max_s": 0.0, "cpu_max_audio_s": 0.0}
        l12_settings(monkeypatch, cpu_max_s=-1, cpu_max_audio_s=True, cpu_contention_factor="x")
        assert cpu_route_settings() == KEYS


@pytest.mark.usefixtures("fast", "quick")
class TestTheRoute:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", ["cpu_max_s", "cpu_max_audio_s"])
    async def test_every_call_goes_to_the_line_and_the_log_says_why(self, tmp_path, monkeypatch, caplog, key):
        host_ram(monkeypatch, 64_000)
        l12_settings(monkeypatch, **{key: 0})
        await running_strata(tmp_path)
        p = gpu_whisper(monkeypatch)
        twin = cpu_twin(monkeypatch, p)
        over, turn = await a_turn_on("flash")
        with caplog.at_level(logging.INFO), pytest.raises(LeaseWaitedTooLong):
            await asyncio.wait_for(transcribe_with_route(p, "f", **ask(p, 30.0, wait_s=0.1)), BOUND)
        assert p.last_route["route"] == "line"
        assert p.last_route["why"].startswith(f"CPU route off: {key} is 0")
        assert any(f"CPU route off: {key} is 0" in r.getMessage() for r in caplog.records)
        assert twin.ran == [] and p._cpu_twin is twin and not twin.model_loaded
        over.set()
        await asyncio.wait_for(turn, BOUND)
        await nothing_left()

    @pytest.mark.asyncio
    async def test_a_free_card_still_gives_the_gpu_at_once(self, monkeypatch):
        host_ram(monkeypatch, 64_000)
        l12_settings(monkeypatch, cpu_max_s=0)
        p = gpu_whisper(monkeypatch)
        assert await asyncio.wait_for(transcribe_with_route(p, "f", **ask(p, 30.0)), BOUND) == {"text": "f"}
        assert p.last_route["route"] == "gpu"
        await nothing_left()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", ["cpu_max_s", "cpu_max_audio_s"])
    async def test_inside_its_own_turn_it_is_refused_at_once_with_the_reason(self, tmp_path, monkeypatch, key):
        host_ram(monkeypatch, 64_000)
        l12_settings(monkeypatch, **{key: 0})
        _strata, events = await running_strata(tmp_path)
        p = gpu_whisper(monkeypatch)
        the_ask = {"purpose": "agent tool", "wait_s": 1790.0, "audio_s": 30.0,
                   "expected_s": p.expected_seconds(30.0)}
        started = time.monotonic()
        with pytest.raises(LeaseWouldWaitOnItself) as refused:
            await in_its_own_turn(lambda: transcribe_with_route(p, "f", **the_ask))
        assert time.monotonic() - started < 0.5
        text = str(refused.value)
        assert text.startswith("Not transcribed: this turn runs on the local model 'flash', which holds the card")
        assert f"the CPU route is off ([local_transcription] {key} is 0)" in text
        assert text.endswith("so nothing was queued.")
        assert refused.value.holder_alias == "flash"
        assert p.last_route["route"] == "line" and f"CPU route off: {key} is 0" in p.last_route["why"]
        assert p.loads == 0 and events == [] and waiting() == [] and p._cpu_twin is None
        await nothing_left()

    @pytest.mark.asyncio
    async def test_a_peer_the_owner_lets_onto_the_twin_does_not_reach_it(self, monkeypatch, busy_music):
        host_ram(monkeypatch, 64_000)
        l12_settings(monkeypatch, cpu_max_s=0)
        p = gpu_whisper(monkeypatch)
        p.config["let_peers_use_cpu_twin"] = True
        twin = cpu_twin(monkeypatch, p)
        with serving_a_peer("node-guest"):
            with pytest.raises(LeaseRefusedForPeer):
                await asyncio.wait_for(transcribe_with_route(
                    p, "f", purpose="peer node-guest", wait_s=None, expected_s=p.expected_seconds(20.0),
                    audio_s=20.0), BOUND)
        assert p.last_route["why"].startswith("CPU route off: cpu_max_s is 0") and twin.ran == []
        busy_music.set()
        await nothing_left()
