"""Two lanes on one peer connection: chat and control first, file data after.

ADR-047 §7 ("Priority"): "each peer connection gets a two-lane send queue:
every other message first, file data only when the first lane is empty". The
first user is the FILE_CHUNK stream (phase 0); phase 2's FILE_DATA frames and
pull answers are meant to go through the same `P2PManager.send_data_window`.

Every message to a peer leaves through one of two doors of `P2PManager`:

- `send_message_to_peer` — the **priority lane**: chat, control, offers,
  cancels, re-asks. It writes at once, as it always did, and is counted here
  while it is on its way.
- `send_data_window` — the **data lane**: a window of file frames. One window
  at a time per connection; it starts only when no priority frame is on its
  way, and on a transport that awaits each frame (relay, UDP) it looks again
  between frames.

A connection is one ordered byte stream (TLS) or one ordered channel (WebRTC):
a frame handed to it cannot be overtaken. So the priority is enforced before
the hand-over, one frame at a time: the data lane hands a frame over only
when the connection is below its write mark (its `drain` returns without
waiting) and no priority frame is on its way, and checks both again before
the next frame. A chat frame therefore waits behind at most the one file
frame already handed to the transport (on TLS one in the TLS layer and one
beneath it, each layer holding at most its 64 KiB mark besides,
`p2p_manager.set_write_marks`), plus what the operating system's socket
buffer already holds — as close to ADR-047's "the one chunk already being
written" as asyncio's transports allow (review of 053157be, 2026-11-20).

One exception, for a node whose event loop is busy (a model running): one
chunk per turn of a loop that turns every 0.1-0.4 s is the 0.3 MB/s that
ADR-047 phase 0 set out to fix, so a window that follows one the loop rather
than the link held back may leave its other K-1 chunks in the transport too
(`ChunkWindow.allowance_bytes`, `set_write_allowance`), K within the budget:
the rate last measured times half of `CHAT_WAIT_SLO_S`, or the window the
transport was seen to clear within a turn of the loop (1 MiB at a cold
start). Which of the two held a window back is read from two signals: the
loop's lag, summed over every wait of the window (`LoopLagProbe`,
`LOOP_TURNS_PER_WAIT`), and the transport's write buffer a turn of the loop
after the window's last frame (`TransportReading`). The mark is set inside
the data lane, so one upload's allowance never applies to another's window
or to a re-sent chunk. After a window the link held back the mark is the
transport's own again. The chunk size keeps one frame within what the
link moves in `CHAT_WAIT_SLO_S` (managers/chunk_window.py);
`tests/test_a_chat_message_is_not_queued_behind_a_file.py` measures the wait.

A window ends with at least one turn of the event loop: a selector loop
(Linux, macOS) never suspends a writer whose socket keeps taking data, so
without it a whole file could be read, framed and written in one go while
chat, re-asks and LINK_PONG waited. Its wait for the transport is bounded
(`drain_timeout_s`): a peer that stops reading costs the window, not the lane.
"""

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import Any, Optional


class SendLanes:
    """The two lanes of one connection (one object per connection, `lanes_of`)."""

    __slots__ = ("_priority_on_way", "_priority_idle", "_data_turn")

    def __init__(self):
        self._priority_on_way = 0
        self._priority_idle = asyncio.Event()
        self._priority_idle.set()
        self._data_turn = asyncio.Lock()

    @property
    def priority_on_way(self) -> int:
        return self._priority_on_way

    @contextlib.contextmanager
    def priority_frame(self):
        """Count one priority frame for as long as it is being sent."""
        self._priority_on_way += 1
        self._priority_idle.clear()
        try:
            yield
        finally:
            self._priority_on_way -= 1
            if self._priority_on_way <= 0:
                self._priority_on_way = 0
                self._priority_idle.set()

    async def wait_priority_idle(self) -> None:
        """Return once no priority frame is on its way."""
        while self._priority_on_way:
            await self._priority_idle.wait()

    @contextlib.asynccontextmanager
    async def data_turn(self):
        """This connection's data lane, once the priority lane is empty."""
        async with self._data_turn:
            await self.wait_priority_idle()
            yield

    @contextlib.asynccontextmanager
    async def data_lane(self):
        """This connection's data lane, one window at a time; the caller checks the priority lane per frame."""
        async with self._data_turn:
            yield


def lanes_of(connection: Any) -> SendLanes:
    """The lanes kept on a connection object; a new pair for a reconnect's new object."""
    lanes = getattr(connection, "_send_lanes", None)
    if isinstance(lanes, SendLanes):
        return lanes
    lanes = SendLanes()
    try:
        connection._send_lanes = lanes
    except AttributeError:  # a stand-in that takes no attributes: lanes for this call only
        pass
    return lanes


# Turns of the loop one wait of the data lane costs when it suspends: the one
# the lag probe measures, and the one that resumes the sender once what it
# waited for is done (measured 2026-11-19 on Windows with a loop of 0.1 s
# turns: a window of 64 KiB took two turns). The data lane charges a window
# this for every wait that suspended, not once per window: handed over frame
# by frame, a window of K chunks may wait K+1 times, and counted once the
# loop's pace read as the link's rate (review of 9f8538f0, Zcode, 2026-11-20).
LOOP_TURNS_PER_WAIT = 2.0
# The longest the data lane waits, after a window the loop held up, for the
# transport's reading (`TransportReader`); a loop slower than this gives none.
READING_WAIT_MAX_S = 1.0


