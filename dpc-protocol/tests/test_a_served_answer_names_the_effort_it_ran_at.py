"""A served answer names the reasoning effort the host actually ran at.

The request carries `reasoning_effort`, the word the guest wants; the host
clamps it to what it will spend. DPTP v1.7 adds `served_effort` to
REMOTE_INFERENCE_RESPONSE: the word after the clamp, so a guest paying by the
token can check the depth it paid for against the depth it asked for. Optional
both ways — absent means the host applied no effort control and answered at its
own default — and never on an error.
"""

from dpc_protocol.protocol import create_remote_inference_response


def test_the_served_effort_travels_when_the_host_chose_one():
    message = create_remote_inference_response("req-1", response="ok", served_effort="low")

    assert message["payload"]["served_effort"] == "low"


def test_a_host_that_applied_no_effort_control_sends_no_field():
    message = create_remote_inference_response("req-1", response="ok")

    assert "served_effort" not in message["payload"]


def test_an_error_carries_no_served_effort():
    message = create_remote_inference_response("req-1", error="refused", served_effort="low")

    assert "served_effort" not in message["payload"]
