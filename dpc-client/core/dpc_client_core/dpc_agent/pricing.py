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

from typing import Dict, Optional


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
# Source: https://api-docs.deepseek.com/quick_start/pricing (verified 2026-06-16).
PAY_PER_USE_RATES: Dict[str, Dict[str, float]] = {
    "deepseek-v4-flash": {"cache_hit": 0.0028, "cache_miss": 0.14, "output": 0.28},
    "deepseek-v4-pro": {"cache_hit": 0.003625, "cache_miss": 0.435, "output": 0.87},
}


def _resolve_pay_rates(
    provider_alias: str, model: Optional[str]
) -> Optional[Dict[str, float]]:
    """Return per-1M pay-per-use rates for (model, alias), or None if not pay-per-use.

    Resolves by model name first (authoritative — flash vs pro differ ~3x), then
    falls back to alias substring so a custom alias (e.g. "deepseek_pro") still
    matches when the model string is unavailable.
    """
    if model and model in PAY_PER_USE_RATES:
        return PAY_PER_USE_RATES[model]
    hay = f"{model or ''} {provider_alias or ''}".lower()
    if "deepseek" in hay:
        if "pro" in hay:
            return PAY_PER_USE_RATES["deepseek-v4-pro"]
        if "flash" in hay:
            return PAY_PER_USE_RATES["deepseek-v4-flash"]
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
    if _resolve_pay_rates(provider_alias, model) is not None:
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
) -> float:
    """Compute USD cost for a single LLM call.

    Pay-per-use (DeepSeek): bills cache-hit + cache-miss input + output by the
    per-1M rates. When the cache split is not supplied, treats all prompt tokens
    as cache-miss (conservative — never undershoots). Subscription/unknown
    providers return 0.0. Never raises.
    """
    rates = _resolve_pay_rates(provider_alias, model)
    if rates is not None:
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
