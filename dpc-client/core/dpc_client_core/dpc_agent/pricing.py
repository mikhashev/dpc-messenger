"""
DPC Agent Pricing — convert token usage to USD cost per provider.

Subscription providers (z.ai annual plan, etc.) return 0.0 — they have
no per-token cost, only rate/concurrency limits handled in budget.py.

Pay-per-use providers (OpenAI, Anthropic direct) get real dollar cost
from per-1k-token rates. None today, but the shape is here for when
the first pay-per-use provider is wired in.
"""

from __future__ import annotations

from typing import Dict


# Per-provider billing + rates.
# Unknown providers fall back to "default" (subscription, $0.0) — see
# _resolve_provider_key. This is fail-safe: an un-mapped provider never
# silently accumulates a fake cost. Add an explicit entry when wiring in
# a pay-per-use provider that needs real per-token rates.
PROVIDERS: Dict[str, Dict[str, float | str]] = {
    "zai": {"billing": "subscription", "input_per_1k": 0.0, "output_per_1k": 0.0},
    "default": {"billing": "subscription", "input_per_1k": 0.0, "output_per_1k": 0.0},
}


def _resolve_provider_key(provider_alias: str) -> str:
    """Map a provider alias (e.g. 'GLM-5.1', 'zai_glm47') to a PROVIDERS key.

    Anything starting with the z.ai naming convention maps to 'zai'. Unknown
    aliases fall back to 'default' (subscription, $0.0) — same posture as
    budget.py.get_provider_limits.
    """
    if not provider_alias:
        return "default"
    lowered = provider_alias.lower()
    if lowered.startswith("zai") or lowered.startswith("glm"):
        return "zai"
    return provider_alias if provider_alias in PROVIDERS else "default"


def get_billing_model(provider_alias: str) -> str:
    """Return 'subscription' or 'pay_per_use' for a provider alias."""
    key = _resolve_provider_key(provider_alias)
    return str(PROVIDERS[key]["billing"])


def compute_cost_usd(
    provider_alias: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> float:
    """Compute USD cost for a single LLM call.

    Returns 0.0 for subscription providers. For pay-per-use, applies the
    per-1k rates from PROVIDERS. Never raises — unknown providers fall back
    to 'default' (subscription, 0.0).
    """
    key = _resolve_provider_key(provider_alias)
    entry = PROVIDERS[key]
    if entry["billing"] == "subscription":
        return 0.0
    input_rate = float(entry["input_per_1k"])
    output_rate = float(entry["output_per_1k"])
    return (prompt_tokens / 1000.0) * input_rate + (completion_tokens / 1000.0) * output_rate
