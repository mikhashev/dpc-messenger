"""Two honest nodes must stop asking each other for history they both have.

Before this, every connection reported divergence, the full history was
fetched, `merge_history` added nothing (dedup by id), and the tips still
differed — so the same exchange repeated on the next connection, forever. An
alarm that is always on is an alarm nobody reads.
"""

import pytest

from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.message_handlers.group_handler import GroupHistoryStatusHandler


GROUP = "group-1234"
ALICE = "dpc-node-6d218e95dee9cfeebfc3caa705ae8c95"
BOB = "dpc-node-86cdcd262c7f81bb58f48adbccdc86e3"


class _P2P:
    def __init__(self):
        self.sent = []

    async def send_message_to_peer(self, node_id, message):
        self.sent.append((node_id, message))


class _Service:
    def __init__(self, monitor):
        self.p2p_manager = _P2P()
        self.conversation_monitors = {GROUP: monitor}


def _monitor(rows):
    m = ConversationMonitor(
        conversation_id=GROUP,
        participants=[{"node_id": BOB, "name": "self", "context": "local"}],
        llm_manager=None,
    )
    for role, author, text in rows:
        m.add_message(role=role, content=text, sender_node_id=author,
                      sender_name="whoever", message_id=f"m-{text}",
                      timestamp="2026-08-06T00:00:00+00:00")
    return m


MINE = [("user", BOB, "first"), ("peer", ALICE, "second")]
# The same two messages as the other node sees them: roles inverted, order
# reversed. Everything the old tip covered differs; nothing real does.
THEIRS = [("user", ALICE, "second"), ("peer", BOB, "first")]


def _commands(service):
    return [m["command"] for _, m in service.p2p_manager.sent]


@pytest.mark.asyncio
async def test_agreeing_nodes_do_not_request_anything():
    mine, theirs = _monitor(MINE), _monitor(THEIRS)
    service = _Service(mine)

    await GroupHistoryStatusHandler(service).handle(ALICE, {
        "group_id": GROUP,
        "history_hash": theirs.compute_history_hash(),
        "message_count": 2,
        "history_digest": theirs.history_digest(),
        "is_reply": True,
    })

    assert "GROUP_HISTORY_REQUEST" not in _commands(service)


@pytest.mark.asyncio
async def test_the_old_tip_would_have_disagreed():
    """Guards the premise: without this fix the same input asks for a sync."""
    mine, theirs = _monitor(MINE), _monitor(THEIRS)

    assert mine.compute_history_hash() != theirs.compute_history_hash()


@pytest.mark.asyncio
async def test_a_real_gap_is_requested_and_named():
    mine = _monitor(MINE)
    theirs = _monitor(THEIRS + [("user", ALICE, "third")])
    service = _Service(mine)

    await GroupHistoryStatusHandler(service).handle(ALICE, {
        "group_id": GROUP,
        "history_hash": theirs.compute_history_hash(),
        "message_count": 3,
        "history_digest": theirs.history_digest(),
        "is_reply": True,
    })

    requests = [m for _, m in service.p2p_manager.sent if m["command"] == "GROUP_HISTORY_REQUEST"]
    assert requests, "a genuine difference must still be fetched"
    assert requests[0]["payload"]["authors"] == [ALICE], "only the author who differs"


@pytest.mark.asyncio
async def test_a_peer_without_a_digest_falls_back_to_the_old_comparison():
    """An old node can only answer the old question; it must still be asked."""
    mine, theirs = _monitor(MINE), _monitor(THEIRS)
    service = _Service(mine)

    await GroupHistoryStatusHandler(service).handle(ALICE, {
        "group_id": GROUP,
        "history_hash": theirs.compute_history_hash(),
        "message_count": 2,
        "is_reply": True,
    })

    assert "GROUP_HISTORY_REQUEST" in _commands(service)


@pytest.mark.asyncio
async def test_the_reply_carries_the_digest_onward():
    mine = _monitor(MINE)
    service = _Service(mine)

    await GroupHistoryStatusHandler(service).handle(ALICE, {
        "group_id": GROUP,
        "history_hash": "sha256:whatever",
        "message_count": 2,
        "history_digest": _monitor(THEIRS).history_digest(),
    })

    replies = [m for _, m in service.p2p_manager.sent if m["command"] == "GROUP_HISTORY_STATUS"]
    assert replies and replies[0]["payload"]["history_digest"]["authors"]
