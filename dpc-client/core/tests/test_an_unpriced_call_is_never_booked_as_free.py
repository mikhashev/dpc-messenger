"""An unpriced call is never booked as free, and a free call is not dollars.

Follow-up to the currency-aware ledger of 2026-09-28. Each case here is a
place where the two statements «this call cost nothing» and «nobody priced
this call» had been folded into one, or a zero had been filed under a
currency it was never in:

- a legacy row (`cost_usd` only) from a local card read back as 0.0 USD
  charged, so it sat in the USD bucket instead of under `cost_free`;
- `pricing.price_call` gave a provider type with no price table 0.0, the same
  answer a local card gets;
- `usage_row` accepted a basis on a row with no amount;
- `price_call` said it never raises and raised on a non-numeric count;
- the scheduled-task record, the GAIA run total and the gateway's peer
  fallback each kept a narrower copy of the cost fields than the writer has.
"""

import json
import sys
from pathlib import Path

import pytest

from dpc_client_core.dpc_agent import loop, pricing
from dpc_client_core.node_ledger import NodeLedger, summarize
from tests.test_a_usage_row_carries_its_cost_in_a_named_currency import NOON, _legacy, _row
from tests.test_the_gateway_routes_a_peer_alias_over_a_proved_connection_and_writes_the_requester_row import (
    REMOTE_MODEL,
    _peer_service,
    _rows,
    _unpriced_result,
)
from tests.test_the_gateway_serves_only_the_two_lists_on_loopback import _chat, _key, _request, _running

EVAL = Path(__file__).resolve().parents[3] / "eval"
sys.path.insert(0, str(EVAL))
sys.path.insert(0, str(EVAL / "gaia"))

import run_gaia_eval as gaia  # noqa: E402

COUNTS = dict(prompt_tokens=1000, completion_tokens=500)


# --- item 4: a legacy local zero is free, not dollars -----------------------


def test_a_legacy_local_zero_reads_back_free_and_a_legacy_paid_zero_stays_dollars(tmp_path):
    ledger = NodeLedger(tmp_path / "ledger")
    _legacy(ledger, "old-local", 0.0, alias="qwen_card", billing="subscription", model="qwen3.8-27b")
    _legacy(ledger, "old-paid-zero", 0.0, alias="deepseek_flash")
    _legacy(ledger, "old-paid", 0.03, alias="deepseek_flash")

    local, paid_zero, paid = ledger.rows()
    assert (local["cost_amount"], local["cost_currency"], local["cost_basis"]) == (0.0, None, None)
    assert (paid_zero["cost_amount"], paid_zero["cost_currency"], paid_zero["cost_basis"]) == (0.0, "USD", "charged")
    assert (paid["cost_amount"], paid["cost_currency"], paid["cost_basis"]) == (0.03, "USD", "charged")

    by_alias = summarize(ledger.rows())["by_alias"]
    assert by_alias["qwen_card"]["cost"] == {} and by_alias["qwen_card"]["cost_free"] == 1
    assert by_alias["deepseek_flash"]["cost"] == {"USD": {"amount": pytest.approx(0.03), "rows": 2}}


# --- item 5: a type with no price table is unpriced, not free ---------------


@pytest.mark.parametrize("provider_type", [
    "anthropic", "gemini", "github_models", "gigachat", "openai_compatible", "dpc_agent", "remote_peer", None,
])
def test_a_call_no_table_can_price_is_unpriced_with_a_reason(provider_type):
    priced = pricing.price_call(provider_type=provider_type, alias="some_alias", model="some-model", **COUNTS)
    assert priced["cost_amount"] is None, provider_type
    assert priced["cost_currency"] is None and priced["cost_basis"] is None
    assert priced["cost_unpriced_reason"]


def test_every_loaded_type_is_either_local_or_answers_for_its_price():
    """No type in the registry falls through to a silent zero: the local ones
    are free by being local, every other one is priced or says why not."""
    from dpc_client_core.llm_manager import PROVIDER_MAP

    for provider_type in PROVIDER_MAP:
        priced = pricing.price_call(provider_type=provider_type, alias="x", model="unheard-of", **COUNTS)
        if provider_type in pricing.LOCAL_PROVIDER_TYPES:
            assert (priced["cost_amount"], priced["cost_currency"]) == (0.0, None), provider_type
        else:
            assert priced["cost_amount"] is None and priced["cost_unpriced_reason"], provider_type


