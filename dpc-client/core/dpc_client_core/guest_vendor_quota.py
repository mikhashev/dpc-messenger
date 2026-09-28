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
branch): the *rule* is one rule, read from one place, so the two doors cannot
apply two different ceilings. What they still legitimately differ on is
**what each does when the ceiling cannot be read at all** (P2b, this
amendment): both first reach for the provider's last known-good
`/limits` read — `NeuralDeepProvider.get_balance()`'s own
`STALE_QUOTA_MAX_AGE` (5 minutes) fallback — so a few seconds of vendor
flakiness never flips either door's answer. Past that staleness window the
two *doors* diverge on purpose, by `is_owner_caller`, which this function
takes as given rather than deciding: the peer door has nothing else standing
between a guest and the vendor's key, so an unreadable ceiling refuses
fail-closed; the gateway's owner path is the owner's own loopback client, for
whom `compute.vendor_quotas` was never the whole story and an unreadable
quota is the vendor's stop, not this node's, so it proceeds fail-open (see
`is_owner_caller` below, and `Gateway._refuse_vendor_daily_capacity_exhausted`'s
own docstring). This asymmetry is deliberate, not a bug the two callers happen
to share.

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

Which ledger rows count (P3, 2026-09-29, documented rather than filtered
here): `count_since`/`tokens_since` are asked with no `route` filter, and none
is needed. Every row a guest ceiling here could see was written by
`P2PCoordinator._record_peer_call`, which always writes `route="local"`
(this node ran the call) and `caller_kind="peer"` (a guest asked for it) — the
one shape a served guest call takes. A `route="peer"` row (this node
*consuming* a call from one of its own peers) never carries `caller_kind=
"peer"`, because the asker there is a local agent or this node's own gateway
client, never a remote guest; such a row is invisible to `caller_kind="peer"`
regardless of `route`. So the filter that matters is already the one applied
— `aliases` (the account's siblings) plus `caller`/`caller_kind="peer"` — and
`route` narrows nothing further.

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
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional

from dpc_protocol.protocol import REFUSAL_INSUFFICIENT_QUOTA, REFUSAL_MISCONFIGURED

logger = logging.getLogger(__name__)

# The one refusal code most of this ceiling's refusals send. Only
# `insufficient_quota` clears by itself (DPTP §3.4), which is true here too: a
# spent request window, a spent token day and an exhausted daily-capacity gate
# all clear on their own at a known time. A window with no `resets_at`, or a
# guest ceiling this node cannot determine the vendor account for, is refused
# with `misconfigured` instead (P3, 2026-09-29): waiting does not clear either
# one, only the owner's own configuration does, so a receiver must not be told
# "come back later" and then loop on a 429 forever.
GUEST_VENDOR_QUOTA_REFUSAL_CODE = REFUSAL_INSUFFICIENT_QUOTA

_WINDOW_LENGTHS = {
    "3h": timedelta(hours=3),
    "week": timedelta(days=7),
}

DEFAULT_OWNER_RESERVE = 0.25

#: The wallet values a vendor may report. NeuralDeep's own client shows its
#: hub sending "wallet" (coddy-agent docs/plans/neuraldeep-usage.md, captured
#: /limits payload); "pay_per_use" is this project's word for the same billing
#: (docs/CONFIGURATION.md) and is accepted too. Anything else is unknown and
#: refused (P3, 2026-09-29) rather than treated as a wallet.
KNOWN_WALLET_BILLING_MODES = frozenset({"wallet", "pay_per_use"})


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


def _window_resets_at(window: Dict[str, Any]) -> Optional[datetime]:
    """The window's own `resets_at`, parsed and made timezone-aware; None
    where it is absent or will not parse."""
    resets_at = window.get("resets_at")
    if not resets_at:
        return None
    try:
        resets_dt = datetime.fromisoformat(str(resets_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if resets_dt.tzinfo is None:
        resets_dt = resets_dt.replace(tzinfo=timezone.utc)
    return resets_dt


def _window_start(window: Dict[str, Any]) -> Optional[datetime]:
    resets_dt = _window_resets_at(window)
    if resets_dt is None:
        return None
    length = _window_length(window)
    if length is None:
        return None
    return resets_dt - length


def _retry_after_for_window(window: Dict[str, Any], now: datetime) -> Optional[float]:
    """Seconds until this window resets, computed from its own `resets_at`
    at the moment of refusal (P3, 2026-09-29) — never the vendor's
    `reset_in_sec`, which is a snapshot from whenever `/limits` was last
    fetched and is already stale by the time a call is refused with it,
    sometimes by the length of the cache TTL or more. Clamped to at least 1
    second: a `resets_at` in the past (a clock skew, a window that just
    turned over between the read and this refusal) must still tell the guest
    to wait, not to retry with `Retry-After: 0` or a negative header."""
    resets_dt = _window_resets_at(window)
    if resets_dt is None:
        return None
    return max(1.0, (resets_dt - now).total_seconds())


def seconds_to_utc_midnight(now: datetime) -> int:
    now = now.astimezone(timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(0, int((tomorrow - now).total_seconds()))


def _refusal(
    message: str, retry_after_sec: Optional[float] = None, *, code: str = GUEST_VENDOR_QUOTA_REFUSAL_CODE,
) -> Dict[str, Any]:
    return {
        "message": message,
        "code": code,
        # `misconfigured` carries no Retry-After (P3, 2026-09-29): the gap is
        # the owner's configuration, not a clock, and a retry_after here would
        # tell a receiver a wait would help when it would not.
        "retry_after_sec": None if code == REFUSAL_MISCONFIGURED else retry_after_sec,
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
        if billing_mode not in KNOWN_WALLET_BILLING_MODES:
            # P3, 2026-09-29: `subscription` is handled above; a wallet value
            # (KNOWN_WALLET_BILLING_MODES) is bounded by the money ceiling.
            # Anything else is a value this node cannot place on either side,
            # so it is refused rather than defaulted to "must be a wallet".
            return _refusal(
                f"model '{alias}' reports billing_mode={billing_mode!r}, which this node knows "
                f"as neither 'subscription' nor a wallet {sorted(KNOWN_WALLET_BILLING_MODES)}; refused rather "
                "than guessed which ceiling — the guest one or the money one — applies"
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
        # P3, 2026-09-29: a gap in the owner's own configuration, not a spent
        # window — waiting does not close it, so it is `misconfigured` (503,
        # no Retry-After), not `insufficient_quota` (429, "come back later").
        return _refusal(
            f"model '{alias}' is a subscription vendor alias with no "
            f"compute.vendor_request_quotas entry for it; a guest ceiling on it cannot be "
            "enforced, so it is refused rather than served unbounded",
            code=REFUSAL_MISCONFIGURED,
        )

    # P3, 2026-09-29: the account id lookup answered explicitly that it does
    # not know which vendor account `alias` spends from (as opposed to `alias`
    # being absent from the map at all, which `account_siblings` reads as "no
    # sibling, count the alias alone" — a deliberate, narrower fallback). A
    # guest ceiling counted per alias where the key is in fact shared with
    # another alias would undercount the account's real traffic, so this is
    # refused rather than silently narrowed to a single-alias counter.
    if alias in account_id_of and account_id_of[alias] is None:
        return _refusal(
            f"model '{alias}': this node could not determine which vendor account it spends "
            "from (_provider_account_id answered none); a guest ceiling counted per alias "
            "instead of per account could undercount a key shared with another alias, so the "
            "call is refused rather than bounded by the wrong counter",
            code=REFUSAL_MISCONFIGURED,
        )

    siblings = account_siblings(alias, account_id_of)
    windows_by_name = {
        str(w.get("name")).lower(): w for w in (quota.get("windows") or []) if isinstance(w, dict)
    }
    reserve = owner_reserve.get(alias, DEFAULT_OWNER_RESERVE)

    for period_key, window_name in (("per_session", "3h"), ("per_week", "week")):
        limit = req_quota.get(period_key)
        if limit is None:
            continue
        # P3, 2026-09-29: `per_session`/`per_week` = 0 is the owner's explicit
        # "no guest calls on this window" — it must refuse, not be read as "no
        # ceiling configured" the way a falsy `if not limit` would.
        if limit == 0:
            return _refusal(
                f"model '{alias}' is refused: compute.vendor_request_quotas.{period_key} is 0 — "
                f"guests are not served this alias on its vendor's {window_name} window at all"
            )
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
        retry_after = _retry_after_for_window(window, now)

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

        # P2c/P3, 2026-09-29: the owner's reserve must protect the owner's
        # *own* share, not merely be counted from this node's own guest rows
        # (which see nothing of the owner's own calls, or of any other client
        # of the same key). The vendor's own `remaining` for the window
        # already nets out every call against the key, owner's included, so a
        # guest is admitted only while one more call would still leave the
        # reserve intact. `remaining` unknown, or a window with no `limit` at
        # all, refuses fail-closed rather than skip the check silently (P3:
        # "a window without limit in the aggregate check -> refuse, not
        # skip").
        vendor_limit = window.get("limit")
        if not isinstance(vendor_limit, (int, float)) or isinstance(vendor_limit, bool) or vendor_limit <= 0:
            return _refusal(
                f"model '{alias}': the vendor's {window_name} window carries no usable `limit`; "
                "the owner's reserve (compute.vendor_owner_reserve) cannot be protected without "
                "one, so the call is refused rather than served with the reserve unchecked",
                retry_after,
            )
        reserve_floor = math.floor(reserve * vendor_limit)
        remaining = window.get("remaining")
        if not isinstance(remaining, (int, float)) or isinstance(remaining, bool):
            return _refusal(
                f"model '{alias}': the vendor's {window_name} window carries no usable "
                "`remaining`; the owner's guest reserve cannot be read from it, so the call is "
                "refused fail-closed rather than served with the reserve unchecked",
                retry_after,
            )
        if remaining - 1 < reserve_floor:
            return _refusal(
                f"model '{alias}' is refused: the vendor's live remaining for its current "
                f"{window_name} window is {remaining}; one more call would reach inside the "
                f"guest reserve (compute.vendor_owner_reserve={reserve} of the vendor's own "
                f"{vendor_limit} limit, {reserve_floor} kept for the owner); it is served again "
                "when the vendor's window resets",
                retry_after,
            )

        guest_ceiling = math.floor((1 - reserve) * vendor_limit)
        all_guests_count = ledger.count_since(
            aliases=siblings, since=start, until=now, caller_kind="peer",
        )
        if all_guests_count >= guest_ceiling:
            return _refusal(
                f"model '{alias}' is refused: all guests together have made "
                f"{all_guests_count} request(s) in the vendor's current {window_name} "
                f"window, at or over the guest reserve of {guest_ceiling} "
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
    if per_day is not None:
        # P3, 2026-09-29: per_day=0 is "no guest calls", not "no ceiling".
        if per_day == 0:
            return _refusal(
                f"model '{alias}' is refused: compute.vendor_token_quotas.{alias}.per_day is 0 — "
                "guests are not served this alias at all"
            )
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
