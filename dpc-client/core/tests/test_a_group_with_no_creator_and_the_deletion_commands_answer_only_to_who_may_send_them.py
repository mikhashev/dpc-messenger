"""The group commands that remove or rewrite a group answer only to a sender who may send them.

Follow-up 11 to 10a, from Ark's review (#24, items 4 and 5):

* A group held here with an empty `created_by` (a copy written before the field
  existed) has no creator, so `GROUP_CREATE` for it is ignored from every
  sender, as `_creator_pin_holds` freezes its roster. That is deliberate and
  this pins it, with what it costs: nothing in the code hands such a group back
  to a member that lost it.
* `GROUP_DELETED_STATUS` (the offline form of `GROUP_DELETE`) had no test at
  all. Like `GROUP_DELETE` it removes a held group, and only for the node the
  copy names as its creator.

The membership audit of the other three (GROUP_LEAVE, GROUP_DELETE,
GROUP_HISTORY_REQUEST) is pinned in `test_the_creator_owns_the_roster`,
`test_group_roster_and_history_gate` and what they import.
"""

import pytest

from dpc_client_core.managers.group_manager import GroupManager
from dpc_client_core.message_handlers.group_handler import (
    GroupCreateHandler,
    GroupDeletedStatusHandler,
    GroupDeleteHandler,
    GroupSyncHandler,
)
from tests.test_group_roster_and_history_gate import MEMBER, OUTSIDER, SELF
from tests.test_group_roster_and_history_gate import _Service as _GateService

GID = "group-0123456789ab"
OTHER_MEMBER = "dpc-node-" + "c" * 32


class _Service(_GateService):
    peer_metadata = {}  # the invite event reads the creator's display name


def _record(creator, **over):
    record = {"group_id": GID, "name": "work", "topic": "the held topic", "created_by": creator,
              "members": [MEMBER, SELF, OTHER_MEMBER], "agents": {OTHER_MEMBER: ["agent_001"]},
              "agent_names": {}, "version": 5}
    record.update(over)
    return record


@pytest.fixture
def no_creator(tmp_path):
    """A group held here whose copy names no creator, as one written before the field."""
    manager = GroupManager(dpc_home=tmp_path, node_id=SELF)
    assert manager.apply_sync(_record(""), MEMBER) is not None
    assert manager.get_group(GID).created_by == ""
    return manager, _Service(manager)


# --- GROUP_CREATE for a held group with no creator ----------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("sender", [MEMBER, OTHER_MEMBER, OUTSIDER, SELF],
                         ids=["member", "other-member", "outsider", "this-node"])
async def test_a_group_create_for_a_held_group_with_no_creator_is_ignored_from_everyone(
        no_creator, sender, caplog):
    """Nobody is its creator, so nobody rewrites it; its roster, topic and version stay."""
    manager, service = no_creator

    await GroupCreateHandler(service).handle(sender, _record(
        "", version=50, topic="rewritten", members=[sender], agents={sender: ["forged"]}))

    kept = manager.get_group(GID)
    assert (kept.members, kept.topic, kept.version) == (
        [MEMBER, SELF, OTHER_MEMBER], "the held topic", 5)
    assert kept.agents == {OTHER_MEMBER: ["agent_001"]}
    assert not service.local_api.events
    assert not service.p2p_manager.sent, "no history exchange is opened either"
    assert any("only its creator" in r.getMessage() and "(none)" in r.getMessage()
               for r in caplog.records)


@pytest.mark.asyncio
async def test_a_group_create_naming_a_creator_for_a_held_group_with_no_creator_is_ignored(no_creator):
    """The record cannot name one either: the pin is the held value, empty included."""
    manager, service = no_creator

    await GroupCreateHandler(service).handle(MEMBER, _record(MEMBER, version=50))

    assert manager.get_group(GID).created_by == ""
    assert manager.get_group(GID).version == 5
    assert not service.local_api.events


@pytest.mark.asyncio
async def test_a_member_that_lost_a_group_with_no_creator_is_not_given_it_back_by_the_other_doors(
        tmp_path):
    """The cost of the freeze, pinned: a node that no longer holds such a group gets
    it back from neither GROUP_SYNC nor a delete-status, and nothing in the code sends
    the GROUP_CREATE that would be taken as first sight."""
    manager = GroupManager(dpc_home=tmp_path, node_id=SELF)
    service = _Service(manager)

    await GroupSyncHandler(service).handle(MEMBER, _record(""))
    await GroupDeletedStatusHandler(service).handle(MEMBER, {"deleted_groups": [GID]})

    assert manager.get_group(GID) is None
    assert not service.local_api.events


@pytest.mark.parametrize("requester", [MEMBER, SELF, OUTSIDER])
def test_nobody_can_add_to_a_group_with_no_creator_so_nothing_sends_its_group_create(requester):
    """Why nothing sends GROUP_CREATE for a group with no creator: `add_group_member`
    is the only sender besides group creation, and the manager refuses it."""
    group = type("G", (), {"created_by": ""})()

    reason = GroupManager.roster_change_refusal(group, requester, OTHER_MEMBER, removing=False)

    assert reason and "no creator" in reason


# --- GROUP_DELETED_STATUS ------------------------------------------------------


@pytest.fixture
def held(tmp_path):
    """A group MEMBER created, held here, with this node and OTHER_MEMBER on it."""
    manager = GroupManager(dpc_home=tmp_path, node_id=SELF)
    assert manager.apply_sync(_record(MEMBER), MEMBER) is not None
    return manager, _Service(manager)


@pytest.mark.asyncio
@pytest.mark.parametrize("sender", [OTHER_MEMBER, OUTSIDER, SELF],
                         ids=["member", "outsider", "this-node"])
async def test_a_delete_status_from_a_node_that_is_not_the_creator_removes_nothing(held, sender):
    manager, service = held
    service.conversation_monitors[GID] = object()

    await GroupDeletedStatusHandler(service).handle(sender, {"deleted_groups": [GID]})

    assert manager.get_group(GID) is not None
    assert not manager.is_group_deleted(GID)
    assert GID in service.conversation_monitors
    assert not service.local_api.events


@pytest.mark.asyncio
async def test_a_delete_status_from_the_creator_removes_the_group_and_announces_it(held):
    """Control: the offline form of the creator's GROUP_DELETE still works."""
    manager, service = held
    service.conversation_monitors[GID] = object()

    await GroupDeletedStatusHandler(service).handle(MEMBER, {"deleted_groups": ["group-000000000000", GID]})

    assert manager.get_group(GID) is None
    assert manager.is_group_deleted(GID)
    assert GID not in service.conversation_monitors
    assert [(n, p["group_id"], p["deleted_by"]) for n, p in service.local_api.events] == [
        ("group_deleted", GID, MEMBER)]


@pytest.mark.asyncio
@pytest.mark.parametrize("handler,payload", [
    (GroupDeletedStatusHandler, {"deleted_groups": [GID]}),
    (GroupDeleteHandler, {"group_id": GID}),
], ids=["delete-status", "delete"])
async def test_a_deletion_of_a_group_with_no_creator_is_taken_from_nobody(no_creator, handler, payload):
    """No sender equals an empty `created_by`, so the copy stays and says nothing."""
    manager, service = no_creator

    for sender in (MEMBER, OTHER_MEMBER, OUTSIDER, SELF):
        await handler(service).handle(sender, payload)

    assert manager.get_group(GID) is not None
    assert not manager.is_group_deleted(GID)
    assert not service.local_api.events
