"""The creator owns the roster: `created_by` is pinned and `members` is the creator's.

Team vote 2026-11-13, ADR-038 part 1 (GROUP-ROSTER-HAS-NO-OWNER). Before it,
`apply_sync` replaced the whole record on a higher version, `created_by`
included, so any member could name itself creator with one record at
version + 1 and then pass every creator check. Now:

- a record naming another creator than the pinned one is refused whole, at any
  version, with a WARNING carrying both ids;
- `members` is taken only from the creator (the transport sender: the record is
  unsigned, so that is the only author a receiver can name); from anyone else
  the record goes on with our roster, so a member's own `agents` entry and a
  topic still ride the version bump, and a refused roster cannot win the
  equal-version tie-break;
- at equal versions the creator's roster wins outright;
- the local doors refuse too: add and remove for a node that is not the
  creator, and leave (and removing itself) for the creator.
"""

import logging

import pytest

from dpc_client_core.managers.group_manager import GroupManager, GroupMetadata
from dpc_client_core.message_handlers.group_handler import GroupSyncHandler
from tests.test_group_roster_and_history_gate import _Api, _P2P, _Service

CREATOR = "dpc-node-" + "c" * 32
SELF = "dpc-node-3333333333333333333333333333cccc"  # _P2P's node id
MEMBER = "dpc-node-" + "e" * 32
GID = "group-0123456789ab"
MANAGER_LOGGER = "dpc_client_core.managers.group_manager"


def _record(**over):
    fields = dict(group_id=GID, name="work", topic="", created_by=CREATOR,
                  members=[CREATOR, SELF, MEMBER], version=3)
    fields.update(over)
    return GroupMetadata(**fields).to_dict()


@pytest.fixture
def manager(tmp_path):
    manager = GroupManager(dpc_home=tmp_path, node_id=SELF)
    assert manager.apply_sync(_record(), CREATOR) is not None
    return manager


def _held(manager):
    return manager.get_group(GID)


def _refusals(caplog, words):
    return [r.getMessage() for r in caplog.records if words in r.getMessage()]


# --- created_by is pinned ------------------------------------------------

@pytest.mark.parametrize("version", [2, 3, 4, 10 ** 6])
def test_a_record_naming_another_creator_is_refused_at_any_version(manager, caplog, version):
    caplog.set_level(logging.WARNING, logger=MANAGER_LOGGER)

    result = manager.apply_sync(
        _record(created_by=MEMBER, version=version, topic="mine now"), MEMBER)

    assert result is None
    assert _held(manager).created_by == CREATOR
    assert _held(manager).topic == "" and _held(manager).version == 3
    (line,) = _refusals(caplog, "names creator")
    assert MEMBER in line and CREATOR in line


@pytest.mark.asyncio
async def test_the_one_record_takeover_adr_038_derived_no_longer_works(manager):
    """A member sends created_by = itself at version + 1; nobody adopts it, so
    the creator-only delete still names the real creator."""
    service = _Service(manager)

    await GroupSyncHandler(service).handle(
        MEMBER, _record(created_by=MEMBER, version=4, members=[MEMBER]))

    assert _held(manager).created_by == CREATOR
    assert _held(manager).members == [CREATOR, SELF, MEMBER]
    assert manager.delete_group(GID, MEMBER) is False


@pytest.mark.asyncio
async def test_the_creators_group_sync_through_the_handler_changes_the_roster(manager):
    """The handler hands the transport sender to apply_sync: were it None, the
    creator's roster would be refused like any unnamed sender's."""
    service = _Service(manager)
    newcomer = "dpc-node-" + "f" * 32
    roster = [CREATOR, SELF, newcomer]  # MEMBER removed, newcomer added

    await GroupSyncHandler(service).handle(CREATOR, _record(version=4, members=roster))

    assert _held(manager).members == roster
    assert _held(manager).version == 4


# --- members is the creator's --------------------------------------------

def test_a_non_creators_roster_at_a_higher_version_is_not_applied(manager, caplog):
    caplog.set_level(logging.WARNING, logger=MANAGER_LOGGER)

    applied = manager.apply_sync(
        _record(version=4, members=[MEMBER, "dpc-node-phantom"], topic="from a member"), MEMBER)

    assert applied is not None, "the rest of the record still rides the version"
    assert _held(manager).members == [CREATOR, SELF, MEMBER]
    assert _held(manager).topic == "from a member"
    assert _held(manager).version == 4
    (line,) = _refusals(caplog, "Not applying the roster change")
    assert MEMBER in line and CREATOR in line


def test_a_non_creators_roster_at_equal_version_does_not_win_the_tie_break(manager):
    """Johnny's test. The phantom is chosen so the record's content hash is the
    greater one: without the rule it would win the tie and bump the version."""
    local_hash = GroupManager._content_hash(_held(manager))
    for i in range(64):
        offered = _record(members=[MEMBER, f"dpc-node-phantom-{i}"])
        if GroupManager._content_hash(GroupMetadata.from_dict(offered)) > local_hash:
            break
    else:
        pytest.fail("no phantom roster with a greater hash in 64 tries")

    result = manager.apply_sync(offered, MEMBER)

    assert result is None
    assert _held(manager).members == [CREATOR, SELF, MEMBER]
    assert _held(manager).version == 3


