"""A NeuralDeep call through the gateway leaves its roubles on the row.

NeuralDeep prices its own calls in RUB from the vendor's live list and puts
`cost_amount`, `cost_currency`, `cost_basis` and, when it could not price,
`cost_unpriced_reason` in its usage dict. Until 2026-09-28 the gateway threw
that report away and priced the call again with the dollar tables, by model
name — so a NeuralDeep alias whose model string happened to read like a
DeepSeek one was booked in dollars, and a RUB report reached no row at all.

The decision (Mike's call, 2026-09-28): a provider's own report wins whenever
its usage dict carries the key `cost_amount` — even when the value is None —
and otherwise the price follows the provider *type*, never the model name.
The daily ceiling of a vendor alias then counts the rows in the ceiling's own
currency that may have been debited — `charged` and `unknown`, never
`list_price_reference` — which admits a NeuralDeep alias it used to refuse as
unrated and bounds it in roubles.

The provider layer is stood in for by a fake `query_messages` returning what
the real one returns, the cost keys included; no network.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dpc_client_core.gateway import Gateway, GatewayError
from dpc_client_core.node_ledger import NodeLedger, usage_row
from tests.test_the_gateway_serves_only_the_two_lists_on_loopback import (
    NODE_ID,
    _service,
)

ND = "nd_qwen"
LOCAL = "llama_ds_named"
MESSAGES = [{"role": "user", "content": "hi"}]


class _NeuralDeep:
    """What the gateway reads off a NeuralDeep provider: its config, its model
    and the currency it bills in."""

    def __init__(self, model="qwen3.8-27b"):
        self.config = {"type": "neuraldeep", "model": model}
        self.model = model

    def billing_currency(self):
        return "RUB"

    def supports_vision(self):
        return False

    async def generate_with_tools(self, *args, **kwargs):
        raise AssertionError("the gateway speaks to the manager, never to the provider")


class _LocalCard:
    """A local llama-server alias whose GGUF happens to be named like a vendor model."""

    def __init__(self):
        self.config = {"type": "llamacpp_server", "model": "deepseek-v4-flash"}
        self.model = "deepseek-v4-flash"

    def supports_vision(self):
        return False

    async def generate_with_tools(self, *args, **kwargs):
        raise AssertionError("the gateway speaks to the manager, never to the provider")


def _gateway(tmp_path: Path, compute: dict, providers: dict, report: dict):
    """A Gateway over the loopback test's stand-in service, whose manager
    returns `report` beside the counts — the keys `query_messages` copies from
    the provider's own usage dict."""
    service = _service(tmp_path, compute, providers=providers)
    inner = service.llm_manager.query_messages

    async def query_messages(messages, **kwargs):
        result = await inner(messages, **kwargs)
        result.update(report)
        return result

    service.llm_manager.query_messages = query_messages
    ledger = NodeLedger(tmp_path / "ledger")
    return Gateway(service, ledger=ledger), ledger, service


#: A NeuralDeep alias is a vendor alias: money bounds it, so it stands under
#: `serving_vendor` with a ceiling high enough not to be the subject here.
ND_SERVED = {"serving_vendor": [ND], "vendor_quotas": {ND: 1000.0}}

PRICED_RUB = {
    "cost_amount": 0.0132, "cost_currency": "RUB",
    "cost_basis": "list_price_reference", "cost_price_list_at": "2026-09-28T00:00:00+00:00",
}


@pytest.mark.asyncio
async def test_a_neuraldeep_report_reaches_the_row_in_roubles(tmp_path):
    """(i) The provider's RUB amount, currency and basis are the row's."""
    gateway, ledger, _ = _gateway(
        tmp_path, ND_SERVED, {ND: _NeuralDeep()}, PRICED_RUB,
    )

    await gateway.complete(ND, "hi", messages=MESSAGES)

    (row,) = list(ledger.rows())
    assert row["cost_currency"] == "RUB"
    assert row["cost_amount"] == pytest.approx(0.0132)
    assert row["cost_basis"] == "list_price_reference"
    assert "cost_usd" not in row, "no writer of cost_usd is left"


