"""The tariff currency is per served alias.

ADR-041 D3, amendment of 2026-09-28 (Mike's call, option b): `compute.currency`
was one ISO 4217 code for the whole node, so an owner could not price a
NeuralDeep alias in the roubles it is bought in while the rest of the node
priced in dollars. Now each alias resolves its own unit, by one function
(`ContextFirewall.tariff_currency_for`), in this order:

1. an explicit `compute.tariff_currency.<alias>`;
2. for an alias in `compute.serving_vendor`, the currency its provider bills in
   — the same `pricing.vendor_ceiling_currency` the daily ceiling reads;
3. `compute.currency`, the node default, which is all a local alias ever had;
4. nothing — no tariff declared, the gift.

The rates are numbers in the resolved unit; nothing converts between units.
"""

import json
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core import provider_alias_refs
from dpc_client_core.firewall import AppliedTariff, ContextFirewall
from dpc_client_core.node_ledger import NodeLedger
from dpc_client_core.service import CoreService, menu_tariff_row
from tests.test_p2p_coordinator import make_coordinator

ND = "qwen 3.8 27b ND"
DS = "ds_flash"
LOCAL = "ollama_local"
GUEST = "dpc-node-alice-123"

COMPUTE = {
    "enabled": True,
    "currency": "USD",
    "allow_nodes": [GUEST],
    "serving_local": [LOCAL],
    "serving_vendor": [ND, DS],
    "vendor_quotas": {ND: 1000.0, DS: 5.0},
    "serving_tariff": {
        ND: [{"from": "2026-09-01", "in": 20, "out": 60}],
        DS: [{"from": "2026-09-01", "in": 0.5, "out": 1.5}],
        LOCAL: [{"from": "2026-09-01", "in": 0.1, "out": 0.3}],
    },
}
ANSWER = {
    "response": "pong", "model": "qwen3.8-27b", "provider": ND,
    "prompt_tokens": 1000, "response_tokens": 100, "thinking_tokens": 0,
    "tokens_used": 1100, "output_includes_thinking": "includes",
}


class _Provider:
    """What the resolver reads off a provider: its config, its model and, for
    a self-priced type, the currency it bills in."""

    def __init__(self, provider_type, model, currency=None):
        self.config = {"type": provider_type, "model": model}
        self.model = model
        self._currency = currency

    def billing_currency(self):
        return self._currency

    def supports_vision(self):
        return False


PROVIDERS = {
    ND: _Provider("neuraldeep", "qwen3.8-27b", currency="RUB"),
    DS: _Provider("deepseek", "deepseek-v4-flash"),
    LOCAL: _Provider("ollama", "qwen3:8b"),
}


def _firewall(tmp_path: Path, compute: dict) -> ContextFirewall:
    rules = tmp_path / "privacy_rules.json"
    rules.write_text(json.dumps({"compute": compute}), encoding="utf-8")
    return ContextFirewall(rules)


def _at(day: str) -> datetime:
    return datetime.fromisoformat(day + "T12:00:00+00:00")


# --- (1) the resolver -----------------------------------------------------------------


def test_a_neuraldeep_alias_with_no_explicit_currency_is_priced_in_roubles(tmp_path):
    fw = _firewall(tmp_path, COMPUTE)

    assert fw.tariff_currency_for(ND, PROVIDERS[ND]) == ("RUB", "provider")
    assert fw.tariff_currency_for(DS, PROVIDERS[DS]) == ("USD", "provider")
    applied = fw.tariff_for(ND, peer_id=GUEST, at=_at("2026-09-10"), provider=PROVIDERS[ND])
    assert applied == AppliedTariff(20.0, 60.0, "RUB", date(2026, 9, 1))


def test_an_explicit_currency_on_the_alias_overrides_the_provider_and_the_node(tmp_path):
    fw = _firewall(tmp_path, dict(COMPUTE, tariff_currency={ND: "EUR", LOCAL: "RUB"}))

    assert fw.tariff_currency_for(ND, PROVIDERS[ND]) == ("EUR", "explicit")
    assert fw.tariff_currency_for(LOCAL, PROVIDERS[LOCAL]) == ("RUB", "explicit")
    assert fw.tariff_for(ND, peer_id=GUEST, at=_at("2026-09-10"), provider=PROVIDERS[ND]).currency == "EUR"
    # An explicit currency declares a tariff even on a node with no default.
    bare = _firewall(tmp_path, dict(COMPUTE, currency=None, tariff_currency={LOCAL: "RUB"}))
    assert bare.tariff_for(LOCAL, peer_id=GUEST, at=_at("2026-09-10")).currency == "RUB"


