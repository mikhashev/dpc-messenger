"""A served answer may say what it cost the host, and says nothing when it was not counted.

DPTP v1.7 adds `cost_usd` and, beside it, `billing` to REMOTE_INFERENCE_RESPONSE
(ADR-041 D3). The fields are in v1 of the field set on purpose: a cost added
later is the one kind of addition an older client cannot read. Each is optional
both ways — absent is
«not counted», never «free» — and it never rides on an error.
"""

from dpc_protocol.protocol import create_remote_inference_response


def test_the_hosts_price_travels_when_the_host_counted_it():
    message = create_remote_inference_response("req-1", response="ok", cost_usd=0.0041)

    assert message["payload"]["cost_usd"] == 0.0041


def test_an_uncounted_call_carries_no_cost_field():
    message = create_remote_inference_response("req-1", response="ok")

    assert "cost_usd" not in message["payload"]


def test_a_zero_is_a_count_and_travels():
    """A locally served model costs the host no dollars by construction; that
    is a number the requester can read, not an absence."""
    message = create_remote_inference_response("req-1", response="ok", cost_usd=0.0)

    assert message["payload"]["cost_usd"] == 0.0


def test_an_error_carries_no_cost():
    message = create_remote_inference_response("req-1", error="refused", cost_usd=0.5)

    assert "cost_usd" not in message["payload"]


def test_the_billing_model_travels_beside_the_price_and_never_on_an_error():
    priced = create_remote_inference_response("req-1", response="ok", cost_usd=0.0041, billing="pay_per_use")
    silent = create_remote_inference_response("req-1", response="ok")
    failed = create_remote_inference_response("req-1", error="refused", cost_usd=0.5, billing="pay_per_use")

    assert priced["payload"]["billing"] == "pay_per_use"
    assert "billing" not in silent["payload"]
    assert "billing" not in failed["payload"]
