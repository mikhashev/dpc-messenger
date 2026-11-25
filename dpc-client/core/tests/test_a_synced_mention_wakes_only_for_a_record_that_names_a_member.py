"""A mention that arrives by history sync wakes an agent only for a record that names a member.

Follow-up 11 to 10a (review by Zcode, #27, history merge P2): `off_the_roster`
skips an empty id, so a record naming no author passed the gate at the merge
and then woke an agent, and the roster check at wake time (`group_handler.py`,
`_wake_merged_mentions`) was held in place by no test.

Real `ConversationMonitor` and real `GroupHistoryResponseHandler` on the
receiving side of the merge, the same doubles as
`test_a_mention_that_arrives_by_history_sync_wakes_once`. Only the agent run
itself is a double.
"""

import logging

import pytest

from tests.test_a_mention_that_arrives_by_history_sync_wakes_once import (
    HANDLER_LOGGER,
    _Service,
    _key,
    _record,
    _sync,
    keys,  # noqa: F401  (fixture)
)


@pytest.mark.asyncio
async def test_a_merged_record_that_names_no_author_wakes_nobody_and_says_so(keys, caplog):
    """The record is kept (history is not censored); the agent is not started by it."""
    caplog.set_level(logging.INFO, logger=HANDLER_LOGGER)
    peer = keys.peer[0]
    service = _Service([peer])
    record = _record(keys.peer, "@Ark do the thing", signed=False)
    del record["sender_node_id"]
    del record["sender_name"]

    await _sync(service, peer, [record])

    assert record["id"] in service.monitor.message_ids, "merged: the gate is for waking only"
    assert service.run.await_count == 0
    assert any("names no author" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_record_naming_a_member_in_the_same_answer_still_wakes(keys):
    """Control: the author-less record is skipped alone."""
    peer = keys.peer[0]
    service = _Service([peer])
    nameless = _record(keys.peer, "@Ark from nobody", signed=False)
    del nameless["sender_node_id"]
    ours = _record(keys.peer, "@Ark from a member")

    await _sync(service, peer, [nameless, ours])

    assert nameless["id"] in service.monitor.message_ids and ours["id"] in service.monitor.message_ids
    assert service.run.await_count == 1
    assert service.run.await_args.kwargs["trigger_message_id"] == ours["id"]


@pytest.fixture
def second_member(keys, tmp_path):
    """A second signing node whose certificate is cached, so its record verifies."""
    return _key(tmp_path / "peers")


@pytest.mark.asyncio
async def test_an_author_removed_between_the_merge_and_the_wake_wakes_nobody(keys, second_member, caplog):
    """The roster is read again at wake time: the awaits after the merge (the UI
    refresh, the votes) let it move, and a record merged as a member's must not
    start an agent for a node the creator has removed since.

    The removal happens inside the `group_history_synced` broadcast, the last
    await before the wake, on the path the live handler takes.
    """
    caplog.set_level(logging.INFO, logger=HANDLER_LOGGER)
    peer, other = keys.peer[0], second_member[0]
    service = _Service([peer, other])
    record = _record(second_member, "@Ark from a node removed a moment ago")

    async def the_creator_removes_the_author(name, payload):
        if name == "group_history_synced":
            service.group.members.remove(other)

    service.local_api.broadcast_event.side_effect = the_creator_removes_the_author

    await _sync(service, peer, [record])

    assert record["id"] in service.monitor.message_ids, "it was a member's when merged"
    assert other not in service.group.members
    assert service.run.await_count == 0
    assert any("is not a member" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_an_author_still_on_the_roster_at_the_wake_does_wake(keys, second_member):
    """Control for the test above: the same flow with no removal wakes once."""
    peer, other = keys.peer[0], second_member[0]
    service = _Service([peer, other])
    record = _record(second_member, "@Ark from a member")

    await _sync(service, peer, [record])

    assert service.run.await_count == 1
    assert service.run.await_args.kwargs["trigger_message_id"] == record["id"]


# -- follow-up 14 (Zcode #176): an agent's file post wakes nobody, here as on its node ----------

@pytest.mark.asyncio
async def test_a_synced_agent_file_post_wakes_nobody_and_says_so(keys, caplog):
    caplog.set_level(logging.INFO, logger=HANDLER_LOGGER)
    peer = keys.peer[0]
    service = _Service([peer])
    record = _record(keys.peer, "@Ark here is the take")
    record["attachments"] = [{"type": "file", "filename": "take.flac", "size_bytes": 10,
                              "mime_type": "audio/flac"}]

    await _sync(service, peer, [record])

    assert record["id"] in service.monitor.message_ids, "merged: the rule is for waking only"
    assert service.run.await_count == 0
    assert any("an agent's file post" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_persons_synced_file_caption_still_wakes(keys):
    peer = keys.peer[0]
    service = _Service([peer])
    record = _record(keys.peer, "@Ark listen to this", sender_type="human", name="Miron")
    record["attachments"] = [{"type": "file", "filename": "take.flac", "size_bytes": 10,
                              "mime_type": "audio/flac"}]

    await _sync(service, peer, [record])

    assert service.run.await_count == 1
