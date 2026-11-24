"""ADR-052 L11 (slice S3e): a llama-server child's declared peak is the figure its own start admitted it
with, `_predicted_total_mib(admission, cache_type)` (weights + KV for its `n_ctx` and KV type +
overhead), recorded on the supervisor at launch as `predicted_vram_mib`. One attribute; no
measurement (per-process VRAM reads `[N/A]` under WDDM). A start that could not size the model has
none, which L11 reads as unknown. A Strata child's figure is the card less its own reserve
(`test_a_strata_child_declares_its_vram_peak_from_its_reserve.py`).
"""

import pytest

from dpc_client_core.managers import llama_server_supervisor as base_mod

from tests.test_one_local_engine_holds_the_card import _llama, _strata, card  # noqa: F401

# A 28 GiB-class card and the production model's shape (the ladder tests' figures): q8_0 at 262 144
# predicts 16031 + 9248 + 4608 = 29887 MiB.
WEIGHTS, DIMS, N_CTX = 16031, (17, 1024), 262144


def _sized(monkeypatch, total=32623):
    monkeypatch.setattr(base_mod, "_total_vram_mib", lambda: total)
    monkeypatch.setattr(base_mod, "_gguf_mib", lambda path: WEIGHTS)
    monkeypatch.setattr(base_mod, "_gguf_attention_kv_dims", lambda path: DIMS)


@pytest.mark.asyncio
async def test_an_explicit_kv_type_records_what_its_admission_predicted(monkeypatch):
    _sized(monkeypatch)
    sup = _llama(n_ctx=N_CTX)
    assert sup.predicted_vram_mib is None
    await sup.ensure_running()
    assert sup.predicted_vram_mib == 16031 + 9248 + 4608 == 29887
    await sup.stop()


@pytest.mark.asyncio
async def test_the_auto_ladder_records_the_rung_it_started_on(monkeypatch):
    _sized(monkeypatch, total=28672)  # q8_0 (29887) over the 26624 budget: q4_0 starts
    sup = _llama(n_ctx=N_CTX, cache_type_k=None, cache_type_v=None)
    sup.config.pop("cache_type_k", None)
    sup.config.pop("cache_type_v", None)
    sup._read_fit_memo = lambda: {}
    sup._write_fit_memo = lambda *a: None
    await sup.ensure_running()
    assert sup.predicted_vram_mib == 16031 + 4896 + 4608 == 25535
    await sup.stop()


@pytest.mark.asyncio
async def test_a_start_that_could_not_size_the_model_has_no_figure(monkeypatch):
    monkeypatch.setattr(base_mod, "_total_vram_mib", lambda: None)  # no nvidia-smi: no admission
    sup = _llama(n_ctx=N_CTX)
    await sup.ensure_running()
    assert sup.predicted_vram_mib is None
    await sup.stop()


@pytest.mark.asyncio
async def test_a_strata_child_records_the_card_less_its_reserve_not_a_model_admission(tmp_path, monkeypatch):
    """Forge #26 F1: Strata runs no admission; its figure is the card's total less the
    `--vram-reserve-mib` of its start config (1536 in the test base config), `declared_peak_of`."""
    _sized(monkeypatch)
    sup = _strata(tmp_path)
    await sup.ensure_running()
    assert sup.predicted_vram_mib == 32623 - 1536
    await sup.stop()
