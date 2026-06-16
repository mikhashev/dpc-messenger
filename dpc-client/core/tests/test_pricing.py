"""Tests for dpc_agent.pricing — per-provider USD cost, DeepSeek cache split."""

import pytest

from dpc_client_core.dpc_agent.pricing import (
    compute_cost_usd,
    get_billing_model,
)


# --- subscription / unknown providers return 0.0 ---

@pytest.mark.parametrize("alias", ["zai_coding_glm", "GLM-5.1", "glm-4.7", "", "mystery"])
def test_subscription_and_unknown_cost_zero(alias):
    assert compute_cost_usd(alias, 10_000, 5_000) == 0.0


def test_billing_model_subscription():
    assert get_billing_model("zai_coding_glm") == "subscription"
    assert get_billing_model("unknown_alias") == "subscription"


# --- DeepSeek pay-per-use, resolved by model name ---

def test_billing_model_deepseek_pay_per_use():
    assert get_billing_model("deepseek_flash", "deepseek-v4-flash") == "pay_per_use"
    assert get_billing_model("deepseek_pro", "deepseek-v4-pro") == "pay_per_use"


def test_flash_cost_with_cache_split():
    # hit 200 * 0.0028 + miss 800 * 0.14 + out 500 * 0.28, per 1M
    cost = compute_cost_usd(
        "deepseek_flash", 1000, 500,
        model="deepseek-v4-flash", cache_hit_tokens=200, cache_miss_tokens=800,
    )
    expected = (200 * 0.0028 + 800 * 0.14 + 500 * 0.28) / 1_000_000
    assert cost == pytest.approx(expected)


def test_pro_cost_with_cache_split():
    cost = compute_cost_usd(
        "deepseek_pro", 1000, 1000,
        model="deepseek-v4-pro", cache_hit_tokens=0, cache_miss_tokens=1000,
    )
    expected = (1000 * 0.435 + 1000 * 0.87) / 1_000_000
    assert cost == pytest.approx(expected)


def test_flash_and_pro_differ():
    common = dict(prompt_tokens=1000, completion_tokens=1000, cache_miss_tokens=1000)
    flash = compute_cost_usd("a", model="deepseek-v4-flash", **common)
    pro = compute_cost_usd("a", model="deepseek-v4-pro", **common)
    assert pro > flash


# --- conservative fallback: no cache split → all prompt billed as cache-miss ---

def test_conservative_treats_all_prompt_as_miss():
    cost = compute_cost_usd("deepseek_flash", 1000, 500, model="deepseek-v4-flash")
    expected = (1000 * 0.14 + 500 * 0.28) / 1_000_000  # hit=0, miss=1000
    assert cost == pytest.approx(expected)


def test_cache_hit_is_cheaper_than_miss():
    all_miss = compute_cost_usd(
        "x", 1000, 0, model="deepseek-v4-flash", cache_hit_tokens=0, cache_miss_tokens=1000
    )
    all_hit = compute_cost_usd(
        "x", 1000, 0, model="deepseek-v4-flash", cache_hit_tokens=1000, cache_miss_tokens=0
    )
    assert all_hit < all_miss


# --- resolution by alias substring when the model string is unavailable ---

def test_resolve_by_alias_when_model_missing():
    assert compute_cost_usd("deepseek_pro", 1000, 0, cache_miss_tokens=1000) == pytest.approx(
        1000 * 0.435 / 1_000_000
    )
    assert compute_cost_usd("deepseek_flash", 1000, 0, cache_miss_tokens=1000) == pytest.approx(
        1000 * 0.14 / 1_000_000
    )


# --- back-compat: legacy positional call (no model/cache kwargs) still works ---

def test_legacy_positional_signature_subscription():
    assert compute_cost_usd("zai_coding_glm", 1234, 567) == 0.0


def test_never_raises_on_negative_or_none_like():
    # defensive: zero/empty inputs do not raise and clamp to >= 0
    assert compute_cost_usd("deepseek_flash", 0, 0, model="deepseek-v4-flash") == 0.0
