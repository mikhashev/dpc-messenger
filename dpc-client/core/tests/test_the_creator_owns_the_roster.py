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
    """Johnny: prove the act, not the status. The real fan-out runs, so the
    GROUP_LEAVE that reaches each connected member is what is asserted."""
    service = _service(manager)
    service.conversation_monitors = {}
    service.p2p_manager.peers = {CREATOR: object(), MEMBER: object()}

    assert (await service.leave_group(GID))["status"] == "success"

    leave = {"command": "GROUP_LEAVE", "payload": {"group_id": GID}}
    assert service.p2p_manager.sent == [(CREATOR, leave), (MEMBER, leave)]
    assert _held(manager) is None


# --- one owner of the rule (Johnny) ----------------------------------------

@pytest.mark.asyncio
async def test_a_member_removing_itself_gets_the_managers_answer_and_leaves(manager):
    """The service used to refuse what the manager allows. It now asks the
    manager, and a member's own removal goes out as GROUP_LEAVE, the frame the
    other members apply (a GROUP_SYNC dropping it would be applied by nobody)."""
    service = _service(manager)
    service.conversation_monitors = {}
    service.p2p_manager.peers = {CREATOR: object(), MEMBER: object()}
    assert GroupManager.roster_change_refusal(_held(manager), SELF, SELF, True) is None

    result = await service.remove_group_member(GID, SELF)

    assert result["status"] == "success"
    leave = {"command": "GROUP_LEAVE", "payload": {"group_id": GID}}
    assert service.p2p_manager.sent == [(CREATOR, leave), (MEMBER, leave)]
    assert _held(manager) is None


@pytest.mark.asyncio
async def test_a_member_removing_another_is_refused_with_the_managers_reason(manager):
    service = _service(manager)
    reason = GroupManager.roster_change_refusal(_held(manager), SELF, MEMBER, True)

    result = await service.remove_group_member(GID, MEMBER)

    assert reason == f"only the creator {CREATOR} changes members"
    assert result == {"status": "error", "message": f"Cannot remove {MEMBER}: {reason}"}
    assert _held(manager).members == [CREATOR, SELF, MEMBER]
    assert service.p2p_manager.sent == []


@pytest.mark.parametrize("created_by", [CREATOR, SELF, ""], ids=["member", "creator", "no-creator"])
@pytest.mark.parametrize("target,removing", [
    (SELF, True), (MEMBER, True), ("dpc-node-newcomer", False),
], ids=["remove-self", "remove-other", "add"])
def test_the_service_refuses_exactly_when_the_manager_does(tmp_path, created_by, target, removing):
    from dpc_client_core.service import roster_refusal

    manager = GroupManager(dpc_home=tmp_path, node_id=SELF)
    manager.apply_sync(_record(created_by=created_by), created_by or MEMBER)
    group = _held(manager)

    reason = GroupManager.roster_change_refusal(group, SELF, target, removing)
    answer = roster_refusal(manager, SELF, GID, target, removing)

    assert (answer is None) == (reason is None)
    if reason:
        assert answer["message"].endswith(reason)


# --- the manager enforces it too (Ark) -------------------------------------

def test_the_manager_refuses_an_add_or_remove_asked_by_a_member(manager, caplog):
    """The service checks first for the UI's message; the module holds the rule."""
    caplog.set_level(logging.WARNING, logger=MANAGER_LOGGER)

    assert manager.add_member(GID, "dpc-node-newcomer", MEMBER) is None
    assert manager.remove_member(GID, SELF, MEMBER) is None
    assert manager.add_member(GID, "dpc-node-newcomer", SELF) is None, "nor this node"

    assert _held(manager).members == [CREATOR, SELF, MEMBER]
    assert _held(manager).version == 3
    assert len(_refusals(caplog, "only the creator")) == 3


def test_the_manager_takes_the_creators_add_and_remove(manager):
    assert manager.add_member(GID, "dpc-node-newcomer", CREATOR) is not None
    assert manager.remove_member(GID, MEMBER, CREATOR) is not None
    assert _held(manager).members == [CREATOR, SELF, "dpc-node-newcomer"]
    assert _held(manager).version == 5


def test_a_member_removing_itself_needs_no_creator_but_the_creator_cannot(manager, caplog):
    caplog.set_level(logging.WARNING, logger=MANAGER_LOGGER)

    assert manager.remove_member(GID, CREATOR, CREATOR) is None
    assert manager.remove_member(GID, MEMBER, MEMBER) is not None

    assert _held(manager).members == [CREATOR, SELF]
    (line,) = _refusals(caplog, "cannot remove itself")
    assert CREATOR in line


def test_a_group_with_no_creator_takes_no_add_or_remove_but_a_member_may_leave(tmp_path):
    manager = GroupManager(dpc_home=tmp_path, node_id=SELF)
    manager.apply_sync(_record(created_by=""), MEMBER)

    assert manager.add_member(GID, "dpc-node-newcomer", SELF) is None
    assert manager.remove_member(GID, CREATOR, SELF) is None
    assert manager.remove_member(GID, MEMBER, MEMBER) is not None
    assert _held(manager).members == [CREATOR, SELF]


@pytest.mark.asyncio
async def test_group_leave_removes_its_sender_and_never_another_node(manager):
    """GROUP_LEAVE names no node: the transport sender is the one that leaves."""
    from dpc_client_core.message_handlers.group_handler import GroupLeaveHandler

    service = _Service(manager)
    service.peer_metadata = {}

    await GroupLeaveHandler(service).handle(MEMBER, {"group_id": GID, "node_id": CREATOR})
    assert _held(manager).members == [CREATOR, SELF]
    assert [name for name, _ in service.local_api.events] == ["group_member_left"]
    # Named as the invite and the settings panel name a node nobody has named.
    assert service.local_api.events[0][1]["member_name"] == MEMBER[:16]

    service.local_api.events.clear()
    await GroupLeaveHandler(service).handle(CREATOR, {"group_id": GID})
    assert _held(manager).members == [CREATOR, SELF], "the creator cannot leave"
    assert service.local_api.events == []


@pytest.mark.asyncio
async def test_group_leave_from_a_node_off_the_roster_tells_the_ui_nothing(manager):
    from dpc_client_core.message_handlers.group_handler import GroupLeaveHandler

    service = _Service(manager)
    service.peer_metadata = {}
    stranger = "dpc-node-" + "f" * 32

    await GroupLeaveHandler(service).handle(stranger, {"group_id": GID})

    assert service.local_api.events == []
    assert _held(manager).members == [CREATOR, SELF, MEMBER]
    assert _held(manager).version == 3
