"""`mention_depth` is inside the v3 preimage, and v1/v2 records keep their own bytes.

The receiver lets a peer agent's post wake an agent only while its depth is
below the bound (specs/dptp_v1.md §4.3). A relay that could lower the number
would lift the bound, so the number is signed — under a new version tag,
because a field appended to an existing one would make every v2 signature
already on disk fail its recomputation.
"""

import pytest

from dpc_protocol.message_signing import (
    LEGACY_PREIMAGE_VERSIONS,
    PREIMAGE_VERSION,
    message_content_hash,
    message_preimage,
)


def _fields(**overrides):
    base = dict(
        conversation_id="group-b88b65076b85",
        message_id="0adb7474c0ffee00",
        sender_node_id="dpc-node-eddf0e58aaaaaaaaaaaaaaaaaaaaaaaa",
        sender_name="CC_win10",
        sender_type="agent",
        agent_owner="dpc-node-eddf0e58aaaaaaaaaaaaaaaaaaaaaaaa",
        timestamp="2026-10-20T10:00:00+00:00",
        content="@Ark can you check?",
        tool_calls=None,
    )
    base.update(overrides)
    return base


def test_the_current_version_is_v3_and_both_older_ones_stay_readable():
    assert PREIMAGE_VERSION == "dptp-msg-v3"
    assert LEGACY_PREIMAGE_VERSIONS == ("dptp-msg-v1", "dptp-msg-v2"), (
        "index 0 is the default for a record with no tag, and it is v1")


def test_v3_covers_the_depth():
    assert message_content_hash(**_fields(mention_depth=0)) != \
        message_content_hash(**_fields(mention_depth=1))


@pytest.mark.parametrize("same_as_zero", [None, "", 0, "0"])
def test_absent_and_zero_are_one_value(same_as_zero):
    assert message_content_hash(**_fields(mention_depth=same_as_zero)) == \
        message_content_hash(**_fields(mention_depth=0))


def test_a_depth_spelled_as_text_hashes_as_the_number():
    assert message_content_hash(**_fields(mention_depth="1")) == \
        message_content_hash(**_fields(mention_depth=1))


@pytest.mark.parametrize("version", ["dptp-msg-v1", "dptp-msg-v2"])
def test_an_older_version_ignores_the_depth_and_keeps_its_ten_fields(version):
    """A v2 signature already on disk must still recompute to the same bytes."""
    with_depth = message_preimage(**_fields(mention_depth=3), version=version)
    without = message_preimage(**_fields(), version=version)
    assert with_depth == without
    assert without.count(b":") >= 10
    assert not message_preimage(**_fields(), version=PREIMAGE_VERSION).startswith(
        without), "v3 must be told apart by its tag, not only by a longer tail"


def test_v3_appends_the_depth_as_the_last_field():
    preimage = message_preimage(**_fields(mention_depth=1))
    assert preimage.endswith(b"1:1")
    assert message_preimage(**_fields()).endswith(b"0:"), "depth 0 is an empty field"
