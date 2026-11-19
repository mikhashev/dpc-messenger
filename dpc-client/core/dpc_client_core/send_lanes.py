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
the rate last measured times half of `CHAT_WAIT_SLO_S`, or 1 MiB while no
rate is known. After a window the link held back the mark is the
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
from typing import Any


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


@dataclass
class DataWindowResult:
    """What one window on the data lane did: frames handed over, wall time, loop lag.

    `timed_out`: the transport did not take the window within the caller's
    `drain_timeout_s`; the lane was given up there. The frames counted were
    handed over and stay in the connection's order.
    """
    frames: int
    elapsed_s: float
    loop_lag_s: float
    timed_out: bool = False
