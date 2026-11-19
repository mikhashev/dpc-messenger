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
behind file frames waits for them as on a TCP stream. Its pump is a timer;
on Windows the tests here raise the system timer to 1 ms while they run
(`_fine_timer`), since the data lane now waits for the transport before every
frame and a 15.6 ms tick would set the pace of a fast link instead of the
sender (a real socket wakes its writer on I/O). The bounds carry a margin for
the timer, not for a race. Same on every OS.

The review of 053157be (2026-11-20) asked for what the ADR promised: a chat
frame waits behind at most the one file frame already handed to the
transport. `ThrottledLink.ahead_of_priority` records the file bytes queued in
front of each chat frame as it is handed over; the window on a slow link with
a busy loop and no rate known, the cancelled or failed drain, and the
selector loop that never suspended a writer are below.
"""

import asyncio
import contextlib
import socket
import statistics
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from dpc_client_core.managers import chunk_window as cw
from dpc_client_core.p2p_manager import P2PManager, PeerConnection
from dpc_client_core.send_lanes import SendLanes, lanes_of
from tests.perf import file_transfer_throughput as bench

MB = 1_000_000
# Two turns of a coarse (15.6 ms) timer on the way, and the loop's own scheduling.
TIMER_MARGIN_S = 0.06


@pytest.fixture(autouse=True)
def _fine_timer():
    """Windows: a 1 ms system timer while the test runs, for the in-process link's pump."""
    if sys.platform != "win32":
        yield
        return
    import ctypes
    ctypes.windll.winmm.timeBeginPeriod(1)
    try:
        yield
    finally:
        ctypes.windll.winmm.timeEndPeriod(1)


async def _transfer_with_chat(tmp_path, rate_bps, size, settings=None, probe_s=0.1, monkeypatch=None,
                              load=False):
    monkeypatch.setattr(bench, "CHAT_PROBE_INTERVAL_S", probe_s)
    sender = bench.Sender(tmp_path, settings or {})
    receiver = bench.Receiver(tmp_path, settings or {})
    forward = bench.ThrottledLink(bench.RECEIVER, rate_bps, receiver.dispatch)
    back = bench.ThrottledLink(bench.SENDER, rate_bps, sender.dispatch)
    sender.peers[bench.RECEIVER], receiver.peers[bench.SENDER] = forward, back
    outbox = tmp_path / "outbox"
    outbox.mkdir(exist_ok=True)
    path = bench.make_file(outbox, size, "f.bin")
    stop_load = asyncio.Event()
    loader = asyncio.get_running_loop().create_task(bench.loop_load(stop_load)) if load else None
    windows = []
    note = sender.manager._note_window

    def noting(node_id, tier, transfer, window, elapsed_s):
        windows.append(window)
        return note(node_id, tier, transfer, window, elapsed_s)

    monkeypatch.setattr(sender.manager, "_note_window", noting)
    try:
        row = await sender.send(path, 120, receiver.chat_waits, lambda: receiver.offers)
    finally:
        stop_load.set()
        if loader is not None:
            await loader
        forward.close()
        back.close()
    assert not row["timed_out"] and not row["errors"] and not receiver.errors, row
    row["ahead_of_chat"] = list(forward.ahead_of_priority)
    row["window"] = windows[0] if windows else None
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
    # Measured, and remembered for the next file. Well below the link's 40 MB/s:
    # the in-process pump runs on a timer (15.6 ms on Windows when the 1 ms
    # request is not honoured), and the lane waits for it before each frame.
    assert rate and rate > 2 * MB


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
    assert max(row["ahead_of_chat"]) <= ONE_FRAME_BYTES, row["ahead_of_chat"]

    # The next file to the same peer is cut to what the link moves in the SLO
    remembered = sender.manager.link_rates.get(bench.RECEIVER, "direct_tls")
    assert remembered and remembered < rate
    assert sender.manager.chunk_size_for(400_000, [bench.RECEIVER]) == 32 * 1024


# One FILE_CHUNK of 64 KiB as the link counts it: base64, JSON and the 10-byte header.
ONE_FRAME_BYTES = 64 * 1024 * 4 // 3 + 200


