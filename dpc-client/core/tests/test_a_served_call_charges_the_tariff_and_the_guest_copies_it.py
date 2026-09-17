"""A served call is charged at the tariff of the moment, and the guest copies it.

ADR-041 D3, amendment: the host prices a peer's call with
`ContextFirewall.tariff_for` at `started_at`, writes the applied rates, their
currency, the dated entry and the amount into its own row, and sends the same
group on the wire. The guest copies what arrived — it re-derives nothing — and
its `cost_usd` stays null, because it spent nothing of its own and prices
nothing. What the call cost the host stays on the host's row.
"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter
from dpc_client_core.firewall import ContextFirewall
from dpc_client_core.node_ledger import NodeLedger
from tests.test_p2p_coordinator import make_coordinator
from tests.test_the_gateway_routes_a_peer_alias_over_a_proved_connection_and_writes_the_requester_row import (
    PEER,
    WIRE_ID,
    _peer_service,
    _rows,
)
from tests.test_the_gateway_serves_only_the_two_lists_on_loopback import (
    _chat,
    _key,
    _request,
    _running,
)

ALIAS = "ollama_local"
GUEST = "dpc-node-alice-123"
FRIEND = "dpc-node-bob-456"
# 8 prompt tokens, 1 visible token and 56 of reasoning: the live «понг» call.
ANSWER = {
    "response": "pong", "model": "qwen3:8b", "provider": ALIAS,
    "prompt_tokens": 8, "response_tokens": 1, "thinking_tokens": 56,
    "tokens_used": 9, "output_includes_thinking": "excludes",
}
COMPUTE = {
    "enabled": True,
    "currency": "RUB",
    "allow_nodes": [GUEST, FRIEND],
    "free_nodes": [FRIEND],
    "serving_local": [ALIAS],
    "serving_tariff": {ALIAS: [{"from": "2026-09-01", "in": 20, "out": 60}]},
}
# 8 x 20 + (1 + 56) x 60, per 1M.
EXPECTED = (8 * 20.0 + 57 * 60.0) / 1_000_000.0


class _Connection:
    def __init__(self, node_id, connection_type="direct_tls"):
        self.node_id = node_id
        self.connection_type = connection_type


def _host(tmp_path, compute=COMPUTE, answer=ANSWER):
    """A coordinator whose firewall is the real one, reading the rules below."""
    rules = tmp_path / "privacy_rules.json"
    rules.write_text(json.dumps({"compute": compute}), encoding="utf-8")
    coord, svc = make_coordinator({GUEST: _Connection(GUEST), FRIEND: _Connection(FRIEND)})
    svc.firewall = ContextFirewall(rules)
    # The type is what the serving lists are classified by, and an alias whose
    # class cannot be established is refused at the door.
    svc.llm_manager.providers = {ALIAS: SimpleNamespace(config={"type": "ollama"})}
    svc.llm_manager.query = AsyncMock(return_value=dict(answer))
    coord._ledger = NodeLedger(tmp_path / "ledger")
    return coord, svc


def _sent(svc):
    """The REMOTE_INFERENCE_RESPONSE payload the host put on the wire."""
    (_, message), _ = svc.p2p_manager.send_message_to_peer.call_args
    return message["payload"]


# --- (1) the host's row -------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_host_row_carries_the_applied_tariff_and_what_it_came_to(tmp_path):
    coord, _ = _host(tmp_path)

    await coord.handle_inference_request(GUEST, "req-1", "ping")

    (row,) = coord._ledger.rows()
    assert (row["tariff_in"], row["tariff_out"]) == (20.0, 60.0)
    assert (row["tariff_currency"], row["tariff_at"]) == ("RUB", "2026-09-01")
    assert row["tariff_amount"] == pytest.approx(EXPECTED)
    # The host's own cost is the host's, and it stays here.
    assert row["cost_usd"] == 0.0


@pytest.mark.asyncio
async def test_a_free_peer_is_charged_a_declared_zero_and_not_an_absence(tmp_path):
    coord, _ = _host(tmp_path)

    await coord.handle_inference_request(FRIEND, "req-1", "ping")

    (row,) = coord._ledger.rows()
    assert (row["tariff_in"], row["tariff_out"]) == (0.0, 0.0)
    assert row["tariff_amount"] == 0.0
    assert row["tariff_currency"] == "RUB"


@pytest.mark.asyncio
async def test_a_host_that_declared_no_tariff_writes_no_tariff_column(tmp_path):
    """The v1 gift. Every column of the group is absent — not zero."""
    coord, _ = _host(tmp_path, compute={k: v for k, v in COMPUTE.items()
                                        if k not in ("currency", "serving_tariff")})

    await coord.handle_inference_request(GUEST, "req-1", "ping")

    (row,) = coord._ledger.rows()
    assert not [key for key in row if key.startswith("tariff_")]


@pytest.mark.asyncio
async def test_an_entry_dated_after_the_call_does_not_price_it(tmp_path):
    """The call is priced at `started_at`, so a rate declared to start tomorrow
    prices nothing today — and with no earlier entry there is no tariff at all."""
    tomorrow = (datetime.now(timezone.utc).date().toordinal() + 1)
    future = datetime.fromordinal(tomorrow).date().isoformat()
    coord, _ = _host(tmp_path, compute=dict(
        COMPUTE, serving_tariff={ALIAS: [{"from": future, "in": 999, "out": 999}]}))

    await coord.handle_inference_request(GUEST, "req-1", "ping")

    (row,) = coord._ledger.rows()
    assert not [key for key in row if key.startswith("tariff_")]


@pytest.mark.asyncio
async def test_counts_whose_convention_is_unknown_leave_the_rates_and_no_amount(tmp_path):
    coord, _ = _host(tmp_path, answer=dict(ANSWER, output_includes_thinking="unknown"))

    await coord.handle_inference_request(GUEST, "req-1", "ping")

    (row,) = coord._ledger.rows()
    assert row["tariff_in"] == 20.0 and row["tariff_amount"] is None


# --- (2) the wire ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_answer_carries_the_tariff_group_and_never_the_hosts_cost(tmp_path):
    coord, svc = _host(tmp_path)

    await coord.handle_inference_request(GUEST, "req-1", "ping")

    payload = _sent(svc)
    assert payload["status"] == "success"
    assert (payload["tariff_in"], payload["tariff_out"]) == (20.0, 60.0)
    assert (payload["tariff_currency"], payload["tariff_at"]) == ("RUB", "2026-09-01")
    assert payload["tariff_amount"] == pytest.approx(EXPECTED)
    assert "cost_usd" not in payload


@pytest.mark.asyncio
async def test_an_undeclared_tariff_puts_nothing_on_the_wire(tmp_path):
    coord, svc = _host(tmp_path, compute={k: v for k, v in COMPUTE.items()
                                          if k not in ("currency", "serving_tariff")})

    await coord.handle_inference_request(GUEST, "req-1", "ping")

    payload = _sent(svc)
    assert payload["status"] == "success"
    assert not [key for key in payload if key.startswith("tariff_")]


@pytest.mark.asyncio
async def test_a_refused_call_sends_no_tariff(tmp_path):
    coord, svc = _host(tmp_path)
    svc.llm_manager.query = AsyncMock(side_effect=RuntimeError("the card is out"))

    await coord.handle_inference_request(GUEST, "req-1", "ping")

    payload = _sent(svc)
    assert payload["status"] == "error"
    assert not [key for key in payload if key.startswith("tariff_")]


# --- (3) the guest's row: copied, never re-derived ----------------------------------


WIRE = {
    "request_id": WIRE_ID, "response": "from afar", "model": "qwen-on-the-peer",
    "prompt_tokens": 8, "response_tokens": 1, "thinking_tokens": 56, "tokens_used": 9,
    "output_includes_thinking": "excludes", "billing": "subscription",
    "tariff_in": 20.0, "tariff_out": 60.0, "tariff_currency": "RUB",
    "tariff_at": "2026-09-01", "tariff_amount": EXPECTED,
}


class _SilentProvider:
    alias = "qwen"
    model = "qwen3:8b"

    async def generate_response(self, prompt, **kwargs):
        return "unused"

    def get_last_usage(self):
        return {}


def _requester(tmp_path, wire):
    ledger = NodeLedger(tmp_path / "ledger")
    manager = SimpleNamespace(
        token_count_manager=None, providers={"qwen": _SilentProvider()},
        agent_provider=None, default_provider="qwen",
    )
    adapter = DpcLlmAdapter(manager, provider_alias="qwen", caller="agent_test",
                            ledger=ledger, compute_host=PEER)
    service = SimpleNamespace(
        _request_inference_from_peer=AsyncMock(return_value=dict(wire)),
        p2p_manager=SimpleNamespace(peers={PEER: _Connection(PEER)}),
    )
    adapter._llm_manager.providers["dpc_agent"] = SimpleNamespace(
        peer_id=None, remote_model=None, timeout=5, _service=service,
    )
    return adapter, ledger


@pytest.mark.asyncio
async def test_the_requester_row_copies_the_group_and_prices_nothing_itself(tmp_path):
    adapter, ledger = _requester(tmp_path, WIRE)

    await adapter.chat([{"role": "user", "content": "x"}])

    (row,) = ledger.rows()
    assert (row["tariff_in"], row["tariff_out"]) == (20.0, 60.0)
    assert (row["tariff_currency"], row["tariff_at"]) == ("RUB", "2026-09-01")
    assert row["tariff_amount"] == pytest.approx(EXPECTED)
    assert row["cost_usd"] is None


@pytest.mark.asyncio
async def test_a_host_that_sends_a_cost_is_not_believed_about_what_the_guest_spent(tmp_path):
    """An older host still sends `cost_usd`. It is the host's number about the
    host's money, and copying it is what the amendment ended."""
    adapter, ledger = _requester(tmp_path, dict(WIRE, cost_usd=0.0041))

    await adapter.chat([{"role": "user", "content": "x"}])

    (row,) = ledger.rows()
    assert row["cost_usd"] is None


