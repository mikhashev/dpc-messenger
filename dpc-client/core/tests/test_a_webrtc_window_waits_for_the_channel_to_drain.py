"""On WebRTC a window of file data waits for the channel's buffer to drain.

aiortc's `RTCDataChannel.send` queues whatever it is given and never pushes
back, and `WebRTCPeerConnection.send` never looked at the buffer (ADR-047,
"On WebRTC ... `send` does not look at the channel's buffer"). A window
written that way would queue a whole file in memory and every chat frame
behind it. The data lane's drain on WebRTC waits until `bufferedAmount` is at
most WEBRTC_DRAIN_LOW_BYTES, woken by aiortc's `bufferedamountlow` event.

A stand-in channel with aiortc's names (pyee-style `on` / `remove_listener`);
no network, no aiortc peer connection; the same on every OS.
"""

import asyncio

import pytest

from dpc_client_core import webrtc_peer
from dpc_client_core.webrtc_peer import WEBRTC_DRAIN_LOW_BYTES, WebRTCPeerConnection


class _Channel:
    def __init__(self):
        self.readyState = "open"
        self.bufferedAmount = 0
        self.bufferedAmountLowThreshold = 0
        self.sent = []
        self._listeners = {}

    def send(self, data):
        self.sent.append(data)
        self.bufferedAmount += len(data)

    def on(self, event, f):
        self._listeners.setdefault(event, []).append(f)

    def remove_listener(self, event, f):
        self._listeners[event].remove(f)

    def flush_to(self, amount):
        crossed = self.bufferedAmount > self.bufferedAmountLowThreshold >= amount
        self.bufferedAmount = amount
        if crossed:
            for f in list(self._listeners.get("bufferedamountlow", [])):
                f()


def _peer(channel):
    peer = WebRTCPeerConnection.__new__(WebRTCPeerConnection)
    peer.node_id = "dpc-node-" + "d" * 32
    peer.data_channel = channel
    return peer


@pytest.mark.asyncio
async def test_a_drain_returns_at_once_when_little_is_queued():
    channel = _Channel()
    peer = _peer(channel)
    assert peer.write_nowait({"command": "FILE_CHUNK", "payload": {"data": "x" * 100}}) > 100
    await asyncio.wait_for(peer.drain(), 1)


@pytest.mark.asyncio
async def test_a_drain_waits_until_the_channel_reports_its_buffer_low():
    channel = _Channel()
    peer = _peer(channel)
    peer.write_nowait({"command": "FILE_CHUNK", "payload": {"data": "x" * (4 * WEBRTC_DRAIN_LOW_BYTES)}})
    drain = asyncio.create_task(peer.drain())
    await asyncio.sleep(0.05)
    assert not drain.done()
    assert channel.bufferedAmountLowThreshold == WEBRTC_DRAIN_LOW_BYTES
    channel.flush_to(WEBRTC_DRAIN_LOW_BYTES // 2)
    await asyncio.wait_for(drain, 1)
    assert channel._listeners["bufferedamountlow"] == []  # nothing left behind


@pytest.mark.asyncio
async def test_a_channel_that_closes_while_draining_ends_the_window_with_an_error(monkeypatch):
    monkeypatch.setattr(webrtc_peer, "WEBRTC_DRAIN_RECHECK_S", 0.02)
    channel = _Channel()
    peer = _peer(channel)
    peer.write_nowait({"command": "FILE_CHUNK", "payload": {"data": "x" * (4 * WEBRTC_DRAIN_LOW_BYTES)}})
    drain = asyncio.create_task(peer.drain())
    await asyncio.sleep(0.01)
    channel.readyState = "closed"
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(drain, 1)


def test_a_write_on_a_closed_channel_is_refused():
    channel = _Channel()
    channel.readyState = "closing"
    with pytest.raises(ConnectionError):
        _peer(channel).write_nowait({"command": "FILE_CHUNK", "payload": {}})
