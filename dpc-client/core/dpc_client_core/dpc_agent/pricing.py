"""
DPC Agent Pricing — convert token usage to USD cost per provider.

Subscription providers return 0.0 — they have no per-token cost, only
rate/concurrency limits handled in budget.py. z.ai used to be the example here,
back when it was reached through the GLM Coding Plan; it is now billed per token
like DeepSeek, and the entry that still called it a subscription was pricing real
spend at zero.

Pay-per-use providers (DeepSeek, z.ai) get real dollar cost from per-1M-token rates,
applying DeepSeek's cache-hit / cache-miss input split when the provider reports
it (prompt_cache_hit_tokens / prompt_cache_miss_tokens). reasoning_content tokens
are already counted inside completion_tokens, so the output rate covers them.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


# Subscription providers: no per-token cost. Unknown providers fall back to
# "default" (subscription, $0.0) — fail-safe so an un-mapped provider never
# silently accumulates a fake cost. Add an explicit entry only for legacy
# per-1k pay-per-use providers; pay-per-use rates by model live in
# PAY_PER_USE_RATES below.
PROVIDERS: Dict[str, Dict[str, float | str]] = {
    # There is deliberately no "zai" entry any more. It used to read
    # {"billing": "subscription", 0.0, 0.0} — correct while Z.AI was reached through
    # the GLM Coding Plan, and a silent lie the moment the provider moved to the
    # prepaid platform API, because `_resolve_provider_key` funnels every alias
    # starting with "zai" or "glm" into it. A real spend would have been reported as
    # $0.00 to the burn digest, the alert thresholds and the runway. GLM now prices
    # through the pay-per-use tables below, like DeepSeek.
    "default": {"billing": "subscription", "input_per_1k": 0.0, "output_per_1k": 0.0},
}


# Pay-per-use rates in USD per 1M tokens, resolved by model name. DeepSeek bills
# input at two rates — cache-hit far cheaper than cache-miss — plus output.
# Source: https://api-docs.deepseek.com/quick_start/pricing (read 2026-08-15).
#
# There are two tables because there are two tariffs, and a cost is a property
# of the moment a call was made. Keeping only the newer one would silently
# reprice every call made before the change; keeping only the older one is the
# defect this replaces — from the switchover minute every cost this system
# reported would have been understated by two to six times.
PAY_PER_USE_RATES: Dict[str, Dict[str, float]] = {
    "deepseek-v4-flash": {"cache_hit": 0.0028, "cache_miss": 0.14, "output": 0.28},
    "deepseek-v4-pro": {"cache_hit": 0.003625, "cache_miss": 0.435, "output": 0.87},
}

# From 2026-08-16 16:00 UTC. The vendor states off-peak as half of peak, so the
# base table here is off-peak and peak is exactly double — one number to keep
# right instead of two tables that can drift apart.
PAY_PER_USE_RATES_FROM_2026_08_16: Dict[str, Dict[str, float]] = {
    "deepseek-v4-flash": {"cache_hit": 0.007, "cache_miss": 0.22, "output": 0.66},
    "deepseek-v4-pro": {"cache_hit": 0.022, "cache_miss": 0.66, "output": 1.98},
}
NEW_TARIFF_FROM = datetime(2026, 8, 16, 16, 0, tzinfo=timezone.utc)
PEAK_MULTIPLIER = 2.0


# Z.AI's open-platform rates, USD per 1M tokens, in this module's three-slot
# shape: their "Cached Input" column is cache_hit, their "Input" column is
# cache_miss (it is the uncached price), their "Output" is output.
#
# Source: https://docs.z.ai/guides/overview/pricing, fetched 2026-08-23, and
# cross-checked row by row against the table Mike pasted from the same page the
# same day — five rows agree exactly (4.7-FlashX, 4.5-X, 4.5-AirX,
# 4-32B-0414-128K, 4.6V-Flash), which is why this is recorded as read rather
# than as remembered.
#
# Two rows carry a judgement rather than a number. GLM-4-32B-0414-128K and
# GLM-OCR print no cached-input price at all ("-" and "\"); their input price is
# used for both slots, which can only overcharge us in our own records and never
# under-report. The free models are entered as real zeros: a priced zero and an
# unknown model both read $0.00 in the digest, and only one of them is true.
#
# These rates have no tariff eras — Z.AI publishes one table, with no peak hours
# and no switchover date. That is why the same dict is spliced into both DeepSeek
# tariff tables below instead of being duplicated: one place to correct.
ZAI_RATES: Dict[str, Dict[str, float]] = {
    # text
    "glm-5.3": {"cache_hit": 0.26, "cache_miss": 1.4, "output": 4.4},
    "glm-5.2": {"cache_hit": 0.26, "cache_miss": 1.4, "output": 4.4},
    "glm-5.1": {"cache_hit": 0.26, "cache_miss": 1.4, "output": 4.4},
    "glm-5": {"cache_hit": 0.2, "cache_miss": 1.0, "output": 3.2},
    "glm-5-turbo": {"cache_hit": 0.24, "cache_miss": 1.2, "output": 4.0},
    "glm-4.7": {"cache_hit": 0.11, "cache_miss": 0.6, "output": 2.2},
    "glm-4.7-flashx": {"cache_hit": 0.01, "cache_miss": 0.07, "output": 0.4},
    "glm-4.7-flash": {"cache_hit": 0.0, "cache_miss": 0.0, "output": 0.0},
    "glm-4.6": {"cache_hit": 0.11, "cache_miss": 0.6, "output": 2.2},
    "glm-4.5": {"cache_hit": 0.11, "cache_miss": 0.6, "output": 2.2},
    "glm-4.5-x": {"cache_hit": 0.45, "cache_miss": 2.2, "output": 8.9},
    "glm-4.5-air": {"cache_hit": 0.03, "cache_miss": 0.2, "output": 1.1},
    "glm-4.5-airx": {"cache_hit": 0.22, "cache_miss": 1.1, "output": 4.5},
    "glm-4.5-flash": {"cache_hit": 0.0, "cache_miss": 0.0, "output": 0.0},
    "glm-4-32b-0414-128k": {"cache_hit": 0.1, "cache_miss": 0.1, "output": 0.1},
    # vision
    "glm-5v-turbo": {"cache_hit": 0.24, "cache_miss": 1.2, "output": 4.0},
    "glm-4.6v": {"cache_hit": 0.05, "cache_miss": 0.3, "output": 0.9},
    "glm-4.6v-flashx": {"cache_hit": 0.004, "cache_miss": 0.04, "output": 0.4},
    "glm-4.6v-flash": {"cache_hit": 0.0, "cache_miss": 0.0, "output": 0.0},
    "glm-4.5v": {"cache_hit": 0.11, "cache_miss": 0.6, "output": 1.8},
    "glm-ocr": {"cache_hit": 0.03, "cache_miss": 0.03, "output": 0.03},
}

PAY_PER_USE_RATES.update(ZAI_RATES)
PAY_PER_USE_RATES_FROM_2026_08_16.update(ZAI_RATES)


def _peak_applies(model_key: str) -> bool:
    """Whether the doubled hours are this model's vendor's rule at all.

    The peak windows below are DeepSeek's, and until Z.AI arrived every model in
    the tables was DeepSeek's too — so `rates_at` doubled by the clock alone and
    was right by accident. Splicing GLM into the same tables ends that accident:
    without this test every GLM call would be recorded at twice its price for
    seven hours of every weekday, in our records only, which is the direction
    nobody notices until the invoice disagrees.
    """
    return model_key.startswith("deepseek")

# Peak hours as the vendor writes them: "01:00 - 04:00 and 06:00 - 10:00 UTC".
# Read as half-open [start, end) — 04:00:00 itself is off-peak. The vendor does
# not say which side the boundary falls on; the reading is recorded here rather
# than left to whoever next reads the arithmetic, and it is worth at most a few
# seconds of billing either way.
PEAK_WINDOWS_UTC: Tuple[Tuple[time, time], ...] = (
    (time(1, 0), time(4, 0)),
    (time(6, 0), time(10, 0)),
)


# The vendor's calendar, not ours. Beijing is UTC+8 and keeps no daylight time,
# so a Beijing weekend runs from Friday 16:00 UTC to Sunday 16:00 UTC — a window
# that belongs to three different UTC days and to no UTC weekend.
BEIJING = timezone(timedelta(hours=8))

# From this moment the peak windows stop applying on Beijing weekends: "all calls
# made on weekends will be charged uniformly at the off-peak rate" (vendor notice
# of 2026-08-22, quoted whole in the team channel).
#
# The notice dates itself "00:00 (Beijing Time) on Sunday, August 23" and that is
# what is coded here, but it is internally inconsistent: 23 August 2026 is indeed
# a Sunday, while the weekend it describes begins on the Saturday. Warren read it
# 80/20 for the weekend window — Saturday 00:00 Beijing, i.e. 2026-08-21 16:00
# UTC — and still argued for the literal date, which is what this is.
#
# The reason is direction, not confidence. If the vendor started a day earlier
# than this constant, then during the gap we book peak where they charge off-peak
# and our own costs read high: the burn median rises, the alerts fire sooner and
# the runway looks shorter. Every one of those errs toward noticing. The opposite
# mistake — a rule that starts too early — under-reports real spend, which is the
# error nobody sees until the balance does.
#
# Settled by one call in the Saturday 16:00-24:00 UTC band checked against the
# invoice; until then the constant carries the ambiguity rather than hiding it.
WEEKEND_OFF_PEAK_FROM = datetime(2026, 8, 22, 16, 0, tzinfo=timezone.utc)


def _is_weekend_in_beijing(moment: datetime) -> bool:
    """Saturday or Sunday on the vendor's calendar, whatever weekday it is here."""
    return moment.astimezone(BEIJING).weekday() >= 5


