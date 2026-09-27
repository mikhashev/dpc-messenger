"""The provider list names the currency each vendor alias's ceiling counts in.

Since 2026-09-28 the gateway and the peer door compare `compute.vendor_quotas`
with rows in the currency the serving provider bills in
(`pricing.vendor_ceiling_currency`): roubles for NeuralDeep, dollars for
DeepSeek, and no currency at all for an alias this node cannot price, which
both doors refuse as unrated. The Inference Sharing tab labels the ceiling
input from this field, so the tab and the doors read the number the same way;
the mapping is not repeated in TypeScript.

Local rows carry no such field — a local alias has no money ceiling.
"""

from __future__ import annotations

from types import MethodType, SimpleNamespace

import pytest

from dpc_client_core.service import CoreService


class _Provider:
    def __init__(self, provider_type, model, currency=None):
        self.config = {"type": provider_type, "model": model}
        self.model = model
        self._currency = currency

    def billing_currency(self):
        return self._currency

    def supports_vision(self) -> bool:
        return False


def _fake_service(providers: dict):
    fake = SimpleNamespace(
        llm_manager=SimpleNamespace(
            providers=providers,
            default_provider="",
            vision_provider="",
            voice_provider="",
            agent_provider="",
            get_context_window=lambda model: 4096,
        ),
        _provider_supports_voice=lambda provider: False,
    )
    fake._provider_rows = MethodType(CoreService._provider_rows, fake)
    return fake


@pytest.mark.asyncio
async def test_each_vendor_row_carries_its_ceiling_currency_and_a_local_row_none():
    fake = _fake_service({
        "qwen 3.8 27b ND": _Provider("neuraldeep", "qwen3.8-27b", currency="RUB"),
        "ds_flash": _Provider("deepseek", "deepseek-v4-flash", currency="USD"),
        "unrated_vendor": _Provider("openai_compatible", "a-model-no-table-prices"),
        "llama_local": _Provider("llamacpp_server", "deepseek-v4-flash"),
    })

    rows = {p["alias"]: p for p in (await CoreService.get_providers_list(fake))["providers"]}

    assert rows["qwen 3.8 27b ND"]["ceiling_currency"] == "RUB"
    assert rows["ds_flash"]["ceiling_currency"] == "USD"
    assert "ceiling_currency" in rows["unrated_vendor"]
    assert rows["unrated_vendor"]["ceiling_currency"] is None
    assert "ceiling_currency" not in rows["llama_local"]