# --- item 6: a basis needs an amount ----------------------------------------


def test_a_basis_on_a_row_with_no_amount_is_refused():
    with pytest.raises(ValueError, match="cost_basis"):
        _row(cost_amount=None, cost_basis="charged")


# --- item 7: price_call does not raise; the narrower copies are whole -------


def test_price_call_does_not_raise_on_counts_that_are_not_numbers():
    priced = pricing.price_call(provider_type="deepseek", alias="deepseek_flash", model="deepseek-v4-flash",
                                prompt_tokens="many", completion_tokens=None, thinking_tokens="x",
                                cache_hit_tokens=object())
    assert priced["cost_currency"] == "USD" and priced["cost_amount"] == 0.0


def test_an_unmeasured_task_record_carries_every_cost_field_as_null():
    unmeasured = loop.unmeasured_task_cost()
    assert set(unmeasured) == set(loop.task_cost_fields({}))
    assert all(value is None for value in unmeasured.values())


def test_the_gaia_total_sums_a_two_currency_task_per_currency():
    two = {"usage": loop.task_cost_fields({"cost_by_currency": {"USD": 0.5, "RUB": 40.0},
                                           "cost_bases": ["charged"], "cost_priced_calls": 2})}
    one = {"usage": {"cost_amount": 0.25, "cost_currency": "USD"}}
    none = {"usage": {"cost_amount": None}}
    total = gaia._sum_cost([two, one, none])
    assert total["by_currency"]["USD"] == {"total": pytest.approx(0.75), "reported_by": 2}
    assert total["by_currency"]["RUB"] == {"total": pytest.approx(40.0), "reported_by": 1}
    assert total["not_reported_by"] == 1


# --- item 9: the gateway's peer fallback reads the host's type --------------


@pytest.mark.asyncio
async def test_a_host_that_sent_no_billing_is_classified_by_the_type_its_menu_names(tmp_path):
    """A host that predates `billing` on the wire: the requester's row falls
    back to this node's table, and the host's menu row already says what type
    the alias is — a `deepseek` alias is pay-per-use whatever it is called."""
    service = _peer_service(tmp_path, result=_unpriced_result())
    for row in service.peer_metadata[next(iter(service.peer_metadata))]["providers"]:
        row["type"] = "deepseek"
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat(REMOTE_MODEL))
        assert status == 200, text
        (row,) = _rows(ledger)
        assert row["billing"] == "pay_per_use"
        assert row["cost_amount"] is None


@pytest.mark.asyncio
async def test_the_agents_peer_row_is_classified_by_the_type_the_hosts_menu_names(tmp_path):
    """The same fallback on the agent path: a host older than `billing` on the
    wire answered with a model named like DeepSeek's, and its menu says the
    alias is a llama-server card — a card is not pay-per-use."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from tests.test_every_model_call_leaves_one_usage_row_on_the_node_that_ran_it import (
        MESSAGES as AGENT_MESSAGES,
        PEER,
        _adapter,
        _PricedProvider,
    )

    ledger = NodeLedger(tmp_path / "ledger")
    service = SimpleNamespace(
        _request_inference_from_peer=AsyncMock(return_value={
            "response": "from afar", "prompt_tokens": 40, "response_tokens": 12,
            "tokens_used": 52, "model": "deepseek-v4-flash",
        }),
        peer_metadata={PEER: {"providers": [{"alias": "ds_flash", "type": "llamacpp_server"}]}},
    )
    adapter = _adapter(_PricedProvider(), ledger, compute_host=PEER)
    adapter._llm_manager.providers["dpc_agent"] = SimpleNamespace(
        peer_id=None, remote_model=None, timeout=5, _service=service,
    )

    await adapter.chat(AGENT_MESSAGES)

    (row,) = list(ledger.rows())
    assert row["route"] == "peer" and row["alias"] == "ds_flash"
    assert row["billing"] == "subscription"
    assert row["cost_amount"] is None
