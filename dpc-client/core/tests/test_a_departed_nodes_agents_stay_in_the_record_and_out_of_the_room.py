"""A node that left the roster keeps its agents in the record and has none in the room.

REMOVING-A-MEMBER-EDITS-ONE-THIRD-OF-THE-ROSTER-AND-ITS-AGENTS-STAY-TAGGABLE.
Mike chose, 2026-11-04, not to prune `agents` / `agent_names` on removal: a prune
loses the node's registered `ext:` tag and its agents for good on remove + re-add,
because `set_node_agents` (Group Settings save on that node) is the only writer.
So the entries stay in `metadata.json` and travel in GROUP_SYNC, and every place
that SHOWS another node's agents filters them by `members`. This file holds the
backend half: the participants block every woken agent gets.

The group is a real GroupManager on disk, with the shape the live record has had
since 2026-10-31 (a departed key in both maps), not a hand-built namespace.
"""

import types

from dpc_client_core.managers.group_manager import GroupManager
from dpc_client_core.service import CoreService

OWN = "dpc-node-" + "a" * 32
GONE = "dpc-node-" + "b" * 32
STAYS = "dpc-node-" + "c" * 32

GONE_AGENTS = ["agent_ubu_acbf15fb", "ext:CC_win10"]
GONE_NAMES = {"agent_ubu_acbf15fb": "Ubu", "ext:CC_win10": "CC_win10"}
STAYS_AGENTS = ["agent_win_1", "ext:CC_c"]
STAYS_NAMES = {"agent_win_1": "Warren", "ext:CC_c": "CC_c"}
OWN_AGENTS = ["agent_001"]
OWN_NAMES = {"agent_001": "Ark"}


def _manager(home):
    return GroupManager(home, node_id=OWN)


def _live_like_group(manager):
    """Three nodes registered; then GONE is removed, as in group-b88b65076b85."""
    group = manager.create_group("DPC Project", "work", [GONE, STAYS])
    gid = group.group_id
    manager.set_node_agents(gid, OWN, list(OWN_AGENTS), dict(OWN_NAMES))
    manager.set_node_agents(gid, GONE, list(GONE_AGENTS), dict(GONE_NAMES))
    manager.set_node_agents(gid, STAYS, list(STAYS_AGENTS), dict(STAYS_NAMES))
    manager.remove_member(gid, GONE)
    return gid


def _service(node_id=OWN):
    svc = CoreService.__new__(CoreService)
    svc.peer_metadata = {GONE: {"name": "Win10 box"}, STAYS: {"name": "Mike (linux)"}}
    svc.p2p_manager = types.SimpleNamespace(
        node_id=node_id,
        get_display_name=lambda: "Mike",
        peer_cache=types.SimpleNamespace(get_peer=lambda nid: None),
    )
    return svc


def test_a_departed_nodes_agents_are_not_in_the_participants(tmp_path):
    manager = _manager(tmp_path)
    gid = _live_like_group(manager)
    labels = _service()._group_participants(manager.get_group(gid))
    assert not any("Ubu" in label or "CC_win10" in label for label in labels), labels
    assert not any("Win10 box" in label for label in labels), labels


def test_a_present_members_agents_are_in_the_participants(tmp_path):
    manager = _manager(tmp_path)
    gid = _live_like_group(manager)
    labels = _service()._group_participants(manager.get_group(gid))
    assert "Warren [agent on peer Mike (linux)]" in labels
    assert "CC_c [external agent on peer Mike (linux)]" in labels
    assert "Ark [agent on this node]" in labels


def test_the_departed_nodes_entries_stay_in_the_record_and_in_group_sync(tmp_path):
    manager = _manager(tmp_path)
    gid = _live_like_group(manager)
    group = manager.get_group(gid)
    assert GONE not in group.members
    wire = group.to_dict()
    assert wire["agents"][GONE] == GONE_AGENTS
    assert wire["agent_names"][GONE] == GONE_NAMES
    # and on disk, across a restart of the manager
    reread = _manager(tmp_path)
    reread.load_from_disk()
    assert reread.get_group(gid).agents[GONE] == GONE_AGENTS
    assert reread.get_group(gid).agent_names[GONE] == GONE_NAMES


def test_after_remove_and_re_add_the_agents_and_their_ext_tags_are_back(tmp_path):
    manager = _manager(tmp_path)
    gid = _live_like_group(manager)
    manager.add_member(gid, GONE)
    # nobody re-registered anything: set_node_agents ran for GONE only once, before the removal
    group = manager.get_group(gid)
    assert group.agents[GONE] == GONE_AGENTS
    labels = _service()._group_participants(group)
    assert "Ubu [agent on peer Win10 box]" in labels
    assert "CC_win10 [external agent on peer Win10 box]" in labels


def test_a_restarted_manager_filters_the_same_way(tmp_path):
    manager = _manager(tmp_path)
    gid = _live_like_group(manager)
    reread = _manager(tmp_path)
    reread.load_from_disk()
    labels = _service()._group_participants(reread.get_group(gid))
    assert not any("Ubu" in label or "CC_win10" in label for label in labels)
    assert "Warren [agent on peer Mike (linux)]" in labels


def test_this_node_out_of_the_roster_shows_none_of_the_agents_of_the_room(tmp_path):
    """A removed node's own copy: the roster no longer lists it, so it is no more a
    participant than any other departed node. Its own slot is still on disk for a
    re-add, which is the whole point of not pruning."""
    manager = _manager(tmp_path)
    gid = _live_like_group(manager)
    manager.remove_member(gid, OWN)
    group = manager.get_group(gid)
    assert group.agents[OWN] == OWN_AGENTS
    labels = _service()._group_participants(group)
    assert not any("Ark" in label for label in labels), labels
    assert not any("Mike (User)" in label for label in labels), labels
    assert "Warren [agent on peer Mike (linux)]" in labels
