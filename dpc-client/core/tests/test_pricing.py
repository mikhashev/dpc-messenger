"""Tests for dpc_agent.pricing — per-provider USD cost, DeepSeek cache split.

The rate tests below name the moment they price at. Four of them used to leave
`at` unset, which means "now", and asserted the pre-2026-08-16 numbers: green
until the tariff changed and red on every machine afterwards, for no reason
connected to the code. A test that asserts a rate has to say which tariff it is
asserting.
"""

from datetime import datetime, timezone

import pytest

# Any moment before the switchover; the new-tariff tests use their own constants.
AT_OLD_TARIFF = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)

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
        at=AT_OLD_TARIFF,
    )
    expected = (200 * 0.0028 + 800 * 0.14 + 500 * 0.28) / 1_000_000
    assert cost == pytest.approx(expected)


def test_pro_cost_with_cache_split():
    cost = compute_cost_usd(
        "deepseek_pro", 1000, 1000,
        model="deepseek-v4-pro", cache_hit_tokens=0, cache_miss_tokens=1000,
        at=AT_OLD_TARIFF,
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
    cost = compute_cost_usd("deepseek_flash", 1000, 500, model="deepseek-v4-flash", at=AT_OLD_TARIFF)
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
    assert compute_cost_usd(
        "deepseek_pro", 1000, 0, cache_miss_tokens=1000, at=AT_OLD_TARIFF
    ) == pytest.approx(1000 * 0.435 / 1_000_000)
    assert compute_cost_usd(
        "deepseek_flash", 1000, 0, cache_miss_tokens=1000, at=AT_OLD_TARIFF
    ) == pytest.approx(1000 * 0.14 / 1_000_000)


# --- back-compat: legacy positional call (no model/cache kwargs) still works ---

def test_legacy_positional_signature_subscription():
    assert compute_cost_usd("zai_coding_glm", 1234, 567) == 0.0


def test_never_raises_on_negative_or_none_like():
    # defensive: zero/empty inputs do not raise and clamp to >= 0
    assert compute_cost_usd("deepseek_flash", 0, 0, model="deepseek-v4-flash") == 0.0


# --- the tariff has a clock now (DeepSeek, from 2026-08-16 16:00 UTC) ---

from dpc_client_core.dpc_agent.pricing import NEW_TARIFF_FROM, rates_at

BEFORE = datetime(2026, 8, 16, 15, 59, 59, tzinfo=timezone.utc)
AFTER_OFF_PEAK = datetime(2026, 8, 16, 16, 0, 0, tzinfo=timezone.utc)   # 16:00 is off-peak
AFTER_PEAK = datetime(2026, 8, 17, 2, 0, 0, tzinfo=timezone.utc)        # inside 01:00-04:00


def test_a_call_a_second_before_the_change_bills_at_the_old_rates():
    """The boundary, from the cheap side. A cost belongs to the moment of the
    call, so the old table cannot simply be deleted."""
    assert rates_at("deepseek-v4-flash", BEFORE)["output"] == 0.28


def test_the_change_takes_effect_at_the_stated_minute():
    assert rates_at("deepseek-v4-flash", AFTER_OFF_PEAK)["output"] == 0.66
    assert NEW_TARIFF_FROM == datetime(2026, 8, 16, 16, 0, tzinfo=timezone.utc)


def test_peak_hours_cost_exactly_double():
    off = rates_at("deepseek-v4-pro", AFTER_OFF_PEAK)
    peak = rates_at("deepseek-v4-pro", AFTER_PEAK)
    assert peak == {k: v * 2 for k, v in off.items()}
    assert off["output"] == 1.98 and peak["output"] == 3.96


def test_the_window_is_half_open():
    """04:00:00 is off-peak. The vendor does not say which side the boundary
    falls on; this pins our reading so it cannot drift silently."""
    at_end = datetime(2026, 8, 17, 4, 0, 0, tzinfo=timezone.utc)
    just_inside = datetime(2026, 8, 17, 3, 59, 59, tzinfo=timezone.utc)
    assert rates_at("deepseek-v4-flash", at_end)["output"] == 0.66
    assert rates_at("deepseek-v4-flash", just_inside)["output"] == 1.32


def test_between_the_two_peak_windows_is_off_peak():
    """05:00 sits in the gap the vendor leaves between 04:00 and 06:00."""
    gap = datetime(2026, 8, 17, 5, 0, 0, tzinfo=timezone.utc)
    assert rates_at("deepseek-v4-flash", gap)["output"] == 0.66


def test_cost_follows_the_clock_end_to_end():
    """The whole point: the same call, priced at three moments."""
    args = ("deepseek_flash", 1_000_000, 1_000_000)
    kw = dict(model="deepseek-v4-flash", cache_hit_tokens=0)
    old = compute_cost_usd(*args, **kw, at=BEFORE)
    new_off = compute_cost_usd(*args, **kw, at=AFTER_OFF_PEAK)
    new_peak = compute_cost_usd(*args, **kw, at=AFTER_PEAK)
    assert old == pytest.approx(0.14 + 0.28)
    assert new_off == pytest.approx(0.22 + 0.66)
    assert new_peak == pytest.approx(2 * (0.22 + 0.66))
    assert new_peak > new_off > old


def test_a_naive_moment_is_read_as_utc():
    """Nothing in this codebase stamps a local time, but a caller that does
    must not be billed by an accident of its timezone."""
    naive = datetime(2026, 8, 17, 2, 0, 0)
    assert rates_at("deepseek-v4-flash", naive) == rates_at("deepseek-v4-flash", AFTER_PEAK)


def test_subscription_providers_are_untouched_by_the_clock():
    assert compute_cost_usd("zai_coding_glm", 10_000, 5_000, at=AFTER_PEAK) == 0.0


# --- the two traps GLM 5.3 found by moving the clock, 2026-08-16 ---

def test_the_rate_tests_do_not_depend_on_what_day_it_is():
    """The suite must not go red because the tariff changed on schedule.

    Four tests above asserted the old numbers through the default `at=None`,
    which means "now". They were green only while "now" was before
    2026-08-16 16:00 UTC. This pins the property rather than the arithmetic:
    the same call priced at a stated moment must give the same answer whatever
    the wall clock says.
    """
    args = ("deepseek_flash", 1000, 500)
    kw = dict(model="deepseek-v4-flash", cache_hit_tokens=200, cache_miss_tokens=800)
    old = (200 * 0.0028 + 800 * 0.14 + 500 * 0.28) / 1_000_000
    new_off = (200 * 0.007 + 800 * 0.22 + 500 * 0.66) / 1_000_000
    assert compute_cost_usd(*args, **kw, at=AT_OLD_TARIFF) == pytest.approx(old)
    assert compute_cost_usd(*args, **kw, at=AFTER_OFF_PEAK) == pytest.approx(new_off)
    assert new_off > old, "the change this whole module exists for"


def test_a_model_known_only_to_the_new_table_is_still_pay_per_use():
    """Resolution used to test membership in the old table alone. Both tables
    carry the same keys today, so the failure was scheduled rather than
    present: a model added to the new table only would resolve to nothing,
    fall through to subscription, and bill $0.00 without saying so."""
    from dpc_client_core.dpc_agent import pricing

    pricing.PAY_PER_USE_RATES_FROM_2026_08_16["deepseek-v5-future"] = {
        "cache_hit": 0.01, "cache_miss": 0.5, "output": 1.5,
    }
    try:
        assert get_billing_model("whatever", model="deepseek-v5-future") == "pay_per_use"
        cost = compute_cost_usd(
            "whatever", 1000, 1000, model="deepseek-v5-future",
            cache_miss_tokens=1000, at=AFTER_OFF_PEAK,
        )
        assert cost == pytest.approx((1000 * 0.5 + 1000 * 1.5) / 1_000_000)
        assert cost > 0, "a model we know the price of must never bill as free"
    finally:
        pricing.PAY_PER_USE_RATES_FROM_2026_08_16.pop("deepseek-v5-future", None)


def test_a_new_table_model_priced_before_the_new_table_existed_does_not_raise():
    """The contract says "never raises", and it was formally false for one case
    the union fix created: a model only the new tariff names, asked for at a
    moment before that tariff. There is no old rate for it; its own table is
    the only figure that exists, and a cost meter that throws is one nobody
    calls. (Ark and Warren, reading the diff, 2026-08-15.)"""
    from dpc_client_core.dpc_agent import pricing

    pricing.PAY_PER_USE_RATES_FROM_2026_08_16["deepseek-v5-future"] = {
        "cache_hit": 0.01, "cache_miss": 0.5, "output": 1.5,
    }
    try:
        cost = compute_cost_usd(
            "whatever", 1000, 1000, model="deepseek-v5-future",
            cache_miss_tokens=1000, at=AT_OLD_TARIFF,
        )
        assert cost == pytest.approx((1000 * 0.5 + 1000 * 1.5) / 1_000_000)
    finally:
        pricing.PAY_PER_USE_RATES_FROM_2026_08_16.pop("deepseek-v5-future", None)


# --- the tariff has a calendar too (weekends off-peak, from 2026-08-22 16:00 UTC) ---

from dpc_client_core.dpc_agent.pricing import WEEKEND_OFF_PEAK_FROM, _is_weekend_in_beijing

# Beijing is UTC+8, so its weekend is Friday 16:00 UTC to Sunday 16:00 UTC and
# belongs to three different UTC days. Every moment below is after the effective
# constant unless it says otherwise.
WEEKDAY_PEAK = datetime(2026, 8, 24, 2, 0, tzinfo=timezone.utc)      # Beijing Mon 10:00
WEEKDAY_OFF_PEAK = datetime(2026, 8, 24, 5, 0, tzinfo=timezone.utc)  # Beijing Mon 13:00
WEEKEND_IN_OLD_PEAK = datetime(2026, 8, 29, 2, 0, tzinfo=timezone.utc)  # Beijing Sat 10:00


def test_a_weekday_peak_hour_still_costs_double():
    off = rates_at("deepseek-v4-pro", WEEKDAY_OFF_PEAK)
    peak = rates_at("deepseek-v4-pro", WEEKDAY_PEAK)
    assert peak == {k: v * 2 for k, v in off.items()}


def test_a_weekend_hour_inside_the_old_peak_window_is_charged_once():
    """The case that is green on the clock alone and wrong from 23 August: the
    vendor stops applying peak windows on Beijing weekends, so our own records
    would book double where the invoice says single."""
    assert _is_weekend_in_beijing(WEEKEND_IN_OLD_PEAK)
    assert rates_at("deepseek-v4-pro", WEEKEND_IN_OLD_PEAK)["output"] == 1.98
    assert rates_at("deepseek-v4-pro", WEEKEND_IN_OLD_PEAK) == rates_at(
        "deepseek-v4-pro", WEEKDAY_OFF_PEAK
    )


def test_the_weekend_is_the_vendors_and_not_ours():
    """Friday evening UTC is already Saturday in Beijing, and Sunday evening UTC
    is already Monday. Reading the UTC weekday would get both ends wrong."""
    friday_utc = datetime(2026, 8, 28, 17, 0, tzinfo=timezone.utc)   # Beijing Sat 01:00
    sunday_utc = datetime(2026, 8, 30, 17, 0, tzinfo=timezone.utc)   # Beijing Mon 01:00
    assert friday_utc.weekday() == 4 and _is_weekend_in_beijing(friday_utc)
    assert sunday_utc.weekday() == 6 and not _is_weekend_in_beijing(sunday_utc)


def test_a_weekend_call_made_before_the_change_still_bills_at_the_old_rule():
    """A cost belongs to the moment of the call — the same reason the old rate
    table is still in the file. Beijing Saturday, inside a peak window, but
    before the vendor changed the rule: double, as it was charged then."""
    earlier = datetime(2026, 8, 22, 2, 0, tzinfo=timezone.utc)  # Beijing Sat 10:00
    assert earlier < WEEKEND_OFF_PEAK_FROM and _is_weekend_in_beijing(earlier)
    assert rates_at("deepseek-v4-pro", earlier)["output"] == 3.96


def test_the_weekend_rule_starts_at_the_stated_minute():
    assert WEEKEND_OFF_PEAK_FROM == datetime(2026, 8, 22, 16, 0, tzinfo=timezone.utc)
