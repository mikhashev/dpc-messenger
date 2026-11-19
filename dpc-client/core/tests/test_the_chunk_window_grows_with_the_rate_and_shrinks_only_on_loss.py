"""The window of chunks in flight grows with the measured rate and shrinks only on a loss.

The sender used to send one chunk, await it, and sleep `chunk_delay`, so every
64 KB cost a turn of the event loop; under model load a turn was 0.4 s (Zcode,
2026-11-19: a 4 MB file in 25 s on a LAN, LINK_PONG 1.6-3.3 s loaded against
0.03-0.09 s idle). Now a window of K chunks is written and drained once
(managers/chunk_window.py). Zcode's review asked for two things checked here:
K grows by the measured delivery rate and shrinks only on a timeout, an error
or backpressure, never on latency alone — a busy loop is not a congested link;
and the round trip is kept out of the rate.

The review of 053157be (a fresh subagent and Zcode, 2026-11-20) added: a
loop-limited window grows only within the rate last measured, or 1 MiB while
none is; a halving lets the window grow back; one stall round of re-asks is
one loss, not 64; a window the transport does not take in time gives up the
lane, and a peer that takes nothing ends the upload.

Driven with numbers, no clock and no network; the same on every OS.
"""

from types import SimpleNamespace

import pytest

from dpc_client_core.managers import chunk_window as cw
from dpc_client_core.managers.chunk_window import ChunkWindow, DeliveryRate, KiB, MiB
from dpc_client_core.managers.file_transfer_manager import (
    FileTransfer, FileTransferManager, TransferStatus,
)

CHUNK = 64 * KiB


def _link_window(window: ChunkWindow, link_rate: float, overhead_s: float = 0.0, lag_s: float = 0.0):
    """One window of `window.chunks` chunks over a link of `link_rate` bytes/s."""
    nbytes = window.chunks * window.chunk_size
    elapsed = nbytes / link_rate + overhead_s + lag_s
    return window.on_window(nbytes, elapsed, lag_s)


def _measure(window: ChunkWindow, link_rate: float, seconds: float = 1.0, **kw):
    """Windows over `seconds` of link time."""
    spent = 0.0
    while spent < seconds:
        before = window.chunks * window.chunk_size / link_rate
        _link_window(window, link_rate, **kw)
        spent += before + kw.get("overhead_s", 0.0)


# --- the rate --------------------------------------------------------------------


def test_the_first_sample_is_dropped_and_short_windows_are_summed():
    rate = DeliveryRate()
    rate.add(10_000_000, 0.01)          # a kernel buffer swallowing a burst: not a sample yet
    assert rate.bytes_per_second() is None
    rate.add(1_000_000, 0.3)            # closes the first sample, which is dropped
    assert rate.bytes_per_second() is None and not rate.measured
    for _ in range(30):
        rate.add(100_000, 0.01)         # 30 short windows make one sample
    assert rate.bytes_per_second() == pytest.approx(10e6)


def test_the_rate_is_the_highest_recent_sample_and_a_seed_stands_in_until_then():
    rate = DeliveryRate(seed=2e6)
    assert rate.bytes_per_second() == 2e6 and not rate.measured
    rate.add(1, cw.RATE_SAMPLE_MIN_S)  # warm-up, dropped
    rate.add(int(5e6 * 0.3), 0.3)
    rate.add(int(1e6 * 0.3), 0.3)
    assert rate.bytes_per_second() == pytest.approx(5e6)


# --- growth --------------------------------------------------------------------------


def test_with_no_rate_measured_a_link_limited_window_stays_at_one_chunk():
    window = ChunkWindow(CHUNK)
    for _ in range(3):
        assert _link_window(window, 1e6) == "held"
    assert window.chunks == 1


def test_on_a_slow_link_the_window_stays_at_one_chunk():
    """300 KB/s: the budget (rate x SLO share) is below two chunks."""
    window = ChunkWindow(CHUNK)
    _measure(window, 300e3, seconds=5)
    assert window.rate.measured and window.chunks == 1


