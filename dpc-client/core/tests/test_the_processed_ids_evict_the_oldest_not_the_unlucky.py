"""The dedup window drops the keys added first, not whichever hashed first.

The handlers used to cut `list(a_set)[:half]` at the cap. A set has no order, so
the key added a moment ago was as likely to go as the oldest, and a duplicate
arriving after that was stored twice (a 1:1 message has no history to catch it).
"""

from dpc_client_core.processed_message_ids import ProcessedMessageIds


def test_past_the_cap_the_first_added_is_gone_and_the_last_added_stays():
    ids = ProcessedMessageIds(max_size=10)
    keys = [f"group-x:{i:04d}" for i in range(11)]
    for key in keys:
        ids.add(key)

    assert keys[0] not in ids
    assert keys[-1] in ids


def test_the_oldest_half_goes_in_order_and_the_newer_half_stays():
    ids = ProcessedMessageIds(max_size=10)
    keys = [f"k{i}" for i in range(11)]
    for key in keys:
        ids.add(key)

    assert list(ids) == keys[5:]


def test_at_the_cap_nothing_goes():
    ids = ProcessedMessageIds(max_size=10)
    for i in range(10):
        ids.add(i)

    assert len(ids) == 10 and 0 in ids


def test_adding_a_key_again_does_not_grow_or_reorder():
    ids = ProcessedMessageIds(max_size=10)
    ids.add("a")
    ids.add("b")
    ids.add("a")

    assert list(ids) == ["a", "b"]


def test_discard_is_quiet_about_an_absent_key():
    ids = ProcessedMessageIds(max_size=10)
    ids.add("a")
    ids.discard("a")
    ids.discard("never-added")

    assert len(ids) == 0 and "a" not in ids

