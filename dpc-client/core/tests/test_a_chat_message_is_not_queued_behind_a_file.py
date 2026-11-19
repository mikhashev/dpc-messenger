"""A chat message sent during a file transfer is not queued behind the file.

File chunks travel on the connection that carries chat (card
FILE-CHUNKS-TRAVEL-AS-64-KB-BASE64-JSON-ON-THE-CHAT-CHANNEL). With larger
chunks and windows of them (ADR-047 phase 0, and the window and two-lane
queue of phase 2), chat must stay ahead: every message other than file data
goes on the priority lane, file data waits for it, and a window holds no more
than the link moves in half of CHAT_WAIT_SLO_S (send_lanes.py,
managers/chunk_window.py). The SLO, Zcode's point 2 of 2026-11-19: a chat
message waits at most about 200 ms during a transfer on a fast link, and at
most one frame's transmit time on a slow one.

The real FileTransferManager, FILE_OFFER handler and P2PManager send path on
both sides, joined by `ThrottledLink` (tests/perf/file_transfer_throughput.py),
an in-process link that moves a fixed number of bytes a second in order and
delivers a frame whole when its last byte has crossed, so a chat frame written
behind file frames waits for them as on a TCP stream. Its virtual clock keeps
the rate exact under Windows' 15.6 ms timer; the bounds carry a margin for that
timer, not for a race. Same on every OS.
"""

import asyncio
import statistics
from types import SimpleNamespace

import pytest

from dpc_client_core.managers import chunk_window as cw
from dpc_client_core.p2p_manager import P2PManager
from dpc_client_core.send_lanes import SendLanes, lanes_of
from tests.perf import file_transfer_throughput as bench

MB = 1_000_000
# Two turns of a coarse (15.6 ms) timer on the way, and the loop's own scheduling.
TIMER_MARGIN_S = 0.06


async def _transfer_with_chat(tmp_path, rate_bps, size, settings=None, probe_s=0.1, monkeypatch=None):
    monkeypatch.setattr(bench, "CHAT_PROBE_INTERVAL_S", probe_s)
    sender = bench.Sender(tmp_path, settings or {})
    receiver = bench.Receiver(tmp_path, settings or {})
    forward = bench.ThrottledLink(bench.RECEIVER, rate_bps, receiver.dispatch)
    back = bench.ThrottledLink(bench.SENDER, rate_bps, sender.dispatch)
    sender.peers[bench.RECEIVER], receiver.peers[bench.SENDER] = forward, back
    outbox = tmp_path / "outbox"
    outbox.mkdir(exist_ok=True)
    path = bench.make_file(outbox, size, "f.bin")
    try:
        row = await sender.send(path, 60, receiver.chat_waits, lambda: receiver.offers)
    finally:
        forward.close()
        back.close()
    assert not row["timed_out"] and not row["errors"] and not receiver.errors, row
    return row, sender


@pytest.mark.asyncio
async def test_on_a_fast_link_a_chat_message_waits_at_most_the_slo(tmp_path, monkeypatch):
    """40 MB/s: the window fills to its budget, and chat still passes within ~200 ms."""
    row, sender = await _transfer_with_chat(tmp_path, 40 * MB, 30 * MB, monkeypatch=monkeypatch)
    waits = row["chat_waits"]
    assert len(waits) >= 5, row
    assert max(waits) <= cw.CHAT_WAIT_SLO_S + TIMER_MARGIN_S, waits
    assert statistics.median(waits) <= cw.CHAT_WAIT_SLO_S, waits
    rate = sender.manager.link_rates.get(bench.RECEIVER, "direct_tls")
    assert rate and rate > 10 * MB  # measured, and remembered for the next file


@pytest.mark.asyncio
async def test_on_a_slow_link_a_chat_message_waits_at_most_one_frame(tmp_path, monkeypatch):
    """300 KB/s: one 64 KiB chunk (about 88 KB framed) holds the link about 0.29 s."""
    rate = 300_000
    row, sender = await _transfer_with_chat(tmp_path, rate, 400_000, probe_s=0.15, monkeypatch=monkeypatch)
    one_frame_s = (64 * 1024 * 4 / 3 + 200) / rate
    waits = row["chat_waits"]
    assert len(waits) >= 5, row
    assert max(waits) <= one_frame_s + TIMER_MARGIN_S, (one_frame_s, waits)
    assert row["chunk_size"] == 64 * 1024

    # The next file to the same peer is cut to what the link moves in the SLO
    remembered = sender.manager.link_rates.get(bench.RECEIVER, "direct_tls")
    assert remembered and remembered < rate
    assert sender.manager.chunk_size_for(400_000, [bench.RECEIVER]) == 32 * 1024


