"""A peer reached on two routes keeps both.

`CachedPeer.last_direct_ip` was one value overwritten on every write, so a peer
reached through a VPN exit and later on its LAN address remembered only the
latest, and an IPv4 and an IPv6 endpoint of one peer could not coexist
(A-PEER-CACHE-HOLDS-ONE-ADDRESS-SO-A-SECOND-ROUTE-THAT-WORKED-IS-FORGOTTEN).
`addresses` keeps every route that worked; a dial from the cache tries them
local-network first and stops on the first that answers.

Addresses here are RFC 5737, RFC 1918 and 2001:db8::/32 only; every file lives
in tmp_path.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from dpc_client_core.own_addresses import OwnAddresses
from dpc_client_core.peer_cache import (
    MAX_ADDRESSES,
    PeerCache,
    addresses_to_try,
    dial_in_order,
)
from dpc_client_core.service import CoreService

PEER = "dpc-node-" + "d" * 32


def _cache(tmp_path) -> PeerCache:
    return PeerCache(tmp_path / "peer_cache.json")


def _ips(peer):
    return sorted(e["ip"] for e in peer.addresses)


def test_two_routes_that_worked_are_both_kept(tmp_path):
    cache = _cache(tmp_path)
    cache.add_or_update_peer(PEER, direct_ip="203.0.113.10", direct_port=8888,
                             supports_direct=True, direction="out")
    cache.add_or_update_peer(PEER, direct_ip="192.168.1.20", direct_port=8888,
                             supports_direct=True, direction="in")

    peer = cache.get_peer(PEER)
    assert _ips(peer) == ["192.168.1.20", "203.0.113.10"]
    by_ip = {e["ip"]: e for e in peer.addresses}
    assert by_ip["203.0.113.10"]["direction"] == "out"
    assert by_ip["192.168.1.20"]["direction"] == "in"


def test_ipv4_and_ipv6_of_one_peer_coexist_and_ipv6_is_stored_bare(tmp_path):
    cache = _cache(tmp_path)
    cache.add_or_update_peer(PEER, direct_ip="198.51.100.3", direct_port=8888, direction="out")
    cache.add_or_update_peer(PEER, direct_ip="[2001:DB8::7%eth0]", direct_port=8888, direction="out")

    assert _ips(cache.get_peer(PEER)) == ["198.51.100.3", "2001:db8::7"]


def test_a_refresh_of_a_known_address_does_not_duplicate_it(tmp_path):
    cache = _cache(tmp_path)
    cache.add_or_update_peer(PEER, direct_ip="203.0.113.10", direct_port=9001, direction="out")
    first_seen = cache.get_peer(PEER).addresses[0]["last_seen"]
    cache.get_peer(PEER).addresses[0]["last_seen"] = "2026-01-01T00:00:00+00:00"
    # The same address as an IPv4-mapped IPv6 peername, from a dual-stack socket.
    cache.add_or_update_peer(PEER, direct_ip="::ffff:203.0.113.10", direction="in")

    peer = cache.get_peer(PEER)
    assert len(peer.addresses) == 1
    entry = peer.addresses[0]
    assert entry["last_seen"] > "2026-01-01T00:00:00+00:00"
    assert first_seen is not None
    assert entry["direction"] == "in"


def test_an_inbound_connection_keeps_the_port_the_address_was_dialled_on(tmp_path):
    """The source port of an inbound connection is not the peer's listener."""
    cache = _cache(tmp_path)
    cache.add_or_update_peer(PEER, direct_ip="192.168.1.20", direct_port=9001, direction="out")
    cache.add_or_update_peer(PEER, direct_ip="192.168.1.20", direct_port=None, direction="in")
    cache.add_or_update_peer(PEER, direct_ip="203.0.113.10", direct_port=None, direction="in")

    by_ip = {e["ip"]: e["port"] for e in cache.get_peer(PEER).addresses}
    assert by_ip == {"192.168.1.20": 9001, "203.0.113.10": 8888}


