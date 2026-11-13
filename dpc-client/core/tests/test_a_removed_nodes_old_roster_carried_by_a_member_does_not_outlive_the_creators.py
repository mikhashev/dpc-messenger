"""A removed node's old roster, carried by a third member, does not outlive the creator's.

Ark's three-hop case (review of ADR-038 part 1, 2026-11-13). The creator removes
X at v21. X was offline and so was M, so both still hold v20 with X on it. X
makes a change of its own while away (its agents: a field any member may move),
which raises its copy past v20, and syncs it to M; M takes it, X's roster
included, because that roster is the one M already holds. Then M and the creator
meet: our v21 without X against M's copy with X.

Two outcomes are required: the creator never takes X back from M (step 2 of the
procedure, `_roster_from_its_owner`), and M takes the creator's roster at once.

The second failed before this file, whenever X's own changes had carried M's
copy to a *higher* version than the creator's: M's version rule ("local v22 >
remote v21, keep") ran before any question of who owns `members`, so M kept X
until the creator happened to send again at a version M had not passed. The
creator's roster now prevails at any version: from the creator, `members` is
applied onto M's copy even when that copy is newer (only `members`: the other
fields still obey the version), and M raises its version so the next sync
carries the result. The creator is the only node that changes `members`, so its
copy holds the newest roster whatever its version number says.

At equal versions (the first case) nothing changed: the creator's roster already
won the tie outright. That tie still takes one whole record, so X's agents change
can be lost there, as any concurrent change at one version can; this file asserts
the roster, not that field.
"""

import copy

import pytest

from dpc_client_core.managers.group_manager import GroupManager, GroupMetadata

CREATOR = "dpc-node-" + "c" * 32
M = "dpc-node-" + "d" * 32
X = "dpc-node-" + "e" * 32
GID = "group-0123456789ab"


def _nodes(tmp_path):
    base = GroupMetadata(group_id=GID, name="work", topic="", created_by=CREATOR,
                         members=[CREATOR, M, X], version=20).to_dict()
    nodes = {}
    for node_id in (CREATOR, M, X):
        manager = GroupManager(dpc_home=tmp_path / node_id[-4:], node_id=node_id)
        assert manager.apply_sync(copy.deepcopy(base), CREATOR) is not None
        nodes[node_id] = manager
    return nodes


def _members(manager):
    return sorted(manager.get_group(GID).members)


def _exchange(c, m):
    """One on-connect round: each side sends the copy it holds, then each applies."""
    to_creator = m.get_group(GID).to_dict()
    to_member = c.get_group(GID).to_dict()
    c.apply_sync(to_creator, M)
    m.apply_sync(to_member, CREATOR)


@pytest.mark.parametrize("x_changes", [1, 2, 4], ids=["M-equal-v21", "M-ahead-v22", "M-ahead-v24"])
def test_our_v21_without_x_against_ms_copy_with_x(tmp_path, x_changes):
    nodes = _nodes(tmp_path)
    c, m, x = nodes[CREATOR], nodes[M], nodes[X]

    assert c.remove_member(GID, X, CREATOR).version == 21  # M and X are offline
    for i in range(x_changes):
        x.set_node_agents(GID, X, [f"agent_{i}"], {f"agent_{i}": f"Agent {i}"})
    assert m.apply_sync(x.get_group(GID).to_dict(), X) is not None
    assert X in m.get_group(GID).members, "M took X's record: its roster is M's own"
    assert m.get_group(GID).version == 20 + x_changes

    _exchange(c, m)

    assert X not in c.get_group(GID).members, "the creator never takes X back"
    assert X not in m.get_group(GID).members, "M takes the creator's roster at once"

    _exchange(c, m)

    held_c, held_m = c.get_group(GID), m.get_group(GID)
    assert _members(c) == _members(m) == sorted([CREATOR, M])
    assert GroupManager._content_hash(held_c) == GroupManager._content_hash(held_m)
    assert held_c.version == held_m.version


def test_an_older_record_from_the_creator_moves_only_members(tmp_path):
    """Per-field: the creator's roster at a lower version, not its other fields."""
    nodes = _nodes(tmp_path)
    c, m = nodes[CREATOR], nodes[M]
    c.remove_member(GID, X, CREATOR)                      # creator v21
    m.set_node_agents(GID, M, ["mine"], {"mine": "Mine"})  # M v21
    m.set_node_agents(GID, M, ["mine2"], {"mine2": "Mine"})  # M v22
    stale_topic = dict(c.get_group(GID).to_dict(), topic="older topic")

    applied = m.apply_sync(stale_topic, CREATOR)

    assert applied is not None
    held = m.get_group(GID)
    assert sorted(held.members) == sorted([CREATOR, M])
    assert held.topic == "" and held.agents[M] == ["mine2"]
    assert held.version == 23


def test_an_older_record_from_a_member_is_still_ignored(tmp_path):
    nodes = _nodes(tmp_path)
    c, m = nodes[CREATOR], nodes[M]
    c.remove_member(GID, X, CREATOR)
    c.update_topic(GID, "v22")
    old = dict(m.get_group(GID).to_dict(), members=[CREATOR, M, X, "dpc-node-phantom"])

    assert c.apply_sync(old, M) is None
    assert _members(c) == sorted([CREATOR, M])
