"""A vendor alias served to a peer stops at the same daily ceiling the gateway enforces.

ADR-041 D5: a local alias is bounded by the card, a vendor alias by money —
`compute.vendor_quotas`, USD per day and per caller. The gateway's local route
has enforced it since the quota shipped; the peer door had no quota at all, so
a peer calling a vendor alias this node serves could spend without bound.

The ceiling is read from the node ledger rather than from a counter, which is
what makes it survive a restart: the sum is over the rows
`_record_peer_call` wrote under that peer's own name with
`caller_kind=peer`, and a second `NodeLedger` over the same directory sums the
same rows.

Two states are told apart here. With the lists classified, a vendor serving
alias is weighed against its ceiling; with the lists refused as a
configuration error — which is what a paying alias sitting in
`compute.serving_local` is — the door refuses rather than guess the class, and
guessing wrong spends the host's money.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.firewall import ContextFirewall, ServingLists
from dpc_client_core.gateway import Gateway
from dpc_client_core.node_ledger import NodeLedger, usage_row
from dpc_client_core.p2p_coordinator import PEER_CALLER_KIND
from tests.test_p2p_coordinator import make_coordinator

GUEST = "peer-1"
OTHER = "peer-2"
VENDOR = "ds_flash"
#: A model the pricing tables know: an alias nobody can price is refused on
#: this door now, so a fixture that means «served» must be priceable.
VENDOR_MODEL = "deepseek-v4-flash"
LOCAL = "ollama_local"
QUOTA = 1.00


def _spend(
    ledger: NodeLedger, caller: str, cost_usd: float, *,
    alias: str = VENDOR, caller_kind: str = PEER_CALLER_KIND, at: datetime = None,
) -> None:
    """One served call's row, as `_record_peer_call` writes it."""
    ledger.append(usage_row(
        request_id=f"{caller}-{cost_usd}-{caller_kind}",
        caller=caller, caller_kind=caller_kind, alias=alias, model="m",
        route="local", prompt_tokens=10, completion_tokens=5, thinking_tokens=None,
        counts_source="engine", started_at=at or datetime.now(timezone.utc),
        duration_s=0.5, billing="pay_per_use", cost_usd=cost_usd,
    ))


def _vendor_host(tmp_path: Path, *, quotas=None):
    """A door whose serving alias is a vendor alias with a ceiling.

    The lists are stood in rather than written into a rules file: today
    `compute_serving_alias` is `serving_local[0]`, so the load path cannot
    reach this state, and the gate under test is what the door does once the
    alias it serves is classified `vendor`.
    """
    coord, svc = make_coordinator()
    svc.firewall.can_request_inference.return_value = True
    svc.firewall.compute_serving_alias = VENDOR
    svc.firewall.classify_serving_lists.return_value = ServingLists(
        local=(LOCAL,), vendor=(VENDOR,),
        quotas={VENDOR: QUOTA} if quotas is None else quotas,
    )
    # The model is in the config because that is where providers.json puts it,
    # and the door reads it to ask whether the alias can be priced at all.
    svc.llm_manager.providers = {
        VENDOR: SimpleNamespace(config={"type": "deepseek", "model": VENDOR_MODEL}),
    }
    svc.llm_manager.query = AsyncMock(return_value={"response": "pong", "model": "m"})
    coord._ledger = NodeLedger(tmp_path / "ledger")
    return coord, svc


def _real_firewall_host(tmp_path: Path, compute: dict, providers: dict):
    """A door reading a real `ContextFirewall` over a rules file."""
    rules = tmp_path / "privacy_rules.json"
    rules.write_text(json.dumps({"compute": dict(compute, enabled=True)}), encoding="utf-8")
    coord, svc = make_coordinator()
    svc.firewall = ContextFirewall(rules)
    svc.llm_manager.providers = providers
    svc.llm_manager.query = AsyncMock(return_value={"response": "pong", "model": "m"})
    coord._ledger = NodeLedger(tmp_path / "ledger")
    return coord, svc


def _refusal(svc) -> dict:
    message = svc.p2p_manager.send_message_to_peer.call_args[0][1]
    payload = message["payload"]
    assert payload["status"] == "error"
    return payload


def _requests(ledger: NodeLedger) -> list:
    return [row["request_id"] for row in ledger.rows()]


