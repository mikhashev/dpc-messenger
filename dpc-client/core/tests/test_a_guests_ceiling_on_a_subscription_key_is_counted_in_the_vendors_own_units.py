"""A-GUESTS-CEILING-ON-A-SUBSCRIPTION-KEY-IS-COUNTED-IN-THE-VENDORS-OWN-UNITS.

ADR-041 D5, amendment 2026-09-29. `compute.vendor_quotas` bounds a vendor
alias in money read from this node's own ledger rows; on a
`billing_mode: "subscription"` key those rows are always
`list_price_reference` (ADR-041 D3's amendment) and the ceiling never trips.
`guest_vendor_quota.guest_vendor_quota_refusal` is the guest ceiling counted
instead in the vendor's own units: requests per fixed window
(`vendor_request_quotas`), tokens per UTC day (`vendor_token_quotas`), and the
vendor's own `daily_capacity.exhausted` gate. One function, exercised here
directly (fast, no provider or wire) and once each through the peer door and
the gateway, so the two callers cannot diverge.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.firewall import ContextFirewall
from dpc_client_core.gateway import Gateway, GatewayError
from dpc_client_core.guest_vendor_quota import (
    account_siblings,
    guest_vendor_quota_refusal,
    seconds_to_utc_midnight,
)
from dpc_client_core.node_ledger import NodeLedger, usage_row
from dpc_client_core.p2p_coordinator import PEER_CALLER_KIND
from tests.test_p2p_coordinator import make_coordinator

ALIAS = "nd_free"
TWIN = "nd_free_noreason"
GUEST = "peer-1"
OTHER_GUEST = "peer-2"


def _row(ledger: NodeLedger, *, caller: str, alias: str, at: datetime,
         caller_kind: str = PEER_CALLER_KIND, prompt_tokens: int = 100,
         completion_tokens: int = 50) -> None:
    ledger.append(usage_row(
        request_id=f"{caller}-{alias}-{at.isoformat()}",
        caller=caller, caller_kind=caller_kind, alias=alias, model="m",
        route="local", prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        thinking_tokens=None, counts_source="engine", started_at=at, duration_s=0.1,
        billing="subscription", cost_amount=0.0, cost_currency="RUB",
        cost_basis="list_price_reference",
    ))


def _session_window(resets_at: datetime, *, used: int, limit: int, reset_in_sec: int = 1000) -> dict:
    return {
        "name": "3h", "unit": "requests", "used": used, "limit": limit,
        "remaining": limit - used, "resets_at": resets_at.isoformat().replace("+00:00", "Z"),
        "reset_in_sec": reset_in_sec,
    }


def _quota(*windows: dict, daily_capacity: dict = None, blockers=None) -> dict:
    return {
        "windows": list(windows),
        "daily_capacity": daily_capacity or {"exhausted": False},
        "blockers": blockers or [],
    }


# --- the pure function -------------------------------------------------------


def test_a_guest_at_its_per_session_ceiling_is_refused_with_retry_after(tmp_path):
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    resets_at = now + timedelta(hours=1)
    ledger = NodeLedger(tmp_path / "ledger")
    window_start = resets_at - timedelta(hours=3)
    _row(ledger, caller=GUEST, alias=ALIAS, at=window_start + timedelta(minutes=1))
    _row(ledger, caller=GUEST, alias=ALIAS, at=window_start + timedelta(minutes=2))

    refusal = guest_vendor_quota_refusal(
        alias=ALIAS, caller=GUEST, billing_mode="subscription",
        quota=_quota(_session_window(resets_at, used=2, limit=400, reset_in_sec=3600)),
        ledger=ledger, account_id_of={ALIAS: "acct-1"},
        request_quotas={ALIAS: {"per_session": 2}}, token_quotas={}, owner_reserve={},
        now=now,
    )

    assert refusal is not None
    assert refusal["code"] == "insufficient_quota"
    assert refusal["retry_after_sec"] == 3600
    assert "2 request" in refusal["message"] and "per_session" in refusal["message"]


def test_a_guest_under_its_ceiling_is_admitted(tmp_path):
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    resets_at = now + timedelta(hours=1)
    ledger = NodeLedger(tmp_path / "ledger")
    window_start = resets_at - timedelta(hours=3)
    _row(ledger, caller=GUEST, alias=ALIAS, at=window_start + timedelta(minutes=1))

    refusal = guest_vendor_quota_refusal(
        alias=ALIAS, caller=GUEST, billing_mode="subscription",
        quota=_quota(_session_window(resets_at, used=1, limit=400)),
        ledger=ledger, account_id_of={ALIAS: "acct-1"},
        request_quotas={ALIAS: {"per_session": 2}}, token_quotas={}, owner_reserve={},
        now=now,
    )

    assert refusal is None


def test_alias_and_its_noreason_twin_share_one_account_count(tmp_path):
    """An alias and its `-noreason` twin share one key: a request on the twin
    counts against the ceiling checked on the named alias."""
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    resets_at = now + timedelta(hours=1)
    ledger = NodeLedger(tmp_path / "ledger")
    window_start = resets_at - timedelta(hours=3)
    _row(ledger, caller=GUEST, alias=TWIN, at=window_start + timedelta(minutes=1))

    account_id_of = {ALIAS: "acct-1", TWIN: "acct-1"}
    assert sorted(account_siblings(ALIAS, account_id_of)) == sorted([ALIAS, TWIN])

    refusal = guest_vendor_quota_refusal(
        alias=ALIAS, caller=GUEST, billing_mode="subscription",
        quota=_quota(_session_window(resets_at, used=1, limit=400)),
        ledger=ledger, account_id_of=account_id_of,
        request_quotas={ALIAS: {"per_session": 1}}, token_quotas={}, owner_reserve={},
        now=now,
    )

    assert refusal is not None, "the twin's row must count against the named alias's ceiling"


def test_a_row_just_before_the_window_start_is_not_counted(tmp_path):
    """The window start is `resets_at` minus its length, not a rolling
    lookback: a row one second older than the window start does not count."""
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    resets_at = now + timedelta(hours=1)
    window_start = resets_at - timedelta(hours=3)
    ledger = NodeLedger(tmp_path / "ledger")
    _row(ledger, caller=GUEST, alias=ALIAS, at=window_start - timedelta(seconds=1))

    refusal = guest_vendor_quota_refusal(
        alias=ALIAS, caller=GUEST, billing_mode="subscription",
        quota=_quota(_session_window(resets_at, used=0, limit=400)),
        ledger=ledger, account_id_of={ALIAS: "acct-1"},
        request_quotas={ALIAS: {"per_session": 1}}, token_quotas={}, owner_reserve={},
        now=now,
    )

    assert refusal is None, "a row older than the window start is out of it"


def test_the_window_can_cross_a_month_boundary(tmp_path):
    """`rows_since` reads every month partition the window touches: a
    session window starting in August and a call made in September must both
    be counted, and the previous month's partition is read for it."""
    now = datetime(2026, 9, 1, 1, 0, tzinfo=timezone.utc)
    resets_at = now + timedelta(hours=1)
    window_start = resets_at - timedelta(hours=3)  # 2026-08-31 23:00 UTC
    assert window_start.month == 8
    ledger = NodeLedger(tmp_path / "ledger")
    _row(ledger, caller=GUEST, alias=ALIAS, at=window_start + timedelta(minutes=1))

    refusal = guest_vendor_quota_refusal(
        alias=ALIAS, caller=GUEST, billing_mode="subscription",
        quota=_quota(_session_window(resets_at, used=1, limit=400)),
        ledger=ledger, account_id_of={ALIAS: "acct-1"},
        request_quotas={ALIAS: {"per_session": 1}}, token_quotas={}, owner_reserve={},
        now=now,
    )

    assert refusal is not None, "the previous month's partition must be read for the window"


