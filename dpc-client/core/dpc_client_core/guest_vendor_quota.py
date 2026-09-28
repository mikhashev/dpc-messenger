"""The guest ceiling on a subscription-billed vendor alias, counted in the
vendor's own units (ADR-041 D5, amendment 2026-09-29).

`compute.vendor_quotas` bounds a vendor alias in money, read from this node's
own ledger rows. On a `billing_mode: "subscription"` key that ceiling never
trips: the rows it counts are `list_price_reference`, what a call *would*
have cost against a metered key, and are excluded from `spent_today` by
design (ADR-041 D3's amendment). A subscription alias served to guests still
spends something real — the vendor's own request windows (a fixed count per
"3h" grid or per ISO week) and its money-based daily-capacity gate — so this
module counts *those* instead, one function used by both doors (the peer
door's `p2p_coordinator._vendor_quota_refusal` and the gateway's own vendor
branch) so they cannot diverge.

Config, in `privacy_rules.json` under `compute`, keyed by alias like
`vendor_quotas` (`provider_alias_refs._FIREWALL_KEYED_BY_ALIAS` carries a
rename):

- `vendor_request_quotas: {alias: {per_session: int, per_week: int}}` —
  requests per guest per vendor window.
- `vendor_token_quotas: {alias: {per_day: int}}` — prompt+completion tokens
  per guest per UTC day, the proxy for the vendor's money-based daily gate.
  Optional.
- `vendor_owner_reserve: {alias: float}` — fraction (0..1) of each vendor
  window kept for the owner; default 0.25 when an alias names none.

Applies only to a guest call (`caller_kind == "peer"`) on an alias whose
key's `billing_mode` is `subscription`; a wallet key keeps the existing
`spent_today` money ceiling untouched, and `vendor_request_quotas` /
`vendor_token_quotas`, if set on a wallet alias, are simply not read here.
An unknown `billing_mode` (the read failed, is stale past its own TTL, or the
provider gives none) refuses fail-closed: never guessed as either kind.

The resource counted is the vendor **account**, not the alias: an alias and
its `-noreason` twin share one key, so a request or token quota is checked
against every alias on the same `CoreService._provider_account_id`, not the
one named in the call. The quota *limit* applied is still the called alias's
own `vendor_request_quotas` / `vendor_token_quotas` entry.

A window's start is the vendor's own, never rolling: `resets_at` minus the
window's length (3h for the session window, 7 days for the week window,
read from the window's own `name`/`window` field). An unknown `resets_at`
refuses rather than guesses a start.

Known, accepted drift (stated, not fixed, here): no per-guest requests-per-
minute ceiling (a retried 429 from the host's own backoff hits the vendor
more than once per ledger row — card
A-GUEST-WAITS-UP-TO-TEN-MINUTES-WHILE-THE-HOST-RETRIES-A-429-ON-A-SHARED-
VENDOR-KEY) and the ordinary check-then-act overrun already noted beside
`spent_today` (a row is written only after a call returns, so calls in
flight at once all pass on the same count).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger(__name__)

# The one refusal code this ceiling ever sends. Only `insufficient_quota`
# clears by itself (DPTP §3.4), which is true here too: a spent request
# window, a spent token day and an exhausted daily-capacity gate all clear on
# their own at a known time. A configuration gap (no `vendor_request_quotas`
# entry, no readable `billing_mode`, no `resets_at`) is refused the same way
# because the guest cannot tell the two apart from the outside, and both are
# "come back later" from where the guest sits — the text says which.
GUEST_VENDOR_QUOTA_REFUSAL_CODE = "insufficient_quota"

_WINDOW_LENGTHS = {
    "3h": timedelta(hours=3),
    "week": timedelta(days=7),
}

DEFAULT_OWNER_RESERVE = 0.25


def account_siblings(alias: str, account_id_of: Dict[str, Optional[str]]) -> list:
    """Every alias sharing `alias`'s vendor account, `alias` included.

    `account_id_of` maps every loaded alias to its account id
    (`CoreService._provider_account_id`); an alias missing from it, or mapped
    to `None`, is treated as its own single-alias account.
    """
    account = account_id_of.get(alias)
    if account is None:
        return [alias]
    return [a for a, acc in account_id_of.items() if acc == account] or [alias]


def _window_length(window: Dict[str, Any]) -> Optional[timedelta]:
    name = window.get("name")
    if not name:
        return None
    return _WINDOW_LENGTHS.get(str(name).lower())


def _window_start(window: Dict[str, Any]) -> Optional[datetime]:
    resets_at = window.get("resets_at")
    if not resets_at:
        return None
    length = _window_length(window)
    if length is None:
        return None
    try:
        resets_dt = datetime.fromisoformat(str(resets_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if resets_dt.tzinfo is None:
        resets_dt = resets_dt.replace(tzinfo=timezone.utc)
    return resets_dt - length


def seconds_to_utc_midnight(now: datetime) -> int:
    now = now.astimezone(timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(0, int((tomorrow - now).total_seconds()))


def _refusal(message: str, retry_after_sec: Optional[float] = None) -> Dict[str, Any]:
    return {
        "message": message,
        "code": GUEST_VENDOR_QUOTA_REFUSAL_CODE,
        "retry_after_sec": retry_after_sec,
    }


def guest_vendor_quota_refusal(
    *,
    alias: str,
    caller: str,
    billing_mode: Optional[str],
    quota: Optional[Dict[str, Any]],
    ledger: Any,
    account_id_of: Dict[str, Optional[str]],
    request_quotas: Dict[str, Dict[str, Any]],
    token_quotas: Dict[str, Dict[str, Any]],
    owner_reserve: Dict[str, float],
    is_owner_caller: bool = False,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """`None` admits the call; a dict refuses it: `{message, code,
    retry_after_sec}`. Called on an alias already resolved to `owner ==
    "vendor"` with a known currency; this function does not reclassify it.

    `is_owner_caller` is true for the gateway's own loopback client (ADR-041
    D3: never a peer). Per-guest ceilings are skipped for the owner; the
    vendor's daily-capacity gate is not, since it binds every caller.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    if billing_mode != "subscription":
        if is_owner_caller:
            return None
        if billing_mode is None or quota is None:
            return _refusal(
                f"model '{alias}' cannot be bounded for a guest: this node could not read this "
                "key's billing_mode / quota from the vendor (unread, stale, or the provider "
                "gives none); refused rather than served with no ceiling"
            )
        # Wallet key: the existing spent_today money ceiling is the whole
        # answer, checked by the caller before or after this function.
        return None

    if quota is None:
        if is_owner_caller:
            return None
        return _refusal(
            f"model '{alias}' cannot be bounded for a guest: this node could not read this "
            "subscription key's quota from the vendor (unread or stale); refused rather than "
            "served with no ceiling"
        )

    if is_owner_caller:
        daily_capacity = quota.get("daily_capacity") or {}
        blockers = quota.get("blockers") or []
        if daily_capacity.get("exhausted") or "daily_capacity_exhausted" in blockers:
            return _refusal(
                f"model '{alias}' is refused: the vendor's daily money budget is exhausted "
                "(daily_capacity.exhausted); it is served again after UTC midnight",
                seconds_to_utc_midnight(now),
            )
        return None

    req_quota = request_quotas.get(alias) or {}
    if not req_quota:
        return _refusal(
            f"model '{alias}' is a subscription vendor alias with no "
            f"compute.vendor_request_quotas entry for it; a guest ceiling on it cannot be "
            "enforced, so it is refused rather than served unbounded"
        )

    siblings = account_siblings(alias, account_id_of)
    windows_by_name = {
        str(w.get("name")).lower(): w for w in (quota.get("windows") or []) if isinstance(w, dict)
    }
    reserve = owner_reserve.get(alias, DEFAULT_OWNER_RESERVE)

    for period_key, window_name in (("per_session", "3h"), ("per_week", "week")):
        limit = req_quota.get(period_key)
        if not limit:
            continue
        window = windows_by_name.get(window_name)
        if not isinstance(window, dict) or not window.get("resets_at"):
            return _refusal(
                f"model '{alias}': the vendor's {window_name} window carries no resets_at; the "
                f"compute.vendor_request_quotas.{period_key} ceiling cannot be bounded, so the "
                "call is refused rather than served unbounded"
            )
        start = _window_start(window)
        if start is None:
            return _refusal(
                f"model '{alias}': the vendor's {window_name} window start could not be "
                "computed from resets_at; refused rather than served unbounded"
            )
        reset_in = window.get("reset_in_sec")
        retry_after = float(reset_in) if isinstance(reset_in, (int, float)) else None

        guest_count = ledger.count_since(
            aliases=siblings, since=start, until=now, caller=caller, caller_kind="peer",
        )
        if guest_count >= int(limit):
            return _refusal(
                f"model '{alias}' is refused: {caller} has made {guest_count} request(s) in the "
                f"vendor's current {window_name} window, at or over "
                f"compute.vendor_request_quotas.{period_key} of {limit}; it is served again when "
                "the vendor's window resets",
                retry_after,
            )

        vendor_limit = window.get("limit")
        if isinstance(vendor_limit, (int, float)) and vendor_limit > 0:
            guest_ceiling = (1 - reserve) * vendor_limit
            all_guests_count = ledger.count_since(
                aliases=siblings, since=start, until=now, caller_kind="peer",
            )
            if all_guests_count >= guest_ceiling:
                return _refusal(
                    f"model '{alias}' is refused: all guests together have made "
                    f"{all_guests_count} request(s) in the vendor's current {window_name} "
                    f"window, at or over the guest reserve of {guest_ceiling:.1f} "
                    f"(compute.vendor_owner_reserve={reserve} of the vendor's own "
                    f"{vendor_limit} limit); it is served again when the vendor's window resets",
                    retry_after,
                )

    daily_capacity = quota.get("daily_capacity") or {}
    blockers = quota.get("blockers") or []
    if daily_capacity.get("exhausted") or "daily_capacity_exhausted" in blockers:
        return _refusal(
            f"model '{alias}' is refused: the vendor's daily money budget is exhausted "
            "(daily_capacity.exhausted); it is served again after UTC midnight",
            seconds_to_utc_midnight(now),
        )

    tok_quota = token_quotas.get(alias) or {}
    per_day = tok_quota.get("per_day")
    if per_day:
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        used = ledger.tokens_since(
            aliases=siblings, since=midnight, until=now, caller=caller, caller_kind="peer",
        )
        if used >= int(per_day):
            return _refusal(
                f"model '{alias}' is refused: {caller} has used {used} token(s) today of "
                f"compute.vendor_token_quotas.per_day={per_day}; it is served again after UTC "
                "midnight",
                seconds_to_utc_midnight(now),
            )
    return None
