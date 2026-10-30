"""One address holds at most eight inbound proved connections.

`max_pending_hellos_per_ip` counts connections only until HELLO_ACK, and a node
id costs one RSA key pair, so one address could hold any number of proved
connections, each able to keep a 64 MiB frame half-sent. The listener now counts
the inbound direct entries in `self.peers` from the connecting address and,
at `max_proved_connections_per_ip`, closes a new node after its identity check
and before HELLO_ACK: silently to it, one INFO line here, not a failed HELLO. A
node that holds an entry already is replacing its own connection and is never
refused; connections this node dialled are not counted.

Loopback TLS through the listener callback `start_server` uses and the real
`connect_directly`; every file in tmp_path, never ~/.dpc; other addresses from
RFC 5737 only.
"""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from dpc_client_core.p2p_manager import PeerConnection
from tests.test_the_listener_bounds_what_a_stranger_can_hold_open import (
    LOOPBACK,
    _manager,
    _serve,
    _stop,
    _until,
)


async def _dial(dialler, port, listener):
    await dialler.connect_directly(LOOPBACK, port, listener.node_id, timeout=15.0)
    await _until(lambda: dialler.node_id in listener.peers)


async def _teardown(server, *managers):
    for manager in managers:
        for peer in list(manager.peers.values()):
            await peer.close()
    tasks = [t for m in managers for t in m._peer_listener_tasks.values()]
    if tasks:
        await asyncio.wait(tasks, timeout=5)
    await _stop(server)


@pytest.mark.asyncio
async def test_the_ninth_proved_node_from_one_address_is_refused_before_hello_ack(tmp_path, caplog):
    listener = _manager(tmp_path, "listener")
    diallers = [_manager(tmp_path, f"d{i}") for i in range(9)]
    server, port = await _serve(listener)
    try:
        for dialler in diallers[:8]:
            await _dial(dialler, port, listener)
        assert len(listener.peers) == 8

        caplog.set_level(logging.INFO, logger="dpc_client_core.p2p_manager")
        ninth = diallers[8]
        # No HELLO_ACK reaches it: the dial fails on the read that waits for one.
        with pytest.raises(Exception):
            await ninth.connect_directly(LOOPBACK, port, listener.node_id, timeout=15.0)

        assert ninth.node_id not in listener.peers
        assert listener.node_id not in ninth.peers
        assert len(listener.peers) == 8
        # Never registered, never cached.
        assert listener.peer_cache.get_peer(ninth.node_id) is None
        assert any("already holds 8 proved connections" in r.getMessage()
                   and ninth.node_id in r.getMessage() for r in caplog.records)
        # The peer proved its key: not a failed HELLO, and the pending slot is back.
        assert LOOPBACK not in listener._failed_hello_counts
        await _until(lambda: LOOPBACK not in listener._pending_hello_counts)
    finally:
        await _teardown(server, listener, *diallers)


@pytest.mark.asyncio
async def test_a_node_that_holds_a_connection_may_replace_it_at_the_ceiling(tmp_path):
    listener = _manager(tmp_path, "listener")
    diallers = [_manager(tmp_path, f"d{i}") for i in range(8)]
    server, port = await _serve(listener)
    try:
        for dialler in diallers:
            await _dial(dialler, port, listener)
        again = diallers[3]
        old = listener.peers[again.node_id]

        await again.connect_directly(LOOPBACK, port, listener.node_id, timeout=15.0)
        await _until(lambda: listener.peers.get(again.node_id) is not old)

        assert len(listener.peers) == 8
        assert old.writer.is_closing()
    finally:
        await _teardown(server, listener, *diallers)


@pytest.mark.asyncio
async def test_connections_from_another_address_are_not_counted_against_this_one(tmp_path):
    listener = _manager(tmp_path, "listener")
    dialler = _manager(tmp_path, "dialler")
    # Eight inbound proved connections held from an address that is not loopback.
    for i in range(8):
        writer = SimpleNamespace(get_extra_info=lambda key: ("192.0.2.10", 40000) if key == "peername" else None)
        listener.peers[f"dpc-node-{i:032x}"] = PeerConnection(
            f"dpc-node-{i:032x}", reader=None, writer=writer, direction="in",
        )
    assert listener._proved_connections_from("192.0.2.10") == 8
    server, port = await _serve(listener)
    try:
        await _dial(dialler, port, listener)

        assert dialler.node_id in listener.peers
        assert listener._proved_connections_from(LOOPBACK) == 1
    finally:
        for i in range(8):
            listener.peers.pop(f"dpc-node-{i:032x}")
        await _teardown(server, listener, dialler)


@pytest.mark.asyncio
async def test_an_outbound_connection_is_not_counted_against_the_address(tmp_path):
    listener = _manager(tmp_path, "listener")
    far = _manager(tmp_path, "far")
    diallers = [_manager(tmp_path, f"d{i}") for i in range(8)]
    server, port = await _serve(listener)
    far_server, far_port = await _serve(far)
    try:
        # The listener dials out to a node on the same address.
        await listener.connect_directly(LOOPBACK, far_port, far.node_id, timeout=15.0)
        assert listener.peers[far.node_id].direction == "out"
        for dialler in diallers[:7]:
            await _dial(dialler, port, listener)

        # Seven inbound and one outbound from 127.0.0.1: the eighth inbound is admitted.
        await _dial(diallers[7], port, listener)

        assert listener._proved_connections_from(LOOPBACK) == 8
        assert len(listener.peers) == 9
        assert not listener.peers[far.node_id].writer.is_closing()
    finally:
        await _teardown(far_server, far)
        await _teardown(server, listener, *diallers)


def test_the_ceiling_is_read_from_configuration(tmp_path):
    from dpc_client_core.settings import Settings

    assert Settings(tmp_path).get_max_proved_connections_per_ip() == 8

    (tmp_path / "config.ini").write_text("[connection]\nmax_proved_connections_per_ip = 3\n")
    assert Settings(tmp_path).get_max_proved_connections_per_ip() == 3
