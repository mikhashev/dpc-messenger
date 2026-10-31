"""A blocked node is refused on the volunteer relay, on gossip and by every dial strategy.

The other doors that admit a peer or deliver a message as coming from it:

- relay, server side: no session through us has a blocked node at either end;
- relay, client side: no dial through a relay to a blocked node, and a
  relayed message from one is not dispatched;
- gossip: a message addressed to us whose signed origin is blocked is not
  delivered, by the origin its certificate proves, not the hop;
- the connection orchestrator, which every strategy (IPv6, IPv4, Hub WebRTC,
  UDP hole punch, relay, gossip) dials through;
- the service's incoming-message callback, for a session that outlives its
  Block by a frame.

Each with the same thing from an unblocked node beside it. Real managers and
the real `P2PManager.refuse_if_blocked`; transports are doubles. Addresses are
RFC 5737.
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from dpc_client_core.coordinators.connection_orchestrator import (
    ConnectionFailedError,
    ConnectionOrchestrator,
)
from dpc_client_core.managers.relay_manager import RelayManager
from dpc_client_core.p2p_manager import P2PManager
from dpc_client_core.service import CoreService
from tests.test_gossip_signed_source import Identity, new_message, node, signed

SELF = "dpc-node-" + "a" * 32
BLOCKED = "dpc-node-" + "b" * 32
FRIEND = "dpc-node-" + "c" * 32
RELAY = "dpc-node-" + "d" * 32


class _Firewall:
    def __init__(self, *blocked):
        self.blocked = set(blocked)

    def is_blocked(self, node_id):
        return node_id in self.blocked


def _p2p(*blocked):
    """A P2PManager without __init__ (which reads ~/.dpc), with a block list."""
    m = P2PManager.__new__(P2PManager)
    m.node_id = SELF
    m.firewall = _Firewall(*blocked)
    m.peers = {}
    return m


def _refusals(caplog):
    return [r.getMessage() for r in caplog.records
            if r.levelno == logging.INFO and "Refused blocked node" in r.getMessage()]


# --- relay, server side ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_relay_session_naming_a_blocked_node_is_never_created(caplog):
    caplog.set_level(logging.INFO, logger="dpc_client_core.p2p_manager")
    relay = RelayManager(SimpleNamespace(node_id=SELF), p2p_manager=_p2p(BLOCKED), volunteer=True)

    assert await relay.handle_relay_register(FRIEND, BLOCKED, object()) is None
    assert await relay.handle_relay_register(BLOCKED, FRIEND, object()) is None

    assert relay.peer_connections == {} and relay.sessions == {}
    lines = _refusals(caplog)
    assert len(lines) == 2 and all("relay registration" in line for line in lines)


@pytest.mark.asyncio
async def test_a_registration_between_unblocked_nodes_is_admitted():
    """Positive control: two unblocked nodes registering for each other form a session."""
    relay = RelayManager(SimpleNamespace(node_id=SELF), p2p_manager=_p2p(BLOCKED), volunteer=True)
    other = "dpc-node-" + "e" * 32
    conn, other_conn = object(), object()

    assert await relay.handle_relay_register(FRIEND, other, conn) is None
    session_id = await relay.handle_relay_register(other, FRIEND, other_conn)

    assert session_id is not None and session_id in relay.sessions
    assert relay.peer_connections == {FRIEND: conn, other: other_conn}
    assert relay.peer_to_session == {FRIEND: session_id, other: session_id}


# --- relay, client side ----------------------------------------------------


@pytest.mark.asyncio
async def test_no_relayed_dial_goes_to_a_blocked_node():
    p2p = _p2p(BLOCKED)
    p2p.connect_directly = AsyncMock()
    relay = RelayManager(SimpleNamespace(node_id=SELF), p2p_manager=p2p)
    via = SimpleNamespace(node_id=RELAY, ip="192.0.2.7", port=8888)

    with pytest.raises(ConnectionError, match="Unblock it first"):
        await relay.connect_via_relay(BLOCKED, via)

    p2p.connect_directly.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_relayed_dial_to_an_unblocked_node_reaches_the_relay():
    p2p = _p2p(BLOCKED)
    p2p.connect_directly = AsyncMock(side_effect=OSError("relay unreachable"))
    relay = RelayManager(SimpleNamespace(node_id=SELF), p2p_manager=p2p)
    via = SimpleNamespace(node_id=RELAY, ip="192.0.2.7", port=8888)

    with pytest.raises(Exception):
        await relay.connect_via_relay(FRIEND, via)

    p2p.connect_directly.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("sender, dispatched", [(BLOCKED, False), (FRIEND, True)])
async def test_a_relayed_message_from_a_blocked_node_is_not_dispatched(sender, dispatched):
    relay = RelayManager(SimpleNamespace(node_id=SELF), p2p_manager=_p2p(BLOCKED))
    conn = SimpleNamespace(session_id="s-1", _dispatch_incoming=AsyncMock())
    relay._active_relay_connections[sender] = conn

    result = await relay.dispatch_incoming_relay_message(sender, "s-1", "b64")

    assert result is dispatched
    assert conn._dispatch_incoming.await_count == (1 if dispatched else 0)


# --- gossip ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_gossip_message_from_a_blocked_origin_is_not_delivered(caplog):
    caplog.set_level(logging.INFO, logger="dpc_client_core.p2p_manager")
    me, origin, friend = Identity(), Identity(), Identity()
    manager = node(me)
    gate = _p2p(origin.node_id)
    manager.p2p_manager.refuse_if_blocked = gate.refuse_if_blocked

    await manager.handle_gossip_message(signed(new_message(origin.node_id, me.node_id), origin))
    manager.message_router.route_message.assert_not_awaited()
    assert any(origin.node_id in line and "gossip delivery" in line for line in _refusals(caplog))

    await manager.handle_gossip_message(signed(new_message(friend.node_id, me.node_id), friend))
    manager.message_router.route_message.assert_awaited_once()
    assert manager.message_router.route_message.await_args.args[0] == friend.node_id


# --- the orchestrator every strategy dials through --------------------------


def _orchestrator(*blocked):
    p2p = _p2p(*blocked)
    p2p.peer_cache = Mock(get_peer=Mock(return_value=None))
    dht = SimpleNamespace(find_peer_full=AsyncMock(return_value=None))
    return ConnectionOrchestrator(p2p, dht), dht


@pytest.mark.asyncio
async def test_the_orchestrator_dials_no_strategy_to_a_blocked_node():
    orchestrator, dht = _orchestrator(BLOCKED)

    with pytest.raises(ConnectionFailedError, match="Unblock it first"):
        await orchestrator.connect(BLOCKED)

    dht.find_peer_full.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_orchestrator_still_looks_up_an_unblocked_node():
    orchestrator, dht = _orchestrator(BLOCKED)

    with pytest.raises(Exception):
        await orchestrator.connect(FRIEND, timeout=0.1)

    dht.find_peer_full.assert_awaited_once_with(FRIEND)


# --- a frame from a session that outlived its Block --------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("sender, routed", [(BLOCKED, False), (FRIEND, True)])
async def test_a_frame_from_a_blocked_node_reaches_no_handler(sender, routed):
    service = CoreService.__new__(CoreService)
    service.p2p_manager = _p2p(BLOCKED)
    service.message_router = SimpleNamespace(route_message=AsyncMock())

    await service.on_p2p_message_received(sender, {"command": "SEND_TEXT", "payload": {}})

    assert service.message_router.route_message.await_count == (1 if routed else 0)
