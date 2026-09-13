"""A-RELAY-MESSAGE-CARRIES-DATA-WHERE-THE-SPEC-SAYS-MESSAGE-AND-A-RELAY-ERROR-HAS-NO-HANDLER-SO-THE-CLIENT-NEVER-LEARNS-WHY

Bucket b of that entry: the relay answers RELAY_REGISTER / RELAY_MESSAGE
failures with {"command": "ERROR", "payload": {"error": <code>, ...}}, and
RELAY_DISCONNECT with RELAY_DISCONNECT_ACK — commands no handler was
registered for (message_router.py logged them at WARNING and dropped them).
Before this fix, a rejected registration ran the pending RELAY_READY wait
all the way to its 60s timeout instead of failing on the ERROR that named
the reason.
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from dpc_client_core.managers.relay_manager import RelayManager
from dpc_client_core.message_handlers.relay_error_handler import RelayErrorHandler
from dpc_client_core.message_handlers.relay_response_handler import RelayDisconnectAckHandler
from dpc_client_core.models.relay_node import RelayNode
from dpc_client_core.transports.relayed_connection import RelayedPeerConnection

RELAY_NODE_ID = "dpc-node-relay0000000000000000000000000000000"
TARGET_PEER_ID = "dpc-node-target00000000000000000000000000000"


def _relay_node():
    return RelayNode(
        node_id=RELAY_NODE_ID, ip="127.0.0.1", port=9999,
        available=True, max_peers=10,
    )


def _relay_manager():
    """A RelayManager whose p2p_manager is mocked enough to drive
    connect_via_relay(): connect_directly() and send_message_to_peer() are
    no-ops, and .peers holds a stand-in connection for the relay node."""
    dht = MagicMock()
    dht.node_id = "dpc-node-self000000000000000000000000000000"
    p2p = MagicMock()
    p2p.connect_directly = AsyncMock()
    p2p.send_message_to_peer = AsyncMock()
    p2p.peers = {RELAY_NODE_ID: MagicMock(send=AsyncMock())}
    return RelayManager(dht_manager=dht, p2p_manager=p2p, volunteer=False), p2p


async def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never became true")


@pytest.mark.asyncio
async def test_not_volunteering_fails_the_pending_register_promptly():
    """ERROR not_volunteering during RELAY_REGISTER fails connect_via_relay
    with the code in the message, well under its 60s RELAY_READY timeout."""
    manager, _p2p = _relay_manager()
    relay_node = _relay_node()

    connect_task = asyncio.create_task(
        manager.connect_via_relay(TARGET_PEER_ID, relay_node)
    )
    await _wait_until(lambda: TARGET_PEER_ID in manager._pending_relay_sessions)

    handler = RelayErrorHandler(service=SimpleNamespace(relay_manager=manager))
    started = time.monotonic()
    await handler.handle(RELAY_NODE_ID, {
        "error": "not_volunteering",
        "message": "This node is not volunteering as a relay",
    })

    with pytest.raises(ConnectionError, match="not_volunteering"):
        await asyncio.wait_for(connect_task, timeout=1.0)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, "settled via the timeout path, not the ERROR frame"
    # And the per-relay index is cleaned up, not left to leak.
    assert TARGET_PEER_ID not in manager._pending_relay_register_by_relay[RELAY_NODE_ID]


def _sendable_connection(manager, relay_node, session_id="session-1"):
    conn = RelayedPeerConnection(
        peer_id=TARGET_PEER_ID,
        relay_node=relay_node,
        relay_connection=MagicMock(send=AsyncMock()),
        session_id=session_id,
        own_node_id="dpc-node-self000000000000000000000000000000",
        relay_manager=manager,
    )
    conn.running = True
    conn._get_peer_certificate = AsyncMock(return_value=MagicMock(public_key=MagicMock()))
    return conn


@pytest.mark.asyncio
async def test_forward_failed_marks_the_connection_and_the_next_send_reports_it():
    """send_message() must not block waiting for a forward_failed that
    usually never comes — a chat message pays no per-send latency for it.
    It returns as soon as the write is handed to the relay; a *later* ERROR
    marks the connection dead, and the *next* send raises promptly, naming
    the recorded code, without writing anything."""
    manager, _p2p = _relay_manager()
    relay_node = _relay_node()
    conn = _sendable_connection(manager, relay_node)
    handler = RelayErrorHandler(service=SimpleNamespace(relay_manager=manager))

    import dpc_client_core.transports.relayed_connection as relayed_connection_module
    orig_encrypt = relayed_connection_module.encrypt_with_public_key_hybrid
    relayed_connection_module.encrypt_with_public_key_hybrid = lambda *_a, **_kw: b"cipher"
    try:
        started = time.monotonic()
        await conn.send_message({"command": "SEND_TEXT", "payload": {"text": "hi"}})
        elapsed = time.monotonic() - started

        assert elapsed < 0.2, "send_message waited for an ERROR instead of returning"
        assert conn.last_forward_error is None
        assert conn.running is True

        # The relay answers after send_message has already returned.
        await handler.handle(RELAY_NODE_ID, {
            "error": "forward_failed",
            "message": "Failed to forward message (session not found or rate limited)",
        })
        await asyncio.sleep(0)  # let the Future's done-callback run

        assert conn.last_forward_error == "forward_failed"
        assert conn.running is False

        started = time.monotonic()
        with pytest.raises(ConnectionError, match="forward_failed"):
            await conn.send_message({"command": "SEND_TEXT", "payload": {"text": "again"}})
        assert time.monotonic() - started < 0.2, "the next send should fail before doing any work"
    finally:
        relayed_connection_module.encrypt_with_public_key_hybrid = orig_encrypt


@pytest.mark.asyncio
async def test_stale_pending_forwards_are_settled_on_disconnect():
    """A successful forward's Future has nothing that ever resolves it (the
    relay sends no success ack) — stop() must settle and discard this
    connection's own pending Futures, or they leak in
    relay_manager._pending_forwards_by_relay for the life of the process."""
    manager, _p2p = _relay_manager()
    relay_node = _relay_node()
    conn = _sendable_connection(manager, relay_node)

    import dpc_client_core.transports.relayed_connection as relayed_connection_module
    orig_encrypt = relayed_connection_module.encrypt_with_public_key_hybrid
    relayed_connection_module.encrypt_with_public_key_hybrid = lambda *_a, **_kw: b"cipher"
    try:
        await conn.send_message({"command": "SEND_TEXT", "payload": {"text": "hi"}})
    finally:
        relayed_connection_module.encrypt_with_public_key_hybrid = orig_encrypt

    assert len(manager._pending_forwards_by_relay[RELAY_NODE_ID]) == 1

    await conn.stop()
    await asyncio.sleep(0)  # let the cancelled Future's done-callback run

    assert len(manager._pending_forwards_by_relay[RELAY_NODE_ID]) == 0
    assert conn._pending_forwards == []


@pytest.mark.asyncio
async def test_relay_disconnect_ack_settles_the_connection_state():
    """RELAY_DISCONNECT_ACK records the ack on the matching RelayedPeerConnection
    (session_id is the only correlation the payload carries)."""
    manager, _p2p = _relay_manager()
    relay_node = _relay_node()
    conn = RelayedPeerConnection(
        peer_id=TARGET_PEER_ID,
        relay_node=relay_node,
        relay_connection=MagicMock(send=AsyncMock()),
        session_id="session-xyz",
        relay_manager=manager,
    )
    manager._active_relay_connections[TARGET_PEER_ID] = conn
    assert conn.disconnect_acked is False

    handler = RelayDisconnectAckHandler(service=SimpleNamespace(relay_manager=manager))
    await handler.handle(RELAY_NODE_ID, {"session_id": "session-xyz", "status": "cleaned_up"})

    assert conn.disconnect_acked is True
    assert conn.disconnect_ack_status == "cleaned_up"


@pytest.mark.asyncio
async def test_an_unrelated_error_shape_is_left_alone():
    """An ERROR whose code the relay handlers never send must not be
    consumed as if it were a relay failure — no pending register or
    forward may be touched by it."""
    manager, _p2p = _relay_manager()
    relay_node = _relay_node()

    connect_task = asyncio.create_task(
        manager.connect_via_relay(TARGET_PEER_ID, relay_node)
    )
    await _wait_until(lambda: TARGET_PEER_ID in manager._pending_relay_sessions)

    handler = RelayErrorHandler(service=SimpleNamespace(relay_manager=manager))
    await handler.handle(RELAY_NODE_ID, {
        "error": "invalid_signature",  # not one of the codes relay handlers send
        "message": "unrelated subsystem's complaint",
    })

    future = manager._pending_relay_sessions[TARGET_PEER_ID]
    assert not future.done(), "an unrecognised ERROR code settled a relay Future"

    # Clean up the still-pending task rather than letting it leak.
    connect_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await connect_task
