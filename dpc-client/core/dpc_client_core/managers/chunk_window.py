"""How big a file's chunks are, and how many of them the sender puts on the link at once.

ADR-047 phase 0 (the sender picks `chunk_size` per file, no wire change: the
receiver already writes at index * the offer's `chunk_size`) and, from phase 2,
the window of chunks in flight. The send loop is `FileTransferManager.handle_file_accept`;
the two lanes that keep chat ahead of the chunks are in `send_lanes.py`.

**The chunk size is decided once per file, before the fingerprint.** The CRC
list, the offer and the send loop all use it, and the receiver cannot follow a
change mid-transfer, so speed decides the size at offer time and the window
during the transfer (`choose_chunk_size`):

1. by file size — 64 KiB up to 100 MiB, 1 MiB up to 1 GiB, 4 MiB above (the
   card's proposal and ADR-047's table): a small file stays in small frames, so
   a chat frame never waits behind a big one for it;
2. by connection tier — direct TLS takes any of these; WebRTC at most 1 MiB
   (aiortc cuts a message into 1200-byte SCTP chunks and has no flow control of
   its own); a volunteer relay, gossip, UDP/DTLS or an unknown wrapper keep
   today's 64 KiB (another person's bandwidth, and a DTLS record is 16 KiB);
3. by the rate last measured to this peer — one frame holds the link for at
   most `CHAT_WAIT_SLO_S`, so the chunk is the largest power of two the rate
   moves in that time, never below `MIN_CHUNK_BYTES`; with no rate measured
   yet, at most 1 MiB, which also keeps an older receiver's 30 s stall watch
   (`RECEIVE_STALL_TIMEOUT_S`) out of reach above ~35 KB/s;
4. the rate cap never makes the CRC list longer than `MAX_CHUNKS_PER_OFFER`.

A `[file_transfer] chunk_size` the owner set fixes every file's size
(`parse_chunk_size_setting`), with two limits applied per file
(`pinned_chunk_size`): on any tier but direct TLS that tier's cap still
applies (another person's relay, aiortc's missing flow control and the 16 KiB
DTLS record do not change because the owner asked), and it is raised so the
CRC list stays within `MAX_CHUNKS_PER_OFFER` (1 KiB over a 6 GiB file would
otherwise make an offer larger than a DPTP frame).

**The window** (`ChunkWindow`) is the K chunks the sender hands to the
connection before it lets the event loop turn at least once, and the unit it
measures the link by. The data lane writes them frame by frame, with the
connection's backpressure and the priority lane checked between frames
(`P2PManager.send_data_window`): with K = 1 a chat frame waits behind at
most the chunk already handed over, whatever K is, unless the window before
was loop-limited: then the transport may hold the other K-1 too
(`allowance_bytes`), what a node whose loop turns every 0.1-0.4 s needs. AIMD on K, with the signals Zcode asked
for (review, 2026-11-19):

- it grows by the measured delivery rate: only while K+1 chunks fit the
  budget, the rate times `WINDOW_SHARE_OF_SLO` x `CHAT_WAIT_SLO_S`, and never
  past `MAX_BYTES_IN_FLIGHT` whatever the window was — doubling until the
  first loss, one chunk at a time after it — and, on windows the link
  limited, only while each growth bought at least a quarter of the rate it
  could have (`GROWTH_GAIN` for a doubling, BBR's startup test; in proportion
  for a smaller step): past that plateau more chunks per window would only
  queue in front of chat;
- a window the event loop held up rather than the link (`loop_lag_s`, from
  `send_lanes.LoopLagProbe`, counted for the two turns of the loop a window
  waits, at least half its time) is "loop-limited", as BBR calls a sample
  app-limited: it gives no rate sample, so a node busy with a model never
  reads as a slow link, and it may grow, since more chunks per turn of the
  loop is what such a node needs — but only within the budget of the rate
  last measured, or `UNKNOWN_RATE_WINDOW_BYTES` while none is: a busy loop on
  a slow link must not pile megabytes up on it (review of 053157be,
  2026-11-20);
- it halves only on a loss event: a timeout (the transport did not take the
  window within `drain_timeout_s`: backpressure; the data lane stops waiting
  there and gives the lane up), a send error, or chunks the receiver asks
  again (`FILE_CHUNK_RETRY`) — once per event however many re-asks one stall
  round sends (`on_error`); never on latency alone — a slow window within its
  timeout changes nothing, and a lower budget only stops the growth. A
  halving restarts the plateau test, as TCP re-enters congestion avoidance,
  so the window can grow back;
- the round trip (LINK_PING / LINK_PONG, `PeerConnection.link_rtt_s`) is kept
  apart and never enters the rate.

A rate is file bytes per second (raw, before base64): a chunk takes the same
time on the wire whether counted raw at the raw rate or framed at the framed one.
"""

