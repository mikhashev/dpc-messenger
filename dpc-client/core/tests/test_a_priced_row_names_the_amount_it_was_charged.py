"""A row that carries a tariff also carries the amount that tariff came to.

ADR-041 D3, amendment of 2026-09-13: the amount column is `tariff_amount`
(Mike's call), in the row's own `tariff_currency`, and reasoning is billable
output at `tariff_out` — there is no separate thinking rate. Which arithmetic
applies is decided by the row's `output_includes_thinking`, not by
`counts_source`, because that label answers a different question
([[THE-LABEL-ON-A-USAGE-ROW-SAYS-WHO-COUNTED-AND-THE-ARITHMETIC-NEEDS-TO-KNOW-WHAT-WAS-COUNTED]]):

    includes -> tariff_out x completion_tokens
    excludes -> tariff_out x (completion_tokens + thinking_tokens)
    unknown  -> not billed; the amount is None and the row is analytics only

`tariff_amount_for` is the one place that arithmetic lives, and it is called
at write time only: D3 prices a call at `started_at` and never re-derives it,
which is the same rule `cost_usd` and `dpc_agent/pricing.py:177` already keep.
"""

from datetime import datetime, timezone

import pytest

from dpc_client_core.node_ledger import NodeLedger, tariff_amount_for, usage_row

NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)
# The rates of the live example: 20 in / 60 out per 1M tokens, in RUB.
TARIFF = dict(tariff_in=20.0, tariff_out=60.0, tariff_currency="RUB", tariff_at="2026-09-01")


def _row(**overrides):
    fields = dict(
        request_id="req-1", caller="dpc-node-alice-123", caller_kind="peer",
        alias="ollama_local", model="qwen3:8b", route="local",
        prompt_tokens=8, completion_tokens=1, thinking_tokens=56,
        counts_source="ours", started_at=NOW, duration_s=1.0,
        billing="subscription", cost_usd=0.0,
    )
    fields.update(overrides)
    return usage_row(**fields)


def _amount(**overrides):
    fields = dict(
        prompt_tokens=8, completion_tokens=1, thinking_tokens=56,
        output_includes_thinking="excludes", tariff_in=20.0, tariff_out=60.0,
    )
    fields.update(overrides)
    return tariff_amount_for(**fields)


# --- (1) the three states -----------------------------------------------------------


def test_an_inclusive_count_bills_the_completion_alone():
    """The host's number already has the reasoning in it; adding thinking would
    bill the same tokens twice."""
    assert _amount(output_includes_thinking="includes") == pytest.approx(
        (8 * 20.0 + 1 * 60.0) / 1_000_000.0
    )


def test_an_exclusive_count_bills_the_completion_and_the_thinking():
    """The live «понг» call: completion 1, thinking 56. Billing the visible
    token alone would charge for one of the fifty-seven the card generated."""
    assert _amount(output_includes_thinking="excludes") == pytest.approx(
        (8 * 20.0 + 57 * 60.0) / 1_000_000.0
    )


def test_an_unknown_count_is_not_billed_at_all():
    """Every row written before the column says `unknown`, and none of them is
    a receipt to anybody: analytics only, never an amount."""
    assert _amount(output_includes_thinking="unknown") is None


# --- (2) no declared tariff, and the free peer --------------------------------------


@pytest.mark.parametrize("rates", [
    dict(tariff_in=None, tariff_out=None),
    dict(tariff_in=20.0, tariff_out=None),
    dict(tariff_in=None, tariff_out=60.0),
])
def test_nothing_declared_is_no_amount_rather_than_a_zero(rates):
    """The v1 gift: an absent tariff has no amount. A zero here would say the
    owner priced the call at nothing, which is a different statement."""
    assert _amount(**rates) is None


def test_a_free_peer_is_charged_a_declared_zero_and_not_an_absence():
    """`tariff_for` gives a free peer the declared entry at zero, so the amount
    is 0.0 — a price, not «unpriced»."""
    assert _amount(tariff_in=0.0, tariff_out=0.0) == 0.0


def test_a_call_with_no_thinking_bills_the_same_under_both_conventions():
    inclusive = _amount(thinking_tokens=None, output_includes_thinking="includes")
    exclusive = _amount(thinking_tokens=0, output_includes_thinking="excludes")
    assert inclusive == exclusive == pytest.approx((8 * 20.0 + 1 * 60.0) / 1_000_000.0)


# --- (3) the column -----------------------------------------------------------------


def test_the_amount_is_written_after_the_tariff_group_it_belongs_to():
    row = _row(**TARIFF, tariff_amount=0.00346, output_includes_thinking="excludes")

    assert list(row) == [
        "request_id", "caller", "caller_kind", "alias", "model", "route",
        "prompt_tokens", "completion_tokens", "thinking_tokens", "counts_source",
        "output_includes_thinking", "served_effort", "peer_proved", "peer_connection_type",
        "started_at", "duration_s", "billing", "cost_usd",
        "tariff_in", "tariff_out", "tariff_currency", "tariff_at", "tariff_amount",
    ]
    assert row["tariff_amount"] == 0.00346


def test_a_declared_tariff_with_an_unknown_count_writes_the_column_as_null():
    """The column is there so a reader can tell «priced at nothing» from «a
    tariff applied and the arithmetic could not be done»."""
    row = _row(**TARIFF, tariff_amount=None)
    assert row["tariff_amount"] is None


def test_a_row_with_no_tariff_carries_no_amount_key():
    assert not [key for key in _row() if key.startswith("tariff_")]


def test_an_amount_without_a_tariff_is_refused_rather_than_written():
    """The amount's unit is the row's `tariff_currency`; with no group beside
    it the number names no currency and no rates."""
    with pytest.raises(ValueError, match="tariff_amount"):
        _row(tariff_amount=1.0)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), -0.5, True, "0.01"])
def test_an_amount_that_is_not_a_finite_non_negative_number_is_refused(bad):
    """The NaN a bad rate would have produced must not reach a partition either:
    a row is forever and nothing downstream re-derives it."""
    with pytest.raises(ValueError, match="tariff_amount"):
        _row(**TARIFF, tariff_amount=bad)


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_a_rate_that_is_not_finite_is_refused_on_the_row_as_it_is_in_the_rules(bad):
    """The guest builds its row from the wire, where any number can arrive."""
    with pytest.raises(ValueError, match="tariff_in"):
        _row(**dict(TARIFF, tariff_in=bad))


def test_a_row_written_before_the_column_reads_as_no_amount(tmp_path):
    ledger = NodeLedger(tmp_path / "ledger")
    old = {key: value for key, value in _row(**TARIFF, tariff_amount=0.5).items()
           if key != "tariff_amount"}
    ledger.append(old)

    (row,) = ledger.rows()
    assert row["tariff_amount"] is None
    assert row["tariff_in"] == 20.0


def test_the_amount_survives_the_round_trip_to_a_partition(tmp_path):
    ledger = NodeLedger(tmp_path / "ledger")
    ledger.append(_row(**TARIFF, tariff_amount=0.0))

    (row,) = ledger.rows()
    assert row["tariff_amount"] == 0.0
