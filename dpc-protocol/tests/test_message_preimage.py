"""What a message signature must cover, and how that coverage is encoded.

The old preimage was an f-string of four fields joined by "|". Two things were
wrong with it, and only one is obvious:

  - it left group_id, sender_name, sender_type, agent_owner and tool_calls
    outside the signature, so each was forgeable under a valid one;
  - it was injective only by accident. Four fields means three separators,
    always, so no field could swallow a boundary. Add one optional field and
    that accident ends.

Length-prefixed encoding removes the accident, and the tests below pin both
properties: what is covered, and that coverage stays unambiguous.
"""

import pytest

from dpc_protocol.message_signing import (
    PREIMAGE_VERSION,
    message_content_hash,
    message_preimage,
)


def _fields(**overrides):
    base = dict(
        conversation_id="group-b88b65076b85",
        message_id="c0ffee00-1234-4000-8000-000000000001",
        sender_node_id="dpc-node-86cdcd262c7f81bb58f48adbccdc86e3",
        sender_name="Mike Windows PC",
        sender_type="human",
        agent_owner=None,
        timestamp="2026-08-05T10:00:00+00:00",
        content="transfer approved",
        tool_calls=None,
    )
    base.update(overrides)
    return base


# --- what the signature covers -------------------------------------------

@pytest.mark.parametrize("field, other", [
    ("conversation_id", "group-970e5c7006a0"),   # replay into another group
    ("message_id", "c0ffee00-1234-4000-8000-000000000002"),
    ("sender_node_id", "dpc-node-6d218e95dee9cfeebfc3caa705ae8c95"),
    ("sender_name", "Ark"),                       # display-name spoofing
    ("sender_type", "agent"),                     # human presented as agent
    ("agent_owner", "dpc-node-6d218e95dee9cfeebfc3caa705ae8c95"),
    ("timestamp", "2026-08-05T11:00:00+00:00"),
    ("content", "transfer denied"),
])
def test_changing_any_covered_field_changes_the_hash(field, other):
    assert message_content_hash(**_fields()) != message_content_hash(**_fields(**{field: other}))


def test_tool_calls_are_covered_because_they_are_an_audit_trail():
    """An agent's actions are the part of its message worth forging."""
    honest = _fields(sender_type="agent", tool_calls=[{"name": "shell", "args": {"cmd": "ls"}}])
    tampered = _fields(sender_type="agent", tool_calls=[{"name": "shell", "args": {"cmd": "rm -rf /"}}])

    assert message_content_hash(**honest) != message_content_hash(**tampered)


def test_tool_calls_hash_the_same_whatever_order_the_keys_arrive_in():
    """Two nodes serialising the same call must agree, or every agent message rejects."""
    one = _fields(tool_calls=[{"name": "shell", "args": {"b": 2, "a": 1}}])
    other = _fields(tool_calls=[{"args": {"a": 1, "b": 2}, "name": "shell"}])

    assert message_content_hash(**one) == message_content_hash(**other)


# --- unambiguous encoding -------------------------------------------------

def test_a_field_cannot_swallow_the_boundary_of_the_next_one():
    """The property the old "|".join relied on by luck, held on purpose here."""
    split = _fields(sender_name="Mike", content="hello")
    merged = _fields(sender_name="Mike|hello", content="")

    assert message_content_hash(**split) != message_content_hash(**merged)


def test_an_empty_field_is_distinct_from_an_absent_one():
    assert message_content_hash(**_fields(agent_owner="")) == \
           message_content_hash(**_fields(agent_owner=None))
    assert message_content_hash(**_fields(content="")) != \
           message_content_hash(**_fields(content=" "))


def test_the_version_tag_opens_the_preimage():
    """A future field set must not verify against this one's signatures."""
    assert message_preimage(**_fields()).startswith(
        f"{len(PREIMAGE_VERSION)}:{PREIMAGE_VERSION}".encode("utf-8")
    )


# --- cross-platform determinism ------------------------------------------

def test_the_same_instant_written_three_ways_hashes_the_same():
    """Linux, macOS and Windows do not have to agree on how to spell UTC.

    Otherwise a message signed on one and verified on another rejects while
    nothing malicious happened (GLM A1.5).
    """
    z = _fields(timestamp="2026-08-05T10:00:00Z")
    offset = _fields(timestamp="2026-08-05T10:00:00+00:00")
    shifted = _fields(timestamp="2026-08-05T13:00:00+03:00")

    assert message_content_hash(**z) == message_content_hash(**offset)
    assert message_content_hash(**z) == message_content_hash(**shifted)


def test_a_timestamp_that_cannot_be_parsed_is_still_covered_verbatim():
    """Unparseable is not a licence to ignore the field."""
    assert message_content_hash(**_fields(timestamp="whenever")) != \
           message_content_hash(**_fields(timestamp="later"))


def test_the_preimage_is_bytes_and_utf8_content_survives_it():
    preimage = message_preimage(**_fields(content="перевод одобрен ✅"))

    assert isinstance(preimage, bytes)
    assert "перевод одобрен ✅".encode("utf-8") in preimage
