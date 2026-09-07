"""A node that does not know its own addresses calls itself, and says nothing.

Two consumers of one set: a DHT seed list naming this host's exits, and a peer
cache holding a peer's NAT address that forwards back here. The address is a
guess and only warns; the node id in a reply is proof and decides.
"""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from dpc_client_core.own_addresses import OwnAddresses
from dpc_client_core.dht.manager import DHTManager
from dpc_client_core.dht.rpc import DHTRPCHandler

ME = "dpc-node-" + "a" * 32
PEER = "dpc-node-" + "b" * 32
MINE_A = "38.180.192.51"
MINE_B = "5.136.122.100"
STRANGER = "203.0.113.9"


# --------------------------------------------------------------------------
# The set itself
# --------------------------------------------------------------------------

def test_an_address_learned_is_recognised():
    own = OwnAddresses()
    own.learn(MINE_A, "stun")

    assert own.contains(MINE_A)
    assert MINE_A in own
    assert not own.contains(STRANGER)


def test_the_unspecified_addresses_are_never_learned():
    """0.0.0.0 would make every comparison true."""
    own = OwnAddresses()
    own.learn_all(["0.0.0.0", "::", "", None and ""])

    assert own.known() == set()
    assert not own.contains("0.0.0.0")


def test_nothing_is_ours_until_it_is_learned():
    assert not OwnAddresses().contains(MINE_A)


def test_the_self_seeds_are_the_ones_whose_address_is_ours():
    own = OwnAddresses()
    own.learn_all([MINE_A, MINE_B])

    seeds = [(MINE_A, 8889), (STRANGER, 8889), (MINE_B, 8889)]

    assert own.self_seeds(seeds) == [(MINE_A, 8889), (MINE_B, 8889)]


def test_local_interfaces_are_learned_and_a_failure_does_not_raise(monkeypatch):
    own = OwnAddresses()

    class _Boom:
        @staticmethod
        def net_if_addrs():
            raise OSError("no interfaces here")

    monkeypatch.setitem(__import__("sys").modules, "psutil", _Boom)

    assert own.learn_local_interfaces() == set()
    assert own.known() == set()


# --------------------------------------------------------------------------
# The bootstrap, which decides on the node id rather than the address
# --------------------------------------------------------------------------

def _manager(responses):
    """A DHTManager whose pings return whatever the test says they return."""
    manager = DHTManager.__new__(DHTManager)
    manager.node_id = ME
    manager.logger = logging.getLogger("test")
    manager.stats = {"bootstraps": 0}
    manager.config = SimpleNamespace(bootstrap_timeout=5.0)
    manager._seed_nodes = []

    async def _ping(ip, port):
        return responses.get((ip, port))

    manager._ping_node = _ping
    return manager


@pytest.mark.asyncio
async def test_a_seed_that_answers_with_our_own_id_is_not_a_responsive_seed(caplog):
    manager = _manager({
        (MINE_A, 8889): {"type": "PONG", "node_id": ME},
        (MINE_B, 8889): {"type": "PONG", "node_id": ME},
    })

    with caplog.at_level(logging.INFO):
        ok = await manager.bootstrap([(MINE_A, 8889), (MINE_B, 8889)])

    assert ok is False
    # The point of the change: not an error about the network.
    assert "is this node itself" in caplog.text or "are this node" in caplog.text
    assert "no responsive seed nodes" not in caplog.text


@pytest.mark.asyncio
async def test_a_seed_that_never_answers_still_says_so(caplog):
    manager = _manager({})

    with caplog.at_level(logging.INFO):
        ok = await manager.bootstrap([(STRANGER, 8889)])

    assert ok is False
    assert "no responsive seed nodes" in caplog.text


