"""A record for a group this node does not hold is taken only from the creator it names.

Team vote 2026-11-13 (3 of 4, follow-up to ADR-038 part 1). The record that
creates our copy also fixes the `created_by` pin, so before this an outsider
could enrol us in a group "Alice created", with a roster of its choosing, and
every later record from Alice would be judged against that. Now a record for an
unheld group whose non-empty `created_by` is not its transport sender is
refused, with one WARNING naming both. An empty `created_by` keeps its meaning,
a group with no creator (Zcode's condition), and is not refused.
"""

import logging

import pytest

from dpc_client_core.managers.group_manager import GroupManager
from dpc_client_core.message_handlers.group_handler import GroupCreateHandler
from tests.test_group_roster_and_history_gate import MEMBER, OUTSIDER, SELF
from tests.test_a_deleted_group_is_not_created_again_by_an_invitation import _Service

MANAGER_LOGGER = "dpc_client_core.managers.group_manager"
GID = "group-0a1b2c3d4e5f"
ALICE = MEMBER


def _invitation(creator, members, version=1):
    return {"group_id": GID, "name": "work", "topic": "", "created_by": creator,
            "members": members, "agents": {}, "agent_names": {}, "version": version}


@pytest.fixture
def node(tmp_path):
    manager = GroupManager(dpc_home=tmp_path, node_id=SELF)
    return manager, _Service(manager), tmp_path


def _first_sight_refusals(caplog):
    return [r.getMessage() for r in caplog.records
            if "does not hold the group" in r.getMessage()]


@pytest.mark.asyncio
async def test_an_outsider_inviting_us_into_a_group_it_says_alice_created_creates_nothing(node, caplog):
    manager, service, home = node
    caplog.set_level(logging.WARNING, logger=MANAGER_LOGGER)

    await GroupCreateHandler(service).handle(
        OUTSIDER, _invitation(ALICE, [ALICE, SELF, OUTSIDER]))

    assert manager.get_group(GID) is None
    assert not list((home / "conversations").glob(f"{GID}*")), "no folder either"
    assert service.local_api.events == [], "nothing announced"
    assert service.p2p_manager.sent == [], "no history exchange opened"
    (line,) = _first_sight_refusals(caplog)
    assert ALICE in line and OUTSIDER in line


@pytest.mark.asyncio
async def test_alice_inviting_us_into_her_group_creates_it(node, caplog):
    manager, service, _ = node
    caplog.set_level(logging.WARNING, logger=MANAGER_LOGGER)

    await GroupCreateHandler(service).handle(ALICE, _invitation(ALICE, [ALICE, SELF]))

    assert manager.get_group(GID).created_by == ALICE
    assert [name for name, _ in service.local_api.events] == ["group_invite_received"]
    assert _first_sight_refusals(caplog) == []


@pytest.mark.asyncio
async def test_after_the_outsiders_refusal_alices_own_invitation_still_works(node):
    """The refusal stores nothing, so it pins nothing that Alice would then fail."""
    manager, service, _ = node

    await GroupCreateHandler(service).handle(
        OUTSIDER, _invitation(ALICE, [ALICE, SELF, OUTSIDER], version=9))
    await GroupCreateHandler(service).handle(ALICE, _invitation(ALICE, [ALICE, SELF]))

    held = manager.get_group(GID)
    assert held.members == [ALICE, SELF] and held.version == 1


def test_an_empty_creator_keeps_its_no_creator_meaning(node, caplog):
    """Zcode's condition: not refused at first sight; its roster is frozen by the pin."""
    manager, _, _ = node
    caplog.set_level(logging.WARNING, logger=MANAGER_LOGGER)

    assert manager.apply_sync(_invitation("", [ALICE, SELF]), OUTSIDER) is not None

    assert manager.get_group(GID).created_by == ""
    assert _first_sight_refusals(caplog) == []


def test_an_unnamed_sender_is_not_the_creator_at_first_sight(node):
    manager, _, _ = node

    assert manager.apply_sync(_invitation(ALICE, [ALICE, SELF])) is None
    assert manager.get_group(GID) is None
