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

A `[file_transfer] chunk_size` the owner set is honoured for every file as it
is — an override, not a ceiling (`parse_chunk_size_setting`).

**The window** (`ChunkWindow`) is K chunks written back to back, then one
drain. AIMD on K, with the signals Zcode asked for (review, 2026-11-19):

- it grows by the measured delivery rate: only while K+1 chunks fit the
  budget, the rate times `WINDOW_SHARE_OF_SLO` x `CHAT_WAIT_SLO_S`, capped by
  `MAX_BYTES_IN_FLIGHT` — doubling until the first loss, one chunk at a time
  after it — and, on windows the link limited, only while the rate the last
  growth bought is at least `GROWTH_GAIN` times the rate before it (BBR's
  startup test): past that plateau more chunks in flight would only queue in
  front of chat. With no rate yet it stays at one chunk;
- a window the event loop held up rather than the link (`loop_lag_s`, from
  `send_lanes.LoopLagProbe`, counted for the two turns of the loop a window
  waits, at least half its time) is "loop-limited", as
  BBR calls a sample app-limited: it gives no rate sample, so a node busy with
  a model never reads as a slow link, and it lets K grow, since more chunks
  per turn of the loop is what such a node needs;
- it halves only on a timeout (the transport did not take the window within
  `drain_timeout_s`: backpressure), on a send error, or on a chunk the
  receiver asks again (`FILE_CHUNK_RETRY`); never on latency alone — a slow
  window within its timeout changes nothing, and a lower budget only stops the
  growth;
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
ADAPTIVE_CHUNK_SIZE = "auto"
_SIZE_SETTING = re.compile(r"^\s*(\d+)\s*(kib|mib|k|m)?\s*$", re.IGNORECASE)
_UNIT = {"": 1, "k": KiB, "kib": KiB, "m": MiB, "mib": MiB}

# --- the window ----------------------------------------------------------------

# The most file bytes one window puts on the link before it drains.
MAX_BYTES_IN_FLIGHT = 8 * MiB
# A window is loop-limited when the loop lag took at least this share of it.
LOOP_LIMITED_SHARE = 0.5
# Turns of the loop one window waits for: the one the lag probe measures, and
# the one that resumes the sender after its drain (measured 2026-11-19 on
# Windows with a loop of 0.1 s turns: a window of 64 KiB took two turns).
LOOP_TURNS_PER_WINDOW = 2.0
# A growth on link-limited windows is worth another while it raised the rate
# by at least this factor (BBR leaves startup below 1.25 over three rounds).
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
    if text.lower() in ("", ADAPTIVE_CHUNK_SIZE, LEGACY_DEFAULT_CHUNK_SIZE):
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
    or None; `pinned` is the owner's setting (`parse_chunk_size_setting`).
    """
    if pinned:
        return pinned
    by_size = chunk_size_by_file_size(file_size)
    by_tier = TIER_CHUNK_CAP_BYTES.get(connection_type or "", OTHER_TIER_CHUNK_CAP_BYTES)
    by_rate = max(chunk_size_by_rate(link_rate), _pow2_ceil(file_size / MAX_CHUNKS_PER_OFFER))
    return max(MIN_CHUNK_BYTES, min(by_size, by_tier, by_rate))


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
    """K, the chunks one window writes before it drains (module docstring)."""

    def __init__(self, chunk_size: int, link_rate: Optional[float] = None):
        self.chunk_size = max(1, int(chunk_size))
        self.chunks = 1
        self.peak_chunks = 1
        self.slow_start = True
        self.rate = DeliveryRate(seed=link_rate)
        self.timeouts = 0
        self.errors = 0
        self.loop_limited_windows = 0
        # The plateau test on link-limited windows: the rate when K last grew
        # there, and how many samples the rate had then.
        self._rate_at_growth: Optional[float] = None
        self._samples_at_growth = 0
        self.plateau = False

    def budget_bytes(self) -> Optional[int]:
        """File bytes one window may put ahead of a chat frame; None while no rate is known."""
        rate = self.rate.bytes_per_second()
        if rate is None:
            return None
        return int(max(self.chunk_size, min(MAX_BYTES_IN_FLIGHT, rate * CHAT_WAIT_SLO_S * WINDOW_SHARE_OF_SLO)))

    def drain_timeout_s(self, nbytes: int) -> float:
        rate = self.rate.bytes_per_second()
        expected = nbytes / rate if rate else 0.0
        return max(DRAIN_TIMEOUT_FLOOR_S, DRAIN_TIMEOUT_FACTOR * expected)

    def _grow(self, budget: int) -> None:
        most = max(1, budget // self.chunk_size)
        if self.chunks >= most:
            return  # held, never cut: a lower budget only stops the growth
        self.chunks = min(most, self.chunks * 2 if self.slow_start else self.chunks + 1)
        self.peak_chunks = max(self.peak_chunks, self.chunks)

    def _halve(self) -> None:
        self.slow_start = False
        self.chunks = max(1, self.chunks // 2)

    def _grow_if_it_pays(self) -> None:
        """After a link-limited window: grow only while growing still raises the rate."""
        budget = self.budget_bytes()
        rate = self.rate.bytes_per_second()
        if budget is None or rate is None or not self.rate.measured:
            return  # nothing measured on this transfer yet: a remembered rate sizes, never grows
        if self.rate.samples_taken <= self._samples_at_growth and self._rate_at_growth is not None:
            return  # no sample since the last growth: wait for its verdict
        if self._rate_at_growth is not None:
            if rate >= self._rate_at_growth * GROWTH_GAIN:
                self.plateau = False  # it paid, or the link got faster: probe on
            elif not self.plateau:
                self.plateau = True  # it did not pay: hold here, never cut
                return
            else:
                return
        before = self.chunks
        self._grow(budget)
        if self.chunks > before:
            self._rate_at_growth = rate
            self._samples_at_growth = self.rate.samples_taken

    def on_window(self, nbytes: int, elapsed_s: float, loop_lag_s: float = 0.0) -> str:
        """Account one drained window; returns `grew`, `held` or `shrank`."""
        before = self.chunks
        elapsed_s = max(0.0, elapsed_s)
        # The probe sees one turn of the loop; the window pays about two: the
        # turn the probe waited, and the turn that wakes the sender once the
        # drain is done. Both are the loop's, not the link's.
        lag = min(LOOP_TURNS_PER_WINDOW * max(0.0, loop_lag_s), elapsed_s)
        if elapsed_s > self.drain_timeout_s(nbytes):
            self.timeouts += 1
            self._halve()
        elif elapsed_s > 0 and lag >= elapsed_s * LOOP_LIMITED_SHARE:
            # The loop, not the link, held this window: no sample; more per turn
            self.loop_limited_windows += 1
            budget = self.budget_bytes()
            self._grow(MAX_BYTES_IN_FLIGHT if budget is None else budget)
        else:
            self.rate.add(nbytes, elapsed_s - lag)
            self._grow_if_it_pays()
        if self.chunks > before:
            return "grew"
        return "shrank" if self.chunks < before else "held"

    def on_error(self) -> None:
        """A send error, or a chunk the receiver asked again."""
        self.errors += 1
        self._halve()


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
