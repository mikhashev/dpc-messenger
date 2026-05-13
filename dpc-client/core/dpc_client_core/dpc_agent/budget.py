"""
DPC Agent Budget - Subscription-aware rate limiting.

For subscription plans (like ZAI GLM-4.7), track rate limits instead of dollars.
For pay-per-use plans, track token costs.

Supports:
- Concurrent request limits
- Rate limits (requests per minute/day)
- Dollar-based budget tracking (for pay-per-use)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from .utils import utc_now_iso, get_agent_root

log = logging.getLogger(__name__)


class BillingModel(Enum):
    """Billing model type."""
    PAY_PER_USE = "pay_per_use"      # OpenRouter style - track dollars
    SUBSCRIPTION = "subscription"     # ZAI style - track rate limits


@dataclass
class ProviderLimits:
    """Rate limits for a subscription provider."""
    provider: str
    max_concurrent: int = 2
    requests_per_minute: Optional[int] = None
    requests_per_day: Optional[int] = None

    # Tracking (timestamps of requests)
    minute_requests: List[float] = field(default_factory=list)
    day_requests: List[float] = field(default_factory=list)

    # Concurrent tracking
    current_concurrent: int = 0


# Z.AI concurrency limits from https://docs.z.ai (2026-05-04)
# Z.AI only enforces concurrency, not RPM/RPD — those are conservative defaults.
# Update when provider changes limits on their site.
PROVIDER_LIMITS: Dict[str, ProviderLimits] = {
    "zai_glm51": ProviderLimits(provider="zai_glm51", max_concurrent=10),
    "zai_glm5": ProviderLimits(provider="zai_glm5", max_concurrent=2),
    "zai_glm5_turbo": ProviderLimits(provider="zai_glm5_turbo", max_concurrent=1),
    "zai_glm47": ProviderLimits(provider="zai_glm47", max_concurrent=2),
    "zai_glm47_flash": ProviderLimits(provider="zai_glm47_flash", max_concurrent=1),
    "zai_glm47_flashx": ProviderLimits(provider="zai_glm47_flashx", max_concurrent=3),
    "zai_glm46": ProviderLimits(provider="zai_glm46", max_concurrent=3),
    "zai_glm45": ProviderLimits(provider="zai_glm45", max_concurrent=10),
    "zai_glm45_flash": ProviderLimits(provider="zai_glm45_flash", max_concurrent=2),
    "zai_glm45_air": ProviderLimits(provider="zai_glm45_air", max_concurrent=5),
    "zai_glm45_airx": ProviderLimits(provider="zai_glm45_airx", max_concurrent=5),
    "zai_glm4_plus": ProviderLimits(provider="zai_glm4_plus", max_concurrent=20),
    "default": ProviderLimits(provider="default", max_concurrent=2),
}


class SubscriptionBudget:
    """
    Budget tracker for subscription-based LLM providers.

    Unlike pay-per-use, subscriptions have:
    - Concurrent request limits
    - Rate limits (requests per minute/day)
    - No dollar tracking

    Thread-safe via asyncio.Lock.
    """

    def __init__(
        self,
        provider: str,
        billing_model: BillingModel = BillingModel.SUBSCRIPTION,
    ):
        """
        Initialize subscription budget tracker.

        Args:
            provider: Provider identifier (e.g., 'zai_glm47')
            billing_model: Billing model type
        """
        self.provider = provider
        self.billing_model = billing_model
        self.limits = get_provider_limits(provider)

        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(self.limits.max_concurrent)

        log.info(f"SubscriptionBudget initialized for {provider} "
                f"(concurrent={self.limits.max_concurrent}, "
                f"rpm={self.limits.requests_per_minute})")

    async def acquire(self) -> bool:
        """
        Acquire a request slot.

        Returns True if allowed, False if rate limited.
        Does NOT wait for slot - use wait_for_slot() for that.
        """
        async with self._lock:
            now = time.time()

            # RPM/RPD checks — only if provider declares these limits
            if self.limits.requests_per_minute is not None:
                minute_ago = now - 60
                self.limits.minute_requests = [
                    t for t in self.limits.minute_requests if t > minute_ago
                ]
                if len(self.limits.minute_requests) >= self.limits.requests_per_minute:
                    log.warning(f"Rate limit reached: {self.limits.requests_per_minute}/min for {self.provider}")
                    return False

            if self.limits.requests_per_day is not None:
                day_ago = now - 86400
                self.limits.day_requests = [
                    t for t in self.limits.day_requests if t > day_ago
                ]
                if len(self.limits.day_requests) >= self.limits.requests_per_day:
                    log.warning(f"Daily limit reached: {self.limits.requests_per_day}/day for {self.provider}")
                    return False

            # Concurrent limit
            if self._semaphore.locked() and self.limits.current_concurrent >= self.limits.max_concurrent:
                log.warning(f"Concurrent limit reached: {self.limits.max_concurrent} for {self.provider}")
                return False

            # Record request
            if self.limits.requests_per_minute is not None:
                self.limits.minute_requests.append(now)
            if self.limits.requests_per_day is not None:
                self.limits.day_requests.append(now)
            self.limits.current_concurrent += 1

            return True

    async def release(self) -> None:
        """Release a concurrent slot after request completes."""
        async with self._lock:
            if self.limits.current_concurrent > 0:
                self.limits.current_concurrent -= 1
            try:
                self._semaphore.release()
            except ValueError:
                pass  # Semaphore already at max

    async def wait_for_slot(self, timeout: float = 30.0) -> bool:
        """
        Wait for an available request slot.

        Args:
            timeout: Maximum seconds to wait

        Returns:
            True if slot acquired, False if timeout
        """
        start = time.time()

        while time.time() - start < timeout:
            if await self.acquire():
                return True
            await asyncio.sleep(1)

        log.warning(f"wait_for_slot timed out after {timeout}s for {self.provider}")
        return False

    def get_status(self) -> Dict[str, Any]:
        """Get current rate limit status."""
        now = time.time()
        minute_ago = now - 60

        return {
            "provider": self.provider,
            "billing_model": self.billing_model.value,
            "concurrent": {
                "current": self.limits.current_concurrent,
                "max": self.limits.max_concurrent,
                "available": self.limits.max_concurrent - self.limits.current_concurrent,
            },
            "rate_limits": {
                "minute": {
                    "used": len([t for t in self.limits.minute_requests if t > minute_ago]),
                    "max": self.limits.requests_per_minute,
                    "remaining": self.limits.requests_per_minute - len([t for t in self.limits.minute_requests if t > minute_ago]),
                },
                "day": {
                    "used": len(self.limits.day_requests),
                    "max": self.limits.requests_per_day,
                    "remaining": self.limits.requests_per_day - len(self.limits.day_requests),
                },
            },
        }


class PayPerUseBudget:
    """
    Budget tracker for pay-per-use LLM providers.

    Tracks dollar spending against a budget limit.
    """

    def __init__(
        self,
        budget_usd: float = 50.0,
        provider: str = "default",
    ):
        """
        Initialize pay-per-use budget tracker.

        Args:
            budget_usd: Budget limit in USD
            provider: Provider name for logging
        """
        self.provider = provider
        self.budget_usd = budget_usd
        self.spent_usd = 0.0
        self._lock = asyncio.Lock()

        log.info(f"PayPerUseBudget initialized for {provider} (budget=${budget_usd})")

    async def can_spend(self, estimated_cost: float = 0.0) -> bool:
        """
        Check if we can spend (optionally estimated) amount.

        Args:
            estimated_cost: Estimated cost of next request

        Returns:
            True if within budget, False if would exceed
        """
        async with self._lock:
            return (self.spent_usd + estimated_cost) <= self.budget_usd

    async def record_cost(self, cost_usd: float) -> None:
        """
        Record a cost against the budget.

        Args:
            cost_usd: Actual cost in USD
        """
        async with self._lock:
            self.spent_usd += cost_usd

            percentage = (self.spent_usd / self.budget_usd) * 100

            if percentage >= 80:
                log.warning(f"Budget at {percentage:.1f}% for {self.provider}")
            if percentage >= 100:
                log.error(f"Budget EXCEEDED for {self.provider}: ${self.spent_usd:.2f} / ${self.budget_usd:.2f}")

    def get_status(self) -> Dict[str, Any]:
        """Get current budget status."""
        return {
            "provider": self.provider,
            "billing_model": "pay_per_use",
            "budget_usd": self.budget_usd,
            "spent_usd": self.spent_usd,
            "remaining_usd": max(0, self.budget_usd - self.spent_usd),
            "percentage_used": (self.spent_usd / self.budget_usd) * 100 if self.budget_usd > 0 else 0,
        }


class HybridBudget:
    """
    Hybrid budget tracker that supports both subscription and pay-per-use.

    Automatically selects the appropriate tracking based on billing model.
    """

    def __init__(
        self,
        provider: str,
        billing_model: str = "subscription",
        budget_usd: float = 50.0,
    ):
        """
        Initialize hybrid budget tracker.

        Args:
            provider: Provider identifier
            billing_model: 'subscription' or 'pay_per_use'
            budget_usd: Budget limit for pay-per-use mode
        """
        self.provider = provider
        self.billing_model = BillingModel(billing_model) if billing_model in ["subscription", "pay_per_use"] else BillingModel.SUBSCRIPTION

        if self.billing_model == BillingModel.SUBSCRIPTION:
            self._tracker = SubscriptionBudget(provider, self.billing_model)
        else:
            self._tracker = PayPerUseBudget(budget_usd, provider)

        log.info(f"HybridBudget initialized for {provider} (model={self.billing_model.value})")

    async def acquire(self) -> bool:
        """Acquire a request slot (subscription) or check budget (pay-per-use)."""
        if isinstance(self._tracker, SubscriptionBudget):
            return await self._tracker.acquire()
        else:
            return await self._tracker.can_spend()

    async def release(self) -> None:
        """Release a request slot (subscription only)."""
        if isinstance(self._tracker, SubscriptionBudget):
            await self._tracker.release()

    async def record_cost(self, cost_usd: float) -> None:
        """Record a cost (pay-per-use only)."""
        if isinstance(self._tracker, PayPerUseBudget):
            await self._tracker.record_cost(cost_usd)

    async def wait_for_slot(self, timeout: float = 30.0) -> bool:
        """Wait for available slot."""
        if isinstance(self._tracker, SubscriptionBudget):
            return await self._tracker.wait_for_slot(timeout)
        else:
            return await self._tracker.can_spend()

    def get_status(self) -> Dict[str, Any]:
        """Get current status."""
        return self._tracker.get_status()


# Convenience function for checking budget
def check_budget_simple(budget_used: float, budget_limit: float) -> bool:
    """
    Simple percentage-based budget check for subscription plans.

    Args:
        budget_used: Amount used (requests, tokens, etc.)
        budget_limit: Total limit

    Returns:
        True if within budget, False if exceeded
    """
    if budget_limit <= 0:
        return True  # Unlimited

    percentage = (budget_used / budget_limit) * 100

    # Warning at 80%
    if percentage >= 80:
        log.warning(f"Budget at {percentage:.1f}%")

    # Hard limit at 100%
    if percentage >= 100:
        return False

    return True


def _normalize_provider_key(provider: str) -> str:
    """Normalize provider alias to match PROVIDER_LIMITS keys.

    Handles aliases like 'GLM-5.1' → 'zai_glm51', 'zai_glm47' → 'zai_glm47'.
    """
    import re
    key = provider.lower().replace(".", "").replace("-", "_")
    if not key.startswith("zai_"):
        key = f"zai_{key}"
    key = re.sub(r"_+", "_", key)
    return key


def get_provider_limits(provider: str) -> ProviderLimits:
    """
    Get limits for a known provider.

    Args:
        provider: Provider identifier or alias (e.g., 'zai_glm47', 'GLM-5.1')

    Returns:
        ProviderLimits instance
    """
    result = PROVIDER_LIMITS.get(provider)
    if result:
        return result
    normalized = _normalize_provider_key(provider)
    return PROVIDER_LIMITS.get(normalized, PROVIDER_LIMITS["default"])