@pytest.mark.asyncio
async def test_with_no_rate_known_and_a_busy_loop_a_slow_link_still_puts_one_frame_before_chat(
        tmp_path, monkeypatch):
    """0.3 MB/s, no rate remembered, the sender's loop held 0.4 s a turn (bench.loop_load), the
    turn Zcode measured on a node busy with a model (2026-11-19).

    053157be let windows the loop held up grow with no rate measured, toward
    8 MiB; 9f8538f0 to the unknown-rate budget, 1 MiB: all fourteen chunks
    of this file could wait in the transport in front of chat. Now the
    transport's buffer is read after each such window (send_lanes.
    TransportReader) and only what it shows the link clearing earns more. On
    a loop this slow the reading lands two or three turns late, 0.8-1.2 s,
    in which this link moves three or four chunks: that is as far as the
    cold start can probe here (readings later than 2.5 turns are scaled
    down, LATE_READING_TURNS). A chat frame's wait is what is ahead of it on
    the link plus the turns of the busy loop it needs to be sent and read,
    which it pays with or without a file.
    """
    rate = 300_000
    monkeypatch.setattr(bench, "LOAD_SLICE_S", 0.02)  # 20 tasks x 20 ms: a 0.4 s turn
    row, sender = await _transfer_with_chat(tmp_path, rate, 900_000, probe_s=0.3, monkeypatch=monkeypatch,
                                            load=True)
    window = row["window"]
    print(f"ahead of chat {row['ahead_of_chat']}, waits {[round(w, 2) for w in row['chat_waits']]}, "
          f"peak window {window.peak_chunks} chunks, {window.loop_limited_windows} loop-limited, "
          f"{getattr(window, 'link_behind_windows', '-')} found the link behind, rate {window.rate.bytes_per_second()}")
    assert row["chunk_size"] == 64 * 1024
    assert window.rate.measured and window.rate.bytes_per_second() <= rate  # 9f8538f0: none
    ahead = row["ahead_of_chat"]
    assert len(ahead) >= 6, row
    assert max(ahead) <= window.peak_chunks * ONE_FRAME_BYTES, (ahead, window.peak_chunks)
    assert max(ahead) <= 4 * ONE_FRAME_BYTES, ahead  # 9f8538f0: up to 16 frames
    # The wait: that one frame's transmit time, and the turns of the busy loop
    # the probe needs to be sent, carried and read (four at the most here).
    one_frame_s = ONE_FRAME_BYTES / rate
    turn_s = bench.LOAD_TASKS * bench.LOAD_SLICE_S
    waits = row["chat_waits"]
    assert max(waits) <= window.peak_chunks * one_frame_s + 4 * turn_s + TIMER_MARGIN_S, (one_frame_s, waits)
    assert statistics.median(waits) <= one_frame_s + 2 * turn_s + TIMER_MARGIN_S, waits


@pytest.mark.asyncio
async def test_a_busy_loop_on_a_link_slower_than_its_turns_keeps_one_frame_ahead_of_chat(tmp_path, monkeypatch):
    """0.3 MB/s, the loop held 0.1 s a turn (the benchmark's --load): one 64 KiB chunk takes
    three turns of this link, so the first reading finds the link behind. The allowance
    stays 0 — one frame ahead of chat — and the rate is what the link took, not the loop's
    pace (74c2de84 remembered 0.12 MB/s for this link, the loop's pace)."""
    rate = 300_000
    row, sender = await _transfer_with_chat(tmp_path, rate, 400_000, probe_s=0.15, monkeypatch=monkeypatch,
                                            load=True)
    window = row["window"]
    print(f"ahead of chat {row['ahead_of_chat']}, peak window {window.peak_chunks}, "
          f"{getattr(window, 'link_behind_windows', '-')}/{window.windows} found the link behind, rate {window.rate.bytes_per_second()}")
    measured = window.rate.bytes_per_second()
    # File bytes of a 0.3 MB/s framed link: about 225 KB/s (74c2de84: 0.12 MB/s, the loop's pace)
    assert measured and 0.8 * rate * 0.75 <= measured <= rate, measured
    assert window.link_behind and window.peak_chunks == 1
    assert max(row["ahead_of_chat"]) <= ONE_FRAME_BYTES, row["ahead_of_chat"]


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
    # Frame by frame: the transport below its mark before each one
    assert peer.got == [("send", "SEND_TEXT"),
                        ("drain", None), ("write", "FILE_CHUNK"),
                        ("drain", None), ("write", "FILE_CHUNK"),
                        ("drain", None), ("write", "FILE_CHUNK"), ("drain", None)]
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
    one_window = ["drain", "write", "drain", "write", "drain"]
    assert [kind for kind, _ in peer.got] == one_window + one_window