def _is_peak(moment: datetime) -> bool:
    """Whether a moment falls inside DeepSeek's doubled hours.

    Two conditions, and the second one arrived late: the hour has to be inside a
    peak window *and* the day has to be a Beijing weekday. Reading only the clock
    was correct until 2026-08-23 and silently doubles every weekend peak-hour
    call afterwards — in our own records only, since the vendor bills the real
    rate either way. That asymmetry is what makes it worth fixing before the
    weekend rather than after: a call is priced once, when it is made, and no
    later repair reprices the line already written to events.jsonl.
    """
    when = moment.astimezone(timezone.utc)
    if when >= WEEKEND_OFF_PEAK_FROM and _is_weekend_in_beijing(when):
        return False
    return any(start <= when.time() < end for start, end in PEAK_WINDOWS_UTC)


def rates_at(model_key: str, moment: Optional[datetime] = None) -> Dict[str, float]:
    """The three per-1M rates in force for `model_key` at `moment` (default now).

    Before the switchover the old flat table applies and the hour is irrelevant;
    after it, the off-peak table, doubled inside the peak windows — and only for
    the vendor whose windows they are (`_peak_applies`).
    """
    when = moment or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    if when < NEW_TARIFF_FROM:
        if model_key in PAY_PER_USE_RATES:
            return PAY_PER_USE_RATES[model_key]
        # A model only the new tariff names, priced at a moment before that
        # tariff: no old rate for it exists. Its own table is the only figure
        # there is, and `compute_cost_usd` promises never to raise — a cost
        # meter that throws is a cost meter nobody calls.
    base = PAY_PER_USE_RATES_FROM_2026_08_16[model_key]
    if not _peak_applies(model_key) or not _is_peak(when):
        return base
    return {k: v * PEAK_MULTIPLIER for k, v in base.items()}