def test_the_window_grows_while_growing_raises_the_rate_and_stops_at_the_plateau():
    """A fixed per-window cost (a turn of the loop) makes more chunks per window pay, up to a point."""
    window = ChunkWindow(CHUNK)
    _measure(window, 100e6, seconds=20, overhead_s=0.004)
    grown = window.chunks
    assert grown > 4, grown
    assert window.plateau
    assert grown * CHUNK <= window.budget_bytes()
    _measure(window, 100e6, seconds=5, overhead_s=0.004)
    assert window.chunks == grown  # held at the plateau


def test_without_a_per_window_cost_one_growth_does_not_pay_and_the_window_holds():
    window = ChunkWindow(CHUNK)
    _measure(window, 100e6, seconds=10)
    assert window.chunks == 2 and window.plateau


def test_the_window_never_puts_more_than_the_byte_cap_in_flight():
    window = ChunkWindow(1 * MiB, link_rate=1e9)
    for _ in range(50):
        window.on_window(window.chunks * MiB, 1.0, loop_lag_s=0.9)  # loop-limited: grows
    assert window.chunks * MiB == cw.MAX_BYTES_IN_FLIGHT


def test_the_budget_is_the_rate_times_half_the_chat_slo():
    window = ChunkWindow(16 * KiB, link_rate=1e6)
    assert window.budget_bytes() == int(1e6 * cw.CHAT_WAIT_SLO_S * cw.WINDOW_SHARE_OF_SLO)
    for _ in range(20):
        window.on_window(window.chunks * 16 * KiB, 1.0, loop_lag_s=0.9)
    assert window.chunks == window.budget_bytes() // (16 * KiB) == 6


# --- never shrinks on latency ----------------------------------------------------------


def test_a_slow_window_within_its_timeout_changes_nothing():
    window = ChunkWindow(CHUNK, link_rate=100e6)
    for _ in range(4):
        window.on_window(window.chunks * CHUNK, 1.0, loop_lag_s=0.9)
    grown = window.chunks
    assert grown == 16
    for _ in range(10):
        assert window.on_window(grown * CHUNK, cw.DRAIN_TIMEOUT_FLOOR_S * 0.9) == "held"
    assert window.chunks == grown and window.timeouts == 0


def test_a_window_the_loop_held_up_gives_no_rate_sample_never_shrinks_and_grows_only_to_the_unknown_rate_budget():
    """A node busy with a model: each window waits 0.4 s for the loop, the link is fast.

    053157be let such windows grow to MAX_BYTES_IN_FLIGHT (8 MiB) with no rate
    measured, against its own docstring and ADR-047 ("grows only on measured
    delivery rate"); on a slow link that is tens of seconds of file data in
    front of chat. With no rate the bound is UNKNOWN_RATE_WINDOW_BYTES.
    """
    window = ChunkWindow(CHUNK)
    for _ in range(12):
        nbytes = window.chunks * CHUNK
        window.on_window(nbytes, 0.4 + nbytes / 100e6, loop_lag_s=0.4)
    assert window.rate.bytes_per_second() is None  # nothing measured from them
    assert window.chunks * CHUNK == cw.UNKNOWN_RATE_WINDOW_BYTES  # more per turn, but not past 1 MiB
    assert window.loop_limited_windows == 12


def test_a_loop_limited_window_grows_only_within_the_rate_last_measured():
    """A busy loop on a 300 KB/s link with that rate remembered: the window stays at one chunk."""
    window = ChunkWindow(CHUNK, link_rate=300e3)
    for _ in range(50):
        window.on_window(window.chunks * CHUNK, 1.0, loop_lag_s=0.9)
    assert window.chunks == 1  # the budget (30 KB) is below two chunks
    assert window.loop_limited_windows == 50


def test_no_window_hands_over_more_than_the_byte_cap_whatever_k_says():
    window = ChunkWindow(4 * MiB, link_rate=1e9)
    window.chunks = 64  # however it got there
    assert window.window_chunks() * 4 * MiB == cw.MAX_BYTES_IN_FLIGHT
    assert ChunkWindow(16 * MiB).window_chunks() == 1  # one chunk always goes


