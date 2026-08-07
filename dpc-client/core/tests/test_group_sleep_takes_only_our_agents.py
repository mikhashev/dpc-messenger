"""Group Sleep used to run every node's agents, on whichever node pressed it.

The selection read the roster per node and then, if the local list came out
empty, extended it with the agents of *all* nodes. The guard in front of that
fallback was `self.node_id if hasattr(self, "node_id") else None` — and
`CoreService` has no `node_id`; it lives on `p2p_manager`. So the local list was
always empty, the fallback always ran, and both logs say so in every line:
`Group sleep: found N agents for … (node=None)`.

It is not a resource question. Sleep posts a morning brief into the group under
the agent's display name, so a foreign agent's brief went out signed with our
key — the message says one author and the signature says another.

Not latent either: the Linux node's archived log has
`2026-05-11 18:48:17` and `19:33:54` running `agent_001`, which belongs to the
Windows node. Reported latent in the group chat and corrected here.
"""

from dpc_client_core.service import CoreService

WINDOWS = "dpc-node-" + "a" * 32
LINUX = "dpc-node-" + "b" * 32
MACOS = "dpc-node-" + "c" * 32

ROSTER = {
    "agents": {
        WINDOWS: ["agent_001", "agent_warren_0e96b5cb"],
        LINUX: ["agent_ubu_acbf15fb"],
    }
}


def test_each_node_takes_its_own_agents():
    assert CoreService._local_group_agents(ROSTER, WINDOWS) == [
        "agent_001",
        "agent_warren_0e96b5cb",
    ]
    assert CoreService._local_group_agents(ROSTER, LINUX) == ["agent_ubu_acbf15fb"]


def test_a_node_with_no_agents_takes_nobody_elses():
    """The defect verbatim: macOS registered nothing, so it ran everyone."""
    assert CoreService._local_group_agents(ROSTER, MACOS) == []


def test_an_unknown_node_id_is_not_a_licence_to_take_everything():
    """What the dead guard produced — `None` as the lookup key."""
    assert CoreService._local_group_agents(ROSTER, None) == []


def test_a_group_with_no_agents_at_all_is_empty_not_everything():
    assert CoreService._local_group_agents({"agents": {}}, WINDOWS) == []
    assert CoreService._local_group_agents({}, WINDOWS) == []
    assert CoreService._local_group_agents({"agents": None}, WINDOWS) == []


def test_the_returned_list_is_ours_to_mutate():
    """The old code called `.extend()` on whatever the roster handed back, which
    edited the parsed metadata in place."""
    roster = {"agents": {WINDOWS: ["agent_001"]}}
    got = CoreService._local_group_agents(roster, WINDOWS)
    got.append("agent_intruder")
    assert roster["agents"][WINDOWS] == ["agent_001"]