@pytest.mark.asyncio
async def test_a_real_seed_beside_a_self_seed_still_bootstraps(caplog):
    """One stranger is enough, and the self-seed is reported rather than counted."""
    manager = _manager({
        (MINE_A, 8889): {"type": "PONG", "node_id": ME},
        (STRANGER, 8889): {"type": "PONG", "node_id": PEER},
    })
    manager.find_node = lambda target: asyncio.sleep(0, result=[])
    manager._refresh_all_buckets = lambda: asyncio.sleep(0)
    manager.routing_table = SimpleNamespace(get_node_count=lambda: 1)

    with caplog.at_level(logging.INFO):
        ok = await manager.bootstrap([(MINE_A, 8889), (STRANGER, 8889)])

    assert ok is True
    assert "1 seed(s) are this node itself" in caplog.text


# --------------------------------------------------------------------------
# The peer cache, the second consumer of the same set
# --------------------------------------------------------------------------

def _orchestrator(own, cached_ip):
    from dpc_client_core.coordinators.connection_orchestrator import ConnectionOrchestrator

    cached = SimpleNamespace(last_direct_ip=cached_ip, last_direct_port=8888)
    orchestrator = ConnectionOrchestrator.__new__(ConnectionOrchestrator)
    orchestrator.p2p_manager = SimpleNamespace(
        own_addresses=own,
        peer_cache=SimpleNamespace(get_peer=lambda nid: cached),
    )
    # The DHT is empty, which is the state that reaches for the cache at all.
    orchestrator.dht_manager = SimpleNamespace(
        find_peer_full=lambda nid: asyncio.sleep(0, result=None)
    )
    orchestrator.stats = {
        "total_attempts": 0, "successful_connections": 0,
        "failed_connections": 0, "strategy_usage": {},
    }
    orchestrator.strategies = []
    return orchestrator


@pytest.mark.asyncio
async def test_a_cached_endpoint_that_is_our_own_door_is_refused(caplog):
    from dpc_client_core.coordinators.connection_orchestrator import (
        ConnectionFailedError,
        ConnectionOrchestrator,
    )

    own = OwnAddresses()
    own.learn(MINE_A)
    orchestrator = _orchestrator(own, MINE_A)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(ConnectionFailedError) as excinfo:
            await ConnectionOrchestrator.connect(orchestrator, PEER)

    assert "own address" in caplog.text
    assert "not found in DHT or peer cache" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_cached_endpoint_elsewhere_is_still_used():
    from dpc_client_core.coordinators.connection_orchestrator import (
        ConnectionFailedError,
        ConnectionOrchestrator,
    )

    own = OwnAddresses()
    own.learn(MINE_A)
    orchestrator = _orchestrator(own, STRANGER)

    with pytest.raises(ConnectionFailedError) as excinfo:
        await ConnectionOrchestrator.connect(orchestrator, PEER)

    # Past the cache check: the failure is about strategies, not a missing endpoint.
    assert "not found in DHT or peer cache" not in str(excinfo.value)


# --------------------------------------------------------------------------
# The RPC layer, where a refused add used to erase the answer
# --------------------------------------------------------------------------

def _rpc(add_raises):
    handler = DHTRPCHandler.__new__(DHTRPCHandler)

    def _add(node_id, ip, port):
        if add_raises:
            raise ValueError("Cannot add self to routing table")

    handler.routing_table = SimpleNamespace(node_id=ME, add_node=_add)

    async def _send(ip, port, rpc):
        return {"type": "PONG", "node_id": ME if add_raises else PEER}

    handler._send_rpc = _send
    return handler


@pytest.mark.asyncio
async def test_a_pong_survives_a_routing_table_that_refuses_it(caplog):
    """The answer is what makes a seed responsive; recording it is a separate act."""
    handler = _rpc(add_raises=True)

    with caplog.at_level(logging.INFO):
        response = await DHTRPCHandler.ping(handler, MINE_A, 8889)

    assert response is not None
    assert response["node_id"] == ME
    assert "not recorded" in caplog.text


@pytest.mark.asyncio
async def test_a_pong_that_can_be_recorded_is_returned_too():
    handler = _rpc(add_raises=False)

    response = await DHTRPCHandler.ping(handler, STRANGER, 8889)

    assert response["node_id"] == PEER
