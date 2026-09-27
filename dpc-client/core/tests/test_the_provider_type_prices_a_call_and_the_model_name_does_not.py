"""The provider type prices a call; the model name does not.

`pricing._resolve_pay_model` resolved a call's price from its model name first
and from substrings of its alias after that, with no word about which vendor
ran it. So a llama-server card whose GGUF was named `deepseek-v4-flash` was
booked as a paid DeepSeek call, and a NeuralDeep call reached the dollar
tables whenever its model string read like one of theirs.

The decided order (Mike's call, 2026-09-28): a provider's own report first —
its usage dict carrying the key `cost_amount`, even as None — and otherwise
the provider type: `deepseek`/`zai` price from the USD tables, a local type
costs 0.0 in no currency, `neuraldeep` never resolves to dollars, and name
matching is left for the types with no pricing of their own. `billing`
follows the same rule. The agent path, the peer door and the burn series
each carry the result, and an unpriced call is a null, never a zero.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter
from dpc_client_core.dpc_agent import loop, pricing
from dpc_client_core.node_ledger import NodeLedger
from dpc_client_core.providers.base import AIProvider
from tests.test_p2p_coordinator import make_coordinator

MESSAGES = [{"role": "user", "content": "hello"}]
AT = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
COUNTS = dict(prompt_tokens=1000, completion_tokens=500)


class _Reporting(AIProvider):
    """A provider that reports its counts, and a price when it has one."""

    def __init__(self, alias, type_, model, extra=None):
        super().__init__(alias, {"type": type_, "model": model})
        self._report = dict({"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500},
                            **(extra or {}))

    async def generate_response(self, prompt, **kwargs):
        self._record_last_usage(self._report)
        return "answer"


def _adapter(provider, ledger):
    manager = SimpleNamespace(
        token_count_manager=None, providers={provider.alias: provider},
        agent_provider=None, default_provider=provider.alias,
    )
    return DpcLlmAdapter(manager, provider_alias=provider.alias, caller="agent_t", ledger=ledger)


# --- pricing itself ---------------------------------------------------------


def test_a_local_type_named_like_a_vendor_model_is_free_and_not_pay_per_use():
    """(iii)"""
    for type_ in ("llamacpp_server", "ollama", "local_whisper"):
        priced = pricing.price_call(provider_type=type_, alias="deepseek_flash", model="deepseek-v4-flash",
                            at=AT, **COUNTS)
        assert (priced["cost_amount"], priced["cost_currency"]) == (0.0, None), type_
        assert priced["billing"] != "pay_per_use", type_
        assert pricing.get_billing_model("deepseek_flash", "deepseek-v4-flash", provider_type=type_) != "pay_per_use"


def test_a_neuraldeep_type_never_reaches_the_dollar_tables():
    priced = pricing.price_call(provider_type="neuraldeep", alias="nd_deepseek", model="deepseek-v4-flash",
                        at=AT, **COUNTS)
    assert priced["cost_amount"] is None and priced["cost_currency"] is None
    assert priced["cost_unpriced_reason"]


def test_a_report_carrying_cost_amount_wins_even_when_it_is_none():
    """(ii) at the pricing layer: presence of the key, not its value."""
    report = {"cost_amount": None, "cost_unpriced_reason": "not in the price list"}
    priced = pricing.price_call(provider_type="deepseek", alias="ds", model="deepseek-v4-flash",
                        at=AT, reported=report, **COUNTS)
    assert priced["cost_amount"] is None and priced["cost_currency"] is None
    assert priced["cost_unpriced_reason"] == "not in the price list"


def test_the_typed_vendors_still_price_in_dollars_and_an_untyped_alias_by_name():
    for type_, model in (("deepseek", "deepseek-v4-flash"), ("zai", "glm-4.7"), (None, "deepseek-v4-pro"),
                         ("openai_compatible", "deepseek-v4-pro")):
        priced = pricing.price_call(provider_type=type_, alias="a", model=model, at=AT, **COUNTS)
        assert priced["cost_currency"] == "USD" and priced["cost_amount"] > 0, type_
        assert (priced["cost_basis"], priced["billing"]) == ("charged", "pay_per_use"), type_


# --- the agent path ---------------------------------------------------------


@pytest.mark.asyncio
async def test_an_agent_call_on_a_local_card_named_deepseek_costs_nothing(tmp_path):
    """(iii) on the row the agent path writes."""
    ledger = NodeLedger(tmp_path / "ledger")
    provider = _Reporting("llama_ds", "llamacpp_server", "deepseek-v4-flash")

    await _adapter(provider, ledger).chat(MESSAGES)

    (row,) = list(ledger.rows())
    assert row["billing"] != "pay_per_use"
    assert (row["cost_amount"], row["cost_currency"]) == (0.0, None)


@pytest.mark.asyncio
async def test_an_agent_call_on_neuraldeep_keeps_the_providers_roubles(tmp_path):
    ledger = NodeLedger(tmp_path / "ledger")
    provider = _Reporting("nd", "neuraldeep", "deepseek-v4-flash", {
        "cost_amount": 0.42, "cost_currency": "RUB", "cost_basis": "charged",
    })

    _msg, usage = await _adapter(provider, ledger).chat(MESSAGES)

    (row,) = list(ledger.rows())
    assert (row["cost_amount"], row["cost_currency"], row["cost_basis"]) == (0.42, "RUB", "charged")
    assert row["billing"] == "pay_per_use"
    assert usage.get("cost", 0) == 0, "roubles are not the loop's dollar figure"


@pytest.mark.asyncio
async def test_an_unpriced_neuraldeep_call_is_null_on_the_row_and_in_the_burn_series(tmp_path):
    """(ii) on the agent path and (vii): the task's cost is null, not 0."""
    ledger = NodeLedger(tmp_path / "ledger")
    provider = _Reporting("nd", "neuraldeep", "deepseek-v4-flash", {
        "cost_amount": None, "cost_unpriced_reason": "no price list: fetch failed and nothing cached",
    })

    _msg, usage = await _adapter(provider, ledger).chat(MESSAGES)

    (row,) = list(ledger.rows())
    assert row["cost_amount"] is None and row["cost_currency"] is None
    assert row["cost_unpriced_reason"].startswith("no price list")

    accumulated = {}
    loop.accumulate_call_usage(accumulated, usage)
    fields = loop.task_cost_fields(accumulated)
    assert fields["cost_amount"] is None, "an unpriced task is null, not a zero"
    assert fields["cost_currency"] is None and fields["cost_unpriced_calls"] == 1


