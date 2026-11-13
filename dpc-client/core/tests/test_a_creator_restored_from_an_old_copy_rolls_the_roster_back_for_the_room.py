"""A creator restored from an old copy rolls the roster back for the whole room.

Rule C (the creator's roster applied over a newer local copy, step 5 of the
GROUP_SYNC procedure) was voted with one condition from Johnny and Ark: measure
the room, not a pair. These tests run three nodes through the on-connect
exchange and assert what the code does, including the costs.

The creator C is restored from an old copy: v10, with X on the roster. The
members M and N hold v22, from which C had removed X. C meets M, then N, then M
and N meet each other.
"""

import copy

import pytest

from dpc_client_core.managers.group_manager import GroupManager, GroupMetadata

C = "dpc-node-" + "c" * 32
M = "dpc-node-" + "d" * 32
N = "dpc-node-" + "a" * 32
X = "dpc-node-" + "e" * 32
GID = "group-0123456789ab"


def _room(tmp_path, created_by=C):
    old = GroupMetadata(group_id=GID, name="work", topic="t10", created_by=created_by,
                        members=[C, M, N, X], version=10,
                        agents={M: ["m_old"]}, agent_names={M: {"m_old": "Old"}}).to_dict()
    new = GroupMetadata(group_id=GID, name="work", topic="t22", created_by=created_by,
                        members=[C, M, N], version=22,
                        agents={M: ["m22"], N: ["n22"]},
                        agent_names={M: {"m22": "M"}, N: {"n22": "N"}}).to_dict()
    room = {}
    for node_id, record in ((C, old), (M, new), (N, new)):
        manager = GroupManager(dpc_home=tmp_path / node_id[-4:], node_id=node_id)
        assert manager.apply_sync(copy.deepcopy(record), created_by or node_id) is not None
        room[node_id] = manager
    return room


def _exchange(room, a, b):
    """One on-connect round: each side sends the copy it holds, then each applies."""
    to_a, to_b = room[b].get_group(GID).to_dict(), room[a].get_group(GID).to_dict()
    room[a].apply_sync(to_a, b)
    room[b].apply_sync(to_b, a)


def _held(room, node_id):
    return room[node_id].get_group(GID)


def _versions(room):
    return [_held(room, n).version for n in (C, M, N)]


def _hashes(room):
    return {GroupManager._content_hash(_held(room, n)) for n in (C, M, N)}


@pytest.mark.parametrize("n_changes_agents", [False, True], ids=["quiet", "N-changes-agents"])
def test_the_restored_roster_reaches_everyone_and_the_room_converges(tmp_path, n_changes_agents):
    """Known cost, asserted rather than hidden: the creator's copy is the only
    source of the roster, so its rollback puts X back on every copy, at the
    freshest version in the room, and no member can undo it; only the creator
    removing X again does. What the rollback does not take back is the rest of
    the record: C takes the room's topic and agents with the next higher record,
    and in this order a member's concurrent agents change survives."""
    room = _room(tmp_path)

    _exchange(room, C, M)
    assert X in _held(room, C).members, "C never takes a member's roster"
    assert X in _held(room, M).members, "M takes the creator's, over its newer copy"
    assert _versions(room)[:2] == [22, 23]
    if n_changes_agents:
        room[N].set_node_agents(GID, N, ["n_new"], {"n_new": "New"})
    _exchange(room, C, N)
    _exchange(room, M, N)

    members = [sorted(_held(room, n).members) for n in (C, M, N)]
    assert members == [sorted([C, M, N, X])] * 3
    n_agents = ["n_new"] if n_changes_agents else ["n22"]
    for node_id in (C, M, N):
        held = _held(room, node_id)
        assert held.topic == "t22", "the restored copy's old topic is not brought back"
        assert held.agents == {M: ["m22"], N: n_agents}
    # The creator ends one version behind after this order of meetings...
    assert _versions(room) == ([23, 24, 24] if n_changes_agents else [22, 23, 23])

    _exchange(room, C, M)
    _exchange(room, C, N)
    # ...and level with the room after one more contact.
    assert len(set(_versions(room))) == 1 and len(_hashes(room)) == 1


def _members_meet_before_the_creator_reaches_n(tmp_path, agent_id):
    room = _room(tmp_path)
    _exchange(room, C, M)                                  # M: v23, X back
    room[N].set_node_agents(GID, N, [agent_id], {})        # N: v23, no X, its own change
    _exchange(room, M, N)                                  # equal versions, two rosters
    _exchange(room, C, N)
    for _ in range(3):
        for a, b in ((C, M), (M, N), (C, N)):
            _exchange(room, a, b)
    return room


def test_members_meeting_at_equal_versions_can_lose_a_members_own_agents_change(tmp_path):
    """Known cost, the fourth (ADR-038): at equal versions the tie-break takes a
    whole record, so a member's concurrent agents change can be lost, here by
    the very node that made it. When M (v23, the creator's roster with X) meets
    N (v23, its own agents change, the roster without X), each side first puts
    its own roster into the offered record and then compares hashes; the two
    sides compare different records, so both can take the other's, and N can
    end with M's old entry for N. Which way it goes is decided by the hashes,
    so the test walks candidate agent ids: the room converges every time, on
    one roster with X, but N's change is kept for some ids and lost for others.
    Convergence is not lost; the change is."""
    kept = lost = 0
    for i in range(32):
        agent_id = f"n_new_{i}"
        room = _members_meet_before_the_creator_reaches_n(tmp_path / str(i), agent_id)
        assert [sorted(_held(room, n).members) for n in (C, M, N)] == [sorted([C, M, N, X])] * 3
        assert len(_hashes(room)) == 1 and len(set(_versions(room))) == 1
        n_entry = _held(room, N).agents[N]
        assert n_entry in (["n22"], [agent_id])
        if n_entry == [agent_id]:
            kept += 1
        else:
            lost += 1
    assert kept and lost, (kept, lost)


@pytest.mark.parametrize("sender", [C, M, "", None], ids=["C", "M", "empty", "unnamed"])
def test_rule_c_never_fires_for_a_group_held_with_no_creator(tmp_path, sender):
    """An empty `created_by` matches no sender, so an older record never moves
    `members` onto a newer copy there, whoever sends it."""
    room = _room(tmp_path, created_by="")
    old = GroupMetadata(group_id=GID, name="work", topic="t10", created_by="",
                        members=[C, M, N, X], version=10).to_dict()

    assert room[M].apply_sync(old, sender) is None

    held = _held(room, M)
    assert sorted(held.members) == sorted([C, M, N])
    assert held.version == 22 and held.topic == "t22"
