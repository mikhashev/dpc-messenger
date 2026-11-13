"""A session marker is compared as a moment and refused when it lies in the future.

Team vote 2026-11-13 (A-SESSION-MARKER-IS-COMPARED-AS-A-STRING-AND-NEVER-CHECKED-AGAINST-THE-CLOCK):
markers were compared as strings, and nothing checked them against the clock,
so one sync carrying a far-future marker stuck for good — it survived every
later sync and `set_session_marker` refused every genuine reset after it. Now a
marker that does not parse, or lies further ahead than BOUNDARY_CLOCK_SKEW (the
rule a peer's live-history boundary already meets), is refused with a WARNING
naming the sender, and the receiver keeps the marker it had.
"""

import logging
from datetime import datetime, timedelta, timezone

import pytest

from dpc_client_core.message_handlers.group_handler import GroupSyncHandler
from tests.test_a_session_marker_reaches_an_unloaded_group import (  # noqa: F401
    EARLIER,
    LATER,
    PEER,
    SELF,
    _signed,
    _timestamps_on_disk,
    verifier,
    world,
)

MANAGER_LOGGER = "dpc_client_core.managers.group_manager"


def _iso(moment):
    return moment.isoformat()


def _evidence(gid):
    return {"proposal_id": "p1", "conversation_id": gid, "participants": [SELF, PEER],
            "votes": {SELF: _signed(SELF, gid), PEER: _signed(PEER, gid)}}


def _sync(manager, gid, marker):
    payload = manager.get_group(gid).to_dict()
    payload["version"] += 1
    payload["session_started_at"] = marker
    payload["session_reset_evidence"] = _evidence(gid)
    return payload


@pytest.mark.asyncio
async def test_a_marker_two_days_ahead_trims_nothing_and_keeps_ours(world, verifier, caplog):
    """Johnny's test: marker T here, T + 2 days from a peer, with a quorum that
    would prove — history untouched, T kept, and the next genuine reset lands."""
    service, manager, group, history_path = world
    gid = group.group_id
    t = _iso(datetime.now(timezone.utc) - timedelta(days=1))
    assert manager.set_session_marker(gid, t, _evidence(gid)) is not None
    ahead = _iso(datetime.fromisoformat(t) + timedelta(days=2))
    caplog.set_level(logging.WARNING, logger=MANAGER_LOGGER)

    await GroupSyncHandler(service).handle(PEER, _sync(manager, gid, ahead))

    assert _timestamps_on_disk(history_path) == [EARLIER, EARLIER, LATER]
    assert manager.get_group(gid).session_started_at == t
    refusals = [r.getMessage() for r in caplog.records if "Refusing session marker" in r.getMessage()]
    assert len(refusals) == 1 and PEER in refusals[0] and "in the future" in refusals[0]

    now = _iso(datetime.now(timezone.utc))
    assert manager.set_session_marker(gid, now, _evidence(gid)) is not None
    assert manager.get_group(gid).session_started_at == now


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["9999-01-01T00:00:00+00:00", "yesterday", "2026-13-45"])
async def test_a_far_future_or_unreadable_marker_is_refused_on_a_group_with_none(world, verifier, marker):
    service, manager, group, history_path = world
    gid = group.group_id

    await GroupSyncHandler(service).handle(PEER, _sync(manager, gid, marker))

    assert manager.get_group(gid).session_started_at is None
    assert manager.get_group(gid).session_reset_evidence is None
    assert _timestamps_on_disk(history_path) == [EARLIER, EARLIER, LATER]
    now = _iso(datetime.now(timezone.utc))
    assert manager.set_session_marker(gid, now, None) is not None


def test_the_rest_of_the_record_still_applies_when_its_marker_is_refused(world):
    _, manager, group, _ = world
    gid = group.group_id
    payload = _sync(manager, gid, "9999-01-01T00:00:00+00:00")
    payload["topic"] = "new topic"

    applied = manager.apply_sync(payload, PEER)

    assert applied is not None and applied.topic == "new topic"
    assert applied.session_started_at is None


def test_a_marker_within_the_skew_is_taken(world):
    _, manager, group, _ = world
    gid = group.group_id
    soon = _iso(datetime.now(timezone.utc) + timedelta(minutes=2))

    applied = manager.apply_sync(_sync(manager, gid, soon), PEER)

    assert applied.session_started_at == soon


def test_markers_order_by_moment_not_by_spelling(world):
    """`...45.5+00:00` is later than `...45Z` but sorts before it as a string."""
    _, manager, group, _ = world
    gid = group.group_id
    assert manager.set_session_marker(gid, "2026-11-01T02:17:45Z", None) is not None

    applied = manager.apply_sync(_sync(manager, gid, "2026-11-01T02:17:45.5+00:00"), PEER)
    assert applied.session_started_at == "2026-11-01T02:17:45.5+00:00"

    # And the other way: a record carrying an earlier moment spelled to sort
    # later keeps ours.
    assert manager.set_session_marker(gid, "2026-11-02T00:00:00.5+00:00", None) is not None
    kept = manager.apply_sync(_sync(manager, gid, "2026-11-02T00:00:00Z"), PEER)
    assert kept.session_started_at == "2026-11-02T00:00:00.5+00:00"


def test_set_session_marker_refuses_a_future_or_unreadable_marker_of_its_own(world, caplog):
    _, manager, group, _ = world
    gid = group.group_id
    caplog.set_level(logging.WARNING, logger=MANAGER_LOGGER)

    assert manager.set_session_marker(gid, "9999-01-01T00:00:00+00:00", None) is None
    assert manager.set_session_marker(gid, "not a moment", None) is None

    assert manager.get_group(gid).session_started_at is None
    assert sum("Refusing session marker" in r.getMessage() for r in caplog.records) == 2


def test_a_poisoned_marker_already_on_disk_does_not_block_a_genuine_reset(world):
    """A marker stored before this rule existed stops nothing once read back."""
    _, manager, group, _ = world
    gid = group.group_id
    manager.get_group(gid).session_started_at = "9999-01-01T00:00:00+00:00"

    now = _iso(datetime.now(timezone.utc))
    assert manager.set_session_marker(gid, now, None) is not None
    assert manager.get_group(gid).session_started_at == now