def test_the_lanes_live_on_the_connection_and_a_new_connection_gets_new_ones():
    a, b = SimpleNamespace(), SimpleNamespace()
    assert lanes_of(a) is lanes_of(a)
    assert lanes_of(a) is not lanes_of(b)
    assert isinstance(lanes_of(object()), SendLanes)  # a stand-in without attributes still works


class _HeldPeer(_Peer):
    """A connection whose transport stays above its mark after the first frame until `release`d, or fails."""

    def __init__(self, fail_with=None):
        super().__init__()
        self.release = asyncio.Event()
        self.fail_with = fail_with
        self.writes = 0

    def write_nowait(self, message):
        self.writes += 1
        self.got.append(("write", message["command"]))
        return 1

    async def drain(self):
        if self.writes:
            if self.fail_with is not None:
                await asyncio.sleep(0.01)
                raise self.fail_with
            await self.release.wait()


@pytest.mark.asyncio
async def test_a_window_the_transport_does_not_take_in_time_gives_up_the_lane():
    """053157be awaited drain() without a bound: a peer that stopped reading held the lane
    until the connection died. Now the wait ends at drain_timeout_s."""
    peer = _HeldPeer()
    p2p = _p2p(peer)
    started = time.perf_counter()
    result = await asyncio.wait_for(
        p2p.send_data_window("peer", [{"command": "FILE_CHUNK"}] * 3, drain_timeout_s=0.1), 5)
    assert result.timed_out and result.frames == 1
    assert 0.09 <= time.perf_counter() - started < 2
    assert not lanes_of(peer)._data_turn.locked()  # the lane is free again


@pytest.mark.asyncio
async def test_a_window_cancelled_mid_drain_frees_the_lane_and_the_next_window_goes():
    peer = _HeldPeer()
    p2p = _p2p(peer)
    window = asyncio.create_task(p2p.send_data_window("peer", [{"command": "FILE_CHUNK"}] * 3))
    await asyncio.sleep(0.05)
    assert not window.done() and lanes_of(peer)._data_turn.locked()
    window.cancel()
    with pytest.raises(asyncio.CancelledError):
        await window
    assert not lanes_of(peer)._data_turn.locked()
    peer.release.set()
    result = await asyncio.wait_for(p2p.send_data_window("peer", [{"command": "FILE_CHUNK"}] * 2), 1)
    assert result.frames == 2 and not result.timed_out


@pytest.mark.asyncio
async def test_a_connection_lost_mid_drain_fails_the_window_and_a_reconnect_gets_a_free_lane():
    peer = _HeldPeer(fail_with=ConnectionResetError("peer went away"))
    p2p = _p2p(peer)
    with pytest.raises(ConnectionResetError):
        await asyncio.wait_for(p2p.send_data_window("peer", [{"command": "FILE_CHUNK"}] * 3), 1)
    assert not lanes_of(peer)._data_turn.locked()
    new = _Peer()
    p2p.peers["peer"] = new  # the reconnect's new connection object, with lanes of its own
    assert lanes_of(new) is not lanes_of(peer)
    result = await asyncio.wait_for(p2p.send_data_window("peer", [{"command": "FILE_CHUNK"}] * 2), 1)
    assert result.frames == 2 and [k for k, _ in new.got].count("write") == 2


# --- a selector loop never suspends a writer whose socket keeps taking data -----------