def test_a_lower_budget_stops_the_growth_but_does_not_cut_the_window():
    window = ChunkWindow(CHUNK, link_rate=100e6)
    for _ in range(10):
        window.on_window(window.chunks * CHUNK, 1.0, loop_lag_s=0.9)
    grown = window.chunks
    window.rate = DeliveryRate(seed=100e3)  # the link reads slow now
    window.on_window(grown * CHUNK, 1.0)
    assert window.chunks == grown


# --- shrinks on a loss ----------------------------------------------------------------


def test_a_window_the_transport_did_not_take_in_time_halves_it():
    window = ChunkWindow(CHUNK, link_rate=100e6)
    for _ in range(10):
        window.on_window(window.chunks * CHUNK, 1.0, loop_lag_s=0.9)
    grown = window.chunks
    assert window.on_window(grown * CHUNK, cw.DRAIN_TIMEOUT_FLOOR_S + 1) == "shrank"
    assert window.chunks == grown // 2 and window.timeouts == 1
    assert not window.slow_start  # one chunk at a time from here


def test_after_a_loss_on_a_link_limited_transfer_the_window_grows_back():
    """053157be kept the plateau test's rate from before the loss, so after one halving
    growth needed a 25 % rise over a rate the halved window could not reach, and never
    came. A halving restarts the test, as TCP re-enters congestion avoidance."""
    window = ChunkWindow(CHUNK)
    _measure(window, 100e6, seconds=20, overhead_s=0.004)  # a per-window cost: more per window pays
    peak = window.chunks
    assert peak >= 16 and window.plateau
    assert window.on_error() is True
    assert window.chunks == peak // 2 and not window.plateau
    _measure(window, 100e6, seconds=20, overhead_s=0.004)
    assert window.chunks > peak // 2 + 1, (peak, window.chunks)  # grew back, one chunk at a time
    assert window.chunks <= peak and window.halvings == 1


def test_an_error_halves_it_and_growth_turns_additive():
    window = ChunkWindow(CHUNK, link_rate=100e6)
    for _ in range(4):
        window.on_window(window.chunks * CHUNK, 1.0, loop_lag_s=0.9)
    assert window.chunks == 16
    window.on_error()
    assert window.chunks == 8
    window.on_window(8 * CHUNK, 1.0, loop_lag_s=0.9)
    assert window.chunks == 9


# --- the upload wires it ---------------------------------------------------------------


class _LanedWire:
    """A p2p manager with a data lane: records windows; `peers` names a tier."""

    def __init__(self):
        self.node_id = "dpc-node-" + "a" * 32
        self.peers = {}
        self.windows = []
        self.priority = []
        self.timeouts_given = []

    async def send_message_to_peer(self, node_id, message):
        self.priority.append(message)

    async def send_data_window(self, node_id, messages, still_sending=None, drain_timeout_s=None, **_):
        from dpc_client_core.send_lanes import DataWindowResult
        self.windows.append([m["payload"]["chunk_index"] for m in messages])
        self.timeouts_given.append(drain_timeout_s)
        return DataWindowResult(len(messages), 0.4, 0.4)  # every window loop-limited


