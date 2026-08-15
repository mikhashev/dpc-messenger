"""
DPC Agent Pricing — convert token usage to USD cost per provider.

Subscription providers (z.ai Coding Plan, etc.) return 0.0 — they have no
per-token cost, only rate/concurrency limits handled in budget.py.

Pay-per-use providers (DeepSeek) get real dollar cost from per-1M-token rates,
applying DeepSeek's cache-hit / cache-miss input split when the provider reports
it (prompt_cache_hit_tokens / prompt_cache_miss_tokens). reasoning_content tokens
are already counted inside completion_tokens, so the output rate covers them.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Dict, Optional, Tuple


# Subscription providers: no per-token cost. Unknown providers fall back to
# "default" (subscription, $0.0) — fail-safe so an un-mapped provider never
# silently accumulates a fake cost. Add an explicit entry only for legacy
# per-1k pay-per-use providers; pay-per-use rates by model live in
# PAY_PER_USE_RATES below.
PROVIDERS: Dict[str, Dict[str, float | str]] = {
    "zai": {"billing": "subscription", "input_per_1k": 0.0, "output_per_1k": 0.0},
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

# Peak hours as the vendor writes them: "01:00 - 04:00 and 06:00 - 10:00 UTC".
# Read as half-open [start, end) — 04:00:00 itself is off-peak. The vendor does
# not say which side the boundary falls on; the reading is recorded here rather
# than left to whoever next reads the arithmetic, and it is worth at most a few
# seconds of billing either way.
PEAK_WINDOWS_UTC: Tuple[Tuple[time, time], ...] = (
    (time(1, 0), time(4, 0)),
    (time(6, 0), time(10, 0)),
)


def _is_peak(moment: datetime) -> bool:
    """Whether a UTC moment falls inside DeepSeek's doubled hours."""
    t = moment.astimezone(timezone.utc).time()
    return any(start <= t < end for start, end in PEAK_WINDOWS_UTC)


def rates_at(model_key: str, moment: Optional[datetime] = None) -> Dict[str, float]:
    """The three per-1M rates in force for `model_key` at `moment` (default now).

    Before the switchover the old flat table applies and the hour is irrelevant;
    after it, the off-peak table, doubled inside the peak windows.
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
    if not _is_peak(when):
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
    if model and (model in PAY_PER_USE_RATES or model in PAY_PER_USE_RATES_FROM_2026_08_16):
        return model
    hay = f"{model or ''} {provider_alias or ''}".lower()
    if "deepseek" in hay:
        if "pro" in hay:
            return "deepseek-v4-pro"
        if "flash" in hay:
            return "deepseek-v4-flash"
    return None


def _resolve_provider_key(provider_alias: str) -> str:
    """Map a provider alias (e.g. 'GLM-5.1', 'zai_glm47') to a PROVIDERS key.

    Anything starting with the z.ai naming convention maps to 'zai'. Unknown
    aliases fall back to 'default' (subscription, $0.0).
    """
    if not provider_alias:
        return "default"
    lowered = provider_alias.lower()
    if lowered.startswith("zai") or lowered.startswith("glm"):
        return "zai"
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

    Pay-per-use (DeepSeek): bills cache-hit + cache-miss input + output by the
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