def test_a_ninth_address_evicts_the_oldest_by_last_seen(tmp_path):
    cache = _cache(tmp_path)
    for i in range(MAX_ADDRESSES):
        cache.add_or_update_peer(PEER, direct_ip=f"198.51.100.{i + 1}", direct_port=8888, direction="out")
    peer = cache.get_peer(PEER)
    for i, entry in enumerate(peer.addresses):
        entry["last_seen"] = f"2026-10-{10 + i:02d}T00:00:00+00:00"
    # The oldest is not the first appended: the third gets the earliest date.
    peer.addresses[2]["last_seen"] = "2026-09-01T00:00:00+00:00"

    cache.add_or_update_peer(PEER, direct_ip="203.0.113.99", direct_port=8888, direction="out")

    ips = _ips(cache.get_peer(PEER))
    assert len(ips) == MAX_ADDRESSES
    assert "198.51.100.3" not in ips
    assert "203.0.113.99" in ips


def test_the_old_fields_still_name_the_newest_address(tmp_path):
    cache = _cache(tmp_path)
    cache.add_or_update_peer(PEER, direct_ip="203.0.113.10", direct_port=9001, direction="out")
    cache.add_or_update_peer(PEER, direct_ip="192.168.1.20", direct_port=9002, direction="out")

    row = json.loads((tmp_path / "peer_cache.json").read_text())["peers"][0]
    assert (row["last_direct_ip"], row["last_direct_port"]) == ("192.168.1.20", 9002)
    # An older build builds CachedPeer(**row): no key it does not know may sit
    # beside the old ones, so the list travels inside metadata.
    assert "addresses" not in row
    assert sorted(e["ip"] for e in row["metadata"]["addresses"]) == ["192.168.1.20", "203.0.113.10"]


def test_a_file_from_before_the_list_loads_its_one_address_as_the_list(tmp_path):
    path = tmp_path / "peer_cache.json"
    path.write_text(json.dumps({"version": "1.0", "peers": [{
        "node_id": PEER, "display_name": "Old", "last_seen": "2026-10-20T10:00:00+00:00",
        "last_direct_ip": "203.0.113.10", "last_direct_port": 9001,
        "supports_webrtc": False, "supports_direct": True,
        "last_connection_direction": "out", "metadata": {},
    }]}))

    peer = PeerCache(path).get_peer(PEER)
    assert peer.addresses == [{
        "ip": "203.0.113.10", "port": 9001,
        "last_seen": "2026-10-20T10:00:00+00:00", "direction": "out",
    }]


def test_a_file_this_build_wrote_reloads_with_its_list_and_nothing_in_metadata(tmp_path):
    cache = _cache(tmp_path)
    cache.add_or_update_peer(PEER, direct_ip="203.0.113.10", direct_port=8888, direction="out")
    cache.add_or_update_peer(PEER, direct_ip="192.168.1.20", direct_port=8888, direction="in",
                             metadata={"last_connection_type": "direct_tls"})

    peer = _cache(tmp_path).get_peer(PEER)
    assert _ips(peer) == ["192.168.1.20", "203.0.113.10"]
    assert "addresses" not in peer.metadata
    assert peer.metadata["last_connection_type"] == "direct_tls"


def test_a_dial_tries_the_local_network_first_then_the_newest(tmp_path):
    cached = SimpleNamespace(addresses=[
        {"ip": "203.0.113.10", "port": 8888, "last_seen": "2026-10-28T10:00:00+00:00", "direction": "out"},
        {"ip": "198.51.100.3", "port": 8888, "last_seen": "2026-10-27T10:00:00+00:00", "direction": "out"},
        {"ip": "192.168.1.20", "port": 8888, "last_seen": "2026-10-01T10:00:00+00:00", "direction": "in"},
        {"ip": "fd00::5", "port": 8888, "last_seen": "2026-10-02T10:00:00+00:00", "direction": "out"},
        {"ip": "2001:db8::7", "port": 8888, "last_seen": "2026-10-26T10:00:00+00:00", "direction": "out"},
        {"ip": "fe80::1", "port": 8888, "last_seen": "2026-10-28T11:00:00+00:00", "direction": "in"},
        {"ip": "10.0.0.4", "port": 8888, "last_seen": "2026-10-03T10:00:00+00:00", "direction": "out"},
    ])

    order = [e["ip"] for e in addresses_to_try(cached)]

    assert order == [
        "10.0.0.4", "fd00::5", "192.168.1.20",       # local network, newest first
        "203.0.113.10", "198.51.100.3", "2001:db8::7",  # the rest, newest first
    ]  # fe80::1 has no zone and cannot be dialled