@pytest.mark.asyncio
async def test_an_upload_sends_growing_windows_in_order_and_a_reask_halves_its_window(tmp_path):
    wire = _LanedWire()
    manager = FileTransferManager(
        p2p_manager=wire, firewall=SimpleNamespace(rules={}),
        settings=SimpleNamespace(get=lambda s, k, d=None: d), local_api=None, storage_base_path=tmp_path)
    peer = "dpc-node-" + "b" * 32
    path = tmp_path / "f.bin"
    path.write_bytes(bytes(range(256)) * (4 * 40))  # 40 chunks of 1 KiB
    transfer = FileTransfer(
        transfer_id="up-1", filename="f.bin", size_bytes=path.stat().st_size, hash="x",
        mime_type="application/octet-stream", chunk_size=1024, node_id=peer, direction="upload",
        status=TransferStatus.PENDING, chunks_received=set(), total_chunks=40, file_path=path)
    manager.active_transfers["up-1"] = transfer

    await manager.handle_file_accept(peer, "up-1")

    assert [len(w) for w in wire.windows][:4] == [1, 2, 4, 8]
    assert [i for w in wire.windows for i in w] == list(range(40))
    assert transfer.window.chunks > 8

    grown = transfer.window.chunks
    manager.active_transfers["up-1"] = transfer  # still offered: a re-ask arrives
    await manager.handle_file_chunk_retry(peer, {"transfer_id": "up-1", "chunk_index": 7})
    assert transfer.window.chunks == grown // 2 and transfer.window.errors == 1
    assert wire.windows[-1] == [7]  # the re-asked chunk went on the data lane too


def _upload(tmp_path, wire, nbytes, chunk=1024, tid="up-2"):
    manager = FileTransferManager(
        p2p_manager=wire, firewall=SimpleNamespace(rules={}),
        settings=SimpleNamespace(get=lambda s, k, d=None: d), local_api=None, storage_base_path=tmp_path)
    peer = "dpc-node-" + "b" * 32
    path = tmp_path / f"{tid}.bin"
    path.write_bytes(bytes(i % 251 for i in range(nbytes)))
    transfer = FileTransfer(
        transfer_id=tid, filename=path.name, size_bytes=nbytes, hash="x",
        mime_type="application/octet-stream", chunk_size=chunk, node_id=peer, direction="upload",
        status=TransferStatus.PENDING, chunks_received=set(), total_chunks=(nbytes + chunk - 1) // chunk,
        file_path=path)
    manager.active_transfers[tid] = transfer
    return manager, peer, transfer


@pytest.mark.asyncio
async def test_a_stall_rounds_batch_of_reasks_halves_the_window_once(tmp_path):
    """One stall round re-asks up to RECEIVE_REASK_BATCH chunks; 053157be halved per re-ask,
    so one round took K from 16 to 1. It is one loss event."""
    from dpc_client_core.managers.file_transfer_manager import RECEIVE_REASK_BATCH
    wire = _LanedWire()
    manager, peer, transfer = _upload(tmp_path, wire, 100 * 1024)
    await manager.handle_file_accept(peer, transfer.transfer_id)
    grown = transfer.window.chunks
    assert grown >= 16
    manager.active_transfers[transfer.transfer_id] = transfer
    for index in range(RECEIVE_REASK_BATCH):
        await manager.handle_file_chunk_retry(peer, {"transfer_id": transfer.transfer_id, "chunk_index": index})
    assert transfer.window.chunks == grown // 2
    assert transfer.window.errors == RECEIVE_REASK_BATCH and transfer.window.halvings == 1
    assert [w for w in wire.windows[-RECEIVE_REASK_BATCH:]] == [[i] for i in range(RECEIVE_REASK_BATCH)]

    # A loss after another window has been accounted is a new event
    transfer.window.on_window(transfer.window.chunks * 1024, 0.4, 0.4)
    before = transfer.window.chunks
    assert transfer.window.on_error() is True and transfer.window.chunks == before // 2


@pytest.mark.asyncio
@pytest.mark.parametrize("nbytes, windows", [(0, []), (1, [[0]]), (1024, [[0]]), (1025, [[0], [1]])])
async def test_a_zero_byte_and_a_one_chunk_file_go_through_the_upload_loop(tmp_path, nbytes, windows):
    wire = _LanedWire()
    manager, peer, transfer = _upload(tmp_path, wire, nbytes)
    await manager.handle_file_accept(peer, transfer.transfer_id)
    assert wire.windows == windows
    assert transfer.status == TransferStatus.TRANSFERRING  # not failed; FILE_COMPLETE ends it
    assert all(t is not None for t in wire.timeouts_given)  # every window carries its deadline


