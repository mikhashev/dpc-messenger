"""The vendor ceiling counts every row that may have been charged.

Until this change `NodeLedger.spent_today` added only rows whose `cost_basis`
was `charged`. NeuralDeep writes `unknown` when its wallet endpoint cannot be
read, so on exactly the day nobody can tell whether the calls were debited,
the ceiling read zero and let every call through. The rule now (Mike's call,
2026-09-28): `charged` and `unknown` both count; only `list_price_reference`
— a price quoted for a call a subscription covered — stays out. Fail-closed:
a row that may have cost money is money until it is shown not to be.
"""

import pytest

from dpc_client_core.gateway import GatewayError
from dpc_client_core.node_ledger import NodeLedger
from tests.test_a_neuraldeep_call_through_the_gateway_leaves_its_roubles_on_the_row import (
    ND,
    MESSAGES,
    PRICED_RUB,
    _charged,
    _gateway,
    _NeuralDeep,
)
from tests.test_a_usage_row_carries_its_cost_in_a_named_currency import NOON, _row
from tests.test_the_gateway_serves_only_the_two_lists_on_loopback import NODE_ID


def test_an_unknown_row_counts_toward_the_ceiling_and_a_reference_row_does_not(tmp_path):
    ledger = NodeLedger(tmp_path / "ledger")
    ledger.append(_row(request_id="unknown", cost_amount=7.0, cost_currency="RUB", cost_basis="unknown"))
    ledger.append(_row(request_id="plan", cost_amount=99.0, cost_currency="RUB",
                       cost_basis="list_price_reference", billing="subscription"))

    assert ledger.spent_today("nd", caller="us", now=NOON, currency="RUB") == pytest.approx(7.0)


def test_an_unknown_row_in_another_currency_still_does_not_add(tmp_path):
    ledger = NodeLedger(tmp_path / "ledger")
    ledger.append(_row(request_id="usd-unknown", cost_amount=3.0, cost_currency="USD", cost_basis="unknown"))

    assert ledger.spent_today("nd", caller="us", now=NOON, currency="RUB") == 0.0
    assert ledger.spent_today("nd", caller="us", now=NOON, currency="USD") == pytest.approx(3.0)


@pytest.mark.asyncio
async def test_unknown_rows_alone_trip_the_429(tmp_path):
    """The gateway door: a NeuralDeep alias whose every row today is `unknown`
    is refused once they reach its rouble ceiling."""
    unknown = dict(PRICED_RUB, cost_basis="unknown", cost_amount=40.0)
    compute = {"serving_vendor": [ND], "vendor_quotas": {ND: 50.0}}
    gateway, ledger, service = _gateway(tmp_path, compute, {ND: _NeuralDeep()}, unknown)

    await gateway.complete(ND, "hi", messages=MESSAGES)
    assert ledger.spent_today(ND, caller=NODE_ID, caller_kind="gateway", currency="RUB") == pytest.approx(40.0)

    _charged(ledger, 10.0, basis="unknown", request_id="the-rest")
    with pytest.raises(GatewayError) as refused:
        await gateway.complete(ND, "hi", messages=MESSAGES)
    assert refused.value.status == 429 and refused.value.code == "insufficient_quota"
    assert len(service.calls) == 1, "the refused call must not reach the provider"