def test_an_address_that_is_also_our_own_is_tried_last_not_dropped():
    own = OwnAddresses()
    own.learn("203.0.113.10")
    cached = SimpleNamespace(addresses=[
        {"ip": "203.0.113.10", "port": 8888, "last_seen": "2026-10-28T10:00:00+00:00", "direction": "in"},
        {"ip": "198.51.100.3", "port": 8888, "last_seen": "2026-10-01T10:00:00+00:00", "direction": "out"},
    ])

    assert [e["ip"] for e in addresses_to_try(cached, own)] == ["198.51.100.3", "203.0.113.10"]


@pytest.mark.asyncio
async def test_the_first_address_that_answers_ends_the_dial():
    tried = []

    async def dial(ip, port, budget):
        tried.append(ip)
        if ip == "192.168.1.20":
            raise ConnectionRefusedError("nobody there")
        return f"connection to {ip}"

    entries = [{"ip": ip, "port": 8888} for ip in ("192.168.1.20", "203.0.113.10", "198.51.100.3")]
    connection, entry, error = await dial_in_order(dial, entries, total_timeout=30.0)

    assert connection == "connection to 203.0.113.10"
    assert entry["ip"] == "203.0.113.10"
    assert error is None
    assert tried == ["192.168.1.20", "203.0.113.10"]


@pytest.mark.asyncio
async def test_a_dead_address_costs_the_next_only_its_own_budget():
    budgets = []

    async def dial(ip, port, budget):
        budgets.append(budget)
        if ip == "192.168.1.20":
            await asyncio.sleep(10)
        return ip

    entries = [{"ip": "192.168.1.20", "port": 8888}, {"ip": "203.0.113.10", "port": 8888}]
    connection, _, _ = await dial_in_order(
        dial, entries, total_timeout=5.0, per_address_timeout=0.05
    )

    assert connection == "203.0.113.10"
    assert budgets[0] == pytest.approx(0.05)
    assert budgets[1] > 4.0  # the last address gets what is left


@pytest.mark.asyncio
async def test_the_orchestrator_dials_every_cached_address_when_the_dht_is_empty():
    from dpc_client_core.coordinators.connection_orchestrator import ConnectionOrchestrator

    tried = []

    async def connect_directly(ip, port, node_id, timeout=None):
        tried.append(ip)
        if ip != "203.0.113.10":
            raise ConnectionRefusedError("nobody there")
        return SimpleNamespace(strategy_used=None)

    cached = SimpleNamespace(addresses=[
        {"ip": "203.0.113.10", "port": 8888, "last_seen": "2026-10-28T10:00:00+00:00", "direction": "out"},
        {"ip": "192.168.1.20", "port": 8888, "last_seen": "2026-10-01T10:00:00+00:00", "direction": "in"},
    ])
    orchestrator = ConnectionOrchestrator.__new__(ConnectionOrchestrator)
    orchestrator.p2p_manager = SimpleNamespace(
        own_addresses=OwnAddresses(),
        peer_cache=SimpleNamespace(get_peer=lambda nid: cached),
        connect_directly=connect_directly,
    )
    orchestrator.dht_manager = SimpleNamespace(find_peer_full=lambda nid: asyncio.sleep(0, result=None))
    orchestrator.stats = {"total_attempts": 0, "successful_connections": 0,
                          "failed_connections": 0, "strategy_usage": {}}
    orchestrator.strategies = []

    connection = await ConnectionOrchestrator.connect(orchestrator, PEER)

    assert tried == ["192.168.1.20", "203.0.113.10"]
    assert connection.strategy_used == "ipv4_direct"


@pytest.mark.asyncio
async def test_the_cached_peers_list_carries_no_address_from_the_list(tmp_path):
    cache = _cache(tmp_path)
    for ip in ("203.0.113.10", "192.168.1.20", "2001:db8::7"):
        cache.add_or_update_peer(PEER, display_name="Dee", direct_ip=ip,
                                 direct_port=9001, direction="out")
    service = SimpleNamespace(p2p_manager=SimpleNamespace(peer_cache=cache, peers={}))

    answer = await CoreService.list_cached_peers(service)

    assert set(answer["peers"][0]) == {"node_id", "display_name", "last_seen", "is_connected"}
    wire = json.dumps(answer)
    for leaked in ("203.0.113.10", "192.168.1.20", "2001:db8::7", "9001", "addresses"):
        assert leaked not in wire
