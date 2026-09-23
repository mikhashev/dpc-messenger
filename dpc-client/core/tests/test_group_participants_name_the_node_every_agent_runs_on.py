"""A group's participant list tells an agent which node each agent belongs to.

The list printed every agent as `Name (agent)`, dropping the node the roster keys it
by, so Johnny could not tell Ark (this node) from Ubu (the Linux node). A peer's name
came only from `peer_metadata`, which is memory: right after a restart the Linux node
read `dpc-node-6d218e9 (peer)` until it reconnected (seen by Ark, 2026-09-23). The
peer cache on disk carries the name it had last time.
"""

import types

from dpc_client_core.service import CoreService

OWN = "dpc-node-own0000000000000000000000000"
LINUX = "dpc-node-6d218e95dee9cfeebfc3caa705ae8c95"


def _service(peer_metadata=None, cached=None):
    cached = cached or {}
    svc = CoreService.__new__(CoreService)
    svc.peer_metadata = peer_metadata or {}
    svc.p2p_manager = types.SimpleNamespace(
        node_id=OWN,
        get_display_name=lambda: "Mike",
        peer_cache=types.SimpleNamespace(
            get_peer=lambda nid: (types.SimpleNamespace(display_name=cached[nid])
                                  if nid in cached else None)),
    )
    return svc


GROUP = types.SimpleNamespace(
    members=[OWN, LINUX],
    agent_names={
        OWN: {"agent_001": "Ark", "ext:CC_windows": "CC_windows"},
        LINUX: {"agent_ubu_acbf15fb": "Ubu", "ext:CC_linux": "CC_linux"},
    },
)


def test_every_agent_carries_its_owning_node():
    labels = _service(peer_metadata={LINUX: {"name": "Mike (linux)"}})._group_participants(GROUP)
    assert "Mike (User)" in labels
    assert "Mike (linux) (peer)" in labels
    assert "Ark [agent on this node]" in labels
    assert "CC_windows [external agent on this node]" in labels
    assert "Ubu [agent on peer Mike (linux)]" in labels
    assert "CC_linux [external agent on peer Mike (linux)]" in labels


def test_own_agents_and_a_peers_agents_read_differently():
    labels = _service(peer_metadata={LINUX: {"name": "Mike (linux)"}})._group_participants(GROUP)
    assert not any(label.endswith("(agent)") for label in labels)


def test_a_peer_name_survives_a_cold_peer_metadata_through_the_peer_cache():
    labels = _service(peer_metadata={}, cached={LINUX: "Mike (linux)"})._group_participants(GROUP)
    assert "Mike (linux) (peer)" in labels
    assert "Ubu [agent on peer Mike (linux)]" in labels
    assert not any("dpc-node-6d218e9 " in label for label in labels)


def test_a_peer_nobody_has_named_still_falls_back_to_its_id():
    labels = _service()._group_participants(GROUP)
    assert f"{LINUX[:16]} (peer)" in labels