class LoopLagProbe:
    """How long the event loop took to come round to a callback queued now.

    Queued right before the window's drain, it runs when the loop has worked
    through what was ready before it; that delay is the loop's, not the
    link's, and `ChunkWindow` takes it out of the rate sample. Costs no extra
    suspension of the sender.
    """

    __slots__ = ("_queued_at", "_ran_at")

    def __init__(self):
        self._queued_at = time.perf_counter()
        self._ran_at = None
        asyncio.get_running_loop().call_soon(self._ran)

    def _ran(self) -> None:
        self._ran_at = time.perf_counter()

    @property
    def ran(self) -> bool:
        """Whether the loop has turned since the probe was queued."""
        return self._ran_at is not None

    def lag_s(self) -> float:
        end = self._ran_at if self._ran_at is not None else time.perf_counter()
        return max(0.0, end - self._queued_at)


def reading_hops(loop) -> int:
    """Timer hops before `TransportReader` reads: 2 on Windows' Proactor, 1 on any other loop.

    A selector loop (the default on Linux and macOS, and chosen on Windows
    by some) runs a turn's I/O callbacks — `_write_ready`, which also resumes
    the TLS layer — before the timers due in that turn, so one hop reads
    after the socket took what it could. The Proactor issues the next send
    from the last one's completion callback, a turn later (review of
    08493d5f, 2026-11-20).
    """
    proactor = getattr(asyncio, "ProactorEventLoop", None)  # Windows only
    return 2 if proactor is not None and isinstance(loop, proactor) else 1


class TransportReader:
    """The transport's write buffer, read by timer callbacks one or two turns of the loop from now.

    asyncio runs a turn's I/O callbacks (a send completed, the socket
    writable) after the tasks already queued, so a sender back from
    `sleep(0)` still sees the buffer as it was before that turn. A timer
    due now runs in the next turn, after that turn's I/O callbacks. On a
    selector loop one such hop is enough. On Windows' Proactor two
    (`reading_hops`), because it moves a transport's buffer one send per
    turn — the next send is issued from the last one's completion callback —
    so even a link that keeps up empties a window's buffer only in the
    second turn (measured 2026-11-20 on loopback TLS with a loop of 0.1 s
    turns: one hop read every window of four 1 MiB chunks as the link
    behind). On a loop of 0.4 s turns the reading lands up to three turns
    after the window's last frame, so a slow link can look as if it kept up
    with one doubling more than it does; `ChunkWindow` caps the budget at
    what the link moved per turn once it sees it behind. It costs the
    sender no wait unless it asks for the reading (`taken`).
    """

    __slots__ = ("_size_of", "_handle", "_hops_left", "taken", "after_bytes", "taken_at")

    def __init__(self, size_of):
        loop = asyncio.get_running_loop()
        self._size_of = size_of
        self._hops_left = reading_hops(loop)
        self.after_bytes = None
        self.taken_at = None
        self.taken = asyncio.Event()
        self._handle = loop.call_later(0, self._hop)

    def _hop(self) -> None:
        self._hops_left -= 1
        if self._hops_left > 0:
            self._handle = asyncio.get_running_loop().call_later(0, self._hop)
        else:
            self._read()

    def _read(self) -> None:
        self._handle = None
        self.after_bytes = self._size_of()
        self.taken_at = time.perf_counter()
        self.taken.set()

    def cancel(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None


@dataclass
class TransportReading:
    """The transport's write buffer at the end of a window, framed bytes (`ChunkWindow.on_window`).

    `at_end_bytes` right after the window's last frame, `after_turn_bytes`
    at least one whole turn of the loop later (`TransportReader`), `turn_s`
    apart; `after_turn_bytes` is None when that reading did not come within
    `READING_WAIT_MAX_S` (or the transport could not say then): the
    connection can report its buffer, but this window has no verdict;
    `burst_bytes` what the window handed over since its last wait that
    suspended (all of it when none did), `written_bytes` all it handed over,
    `loop_turn_s` one turn of the loop as the window's closing turn measured it. Read with
    `get_write_buffer_size`: asyncio's transport (on TLS, the TLS layer and
    the socket transport beneath it), WebRTC's `bufferedAmount`. On Windows'
    Proactor an empty buffer means handed to the operating system, which is
    why `ChunkWindow` compares it with the burst and not with zero.
    """
    at_end_bytes: int
    after_turn_bytes: Optional[int]
    turn_s: float
    burst_bytes: int
    written_bytes: int
    loop_turn_s: float = 0.0


@dataclass
class DataWindowResult:
    """What one window on the data lane did: frames handed over, wall time, loop lag.

    `loop_lag_s` is the loop's share of the window: LOOP_TURNS_PER_WAIT turns
    for every wait that suspended, as its probe measured them, and the
    closing turn. `timed_out`: the transport did not take the window within
    the caller's `drain_timeout_s`; the lane was given up there. The frames
    counted were handed over and stay in the connection's order. `reading`:
    the transport's buffer at the end of the window, or None on a
    connection that cannot report it, or a window that timed out.
    """
    frames: int
    elapsed_s: float
    loop_lag_s: float
    timed_out: bool = False
    reading: Optional[TransportReading] = None
