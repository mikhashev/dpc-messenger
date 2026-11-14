"""The group_invite_received event carries the whole group record.

A-GROUP-INVITE-EVENT-CARRIES-NO-AGENTS-AND-AN-OFFLINE-ADD-NEVER-ARRIVES: the
event listed members only, so a freshly added node's @-list showed no agents
until a reload, though metadata.json held them.
"""

import pytest

from dpc_client_core.managers.group_manager import GroupManager
from dpc_client_core.message_handlers.group_handler import GroupCreateHandler, GroupLeaveHandler
from tests.test_group_roster_and_history_gate import MEMBER, SELF
from tests.test_group_roster_and_history_gate import _Service as _GateService


class _Service(_GateService):
    def __init__(self, group_manager):
        super().__init__(group_manager)
        self.peer_metadata = {MEMBER: {"name": "Mike (linux)"}}


@pytest.mark.asyncio
async def test_the_invite_event_carries_the_group_record_and_the_creators_name(tmp_path):
    manager = GroupManager(dpc_home=tmp_path, node_id=SELF)
    service = _Service(manager)
    gid = "group-0123456789ab"
    agents = {MEMBER: ["agent_001", "ext:CC_linux"]}
    agent_names = {MEMBER: {"agent_001": "Ark", "ext:CC_linux": "CC_linux"}}

    await GroupCreateHandler(service).handle(MEMBER, {
        "group_id": gid, "name": "work", "topic": "", "created_by": MEMBER,
        "members": [MEMBER, SELF], "agents": agents, "agent_names": agent_names,
        "version": 22,
    })

    [(name, payload)] = service.local_api.events
    assert name == "group_invite_received"
    assert payload["agents"] == agents
    assert payload["agent_names"] == agent_names
    assert payload["version"] == 22
    assert payload["members"] == [MEMBER, SELF]
    assert payload["creator_name"] == "Mike (linux)"
    # Same fields get_groups gives the UI, plus the creator's name.
    assert set(payload) == set(manager.get_group(gid).to_dict()) | {"creator_name"}


# The creator's name comes through CoreService._peer_name, as the settings panel
# reads it: live peer metadata, then the peer cache on disk, then the id stub.
OTHER = "dpc-node-0ther00000000000000000000000000"


def _with_peer_name(manager, peer_metadata, cached):
    import types

    from dpc_client_core.service import CoreService

    service = _GateService(manager)
    service.peer_metadata = peer_metadata
    service.p2p_manager.peer_cache = types.SimpleNamespace(
        get_peer=lambda nid: (types.SimpleNamespace(display_name=cached[nid])
                              if nid in cached else None))
    service._peer_name = types.MethodType(CoreService._peer_name, service)
    return service


def _create(gid, version, created_by=MEMBER):
    return {
        "group_id": gid, "name": "work", "topic": "", "created_by": created_by,
        "members": [MEMBER, SELF], "agents": {}, "agent_names": {}, "version": version,
    }


@pytest.mark.asyncio
async def test_after_a_restart_the_creator_is_named_from_the_peer_cache(tmp_path):
    manager = GroupManager(dpc_home=tmp_path, node_id=SELF)
    service = _with_peer_name(manager, peer_metadata={}, cached={MEMBER: "Mike (linux)"})

    await GroupCreateHandler(service).handle(MEMBER, _create("group-0123456789ab", 22))

    [(_, payload)] = service.local_api.events
    assert payload["creator_name"] == "Mike (linux)"


@pytest.mark.asyncio
async def test_the_name_is_the_creators_not_the_senders(tmp_path):
    manager = GroupManager(dpc_home=tmp_path, node_id=SELF)
    service = _with_peer_name(manager, peer_metadata={}, cached={
        MEMBER: "Mike (linux)", OTHER: "someone else"})
    gid = "group-0123456789ab"
    await GroupCreateHandler(service).handle(MEMBER, _create(gid, 22))
    service.local_api.events.clear()

    # A later record of the same group, relayed by another node, still names its creator.
    await GroupCreateHandler(service).handle(OTHER, _create(gid, 23))

    invites = [p for name, p in service.local_api.events if name == "group_invite_received"]
    assert invites, "the relayed record was not applied, so this test proves nothing"
    assert all(p["creator_name"] == "Mike (linux)" for p in invites)


@pytest.mark.asyncio
async def test_a_member_who_left_is_named_from_the_peer_cache_after_a_restart(tmp_path):
    """group_member_left names the node the way the invite names the creator."""
    manager = GroupManager(dpc_home=tmp_path, node_id=SELF)
    service = _with_peer_name(manager, peer_metadata={}, cached={OTHER: "Ark (windows)"})
    gid = "group-0123456789ab"
    await GroupCreateHandler(service).handle(MEMBER, {
        **_create(gid, 22), "members": [MEMBER, SELF, OTHER]})
    service.local_api.events.clear()

    await GroupLeaveHandler(service).handle(OTHER, {"group_id": gid})

    [(name, payload)] = service.local_api.events
    assert name == "group_member_left"
    assert payload["member_name"] == "Ark (windows)"
    assert payload["version"] == manager.get_group(gid).version