@pytest.mark.asyncio
async def test_a_gift_leaves_the_requester_row_without_a_tariff(tmp_path):
    adapter, ledger = _requester(tmp_path, {
        key: value for key, value in WIRE.items() if not key.startswith("tariff_")})

    await adapter.chat([{"role": "user", "content": "x"}])

    (row,) = ledger.rows()
    assert not [key for key in row if key.startswith("tariff_")]
    assert row["cost_usd"] is None


# --- (4) the gateway's peer route writes the same row ------------------------------


@pytest.mark.asyncio
async def test_the_gateways_peer_row_copies_the_tariff_and_leaves_cost_null(tmp_path):
    from tests.test_the_gateway_routes_a_peer_alias_over_a_proved_connection_and_writes_the_requester_row import (
        REMOTE_MODEL,
        _priced_result,
    )

    served = _priced_result(**{key: value for key, value in WIRE.items()
                               if key.startswith("tariff_")})
    service = _peer_service(tmp_path, result=served)

    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat(REMOTE_MODEL))
        assert status == 200, text

        (row,) = _rows(ledger)
        assert (row["tariff_in"], row["tariff_currency"]) == (20.0, "RUB")
        assert row["tariff_amount"] == pytest.approx(EXPECTED)
        assert row["cost_usd"] is None


@pytest.mark.asyncio
async def test_an_older_host_sending_its_cost_does_not_price_the_gateways_row(tmp_path):
    """A host on the version before the amendment still puts `cost_usd` on the
    wire. It is that host's number about that host's money, and copying it here
    would price a call this node did not make."""
    from tests.test_the_gateway_routes_a_peer_alias_over_a_proved_connection_and_writes_the_requester_row import (
        REMOTE_MODEL,
        _priced_result,
    )

    service = _peer_service(tmp_path, result=_priced_result(cost_usd=0.0041))
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat(REMOTE_MODEL))
        assert status == 200, text

        (row,) = _rows(ledger)
        assert row["cost_usd"] is None
