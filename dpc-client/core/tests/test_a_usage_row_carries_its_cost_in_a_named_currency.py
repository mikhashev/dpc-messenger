"""A usage row carries its cost in a named currency, and no reader adds two.

Until 2026-09-28 a row had one money column, `cost_usd`, and every reader
summed it as dollars. NeuralDeep bills in roubles, so its cost either reached
no row or would have reached one as dollars. The row now says `cost_amount`
in `cost_currency` on a `cost_basis` (`charged`, `list_price_reference`,
`unknown`), with `cost_unpriced_reason` where nobody could price the call
(Mike's call, 2026-09-28).

A row written before the change still reads: `NodeLedger.rows()` maps its
`cost_usd` to an amount in USD, charged, so the partitions already on disk
aggregate exactly as they did.
"""

import json
from datetime import datetime, timezone

import pytest

from dpc_client_core.node_ledger import NodeLedger, summarize, usage_by_role, usage_row

NOON = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def _row(**overrides):
    base = dict(
        request_id="r", caller="us", caller_kind="gateway", alias="nd", model="m",
        route="local", prompt_tokens=10, completion_tokens=5, thinking_tokens=None,
        counts_source="engine", started_at=NOON, duration_s=0.5, billing="pay_per_use",
    )
    base.update(overrides)
    return usage_row(**base)


# --- (iv) the writer refuses a half-stated amount ---------------------------


def test_a_non_zero_amount_without_a_currency_is_refused():
    with pytest.raises(ValueError, match="currency"):
        _row(cost_amount=1.5)


def test_a_currency_with_no_amount_is_refused():
    with pytest.raises(ValueError, match="currency"):
        _row(cost_amount=None, cost_currency="RUB")


def test_a_currency_outside_iso_4217_and_a_basis_outside_the_three_words_are_refused():
    with pytest.raises(ValueError, match="ISO 4217"):
        _row(cost_amount=1.5, cost_currency="rubles")
    with pytest.raises(ValueError, match="cost_basis"):
        _row(cost_amount=1.5, cost_currency="RUB", cost_basis="estimated")


def test_the_row_writes_the_four_columns_and_no_cost_usd():
    row = _row(cost_amount=1.5, cost_currency="RUB", cost_basis="charged")
    assert (row["cost_amount"], row["cost_currency"], row["cost_basis"]) == (1.5, "RUB", "charged")
    assert row["cost_unpriced_reason"] is None
    assert "cost_usd" not in row

    free = _row(cost_amount=0.0, billing="subscription")
    assert (free["cost_amount"], free["cost_currency"]) == (0.0, None)

    unpriced = _row(cost_amount=None, cost_unpriced_reason="no price list")
    assert (unpriced["cost_amount"], unpriced["cost_currency"]) == (None, None)
    assert unpriced["cost_unpriced_reason"] == "no price list"


# --- (v) old rows read as dollars, and currencies never add ----------------


def _legacy(ledger: NodeLedger, request_id: str, cost_usd, alias="deepseek_flash", **extra):
    """A row as the writer before 2026-09-28 left it on disk."""
    row = {
        "request_id": request_id, "caller": "us", "caller_kind": "gateway", "alias": alias,
        "model": "deepseek-v4-flash", "route": "local", "prompt_tokens": 10,
        "completion_tokens": 5, "thinking_tokens": None, "counts_source": "engine",
        "started_at": NOON.isoformat(), "duration_s": 0.5, "billing": "pay_per_use",
        "cost_usd": cost_usd,
    }
    row.update(extra)
    ledger.directory.mkdir(parents=True, exist_ok=True)
    with ledger.partition_for(row["started_at"]).open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def test_a_legacy_row_reads_back_as_charged_dollars(tmp_path):
    ledger = NodeLedger(tmp_path / "ledger")
    _legacy(ledger, "old-priced", 0.25)
    _legacy(ledger, "old-unpriced", None)

    priced, unpriced = ledger.rows()
    assert (priced["cost_amount"], priced["cost_currency"], priced["cost_basis"]) == (0.25, "USD", "charged")
    assert (unpriced["cost_amount"], unpriced["cost_currency"], unpriced["cost_basis"]) == (None, None, None)


def test_legacy_deepseek_rows_sum_the_same_before_and_after(tmp_path):
    """The control: `deepseek_flash` rows written with `cost_usd` fold to the
    dollars they always folded to, in the ceiling and in both summaries."""
    ledger = NodeLedger(tmp_path / "ledger")
    for i, cost in enumerate((0.01, 0.02, 0.04)):
        _legacy(ledger, f"old-{i}", cost)
    _legacy(ledger, "old-null", None)

    assert ledger.spent_today("deepseek_flash", caller="us", now=NOON, currency="USD") == pytest.approx(0.07)
    group = summarize(ledger.rows())["by_alias"]["deepseek_flash"]
    assert group["cost"] == {"USD": {"amount": pytest.approx(0.07), "rows": 3}}
    assert group["unpriced"] == 1
    own = usage_by_role(ledger.rows())["own"]["by_alias"]["deepseek_flash"]
    assert own["cost"] == {"USD": {"amount": pytest.approx(0.07), "rows": 3}}


def test_a_rouble_row_and_a_dollar_row_are_two_amounts_never_one(tmp_path):
    ledger = NodeLedger(tmp_path / "ledger")
    _legacy(ledger, "old-usd", 0.5, alias="mixed")
    ledger.append(_row(request_id="new-rub", caller="us", alias="mixed",
                       cost_amount=12.0, cost_currency="RUB", cost_basis="charged"))
    ledger.append(_row(request_id="new-free", caller="us", alias="mixed",
                       cost_amount=0.0, billing="subscription"))

    group = summarize(ledger.rows())["by_alias"]["mixed"]
    assert group["cost"] == {"USD": {"amount": 0.5, "rows": 1}, "RUB": {"amount": 12.0, "rows": 1}}
    assert group["cost_free"] == 1 and group["unpriced"] == 0
    assert ledger.spent_today("mixed", caller="us", now=NOON, currency="USD") == pytest.approx(0.5)
    assert ledger.spent_today("mixed", caller="us", now=NOON, currency="RUB") == pytest.approx(12.0)


# --- (vi) the ceiling counts what may have been charged --------------------


def test_the_ceiling_ignores_rows_priced_only_for_reference(tmp_path):
    """`charged` and `unknown` add, `list_price_reference` does not (fail-closed,
    Mike's call, 2026-09-28)."""
    ledger = NodeLedger(tmp_path / "ledger")
    ledger.append(_row(request_id="wallet", cost_amount=10.0, cost_currency="RUB", cost_basis="charged"))
    ledger.append(_row(request_id="plan", cost_amount=99.0, cost_currency="RUB",
                       cost_basis="list_price_reference", billing="subscription"))
    ledger.append(_row(request_id="unknown", cost_amount=7.0, cost_currency="RUB", cost_basis="unknown"))

    assert ledger.spent_today("nd", caller="us", now=NOON, currency="RUB") == pytest.approx(17.0)
