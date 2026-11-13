"""A GROUP_CREATE for a group id in the deleted-groups registry creates nothing.

Team vote 2026-11-13 (GROUP-CREATE-FABRICATES-GROUP-ROSTER-AND-HISTORY, the
2026-11-13 bullet): `is_group_deleted` had no caller, so after the creator
deleted a group any peer could bring it back with one invitation, roster and
history exchange included. Refused regardless of sender, the creator too.
"""

import logging

import pytest

from dpc_client_core.managers.group_manager import GroupManager
from dpc_client_core.message_handlers.group_handler import (
    GroupCreateHandler,
    GroupDeleteHandler,
)
from tests.test_group_roster_and_history_gate import MEMBER, OUTSIDER, SELF
from tests.test_group_roster_and_history_gate import _Service as _GateService

HANDLER_LOGGER = "dpc_client_core.message_handlers.GroupCreateHandler"


class _Service(_GateService):
    peer_metadata = {}  # the invite event reads the creator's display name


def _invitation(group_id, creator, members):
    return {"group_id": group_id, "name": "work", "topic": "", "created_by": creator,
            "members": members, "agents": {}, "agent_names": {}, "version": 7}


@pytest.fixture
def received(tmp_path):
    """A group MEMBER created and invited this node to, then deleted."""
    manager = GroupManager(dpc_home=tmp_path, node_id=SELF)
    service = _Service(manager)
    return manager, service, tmp_path


@pytest.mark.asyncio
@pytest.mark.parametrize("sender", [MEMBER, OUTSIDER])
async def test_after_the_creators_delete_no_invitation_brings_the_group_back(received, sender, caplog):
    manager, service, home = received
    gid = "group-0123456789ab"
    await GroupCreateHandler(service).handle(MEMBER, _invitation(gid, MEMBER, [MEMBER, SELF]))
    assert manager.get_group(gid) is not None
    await GroupDeleteHandler(service).handle(MEMBER, {"group_id": gid})
    assert manager.is_group_deleted(gid)
    service.local_api.events.clear()
    service.p2p_manager.sent.clear()
    caplog.set_level(logging.WARNING, logger=HANDLER_LOGGER)

    await GroupCreateHandler(service).handle(sender, _invitation(gid, MEMBER, [MEMBER, SELF, sender]))

    assert manager.get_group(gid) is None
    assert not list((home / "conversations").glob(f"{gid}*"))
    assert service.local_api.events == []
    assert service.p2p_manager.sent == [], "no history exchange is opened either"
    lines = [r.getMessage() for r in caplog.records if "deleted-groups registry" in r.getMessage()]
    assert len(lines) == 1 and gid in lines[0] and sender[:20] in lines[0]


@pytest.mark.asyncio
async def test_a_group_this_node_deleted_as_creator_is_refused_too(received):
    manager, service, home = received
    group = manager.create_group("mine", "", [MEMBER])
    assert manager.delete_group(group.group_id, SELF)

    await GroupCreateHandler(service).handle(
        MEMBER, _invitation(group.group_id, SELF, [SELF, MEMBER]))

    assert manager.get_group(group.group_id) is None
    assert service.local_api.events == []


@pytest.mark.asyncio
async def test_an_invitation_to_a_group_never_deleted_still_creates_it(received):
    manager, service, _ = received

    await GroupCreateHandler(service).handle(
        MEMBER, _invitation("group-ba9876543210", MEMBER, [MEMBER, SELF]))

    assert manager.get_group("group-ba9876543210") is not None
    assert [name for name, _ in service.local_api.events] == ["group_invite_received"]