def test_the_aggregate_reserve_is_reached_by_several_guests(tmp_path):
    """`vendor_owner_reserve` keeps a fraction of the vendor window for the
    owner: several guests together, none individually over its own
    per_session ceiling, still trip the aggregate."""
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    resets_at = now + timedelta(hours=1)
    window_start = resets_at - timedelta(hours=3)
    ledger = NodeLedger(tmp_path / "ledger")
    _row(ledger, caller=GUEST, alias=ALIAS, at=window_start + timedelta(minutes=1))
    _row(ledger, caller=OTHER_GUEST, alias=ALIAS, at=window_start + timedelta(minutes=2))
    _row(ledger, caller=OTHER_GUEST, alias=ALIAS, at=window_start + timedelta(minutes=3))

    refusal = guest_vendor_quota_refusal(
        alias=ALIAS, caller=GUEST, billing_mode="subscription",
        # limit=4, reserve=0.25 -> guest ceiling = 3; 3 guest rows already exist
        quota=_quota(_session_window(resets_at, used=3, limit=4)),
        ledger=ledger, account_id_of={ALIAS: "acct-1"},
        request_quotas={ALIAS: {"per_session": 100}}, token_quotas={},
        owner_reserve={ALIAS: 0.25},
        now=now,
    )

    assert refusal is not None
    assert "guest reserve" in refusal["message"]


