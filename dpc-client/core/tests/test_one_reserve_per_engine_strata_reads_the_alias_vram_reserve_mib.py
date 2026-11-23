"""One reserve per engine, from the engine's own config (ADR-052 L11, "One reserve per engine";
Mike's decision of 2026-11-23, #7 and #15: "one source of truth, the value that is configured").

* Strata: the alias's `vram_reserve_mib`, the value the engine itself honours as
  `--vram-reserve-mib`, is what the peer start gate (`strata_provider._peer_start_refusal`)
  subtracts from the card's total, and what L11's fit reads for a Strata child (S3e). The layers,
  in the order the start config applies them: the alias's typed field, then the alias's older
  `model_params.vram_reserve_mib`, then the base config's `--vram-reserve-mib`, and only then the
  2048 MiB fallback (`_DESKTOP_RESERVE_MIB`). The refusal names which number it used.
* llama-server has no engine reserve flag, so its reserve stays `_DESKTOP_RESERVE_MIB` = 2048.
* The two numbers are never summed.
"""

from __future__ import annotations

import asyncio

import pytest

from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.llama_server_supervisor import LlamaServerSupervisor
from dpc_client_core.managers.strata_supervisor import vram_reserve_of
from dpc_client_core.providers import StrataProvider
from dpc_client_core.providers.llamacpp_server_provider import _ACTIVE_SUPERVISORS
from tests.test_strata_supervisor import BASE_ARGS, _install

PEER = "peer-1"
CARD_MIB = 32623  # this machine's card, nvidia-smi memory.total
BASE_RESERVE = 1536  # BASE_ARGS' --vram-reserve-mib


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(base_mod, "DPC_HOME", tmp_path / "dpc-home")
    monkeypatch.setattr(base_mod, "_free_vram_mib", lambda **_: None)
    monkeypatch.setattr(base_mod, "_total_vram_mib", lambda: None)
    _ACTIVE_SUPERVISORS.clear()
    base_mod._CARD_CLAIMS.clear()
    yield
    _ACTIVE_SUPERVISORS.clear()
    base_mod._CARD_CLAIMS.clear()


def _card(monkeypatch, free, total=CARD_MIB):
    monkeypatch.setattr(base_mod, "_free_vram_mib", lambda **_: free)
    monkeypatch.setattr(base_mod, "_total_vram_mib", lambda: total)


def _strata(tmp_path, base_overrides=None, **alias) -> StrataProvider:
    root = _install(tmp_path, **(base_overrides or {}))
    return StrataProvider("flash", {
        "type": "strata", "alias": "flash", "model": "qwen3.8-flash-next-iq3_xxs",
        "strata_install_dir": str(root), "strata_base_config": "strata-base.json",
        "conversation_cache_mib": 10240, "context_window": 200000, **alias,
    })


def _no_start(sup):
    started = []

    async def ensure_running():
        started.append(True)
        return {}

    sup.ensure_running = ensure_running
    return started


def _args_without_reserve():
    args = list(BASE_ARGS)
    i = args.index("--vram-reserve-mib")
    del args[i:i + 2]
    return args


