"""A file preimage is length-prefixed and tagged apart from every other (ADR-047 phase 1).

The author signs a file's description with node.key through the same
`CommitSigner.sign_commit` as messages, votes, gossip, commits and HELLOs, so
only the preimage keeps a file signature from standing for one of those: its
own tag `dptp-file-v1` first, then the nine fields length-prefixed in a fixed
order. These tests pin the bytes, show that every field moves the hash, and
that the same strings never hash to a message, vote, gossip or HELLO value.
"""

import hashlib

import pytest

from dpc_protocol.message_signing import (
    FILE_PREIMAGE_VERSION,
    file_content_hash,
    file_preimage,
    gossip_content_hash,
    hello_content_hash,
    message_content_hash,
    vote_content_hash,
)

GROUP = "group-0000000000aa"
AUTHOR = "dpc-node-86cdcd262c7f81bb58f48adbccdc86e3"
FIELDS = dict(
    conversation_id=GROUP,
    message_id="0123456789abcdef",
    sender_node_id=AUTHOR,
    size_bytes=1000,
    chunk_size=65536,
    hash="44f8354494a5ba03ba1792a8d3e9c534c47a9181980fde7a3f44b06ef2ae7c7f",
    chunks_hash="f" * 64,
    filename="notes.txt",
    mime_type="text/plain",
)


def _lp(*values):
    return "".join(f"{len(v.encode())}:{v}" for v in values).encode()


def test_the_preimage_is_the_tag_then_nine_length_prefixed_fields_in_order():
    expected = _lp(
        FILE_PREIMAGE_VERSION, GROUP, "0123456789abcdef", AUTHOR, "1000", "65536",
        FIELDS["hash"], FIELDS["chunks_hash"], "notes.txt", "text/plain",
    )
    assert file_preimage(**FIELDS) == expected
    assert file_preimage(**FIELDS).startswith(b"12:dptp-file-v1")
    assert file_content_hash(**FIELDS) == hashlib.sha256(expected).hexdigest()


def test_a_name_in_any_script_is_counted_in_utf8_bytes():
    name = "résumé 報告.pdf"
    got = file_preimage(**dict(FIELDS, filename=name))
    assert f"{len(name.encode('utf-8'))}:{name}".encode("utf-8") in got


@pytest.mark.parametrize("field,value", [
    ("conversation_id", "group-ffffffffffff"),
    ("message_id", "0123456789abcdee"),
    ("sender_node_id", "dpc-node-0123456789abcdef0123456789abcdef"),
    ("size_bytes", 1001),
    ("chunk_size", 65537),
    ("hash", "0" * 64),
    ("chunks_hash", "e" * 64),
    ("filename", "invoice.exe"),
    ("mime_type", "application/x-msdownload"),
])
def test_every_field_changes_the_hash(field, value):
    assert file_content_hash(**dict(FIELDS, **{field: value})) != file_content_hash(**FIELDS)


def test_moving_a_boundary_between_two_fields_changes_the_hash():
    """Same concatenation, other split: a separator scheme would sign both alike."""
    honest = file_content_hash(**FIELDS)
    assert honest != file_content_hash(**dict(FIELDS, filename="notes.tx", mime_type="ttext/plain"))
    assert honest != file_content_hash(**dict(FIELDS, size_bytes=10006, chunk_size=5536))
    assert honest != file_content_hash(**dict(FIELDS, message_id=FIELDS["message_id"] + AUTHOR[:4],
                                               sender_node_id=AUTHOR[4:]))


@pytest.mark.parametrize("other", ["message", "vote", "gossip", "hello"])
def test_a_file_hash_never_equals_another_preimages_hash_over_the_same_strings(other):
    mine = file_content_hash(**FIELDS)
    if other == "message":
        others = [
            message_content_hash(conversation_id=GROUP, message_id=FIELDS["message_id"],
                                 sender_node_id=AUTHOR, sender_name="1000", sender_type="65536",
                                 agent_owner=FIELDS["hash"], content=FIELDS["chunks_hash"]),
            message_content_hash(conversation_id=FILE_PREIMAGE_VERSION, message_id=GROUP,
                                 sender_node_id=FIELDS["message_id"], sender_name=AUTHOR),
        ]
    elif other == "vote":
        others = [vote_content_hash(proposal_id=FIELDS["message_id"], conversation_id=GROUP,
                                    voter_node_id=AUTHOR, vote="yes")]
    elif other == "gossip":
        others = [gossip_content_hash(message_id=FIELDS["message_id"], source=AUTHOR,
                                      destination=GROUP, payload=None, max_hops=None,
                                      created_at=None, ttl=None, priority=None, vector_clock=None)]
    else:
        others = [hello_content_hash(nonce=bytes(32), listener_node_id=GROUP, dialer_node_id=AUTHOR)]
    for value in others:
        assert value != mine


def test_the_time_is_not_signed():
    """M1 (pending Mike): the record's own signed timestamp dates the file."""
    with pytest.raises(TypeError):
        file_preimage(**FIELDS, created_at="2026-11-20T00:00:00Z")