def test_a_task_sums_per_currency_and_never_adds_two():
    accumulated = {}
    for usage in (
        {"cost_amount": 0.01, "cost_currency": "USD", "cost_basis": "charged"},
        {"cost_amount": 0.02, "cost_currency": "USD", "cost_basis": "charged"},
    ):
        loop.accumulate_call_usage(accumulated, dict(usage, prompt_tokens=1, completion_tokens=1, total_tokens=2))
    fields = loop.task_cost_fields(accumulated)
    assert (fields["cost_amount"], fields["cost_currency"], fields["cost_basis"]) == (
        pytest.approx(0.03), "USD", "charged")

    loop.accumulate_call_usage(accumulated, {"cost_amount": 5.0, "cost_currency": "RUB", "cost_basis": "charged"})
    fields = loop.task_cost_fields(accumulated)
    assert fields["cost_amount"] is None and fields["cost_currency"] is None
    assert fields["cost_by_currency"] == {"USD": pytest.approx(0.03), "RUB": 5.0}


# --- the peer door (host side) ----------------------------------------------


@pytest.mark.asyncio
async def test_a_served_neuraldeep_call_writes_the_providers_roubles_on_the_hosts_row(tmp_path):
    coord, svc = make_coordinator()
    coord._ledger = NodeLedger(tmp_path / "ledger")
    svc.firewall.can_request_inference.return_value = True
    svc.firewall.compute_serving_alias = "nd"
    svc.llm_manager.providers = {"nd": SimpleNamespace(config={"type": "neuraldeep", "model": "qwen3.8-27b"})}
    svc.llm_manager.query = AsyncMock(return_value={
        "response": "ok", "model": "qwen3.8-27b", "prompt_tokens": 100, "response_tokens": 50,
        "cost_amount": 0.07, "cost_currency": "RUB", "cost_basis": "list_price_reference",
    })

    await coord.handle_inference_request("peer-1", "req-1", "hello")

    (row,) = list(coord._ledger.rows())
    assert (row["cost_amount"], row["cost_currency"], row["cost_basis"]) == (0.07, "RUB", "list_price_reference")
    sent = svc.p2p_manager.send_message_to_peer.call_args[0][1]["payload"]
    assert not [key for key in sent if key.startswith("cost")], "the host's own cost does not travel"


@pytest.mark.asyncio
async def test_a_served_call_on_a_local_card_named_deepseek_costs_the_host_nothing(tmp_path):
    coord, svc = make_coordinator()
    coord._ledger = NodeLedger(tmp_path / "ledger")
    svc.firewall.can_request_inference.return_value = True
    svc.firewall.compute_serving_alias = "llama_ds"
    svc.llm_manager.providers = {
        "llama_ds": SimpleNamespace(config={"type": "llamacpp_server", "model": "deepseek-v4-flash"}),
    }
    svc.llm_manager.query = AsyncMock(return_value={
        "response": "ok", "model": "deepseek-v4-flash", "prompt_tokens": 100, "response_tokens": 50,
    })

    await coord.handle_inference_request("peer-1", "req-1", "hello")

    (row,) = list(coord._ledger.rows())
    assert row["billing"] != "pay_per_use"
    assert (row["cost_amount"], row["cost_currency"]) == (0.0, None)