# --- the ceiling ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_peer_under_its_ceiling_is_served(tmp_path):
    coord, svc = _vendor_host(tmp_path)
    _spend(coord._ledger, GUEST, 0.99)

    await coord.handle_inference_request(GUEST, "req-1", "ping")

    svc.llm_manager.query.assert_awaited_once()
    payload = svc.p2p_manager.send_message_to_peer.call_args[0][1]["payload"]
    assert payload["status"] == "success"
    assert "req-1" in _requests(coord._ledger)


@pytest.mark.asyncio
@pytest.mark.parametrize("spent", [1.00, 2.50])
async def test_a_peer_at_or_over_its_ceiling_is_refused_with_the_code_and_nothing_runs(
    tmp_path, caplog, spent,
):
    """Spent equals the ceiling is spent: the row that reached it was served,
    the next call is not."""
    coord, svc = _vendor_host(tmp_path)
    _spend(coord._ledger, GUEST, spent)
    before = _requests(coord._ledger)

    with caplog.at_level(logging.WARNING, logger="dpc_client_core.p2p_coordinator"):
        await coord.handle_inference_request(GUEST, "req-1", "ping")

    svc.llm_manager.query.assert_not_awaited()
    payload = _refusal(svc)
    assert payload["code"] == "insufficient_quota"
    assert VENDOR in payload["error"] and f"${spent:.4f}" in payload["error"]
    assert "$1.00" in payload["error"] and "vendor_quotas" in payload["error"]
    assert _requests(coord._ledger) == before, "a refused call is not a call"
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 1
    assert GUEST in warned[0] and VENDOR in warned[0]
    assert f"${spent:.4f}" in warned[0] and "$1.00" in warned[0]


@pytest.mark.asyncio
async def test_the_ceiling_counts_this_callers_own_rows_and_no_others(tmp_path):
    """Per caller, not per node: another peer's spending is its own, and so is
    what this node spent on its own gateway under a different `caller_kind`."""
    coord, svc = _vendor_host(tmp_path)
    _spend(coord._ledger, OTHER, 5.00)
    _spend(coord._ledger, GUEST, 5.00, caller_kind="gateway")
    _spend(coord._ledger, GUEST, 5.00, alias=LOCAL)
    _spend(coord._ledger, GUEST, 0.99)

    await coord.handle_inference_request(GUEST, "req-1", "ping")

    svc.llm_manager.query.assert_awaited_once()


@pytest.mark.asyncio
async def test_yesterdays_spending_does_not_bind_today(tmp_path):
    coord, svc = _vendor_host(tmp_path)
    _spend(coord._ledger, GUEST, 9.00, at=datetime.now(timezone.utc) - timedelta(days=1))

    await coord.handle_inference_request(GUEST, "req-1", "ping")

    svc.llm_manager.query.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_restart_changes_nothing_because_the_ledger_is_the_source(tmp_path):
    """The falsifier for «persistent»: a second coordinator with a second
    `NodeLedger` over the same directory — what a restarted node has — refuses
    the same call the first one refused."""
    first, _ = _vendor_host(tmp_path)
    _spend(first._ledger, GUEST, QUOTA)

    second, svc = _vendor_host(tmp_path)
    assert second._ledger is not first._ledger
    await second.handle_inference_request(GUEST, "req-1", "ping")

    svc.llm_manager.query.assert_not_awaited()
    assert _refusal(svc)["code"] == "insufficient_quota"


@pytest.mark.asyncio
async def test_a_vendor_alias_with_no_ceiling_serves_nothing(tmp_path):
    """Absent is not unlimited. A vendor alias without a ceiling is refused
    when the rules are read, so a list that reaches the door without one never
    passed that check and is not trusted here."""
    coord, svc = _vendor_host(tmp_path, quotas={})

    await coord.handle_inference_request(GUEST, "req-1", "ping")

    svc.llm_manager.query.assert_not_awaited()
    assert _refusal(svc)["code"] == "insufficient_quota"