import logging
import math
import re
import time
from collections import deque
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

KiB = 1024
MiB = 1024 * KiB
GiB = 1024 * MiB

# How long one chat frame may wait behind file data on a fast link (Zcode's
# point 2, 2026-11-19). On a slow link it is one frame's transmit time, since
# a frame on the wire cannot be overtaken; the chunk size keeps that short.
CHAT_WAIT_SLO_S = 0.2
# The share of the SLO one window may fill; the rest is for the frame being
# encrypted and written and for the loop to come round to the chat frame.
WINDOW_SHARE_OF_SLO = 0.5

# --- the chunk size ------------------------------------------------------------

SMALL_FILE_CHUNK_BYTES = 64 * KiB
MEDIUM_FILE_CHUNK_BYTES = 1 * MiB
LARGE_FILE_CHUNK_BYTES = 4 * MiB
SMALL_FILE_MAX_BYTES = 100 * MiB
MEDIUM_FILE_MAX_BYTES = 1 * GiB

# The smallest chunk the rate cap picks: about 1 s at 20 KB/s. Below it the
# per-message cost outweighs the chat it protects.
MIN_CHUNK_BYTES = 16 * KiB
# The largest chunk the owner may set: 21.3 MiB once base64 and JSON are
# added, a third of MAX_FRAME_BYTES (64 MiB, dptp_v1.md §2).
MAX_CHUNK_BYTES = 16 * MiB
# The cap while no rate to this peer has been measured.
UNKNOWN_RATE_CHUNK_BYTES = 1 * MiB
# The most file bytes a loop-limited window grows to while no rate is known:
# the same bound as the chunk, which the receiver's 30 s stall watch clears
# on any link above ~35 KB/s.
UNKNOWN_RATE_WINDOW_BYTES = UNKNOWN_RATE_CHUNK_BYTES
# A CRC list of 2^17 entries is about 1.4 MB of JSON in the offer.
MAX_CHUNKS_PER_OFFER = 1 << 17

# Per connection tier (`connection_type`); a tier not named keeps 64 KiB.
TIER_CHUNK_CAP_BYTES: Dict[str, int] = {
    "direct_tls": LARGE_FILE_CHUNK_BYTES,
    "webrtc": MEDIUM_FILE_CHUNK_BYTES,
}
OTHER_TIER_CHUNK_CAP_BYTES = SMALL_FILE_CHUNK_BYTES

# Every config.ini written before 2026-11-19 carries `chunk_size = 65536`, the
# old default, so that value records no choice and reads as adaptive.
LEGACY_DEFAULT_CHUNK_SIZE = "65536"
# The same for `chunk_delay = 0.001`, the old default: a pause after every
# window, ~15.6 ms on Windows' timer. That value reads as the new default 0.
LEGACY_DEFAULT_CHUNK_DELAY_S = 0.001
DEFAULT_CHUNK_DELAY_S = 0.0
ADAPTIVE_CHUNK_SIZE = "auto"
_SIZE_SETTING = re.compile(r"^\s*(\d+)\s*(kib|mib|k|m)?\s*$", re.IGNORECASE)
_UNIT = {"": 1, "k": KiB, "kib": KiB, "m": MiB, "mib": MiB}

# --- the window ----------------------------------------------------------------