def test_a_local_alias_keeps_the_node_currency_exactly_as_before(tmp_path):
    fw = _firewall(tmp_path, COMPUTE)

    assert fw.tariff_currency_for(LOCAL, PROVIDERS[LOCAL]) == ("USD", "node")
    assert fw.tariff_currency_for(LOCAL) == ("USD", "node")
    assert fw.tariff_for(LOCAL, peer_id=GUEST, at=_at("2026-09-10"), provider=PROVIDERS[LOCAL]) == \
        AppliedTariff(0.1, 0.3, "USD", date(2026, 9, 1))


def test_a_vendor_alias_whose_provider_names_no_currency_falls_back_to_the_node(tmp_path):
    unrated = _Provider("openai_compatible", "a-model-no-table-prices")
    fw = _firewall(tmp_path, COMPUTE)
    assert fw.tariff_currency_for(ND, unrated) == ("USD", "node")
    # Without a provider at hand the provider step is skipped, never guessed.
    assert fw.tariff_currency_for(ND) == ("USD", "node")


def test_with_no_unit_anywhere_the_call_is_still_a_gift(tmp_path):
    fw = _firewall(tmp_path, dict(COMPUTE, currency=None))
    assert fw.tariff_currency_for(LOCAL, PROVIDERS[LOCAL]) == (None, None)
    assert fw.tariff_for(LOCAL, peer_id=GUEST, at=_at("2026-09-10"), provider=PROVIDERS[LOCAL]) is None
    # A vendor alias still has its provider's unit.
    assert fw.tariff_for(ND, peer_id=GUEST, at=_at("2026-09-10"), provider=PROVIDERS[ND]).currency == "RUB"


# --- (2) the validator ------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["rub", "XYZ", "", 643, None])
def test_an_invalid_per_alias_code_is_refused_naming_the_alias(tmp_path, bad):
    compute = dict(COMPUTE, tariff_currency={ND: bad})
    with pytest.raises(ValueError) as refused:
        _firewall(tmp_path, compute)
    reason = str(refused.value)
    assert f"compute.tariff_currency.{ND}" in reason and "ISO 4217" in reason

    ok, errors = ContextFirewall.validate_config({"compute": compute})
    assert ok is False and any(ND in e and "ISO 4217" in e for e in errors)


def test_a_per_alias_table_that_is_not_an_object_is_refused(tmp_path):
    with pytest.raises(ValueError) as refused:
        _firewall(tmp_path, dict(COMPUTE, tariff_currency=["RUB"]))
    assert "tariff_currency" in str(refused.value)


def test_a_comment_key_in_the_table_is_not_an_alias(tmp_path):
    fw = _firewall(tmp_path, dict(COMPUTE, tariff_currency={"_comment": "per alias", ND: "RUB"}))
    assert fw.compute_tariff_currency == {ND: "RUB"}


def test_renaming_an_alias_follows_its_currency(tmp_path):
    (tmp_path / "privacy_rules.json").write_text(
        json.dumps({"compute": dict(COMPUTE, tariff_currency={LOCAL: "RUB"})}), encoding="utf-8")
    assert "privacy_rules.json:compute.tariff_currency" in provider_alias_refs.find_references(LOCAL, tmp_path)

    provider_alias_refs.rename_references(LOCAL, "renamed_local", tmp_path)
    compute = json.loads((tmp_path / "privacy_rules.json").read_text(encoding="utf-8"))["compute"]
    assert compute["tariff_currency"] == {"renamed_local": "RUB"}


# --- (3) the served row and the wire ----------------------------------------------------


class _Connection:
    def __init__(self, node_id, connection_type="direct_tls"):
        self.node_id = node_id
        self.connection_type = connection_type


