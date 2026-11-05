"""`block_peer` takes the node out of the DHT routing table and drops its stored value.

The DHT asks the firewall per packet (see tests/dht/test_rpc_block.py); this
is the other half: what the node already held when the Block came. Real
`CoreService.block_peer`, real `ContextFirewall`, real `DHTManager` (not
started, no socket); Unblock restores nothing.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.dht import DHTManager
from dpc_client_core.firewall import ContextFirewall
from dpc_client_core.managers.group_manager import GroupManager
from dpc_client_core.service import CoreService

SELF = "dpc-node-" + "a" * 32
BLOCKED = "dpc-node-" + "b" * 32
OTHER = "dpc-node-" + "c" * 32


class _Api:
    async def broadcast_event(self, name, payload):
        pass


@pytest.fixture
def node(tmp_path):
    rules = tmp_path / "privacy_rules.json"
    rules.write_text(json.dumps({}))
    dht = DHTManager(node_id=SELF, ip="192.0.2.1", port=8888)
    dht.routing_table.add_node(BLOCKED, "198.51.100.2", 8889)
    dht.routing_table.add_node(OTHER, "198.51.100.3", 8889)
    dht.rpc_handler._storage[BLOCKED] = "198.51.100.2:8888"
    dht.rpc_handler._storage[OTHER] = "198.51.100.3:8888"
    dht.rpc_handler._storage["cert:" + BLOCKED] = "PEM"
    dht.rpc_handler._storage["cert:" + OTHER] = "PEM2"

    service = CoreService.__new__(CoreService)
    service.firewall = ContextFirewall(rules)
    service.group_manager = GroupManager(tmp_path / "home", SELF)
    service.p2p_manager = SimpleNamespace(node_id=SELF, peers={}, dht_manager=dht)
    service.local_api = _Api()
    service.file_transfer_manager = SimpleNamespace(active_transfers={})
    service.llm_manager = SimpleNamespace(providers={})
    service._notify_peers_of_provider_changes = AsyncMock()
    service._user_disconnected_peers = set()
    service._reconnect_tasks = {}
    service._reconnect_slow_phase = set()
    service.p2p_coordinator = SimpleNamespace(disconnect=AsyncMock())
    return SimpleNamespace(service=service, dht=dht)


@pytest.mark.asyncio
async def test_block_removes_the_node_from_the_routing_table_and_drops_its_value(node):
    result = await node.service.block_peer(BLOCKED)

    assert result["status"] == "success"
    assert node.dht.routing_table.get_node(BLOCKED) is None
    assert BLOCKED not in node.dht.rpc_handler._storage
    assert "cert:" + BLOCKED not in node.dht.rpc_handler._storage


@pytest.mark.asyncio
async def test_block_leaves_every_other_entry_and_unblock_restores_nothing(node):
    await node.service.block_peer(BLOCKED)

    assert node.dht.routing_table.get_node(OTHER) is not None
    assert node.dht.rpc_handler._storage[OTHER] == "198.51.100.3:8888"
    assert node.dht.rpc_handler._storage["cert:" + OTHER] == "PEM2"

    await node.service.unblock_peer(BLOCKED)

    assert node.dht.routing_table.get_node(BLOCKED) is None
    assert BLOCKED not in node.dht.rpc_handler._storage


@pytest.mark.asyncio
async def test_a_node_with_the_dht_off_is_blocked_all_the_same(node):
    node.service.p2p_manager.dht_manager = None

    result = await node.service.block_peer(BLOCKED)

    assert result["status"] == "success"


def test_the_service_builds_the_dht_with_the_question_wired_to_the_firewall():
    """A source guard: start_server binds real ports, so it is not run here.
    The callable must read `self.firewall` when asked, not capture it at start."""
    import inspect
    from dpc_client_core.p2p_manager import P2PManager

    source = inspect.getsource(P2PManager.start_server)
    built = source[source.index("DHTManager("):]
    built = built[:built.index("await self.dht_manager.start")]
    assert 'is_blocked=lambda nid: is_blocked_by(getattr(self, "firewall", None), nid)' in built