# --- the lanes ---------------------------------------------------------------------


class _Peer:
    """A connection that records what reached it; `send` may be held open."""

    connection_type = "direct_tls"

    def __init__(self, with_window_writes=True):
        self.got = []
        self.hold = None
        if not with_window_writes:
            self.write_nowait = None

    async def send(self, message):
        if self.hold is not None:
            await self.hold.wait()
        self.got.append(("send", message["command"]))

    def write_nowait(self, message):
        self.got.append(("write", message["command"]))
        return 1

    async def drain(self):
        self.got.append(("drain", None))


def _p2p(peer):
    p2p = P2PManager.__new__(P2PManager)
    p2p.node_id = "dpc-node-" + "a" * 32
    p2p.peers = {"peer": peer}
    return p2p


@pytest.mark.asyncio
async def test_a_window_waits_for_a_chat_frame_already_on_its_way():
    peer = _Peer()
    p2p = _p2p(peer)
    peer.hold = asyncio.Event()
    chat = asyncio.create_task(p2p.send_message_to_peer("peer", {"command": "SEND_TEXT"}))
    await asyncio.sleep(0)
    window = asyncio.create_task(p2p.send_data_window("peer", [{"command": "FILE_CHUNK"}] * 3))
    await asyncio.sleep(0.05)
    assert peer.got == []  # the chat frame is still on its way: no file frame yet
    peer.hold.set()
    await chat
    result = await window
    assert peer.got == [("send", "SEND_TEXT"), ("write", "FILE_CHUNK"), ("write", "FILE_CHUNK"),
                        ("write", "FILE_CHUNK"), ("drain", None)]
    assert result.frames == 3 and result.elapsed_s >= 0 and result.loop_lag_s >= 0


@pytest.mark.asyncio
async def test_on_a_connection_without_window_writes_chat_cuts_in_between_frames():
    """A relayed or UDP connection awaits each frame; a chat frame goes between two of them."""
    peer = _Peer(with_window_writes=False)
    p2p = _p2p(peer)
    sent = []

    async def slow_send(message):
        sent.append(message["command"])
        if message["command"] == "FILE_CHUNK" and sent.count("FILE_CHUNK") == 1:
            asyncio.get_running_loop().create_task(p2p.send_message_to_peer("peer", {"command": "SEND_TEXT"}))
        await asyncio.sleep(0.01)

    peer.send = slow_send
    result = await p2p.send_data_window("peer", [{"command": "FILE_CHUNK"}] * 3)
    assert result.frames == 3
    assert sent.index("SEND_TEXT") < len(sent) - 1  # before the last file frame


@pytest.mark.asyncio
async def test_a_window_on_such_a_connection_stops_when_the_transfer_is_over():
    peer = _Peer(with_window_writes=False)
    p2p = _p2p(peer)
    over = SimpleNamespace(now=False)

    async def send(message):
        peer.got.append(message["command"])
        over.now = True

    peer.send = send
    result = await p2p.send_data_window("peer", [{"command": "FILE_CHUNK"}] * 4, still_sending=lambda: not over.now)
    assert result.frames == 1


@pytest.mark.asyncio
async def test_two_windows_on_one_connection_do_not_interleave():
    peer = _Peer()
    p2p = _p2p(peer)
    first = [{"command": "FILE_CHUNK", "n": 1}] * 2
    second = [{"command": "FILE_CHUNK", "n": 2}] * 2
    await asyncio.gather(p2p.send_data_window("peer", first), p2p.send_data_window("peer", second))
    assert [kind for kind, _ in peer.got] == ["write", "write", "drain", "write", "write", "drain"]


def test_the_lanes_live_on_the_connection_and_a_new_connection_gets_new_ones():
    a, b = SimpleNamespace(), SimpleNamespace()
    assert lanes_of(a) is lanes_of(a)
    assert lanes_of(a) is not lanes_of(b)
    assert isinstance(lanes_of(object()), SendLanes)  # a stand-in without attributes still works