@pytest.mark.asyncio
async def test_the_lists_come_from_the_gateways_own_object_when_this_node_has_one(tmp_path):
    """One classification for both doors. The gateway here is built over a
    second core service, whose ceiling is zero, only so that the two can be
    told apart: the door refuses, which the coordinator's own firewall — quota
    $9 and nothing spent — would not have done."""
    coord, svc = _vendor_host(tmp_path, quotas={VENDOR: 9.00})
    strict = SimpleNamespace(
        firewall=SimpleNamespace(
            classify_serving_lists=lambda types: ServingLists(
                local=(), vendor=(VENDOR,), quotas={VENDOR: 0.0}),
        ),
        llm_manager=SimpleNamespace(providers=svc.llm_manager.providers),
    )
    svc.gateway = SimpleNamespace(gateway=Gateway(strict))

    await coord.handle_inference_request(GUEST, "req-1", "ping")

    svc.llm_manager.query.assert_not_awaited()
    assert _refusal(svc)["code"] == "insufficient_quota"


# --- the classes, on a real firewall ----------------------------------------


@pytest.mark.asyncio
async def test_a_local_alias_is_never_refused_by_this_gate(tmp_path):
    """The card's bound is the queue, not money: a local alias is served
    however much has been spent on it, and no ceiling applies to it."""
    coord, svc = _real_firewall_host(
        tmp_path,
        {"allow_nodes": [GUEST], "serving_local": [LOCAL]},
        {LOCAL: SimpleNamespace(config={"type": "ollama"})},
    )
    _spend(coord._ledger, GUEST, 500.00, alias=LOCAL)

    await coord.handle_inference_request(GUEST, "req-1", "ping")

    svc.llm_manager.query.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_paying_alias_misfiled_under_serving_local_is_refused_not_served(tmp_path):
    """The state the load path can reach today, and the one that costs money:
    `serving_local` naming a vendor-typed alias is a configuration error the
    gateway refuses, and this door served it. Refused now, with the reason and
    with no word for the guest to act on — it is the host's own configuration."""
    coord, svc = _real_firewall_host(
        tmp_path,
        {"allow_nodes": [GUEST], "serving_local": [VENDOR]},
        {VENDOR: SimpleNamespace(config={"type": "deepseek"})},
    )

    await coord.handle_inference_request(GUEST, "req-1", "ping")

    svc.llm_manager.query.assert_not_awaited()
    payload = _refusal(svc)
    assert payload.get("code") in (None, "")
    assert VENDOR in payload["error"] and "serving_vendor" in payload["error"]
    assert _requests(coord._ledger) == []


# --- an alias nobody can price ----------------------------------------------


@pytest.mark.asyncio
async def test_a_vendor_alias_this_node_cannot_price_is_refused_before_it_runs(tmp_path, caplog):
    """A ceiling is money, and money is counted from the rows. An alias no rate
    table knows writes $0.00 on every row, so `spent_today` never moves and
    `vendor_quotas` guards nothing — the meter is absent, not slow (Ark's
    review of `11b1de5c`, 2026-09-14). Refused with the word a spent ceiling
    already uses; the reason is in the text, because the wire vocabulary is
    Mike's to extend."""
    coord, svc = _vendor_host(tmp_path)
    svc.llm_manager.providers = {
        VENDOR: SimpleNamespace(config={"type": "anthropic", "model": "claude-sonnet-4-5"}),
    }

    with caplog.at_level(logging.WARNING, logger="dpc_client_core.p2p_coordinator"):
        await coord.handle_inference_request(GUEST, "req-1", "ping")

    svc.llm_manager.query.assert_not_awaited()
    payload = _refusal(svc)
    assert payload["code"] == "insufficient_quota"
    assert VENDOR in payload["error"] and "no rate" in payload["error"]
    assert "vendor_quotas" in payload["error"]
    assert _requests(coord._ledger) == [], "a refused call is not a call"
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 1, "one refusal, one line"
    assert VENDOR in warned[0] and "no rate" in warned[0] and GUEST in warned[0]


@pytest.mark.asyncio
async def test_a_priced_vendor_alias_under_its_ceiling_is_still_served(tmp_path):
    """The other arm of the same predicate: the refusal above is about the rate
    table, not about vendor aliases."""
    coord, svc = _vendor_host(tmp_path)

    await coord.handle_inference_request(GUEST, "req-1", "ping")

    svc.llm_manager.query.assert_awaited_once()
    assert "req-1" in _requests(coord._ledger)