def _host(tmp_path, compute):
    rules = tmp_path / "privacy_rules.json"
    rules.write_text(json.dumps({"compute": compute}), encoding="utf-8")
    coord, svc = make_coordinator({GUEST: _Connection(GUEST)})
    svc.firewall = ContextFirewall(rules)
    svc.llm_manager.providers = dict(PROVIDERS)
    svc.llm_manager.query = AsyncMock(return_value=dict(ANSWER))
    coord._ledger = NodeLedger(tmp_path / "ledger")
    return coord, svc


def _sent(svc):
    (_, message), _ = svc.p2p_manager.send_message_to_peer.call_args
    return message["payload"]


def test_a_served_neuraldeep_call_freezes_roubles_on_the_row_and_in_what_the_wire_sends(tmp_path):
    """The row writer, called directly. `_record_peer_call` is where a served
    call's tariff is resolved and frozen, and the `tariff` it returns is the
    object the response's tariff group is built from. The same call through
    the door — which serves `serving_vendor` aliases since 2026-09-28 — is
    `test_the_peer_door_serves_a_vendor_alias_the_owner_allows.py`."""
    coord, _ = _host(tmp_path, COMPUTE)

    _, tariff, amount = coord._record_peer_call(
        peer_id=GUEST, request_id="req-1", serving_alias=ND, result=dict(ANSWER),
        model="qwen3.8-27b", started_at=datetime.now(timezone.utc), duration_s=0.1,
    )

    (row,) = coord._ledger.rows()
    # 1000 x 20 + 100 x 60, per 1M — in roubles, the rates as written, unconverted.
    expected = (1000 * 20.0 + 100 * 60.0) / 1_000_000.0
    assert (row["tariff_currency"], row["tariff_in"], row["tariff_out"]) == ("RUB", 20.0, 60.0)
    assert row["tariff_amount"] == pytest.approx(expected)
    assert tariff.currency == "RUB" and amount == pytest.approx(expected)


@pytest.mark.asyncio
async def test_an_explicit_currency_reaches_the_row_and_the_wire_through_the_door(tmp_path):
    coord, svc = _host(tmp_path, dict(COMPUTE, tariff_currency={LOCAL: "RUB"}))
    svc.llm_manager.query = AsyncMock(return_value=dict(ANSWER, provider=LOCAL, model="qwen3:8b"))

    await coord.handle_inference_request(GUEST, "req-1", "ping", provider=LOCAL)

    payload = _sent(svc)
    assert payload["status"] == "success", payload
    (row,) = coord._ledger.rows()
    expected = (1000 * 0.1 + 100 * 0.3) / 1_000_000.0
    assert row["tariff_currency"] == "RUB" and payload["tariff_currency"] == "RUB"
    assert row["tariff_amount"] == pytest.approx(expected) == payload["tariff_amount"]


@pytest.mark.asyncio
async def test_a_local_alias_served_through_the_door_keeps_the_node_currency(tmp_path):
    coord, svc = _host(tmp_path, COMPUTE)
    svc.llm_manager.query = AsyncMock(return_value=dict(ANSWER, provider=LOCAL, model="qwen3:8b"))

    await coord.handle_inference_request(GUEST, "req-1", "ping", provider=LOCAL)

    (row,) = coord._ledger.rows()
    assert row["tariff_currency"] == "USD" and _sent(svc)["tariff_currency"] == "USD"


# --- (4) the menu quotes the same unit the receipt carries ------------------------------


def test_the_menu_row_quotes_the_alias_in_its_own_currency(tmp_path):
    fw = _firewall(tmp_path, COMPUTE)
    assert menu_tariff_row(fw, ND, GUEST, provider=PROVIDERS[ND])["currency"] == "RUB"
    assert menu_tariff_row(fw, LOCAL, GUEST, provider=PROVIDERS[LOCAL])["currency"] == "USD"

    # `CoreService.menu_tariff` — the gateway's reader — finds the provider itself.
    service = SimpleNamespace(firewall=fw, llm_manager=SimpleNamespace(providers=dict(PROVIDERS)))
    assert CoreService.menu_tariff(service, ND, GUEST)["currency"] == "RUB"
