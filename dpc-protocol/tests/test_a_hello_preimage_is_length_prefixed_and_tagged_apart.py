"""A HELLO preimage is length-prefixed and tagged apart from every other.

The direct-TLS HELLO signature is made with node.key through the same
`CommitSigner.sign_commit` as message, vote and gossip signatures, so the only
thing keeping a HELLO signature from standing for one of those is the preimage:
its own version tag first, then length-prefixed fields. These tests pin both.
"""

import base64
import hashlib

import pytest

from dpc_protocol.message_signing import (
    HELLO_PREIMAGE_VERSION,
    gossip_content_hash,
    hello_content_hash,
    hello_preimage,
    message_content_hash,
    vote_content_hash,
)

NONCE = bytes(range(32))
LISTENER = "dpc-node-86cdcd262c7f81bb58f48adbccdc86e3"
DIALER = "dpc-node-0123456789abcdef0123456789abcdef"


def test_the_preimage_is_the_tag_then_three_length_prefixed_fields():
    b64 = base64.b64encode(NONCE).decode()
    expected = (
        f"{len(HELLO_PREIMAGE_VERSION)}:{HELLO_PREIMAGE_VERSION}"
        f"{len(b64)}:{b64}{len(LISTENER)}:{LISTENER}{len(DIALER)}:{DIALER}"
    ).encode()
    got = hello_preimage(nonce=NONCE, listener_node_id=LISTENER, dialer_node_id=DIALER)
    assert got == expected
    assert got.startswith(b"13:dptp-hello-v1")
    assert hello_content_hash(
        nonce=NONCE, listener_node_id=LISTENER, dialer_node_id=DIALER
    ) == hashlib.sha256(expected).hexdigest()


def test_swapping_the_two_ends_or_moving_a_boundary_changes_the_hash():
    honest = hello_content_hash(nonce=NONCE, listener_node_id=LISTENER, dialer_node_id=DIALER)
    assert honest != hello_content_hash(nonce=NONCE, listener_node_id=DIALER, dialer_node_id=LISTENER)
    # One character moved from the listener id to the dialler id: same concatenation.
    assert honest != hello_content_hash(
        nonce=NONCE, listener_node_id=LISTENER[:-1], dialer_node_id=LISTENER[-1] + DIALER
    )
    assert honest != hello_content_hash(
        nonce=bytes(32), listener_node_id=LISTENER, dialer_node_id=DIALER
    )


@pytest.mark.parametrize("other", ["message", "vote", "gossip"])
def test_a_hello_hash_never_equals_another_preimages_hash_over_the_same_strings(other):
    b64 = base64.b64encode(NONCE).decode()
    hello = hello_content_hash(nonce=NONCE, listener_node_id=LISTENER, dialer_node_id=DIALER)
    if other == "message":
        others = [
            message_content_hash(conversation_id=b64, message_id=LISTENER, sender_node_id=DIALER),
            message_content_hash(
                conversation_id=HELLO_PREIMAGE_VERSION, message_id=b64,
                sender_node_id=LISTENER, sender_name=DIALER,
            ),
        ]
    elif other == "vote":
        others = [
            vote_content_hash(
                proposal_id=b64, conversation_id=LISTENER, voter_node_id=DIALER, vote="yes"
            ),
        ]
    else:
        others = [
            gossip_content_hash(
                message_id=b64, source=LISTENER, destination=DIALER, payload=None,
                max_hops=None, created_at=None, ttl=None, priority=None, vector_clock=None,
            ),
        ]
    for value in others:
        assert value != hello
