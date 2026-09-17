"""A served answer carries the owner's tariff, and never what the call cost the host.

This file replaces `test_a_served_answer_may_carry_what_it_cost_the_host.py`,
which pinned `cost_usd` on the wire. That was the decision of 2026-09-10
(`f7490460`) and it was reversed the same day by the ADR-041 D3 amendment: the
host's own cost is the host's economy and stays in the host's ledger, while the
wire carries the price the guest is asked for. Variant B, corrected while DPTP
v1.7 is unreleased.

What travels instead: `tariff_in`, `tariff_out`, `tariff_currency` and
`tariff_at` — the applied rates, their ISO 4217 unit and the dated entry they
came from — as one group or not at all, with `tariff_amount` beside them when
the call could be priced. `billing` stays: it says which meter the host reads,
not what the host paid. Nothing of this rides on an error.
"""

import pytest

from dpc_protocol.protocol import create_remote_inference_response

TARIFF = dict(tariff_in=20.0, tariff_out=60.0, tariff_currency="RUB", tariff_at="2026-09-01")


def _payload(**kwargs):
    return create_remote_inference_response("req-1", response="ok", **kwargs)["payload"]


# --- (1) the host's own cost does not travel ----------------------------------------


def test_the_response_builder_has_no_cost_usd_to_send():
    """Not «absent unless asked for» — there is no way to ask for it."""
    with pytest.raises(TypeError):
        create_remote_inference_response("req-1", response="ok", cost_usd=0.0041)


def test_a_priced_answer_carries_no_cost_field_of_any_kind():
    payload = _payload(**TARIFF, tariff_amount=0.00346, billing="subscription")
    assert "cost_usd" not in payload


# --- (2) the tariff travels as one group --------------------------------------------


def test_the_four_applied_values_travel_together():
    payload = _payload(**TARIFF, tariff_amount=0.00346)

    assert payload["tariff_in"] == 20.0 and payload["tariff_out"] == 60.0
    assert payload["tariff_currency"] == "RUB" and payload["tariff_at"] == "2026-09-01"
    assert payload["tariff_amount"] == 0.00346


def test_a_host_that_declared_nothing_sends_no_tariff_field():
    """The v1 gift: absent, never zero. A zero would say the owner priced the
    call at nothing, which is a different statement."""
    payload = _payload(billing="subscription")
    assert not [key for key in payload if key.startswith("tariff_")]


def test_a_free_peer_gets_a_declared_zero_which_is_a_price():
    payload = _payload(tariff_in=0.0, tariff_out=0.0, tariff_currency="RUB",
                       tariff_at="2026-09-01", tariff_amount=0.0)
    assert payload["tariff_in"] == 0.0 and payload["tariff_amount"] == 0.0


@pytest.mark.parametrize("partial", [
    dict(tariff_in=20.0, tariff_out=60.0, tariff_currency="RUB"),
    dict(tariff_in=20.0, tariff_currency="RUB", tariff_at="2026-09-01"),
    dict(tariff_currency="RUB"),
    dict(tariff_at="2026-09-01"),
])
def test_half_a_tariff_is_sent_as_none_of_it(partial):
    """Half a group is a price to one reader and a gift to another; the guest's
    row refuses it, so the wire does not offer it."""
    payload = _payload(**partial)
    assert not [key for key in payload if key.startswith("tariff_")]


def test_an_amount_without_its_rates_does_not_travel_alone():
    """The amount's unit is `tariff_currency`; without the group it names none."""
    payload = _payload(tariff_amount=0.00346)
    assert "tariff_amount" not in payload


def test_an_unpriced_call_sends_the_rates_and_no_amount():
    """`output_includes_thinking = unknown`: a tariff applied and the arithmetic
    could not be done. The guest sees the rates and no charge."""
    payload = _payload(**TARIFF, output_includes_thinking="unknown")

    assert payload["tariff_in"] == 20.0
    assert "tariff_amount" not in payload


# --- (3) never on an error ----------------------------------------------------------


def test_an_error_carries_no_tariff_and_no_amount():
    payload = create_remote_inference_response(
        "req-1", error="refused", **TARIFF, tariff_amount=0.5,
    )["payload"]

    assert payload["status"] == "error"
    assert not [key for key in payload if key.startswith("tariff_")]


# --- (4) billing stays: which meter, not what the host paid -------------------------


def test_the_billing_model_still_travels_and_never_on_an_error():
    assert _payload(billing="pay_per_use")["billing"] == "pay_per_use"
    assert "billing" not in _payload()
    assert "billing" not in create_remote_inference_response(
        "req-1", error="refused", billing="pay_per_use")["payload"]


def test_the_host_says_whether_its_output_count_includes_thinking_and_never_on_an_error():
    """Kept from the file this one replaces: the guest checks the arithmetic of
    the tariff against the convention of the counts it was applied to."""
    stated = _payload(response_tokens=1, thinking_tokens=56, output_includes_thinking="excludes")
    silent = _payload(response_tokens=1)
    failed = create_remote_inference_response(
        "req-1", error="refused", output_includes_thinking="excludes")["payload"]

    assert stated["output_includes_thinking"] == "excludes"
    assert "output_includes_thinking" not in silent
    assert "output_includes_thinking" not in failed
