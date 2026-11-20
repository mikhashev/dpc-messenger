"""A bridge history line shows the group's own message number, the #N the chat shows.

ChatMessageList.svelte prints `#{msg.msg_index}` beside the time. The bridge prints
[i] for the slot in the window it read; without #N a reader could not cite the
message a human cites ("see #152"). A message with no msg_index (old history) keeps
the old line.
"""

from tests.test_a_bridge_answers_to_the_tag_it_registered import bridge  # noqa: F401

ME = "dpc-node-" + "a" * 32
BASE = {"sender_name": "Mike", "content": "hi", "timestamp": "2026-11-21T10:00:00",
        "sender_type": "human", "sender_node_id": ME}


def test_the_line_carries_the_number_the_chat_shows(bridge):  # noqa: F811
    assert bridge.format_message(9, dict(BASE, msg_index=152), ME) == "  [10] #152 10:00:00 Mike: hi"


def test_a_message_without_a_number_keeps_the_old_line(bridge):  # noqa: F811
    assert bridge.format_message(0, dict(BASE), ME) == "  [1] 10:00:00 Mike: hi"
    for bad in (0, -3, "152", True, None):
        assert bridge.format_message(0, dict(BASE, msg_index=bad), ME) == "  [1] 10:00:00 Mike: hi", bad