class _StalledWire(_LanedWire):
    """The transport takes `takes` frames of each window, then the window times out."""

    def __init__(self, takes):
        super().__init__()
        self.takes = list(takes)

    async def send_data_window(self, node_id, messages, still_sending=None, drain_timeout_s=None, **_):
        from dpc_client_core.send_lanes import DataWindowResult
        n = min(len(messages), self.takes.pop(0) if self.takes else len(messages))
        self.windows.append([m["payload"]["chunk_index"] for m in messages[:n]])
        self.timeouts_given.append(drain_timeout_s)
        return DataWindowResult(n, drain_timeout_s or 0.0, 0.0, timed_out=n < len(messages))


@pytest.mark.asyncio
async def test_a_window_that_times_out_halves_and_the_upload_resumes_where_it_stopped(tmp_path):
    wire = _StalledWire([1, 0, 1, 2])
    manager, peer, transfer = _upload(tmp_path, wire, 20 * 1024)
    await manager.handle_file_accept(peer, transfer.transfer_id)
    assert [i for w in wire.windows for i in w] == list(range(20))  # nothing lost, nothing twice
    assert transfer.window.timeouts >= 1
    assert transfer.status == TransferStatus.TRANSFERRING
    assert not [m for m in wire.priority if m["command"] == "FILE_CANCEL"]


@pytest.mark.asyncio
async def test_a_peer_that_takes_nothing_for_three_windows_ends_the_upload(tmp_path):
    from dpc_client_core.managers.file_transfer_manager import SEND_STALL_WINDOWS
    wire = _StalledWire([2] + [0] * 10)
    manager, peer, transfer = _upload(tmp_path, wire, 20 * 1024)
    await manager.handle_file_accept(peer, transfer.transfer_id)
    assert transfer.status == TransferStatus.FAILED
    assert len(wire.windows) == 1 + SEND_STALL_WINDOWS
    cancels = [m for m in wire.priority if m["command"] == "FILE_CANCEL"]
    assert [c["payload"]["reason"] for c in cancels] == ["send_error"]
    assert transfer.window.timeouts == SEND_STALL_WINDOWS


def test_only_after_a_window_the_loop_held_back_may_the_transport_hold_more_than_one_chunk():
    """The data lane keeps the transport's own mark — chat waits behind at most the chunk
    being handed over — unless the last window was loop-limited: then the window's other
    K-1 chunks, within the budget. A window the link limits puts the mark back at once."""
    window = ChunkWindow(CHUNK)
    assert window.allowance_bytes() == 0
    for _ in range(3):
        window.on_window(window.chunks * CHUNK, 0.4, loop_lag_s=0.4)
    assert window.chunks == 8 and window.loop_limited
    assert window.allowance_bytes() == 7 * cw.framed_chunk_bytes(CHUNK)
    assert window.allowance_bytes() < cw.UNKNOWN_RATE_WINDOW_BYTES * 4 // 3 + 8 * 256
    window.on_window(8 * CHUNK, 1.0, loop_lag_s=0.0)  # the link held this one back
    assert window.chunks == 8 and window.allowance_bytes() == 0


# --- the transport's reading (review of 9f8538f0, a fresh subagent and Zcode, 2026-11-20) ---


def _reading(burst, after, at_end=None, turn_s=0.1, written=None, loop_turn_s=0.0):
    from dpc_client_core.send_lanes import TransportReading
    return TransportReading(at_end_bytes=burst if at_end is None else at_end, after_turn_bytes=after,
                            turn_s=turn_s, burst_bytes=burst, written_bytes=burst if written is None else written,
                            loop_turn_s=loop_turn_s)


