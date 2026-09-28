"""The peer door serves a vendor alias as it serves a local one, to the peers the owner allows.

Mike's call, 2026-09-28 (ADR-041 D5, amendment of that date): an alias in
`compute.serving_vendor` is served over P2P, not only through this node's own
gateway. Who may use it is the owner's choice through `compute.allow_nodes` /
`allow_groups`; DPC does not read the vendor's terms. The door reads the policy
the gateway reads: the classified lists (`owner_of`), the daily ceiling per
caller in the currency the provider bills in, `unrated` for an alias nothing
can price, and no card queue for an alias money bounds. The guest is told on
its menu row that the model is a vendor's and whose.

Everything here runs through the real `ContextFirewall` over a rules file —
no stand-in for `can_request_inference`, which is the gate that refused a
vendor alias before this change.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.firewall import ContextFirewall
from dpc_client_core.node_ledger import NodeLedger
from dpc_client_core.service import CoreService
from tests.test_p2p_coordinator import make_coordinator

GUEST = "peer-1"
STRANGER = "peer-2"
ND = "qwen 3.8 27b ND"
LOCAL = "ollama_local"
QUOTA_RUB = 1.0
TARIFF = {"from": "2026-01-01", "in": 20.0, "out": 60.0}


class _NeuralDeep:
    """A NeuralDeep provider as the door reads it: type, model, the unit it bills in."""

    def __init__(self):
        self.config = {"type": "neuraldeep", "model": "qwen3.8-27b"}
        self.model = "qwen3.8-27b"

    def billing_currency(self):
        return "RUB"

    def supports_vision(self):
        return False


class _Ollama:
    def __init__(self):
        self.config = {"type": "ollama", "model": "qwen3:8b"}
        self.model = "qwen3:8b"

    def supports_vision(self):
        return False


def _answer(cost_rub: float) -> dict:
    """What `LLMManager.query` returns for a NeuralDeep call: the counts and
    the provider's own price, copied in by `reported_cost`."""
    return {
        "response": "pong", "model": "qwen3.8-27b", "provider": ND,
        "prompt_tokens": 1000, "response_tokens": 100, "output_includes_thinking": "includes",
        "cost_amount": cost_rub, "cost_currency": "RUB", "cost_basis": "charged",
    }


COMPUTE = {
    "allow_nodes": [GUEST],
    "serving_local": [LOCAL],
    "serving_vendor": [ND],
    "vendor_quotas": {ND: QUOTA_RUB},
    # A node default in dollars, so the provider step is what makes the tariff roubles.
    "currency": "USD",
    "serving_tariff": {ND: [TARIFF], LOCAL: [TARIFF]},
}


def _host(tmp_path: Path, compute: dict = COMPUTE):
    rules = tmp_path / "privacy_rules.json"
    rules.write_text(json.dumps({"compute": dict(compute, enabled=True)}), encoding="utf-8")
    coord, svc = make_coordinator()
    svc.firewall = ContextFirewall(rules)
    svc.gateway = None
    svc.llm_manager.providers = {ND: _NeuralDeep(), LOCAL: _Ollama()}
    svc.llm_manager.query = AsyncMock(return_value=_answer(0.6))
    coord._ledger = NodeLedger(tmp_path / "ledger")
    return coord, svc


def _sent(svc) -> dict:
    return svc.p2p_manager.send_message_to_peer.call_args[0][1]["payload"]


# --- (i) served, priced in roubles, and stopped at the ceiling ------------------


@pytest.mark.asyncio
async def test_an_allowed_peer_is_served_the_vendor_alias_and_the_row_carries_roubles(tmp_path):
    coord, svc = _host(tmp_path)

    await coord.handle_inference_request(GUEST, "req-1", "ping", provider=ND)

    payload = _sent(svc)
    assert payload["status"] == "success", payload
    assert svc.llm_manager.query.await_args.kwargs["provider_alias"] == ND
    (row,) = coord._ledger.rows()
    assert (row["alias"], row["caller"], row["caller_kind"]) == (ND, GUEST, "peer")
    # The provider's own report, not a dollar table's reading of the model name.
    assert (row["cost_amount"], row["cost_currency"], row["cost_basis"]) == (0.6, "RUB", "charged")
    # The tariff takes the provider's unit over the node's USD default, frozen
    # on the row and sent on the wire alike.
    assert row["tariff_currency"] == "RUB" and payload["tariff_currency"] == "RUB"
    expected = (1000 * 20.0 + 100 * 60.0) / 1_000_000.0
    assert row["tariff_amount"] == pytest.approx(expected) == payload["tariff_amount"]


