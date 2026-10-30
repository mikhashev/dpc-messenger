"""A peer-cache entry makes a node "known" for `local_addresses` only if this node dialled it.

Every inbound handshake writes the peer cache, so the old rule ("in the peer
cache or in a group") let a stranger qualify by dialling in twice: the first
connection made the entry, the second got the LAN. Now a cache entry counts only
when it holds an address with direction "out" — a dial of ours that worked; a
shared group counts as before (agreed by the team, Mike's yes 2026-10-30).

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


async def _close(a, b):
    await _stop(b)
    for peer in list(a.peers.values()):
        await peer.close()


async def _dial_in(a, b, port, sent):
    """a dials b once more; returns the HELLO_ACK b wrote on this connection."""
    before = sum(1 for f in sent if f["command"] == "HELLO_ACK")
    await a.connect_directly(LOOPBACK, port, b.node_id, timeout=15.0)
    await _until(lambda: a.node_id in b.peers and b.peer_cache.get_peer(a.node_id) is not None)
    acks = [f for f in sent if f["command"] == "HELLO_ACK"]
    assert len(acks) == before + 1
    return acks[-1]


@pytest.mark.asyncio
async def test_a_node_that_dialled_in_before_gets_no_local_addresses_on_its_second_connection(tmp_path, monkeypatch):
    a = _manager(tmp_path, "a", A_LAN)
    b = _manager(tmp_path, "b", B_LAN)
    sent = _frames_written(monkeypatch)
    port = await _listen(b, monkeypatch)
    try:
        first = await _dial_in(a, b, port, sent)
        assert "local_addresses" not in first
        # The first connection wrote the listener's cache, with an inbound address only.
        cached = b.peer_cache.get_peer(a.node_id)
        assert cached is not None
        assert [e["direction"] for e in cached.addresses] == ["in"]

        second = await _dial_in(a, b, port, sent)

        assert "local_addresses" not in second
    finally:
        await _close(a, b)


@pytest.mark.asyncio
async def test_a_node_this_node_once_dialled_gets_them(tmp_path, monkeypatch):
    a = _manager(tmp_path, "a", A_LAN)
    b = _manager(tmp_path, "b", B_LAN)
    # b once dialled a successfully, at a's address on an earlier network.
    b.peer_cache.add_or_update_peer(
        node_id=a.node_id, direct_ip="198.51.100.20", direct_port=8888,
        supports_direct=True, direction="out",
    )
    sent = _frames_written(monkeypatch)
    port = await _listen(b, monkeypatch)
    try:
        ack = await _dial_in(a, b, port, sent)

        assert ack["local_addresses"] == B_LAN
    finally:
        await _close(a, b)


@pytest.mark.asyncio
async def test_a_group_co_member_that_only_dialled_in_still_gets_them(tmp_path, monkeypatch):
    a = _manager(tmp_path, "a", A_LAN)
    b = _manager(tmp_path, "b", B_LAN)
    groups_b = GroupManager(tmp_path / "b_home", b.node_id)
    groups_b.create_group("lan", "", [a.node_id])
    b.set_groups_for_peer(groups_b.get_groups_for_peer)
    sent = _frames_written(monkeypatch)
    port = await _listen(b, monkeypatch)
    try:
        await _dial_in(a, b, port, sent)
        assert [e["direction"] for e in b.peer_cache.get_peer(a.node_id).addresses] == ["in"]

        second = await _dial_in(a, b, port, sent)

        assert second["local_addresses"] == B_LAN
    finally:
        await _close(a, b)
