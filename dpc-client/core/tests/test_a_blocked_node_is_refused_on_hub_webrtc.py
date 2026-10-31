"""A blocked node is refused on Hub WebRTC, in both directions.

The Hub relays an offer from any node it knows. A blocked node's offer gets no
answer (silent, as on the direct path), its ICE candidates are not buffered,
a connection whose node was blocked while ICE ran is closed instead of entered
into `peers`, and this node never sends an offer to a blocked node.

Real `P2PManager` WebRTC methods on an instance built without `__init__`
(which reads ~/.dpc); the aiortc peer is a double that records what it is asked.
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core import p2p_manager as p2p_mod
from dpc_client_core.p2p_manager import P2PManager

BLOCKED = "dpc-node-" + "b" * 32
FRIEND = "dpc-node-c" + "0" * 31


class _Firewall:
    def __init__(self, *blocked):
        self.blocked = set(blocked)

    def is_blocked(self, node_id):
        return node_id in self.blocked


class _RTC:
    def __init__(self, node_id, is_initiator):
        self.node_id = node_id
        self.closed = False
        self.sent = []

    async def handle_offer(self, sdp):
        return {"type": "answer", "sdp": "v=0"}

    async def create_offer(self):
        return {"type": "offer", "sdp": "v=0"}

    async def add_ice_candidate(self, candidate):
        pass

    async def wait_ready(self, timeout):
        pass

    async def send(self, message):
        self.sent.append(message)

    async def close(self):
        self.closed = True


@pytest.fixture
def manager(monkeypatch):
    monkeypatch.setattr(p2p_mod, "WebRTCPeerConnection", _RTC)
    m = P2PManager.__new__(P2PManager)
    m.node_id = "dpc-node-" + "a" * 32
    m.display_name = "self"
    m.settings = None
    m.firewall = _Firewall(BLOCKED)
    m.peers = {}
    m._pending_webrtc = {}
    m._ice_candidates_buffer = {}
    m._hub_client_refs = {}
    m._intentional_disconnects = set()
    m.on_peer_list_change = None
    m.on_message_received = None
    m.on_peer_disconnected = None
    m._handle_connection_timeout = AsyncMock()
    return m


def _hub():
    return SimpleNamespace(send_signal=AsyncMock())


@pytest.mark.asyncio
async def test_a_blocked_nodes_offer_gets_no_answer(manager, caplog):
    hub = _hub()
    caplog.set_level(logging.INFO, logger="dpc_client_core.p2p_manager")

    await manager._handle_webrtc_offer(BLOCKED, {"offer": {"sdp": "v=0"}}, hub)

    hub.send_signal.assert_not_awaited()
    assert BLOCKED not in manager._pending_webrtc
    lines = [r.getMessage() for r in caplog.records if "Refused blocked node" in r.getMessage()]
    assert len(lines) == 1 and BLOCKED in lines[0] and "Hub WebRTC offer" in lines[0]


@pytest.mark.asyncio
async def test_an_unblocked_nodes_offer_is_answered(manager):
    hub = _hub()

    await manager._handle_webrtc_offer(FRIEND, {"offer": {"sdp": "v=0"}}, hub)

    hub.send_signal.assert_awaited_once()
    assert hub.send_signal.await_args.args[1]["type"] == "answer"


@pytest.mark.asyncio
async def test_a_blocked_nodes_ice_candidates_are_not_kept(manager):
    await manager._handle_ice_candidate(BLOCKED, {"candidate": {"c": 1}})
    await manager._handle_ice_candidate(FRIEND, {"candidate": {"c": 1}})

    assert BLOCKED not in manager._ice_candidates_buffer
    assert manager._ice_candidates_buffer[FRIEND] == [{"c": 1}]


@pytest.mark.asyncio
@pytest.mark.parametrize("node_id, entered", [(BLOCKED, False), (FRIEND, True)])
async def test_a_connection_blocked_while_ice_ran_is_closed_not_entered(manager, node_id, entered):
    rtc = _RTC(node_id, is_initiator=False)
    manager._pending_webrtc[node_id] = rtc

    await manager._finalize_webrtc_connection(node_id)

    assert (node_id in manager.peers) is entered
    assert rtc.closed is (not entered)


@pytest.mark.asyncio
async def test_no_offer_goes_to_a_blocked_node(manager):
    hub = _hub()

    with pytest.raises(ConnectionError, match="Unblock it first"):
        await manager.connect_via_hub(BLOCKED, hub)

    hub.send_signal.assert_not_awaited()
    assert BLOCKED not in manager._pending_webrtc


@pytest.mark.asyncio
async def test_an_offer_goes_to_an_unblocked_node(manager):
    hub = _hub()

    await manager.connect_via_hub(FRIEND, hub)

    hub.send_signal.assert_awaited_once()
    assert hub.send_signal.await_args.args[1]["type"] == "offer"
