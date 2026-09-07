"""A proposal's window names only records another node could verify.

The window is the promise «I judged the same text as you». It is made of
`content_hash` values, and a voter that lacks one of them is held. That is right
while the hash is one a peer could hold — and wrong for a record whose hash does
not follow from the record itself: no honest node can ever produce it, so naming
it in the window refuses every other participant on every future proposal, for
good.

The function already refused to name what it cannot prove — records written
before signing existed carry no hash and are left out, «rather than faked». This
extends the same rule to the second class of unprovable record, the one that has
a hash which does not reproduce.
"""

import pytest

from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_protocol.message_signing import message_content_hash

GROUP = "group-window-test"
ALICE = "dpc-node-" + "a" * 32


@pytest.fixture(autouse=True)
def _never_touch_disk(monkeypatch):
    monkeypatch.setattr(
        ConversationMonitor, "persist_history", property(lambda self: False)
    )


def _monitor(texts=("one", "two", "three")):
    m = ConversationMonitor(
        conversation_id=GROUP,
        participants=[{"node_id": ALICE, "name": "Mike", "context": "local"}],
        llm_manager=None,
    )
    for text in texts:
        m.add_message(role="user", content=text, sender_node_id=ALICE, sender_name="Mike")
    return m


def _window(monitor):
    """The window a proposal would carry for the whole conversation."""
    return monitor.window_content_hashes(list(monitor.message_history))


def test_a_whole_honest_history_is_named_in_full():
    m = _monitor()
    assert _window(m) == [msg["content_hash"] for msg in m.message_history]


def test_a_record_edited_under_its_own_hash_is_left_out():
    m = _monitor()
    gone = m.message_history[1]["content_hash"]
    m.message_history[1]["content"] = "something else entirely"

    window = _window(m)

    assert gone not in window
    assert len(window) == 2


def test_the_tool_calls_shape_is_the_one_that_matters():
    """The live case, and it is a v1 case: a hash taken before the calls were
    stored beside it.

    Under `dptp-msg-v1` the calls were inside the preimage, so appending them
    after signing left a record no receiver could reproduce — the eleven this
    pair still carries. Under v2 the preimage covers their digest, so the same
    mistake is harmless; the rule this function enforces is unchanged, but the
    records it now catches are the old ones.
    """
    m = _monitor()
    stored = m.message_history[1]
    assert m._hash_matches_record(stored)

    stored["preimage_version"] = "dptp-msg-v1"
    stored["content_hash"] = message_content_hash(
        conversation_id=GROUP,
        message_id=stored.get("id"),
        sender_node_id=stored.get("sender_node_id"),
        sender_name=stored.get("sender_name"),
        sender_type=stored.get("sender_type"),
        agent_owner=stored.get("agent_owner"),
        timestamp=stored.get("timestamp"),
        content=stored.get("content") or "",
        tool_calls=None,
        version="dptp-msg-v1",
    )
    assert m._hash_matches_record(stored), "a v1 record with no calls must still verify"

    stored["tool_calls"] = [{"function": {"name": "read_file"}, "round_text": "..."}]

    assert not m._hash_matches_record(stored)
    assert stored["content_hash"] not in _window(m)


def test_appending_calls_after_signing_is_harmless_under_v2():
    """The bug class that cost this pair two days cannot recur.

    v2 signs the digest the record carries, so calls written into the record
    afterwards change nothing a peer checks — which is also why they no longer
    need to travel at all.
    """
    m = _monitor()
    stored = m.message_history[1]

    stored["tool_calls"] = [{"function": {"name": "read_file"}, "round_text": "..."}]

    assert m._hash_matches_record(stored)
    assert stored["content_hash"] in _window(m)


def test_a_record_with_no_hash_at_all_is_still_left_out():
    """The rule that was already there keeps holding."""
    m = _monitor()
    m.message_history[0].pop("content_hash")

    assert len(_window(m)) == 2


def test_leaving_a_record_out_does_not_reorder_the_rest():
    m = _monitor(("one", "two", "three", "four"))
    kept = [m.message_history[i]["content_hash"] for i in (0, 2, 3)]
    m.message_history[1]["content"] = "edited"

    assert _window(m) == kept
