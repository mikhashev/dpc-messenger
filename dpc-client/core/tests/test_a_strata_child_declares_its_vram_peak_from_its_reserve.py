"""ADR-052 L11, Forge #26 F1 (follow-up 11d): a Strata child's declared VRAM peak.

Strata holds no per-model VRAM figure: its engine grows the expert cache until only `--vram-reserve-mib`
is free on the card. So the honest declared peak is the card's total less the reserve the start config
gives the engine (`declared_peak_of`), read when the launch writes that config, and recorded by the
base's `_launch` as `predicted_vram_mib` once the child answers `/props`. A start with no honest number
keeps None, and L11 stays `unknown` for it: the total cannot be read (no nvidia-smi), or the start
config gives the engine no reserve (the launcher's or the engine's own default applies, which DPC does
not know).

Each Strata here starts through its real `ensure_running` and `_launch`, faked at the supervisor's own
seams (`test_one_local_engine_holds_the_card.py`).
"""

import json
import logging

import pytest

from dpc_client_core.managers import card_lease
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.card_lease import _fit_now, _LeaseWaiter
from dpc_client_core.managers.strata_supervisor import StrataSupervisor, declared_peak_of

from tests._card_lease_helpers import whisper
from tests.test_one_local_engine_holds_the_card import _fake_child, _strata, card  # noqa: F401
from tests.test_strata_supervisor import BASE_ARGS, _install

TOTAL = 32623


def _without_the_reserve(args):
    i = args.index("--vram-reserve-mib")
    return args[:i] + args[i + 2:]


def _fit_beside(free=30000, total=TOTAL):
    contender = whisper("turbo", vram_mib=4447)
    waiter = _LeaseWaiter(contender.lease, "dictation", None, True, None, vram_mib=4447)
    reserve, source = card_lease._reserve_of([sup for sup, _state in base_mod._live_claims()])
    return _fit_now(waiter, {"free": free, "total": total, "reserve": reserve, "reserve_source": source})


class TestTheFigure:
    def test_the_total_less_the_reserve_the_start_gives(self):
        assert declared_peak_of(TOTAL, BASE_ARGS) == (TOTAL - 1536, f"the card's {TOTAL} MiB less "
                                                      "--vram-reserve-mib 1536")

    def test_a_reserve_of_zero_is_strata_leaving_nothing(self):
        assert declared_peak_of(TOTAL, ["--vram-reserve-mib", "0"])[0] == TOTAL

    def test_a_reserve_given_twice_takes_the_larger_peak(self):
        assert declared_peak_of(TOTAL, ["--vram-reserve-mib", "1152", "--x", "--vram-reserve-mib", "2048"])[0] \
            == TOTAL - 1152

    @pytest.mark.parametrize("args", [[], ["--vram-reserve-mib"], ["--vram-reserve-mib", "--kv"],
                                      ["--vram-reserve-mib", "-5"], ["--vram-reserve-mib", "1.5"],
                                      ["--vram-reserve-mib", "nan"], ["--vram-reserve-mib", "inf"]])
    def test_no_valid_reserve_is_no_number(self, args):
        peak, why = declared_peak_of(TOTAL, args)
        assert peak is None and "--vram-reserve-mib" in why

    @pytest.mark.parametrize("total", [None, 0, True])
    def test_no_total_is_no_number(self, total):
        assert declared_peak_of(total, BASE_ARGS) == (None, "the card's total VRAM could not be read at the start")

    def test_a_reserve_as_large_as_the_card_is_no_number(self):
        assert declared_peak_of(1024, ["--vram-reserve-mib", "1024"])[0] is None


class TestTheLaunchRecordsIt:
    @pytest.mark.asyncio
    async def test_the_alias_reserve_written_in_the_start_config_is_the_one_used(self, tmp_path, monkeypatch, caplog):
        """Mike's Strata aliases set `vram_reserve_mib` 1152: the typed field replaces the base config's 1536 in
        the start config, and that is the figure the peak is taken from."""
        monkeypatch.setattr(base_mod, "_total_vram_mib", lambda: TOTAL)
        strata = _strata(tmp_path)
        strata.config["vram_reserve_mib"] = 1152
        with caplog.at_level(logging.INFO):
            await strata.ensure_running()
        args = json.loads(strata.start_config_used.read_text(encoding="utf-8"))["args"]
        assert args[args.index("--vram-reserve-mib") + 1] == "1152"
        assert strata.predicted_vram_mib == TOTAL - 1152
        assert (f"strata[flash]: declared VRAM peak {TOTAL - 1152} MiB (the card's {TOTAL} MiB less "
                "--vram-reserve-mib 1152)") in caplog.messages

    @pytest.mark.asyncio
    async def test_a_resident_with_a_declared_peak_is_decided_by_the_arithmetic_not_unknown(self, tmp_path, monkeypatch):
        """No longer `unknown`: the declared check decides, and a free reading, high or missing, cannot
        turn it into `fits` (the live veto is never what lets a contender in beside Strata)."""
        monkeypatch.setattr(base_mod, "_total_vram_mib", lambda: TOTAL)
        strata = _strata(tmp_path)
        strata.config["vram_reserve_mib"] = 1152
        await strata.ensure_running()
        for free in (TOTAL, None):
            fit = _fit_beside(free=free)
            assert fit["outcome"] == "does_not_fit" and fit["need"] == 4447 + TOTAL
            assert fit["reserve"] == 1152 and fit["peak"] == 4447

    @pytest.mark.asyncio
    async def test_a_start_config_with_no_reserve_has_no_peak_and_the_fit_stays_unknown(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(base_mod, "_total_vram_mib", lambda: TOTAL)
        strata = StrataSupervisor("flash", {
            "strata_install_dir": str(_install(tmp_path, args=_without_the_reserve(list(BASE_ARGS)))),
            "strata_base_config": "strata-base.json",
            "conversation_cache_mib": 10240,
            "engine_watch_interval_s": 3600,
            "vram_release_timeout_s": 0.05,
        })
        _fake_child(strata)
        with caplog.at_level(logging.INFO):
            await strata.ensure_running()
        assert strata.props and strata.predicted_vram_mib is None
        assert any("no declared VRAM peak for this start: the start args give the engine no "
                   "--vram-reserve-mib" in m for m in caplog.messages)
        fit = _fit_beside()
        assert fit["outcome"] == "unknown" and fit["why"] == "strata 'flash' has no declared peak"

    @pytest.mark.asyncio
    async def test_a_card_whose_total_cannot_be_read_has_no_peak(self, tmp_path):
        strata = _strata(tmp_path)  # the `card` fixture reads no total, as AMD, Intel and macOS have none
        await strata.ensure_running()
        assert strata.props and strata.predicted_vram_mib is None
        assert _fit_beside(total=None)["outcome"] == "unknown"

    @pytest.mark.asyncio
    async def test_each_start_is_sized_again(self, tmp_path, monkeypatch):
        total = {"mib": TOTAL}
        monkeypatch.setattr(base_mod, "_total_vram_mib", lambda: total["mib"])
        strata = _strata(tmp_path)
        await strata.ensure_running()
        assert strata.predicted_vram_mib == TOTAL - 1536
        await strata.stop()
        total["mib"] = None
        await strata.ensure_running()
        assert strata.predicted_vram_mib is None