# The most file bytes one window hands to the transport, however the window
# was classified (a hard cap; review of 053157be).
MAX_BYTES_IN_FLIGHT = 8 * MiB
# A window is loop-limited when the loop lag took at least this share of it.
LOOP_LIMITED_SHARE = 0.5
# Turns of the loop one window waits for: the one the lag probe measures, and
# the one that resumes the sender after its drain (measured 2026-11-19 on
# Windows with a loop of 0.1 s turns: a window of 64 KiB took two turns).
LOOP_TURNS_PER_WINDOW = 2.0
# A doubling on link-limited windows is worth another while it raised the
# rate by at least this factor (BBR leaves startup below 1.25 over three
# rounds); a smaller step must buy the same share of what it could have:
# from K to K+1, a factor of 1 + 0.25/K.
GROWTH_GAIN = 1.25
# A rate sample spans at least this much link time, several windows if they
# are short: a window the kernel's send buffer swallows at once says nothing.
RATE_SAMPLE_MIN_S = 0.25
# The rate is the highest of the last samples (a windowed max, as BBR keeps
# its bottleneck bandwidth).
RATE_FILTER_SAMPLES = 8
# A window not drained within max(floor, factor x its expected time) is a
# timeout. The floor is far above the 1.6-3.3 s LINK_PONG a loaded node
# showed (Zcode, 2026-11-19), so a busy loop alone is never one.
DRAIN_TIMEOUT_FLOOR_S = 10.0
DRAIN_TIMEOUT_FACTOR = 8.0
# How long a peer's measured rate is trusted for the next file.
LINK_RATE_MEMORY_S = 600.0


def parse_chunk_size_setting(raw) -> Optional[int]:
    """`[file_transfer] chunk_size` as the sender reads it: None is adaptive.

    `auto` (the default), empty, and the legacy `65536` are adaptive. Any other
    number of bytes — or `64KiB`, `1MiB`, `4MiB` — fixes the chunk size of every
    file sent from this node, whatever its size or the link; it is clamped to
    1 KiB .. `MAX_CHUNK_BYTES`. `64KiB` pins the old size, which the bare
    legacy number no longer does. Anything unreadable is adaptive, and logged.
    """
    text = "" if raw is None else str(raw).strip()
    if text == LEGACY_DEFAULT_CHUNK_SIZE:
        logger.info(
            "[file_transfer] chunk_size = %s is the default of older builds; read as auto "
            "(write 64KiB to keep 64 KiB chunks)", text)
        return None
    if text.lower() in ("", ADAPTIVE_CHUNK_SIZE):
        return None
    match = _SIZE_SETTING.match(text)
    if not match:
        logger.warning("[file_transfer] chunk_size = %r is not a size; chunk sizes stay adaptive", raw)
        return None
    value = int(match.group(1)) * _UNIT[(match.group(2) or "").lower()]
    clamped = max(KiB, min(MAX_CHUNK_BYTES, value))
    if clamped != value:
        logger.warning("[file_transfer] chunk_size = %r clamped to %d bytes", raw, clamped)
    return clamped


def parse_chunk_delay_setting(raw) -> float:
    """`[file_transfer] chunk_delay` as the sender reads it: seconds of pause after each window.

    0, the default since 2026-11-19, is none. The legacy `0.001` every older
    config.ini carries records no choice (it was the old default) and reads as
    0, with one info line; any other value is honoured. A negative or
    unreadable value is 0, logged.
    """
    text = "" if raw is None else str(raw).strip()
    if not text:
        return DEFAULT_CHUNK_DELAY_S
    try:
        value = float(text)
    except ValueError:
        logger.warning("[file_transfer] chunk_delay = %r is not a number of seconds; no pause", raw)
        return DEFAULT_CHUNK_DELAY_S
    if not math.isfinite(value) or value < 0:
        logger.warning("[file_transfer] chunk_delay = %r is not a pause; no pause", raw)
        return DEFAULT_CHUNK_DELAY_S
    if value == LEGACY_DEFAULT_CHUNK_DELAY_S:
        logger.info(
            "[file_transfer] chunk_delay = %s is the default of older builds; read as 0 "
            "(write 0.002 to keep a pause)", text)
        return DEFAULT_CHUNK_DELAY_S
    return value


def framed_chunk_bytes(chunk_size: int) -> int:
    """About what one FILE_CHUNK of `chunk_size` costs on the wire: base64, JSON and the header."""
    return (int(chunk_size) + 2) // 3 * 4 + 256