def test_an_unnamed_sender_is_not_the_creator(manager):
    manager.apply_sync(_record(version=4, members=[CREATOR]))
    assert _held(manager).members == [CREATOR, SELF, MEMBER]


def test_a_members_own_agents_and_a_topic_still_ride_the_version(manager):
    """Zcode's condition: the roster rule freezes nothing but `members`."""
    applied = manager.apply_sync(_record(
        version=4, topic="new topic",
        agents={MEMBER: ["agent_007"]}, agent_names={MEMBER: {"agent_007": "Bond"}},
    ), MEMBER)

    assert applied is not None
    held = _held(manager)
    assert held.agents == {MEMBER: ["agent_007"]}
    assert held.agent_names == {MEMBER: {"agent_007": "Bond"}}
    assert held.topic == "new topic"
    assert held.members == [CREATOR, SELF, MEMBER]


def test_the_creators_roster_change_is_applied(manager):
    applied = manager.apply_sync(_record(version=4, members=[CREATOR, SELF]), CREATOR)

    assert applied is not None
    assert _held(manager).members == [CREATOR, SELF]


def test_at_equal_version_the_creators_roster_wins_even_with_the_lesser_hash(manager):
    """Otherwise a member's copy (our roster, a member's version) and the
    creator's could stay split by the hash until the creator changed something."""
    local_hash = GroupManager._content_hash(_held(manager))
    for i in range(64):
        offered = _record(members=[CREATOR, SELF, f"dpc-node-new-{i}"])
        if GroupManager._content_hash(GroupMetadata.from_dict(offered)) < local_hash:
            break
    else:
        pytest.fail("no roster with a lesser hash in 64 tries")

    applied = manager.apply_sync(offered, CREATOR)

    assert applied is not None
    assert _held(manager).members == offered["members"]
    assert _held(manager).version == 4


def test_a_member_and_the_creator_converge_after_a_refused_roster(tmp_path):
    """The star: a member's record reaches us first, then the creator's."""
    receiver = GroupManager(dpc_home=tmp_path, node_id=SELF)
    receiver.apply_sync(_record(), CREATOR)
    receiver.apply_sync(_record(version=4, agents={MEMBER: ["a1"]}), MEMBER)

    receiver.apply_sync(_record(version=4, members=[CREATOR, SELF]), CREATOR)

    assert receiver.get_group(GID).members == [CREATOR, SELF]


# --- a group with no creator ---------------------------------------------

def test_a_group_held_with_no_creator_takes_no_roster_and_no_creator(tmp_path, caplog):
    manager = GroupManager(dpc_home=tmp_path, node_id=SELF)
    manager.apply_sync(_record(created_by=""), MEMBER)
    caplog.set_level(logging.WARNING, logger=MANAGER_LOGGER)

    assert manager.apply_sync(_record(created_by=MEMBER, version=9), MEMBER) is None
    manager.apply_sync(_record(created_by="", version=9, members=[MEMBER]), MEMBER)

    assert _held(manager).created_by == ""
    assert _held(manager).members == [CREATOR, SELF, MEMBER]
    assert _held(manager).version == 9
    assert _refusals(caplog, "names creator") and _refusals(caplog, "Not applying the roster")


# --- the local doors ------------------------------------------------------

def _service(manager):
    from dpc_client_core.service import CoreService

    service = CoreService.__new__(CoreService)
    service.group_manager = manager
    service.p2p_manager = _P2P()
    service.local_api = _Api()
    return service


@pytest.mark.asyncio
async def test_a_member_that_is_not_the_creator_cannot_add_or_remove(manager):
    service = _service(manager)

    added = await service.add_group_member(GID, "dpc-node-newcomer")
    removed = await service.remove_group_member(GID, MEMBER)

    assert added["status"] == "error" and "creator" in added["message"]
    assert removed["status"] == "error" and "creator" in removed["message"]
    assert _held(manager).members == [CREATOR, SELF, MEMBER]
    assert _held(manager).version == 3
    assert service.p2p_manager.sent == []


@pytest.mark.asyncio
async def test_the_creator_cannot_leave_or_remove_itself_but_can_remove_others(tmp_path):
    manager = GroupManager(dpc_home=tmp_path, node_id=SELF)
    group = manager.create_group("mine", "", [MEMBER])
    service = _service(manager)

    left = await service.leave_group(group.group_id)
    self_removed = await service.remove_group_member(group.group_id, SELF)

    assert left["status"] == "error" and "delete" in left["message"]
    assert self_removed["status"] == "error"
    assert manager.get_group(group.group_id).members == [SELF, MEMBER]
    assert service.p2p_manager.sent == [], "no GROUP_LEAVE went out"

    removed = await service.remove_group_member(group.group_id, MEMBER)
    assert removed["status"] == "success"
    assert manager.get_group(group.group_id).members == [SELF]


@pytest.mark.asyncio
async def test_a_member_that_is_not_the_creator_can_still_leave(manager):
    service = _service(manager)

    async def _no_fanout(group_id, message):
        return None

    service._broadcast_to_group = _no_fanout
    service.conversation_monitors = {}

    assert (await service.leave_group(GID))["status"] == "success"
    assert _held(manager) is None
