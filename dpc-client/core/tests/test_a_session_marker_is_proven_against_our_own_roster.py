"""A session marker is proven against the roster we held before the sync, and
what it trims is archived first.

The evidence's participant list and the record's members travel in the same
GROUP_SYNC as the marker, so proving the quorum over either one let any member
erase everyone's history with a quorum of itself
(A-SESSION-MARKER-PROVES-A-QUORUM-OF-A-SET-THE-ANNOUNCER-SUPPLIED).
"""

import json

import pytest

from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.message_handlers.group_handler import GroupSyncHandler
from tests.test_a_session_marker_reaches_an_unloaded_group import (  # noqa: F401
    EARLIER,
    LATER,
    PEER,
    SELF,
    _signed,
    _sync_with_marker,
    _timestamps_on_disk,
    verifier,
    world,
)


def _archives(history_path):
    return sorted((history_path.parent / "archive").rglob("*_session.json"))


@pytest.mark.asyncio
async def test_a_quorum_of_the_announcer_alone_erases_nothing(world, verifier):
    service, _, group, history_path = world
    gid = group.group_id
    payload = _sync_with_marker(group, {
        "proposal_id": "p1", "conversation_id": gid,
        "participants": [PEER], "votes": {PEER: _signed(PEER, gid)},
    })
    payload["members"] = [PEER]  # the letter rewrites the roster too

    await GroupSyncHandler(service).handle(PEER, payload)

    assert _timestamps_on_disk(history_path) == [EARLIER, EARLIER, LATER]
    assert not any(name == "conversation_reset" for name, _ in service.local_api.events)


@pytest.mark.asyncio
async def test_votes_from_another_group_do_not_prove_this_one(world, verifier):
    service, _, group, history_path = world
    other = "group-000000000000"
    await GroupSyncHandler(service).handle(PEER, _sync_with_marker(group, {
        "proposal_id": "p1", "conversation_id": other,
        "participants": [SELF, PEER],
        "votes": {SELF: _signed(SELF, other), PEER: _signed(PEER, other)},
    }))

    assert _timestamps_on_disk(history_path) == [EARLIER, EARLIER, LATER]


@pytest.mark.asyncio
async def test_no_roster_before_the_sync_means_no_trim(world, verifier):
    service, manager, group, history_path = world
    gid = group.group_id
    manager.apply_sync(_sync_with_marker(group, {
        "proposal_id": "p1", "conversation_id": gid, "participants": [SELF, PEER],
        "votes": {SELF: _signed(SELF, gid), PEER: _signed(PEER, gid)},
    }))

    await GroupSyncHandler(service)._honour_session_marker(
        frozenset(), None, manager.get_group(gid)
    )

    assert _timestamps_on_disk(history_path) == [EARLIER, EARLIER, LATER]


@pytest.mark.asyncio
async def test_the_trimmed_span_is_archived_and_only_that_span(world, verifier):
    service, _, group, history_path = world
    gid = group.group_id
    await GroupSyncHandler(service).handle(PEER, _sync_with_marker(group, {
        "proposal_id": "p1", "conversation_id": gid, "participants": [SELF, PEER],
        "votes": {SELF: _signed(SELF, gid), PEER: _signed(PEER, gid)},
    }))

    assert _timestamps_on_disk(history_path) == [LATER]
    (archive,) = _archives(history_path)
    archived = json.loads(archive.read_text(encoding="utf-8"))
    assert [m["timestamp"] for m in archived["messages"]] == [EARLIER, EARLIER]
    assert archived["session_reason"] == "marker"


@pytest.mark.asyncio
async def test_a_failed_archive_cancels_the_trim(world, verifier, monkeypatch):
    service, _, group, history_path = world
    gid = group.group_id

    def _broken(self, messages, reason):
        raise OSError("disk full")

    monkeypatch.setattr(ConversationMonitor, "_archive_messages", _broken)

    await GroupSyncHandler(service).handle(PEER, _sync_with_marker(group, {
        "proposal_id": "p1", "conversation_id": gid, "participants": [SELF, PEER],
        "votes": {SELF: _signed(SELF, gid), PEER: _signed(PEER, gid)},
    }))

    assert _timestamps_on_disk(history_path) == [EARLIER, EARLIER, LATER]
    assert [m["timestamp"] for m in service.conversation_monitors[gid].message_history] == [
        EARLIER, EARLIER, LATER,
    ]
    assert not any(name == "conversation_reset" for name, _ in service.local_api.events)