def test_a_busy_loop_on_a_link_that_keeps_up_grows_a_one_mib_chunk_window_and_learns_a_rate():
    """A file over 100 MiB, no rate yet: 1 MiB chunks, so UNKNOWN_RATE_WINDOW_BYTES was one
    chunk, K stayed 1, every window was loop-limited and none gave a sample: 2.44 MB/s on
    loopback under load, and a 10 GB file in about 70 min. Now the transport says the link
    took each window within a turn of the loop, so the budget doubles (to MAX_BYTES_IN_FLIGHT)
    and the window's wall time is a lower bound on the rate."""
    framed = cw.framed_chunk_bytes(MiB)
    window = ChunkWindow(MiB)
    for _ in range(12):
        k = window.window_chunks()
        window.on_window(k * MiB, 0.1, loop_lag_s=0.1, reading=_reading(k * framed, 0))
    assert window.loop_limited and window.peak_chunks * MiB == cw.MAX_BYTES_IN_FLIGHT
    assert window.allowance_bytes() == (window.window_chunks() - 1) * framed
    assert window.rate.measured and window.rate.bytes_per_second() >= 8 * MiB / 0.1 * 0.99
    assert not window.link_behind


def test_without_a_reading_of_the_transport_a_busy_loop_still_stops_at_the_unknown_rate_bound():
    """A connection that cannot say what its buffer holds (a relay, UDP): 9f8538f0's rule."""
    window = ChunkWindow(MiB)
    for _ in range(6):
        window.on_window(MiB, 0.1, loop_lag_s=0.1)
    assert window.chunks == 1 and window.allowance_bytes() == 0 and not window.rate.measured


