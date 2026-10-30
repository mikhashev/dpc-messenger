"""HELLO and HELLO_ACK carry the sender's LAN addresses as `local_addresses` (DPTP 1.12).

A node behind a full-tunnel VPN, or on two networks, was reachable on its LAN
address while its peers knew only the address a connection had arrived from.
Each side now names its own LAN addresses ("ip:port", at most four, RFC 1918
IPv4 only) in the handshake. The receiver checks them, keeps them per peer in
the peer cache as metadata.local_addresses, replaced on every handshake, and
dials them first with a short budget. They never enter `addresses` (every
address a direct connection worked on) until a dial over one succeeds: an
advertised address becomes known only after it answers (Mike, 2026-10-29).

Loopback TLS through the real `start_server` and `connect_directly`; every file
in tmp_path, never ~/.dpc; addresses from RFC 1918 and RFC 5737 only.
"""

import asyncio
import datetime
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from dpc_protocol.crypto import generate_node_id
from dpc_protocol.protocol import MAX_HELLO_FRAME_BYTES
from dpc_client_core import p2p_manager as p2p_module
from dpc_client_core import peer_cache as peer_cache_module
from dpc_client_core.own_addresses import OwnAddresses
from dpc_client_core.p2p_manager import P2PManager
from dpc_client_core.peer_cache import (
    ADVERTISED_ADDRESS_DIAL_TIMEOUT_S,
    CachedPeer,
    PeerCache,
    addresses_to_try,
    checked_local_addresses,
    dial_in_order,
    local_addresses_to_advertise,
)

LOOPBACK = "127.0.0.1"
A_LAN = ["192.168.1.10:8888"]
B_LAN = ["10.1.2.3:9001", "192.168.1.20:9001"]


# --- two real managers -------------------------------------------------------------


def _identity(tmp_path: Path, name: str):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    node_id = generate_node_id(key.public_key())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, node_id)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )
    cert_file = tmp_path / f"{name}.crt"
    key_file = tmp_path / f"{name}.key"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return node_id, cert_file, key_file


async def _nothing(*args, **kwargs):
    return None


def _manager(tmp_path: Path, name: str, advertises) -> P2PManager:
    node_id, cert_file, key_file = _identity(tmp_path, name)
    m = P2PManager.__new__(P2PManager)
    m.node_id = node_id
    m.cert_file = cert_file
    m.key_file = key_file
    m.display_name = name
    m.settings = SimpleNamespace(get_dht_enabled=lambda: False)
    m.own_addresses = OwnAddresses()
    m.peers = {}
    m._peer_listener_tasks = {}
    m._intentional_disconnects = set()
    m._hub_client_refs = {}
    m._pending_webrtc = {}
    m._ice_candidates_buffer = {}
    m._server_task = None
    m.dht_manager = None
    m.on_peer_list_change = None
    m.on_message_received = None
    m.on_peer_disconnected = None
    m.peer_cache = PeerCache(tmp_path / f"{name}_peer_cache.json")
    m._persist_peer_certificate = lambda nid, pem: True
    m.announce_to_dht = _nothing
    m._failed_hello_counts = {}
    m._rate_limit_max_failures = 10
    m._rate_limit_window_seconds = 300
    m._pending_hello_counts = {}
    m._hello_timeout = 10.0
    m._max_pending_hellos_per_ip = 8
    m._own_local_addresses = lambda: list(advertises)
    return m


async def _listen(manager, monkeypatch):
    created = []
    real_start_server = asyncio.start_server

    async def _recording(*args, **kwargs):
        server = await real_start_server(*args, **kwargs)
        created.append(server)
        return server

    monkeypatch.setattr(p2p_module.asyncio, "start_server", _recording)
    await manager.start_server(host=LOOPBACK, port=0)
    monkeypatch.setattr(p2p_module.asyncio, "start_server", real_start_server)
    manager._test_server = created[0]
    return created[0].sockets[0].getsockname()[1]


async def _stop(*managers):
    for manager in managers:
        for peer in list(manager.peers.values()):
            await peer.close()
    for manager in managers:
        tasks = list(manager._peer_listener_tasks.values())
        if tasks:
            await asyncio.wait(tasks, timeout=5)
        manager._test_server.close()
        await asyncio.wait_for(manager._test_server.wait_closed(), timeout=5)


