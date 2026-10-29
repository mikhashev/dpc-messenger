"""Speed and time left of one file transfer, for the Active Transfers row.

Both directions feed it the bytes of each chunk as it is sent or written; it
answers the current speed over a sliding window and whether a
`file_transfer_progress` event is due. The caller passes the clock in, so the
manager's `_monotonic` indirection drives it in tests.

Window: 5 s. At the ~17 chunks/s measured on 2026-10-28 that holds ~85 chunks
of 64 KB, enough to smooth the chunk-to-chunk jitter of the event loop and of
`chunk_delay`, while a real change of rate (a route that slows down) shows in
the figure within a few seconds. A 1 s window would hold ~17 samples and jump
on every refresh; 30 s would lag a stall by half a minute.

Cadence: one event per PROGRESS_INTERVAL_S, plus the first chunk and the last.
A chunk cadence was either too sparse (the receiver's 5 % steps: six minutes
between events on an 8 GB file) or too dense on a small chunk count.
"""

import math
from collections import deque
from typing import Any, Dict, Optional

SPEED_WINDOW_S = 5.0
PROGRESS_INTERVAL_S = 1.0
# Below this span the rate is noise (the first chunk over a few milliseconds).
SPEED_MIN_SPAN_S = 1.0


class TransferSpeed:
    """Bytes done so far and the samples of the last SPEED_WINDOW_S."""

    __slots__ = ("done", "_samples", "_last_event")

    def __init__(self, now: float):
        self.done = 0
        self._samples = deque([(now, 0)])
        self._last_event: Optional[float] = None

    def add(self, nbytes: int, now: float) -> None:
        """Count one chunk. O(1) amortised: one append, old samples dropped."""
        self.done += nbytes
        samples = self._samples
        samples.append((now, self.done))
        edge = now - SPEED_WINDOW_S
        # Keep one sample at or before the edge, so the span covers the window
        while len(samples) > 2 and samples[1][0] <= edge:
            samples.popleft()

    def bytes_per_second(self) -> int:
        """The rate over the window; 0 while it is not known yet."""
        (t0, b0), (t1, b1) = self._samples[0], self._samples[-1]
        span = t1 - t0
        if span < SPEED_MIN_SPAN_S:
            return 0
        return max(0, int((b1 - b0) / span))

    def seconds_left(self, size_bytes: int) -> Optional[int]:
        """Seconds to completion at the current rate; None while the rate is unknown or 0."""
        rate = self.bytes_per_second()
        if rate <= 0:
            return None
        return math.ceil(max(0, size_bytes - self.done) / rate)

    def event_due(self, now: float, last_chunk: bool) -> bool:
        """Whether a progress event goes out now; marks it sent when it does."""
        if last_chunk or self._last_event is None or now - self._last_event >= PROGRESS_INTERVAL_S:
            self._last_event = now
            return True
        return False

    def fields(self, size_bytes: int) -> Dict[str, Any]:
        """The two `file_transfer_progress` fields (Mike's names, 2026-10-29)."""
        return {
            "bytes_per_second": self.bytes_per_second(),
            "seconds_left": self.seconds_left(size_bytes),
        }