@pytest.mark.asyncio
async def test_an_explicit_tariff_currency_still_wins_over_the_provider(tmp_path):
    coord, svc = _host(tmp_path, dict(COMPUTE, tariff_currency={ND: "EUR"}))

    await coord.handle_inference_request(GUEST, "req-1", "ping", provider=ND)

    (row,) = coord._ledger.rows()
    assert row["tariff_currency"] == "EUR" and _sent(svc)["tariff_currency"] == "EUR"
    assert row["cost_currency"] == "RUB", "the host's own cost stays in the vendor's unit"


@pytest.mark.asyncio
async def test_the_ceiling_stops_the_peer_in_roubles_and_names_them(tmp_path):
    coord, svc = _host(tmp_path)

    await coord.handle_inference_request(GUEST, "req-1", "ping", provider=ND)  # 0.6 of 1.0
    await coord.handle_inference_request(GUEST, "req-2", "ping", provider=ND)  # 1.2: served, crosses
    assert _sent(svc)["status"] == "success"
    await coord.handle_inference_request(GUEST, "req-3", "ping", provider=ND)

    payload = _sent(svc)
    assert payload["status"] == "error"
    assert payload["code"] == "insufficient_quota"
    assert "1.2000 RUB" in payload["error"] and "1.00 RUB" in payload["error"]
    assert svc.llm_manager.query.await_count == 2
    assert [r["request_id"] for r in coord._ledger.rows()] == ["req-1", "req-2"]


@pytest.mark.asyncio
async def test_a_vendor_alias_is_not_queued_behind_the_card(tmp_path):
    """Money bounds a vendor alias, not the card: with the card's queue held by
    another call, the vendor call still runs — as on the gateway's route."""
    coord, svc = _host(tmp_path)

    async with coord._peer_inference_lock:
        await asyncio.wait_for(
            coord.handle_inference_request(GUEST, "req-1", "ping", provider=ND), timeout=5,
        )

    assert _sent(svc)["status"] == "success"


@pytest.mark.asyncio
async def test_a_local_alias_still_waits_for_the_card(tmp_path):
    coord, svc = _host(tmp_path)
    svc.llm_manager.query = AsyncMock(return_value={"response": "pong", "model": "qwen3:8b"})

    async with coord._peer_inference_lock:
        task = asyncio.create_task(
            coord.handle_inference_request(GUEST, "req-1", "ping", provider=LOCAL)
        )
        await asyncio.sleep(0.05)
        assert not task.done(), "a local alias queues behind the card"
    await asyncio.wait_for(task, timeout=5)
    assert _sent(svc)["status"] == "success"


@pytest.mark.asyncio
async def test_an_unrated_vendor_alias_is_refused_as_before(tmp_path):
    coord, svc = _host(tmp_path)
    svc.llm_manager.providers[ND] = SimpleNamespace(
        config={"type": "anthropic", "model": "claude-sonnet-4-5"}, model="claude-sonnet-4-5",
    )

    await coord.handle_inference_request(GUEST, "req-1", "ping", provider=ND)

    assert _sent(svc)["code"] == "unrated"
    svc.llm_manager.query.assert_not_awaited()


# --- (ii) authorisation is the owner's, and comes first ---------------------------


@pytest.mark.asyncio
async def test_a_peer_the_owner_did_not_allow_is_refused_at_authorisation(tmp_path):
    coord, svc = _host(tmp_path)

    await coord.handle_inference_request(STRANGER, "req-1", "ping", provider=ND)

    payload = _sent(svc)
    assert payload["code"] == "not_allowed", payload
    svc.llm_manager.query.assert_not_awaited()
    assert list(coord._ledger.rows()) == []