@contextlib.contextmanager
def _draining_socket():
    """A listening socket whose one connection a thread reads and discards as fast as it can."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def drain_it():
        try:
            conn, _ = server.accept()
        except OSError:
            return
        with conn:
            while True:
                try:
                    if not conn.recv(1 << 20):
                        return
                except OSError:
                    return

    reader = threading.Thread(target=drain_it, daemon=True)
    reader.start()
    try:
        yield server.getsockname()[1]
    finally:
        server.close()
        reader.join(5)


def _on_a_selector_loop(coro_fn):
    """Run on asyncio.SelectorEventLoop: the default on Linux and macOS, chosen on Windows too."""
    loop = asyncio.SelectorEventLoop()
    try:
        return loop.run_until_complete(coro_fn())
    finally:
        loop.close()


def test_on_a_selector_loop_a_window_lets_the_loop_turn_before_it_returns():
    """A call queued before the window runs before the window returns, though nothing in
    the window had to wait: the socket took every frame at once."""
    async def main():
        with _draining_socket() as port:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            p2p = _p2p(PeerConnection("peer", reader, writer, direction="out"))
            ran = []
            asyncio.get_running_loop().call_soon(ran.append, True)
            result = await p2p.send_data_window("peer", [{"command": "FILE_CHUNK", "payload": {"data": "x"}}] * 4)
            ran_by_then = list(ran)  # read here: the loop runs what is queued once main() returns
            writer.close()
            return ran_by_then, result

    ran, result = _on_a_selector_loop(main)
    assert result.frames == 4
    assert ran == [True]


def test_on_a_selector_loop_a_large_upload_lets_other_work_run(tmp_path):
    """60 MB over a real PeerConnection to a socket a thread empties as fast as the sender
    fills it, on a selector loop. In 053157be the upload never suspended there (an
    uncontended lock, a drain under the mark and, with chunk_delay 0, no pause), so a
    timer due every 10 ms waited for the whole file. Now every window ends with a turn
    of the loop, and its reading and framing run in a thread."""
    async def main():
        with _draining_socket() as port:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            sender = bench.Sender(tmp_path, {})
            sender.peers[bench.RECEIVER] = PeerConnection(bench.RECEIVER, reader, writer, direction="out")
            outbox = tmp_path / "outbox"
            outbox.mkdir()
            path = bench.make_file(outbox, 60 * MB, "big.bin")
            transfer_id = await sender.manager.send_file(bench.RECEIVER, path)
            gaps, stop = [], asyncio.Event()

            async def ticker():
                last = time.perf_counter()
                while not stop.is_set():
                    await asyncio.sleep(0.01)
                    now = time.perf_counter()
                    gaps.append(now - last)
                    last = now

            ticking = asyncio.get_running_loop().create_task(ticker())
            await asyncio.sleep(0.05)
            gaps.clear()
            started = time.perf_counter()
            await sender.manager.handle_file_accept(bench.RECEIVER, transfer_id)
            took = time.perf_counter() - started
            print(f"upload {took:.2f}s, {len(gaps)} ticks, longest gap {max(gaps or [0]) * 1000:.0f} ms")
            stop.set()
            await ticking
            writer.close()
            return gaps, took, sender.manager.active_transfers[transfer_id]

    gaps, took, transfer = _on_a_selector_loop(main)
    assert transfer.status.value == "transferring", transfer.status  # every chunk went, none failed
    assert len(gaps) >= 3, (gaps, took)
    assert max(gaps) <= 0.1, (max(gaps), took)


# --- the review of 9f8538f0 (a fresh subagent and Zcode, 2026-11-20) --------------------


class _MarkedPeer(_HeldPeer):
    """A held connection that records the write mark each window sets."""

    def set_write_allowance(self, nbytes):
        self.got.append(("allow", nbytes))


@pytest.mark.asyncio
async def test_one_uploads_allowance_never_applies_to_another_uploads_window_or_a_resent_chunk():
    """Two uploads to one peer: 9f8538f0 set the mark before taking the lane, so A's
    loop-limited window raised it while B held the lane on a slow window, and chat waited
    behind megabytes of B; a re-sent chunk's allow(0) cut into A's window the same way."""
    peer = _MarkedPeer()
    p2p = _p2p(peer)
    b = asyncio.create_task(p2p.send_data_window("peer", [{"command": "B"}] * 2, allowance_bytes=0))
    await asyncio.sleep(0.05)
    assert lanes_of(peer)._data_turn.locked()  # B holds the lane, its link slow
    a = asyncio.create_task(p2p.send_data_window("peer", [{"command": "A"}] * 2, allowance_bytes=4_000_000))
    resend = asyncio.create_task(p2p.send_data_window("peer", [{"command": "RESEND"}], allowance_bytes=0))
    await asyncio.sleep(0.05)
    assert peer.got == [("allow", 0), ("write", "B")]  # neither A's mark nor the resend's yet
    peer.release.set()
    await asyncio.gather(b, a, resend)
    kinds = [entry for entry in peer.got if entry[0] in ("allow", "write")]
    assert kinds == [("allow", 0), ("write", "B"), ("write", "B"),
                     ("allow", 4_000_000), ("write", "A"), ("write", "A"),
                     ("allow", 0), ("write", "RESEND")]


class _TurnPeer(_Peer):
    """A connection that takes every frame at once, but whose drain costs a turn of the loop."""

    async def drain(self):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_window_handed_over_frame_by_frame_charges_the_loop_for_every_wait():
    """9f8538f0 counted two turns of the loop per window, but the data lane drains before
    every frame: a window of 8 waits 9 times. On a busy loop the other seven turns read as
    link time, the loop's pace became the link's rate, and it was remembered for 10 min.
    The lag is now summed over every wait that suspended."""
    stop = asyncio.Event()

    async def busy():
        while not stop.is_set():
            time.sleep(0.01)
            await asyncio.sleep(0)

    loaders = [asyncio.get_running_loop().create_task(busy()) for _ in range(2)]  # a 20 ms turn
    await asyncio.sleep(0)
    window = cw.ChunkWindow(64 * 1024)
    try:
        p2p = _p2p(_TurnPeer())
        for _ in range(6):
            result = await p2p.send_data_window("peer", [{"command": "FILE_CHUNK"}] * 8)
            window.on_window(8 * 64 * 1024, result.elapsed_s, result.loop_lag_s)
    finally:
        stop.set()
        await asyncio.gather(*loaders)
    assert result.loop_lag_s >= cw.LOOP_LIMITED_SHARE * result.elapsed_s, result
    assert window.loop_limited_windows == 6
    assert window.rate.bytes_per_second() is None  # the loop's pace never read as the link's


@pytest.mark.asyncio
async def test_a_window_hands_over_nothing_once_the_transfer_is_over():
    """Even its first frame: a cancel that came while the window waited for the transport."""
    peer = _Peer()
    p2p = _p2p(peer)
    result = await p2p.send_data_window("peer", [{"command": "FILE_CHUNK"}] * 3, still_sending=lambda: False)
    assert result.frames == 0 and ("write", "FILE_CHUNK") not in peer.got


@pytest.mark.asyncio
async def test_a_busy_loop_on_a_fast_link_grows_the_window_past_one_chunk_and_learns_the_rate(
        tmp_path, monkeypatch):
    """1 MiB chunks (a file over 100 MiB, no rate yet; pinned here to keep the file small),
    the sender's loop held 0.1 s a turn, a 40 MB/s link. At 9f8538f0 every window was one
    chunk, loop-limited, and gave no sample: the reviewer's 200 MB ran at 2.44 MB/s with
    "window up to 1 chunks, link rate not measured". The transport's buffer now says the
    link took each window within a turn: the window grows and the link's rate is learned."""
    row, sender = await _transfer_with_chat(tmp_path, 40 * MB, 30 * MB, settings={"chunk_size": "1MiB"},
                                            monkeypatch=monkeypatch, load=True)
    window = row["window"]
    print(f"{row['mb_per_s']:.2f} MB/s, peak window {window.peak_chunks} chunks, "
          f"{window.loop_limited_windows}/{window.windows} loop-limited, "
          f"{getattr(window, 'link_behind_windows', '-')} found the link behind, rate {window.rate.bytes_per_second()}")
    assert row["chunk_size"] == 1024 * 1024
    assert window.peak_chunks > 1
    assert window.rate.measured
    assert sender.manager.link_rates.get(bench.RECEIVER, "direct_tls")