def _resolve_pay_model(provider_alias: str, model: Optional[str]) -> Optional[str]:
    """Which pay-per-use model this call bills as, or None if not pay-per-use.

    Resolves by model name first (authoritative — flash vs pro differ ~3x), then
    falls back to alias substring so a custom alias (e.g. "deepseek_pro") still
    matches when the model string is unavailable.

    This resolves a *name*, not a price: the price also depends on when the call
    was made, and folding the two together is what let one flat number stand in
    for a tariff that has hours.

    Membership is tested against **both** tables. Testing only the old one is a
    trap with a delay on it: the two carry the same two keys today, so the day a
    model exists in the new table alone it would resolve to None, fall through
    to "subscription", and be billed at **$0.00 in silence** — a cost meter
    reading zero because the model is unknown looks exactly like a cost meter
    reading zero because the calls were free (GLM 5.3, 2026-08-15).
    """
    if model:
        key = model.strip().lower()
        if key in PAY_PER_USE_RATES or key in PAY_PER_USE_RATES_FROM_2026_08_16:
            return key
    hay = f"{model or ''} {provider_alias or ''}".lower()
    if "deepseek" in hay:
        if "pro" in hay:
            return "deepseek-v4-pro"
        if "flash" in hay:
            return "deepseek-v4-flash"
    if "glm" in hay or "zai" in hay:
        squashed = _squash(hay)
        for key in _ZAI_KEYS_LONGEST_FIRST:
            if _squash(key) in squashed:
                return key
    _warn_unpriced_once(hay)
    return None


