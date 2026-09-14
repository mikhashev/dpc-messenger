"""The error form of REMOTE_INFERENCE_RESPONSE may name why (DPTP §3.4, v1.7).

`code` is one machine-readable word beside the prose `error`, which is
unchanged: the code is what a program reads and the text is what a person
reads, and a host sends both. It rides on the error form only — a served call
is not a refusal and wears no reason — and an absent one means the host has no
word for this refusal, or predates the field.
"""

import pytest

from dpc_protocol.protocol import (
    REFUSAL_CODES,
    PeerRefused,
    create_remote_inference_response,
)


def _error(**kwargs):
    return create_remote_inference_response("req-1", error="refused", **kwargs)["payload"]


@pytest.mark.parametrize("code", sorted(REFUSAL_CODES))
def test_every_word_of_the_vocabulary_travels_beside_the_prose(code):
    payload = _error(code=code)

    assert payload["code"] == code
    assert payload["error"] == "refused", "the code adds to the text, it does not replace it"
    assert payload["status"] == "error"


def test_a_host_with_no_word_for_this_refusal_sends_no_field_at_all():
    """Absent, not empty: a reader must be able to tell «no reason given» from
    a reason, and an empty string would be a third state nothing means."""
    assert "code" not in _error()
    assert "code" not in _error(code=None)
    assert "code" not in _error(code="")


def test_a_served_answer_never_wears_a_code():
    payload = create_remote_inference_response("req-1", response="ok", code="not_allowed")["payload"]

    assert payload["status"] == "success" and "code" not in payload


def test_a_word_outside_the_vocabulary_is_carried_rather_than_refused():
    """The library carries; it does not police. A host newer than this one may
    name a cause this vocabulary has no word for, and the receiving side is
    where an unrecognised word is read like an absent one."""
    assert _error(code="quota_exhausted")["code"] == "quota_exhausted"


def test_the_refusal_a_guest_raises_is_a_runtime_error_carrying_the_word():
    """`PeerRefused` is what the guest's handler settles a pending future with.
    It subclasses `RuntimeError` because that is what a refused remote inference
    has raised since before the code existed, and every caller in the tree is
    written against it."""
    refusal = PeerRefused("the host's own words", code="not_allowed")

    assert isinstance(refusal, RuntimeError)
    assert str(refusal) == "the host's own words"
    assert refusal.code == "not_allowed"


def test_a_refusal_with_no_word_says_so_with_an_empty_one():
    assert PeerRefused("refused").code == ""
    assert PeerRefused("refused", code=None).code == ""