def _pow2_floor(n: float) -> int:
    return 1 << max(0, int(math.floor(math.log2(max(1.0, n)))))


def _pow2_ceil(n: float) -> int:
    return 1 << max(0, int(math.ceil(math.log2(max(1.0, n)))))


def chunk_size_by_file_size(file_size: int) -> int:
    """Step 1 of the rule: ADR-047's table."""
    if file_size <= SMALL_FILE_MAX_BYTES:
        return SMALL_FILE_CHUNK_BYTES
    if file_size <= MEDIUM_FILE_MAX_BYTES:
        return MEDIUM_FILE_CHUNK_BYTES
    return LARGE_FILE_CHUNK_BYTES


def chunk_size_by_rate(link_rate: Optional[float]) -> int:
    """Step 3 of the rule: the largest power of two the link moves in CHAT_WAIT_SLO_S."""
    if not link_rate or link_rate <= 0:
        return UNKNOWN_RATE_CHUNK_BYTES
    return max(MIN_CHUNK_BYTES, min(LARGE_FILE_CHUNK_BYTES, _pow2_floor(link_rate * CHAT_WAIT_SLO_S)))


def choose_chunk_size(file_size: int, connection_type: Optional[str] = None,
                      link_rate: Optional[float] = None, pinned: Optional[int] = None) -> int:
    """The chunk size of one file to one peer (module docstring, steps 1-4).

    `link_rate` is the rate last measured to that peer, file bytes per second,
    or None; `pinned` is the owner's setting (`parse_chunk_size_setting`),
    held to `pinned_chunk_size`.
    """
    if pinned:
        return pinned_chunk_size(pinned, file_size, connection_type)
    by_size = chunk_size_by_file_size(file_size)
    by_tier = _tier_cap(connection_type)
    by_rate = max(chunk_size_by_rate(link_rate), crc_list_floor(file_size))
    return max(MIN_CHUNK_BYTES, min(by_size, by_tier, by_rate))


def _tier_cap(connection_type: Optional[str]) -> int:
    return TIER_CHUNK_CAP_BYTES.get(connection_type or "", OTHER_TIER_CHUNK_CAP_BYTES)


def crc_list_floor(file_size: int) -> int:
    """The smallest power-of-two chunk that keeps a file's CRC list within MAX_CHUNKS_PER_OFFER."""
    return _pow2_ceil(max(0, file_size) / MAX_CHUNKS_PER_OFFER)


def pinned_chunk_size(pinned: int, file_size: int, connection_type: Optional[str] = None) -> int:
    """The owner's fixed size for one file, held to two limits (review of 053157be).

    On every tier but direct TLS the tier cap applies to it too, the safer of
    the two options the review offered: a volunteer relay, gossip and UDP/DTLS
    keep 64 KiB and WebRTC 1 MiB whatever the owner wrote. And it is raised to
    the size that keeps the CRC list within MAX_CHUNKS_PER_OFFER, so the offer
    stays far below a frame; that floor wins over the tier cap.
    """
    size = int(pinned)
    if connection_type != "direct_tls":
        size = min(size, _tier_cap(connection_type))
    return max(size, crc_list_floor(file_size))


class DeliveryRate:
    """File bytes per second the link took, from the link-limited windows the sender drained.

    Windows are summed into samples of at least RATE_SAMPLE_MIN_S of link time
    (wall time less loop lag). The first sample of a transfer is dropped: the
    kernel's send buffer fills at memory speed then. The rate is the max of
    the last RATE_FILTER_SAMPLES samples; with none yet, `seed` (the rate
    remembered from the last transfer to the same peer), or None.
    """

    def __init__(self, seed: Optional[float] = None):
        self.seed = seed if seed and seed > 0 else None
        self._samples: deque = deque(maxlen=RATE_FILTER_SAMPLES)
        self._acc_bytes = 0
        self._acc_s = 0.0
        self._warmed_up = False
        self.samples_taken = 0

    @property
    def measured(self) -> bool:
        """Whether this transfer has produced a sample of its own."""
        return bool(self._samples)

    def bytes_per_second(self) -> Optional[float]:
        if self._samples:
            return max(self._samples)
        return self.seed

    @property
    def last_sample(self) -> Optional[float]:
        """The newest sample of this transfer, or None: what the plateau test compares.

        The max filter remembers a rate from before a halving for
        RATE_FILTER_SAMPLES samples; held against it, no growth after a loss
        could ever look as if it paid.
        """
        return self._samples[-1] if self._samples else None

    def restart_sample(self) -> None:
        """Drop the sample being summed: the windows after a halving are measured on their own."""
        self._acc_bytes, self._acc_s = 0, 0.0

    def add(self, nbytes: int, link_s: float) -> None:
        self._acc_bytes += max(0, nbytes)
        self._acc_s += max(0.0, link_s)
        if self._acc_s < RATE_SAMPLE_MIN_S:
            return
        sample = self._acc_bytes / self._acc_s
        self._acc_bytes, self._acc_s = 0, 0.0
        if not self._warmed_up:
            self._warmed_up = True
            return
        self._samples.append(sample)
        self.samples_taken += 1