@pytest.mark.asyncio
async def test_an_unpriced_neuraldeep_report_named_like_deepseek_stays_unpriced(tmp_path):
    """(ii) A NeuralDeep model string that reads like a DeepSeek one is not
    priced from the dollar tables: the provider said it could not price the
    call, and the row keeps that and its reason."""
    report = {"cost_amount": None, "cost_unpriced_reason": "model 'deepseek-v4-flash' is not in the price list"}
    gateway, ledger, _ = _gateway(
        tmp_path, ND_SERVED, {ND: _NeuralDeep("deepseek-v4-flash")}, report,
    )

    await gateway.complete(ND, "hi", messages=MESSAGES)

    (row,) = list(ledger.rows())
    assert row["cost_amount"] is None
    assert row["cost_currency"] is None
    assert row["cost_unpriced_reason"] == report["cost_unpriced_reason"]
    assert "cost_usd" not in row


@pytest.mark.asyncio
async def test_a_local_alias_named_like_a_vendor_model_costs_nothing_in_no_currency(tmp_path):
    """(iii) A llama-server card whose model string is `deepseek-v4-flash` is
    a local card: it is not pay-per-use and it has no dollars."""
    gateway, ledger, _ = _gateway(tmp_path, {"serving_local": [LOCAL]}, {LOCAL: _LocalCard()}, {})

    await gateway.complete(LOCAL, "hi", messages=MESSAGES)

    (row,) = list(ledger.rows())
    assert row["billing"] != "pay_per_use"
    assert row["cost_amount"] == 0.0
    assert row["cost_currency"] is None


def _charged(ledger, amount, *, currency="RUB", basis="charged", request_id):
    ledger.append(usage_row(
        request_id=request_id, caller=NODE_ID, caller_kind="gateway", alias=ND,
        model="qwen3.8-27b", route="local", prompt_tokens=1, completion_tokens=1,
        thinking_tokens=None, counts_source="engine", started_at=datetime.now(timezone.utc),
        duration_s=0.1, billing="pay_per_use",
        cost_amount=amount, cost_currency=currency, cost_basis=basis,
    ))


@pytest.mark.asyncio
async def test_a_neuraldeep_vendor_alias_is_admitted_and_stops_at_its_rouble_ceiling(tmp_path):
    """(vi) A NeuralDeep alias in `compute.serving_vendor` is no longer
    refused as unrated: its ceiling is in roubles, counted from charged RUB
    rows, and it stops there with the currency named in the refusal."""
    charged = dict(PRICED_RUB, cost_basis="charged", cost_amount=40.0)
    compute = {"serving_vendor": [ND], "vendor_quotas": {ND: 100.0}}
    gateway, ledger, service = _gateway(tmp_path, compute, {ND: _NeuralDeep()}, charged)

    await gateway.complete(ND, "hi", messages=MESSAGES)
    assert ledger.spent_today(ND, caller=NODE_ID, caller_kind="gateway", currency="RUB") == pytest.approx(40.0)

    # A dollar row and a reference row on the same alias do not move a rouble ceiling.
    _charged(ledger, 500.0, currency="USD", request_id="dollars")
    _charged(ledger, 500.0, basis="list_price_reference", request_id="reference")
    await gateway.complete(ND, "hi", messages=MESSAGES)

    _charged(ledger, 30.0, request_id="the-rest")
    with pytest.raises(GatewayError) as refused:
        await gateway.complete(ND, "hi", messages=MESSAGES)
    assert refused.value.status == 429 and refused.value.code == "insufficient_quota"
    assert "RUB" in refused.value.message and "$" not in refused.value.message
    assert len(service.calls) == 2, "the refused call must not reach the provider"