async def _until(condition, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, f"not true within {timeout}s"
        await asyncio.sleep(0.02)


def _frames_written(monkeypatch, strip_field=False):
    """Every handshake frame either side writes; with strip_field, sent as a pre-1.12 node would."""
    sent = []
    real = p2p_module.write_message

    async def _write(writer, message, *args, **kwargs):
        if message.get("command") in ("HELLO", "HELLO_ACK"):
            if strip_field:
                message = json.loads(json.dumps(message))
                message.pop("local_addresses", None)
                message.get("payload", {}).pop("local_addresses", None)
            sent.append(message)
        return await real(writer, message, *args, **kwargs)

    monkeypatch.setattr(p2p_module, "write_message", _write)
    return sent


# --- the handshake -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_hello_and_hello_ack_carry_it_and_each_side_keeps_the_others(tmp_path, monkeypatch):
    a = _manager(tmp_path, "a", A_LAN)
    b = _manager(tmp_path, "b", B_LAN)
    # The addresses go only to a known peer: here the two share a group.
    a.set_groups_for_peer(lambda node_id: ["group-shared"])
    b.set_groups_for_peer(lambda node_id: ["group-shared"])
    sent = _frames_written(monkeypatch)
    port = await _listen(b, monkeypatch)
    try:
        await a.connect_directly(LOOPBACK, port, b.node_id, timeout=15.0)
        await _until(lambda: a.node_id in b.peers and b.peer_cache.get_peer(a.node_id) is not None)

        hello = next(f for f in sent if f["command"] == "HELLO")
        ack = next(f for f in sent if f["command"] == "HELLO_ACK")
        assert hello["payload"]["local_addresses"] == A_LAN
        assert ack["local_addresses"] == B_LAN  # top level, as every HELLO_ACK field

        at_b = b.peer_cache.get_peer(a.node_id)
        at_a = a.peer_cache.get_peer(b.node_id)
        assert at_b.metadata["local_addresses"] == A_LAN
        assert at_a.metadata["local_addresses"] == B_LAN
        # Known only after it answers: the one address that answered is loopback.
        assert [e["ip"] for e in at_a.addresses] == [LOOPBACK]
        assert [e["ip"] for e in at_b.addresses] == [LOOPBACK]
    finally:
        await _stop(b)
        for peer in list(a.peers.values()):
            await peer.close()


@pytest.mark.asyncio
async def test_a_peer_without_the_field_connects_as_before_and_nothing_is_stored(tmp_path, monkeypatch):
    a = _manager(tmp_path, "a", A_LAN)
    b = _manager(tmp_path, "b", B_LAN)
    sent = _frames_written(monkeypatch, strip_field=True)
    port = await _listen(b, monkeypatch)
    try:
        await a.connect_directly(LOOPBACK, port, b.node_id, timeout=15.0)
        await _until(lambda: a.node_id in b.peers and b.peer_cache.get_peer(a.node_id) is not None)

        assert {f["command"] for f in sent} == {"HELLO", "HELLO_ACK"}
        assert all("local_addresses" not in f and "local_addresses" not in f.get("payload", {}) for f in sent)
        assert b.node_id in a.peers
        assert "local_addresses" not in b.peer_cache.get_peer(a.node_id).metadata
        assert "local_addresses" not in a.peer_cache.get_peer(b.node_id).metadata
    finally:
        await _stop(b)
        for peer in list(a.peers.values()):
            await peer.close()


def test_four_entries_keep_hello_far_under_the_handshake_cap():
    hello = {"command": "HELLO", "payload": {
        "node_id": "dpc-node-" + "a" * 32, "name": "x" * 64, "cert_pem": "c" * 1800,
        "hello_preimage_version": "dptp-hello-v1", "hello_signature": "s" * 344,
        "local_addresses": ["192.168.100.100:65535", "172.31.255.255:65535",
                            "10.255.255.255:65535", "192.168.200.200:65535"],
    }}
    assert len(json.dumps(hello).encode()) + 10 < MAX_HELLO_FRAME_BYTES // 16


def test_this_node_advertises_its_lan_addresses_with_the_listen_port():
    lan = ["192.168.1.10", "192.168.1.11", "172.16.0.5", "10.0.0.7", "10.0.0.8"]
    assert local_addresses_to_advertise(lan, 8888) == [
        "192.168.1.10:8888", "192.168.1.11:8888", "172.16.0.5:8888", "10.0.0.7:8888",
    ]
    assert local_addresses_to_advertise(["198.51.100.4", "192.168.1.10"], 9001) == ["192.168.1.10:9001"]
    assert local_addresses_to_advertise(lan, None) == []


def test_a_node_not_listening_advertises_nothing():
    m = P2PManager.__new__(P2PManager)
    assert m._own_local_addresses() == []


# --- what the receiver keeps -------------------------------------------------------


def test_five_entries_keep_the_first_four():
    five = [f"192.168.1.{i}:8888" for i in range(1, 6)]
    assert checked_local_addresses(five) == five[:4]


@pytest.mark.parametrize("bad", [
    "198.51.100.7:8888",     # RFC 5737, public
    "203.0.113.9:8888",      # RFC 5737, public
    "127.0.0.1:8888",        # loopback
    "169.254.3.4:8888",      # link-local
    "100.64.0.9:8888",       # CGNAT, not RFC 1918
    "192.168.1.5:0",
    "192.168.1.5:65536",
    "192.168.1.5:+80",
    "192.168.1.5:http",
    "192.168.1.5",
    "[fd00::1]:8888",
    "192.168.001.005:8888",
    12345,
    None,
])
def test_an_entry_that_is_not_a_private_ipv4_with_a_port_is_dropped(bad):
    assert checked_local_addresses([bad, "10.1.2.3:9001"]) == ["10.1.2.3:9001"]


def test_our_own_address_and_a_repeat_are_dropped():
    own = OwnAddresses()
    own.learn("192.168.1.20")
    got = checked_local_addresses(["192.168.1.20:8888", "10.1.2.3:9001", "10.1.2.3:9001"], own)
    assert got == ["10.1.2.3:9001"]


@pytest.mark.parametrize("value", ["192.168.1.5:8888", {"a": 1}, 7, None])
def test_a_field_that_is_not_a_list_is_ignored(value):
    assert checked_local_addresses(value) is None


def _cache_with_peer(tmp_path):
    cache = PeerCache(tmp_path / "peer_cache.json")
    cache.add_or_update_peer("dpc-node-peer", direct_ip="203.0.113.10", direct_port=8888,
                             supports_direct=True, direction="in")
    return cache


def test_each_handshake_replaces_the_list_and_never_merges_it(tmp_path):
    cache = _cache_with_peer(tmp_path)
    cache.set_local_addresses("dpc-node-peer", ["192.168.1.20:8888", "10.1.2.3:9001"])
    cache.set_local_addresses("dpc-node-peer", ["172.16.0.5:8888"])
    assert cache.get_peer("dpc-node-peer").metadata["local_addresses"] == ["172.16.0.5:8888"]

    cache.set_local_addresses("dpc-node-peer", None)  # an older peer: no field, no change
    assert cache.get_peer("dpc-node-peer").metadata["local_addresses"] == ["172.16.0.5:8888"]

    cache.set_local_addresses("dpc-node-peer", [])  # a peer with no LAN address now
    assert cache.get_peer("dpc-node-peer").metadata["local_addresses"] == []


def test_an_advertised_address_never_enters_addresses_on_its_own(tmp_path):
    cache = _cache_with_peer(tmp_path)
    cache.set_local_addresses("dpc-node-peer", ["192.168.1.20:8888", "10.1.2.3:9001"])

    peer = cache.get_peer("dpc-node-peer")
    assert [e["ip"] for e in peer.addresses] == ["203.0.113.10"]
    assert peer.last_direct_ip == "203.0.113.10"

    reloaded = PeerCache(tmp_path / "peer_cache.json").get_peer("dpc-node-peer")
    assert [e["ip"] for e in reloaded.addresses] == ["203.0.113.10"]
    assert reloaded.metadata["local_addresses"] == ["192.168.1.20:8888", "10.1.2.3:9001"]

    # The success path of a dial over one is what makes it known.
    cache.add_or_update_peer("dpc-node-peer", direct_ip="10.1.2.3", direct_port=9001,
                             supports_direct=True, direction="out")
    known = {(e["ip"], e["port"], e["direction"]) for e in cache.get_peer("dpc-node-peer").addresses}
    assert ("10.1.2.3", 9001, "out") in known


# --- the dial order ----------------------------------------------------------------


def _cached(local_addresses, addresses):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return CachedPeer(
        node_id="dpc-node-peer", last_seen=now,
        addresses=[{"ip": ip, "port": port, "last_seen": now, "direction": "in"} for ip, port in addresses],
        metadata={"local_addresses": local_addresses},
    )


def test_advertised_candidates_come_first_with_the_short_budget_and_none_twice():
    cached = _cached(
        ["10.1.2.3:9001", "192.168.1.20:8888", "172.16.0.5:8888"],
        [("203.0.113.10", 8888), ("192.168.1.20", 8888)],
    )
    own = OwnAddresses()
    own.learn("172.16.0.5")

    order = addresses_to_try(cached, own)

    assert [(e["ip"], e["port"]) for e in order] == [
        ("10.1.2.3", 9001),        # advertised, not answered yet
        ("192.168.1.20", 8888),    # answered, LAN first
        ("203.0.113.10", 8888),    # answered
    ]
    assert order[0]["dial_timeout_s"] == ADVERTISED_ADDRESS_DIAL_TIMEOUT_S == 2.0
    assert all("dial_timeout_s" not in e for e in order[1:])
    # The candidate is a new entry: the cache's own list is untouched.
    assert all("dial_timeout_s" not in e for e in cached.addresses)


def test_a_peer_that_never_advertised_dials_as_before():
    cached = _cached(None, [("203.0.113.10", 8888), ("192.168.1.20", 8888)])
    del cached.metadata["local_addresses"]
    assert [e["ip"] for e in addresses_to_try(cached)] == ["192.168.1.20", "203.0.113.10"]


@pytest.mark.asyncio
async def test_a_candidate_gets_only_its_short_budget_even_when_it_is_last():
    budgets = []

    async def dial(ip, port, budget):
        budgets.append((ip, budget))
        raise ConnectionRefusedError("nobody there")

    entries = [
        {"ip": "10.1.2.3", "port": 9001, "dial_timeout_s": 2.0},
        {"ip": "203.0.113.10", "port": 8888},
        {"ip": "192.168.1.30", "port": 9001, "dial_timeout_s": 2.0},
    ]
    await dial_in_order(dial, entries, total_timeout=30.0, per_address_timeout=10.0)

    assert budgets[0] == ("10.1.2.3", 2.0)
    assert budgets[1] == ("203.0.113.10", 10.0)
    assert budgets[2][0] == "192.168.1.30" and budgets[2][1] <= 2.0


@pytest.mark.asyncio
async def test_connect_via_node_id_tries_the_candidates_first_then_the_answered_address(tmp_path):
    m = _manager(tmp_path, "dialler", [])
    m.peer_cache.add_or_update_peer("dpc-node-peer", direct_ip="192.168.1.20", direct_port=8888,
                                    supports_direct=True, direction="out")
    m.peer_cache.set_local_addresses("dpc-node-peer", ["10.1.2.3:9001", "172.16.0.5:9001"])
    calls = []

    async def connect_directly(ip, port, node_id, timeout=None):
        calls.append((ip, port, timeout))
        if ip != "192.168.1.20":
            raise ConnectionRefusedError("not on this network")
        return object()

    m.connect_directly = connect_directly

    assert await m.connect_via_node_id("dpc-node-peer") is True
    assert [(ip, port) for ip, port, _ in calls] == [
        ("10.1.2.3", 9001), ("172.16.0.5", 9001), ("192.168.1.20", 8888),
    ]
    assert calls[0][2] <= 2.0 and calls[1][2] <= 2.0
    # The answered address keeps its own budget: the candidates did not eat it.
    assert calls[2][2] > 4.0