def test_a_subscription_alias_without_a_request_quota_entry_is_refused(tmp_path):
    ledger = NodeLedger(tmp_path / "ledger")
    refusal = guest_vendor_quota_refusal(
        alias=ALIAS, caller=GUEST, billing_mode="subscription",
        quota=_quota(),
        ledger=ledger, account_id_of={}, request_quotas={}, token_quotas={}, owner_reserve={},
    )
    assert refusal is not None
    assert refusal["code"] == "insufficient_quota"
    assert "no" in refusal["message"] and "vendor_request_quotas" in refusal["message"]


def test_an_unknown_billing_mode_is_refused_for_a_guest(tmp_path):
    ledger = NodeLedger(tmp_path / "ledger")
    refusal = guest_vendor_quota_refusal(
        alias=ALIAS, caller=GUEST, billing_mode=None, quota=None,
        ledger=ledger, account_id_of={}, request_quotas={}, token_quotas={}, owner_reserve={},
    )
    assert refusal is not None
    assert refusal["code"] == "insufficient_quota"


def test_a_wallet_key_is_admitted_here_and_keeps_the_existing_money_ceiling(tmp_path):
    """`billing_mode != "subscription"` (a pay-per-use wallet) is not this
    function's business: it admits, and `spent_today`'s money ceiling is the
    whole answer, checked separately by the caller."""
    ledger = NodeLedger(tmp_path / "ledger")
    refusal = guest_vendor_quota_refusal(
        alias=ALIAS, caller=GUEST, billing_mode="pay_per_use", quota=_quota(),
        ledger=ledger, account_id_of={}, request_quotas={}, token_quotas={}, owner_reserve={},
    )
    assert refusal is None


def test_daily_capacity_exhausted_is_refused_with_seconds_to_utc_midnight(tmp_path):
    now = datetime(2026, 9, 29, 23, 0, tzinfo=timezone.utc)
    ledger = NodeLedger(tmp_path / "ledger")
    refusal = guest_vendor_quota_refusal(
        alias=ALIAS, caller=GUEST, billing_mode="subscription",
        quota=_quota(daily_capacity={"exhausted": True}),
        ledger=ledger, account_id_of={ALIAS: "acct-1"},
        request_quotas={ALIAS: {"per_session": 0}}, token_quotas={}, owner_reserve={},
        now=now,
    )
    assert refusal is not None
    assert refusal["retry_after_sec"] == seconds_to_utc_midnight(now) == 3600


def test_a_guest_token_per_day_ceiling_is_refused(tmp_path):
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    ledger = NodeLedger(tmp_path / "ledger")
    _row(ledger, caller=GUEST, alias=ALIAS, at=midnight + timedelta(hours=1),
         prompt_tokens=100, completion_tokens=50)

    refusal = guest_vendor_quota_refusal(
        alias=ALIAS, caller=GUEST, billing_mode="subscription",
        quota=_quota(),
        ledger=ledger, account_id_of={ALIAS: "acct-1"},
        request_quotas={ALIAS: {"per_session": 0}},
        token_quotas={ALIAS: {"per_day": 150}}, owner_reserve={},
        now=now,
    )

    assert refusal is not None
    assert "token" in refusal["message"]


def test_the_owner_caller_skips_per_guest_ceilings_but_not_the_daily_gate(tmp_path):
    ledger = NodeLedger(tmp_path / "ledger")
    # Over any guest ceiling that would apply, but the owner is not a guest.
    admitted = guest_vendor_quota_refusal(
        alias=ALIAS, caller="owner-node", billing_mode="subscription",
        quota=_quota(_session_window(datetime.now(timezone.utc), used=999, limit=1)),
        ledger=ledger, account_id_of={}, request_quotas={}, token_quotas={}, owner_reserve={},
        is_owner_caller=True,
    )
    assert admitted is None

    refused = guest_vendor_quota_refusal(
        alias=ALIAS, caller="owner-node", billing_mode="subscription",
        quota=_quota(daily_capacity={"exhausted": True}),
        ledger=ledger, account_id_of={}, request_quotas={}, token_quotas={}, owner_reserve={},
        is_owner_caller=True,
    )
    assert refused is not None, "the vendor's own daily gate binds the owner's calls too"


# --- NodeLedger.rows_since / count_since / tokens_since ----------------------