class TestTheLayers:
    def test_the_alias_typed_field_wins(self):
        assert vram_reserve_of({"vram_reserve_mib": 1152, "model_params": {"vram_reserve_mib": 900}},
                               list(BASE_ARGS)) == (1152, "the alias's vram_reserve_mib")

    def test_zero_is_a_configured_value_not_an_absence(self):
        assert vram_reserve_of({"vram_reserve_mib": 0}, list(BASE_ARGS))[0] == 0

    def test_then_the_aliass_model_params(self):
        assert vram_reserve_of({"model_params": {"vram_reserve_mib": 900}}, list(BASE_ARGS)) == (
            900, "the alias's model_params.vram_reserve_mib")

    def test_then_the_base_config(self):
        assert vram_reserve_of({}, list(BASE_ARGS)) == (
            BASE_RESERVE, "the base config's --vram-reserve-mib")

    def test_model_params_none_drops_the_base_flag_and_the_fallback_answers(self):
        # The start config drops the flag for a None in model_params, so the engine's own default
        # applies, which DPC does not know: the fallback.
        mib, source = vram_reserve_of({"model_params": {"vram_reserve_mib": None}}, list(BASE_ARGS))
        assert mib == base_mod._DESKTOP_RESERVE_MIB == 2048 and "fallback" in source

    def test_nothing_set_anywhere_is_the_2048_fallback(self):
        mib, source = vram_reserve_of({}, _args_without_reserve())
        assert mib == 2048 and "fallback" in source

    def test_an_unreadable_base_is_the_fallback(self):
        mib, source = vram_reserve_of({}, None)
        assert mib == 2048 and "fallback" in source


class TestEachEngineReadsItsOwn:
    def test_a_strata_supervisor_reads_its_alias_then_its_base(self, tmp_path):
        assert _strata(tmp_path, vram_reserve_mib=1152).supervisor.vram_reserve() == (
            1152, "the alias's vram_reserve_mib")

    def test_a_strata_supervisor_with_no_alias_value_reads_its_base_config(self, tmp_path):
        assert _strata(tmp_path).supervisor.vram_reserve() == (
            BASE_RESERVE, "the base config's --vram-reserve-mib")

    def test_a_strata_supervisor_whose_base_cannot_be_read_falls_back(self, tmp_path):
        p = _strata(tmp_path)
        (tmp_path / "Strata" / "strata-base.json").unlink()
        mib, source = p.supervisor.vram_reserve()
        assert mib == 2048 and "fallback" in source

    def test_llama_server_has_no_engine_flag_and_keeps_2048(self):
        sup = LlamaServerSupervisor("qwen27", {"gguf_path": "D:/models/q.gguf"})
        mib, source = sup.vram_reserve()
        assert mib == base_mod._DESKTOP_RESERVE_MIB == 2048
        assert "no engine reserve flag" in source


class TestThePeerGate:
    @pytest.mark.asyncio
    async def test_the_gate_subtracts_the_aliass_reserve_and_names_it(self, tmp_path, monkeypatch):
        p = _strata(tmp_path, vram_reserve_mib=1152)
        _card(monkeypatch, free=31000)  # enough for total - 2048 = 30575, not for total - 1152 = 31471
        started = _no_start(p.supervisor)

        reason = await asyncio.wait_for(p.peer_refusal(PEER), 5)

        assert f"31000 MiB of VRAM free, {CARD_MIB - 1152} MiB needed" in reason
        assert "less 1152 MiB (the alias's vram_reserve_mib)" in reason
        assert started == []

    @pytest.mark.asyncio
    async def test_the_two_numbers_are_never_summed(self, tmp_path, monkeypatch):
        p = _strata(tmp_path, vram_reserve_mib=1152)
        _card(monkeypatch, free=CARD_MIB - 1152)  # exactly the alias's bar
        _no_start(p.supervisor)

        reason = await asyncio.wait_for(p.peer_refusal(PEER), 5)

        assert "loading" in reason, "free == total - 1152 starts it; a summed 1152 + 2048 would refuse"
        await asyncio.wait_for(p.supervisor.peer_start_task, 5)

    @pytest.mark.asyncio
    async def test_with_no_reserve_anywhere_the_gate_says_it_used_the_fallback(self, tmp_path, monkeypatch):
        p = _strata(tmp_path, base_overrides={"args": _args_without_reserve()})
        _card(monkeypatch, free=20000)
        _no_start(p.supervisor)

        reason = await asyncio.wait_for(p.peer_refusal(PEER), 5)

        assert f"20000 MiB of VRAM free, {CARD_MIB - 2048} MiB needed" in reason
        assert "less 2048 MiB (the 2048 MiB fallback" in reason
