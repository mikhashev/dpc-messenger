"""A blocked node that proves its key gets no HELLO_ACK, and is never dialled.

The listener accepts any node that proves its own key (board entry
A-PEER-CANNOT-BE-BLOCKED-AND-A-DISCONNECTED-ONE-DIALS-STRAIGHT-BACK-IN). A
node in `blocked_nodes` is now closed right after the identity check and before
HELLO_ACK, told nothing, the way the `max_proved_connections_per_ip` ceiling
refuses: one INFO line here, not a failed HELLO. A dial to it is refused before
the first packet with a message that says to unblock first.

Loopback TLS through the listener callback `start_server` uses and the real
`connect_directly`; every file in tmp_path, never ~/.dpc.
"""

import json
import logging

import pytest

from dpc_client_core.firewall import ContextFirewall
from tests.test_the_listener_bounds_what_a_stranger_can_hold_open import (
    LOOPBACK,
    _manager,
    _serve,
    _until,
)
from tests.test_one_address_holds_at_most_eight_proved_connections import _teardown


def _firewall(tmp_path, name, blocked=()):
    path = tmp_path / f"{name}_privacy_rules.json"
    path.write_text(json.dumps({"blocked_nodes": list(blocked)}))
    return ContextFirewall(path)


@pytest.mark.asyncio
async def test_a_blocked_dialler_is_closed_before_hello_ack(tmp_path, caplog):
    listener = _manager(tmp_path, "listener")
    blocked = _manager(tmp_path, "blocked")
    listener.firewall = _firewall(tmp_path, "listener", [blocked.node_id])
    server, port = await _serve(listener)
    try:
        caplog.set_level(logging.INFO, logger="dpc_client_core.p2p_manager")
        with pytest.raises(Exception):
            await blocked.connect_directly(LOOPBACK, port, listener.node_id, timeout=15.0)

        assert blocked.node_id not in listener.peers
        assert listener.node_id not in blocked.peers
        assert listener.peer_cache.get_peer(blocked.node_id) is None
        lines = [r.getMessage() for r in caplog.records
                 if r.levelno == logging.INFO and "Refused blocked node" in r.getMessage()]
        assert len(lines) == 1, lines
        assert blocked.node_id in lines[0] and "direct TLS accept" in lines[0]
        # It proved its key: not a failed HELLO, and its pending slot is back.
        assert LOOPBACK not in listener._failed_hello_counts
        await _until(lambda: LOOPBACK not in listener._pending_hello_counts)
    finally:
        await _teardown(server, listener, blocked)


@pytest.mark.asyncio
async def test_an_unblocked_dialler_still_gets_in(tmp_path):
    """The positive control: the same listener, the same firewall, another node."""
    listener = _manager(tmp_path, "listener")
    blocked = _manager(tmp_path, "blocked")
    friend = _manager(tmp_path, "friend")
    listener.firewall = _firewall(tmp_path, "listener", [blocked.node_id])
    server, port = await _serve(listener)
    try:
        await friend.connect_directly(LOOPBACK, port, listener.node_id, timeout=15.0)
        await _until(lambda: friend.node_id in listener.peers)
    finally:
        await _teardown(server, listener, friend)


@pytest.mark.asyncio
async def test_unblocking_lets_the_node_in_again(tmp_path):
    listener = _manager(tmp_path, "listener")
    blocked = _manager(tmp_path, "blocked")
    listener.firewall = _firewall(tmp_path, "listener", [blocked.node_id])
    server, port = await _serve(listener)
    try:
        assert listener.firewall.unblock_node(blocked.node_id)[0]
        await blocked.connect_directly(LOOPBACK, port, listener.node_id, timeout=15.0)
        await _until(lambda: blocked.node_id in listener.peers)
    finally:
        await _teardown(server, listener, blocked)


@pytest.mark.asyncio
async def test_a_dial_to_a_blocked_node_is_refused_before_the_first_packet(tmp_path, caplog):
    dialler = _manager(tmp_path, "dialler")
    far = _manager(tmp_path, "far")
    dialler.firewall = _firewall(tmp_path, "dialler", [far.node_id])
    server, port = await _serve(far)
    try:
        caplog.set_level(logging.INFO, logger="dpc_client_core.p2p_manager")
        with pytest.raises(ConnectionError, match="Unblock it first"):
            await dialler.connect_directly(LOOPBACK, port, far.node_id, timeout=15.0)

        assert far.node_id not in dialler.peers
        assert not any("Received a direct TLS connection attempt" in r.getMessage()
                       for r in caplog.records), "the listener never saw a packet"
        assert any("Refused blocked node" in r.getMessage() and "direct TLS dial" in r.getMessage()
                   for r in caplog.records)
    finally:
        # Closes whatever a broken gate let through, so it fails rather than hangs.
        await _teardown(server, far, dialler)


@pytest.mark.asyncio
async def test_a_dial_to_an_unblocked_node_goes_out(tmp_path):
    dialler = _manager(tmp_path, "dialler")
    far = _manager(tmp_path, "far")
    other = _manager(tmp_path, "other")
    dialler.firewall = _firewall(tmp_path, "dialler", [other.node_id])
    server, port = await _serve(far)
    try:
        await dialler.connect_directly(LOOPBACK, port, far.node_id, timeout=15.0)
        assert far.node_id in dialler.peers
    finally:
        await _teardown(server, far, dialler)