def test_count_since_reads_only_the_window(tmp_path):
    ledger = NodeLedger(tmp_path / "ledger")
    start = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)
    _row(ledger, caller=GUEST, alias=ALIAS, at=start - timedelta(minutes=1))
    _row(ledger, caller=GUEST, alias=ALIAS, at=start + timedelta(minutes=1))
    _row(ledger, caller=GUEST, alias=ALIAS, at=start + timedelta(minutes=2))

    n = ledger.count_since(
        aliases=[ALIAS], since=start, until=start + timedelta(hours=1),
        caller=GUEST, caller_kind=PEER_CALLER_KIND,
    )
    assert n == 2


def test_tokens_since_sums_prompt_and_completion(tmp_path):
    ledger = NodeLedger(tmp_path / "ledger")
    start = datetime(2026, 9, 29, 0, 0, tzinfo=timezone.utc)
    _row(ledger, caller=GUEST, alias=ALIAS, at=start + timedelta(hours=1),
         prompt_tokens=100, completion_tokens=50)
    _row(ledger, caller=GUEST, alias=ALIAS, at=start + timedelta(hours=2),
         prompt_tokens=200, completion_tokens=25)

    total = ledger.tokens_since(
        aliases=[ALIAS], since=start, until=start + timedelta(hours=3),
        caller=GUEST, caller_kind=PEER_CALLER_KIND,
    )
    assert total == 375


# --- through the peer door ---------------------------------------------------


def _subscription_provider(quota: dict):
    return SimpleNamespace(
        config={"type": "neuraldeep", "model": "qwen3.8-27b", "currency": "RUB"},
        model="qwen3.8-27b",
        billing_currency=lambda: "RUB",
        get_balance=AsyncMock(return_value={"billing_mode": "subscription", "quota": quota}),
    )


@pytest.mark.asyncio
async def test_the_peer_door_refuses_a_guest_over_its_request_quota_with_retry_after(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    rules = tmp_path / "privacy_rules.json"
    rules.write_text(json.dumps({"compute": {
        "enabled": True,
        "allow_nodes": [GUEST],
        "serving_local": [],
        "serving_vendor": [ALIAS],
        "vendor_quotas": {ALIAS: 1000.0},
        "vendor_request_quotas": {ALIAS: {"per_session": 1}},
    }}), encoding="utf-8")

    now = datetime.now(timezone.utc)
    resets_at = now + timedelta(hours=1)
    window_start = resets_at - timedelta(hours=3)

    coord, svc = make_coordinator()
    svc.firewall = ContextFirewall(rules)
    svc.gateway = None
    svc.llm_manager.providers = {
        ALIAS: _subscription_provider(_quota(_session_window(resets_at, used=1, limit=400, reset_in_sec=222))),
    }
    svc.llm_manager.query = AsyncMock(return_value={"response": "pong", "model": "m"})
    coord._ledger = NodeLedger(tmp_path / "ledger")
    _row(coord._ledger, caller=GUEST, alias=ALIAS, at=window_start + timedelta(minutes=1))

    await coord.handle_inference_request(GUEST, "req-1", "ping", provider=ALIAS)

    svc.llm_manager.query.assert_not_awaited()
    message = svc.p2p_manager.send_message_to_peer.call_args[0][1]
    payload = message["payload"]
    assert payload["status"] == "error"
    assert payload["code"] == "insufficient_quota"
    assert payload["retry_after_sec"] == 222


# --- through the gateway -----------------------------------------------------


def _gateway_lists_and_ledger(tmp_path):
    rules = tmp_path / "privacy_rules.json"
    rules.write_text(json.dumps({"compute": {
        "enabled": True,
        "serving_local": [],
        "serving_vendor": [ALIAS],
        "vendor_quotas": {ALIAS: 1000.0},
    }}), encoding="utf-8")
    firewall = ContextFirewall(rules)
    ledger = NodeLedger(tmp_path / "ledger")
    return firewall, ledger


@pytest.mark.asyncio
async def test_the_gateway_refuses_429_with_retry_after_on_daily_capacity_exhausted(tmp_path):
    firewall, ledger = _gateway_lists_and_ledger(tmp_path)
    core = SimpleNamespace(
        firewall=firewall,
        llm_manager=SimpleNamespace(providers={
            ALIAS: _subscription_provider(_quota(daily_capacity={"exhausted": True})),
        }),
        p2p_manager=SimpleNamespace(node_id="owner-node"),
        settings=SimpleNamespace(get_remote_inference_timeout=lambda: 30.0),
    )
    gw = Gateway(core, ledger=ledger)

    with pytest.raises(GatewayError) as exc_info:
        await gw.complete(ALIAS, "ping", request_id="r1")

    assert exc_info.value.status == 429
    assert exc_info.value.code == "insufficient_quota"
    assert exc_info.value.retry_after_sec == pytest.approx(seconds_to_utc_midnight(datetime.now(timezone.utc)), abs=5)

