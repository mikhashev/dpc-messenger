"""A mention merged by history sync wakes nobody when its author is blocked.

`GroupHistoryResponseHandler._wake_merged_mentions` routes the mentions in
records a merge added, once, as if live (Mike's call, 2026-10-30). A record by
a blocked node is merged and kept like any other — a third member serves it
and the chain stays whole — but its mention is skipped, with one INFO line.

Real `ConversationMonitor` and `GroupHistoryResponseHandler`, the harness of
test_a_mention_that_arrives_by_history_sync_wakes_once.
"""

import logging

import pytest

from tests.test_a_mention_that_arrives_by_history_sync_wakes_once import (
    HANDLER_LOGGER,
    _record,
    _Service,
    _sync,
    keys,  # noqa: F401 - the fixture, by name
)


class _Firewall:
    def __init__(self, *blocked):
        self.blocked = set(blocked)

    def is_blocked(self, node_id):
        return node_id in self.blocked


THIRD = "dpc-node-" + "d" * 32


@pytest.mark.asyncio
async def test_a_blocked_authors_synced_mention_is_merged_and_wakes_nobody(keys, caplog):
    caplog.set_level(logging.INFO, logger=HANDLER_LOGGER)
    author = keys.peer[0]
    service = _Service([author, THIRD])
    service.firewall = _Firewall(author)
    record = _record(keys.peer, "@Ark @CC look", sender_type=None)

    await _sync(service, THIRD, [record])

    assert service.monitor.message_history[-1]["id"] == record["id"], "kept"
    assert service.run.await_count == 0
    assert service.cc_wakes() == []
    lines = [r.getMessage() for r in caplog.records
             if "wakes nobody" in r.getMessage() and "blocked node" in r.getMessage()]
    assert len(lines) == 1 and author in lines[0]


@pytest.mark.asyncio
async def test_the_same_synced_mention_by_an_unblocked_author_wakes(keys):
    author = keys.peer[0]
    service = _Service([author, THIRD])
    service.firewall = _Firewall("dpc-node-" + "e" * 32)
    record = _record(keys.peer, "@Ark @CC look", sender_type=None)

    await _sync(service, THIRD, [record])

    assert service.run.await_count == 1
    assert service.cc_wakes() == ["cc"]