def test_a_busy_loop_on_a_link_that_falls_behind_keeps_one_frame_ahead_of_chat_and_measures_the_link():
    """0.3 MB/s, the loop turning every 0.4 s, no rate known: the first window (one 64 KiB chunk)
    is gone within a turn, so a second chunk may wait in the transport; the link leaves a third
    of that two-chunk burst behind a turn later. From then on the link is the limit: the
    allowance is 0 whatever K grew to, and what the link took in each such turn is its rate."""
    link, turn = 300e3, 0.4
    framed = cw.framed_chunk_bytes(CHUNK)
    window = ChunkWindow(CHUNK)
    window.on_window(CHUNK, turn, loop_lag_s=turn, reading=_reading(framed, 0, turn_s=turn))
    assert window.chunks == 2 and window.allowance_bytes() == framed  # the cold start's one probe
    allowances = []
    for _ in range(30):
        k = window.window_chunks()
        allowance = window.allowance_bytes()
        allowances.append(allowance)
        burst = framed * (allowance // framed + 1) if allowance else framed  # frame by frame: the last
        left = max(0.0, burst - link * turn)
        elapsed = turn * (k + 1)
        window.on_window(k * CHUNK, elapsed, loop_lag_s=elapsed,
                         reading=_reading(burst, int(left), turn_s=turn, written=k * framed))
    assert window.link_behind and window.link_behind_windows >= 1
    assert allowances[0] == framed and set(allowances[1:]) == {0}
    assert window.rate.measured and window.rate.bytes_per_second() <= link


def test_a_turn_of_the_loop_that_left_the_transport_full_is_the_link_and_not_the_loop():
    window = ChunkWindow(CHUNK)
    framed = cw.framed_chunk_bytes(CHUNK)
    for _ in range(6):
        k = window.window_chunks()
        window.on_window(k * CHUNK, 0.4, loop_lag_s=0.4,
                         reading=_reading(k * framed, k * framed - 30_000, turn_s=0.1))
    assert window.link_behind_windows == 6 and window.chunks == 1 and window.allowance_bytes() == 0
    # 30 KB a 0.1 s turn: the samples after the first say so
    assert window.rate.bytes_per_second() == pytest.approx(30_000 * CHUNK / framed / 0.1, rel=0.01)


def test_an_app_limited_sample_only_ever_raises_the_rate():
    rate = DeliveryRate()
    rate.add(1, cw.RATE_SAMPLE_MIN_S)  # warm-up, dropped
    rate.add(int(5e6 * 0.3), 0.3)
    rate.add(int(1e6 * 0.3), 0.3, app_limited=True)  # a busy loop's lower bound: says nothing
    assert rate.bytes_per_second() == pytest.approx(5e6) and rate.samples_taken == 1
    rate.add(int(9e6 * 0.3), 0.3, app_limited=True)  # above what is known: the link moves at least this
    assert rate.bytes_per_second() == pytest.approx(9e6)


def test_the_allowance_is_cut_to_the_budget_when_the_budget_falls():
    """K is never cut on a lower budget (only a loss halves it), so 9f8538f0's allowance, K-1
    chunks, could exceed what the link was last measured to move in 0.1 s."""
    window = ChunkWindow(CHUNK, link_rate=100e6)
    for _ in range(5):
        window.on_window(window.chunks * CHUNK, 1.0, loop_lag_s=0.9)
    assert window.chunks == 32 and window.allowance_bytes() == 31 * cw.framed_chunk_bytes(CHUNK)
    window.rate = DeliveryRate(seed=1e6)  # the link reads slower now: 100 KB in the budget
    window.on_window(window.chunks * CHUNK, 1.0, loop_lag_s=0.9)
    assert window.chunks == 32  # held, never cut
    assert window.allowance_bytes() == (window.budget_bytes() // CHUNK - 1) * cw.framed_chunk_bytes(CHUNK) == 0
    window.rate = DeliveryRate(seed=3e6)  # 300 KB: four chunks, three of them waiting
    window.on_window(window.chunks * CHUNK, 1.0, loop_lag_s=0.9)
    assert window.allowance_bytes() == 3 * cw.framed_chunk_bytes(CHUNK)


@pytest.mark.asyncio
async def test_a_cancelled_upload_waits_for_its_reading_thread_before_the_file_is_released(tmp_path, monkeypatch):
    """A cancel while the upload awaited the worker reading its next window reached the
    worker's future, which then read as done while the thread still had the file open:
    `finally` skipped its wait and `_release_upload` deleted an unsaved screenshot under an
    open handle - on Windows a PermissionError, and the file stayed on disk."""
    import asyncio
    import threading

    wire = _LanedWire()
    manager, peer, transfer = _upload(tmp_path, wire, 8 * 1024)
    started, go, finished = threading.Event(), threading.Event(), []
    real_read = FileTransferManager._read_chunk_frames

    def slow_read(t, first, count):
        started.set()
        go.wait(10)
        try:
            return real_read(t, first, count)
        finally:
            finished.append(True)

    monkeypatch.setattr(manager, "_read_chunk_frames", slow_read)
    released = []
    monkeypatch.setattr(manager, "_release_upload", lambda t: released.append(bool(finished)))
    upload = asyncio.get_running_loop().create_task(manager.handle_file_accept(peer, transfer.transfer_id))
    assert await asyncio.to_thread(started.wait, 10)
    upload.cancel()
    asyncio.get_running_loop().call_later(0.2, go.set)
    with pytest.raises(asyncio.CancelledError):
        await upload
    assert released == [True]  # released only once the thread had closed the file


# --- the review of 08493d5f (a fresh subagent, 2026-11-20) ------------------------------


def test_a_readable_connection_whose_second_reading_never_came_gets_no_allowance():
    """A loop slower than READING_WAIT_MAX_S a turn: the connection can report its buffer, so
    the unknown-rate budget (1 MiB, for a relay or UDP) is not its, and with no verdict the
    next window leaves nothing more than its chunk in the transport."""
    framed = cw.framed_chunk_bytes(CHUNK)
    missed, unreadable = ChunkWindow(CHUNK), ChunkWindow(CHUNK)
    missed.on_window(CHUNK, 2.5, loop_lag_s=2.5, reading=_reading(framed, None, turn_s=1.0, loop_turn_s=1.2))
    unreadable.on_window(CHUNK, 2.5, loop_lag_s=2.5)
    assert missed.loop_limited_windows == 1 and not missed.loop_limited
    assert missed.allowance_bytes() == 0 and missed.chunks == 1
    assert missed.loop_budget_bytes() == CHUNK
    assert unreadable.allowance_bytes() == framed  # 9f8538f0's rule, where it still applies
