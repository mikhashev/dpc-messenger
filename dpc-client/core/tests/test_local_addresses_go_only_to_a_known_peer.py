"""`local_addresses` goes only to a peer this node already knows (DPTP 1.12 §3.1).

The listener named its LAN addresses in HELLO_ACK to any node that completed the
HELLO proof, so a stranger that reached the port learned the LAN layout. Now the
key is sent only to a peer in the peer cache or in a group with this node, and
left out for anyone else (Mike's call, 2026-10-30). The dialler's HELLO follows
the same rule; what a receiver does with the field is unchanged.

Loopback TLS through the real `start_server` and `connect_directly`; every file
in tmp_path, never ~/.dpc; addresses from RFC 1918 and RFC 5737 only.
"""

import pytest

from dpc_client_core.managers.group_manager import GroupManager
from tests.test_a_hello_carries_the_senders_local_addresses import (
    A_LAN,
    B_LAN,
    LOOPBACK,
    _frames_written,
    _listen,
    _manager,
    _stop,
    _until,
)


def _knows(manager, other):
    """`manager` has `other` in its peer cache, as after an earlier connection."""
    manager.peer_cache.add_or_update_peer(
        node_id=other.node_id, direct_ip="198.51.100.20", direct_port=8888,
        supports_direct=True, direction="in",
    )


async def _handshake(a, b, monkeypatch):
    """a dials b; returns the HELLO and HELLO_ACK that went over the wire."""
    sent = _frames_written(monkeypatch)
    port = await _listen(b, monkeypatch)
    await a.connect_directly(LOOPBACK, port, b.node_id, timeout=15.0)
    await _until(lambda: a.node_id in b.peers and b.peer_cache.get_peer(a.node_id) is not None)
    hello = next(f for f in sent if f["command"] == "HELLO")
    ack = next(f for f in sent if f["command"] == "HELLO_ACK")
    return hello, ack


async def _close(a, b):
    await _stop(b)
    for peer in list(a.peers.values()):
        await peer.close()


@pytest.mark.asyncio
async def test_a_stranger_that_completes_the_hello_proof_gets_no_local_addresses(tmp_path, monkeypatch):
    a = _manager(tmp_path, "a", A_LAN)
    b = _manager(tmp_path, "b", B_LAN)
    try:
        hello, ack = await _handshake(a, b, monkeypatch)

        assert "local_addresses" not in ack
        assert "local_addresses" not in hello["payload"]
        # The connection itself is made as before.
        assert b.node_id in a.peers and a.node_id in b.peers
        assert "local_addresses" not in a.peer_cache.get_peer(b.node_id).metadata
        assert "local_addresses" not in b.peer_cache.get_peer(a.node_id).metadata
    finally:
        await _close(a, b)


@pytest.mark.asyncio
async def test_a_cached_peer_gets_them_both_ways(tmp_path, monkeypatch):
    a = _manager(tmp_path, "a", A_LAN)
    b = _manager(tmp_path, "b", B_LAN)
    _knows(a, b)
    _knows(b, a)
    try:
        hello, ack = await _handshake(a, b, monkeypatch)

        assert ack["local_addresses"] == B_LAN
        assert hello["payload"]["local_addresses"] == A_LAN
        assert a.peer_cache.get_peer(b.node_id).metadata["local_addresses"] == B_LAN
        assert b.peer_cache.get_peer(a.node_id).metadata["local_addresses"] == A_LAN
    finally:
        await _close(a, b)


@pytest.mark.asyncio
async def test_a_group_co_member_gets_them_without_a_cache_entry(tmp_path, monkeypatch):
    a = _manager(tmp_path, "a", A_LAN)
    b = _manager(tmp_path, "b", B_LAN)
    groups_a = GroupManager(tmp_path / "a_home", a.node_id)
    groups_b = GroupManager(tmp_path / "b_home", b.node_id)
    groups_a.create_group("lan", "", [b.node_id])
    groups_b.create_group("lan", "", [a.node_id])
    a.set_groups_for_peer(groups_a.get_groups_for_peer)
    b.set_groups_for_peer(groups_b.get_groups_for_peer)
    try:
        assert a.peer_cache.get_peer(b.node_id) is None
        hello, ack = await _handshake(a, b, monkeypatch)

        assert ack["local_addresses"] == B_LAN
        assert hello["payload"]["local_addresses"] == A_LAN
    finally:
        await _close(a, b)


@pytest.mark.asyncio
async def test_each_side_decides_by_itself_and_the_receiver_keeps_what_it_gets(tmp_path, monkeypatch):
    # The dialler knows the listener; the listener has never seen the dialler.
    a = _manager(tmp_path, "a", A_LAN)
    b = _manager(tmp_path, "b", B_LAN)
    _knows(a, b)
    try:
        hello, ack = await _handshake(a, b, monkeypatch)

        assert hello["payload"]["local_addresses"] == A_LAN
        assert "local_addresses" not in ack
        # Receiving is not gated: the listener keeps a stranger's list as before.
        assert b.peer_cache.get_peer(a.node_id).metadata["local_addresses"] == A_LAN
        assert "local_addresses" not in a.peer_cache.get_peer(b.node_id).metadata
    finally:
        await _close(a, b)


@pytest.mark.asyncio
async def test_a_group_lookup_that_fails_leaves_the_key_out_and_the_handshake_completes(tmp_path, monkeypatch):
    a = _manager(tmp_path, "a", A_LAN)
    b = _manager(tmp_path, "b", B_LAN)

    def broken(node_id):
        raise RuntimeError("group store unreadable")

    b.set_groups_for_peer(broken)
    try:
        hello, ack = await _handshake(a, b, monkeypatch)

        assert "local_addresses" not in ack
        assert a.node_id in b.peers
    finally:
        await _close(a, b)