class ChunkWindow:
    """K, the chunks one window hands to the connection per turn of the loop (module docstring)."""

    def __init__(self, chunk_size: int, link_rate: Optional[float] = None):
        self.chunk_size = max(1, int(chunk_size))
        self.chunks = 1
        self.peak_chunks = 1
        self.slow_start = True
        self.rate = DeliveryRate(seed=link_rate)
        self.windows = 0
        self.timeouts = 0
        self.errors = 0
        self.halvings = 0
        self.loop_limited_windows = 0
        # The plateau test on link-limited windows: the newest sample when K
        # last grew there, how many samples the rate had then, and the factor
        # a later sample must reach for that growth to have paid.
        self._rate_at_growth: Optional[float] = None
        self._samples_at_growth = 0
        self._gain_needed = GROWTH_GAIN
        self.plateau = False
        # `windows` when K last halved: a loss in the same window is the same event.
        self._windows_at_halving: Optional[int] = None
        # Whether the last window was loop-limited: only then may the next one
        # leave more than the chunk being handed over in the transport.
        self.loop_limited = False

    def budget_bytes(self) -> Optional[int]:
        """File bytes one window may put ahead of a chat frame; None while no rate is known."""
        rate = self.rate.bytes_per_second()
        if rate is None:
            return None
        return int(max(self.chunk_size, min(MAX_BYTES_IN_FLIGHT, rate * CHAT_WAIT_SLO_S * WINDOW_SHARE_OF_SLO)))

    def window_chunks(self) -> int:
        """The chunks the next window hands over: K, within MAX_BYTES_IN_FLIGHT, one at least."""
        return max(1, min(self.chunks, MAX_BYTES_IN_FLIGHT // self.chunk_size))

    def allowance_bytes(self) -> int:
        """What the transport may hold in front of a chat frame during the next window, framed bytes.

        0 — the transport's own 64 KiB mark, so one chunk at a time — unless
        the last window was loop-limited: then the K-1 chunks besides the one
        being handed over, so a busy loop moves K per turn instead of one. K
        is within the budget (the rate times half the SLO, or
        UNKNOWN_RATE_WINDOW_BYTES with none). A window the link limits goes
        back to one chunk ahead of chat at once; on such a link more in the
        transport would only queue in front of it.
        """
        if not self.loop_limited:
            return 0
        return (self.window_chunks() - 1) * framed_chunk_bytes(self.chunk_size)

    def drain_timeout_s(self, nbytes: int) -> float:
        rate = self.rate.bytes_per_second()
        expected = nbytes / rate if rate else 0.0
        return max(DRAIN_TIMEOUT_FLOOR_S, DRAIN_TIMEOUT_FACTOR * expected)

    def _grow(self, budget: int) -> None:
        most = max(1, min(budget, MAX_BYTES_IN_FLIGHT) // self.chunk_size)
        if self.chunks >= most:
            return  # held, never cut: a lower budget only stops the growth
        self.chunks = min(most, self.chunks * 2 if self.slow_start else self.chunks + 1)
        self.peak_chunks = max(self.peak_chunks, self.chunks)

    def _halve(self) -> bool:
        """Halve K once per loss event; True when it did.

        A loss reported before another window has been accounted since the
        last halving (a stall round of up to 64 re-asks, or a timeout and the
        re-asks it causes) is the same event. The plateau test starts again,
        as TCP re-enters congestion avoidance: the first growth after a loss
        needs no gain, and each one after it the share of a one-chunk step.
        """
        if self._windows_at_halving == self.windows:
            return False
        self._windows_at_halving = self.windows
        self.halvings += 1
        self.slow_start = False
        self.chunks = max(1, self.chunks // 2)
        self._rate_at_growth = None
        self.rate.restart_sample()
        self._samples_at_growth = self.rate.samples_taken
        self._gain_needed = GROWTH_GAIN
        self.plateau = False
        return True

    def _grow_if_it_pays(self) -> None:
        """After a link-limited window: grow only while growing still raises the rate."""
        budget = self.budget_bytes()
        rate = self.rate.bytes_per_second()
        if budget is None or rate is None or not self.rate.measured:
            return  # nothing measured on this transfer yet: a remembered rate sizes, never grows
        if self.rate.samples_taken <= self._samples_at_growth:
            return  # no sample since the last growth or halving: wait for its verdict
        latest = self.rate.last_sample
        if self._rate_at_growth is not None:
            if latest >= self._rate_at_growth * self._gain_needed:
                self.plateau = False  # it paid, or the link got faster: probe on
            elif not self.plateau:
                self.plateau = True  # it did not pay: hold here, never cut
                return
            else:
                return
        before = self.chunks
        self._grow(budget)
        if self.chunks > before:
            self._rate_at_growth = latest
            self._samples_at_growth = self.rate.samples_taken
            # A quarter of what the step could have bought, were the rate in proportion to K
            self._gain_needed = 1.0 + (GROWTH_GAIN - 1.0) * (self.chunks / before - 1.0)

    def on_window(self, nbytes: int, elapsed_s: float, loop_lag_s: float = 0.0,
                  timed_out: bool = False) -> str:
        """Account one window; returns `grew`, `held` or `shrank`.

        `timed_out`: the data lane stopped waiting for the transport at
        `drain_timeout_s` (`P2PManager.send_data_window`); a window that took
        longer than that on a lane that cannot stop waiting counts the same.
        """
        before = self.chunks
        self.windows += 1
        elapsed_s = max(0.0, elapsed_s)
        # The probe sees one turn of the loop; the window pays about two: the
        # turn the probe waited, and the turn that wakes the sender once the
        # drain is done. Both are the loop's, not the link's.
        lag = min(LOOP_TURNS_PER_WINDOW * max(0.0, loop_lag_s), elapsed_s)
        self.loop_limited = False
        if timed_out or elapsed_s > self.drain_timeout_s(nbytes):
            self.timeouts += 1
            self._halve()
        elif elapsed_s > 0 and lag >= elapsed_s * LOOP_LIMITED_SHARE:
            self.loop_limited = True
            # The loop, not the link, held this window: no sample; more per
            # turn, but only within what the link is known (or assumed) to move.
            self.loop_limited_windows += 1
            budget = self.budget_bytes()
            self._grow(UNKNOWN_RATE_WINDOW_BYTES if budget is None else budget)
        else:
            self.rate.add(nbytes, elapsed_s - lag)
            self._grow_if_it_pays()
        if self.chunks > before:
            return "grew"
        return "shrank" if self.chunks < before else "held"

    def on_error(self) -> bool:
        """A send error, or a chunk the receiver asked again: a loss. True when K halved."""
        self.errors += 1
        return self._halve()


class PeerLinkRates:
    """The last rate measured to each peer, per tier, for the next file's chunk size and window."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._rates: Dict[str, Tuple[float, Optional[str], float]] = {}

    def get(self, node_id: str, connection_type: Optional[str]) -> Optional[float]:
        entry = self._rates.get(node_id)
        if entry is None:
            return None
        rate, tier, at = entry
        if tier != connection_type or self._clock() - at > LINK_RATE_MEMORY_S:
            return None
        return rate

    def remember(self, node_id: str, connection_type: Optional[str], rate: Optional[float]) -> None:
        if rate and rate > 0:
            self._rates[node_id] = (float(rate), connection_type, self._clock())