def test_the_gate_accepts_both_lists_and_nothing_else(tmp_path):
    coord, svc = _host(tmp_path)
    fw = svc.firewall

    assert fw.can_request_inference(GUEST, provider=ND) is True
    assert fw.can_request_inference(GUEST, provider=LOCAL) is True
    assert fw.can_request_inference(GUEST, provider="deepseek_pro") is False
    assert fw.can_request_inference(STRANGER, provider=ND) is False


# --- (iii) the default stays local; an alias off both lists is refused ----------


@pytest.mark.asyncio
async def test_a_request_naming_no_alias_is_served_the_first_local_one(tmp_path):
    coord, svc = _host(tmp_path)
    svc.llm_manager.query = AsyncMock(return_value={"response": "pong", "model": "qwen3:8b"})

    await coord.handle_inference_request(GUEST, "req-1", "ping")

    assert svc.llm_manager.query.await_args.kwargs["provider_alias"] == LOCAL


@pytest.mark.asyncio
async def test_a_request_naming_no_alias_on_a_vendor_only_node_is_not_given_the_vendor(tmp_path):
    """A vendor alias is never the silent default: a guest that names nothing
    on a node sharing only vendor aliases is refused, not billed."""
    compute = dict(COMPUTE, serving_local=[], serving_tariff={ND: [TARIFF]})
    coord, svc = _host(tmp_path, compute)

    await coord.handle_inference_request(GUEST, "req-1", "ping")

    assert _sent(svc)["code"] == "model_not_found"
    svc.llm_manager.query.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_alias_in_neither_list_is_refused(tmp_path):
    coord, svc = _host(tmp_path)
    svc.llm_manager.providers["deepseek_pro"] = SimpleNamespace(
        config={"type": "deepseek", "model": "deepseek-v4-pro"}, model="deepseek-v4-pro",
    )

    await coord.handle_inference_request(GUEST, "req-1", "ping", provider="deepseek_pro")

    assert _sent(svc)["code"] == "not_allowed"
    svc.llm_manager.query.assert_not_awaited()


# --- (iv) the guest is told the model is a vendor's, and whose ------------------


def _menu_service(firewall, providers):
    service = SimpleNamespace(
        firewall=firewall,
        llm_manager=SimpleNamespace(
            providers=providers,
            lookup_context_window=lambda model: None,
            get_context_window=lambda model: None,
        ),
        _provider_supports_voice=lambda provider: False,
    )
    service.build_p2p_provider_info = (
        lambda alias, provider, **kwargs:
        CoreService.build_p2p_provider_info(service, alias, provider, **kwargs)
    )
    return service


def test_the_menu_offers_the_vendor_alias_marked_as_a_vendors_model(tmp_path):
    _, svc = _host(tmp_path)
    service = _menu_service(svc.firewall, {ND: _NeuralDeep(), LOCAL: _Ollama()})

    rows, reason = CoreService.menu_for_peer(service, GUEST)

    by_alias = {row["alias"]: row for row in rows}
    assert set(by_alias) == {ND, LOCAL}, reason
    assert by_alias[ND]["provider_kind"] == "vendor"
    assert by_alias[ND]["type"] == "neuraldeep"
    assert by_alias[ND]["tariff"]["currency"] == "RUB"
    assert by_alias[LOCAL]["provider_kind"] == "self_hosted"


def test_a_peer_the_owner_did_not_allow_is_offered_nothing(tmp_path):
    _, svc = _host(tmp_path)
    service = _menu_service(svc.firewall, {ND: _NeuralDeep(), LOCAL: _Ollama()})

    rows, _ = CoreService.menu_for_peer(service, STRANGER)

    assert rows == []


def test_the_guest_reads_the_kind_from_the_row_over_its_own_type_table():
    """An `openai_compatible` row cannot be classed from its type alone — the
    guest has no base_url — so the host's word on the row decides."""
    from dpc_client_core.dpc_agent.provider_facts import _peer_facts

    node = "dpc-node-" + "a" * 32
    metadata = {node: {"providers": [
        {"alias": "oa", "type": "openai_compatible", "provider_kind": "vendor"},
        {"alias": "old", "type": "openai_compatible"},
    ]}}

    assert _peer_facts("oa", node, metadata)["provider_kind"] == "vendor"
    assert _peer_facts("old", node, metadata)["provider_kind"] == "unknown"