def _squash(text: str) -> str:
    """Lowercase alphanumerics only — 'zai_glm45_airx' and 'GLM-4.5-AirX' meet here.

    The aliases in this product spell the same model four ways (config alias,
    UI label, vendor model string, budget key), and they differ only in the
    punctuation they drop.
    """
    return "".join(c for c in text.lower() if c.isalnum())


# Longest first, because every shorter key is a prefix of a longer one:
# 'glm45' is inside 'glm45airx', and matching it first would price the AirX
# call at Air's rate — a fifth of the truth.
_ZAI_KEYS_LONGEST_FIRST: Tuple[str, ...] = tuple(
    sorted(ZAI_RATES, key=lambda k: len(_squash(k)), reverse=True)
)

_WARNED_UNPRICED: set = set()


def _warn_unpriced_once(hay: str) -> None:
    """Say once, in the log, that a call from a paid vendor is being booked at $0.

    Without this the failure is silent by construction: an unknown model falls
    through to the subscription branch and returns 0.0, which is exactly what a
    free model returns. The meter reads zero either way and nothing distinguishes
    'we know it is free' from 'we have never heard of it' — the shape that let
    the knowledge-commit metric read zero for four months.
    """
    if not any(v in hay for v in ("glm", "zai", "deepseek")):
        return
    marker = hay.strip()
    if marker in _WARNED_UNPRICED:
        return
    _WARNED_UNPRICED.add(marker)
    logger.warning(
        "No pay-per-use rate for %r — this call is being recorded at $0.00 while "
        "the vendor bills for it. Add the model to the rate table in pricing.py.",
        marker,
    )


def _resolve_provider_key(provider_alias: str) -> str:
    """Map a provider alias to a PROVIDERS key. Unknown aliases → 'default'.

    The z.ai branch that used to live here mapped every 'zai*'/'glm*' alias onto
    a subscription entry worth $0.00. It is gone with that entry: z.ai resolves
    through `_resolve_pay_model` now, and this function is reached only after
    that has already failed to price the call.
    """
    if not provider_alias:
        return "default"
    return provider_alias if provider_alias in PROVIDERS else "default"


def get_billing_model(provider_alias: str, model: Optional[str] = None) -> str:
    """Return 'pay_per_use' or 'subscription' for a provider alias/model."""
    if _resolve_pay_model(provider_alias, model) is not None:
        return "pay_per_use"
    key = _resolve_provider_key(provider_alias)
    return str(PROVIDERS[key]["billing"])


def compute_cost_usd(
    provider_alias: str,
    prompt_tokens: int,
    completion_tokens: int,
    *,
    model: Optional[str] = None,
    cache_hit_tokens: int = 0,
    cache_miss_tokens: Optional[int] = None,
    at: Optional[datetime] = None,
) -> float:
    """Compute USD cost for a single LLM call.

    Pay-per-use (DeepSeek, z.ai): bills cache-hit + cache-miss input + output by the
    per-1M rates. When the cache split is not supplied, treats all prompt tokens
    as cache-miss (conservative — never undershoots). Subscription/unknown
    providers return 0.0. Never raises.

    `at` is the moment the call was made, defaulting to now. It exists because
    from 2026-08-16 16:00 UTC DeepSeek prices by the clock — off-peak, doubled
    between 01:00-04:00 and 06:00-10:00 UTC — so a cost is no longer a function
    of tokens alone.
    """
    pay_model = _resolve_pay_model(provider_alias, model)
    if pay_model is not None:
        rates = rates_at(pay_model, at)
        hit = max(0, cache_hit_tokens or 0)
        if cache_miss_tokens is None:
            miss = max(0, (prompt_tokens or 0) - hit)
        else:
            miss = max(0, cache_miss_tokens)
        out = max(0, completion_tokens or 0)
        return (
            hit * rates["cache_hit"]
            + miss * rates["cache_miss"]
            + out * rates["output"]
        ) / 1_000_000.0

    entry = PROVIDERS[_resolve_provider_key(provider_alias)]
    if entry["billing"] == "subscription":
        return 0.0
    input_rate = float(entry["input_per_1k"])
    output_rate = float(entry["output_per_1k"])
    return (prompt_tokens / 1000.0) * input_rate + (completion_tokens / 1000.0) * output_rate
