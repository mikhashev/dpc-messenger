"""
The card lease (ADR-052): every GPU client in DPC holds, waits for and yields the one card.

`_CARD_CLAIMS` in `llama_server_supervisor` knows only the supervised llama.cpp children. A
`CardLease` is the standing of any other client (Whisper, a music engine, the embedder, GLiNER):
registered once, then granted, used and released many times. Granted leases are holders beside the
supervisors, read together through `llama_server_supervisor._live_holders()`; lease waiters stand in
`_LEASE_LINE`, ordered by the same clock as the card queue's places (`_CARD_ARRIVAL`), so no waiter
overtakes another in either line (L5a). An idle lease holder is asked to let go (`on_yield`); a busy
one is waited for and named (L6). Every wait has one deadline, fixed when it was asked for (L4), and
every refusal is a `CardQueueRefusal`, "never retried" at every door that already reads that (L3).

Pure Python and in-process, the same on Windows, Linux and macOS: no lease is granted by asking a
vendor tool, only this registry. Every write happens on the owner loop (`bind_owner_loop`), where
`acquire` is run from any loop, and `release` from any thread.

The supervisor module never imports this one; importing this one installs the four hooks it reads
(`_LEASE_HOLDERS`, `_LEASE_WAITERS`, `_LEASE_QUEUE_ROWS`, `_LEASE_CHAT_LEAVES`), as the provider
module sets `_still_configured`. Until it is imported no lease exists and the card behaves exactly as
before.

Slice S1: the registry, the line, the order across both lines as seen from a lease, the yield of an
idle lease to a lease, the refusals and the wait estimate. Slice S2: the supervisors honour leases
(their side is in `llama_server_supervisor`: `_free_the_card`, `_blocking_holders`,
`swap_out_for_a_lease`); here, a new use of a granted lease waits behind an earlier card-queue place
too (L5), and a lease that `waits_for_engines` (music, Mike #70), first in both lines, swaps an idle
supervisor out by the owner-start rule with the swap on. Any other lease waits for a supervisor
holder until it goes by itself. Slice S3b: the local Whisper provider is a client (a lease per GPU
alias, `waits_for_engines`, item 1a; `release_when_empty` for a grant with nothing loaded). Slice
S3d: `route_view`, what Whisper's GPU/CPU choice (L12) reads before it asks. Slice S3e: L11's one
shared cell (`shares_with`, `l11_arithmetic`, `_try_beside`): a lease that shares with a busy holder
(Whisper with a music lease) is granted beside it only when the declared peaks fit the card's total
and the live free-VRAM reading does not veto; the card's VRAM reading is dropped on every change of
holder (`commit_memory.forget_vram_reading`). Slice S4: the music engines are clients (`music:yue2`,
`music:acestep`, built by `music.engine_base.SupervisedEngine`), so the cell has its production pair;
`holders_in_the_way` names a waiter's holders for the music job's `card_wait`. Not here yet: the probe
of foreign GPU use (S8).
"""

import asyncio
import contextlib
import contextvars
import logging
import math
import time
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, Tuple

from .. import cpu_work as _cpu_work
from . import commit_memory as _commit_memory
from . import llama_server_supervisor as _sup
from .llama_server_supervisor import CardQueueRefusal

logger = logging.getLogger(__name__)

YieldOutcome = Literal["let_go", "nothing_to_give", "refused", "could_not_let_go"]
_YIELD_OUTCOMES = ("let_go", "nothing_to_give", "refused", "could_not_let_go")
# "Nothing to give" means no model; "could not let go" means a model that is there and was not
# unloaded (ADR-052 L6): only the first two release.
_RELEASING_OUTCOMES = ("let_go", "nothing_to_give")
_YIELD_COSTS = ("cheap", "engine")

# How often a waiter looks again without being woken: a supervisor that stops, or a use that
# closes, does not wake the line. Tests set it lower.
_LEASE_POLL_S = 0.1
# How soon a waiter asks a holder again after `refused` or `could_not_let_go` is the supervisors'
# `_LEASE_ASK_AGAIN_S`, read where used; at most `_MAX_SWAP_ROUNDS` asks per holder and waiter.
# The wait estimate's figure for a cheap holder letting go (Whisper unloads in 0.35-0.61 s, ADR-052
# "GPU figures on speech"). A music engine's stop has no figure: unknown. A supervisor that is going
# counts its own `_expected_stop_s()` (`_supervisor_stop_estimate`).
_CHEAP_YIELD_S = 1.0
# A peer's whole trip (ADR-052 L13, point 2): its wait and its own `expected_s` fit in this, or it is
# refused at once, whatever bound its door passes. A rule, not only arithmetic (Mike #17, 2026-11-23):
# a peer's long audio that only the CPU would take in time is refused too.
PEER_TRIP_S = 120.0
# How long a start by hand (`acquire(..., yields_idle=True)`) waits for the idle holders it asked to let
# go, when the owner's `swap_wait_s` is not longer (Mike, 2026-11-24: anything idle with no queue may be
# unloaded for a start by hand). A Whisper model goes in about a second, a local LLM engine in its stop.
BY_HAND_YIELD_S = 30.0

# Every lease, by holder name, granted or not. Registered when built.
_CARD_LEASES: Dict[str, "CardLease"] = {}
# The waiters, in the order they came. A waiter holds nothing (L2).
_LEASE_LINE: List["_LeaseWaiter"] = []
# The yields running, held until done: a contender's deadline or cancel never cuts one in half, and a
# task nobody references can be collected.
_YIELDS: "set[asyncio.Future]" = set()
# Why the line was closed (the backend stops), or None.
_CLOSED: Optional[str] = None

_WHERE_ORDER = ("busy_holder", "yielding", "lease_line", "card_queue")


# --- refusals (L3) -------------------------------------------------------------------------------


class LeaseRefusal(CardQueueRefusal):
    """The lease let this request go without the card. `wait` is the wait estimate (L10) when one
    was made, else None."""

    def __init__(self, message: str, holder_alias: str = "", holder_engine: str = "",
                 wait: Optional[Dict[str, Any]] = None):
        super().__init__(message, holder_alias, holder_engine)
        self.wait = wait


class LeaseWaitedTooLong(LeaseRefusal):
    """The deadline passed. It names the holder, why it is busy and the bound."""


class LeaseWouldWaitOnItself(LeaseRefusal):
    """The request in `_CARD_HOLD` stands on the very holder it would wait for, a lease or a
    supervisor: refused at once, as the card queue's rule 9 does."""


class LeaseSkipped(LeaseRefusal):
    """`background=True` (the glossary's **background call**, its lease carrier) and the card is not
    free right now: nothing waited, nothing yielded. What follows is the client's (ADR-052 L4): the
    Whisper preload stays lazy, a background transcription would take the CPU twin when L12 passes it (the
    twin is built; no door sends a background transcription into L12 yet), the embedder's steady-state
    query its CPU model. Music engines have no CPU twin and never ask with
    `background`: a job waits in the line."""


class LeaseTakenOut(LeaseRefusal, _sup.TakenOutOfTheCardQueue):
    """A person's Stop took this waiter out of the lease line (`leave_card_queue`, rule 12)."""


class LeaseRefusedForPeer(LeaseRefusal):
    """A peer's request: worded for a foreign node, "right now", with the estimate when known. It
    names no alias, agent or chat (ADR-044 point 7), and carries no holder."""


class LeaseBusy(LeaseRefusal):
    """A person asked a busy lease to let go (`let_go_by_hand`). It names `who_uses()`."""


class LeaseClosed(LeaseRefusal):
    """The backend is stopping: no lease is granted any more."""


# --- helpers ----------------------------------------------------------------------------------------


def _clock() -> float:
    """The card queue's clock, read where used so that a test's clock applies to both lines."""
    return _sup._swap_clock()


def _on_the_owner_loop_now(fn: Callable[..., None], *args: Any) -> None:
    """Run the synchronous `fn` on the owner loop: here when this is that loop or none runs, else
    handed over in call order. Unlike `_now_on_the_owner_loop`, a plain thread (no running loop)
    is handed over too: thread clients (L9) release from their own threads."""
    owner = _sup._OWNER_LOOP
    if owner is None or owner.is_closed() or not owner.is_running():
        fn(*args)
        return
    try:
        here: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
    except RuntimeError:
        here = None
    if here is owner:
        fn(*args)
        return
    try:
        owner.call_soon_threadsafe(fn, *args)
    except RuntimeError:  # the loop closed between the check and the call
        fn(*args)


def _positive(value: Any) -> Optional[float]:
    """A time in seconds, or None for unknown. 0, a negative figure, NaN or anything that is not a
    number is unknown: an estimate never reads 0 when nobody knows (L10)."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return seconds


def seconds_from_ratio(audio_s: Any, audio_s_per_wall_s: Any) -> Optional[float]:
    """Wall-clock seconds a piece of audio takes at a measured real-time ratio, or None for unknown.

    The ratio's orientation (ADR-052 L10) is **audio seconds per wall second**, the number
    `whisper_provider._realtime_ratio` prints (`duration / elapsed`): 20 means 20 s of audio per
    second of work. So the time is a DIVISION, `audio_s / ratio`: 30 s of audio at 20x is 1.5 s (a
    multiplication would say 600 s). A missing, zero, negative or NaN figure on either side is
    unknown, never 0 (L10): the estimate then stays unknown and a peer is refused. The provider
    (S3) turns its recorded ratio into `eta_s` and `expected_s` with this one function."""
    audio, ratio = _positive(audio_s), _positive(audio_s_per_wall_s)
    if audio is None or ratio is None:
        return None
    return _positive(audio / ratio)


def _wait_words(wait_s: Optional[float]) -> str:
    if wait_s is None:
        return "unknown"
    if wait_s < 90:
        return f"{wait_s:.0f} s"
    return f"about {round(wait_s / 60)} min (estimate)"


def _peer_wait_words(wait_s: Optional[float]) -> str:
    if wait_s is None:
        return "and how long is not known"
    if wait_s < 90:
        return f"for about {max(1, round(wait_s))} s"
    return f"for about {round(wait_s / 60)} min"


def _a_lease(holder: Any) -> bool:
    return bool(getattr(holder, "IS_CARD_LEASE", False))


def _stands_on_supervisor(hold: Any, alias: str) -> bool:
    """`_this_request_holds` for the hold the waiter was asked with."""
    if hold is None or not getattr(hold, "live", False):
        return False
    borrow = getattr(hold, "borrow", None)
    return hold.alias == alias or (borrow is not None and borrow.alias == alias)


# --- the lease --------------------------------------------------------------------------------------


class _Use:
    """One open use of a granted lease: what it is for, since when (wall clock), its arrival on the
    card's one clock, and the `_CARD_HOLD` it was opened under (None outside a request)."""

    __slots__ = ("what", "since", "arrival", "hold")

    def __init__(self, what: str, arrival: int, hold: Any):
        self.what = what
        self.since = time.time()
        self.arrival = arrival
        self.hold = hold


class _Ticket:
    """Where the owner loop puts the use it opened for one `use()` call."""

    __slots__ = ("use",)

    def __init__(self) -> None:
        self.use: Optional[_Use] = None


class CardLease:
    """One client's standing on the card: registered once; granted, used, released many times.

    The holder surface the card queue reads (ADR-052 section 2.2.1) is `alias`, `ENGINE`,
    `EXCLUSIVE_ON_THE_CARD`, `DROPS_PARKED_ON_STOP`, the state word, `_draining`, `_swap_for`,
    `_busy_for_the_card`, `_begin_swap_drain`, `_end_swap_drain`, `_parked_conversations`,
    `give_up_for`, `stop_hint` and `who_uses`. A lease has no `_proc`, `props`, `port`,
    `_start_lock` or `_in_flight`: those are a supervisor's and are never read for one.

    `eta_s` is the client's one callback for how long it will keep the card: seconds of work left
    (wall clock), or None for unknown; 0, a negative figure and NaN also read as unknown. A client
    that derives it from a real-time ratio uses `seconds_from_ratio`, which divides."""

    IS_CARD_LEASE = True
    # Read by `LlamaServerSupervisor._excludes` unchanged: every lease excludes every other holder.
    EXCLUSIVE_ON_THE_CARD = True
    DROPS_PARKED_ON_STOP = False

    def __init__(self, holder: str, *, kind: str, engine: str, display: str,
                 vram_mib: Optional[int],
                 yield_cost: Literal["cheap", "engine"],
                 on_yield: Callable[[str], Awaitable[YieldOutcome]],
                 waits_for_engines: bool = False,
                 shares_with: Optional[Callable[["CardLease"], bool]] = None,
                 eta_s: Callable[[], Optional[float]] = lambda: None,
                 stop_hint: str = ""):
        if not holder or not isinstance(holder, str):
            raise ValueError(f"a card lease needs a holder name, not {holder!r}")
        if yield_cost not in _YIELD_COSTS:
            raise ValueError(f"unknown yield cost {yield_cost!r}: one of {_YIELD_COSTS}")
        if vram_mib is not None and (not isinstance(vram_mib, int) or vram_mib <= 0):
            raise ValueError(f"vram_mib is MiB above 0, or None for unknown, never {vram_mib!r}")
        known = _CARD_LEASES.get(holder)
        if known is not None and (known._granted or known._uses or known._waiters()):
            raise ValueError(f"'{holder}' already has a lease that holds or waits for the card")
        self.holder = holder
        self.kind = kind
        self.ENGINE = engine
        self.display = display
        self.vram_mib = vram_mib
        self.yield_cost = yield_cost
        self._on_yield = on_yield
        # Whether this lease waits for a busy supervisor and swaps an idle one out by the owner-start
        # rule, with the swap on (section 2.2 item 12; ADR-052 section 3.2 builds music engines with
        # True, Mike #70; Whisper too since S3b, item 1a decided 2026-11-23). False: it waits for a
        # supervisor holder until that holder goes by itself. No lease ever loads beside a
        # supervisor (Mike #68).
        self.waits_for_engines = waits_for_engines
        # L11's one shared cell (S3e): True for a holder this lease may be granted beside when the
        # arithmetic proves it fits (Whisper: a busy music lease). The arithmetic is this module's
        # (`l11_arithmetic`), never the client's; None (every other lease) never shares.
        self.shares_with = shares_with
        self._eta_s = eta_s
        self.stop_hint = stop_hint
        # The holder this grant was made beside (L11), and the declared peak it was fitted with.
        self._beside: Optional["CardLease"] = None
        self._beside_peak: Optional[int] = None
        self._granted = False
        self._grant_no = 0
        self._granted_at: Optional[float] = None
        self._since: Optional[float] = None
        self._starting = False
        self._draining = False
        self._swap_for: Optional[str] = None
        self._uses: List[_Use] = []
        self._yield_task: Optional[asyncio.Task] = None
        _CARD_LEASES[holder] = self

    def __repr__(self) -> str:
        return f"CardLease({self.holder!r}, {self.state})"

    # -- what it is now --

    @property
    def alias(self) -> str:
        """The holder name: the key in `card_holders`, `card_queue` and `stop_local_engine`."""
        return self.holder

    @property
    def state(self) -> str:
        """`free`, `waiting`, or, granted, `starting` (the client loads), `running` (loaded) or
        `draining` (marked for a contender while it lets go): the supervisors' words on purpose."""
        if not self._granted:
            return "waiting" if self._waiters() else "free"
        if self._draining:
            return "draining"
        return "starting" if self._starting else "running"

    @property
    def granted(self) -> bool:
        return self._granted

    @property
    def beside(self) -> Optional[str]:
        """The alias of the holder this lease was granted beside by L11, while both hold the card;
        None otherwise (`card_holders`' `beside`)."""
        other = self._beside
        return other.alias if self._granted and other is not None and other._granted else None

    def _waiters(self) -> List["_LeaseWaiter"]:
        return [w for w in _LEASE_LINE if w.lease is self and not w.granted]

    def who_uses(self) -> str:
        """The open uses' labels, for a refusal or a log line."""
        labels = [use.what for use in self._uses]
        if labels:
            return ", ".join(labels)
        if self._granted and self._starting:
            return "loading"
        return "nothing"

    def _named(self) -> str:
        """The display name with what it is busy with, as the wait estimate's `behind` lists it."""
        return f"{self.display} ({self.who_uses()})" if self._uses else self.display

    def _eta(self) -> Optional[float]:
        """The holder's remaining time from its client, or None. A client that fails says nothing."""
        try:
            return _positive(self._eta_s())
        except Exception:
            logger.debug("card: %s could not say how long it holds the card", self.display, exc_info=True)
            return None

    def mark_starting(self) -> None:
        """The client loads under its grant (a model load, an engine boot): busy for the card."""
        if self._granted:
            self._starting = True

    def mark_running(self) -> None:
        """The client has loaded: busy only while a use is open."""
        if self._starting:
            self._starting = False
            _the_card_changed()
            _wake()

    # -- the holder surface (section 2.2.1) --

    def _excludes(self, other: Any) -> bool:
        """Every lease excludes every other holder, lease or supervisor (L11 makes the one exception
        from S3 on, by measurement and recorded as `beside`, never here)."""
        return other is not self

    def _busy_for_the_card(self, state: str, but_for: Any = None, before: Optional[int] = None) -> bool:
        """Busy is one source, the lease's own (L1): starting, draining, or an open use. A use opened
        under `but_for` (a card-borrow's own request) does not count; nor, with `before`, a use that
        arrived after the acting waiter, which waits behind that waiter (L5a point 4)."""
        if state in ("starting", "draining"):
            return True
        return any((but_for is None or use.hold is not but_for) and (before is None or use.arrival < before)
                   for use in self._uses)

    def _mark(self, contender: str) -> None:
        self._swap_for = contender
        self._draining = True

    def _begin_swap_drain(self, new: Any) -> None:
        """Mark this lease as draining for `new`, in the stretch of the last check that it is idle:
        from here no new use is admitted until the yield has ended (L5, L6)."""
        self._mark(new.alias)

    def _end_swap_drain(self) -> None:
        """The yield gave up: the lease serves again. Waiting uses are admitted only by L5a (a
        contender still waiting is still earlier)."""
        self._swap_for = None
        self._draining = False
        _settle()
        _wake()

    def _would_wait_on_itself(self, hold: Any, contender: str) -> Optional["LeaseWouldWaitOnItself"]:
        """The refusal of a supervisor's start that would wait for this lease while its own request
        (`hold`, its `_CARD_HOLD`) stands on it through an open use (section 2.2 item 4), or None."""
        if hold is None or not any(use.hold is hold for use in self._uses):
            return None
        refusal = LeaseWouldWaitOnItself(
            f"{contender} would wait for {self.display} ('{self.alias}'), which this very request uses "
            f"({self.who_uses()}); refused at once instead of waiting on itself",
            self.alias, self.ENGINE,
        )
        logger.info("card: %s refused: %s", contender, refusal)
        return refusal

    async def _parked_conversations(self) -> int:
        """A lease parks nothing: a yield drops no conversation."""
        return 0

    async def give_up_for(self, contender: str, bound: Optional[float] = None) -> YieldOutcome:
        """Run `on_yield` for `contender` and return its outcome; release on `let_go` and
        `nothing_to_give`, end the mark on `refused` and `could_not_let_go`, and never raise for
        either. The yield is its own task: `bound` (a `_swap_clock` deadline, as a card-queue
        request's `stop_bound`, or None) bounds only this wait, and raises `TimeoutError` at its end
        while the yield goes on and the mark stays until it has ended."""
        task = self._yield_task
        if task is None:
            if not self._granted:
                return "nothing_to_give"
            if not self._draining:
                self._mark(contender)
            task = self._spawn_yield(contender)
        shielded = asyncio.shield(task)
        if bound is None:
            return await shielded
        return await asyncio.wait_for(shielded, max(0.0, bound - _clock()))

    def _ask_to_let_go(self, contender: str) -> None:
        """Mark this idle lease for `contender` and start its yield, in one synchronous stretch."""
        self._mark(contender)
        logger.info("card: asked %s to let go for %s", self.display, contender)
        self._spawn_yield(contender)

    def _spawn_yield(self, contender: str) -> asyncio.Task:
        # A fresh context: the yield is the holder's own work, not the contender's request, so it
        # carries neither the contender's card hold nor its peer mark.
        task = asyncio.get_running_loop().create_task(self._run_yield(contender), context=contextvars.Context())
        self._yield_task = task
        _YIELDS.add(task)
        task.add_done_callback(_YIELDS.discard)
        return task

    async def _run_yield(self, contender: str) -> YieldOutcome:
        grant_no = self._grant_no
        why = f"yield for {contender}"
        outcome: Optional[str] = None
        try:
            outcome = await self._on_yield(why)
            if outcome not in _YIELD_OUTCOMES:
                logger.warning("card: %s answered a yield with %r, read as could_not_let_go",
                               self.display, outcome)
                outcome = "could_not_let_go"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # What the client holds is not known: keeping the grant is the side that cannot let the
            # next holder load over a model still on the card.
            logger.warning("card: %s could not let go for %s: %s", self.display, contender, exc)
            outcome = "could_not_let_go"
        finally:
            self._yield_task = None
            ours = self._grant_no == grant_no
            if outcome in _RELEASING_OUTCOMES and ours and self._granted:
                self._release_now(why)
            elif ours and self._draining:
                self._end_swap_drain()
            if outcome == "could_not_let_go":
                logger.warning("card: %s did not let go for %s: its memory is still held", self.display, contender)
            elif outcome == "refused":
                logger.info("card: %s refused to let go for %s: in use", self.display, contender)
            _settle()
            _wake()
        return outcome  # type: ignore[return-value]

    # -- grant, use, release --

    def _grant(self, what: str) -> None:
        self._granted = True
        self._grant_no += 1
        self._granted_at = _clock()
        self._since = time.time()
        self._starting = False
        self._beside = None
        self._beside_peak = None
        _the_card_changed()
        logger.info("card: %s granted (%s)", self.display, what)

    def _open(self, what: str, arrival: int, hold: Any) -> _Use:
        use = _Use(what, arrival, hold)
        self._uses.append(use)
        return use

    def _close_now(self, use: Optional[_Use]) -> None:
        if use is not None and use in self._uses:
            self._uses.remove(use)
            _wake()

    def _release_now(self, why: str) -> None:
        if not self._granted:
            return
        held = _clock() - (self._granted_at if self._granted_at is not None else _clock())
        self._granted = False
        self._starting = False
        self._draining = False
        self._swap_for = None
        self._beside = None
        self._beside_peak = None
        _the_card_changed()
        logger.info("card: %s let go (%s), held %.0f s", self.display, why, held)
        # The next waiter is granted in this same stretch (L5a point 2).
        _settle()
        _wake()

    @_sup.on_the_owner_loop
    async def acquire(self, what: str, *, wait_s: Optional[float], background: bool = False,
                      check: Optional[Callable[[], Awaitable[None]]] = None,
                      on_wait: Optional[Callable[[Dict[str, Any]], None]] = None,
                      expected_s: Optional[float] = None, vram_mib: Optional[int] = None,
                      yields_idle: bool = False) -> None:
        """Hold the card: at once when nothing excludes this lease and nobody earlier waits (L1,
        L5a), else in `_LEASE_LINE` up to `wait_s` seconds from now (None: until `check` raises or
        the caller is cancelled). A granted lease is granted again at once. `background` never
        waits or yields (`LeaseSkipped`); a peer's request (`serving_a_peer`) waits only when its
        whole trip is known and fits: the estimate plus its own `expected_s` within `PEER_TRIP_S`
        and `wait_s` (L13). `on_wait` is told the wait estimate (L10) whenever who it
        waits for changes; `expected_s` is how long this request will hold the card once granted,
        which the estimate of a later waiter counts. `vram_mib` is this request's own declared peak
        (Whisper's by the call's length band, S3e), which L11's fit reads instead of the lease's
        `vram_mib` (None: the lease's). `yields_idle` is a start by hand (`wait_s=0`): refused at once
        as ever while a holder is busy or has a queue behind it, but when every holder in the way is
        idle and the lease may ask or swap it (`_may_ask`, `_swaps_engines`) the request goes into the
        line for those holders only, for `swap_wait_s` (at least `BY_HAND_YIELD_S`), and takes the
        same ask and swap path a queued request takes."""
        await _enter(self, what, wait_s=wait_s, background=background, check=check, on_wait=on_wait,
                     expected_s=expected_s, vram_mib=vram_mib, opens_a_use=False, yields_idle=yields_idle)

    @_sup.on_the_owner_loop
    async def _open_a_use(self, what: str, ticket: "_Ticket", **acquire_kw: Any) -> None:
        await _enter(self, what, opens_a_use=True, ticket=ticket, **acquire_kw)

    def _close_ticket_now(self, ticket: "_Ticket") -> None:
        self._close_now(ticket.use)
        ticket.use = None

    @contextlib.asynccontextmanager
    async def use(self, what: str, **acquire_kw: Any):
        """Acquire if not granted, then one open use for the body (re-entrant: a granted lease opens
        one more). While a contender waits for this lease no new use is admitted (L5). The use
        closes however the body ends; the grant stays (a warm grant: granted with no use) until the
        client releases it or yields."""
        # The ticket is the caller's: a caller on another loop cancelled just as the owner loop
        # opened its use never sees the answer, and still closes what was opened for it.
        ticket = _Ticket()
        try:
            await self._open_a_use(what, ticket, **acquire_kw)
            yield
        finally:
            _on_the_owner_loop_now(self._close_ticket_now, ticket)

    def release(self, why: str) -> None:
        """Let go of the card, from any path and any thread: unload, stop, child exit, shutdown, a
        failed load, a cancelled task. Releasing a lease that is not granted does nothing. Open uses
        stay open (an engine whose child exited under a job's use): their next start acquires
        again."""
        _on_the_owner_loop_now(self._release_now, why)

    def release_when_empty(self, why: str, holds: Optional[Callable[[], bool]] = None) -> None:
        """Let go of a grant nobody uses and nothing fills (ADR-052 3.1, "warm means granted with 0
        uses"; S3b): from any thread, decided on the owner loop, after any use closed before this
        call. Released only when granted, no use is open, the client is not loading (`starting`),
        no yield runs (`draining`: the yield releases by its own outcome), and `holds()` says the
        client keeps nothing on the card. An open use keeps the grant: it is about to load, or runs."""
        _on_the_owner_loop_now(self._release_if_empty_now, why, holds)

    def _release_if_empty_now(self, why: str, holds: Optional[Callable[[], bool]]) -> None:
        if not self._granted or self._uses or self._starting or self._draining:
            return
        try:
            kept = bool(holds()) if holds is not None else False
        except Exception:
            # What the client holds is not known: keeping the grant cannot let a load go over it.
            logger.warning("card: %s could not say what it holds; the grant is kept", self.display, exc_info=True)
            return
        if not kept:
            self._release_now(why)

    def acquire_blocking(self, what: str, **acquire_kw: Any) -> None:
        """`acquire` for a thread client (L9): waits on the owner loop. On the owner loop's own
        thread it raises at once instead of deadlocking."""
        owner = _sup._OWNER_LOOP
        if owner is None or owner.is_closed() or not owner.is_running():
            raise RuntimeError("card: acquire_blocking needs the owner loop running (bind_owner_loop)")
        try:
            here: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
        except RuntimeError:
            here = None
        if here is owner:
            raise RuntimeError(
                f"card: {self.display} called acquire_blocking on the owner loop, which it would block "
                "while it waits there; await acquire() instead"
            )
        asyncio.run_coroutine_threadsafe(self.acquire(what, **acquire_kw), owner).result()

    def rebind(self, on_yield: Callable[[str], Awaitable[YieldOutcome]]) -> None:
        """A new client object takes over the same standing (Whisper's providers-reload hand-over):
        one lease follows the model, never two."""
        self._on_yield = on_yield

    @_sup.on_the_owner_loop
    async def let_go_by_hand(self, why: str, bound: Optional[float] = None) -> bool:
        """A person's Stop: an idle lease lets go now, a busy one raises `LeaseBusy` naming its uses.
        `bound` is seconds to wait for the yield (None: until it ends). True when the card was let
        go (or nothing was loaded); False when the client refused or could not, nothing is granted,
        or `bound` ran out first (the yield goes on and the lease stays marked until it ends: the
        caller may ask again or look at the state)."""
        if not self._granted:
            return False
        if not self._draining and self._busy_for_the_card(self.state):
            raise LeaseBusy(f"{self.display} is busy with {self.who_uses()}", self.alias, self.ENGINE)
        deadline = None if bound is None else _clock() + bound
        try:
            outcome = await self.give_up_for(why, deadline)
        except asyncio.TimeoutError:
            return False
        return outcome in _RELEASING_OUTCOMES

    # -- what `card_holders` shows (L7) --

    def card_holder_row(self, state: str) -> Dict[str, Any]:
        return {
            "alias": self.alias, "engine": self.ENGINE, "state": state, "port": None,
            "kind": self.kind, "display": self.display, "busy": self._busy_for_the_card(state),
            "uses": [{"what": use.what, "since": use.since} for use in self._uses],
            "vram_mib": self.vram_mib, "since": self._since, "yield_cost": self.yield_cost,
            "eta_s": self._eta(), "beside": self.beside,
        }


def lease_named(holder: str) -> Optional[CardLease]:
    """The lease registered under `holder`, or None."""
    return _CARD_LEASES.get(holder)


def holders_in_the_way(lease: CardLease) -> List[Dict[str, Any]]:
    """`{"alias", "engine"}` of what keeps `lease`'s earliest waiter from the card, for a client that
    names its wait by holder (the music job's `card_wait`, S4): the holders on the card first (a
    supervisor or another lease); with none, the waiters and card-queue places ahead of it. [] when
    the lease does not wait. Synchronous and read-only: nothing is asked, no number is drawn."""
    mine = sorted((w for w in _LEASE_LINE if w.lease is lease and not w.granted and w.out is None),
                  key=_line_key)
    if not mine:
        return []
    found: List[Dict[str, Any]] = []
    rest: List[Dict[str, Any]] = []
    for kind, what, _state in _blockers(mine[0]):
        alias, engine = _holder_of(kind, what)
        row = {"alias": alias, "engine": engine or None}
        target = found if kind in ("holder", "draining") else rest
        if alias and row not in target:
            target.append(row)
    return found or rest


# --- the line ---------------------------------------------------------------------------------------


class _LeaseWaiter:
    """One request for a lease: a grant (`acquire`) or a use (`use`). It draws its arrival from the
    card's one clock when it is asked for, so a request that goes at once and one that waited are
    ordered alike. Its deadline is fixed here (L4). `hold` is the `_CARD_HOLD` it was asked under,
    `for_a_peer` whether it serves a peer (`serving_a_peer`)."""

    __slots__ = ("lease", "what", "arrival", "queued_at", "give_up_at", "wait_s", "for_a_peer", "hold",
                 "opens_a_use", "expected_s", "event", "granted", "use", "made_the_grant", "grant_no",
                 "out", "asks", "said", "ticket", "vram_mib", "fitted_for")

    def __init__(self, lease: CardLease, what: str, wait_s: Optional[float], opens_a_use: bool,
                 expected_s: Optional[float], ticket: Optional[_Ticket] = None,
                 vram_mib: Optional[int] = None):
        self.lease = lease
        self.what = what
        # This request's declared peak for L11 (MiB), else the lease's (S3e).
        self.vram_mib = vram_mib if isinstance(vram_mib, int) and vram_mib > 0 else lease.vram_mib
        # The holders L11 last tried a fit beside for, so a waiting round reads the card once per
        # change of who holds it, not every poll.
        self.fitted_for: Optional[Tuple[int, ...]] = None
        self.arrival = next(_sup._CARD_ARRIVAL)
        self.queued_at = time.time()
        self.for_a_peer = _sup.peer_being_served() is not None
        self.expected_s = _positive(expected_s)
        if self.for_a_peer:
            # What is left of the trip once its own work is counted, or its door's bound when shorter.
            if self.expected_s is None:
                # Its own work unknown: the trip is unknown, never "no work" (L10). `_enter` refuses
                # such a peer at once, on a free card as on a blocked one (L13 point 2, Johnny #30-4);
                # the whole 120 s is only the bound it would have had.
                trip_left = PEER_TRIP_S
            else:
                trip_left = max(0.0, PEER_TRIP_S - self.expected_s)
            wait_s = trip_left if wait_s is None else min(max(0.0, float(wait_s)), trip_left)
        self.wait_s = wait_s
        self.give_up_at = math.inf if wait_s is None else _clock() + max(0.0, float(wait_s))
        self.hold = _sup.current_card_hold()
        self.opens_a_use = opens_a_use
        self.event: Optional[asyncio.Event] = None
        self.granted = False
        self.use: Optional[_Use] = None
        self.made_the_grant = False
        self.grant_no = 0
        self.out: Optional[LeaseRefusal] = None
        self.asks: Dict[CardLease, Tuple[int, float]] = {}
        self.said: Optional[Tuple[Any, ...]] = None
        self.ticket = ticket

    def bound_words(self) -> str:
        return "until cancelled" if math.isinf(self.give_up_at) else f"up to {self.wait_s:g} s"


def _goes_before(earlier: _LeaseWaiter, later: _LeaseWaiter) -> bool:
    """Whether `earlier` is served before `later`: the one clock, but an owner goes ahead of a
    waiting peer and a peer stands behind every owner (ADR-044 point 3, rule 7)."""
    if earlier.for_a_peer != later.for_a_peer:
        return not earlier.for_a_peer
    return earlier.arrival < later.arrival


def _line_key(waiter: _LeaseWaiter) -> Tuple[bool, int]:
    return (waiter.for_a_peer, waiter.arrival)


def _granted_leases() -> List[CardLease]:
    return sorted((lease for lease in _CARD_LEASES.values() if lease._granted),
                  key=lambda lease: lease._granted_at or 0.0)


def _blockers(waiter: _LeaseWaiter) -> List[Tuple[str, Any, Optional[str]]]:
    """What keeps `waiter` from going now, as (kind, what, state): `holder` (a supervisor or another
    lease on the card), `waiter` (an earlier lease waiter it must not overtake), `place` (an earlier
    place in the card queue), `draining` (its own lease lets go for a contender). Empty: it goes.

    A granted lease is granted again at once unless it is draining; a new use of one is admitted
    unless it is draining or an earlier waiter it blocks is waiting for it (L5). An ungranted lease
    goes when no holder is on the card and nobody earlier waits in either line (L5a): every lease
    excludes every holder."""
    lease = waiter.lease
    found: List[Tuple[str, Any, Optional[str]]] = []
    if lease._granted:
        if lease._draining:
            found.append(("draining", lease, "draining"))
        if not waiter.opens_a_use:
            return found
        shared = lease._beside
        if (shared is not None and shared._granted and shared._busy_for_the_card(shared.state)
                and not _within_the_fitted_peak(waiter)):
            # A grant made beside a busy holder (L11) was fitted for one peak; a use that declares a
            # larger one (a longer length band) is fitted again before it is admitted (S3e).
            found.append(("holder", shared, shared.state))
        found.extend(("waiter", other, None) for other in _LEASE_LINE
                     if other is not waiter and not other.granted and other.lease is not lease
                     and _goes_before(other, waiter))
        # A supervisor's place that came earlier waits for this lease (every lease excludes every
        # supervisor), so a new use does not jump it either (L5, S2).
        found.extend(("place", place, None) for place in _sup._CARD_QUEUE
                     if waiter.for_a_peer or place.arrival < waiter.arrival)
        return found
    found.extend(("holder", sup, state) for sup, state in _sup._live_claims())
    found.extend(("holder", other, other.state) for other in _granted_leases() if other is not lease)
    found.extend(("waiter", other, None) for other in _LEASE_LINE
                 if other is not waiter and not other.granted and other.lease is not lease
                 and _goes_before(other, waiter))
    # Every place in the card queue is an owner's, and every lease excludes every supervisor.
    found.extend(("place", place, None) for place in _sup._CARD_QUEUE
                 if waiter.for_a_peer or place.arrival < waiter.arrival)
    return found


def _proceed(waiter: _LeaseWaiter) -> None:
    """Grant `waiter`, in the stretch of the check that found nothing in its way."""
    lease = waiter.lease
    if not lease._granted:
        lease._grant(waiter.what)
        waiter.made_the_grant = True
    waiter.grant_no = lease._grant_no
    if waiter.opens_a_use:
        waiter.use = lease._open(waiter.what, waiter.arrival, waiter.hold)
        if waiter.ticket is not None:
            waiter.ticket.use = waiter.use
    waiter.granted = True
    if waiter in _LEASE_LINE:
        _LEASE_LINE.remove(waiter)
    if waiter.event is not None:
        waiter.event.set()


def _settle() -> None:
    """Grant every waiter that nothing keeps any more, earliest first (L5a point 2). Synchronous:
    called on every release, every end of a mark and every waiter's leaving, and by each waiter's
    own round, which is how a supervisor that went is noticed."""
    progressed = True
    while progressed:
        progressed = False
        for waiter in sorted(_LEASE_LINE, key=_line_key):
            if waiter.granted or waiter.out is not None:
                continue
            if not _blockers(waiter):
                _proceed(waiter)
                progressed = True


def _wake() -> None:
    for waiter in _LEASE_LINE:
        if waiter.event is not None:
            waiter.event.set()


def _leaves(waiter: _LeaseWaiter) -> None:
    """Out of the line however it ended, in one synchronous stretch. A grant that reached a waiter
    whose caller has already gone (a cancel racing the grant) is given back: nobody would release
    it."""
    if waiter in _LEASE_LINE:
        _LEASE_LINE.remove(waiter)
    if waiter.granted:
        lease = waiter.lease
        if waiter.use is not None:
            lease._close_now(waiter.use)
            waiter.use = None
        if (waiter.made_the_grant and lease._granted and lease._grant_no == waiter.grant_no
                and not lease._uses and not lease._draining):
            lease._release_now("its request left before using the grant")
    _settle()
    _wake()


# --- the wait estimate (L10) ----------------------------------------------------------------------


def _may_ask(waiter: _LeaseWaiter) -> bool:
    """Whether `waiter` may ask an idle holder to let go. An owner may, whatever the swap switch
    says (yielding a lease is not gated by it, ADR-052 item 2's default). A peer only on the
    owner's terms: the swap on and no owner's request waiting in either line (ADR-044, amendment
    2026-11-06); the holder idle is asked of each holder."""
    if not waiter.for_a_peer:
        return True
    if not _sup.swap_policy()[0] or _sup._CARD_QUEUE:
        return False
    return not any(not other.for_a_peer for other in _LEASE_LINE if not other.granted)


def _estimate(waiter: _LeaseWaiter, blockers: Optional[List[Tuple[str, Any, Optional[str]]]] = None
              ) -> Dict[str, Any]:
    """`{"wait_s", "known", "behind", "where"}`. The holders on the card go together (the longest
    counts); the waiters ahead go one after another (their `expected_s` add up). Any part unknown:
    `wait_s` None and `known` False, never 0."""
    if blockers is None:
        blockers = _blockers(waiter)
    if not blockers:
        return {"wait_s": None, "known": False, "behind": [], "where": "lease_line"}
    known, holders_s, ahead_s = True, 0.0, 0.0
    behind: List[str] = []
    wheres = set()
    may_ask = _may_ask(waiter)
    before = None if waiter.for_a_peer else waiter.arrival
    for kind, what, state in blockers:
        seconds: Optional[float]
        if kind == "holder" and _a_lease(what):
            if what._draining:
                seconds, where = _yield_estimate(what), "yielding"
            elif what._busy_for_the_card(state, before=before):
                seconds, where = what._eta(), "busy_holder"
            elif may_ask:
                seconds, where = _yield_estimate(what), "yielding"
            else:
                seconds, where = None, "busy_holder"
            behind.append(what._named())
            if seconds is not None:
                holders_s = max(holders_s, seconds)
        elif kind == "holder":
            # A supervisor has no ETA (L10): one that is going counts its stop, one that is not
            # (busy, starting, or idle with nobody stopping it for this waiter) is unknown.
            swapping = what._draining or state == "stopping" or (
                _swaps_engines(waiter) and state == "running" and not what._busy_for_the_card(state))
            seconds = _supervisor_stop_estimate(what) if swapping else None
            where = "yielding" if swapping else "busy_holder"
            behind.append(f"{what.ENGINE} '{what.alias}'")
            if seconds is not None:
                holders_s = max(holders_s, seconds)
        elif kind == "draining":
            seconds, where = None, "yielding"
            behind.append(f"{what.display} (letting go for {what._swap_for})")
        elif kind == "waiter":
            seconds, where = what.expected_s, "lease_line"
            behind.append(f"{what.lease.display} ({what.what})")
            if seconds is not None:
                ahead_s += seconds
        else:
            seconds, where = None, "card_queue"
            behind.append(f"local engine '{what.alias}'")
        wheres.add(where)
        if seconds is None:
            known = False
    return {
        "wait_s": holders_s + ahead_s if known else None,
        "known": known,
        "behind": behind,
        "where": next(where for where in _WHERE_ORDER if where in wheres),
    }


def _yield_estimate(holder: CardLease) -> Optional[float]:
    return _CHEAP_YIELD_S if holder.yield_cost == "cheap" else None


def _supervisor_stop_estimate(sup: Any) -> Optional[float]:
    """Seconds a going supervisor's stop takes (ADR-052 S3a, Ark #10-4): its own `_expected_stop_s()`,
    the last stop it measured, else its grace default. None (unknown) when it keeps no figure."""
    expected = getattr(sup, "_expected_stop_s", None)
    if not callable(expected):
        return None
    try:
        return _positive(expected())
    except Exception:
        logger.debug("card: the stop figure of %s could not be read", getattr(sup, "alias", sup), exc_info=True)
        return None


def _why_it_waits(waiter: _LeaseWaiter, kind: str, what: Any, state: Optional[str],
                  queued: bool = True) -> Tuple[str, str]:
    """(who, why) of the first thing in the way, in the owner's words. `queued` is whether the request
    is in the line, where an idle holder is asked to let go or swapped out; a refusal at once (a start
    that does not wait, a background call) asked nobody, and says so."""
    if kind == "holder" and _a_lease(what):
        if what._draining:
            return what.display, "yielding"
        if what._busy_for_the_card(state, before=None if waiter.for_a_peer else waiter.arrival):
            return what.display, f"busy: {what.who_uses()}"
        if _may_ask(waiter):
            if not queued:
                return what.display, "idle, not asked to let go: this request is not queued for the card"
            return what.display, "idle, asking it to let go"
        return what.display, "idle; a peer does not ask it to let go"
    if kind == "holder":
        if state == "running" and _swaps_engines(waiter) and not what._draining:
            busy = what._busy_for_the_card(state)
            idle = ("a local engine on the card, idle; swapping it out" if queued else
                    "a local engine on the card, idle; not swapped out: this request is not queued for the card")
            return f"{what.ENGINE} '{what.alias}'", "a local engine on the card, busy" if busy else idle
        return f"{what.ENGINE} '{what.alias}'", f"a local engine on the card, {state}"
    if kind == "draining":
        return what.display, f"letting go for {what._swap_for}"
    if kind == "waiter":
        return f"{what.lease.display} ({what.what})", "earlier in line"
    return f"local engine '{what.alias}'", "earlier in the card queue"


def _holder_of(kind: str, what: Any) -> Tuple[str, str]:
    """(alias, engine) a refusal carries for the first thing in the way."""
    if kind in ("holder", "draining"):
        return what.alias, what.ENGINE
    if kind == "waiter":
        return what.lease.alias, what.lease.ENGINE
    return what.alias, ""


# --- L11: the one shared cell (S3e) ---------------------------------------------------------------


def _card_vram() -> Tuple[Optional[int], Optional[int]]:
    """(free, total) MiB through the agent map's reader (`commit_memory.read_vram_mib`), never the
    per-process figures (ADR-052 L11, "The quantities"). Blocking."""
    return _commit_memory.read_vram_mib()


# What L11 reads the card with; `conftest.py` starts every test with no reading.
_READ_THE_CARD: Callable[[], Tuple[Optional[int], Optional[int]]] = _card_vram


def _the_card_changed() -> None:
    """The card's holders changed: drop the cached free-VRAM reading (`total` stays). Never raises."""
    try:
        _commit_memory.forget_vram_reading()
    except Exception:
        logger.debug("card: the VRAM reading could not be dropped", exc_info=True)


def _within_the_fitted_peak(waiter: "_LeaseWaiter") -> bool:
    """A new use of a grant made beside a holder declares no more than the grant was fitted with."""
    fitted, peak = waiter.lease._beside_peak, waiter.vram_mib
    return fitted is not None and peak is not None and peak <= fitted


def _declared_peak(holder: Any) -> Optional[int]:
    """A resident's declared peak in MiB, or None: a lease's `vram_mib`, a supervisor child's
    `predicted_vram_mib` (llama-server; a Strata child has none)."""
    figure = holder.vram_mib if _a_lease(holder) else getattr(holder, "predicted_vram_mib", None)
    return figure if isinstance(figure, int) and not isinstance(figure, bool) and figure > 0 else None


def _resident_name(holder: Any) -> str:
    return holder.display if _a_lease(holder) else f"{holder.ENGINE} '{holder.alias}'"


def _reserve_of(supervisors: List[Any]) -> Tuple[int, str]:
    """(MiB, source): each resident engine's own `vram_reserve()`, the largest, never a sum; with no
    engine on the card the 2048 MiB desktop reserve. May read a Strata base config: blocking."""
    found: List[Tuple[int, str]] = []
    for sup in supervisors:
        reader = getattr(sup, "vram_reserve", None)
        try:
            mib, source = reader() if callable(reader) else (_sup._DESKTOP_RESERVE_MIB, "no reserve of its own")
        except Exception:
            logger.debug("card: the reserve of %s could not be read", getattr(sup, "alias", sup), exc_info=True)
            mib, source = _sup._DESKTOP_RESERVE_MIB, "its reserve could not be read"
        found.append((int(mib), f"{source}, {sup.ENGINE} '{sup.alias}'"))
    if not found:
        return _sup._DESKTOP_RESERVE_MIB, (f"the {_sup._DESKTOP_RESERVE_MIB} MiB desktop reserve: no "
                                           "local engine on the card")
    return max(found)


def l11_arithmetic(peak_mib: Optional[int], residents: List[Tuple[str, Optional[int], bool]], *,
                   reserve_mib: int, free_mib: Optional[int], total_mib: Optional[int]) -> Dict[str, Any]:
    """ADR-052 L11 in MiB. `residents`: every holder but the contender, as (name, peak, grows).
    Declared check `need = peak + Σ peaks + reserve <= total`, then the live veto `free >= peak +
    growth_left` (the whole peak of every growing resident, a lease; a supervisor allocates at its
    start). Any unknown size or reading is `unknown`, never "fits"; a `need` above a known total does
    not fit whatever `free` is, read or not (Zcode #88-6)."""
    out: Dict[str, Any] = {"peak": peak_mib, "reserve": reserve_mib, "total": total_mib,
                           "free": free_mib, "need": None, "growth_left": None}
    if peak_mib is None:
        return {**out, "outcome": "unknown", "why": "the contender's own peak is unknown"}
    unsized = [name for name, peak, _grows in residents if peak is None]
    if unsized:
        return {**out, "outcome": "unknown", "why": f"{', '.join(unsized)} has no declared peak"}
    need = peak_mib + sum(peak for _name, peak, _grows in residents) + reserve_mib
    growth_left = sum(peak for _name, peak, grows in residents if grows)
    out.update(need=need, growth_left=growth_left)
    if total_mib is not None and need > total_mib:
        return {**out, "outcome": "does_not_fit",
                "why": f"{need} MiB declared against the card's {total_mib} MiB"}
    if total_mib is None or free_mib is None:
        return {**out, "outcome": "unknown", "why": "the card's VRAM cannot be read"}
    if free_mib < peak_mib + growth_left:
        return {**out, "outcome": "does_not_fit",
                "why": (f"{free_mib} MiB free, {peak_mib + growth_left} MiB needed now ({peak_mib} of its "
                        f"own and {growth_left} the holders may still grow)")}
    return {**out, "outcome": "fits",
            "why": f"{need} of {total_mib} MiB declared, {free_mib} MiB free"}


def _shared_holders(waiter: "_LeaseWaiter", blockers: List[Tuple[str, Any, Optional[str]]]) -> List[CardLease]:
    """The busy lease holders `waiter` may be granted beside, or [] when anything else is in its way
    (a supervisor first of all: Mike #68's exclusion comes before any arithmetic)."""
    lease = waiter.lease
    if lease.shares_with is None or not blockers:
        return []
    found: List[CardLease] = []
    for kind, what, state in blockers:
        if kind != "holder" or not _a_lease(what) or what is lease:
            return []
        if what._draining or not what._busy_for_the_card(state):
            return []
        try:
            shares = bool(lease.shares_with(what))
        except Exception:
            logger.debug("card: %s could not say whether it shares with %s", lease.display, what.display,
                         exc_info=True)
            shares = False
        if not shares:
            return []
        found.append(what)
    return found


def _fit_now(waiter: "_LeaseWaiter", reading: Dict[str, Any]) -> Dict[str, Any]:
    """L11's arithmetic on the residents of this very stretch and a reading already made."""
    rows = [(_resident_name(holder), _declared_peak(holder), _a_lease(holder))
            for holder, _state in _sup._live_holders() if holder is not waiter.lease]
    fit = l11_arithmetic(waiter.vram_mib, rows, reserve_mib=reading["reserve"],
                         free_mib=reading["free"], total_mib=reading["total"])
    fit["reserve_source"] = reading["reserve_source"]
    return fit


async def _read_for_the_fit(waiter: "_LeaseWaiter") -> Dict[str, Any]:
    """The card's free and total VRAM and the reserve, read in a thread (point 4). A None reading is
    dropped from the cache, so it is not served for 30 s (Mike #11, item 13)."""
    supervisors = [holder for holder, _state in _sup._live_holders()
                   if holder is not waiter.lease and not _a_lease(holder)]

    def read() -> Tuple[Optional[int], Optional[int], Tuple[int, str]]:
        free, total = _READ_THE_CARD()
        return free, total, _reserve_of(supervisors)

    try:
        free, total, (reserve, source) = await asyncio.to_thread(read)
    except Exception:
        logger.debug("card: the card could not be read for L11", exc_info=True)
        free, total = None, None
        reserve, source = _sup._DESKTOP_RESERVE_MIB, "the card could not be read"
    if free is None or total is None:
        _the_card_changed()
    return {"free": free, "total": total, "reserve": reserve, "reserve_source": source}


def _holders_key(holders: List[CardLease]) -> Tuple[int, ...]:
    return tuple(x for holder in holders for x in (id(holder), holder._grant_no))


async def _try_beside(waiter: "_LeaseWaiter", blockers: List[Tuple[str, Any, Optional[str]]]
                      ) -> List[Tuple[str, Any, Optional[str]]]:
    """L11 at the grant: read the card, check again in the stretch of the grant (L1), grant beside the
    busy holder on `fits`. Returns what is still in the way ([] once granted). One reading per change
    of those holders, not per poll; any other outcome leaves the waiter where it was."""
    holders = _shared_holders(waiter, blockers)
    if not holders or _holders_key(holders) == waiter.fitted_for:
        return blockers
    waiter.fitted_for = _holders_key(holders)
    reading = await _read_for_the_fit(waiter)
    if waiter.granted or waiter.out is not None:
        return []
    blockers = _blockers(waiter)
    now = _shared_holders(waiter, blockers)
    if not now or _holders_key(now) != waiter.fitted_for:
        return blockers
    fit = _fit_now(waiter, reading)
    lease, holder = waiter.lease, now[0]
    if fit["outcome"] != "fits":
        logger.info("card: L11 says %s for %s beside %s (%s)", fit["outcome"], lease.display,
                    holder.display, fit["why"])
        return blockers
    _proceed(waiter)
    lease._beside = holder
    lease._beside_peak = max(lease._beside_peak or 0, waiter.vram_mib)
    logger.info("card: %s loaded beside %s (fits: %d of %d MiB declared, %d MiB free; reserve %d MiB, %s)",
                lease.display, holder.display, fit["need"], fit["total"], fit["free"], fit["reserve"],
                fit["reserve_source"])
    return []


# --- acquire ----------------------------------------------------------------------------------------


def _refuse(waiter: _LeaseWaiter, refusal: LeaseRefusal) -> LeaseRefusal:
    logger.info("card: %s refused: %s", waiter.lease.display, refusal)
    return refusal


def _holder_it_stands_on(waiter: _LeaseWaiter, blockers: List[Tuple[str, Any, Optional[str]]]) -> Any:
    """The holder in the way that the request itself stands on, or None: a supervisor's alias it
    holds or borrowed, or a lease with an open use under the same hold."""
    hold = waiter.hold
    if hold is None or not getattr(hold, "live", False):
        return None
    for kind, what, _state in blockers:
        if kind != "holder":
            continue
        if _a_lease(what):
            if not any(use.hold is hold for use in what._uses):
                continue
        elif not _stands_on_supervisor(hold, what.alias):
            continue
        return what
    return None


def _refusal_for_a_self_wait(waiter: _LeaseWaiter, blockers: List[Tuple[str, Any, Optional[str]]]
                             ) -> Optional[LeaseRefusal]:
    """`LeaseWouldWaitOnItself` when the request stands on a holder in its way."""
    what = _holder_it_stands_on(waiter, blockers)
    if what is None:
        return None
    return _refuse(waiter, LeaseWouldWaitOnItself(
        f"{waiter.lease.display} would wait for '{what.alias}', which this very request stands on; "
        "refused at once instead of waiting on itself",
        what.alias, what.ENGINE,
    ))


_ENGINE_NAMES = {"strata": "Strata"}


def _cpu_contention() -> List[str]:
    """ADR-052 L12 point 3, what computes on the CPU now, each named for the owner's log ("a busy
    Strata 'flash'"). Only small CPU work counts (Mike, 2026-11-24): a resident child (a claim on the
    card), Strata or llama-server, counts when it is busy by its own test, `_busy_for_the_card` (a live
    `accepted_request` hold in `_CARD_HOLDS`, a call in flight, a child starting), not by a guess from
    timing (Zcode #69); an idle one does not. The embedding model and GLiNER count while they compute
    on the CPU (`cpu_work`). A llama-server kept off the card (`--device none`) is not counted, though
    it computes on the CPU: it is heavy, and nobody would run a transcription beside it. The CPU
    transcriptions are the Whisper provider's to add."""
    names: List[str] = []
    for sup, state in _sup._live_claims():
        if sup._busy_for_the_card(state):
            names.append(f"a busy {_ENGINE_NAMES.get(sup.ENGINE, sup.ENGINE)} '{sup.alias}'")
    names.extend(_cpu_work.computing_now())
    return names


@_sup.on_the_owner_loop
async def route_view(lease: CardLease, *, expected_s: Optional[float] = None,
                     vram_mib: Optional[int] = None) -> Dict[str, Any]:
    """What L12 reads before a transcription asks `lease` (ADR-052 L12, slice S3d), on the owner loop:
    the request's blockers as `_enter` would find them, with no place taken, no holder asked and
    nothing logged. The peer mark and the card hold travel with the call (contextvars). The probe
    draws a number on the card's clock, never a place: no waiter's order changes.

    `free`: nothing in the way (L12 point 1). `fit`: L11's answer (S3e) when everything in the way is
    a busy holder the lease shares with, for this request's declared peak `vram_mib` (`fits` is L12
    point 1's other half), else None. `wait_s`: the wait estimate (L10), None for long; a
    supervisor in the way that is not already going makes it None too (item 1a: an idle one would be
    swapped out at a cost no figure holds, its reload or Strata's parked conversations; a busy one
    has no ETA). `engine_stays` says so. `self_wait`: (alias, engine) of a holder the request itself
    stands on (section 3.5), else None. `cpu_contention`: L12 point 3, the engines computing on the
    CPU now (`_cpu_contention`). `behind`: who is in the way."""
    probe = _LeaseWaiter(lease, "L12", None, True, expected_s, vram_mib=vram_mib)
    blockers = _blockers(probe)
    view: Dict[str, Any] = {"free": not blockers, "wait_s": None, "known": False, "behind": [],
                            "engine_stays": False, "self_wait": None, "cpu_contention": _cpu_contention(),
                            "fit": None}
    if not blockers:
        return view
    if _shared_holders(probe, blockers):
        view["fit"] = _fit_now(probe, await _read_for_the_fit(probe))
        blockers = _blockers(probe)
        if not blockers:
            view["free"] = True
            return view
    estimate = _estimate(probe, blockers)
    stays = any(kind == "holder" and not _a_lease(what) and not (what._draining or state == "stopping")
                for kind, what, state in blockers)
    stands_on = _holder_it_stands_on(probe, blockers)
    view.update(wait_s=None if stays else estimate["wait_s"], known=estimate["known"] and not stays,
                behind=estimate["behind"], engine_stays=stays,
                self_wait=None if stands_on is None else (stands_on.alias, stands_on.ENGINE))
    return view


def _refusal_at_the_deadline(waiter: _LeaseWaiter, blockers: List[Tuple[str, Any, Optional[str]]]
                             ) -> LeaseRefusal:
    estimate = _estimate(waiter, blockers)
    if waiter.for_a_peer:
        return _refuse(waiter, LeaseRefusedForPeer(
            f"this node's GPU is busy with another job right now, {_peer_wait_words(estimate['wait_s'])}",
            wait=estimate,
        ))
    kind, what, state = blockers[0]
    who, why = _why_it_waits(waiter, kind, what, state,
                             queued=waiter.wait_s is None or waiter.wait_s > 0)
    alias, engine = _holder_of(kind, what)
    hint = getattr(what, "stop_hint", "") if kind == "holder" else ""
    # `wait_s` None has no deadline of its own (an owner's deadline can come from elsewhere in S2).
    if waiter.wait_s is None:
        bound = ""
    elif waiter.wait_s <= 0:
        bound = " at once"
    else:
        bound = f" for {waiter.wait_s:g} s"
    return _refuse(waiter, LeaseWaitedTooLong(
        f"{waiter.lease.display} waited{bound} for the card and was not granted it: {who} ({why})."
        + (f" {hint}" if hint else ""),
        alias, engine, wait=estimate,
    ))


def _ask_idle_holders(waiter: _LeaseWaiter, blockers: List[Tuple[str, Any, Optional[str]]]) -> None:
    """L6: when only lease holders stand in its way, the first waiter asks each idle one to let go.
    A busy holder is never marked and never asked; it is waited for and named."""
    if waiter.lease._granted or not blockers:
        return
    if any(kind != "holder" or not _a_lease(what) for kind, what, _state in blockers):
        return
    if not _may_ask(waiter):
        return
    now = _clock()
    before = None if waiter.for_a_peer else waiter.arrival
    for _kind, holder, state in blockers:
        if holder._draining or holder._busy_for_the_card(state, before=before):
            continue
        asks, next_at = waiter.asks.get(holder, (0, 0.0))
        if asks >= _sup._MAX_SWAP_ROUNDS or now < next_at:
            continue
        waiter.asks[holder] = (asks + 1, now + _sup._LEASE_ASK_AGAIN_S)
        holder._ask_to_let_go(waiter.lease.alias)


def _idle_holders_may_be_let_go(waiter: _LeaseWaiter, blockers: List[Tuple[str, Any, Optional[str]]]) -> bool:
    """Whether everything in `waiter`'s way is idle, with no queue, and the ask (`_ask_idle_holders`) or
    the swap (`_swap_idle_engines`) the line would make of it is allowed: the one test that lets a start
    by hand wait for them instead of being refused at once. Only lease holders, or only supervisors, as
    those two functions require; a holder already draining for another request, a holder busy for the
    card (to this waiter) and any place or waiter ahead of it refuse."""
    if waiter.lease._granted or not blockers or any(kind != "holder" for kind, _what, _state in blockers):
        return False
    if all(_a_lease(what) for _kind, what, _state in blockers):
        before = None if waiter.for_a_peer else waiter.arrival
        return _may_ask(waiter) and not any(
            what._draining or what._busy_for_the_card(state, before=before) for _kind, what, state in blockers)
    if any(_a_lease(what) for _kind, what, _state in blockers) or not _swaps_engines(waiter):
        return False
    return all(state == "running" and not what._draining and not what._busy_for_the_card(state)
               for _kind, what, state in blockers)


def _swaps_engines(waiter: _LeaseWaiter) -> bool:
    """Whether `waiter` swaps an idle supervisor out (section 2.2 item 12): a request for a lease
    that `waits_for_engines`, with the swap on (the owner-start rule, gated by the switch, Mike #70).
    An owner's request does; a peer's only on the owner's terms (Mike, 2026-11-24; L13 "As built"):
    the one predicate an inference peer's start reads, `why_a_peer_may_not_swap`, over the holders
    on the card (no owner's request waiting in either line, no holder busy or stopping). No other
    lease does."""
    if not (waiter.lease.waits_for_engines and not waiter.lease._granted and _sup.swap_policy()[0]):
        return False
    if not waiter.for_a_peer:
        return True
    holders = ([(sup, state) for sup, state in _sup._live_claims()]
               + [(other, other.state) for other in _granted_leases() if other is not waiter.lease])
    return _sup.why_a_peer_may_not_swap(holders) is None


def _swap_idle_engines(waiter: _LeaseWaiter, blockers: List[Tuple[str, Any, Optional[str]]]) -> None:
    """Mike #70: when only supervisors stand in its way, and nobody earlier waits in either line
    (L5a: those would be blockers too), the waiter swaps each idle one out, as an owner's start
    does; a busy one is waited for and named. At most `_MAX_SWAP_ROUNDS` marks per supervisor and
    waiter, `_LEASE_ASK_AGAIN_S` apart (a stop that failed leaves the holder serving)."""
    if not blockers or not _swaps_engines(waiter):
        return
    if any(kind != "holder" or _a_lease(what) for kind, what, _state in blockers):
        return
    now = _clock()
    stop_bound = None if math.isinf(waiter.give_up_at) else waiter.give_up_at
    for _kind, holder, _state in blockers:
        asks, next_at = waiter.asks.get(holder, (0, 0.0))
        if asks >= _sup._MAX_SWAP_ROUNDS or now < next_at:
            continue
        if _sup.swap_out_for_a_lease(holder, waiter.lease, stop_bound) is not None:
            waiter.asks[holder] = (asks + 1, now + _sup._LEASE_ASK_AGAIN_S)


def _tell(waiter: _LeaseWaiter, blockers: List[Tuple[str, Any, Optional[str]]],
          on_wait: Optional[Callable[[Dict[str, Any]], None]]) -> None:
    """One INFO line and one `on_wait` per change of who it waits for, none while it polls."""
    estimate = _estimate(waiter, blockers)
    kind, what, state = blockers[0]
    who, why = _why_it_waits(waiter, kind, what, state)
    said = (estimate["where"], tuple(estimate["behind"]), why)
    if said == waiter.said:
        return
    waiter.said = said
    logger.info("card: %s waits for %s (%s); wait %s, %s", waiter.lease.display, who, why,
                _wait_words(estimate["wait_s"]), waiter.bound_words())
    if on_wait is not None:
        try:
            on_wait(estimate)
        except Exception:
            logger.warning("card: %s could not show its wait", waiter.lease.display, exc_info=True)


async def _enter(lease: CardLease, what: str, *, wait_s: Optional[float], background: bool = False,
                 check: Optional[Callable[[], Awaitable[None]]] = None,
                 on_wait: Optional[Callable[[Dict[str, Any]], None]] = None,
                 expected_s: Optional[float] = None, opens_a_use: bool,
                 ticket: Optional[_Ticket] = None, vram_mib: Optional[int] = None,
                 yields_idle: bool = False) -> Optional[_Use]:
    """The one way to the card for a lease, run on the owner loop. The check that nothing is in the
    way and the grant are one synchronous stretch (L1), here and in `_settle`."""
    if _CLOSED is not None:
        raise LeaseClosed(f"{lease.display}: the backend is stopping ({_CLOSED}); the card is not handed out",
                          wait=None)
    waiter = _LeaseWaiter(lease, what, wait_s, opens_a_use, expected_s, ticket, vram_mib)
    blockers = _blockers(waiter)
    if waiter.for_a_peer and waiter.expected_s is not None and waiter.expected_s > PEER_TRIP_S:
        # Mike #17 (L13, point 2), Ark #20-1: the 120 s rule holds at the grant too. Its own work alone
        # outlasts the guest, so the card being free does not make the trip fit.
        estimate = _estimate(waiter, blockers)
        raise _refuse(waiter, LeaseRefusedForPeer(
            "this node cannot finish this transcription within the time the request is given, right now",
            wait=estimate,
        ))
    if waiter.for_a_peer and waiter.expected_s is None:
        # Mike #17 (L13, point 2), Johnny #30-4: a trip whose own work is unknown cannot be shown to fit
        # the guest's 120 s, so it is refused at the grant too, free card or not, in the same class
        # (L10: an unknown part leaves the trip unknown). The words name the cause, the audio's length
        # (Ark #44-1): the card may be free, and when it is blocked the unknown length is still what
        # makes the refusal certain (a known length could have waited its turn), so one text serves
        # both. The blocked-card branch below is only reached with a known length.
        estimate = _estimate(waiter, blockers)
        raise _refuse(waiter, LeaseRefusedForPeer(
            "this node cannot tell how long this audio is, so it cannot promise to finish it within "
            "the time the request is given",
            wait=estimate,
        ))
    if not blockers:
        _proceed(waiter)
        return waiter.use
    refusal = _refusal_for_a_self_wait(waiter, blockers)
    if refusal is not None:
        raise refusal
    if _shared_holders(waiter, blockers):
        # L11 (S3e): beside a busy holder it shares with, at once, when the fit proves it; a peer or a
        # background call that does not fit meets the rules below as before.
        blockers = await _try_beside(waiter, blockers)
        if waiter.granted:
            return waiter.use
        if not blockers:
            _proceed(waiter)
            return waiter.use
        refusal = _refusal_for_a_self_wait(waiter, blockers)
        if refusal is not None:
            raise refusal
    if background:
        kind, thing, state = blockers[0]
        who, why = _why_it_waits(waiter, kind, thing, state, queued=False)
        alias, engine = _holder_of(kind, thing)
        raise _refuse(waiter, LeaseSkipped(
            f"{lease.display} skipped ({what}): the card is not free right now, {who} ({why})",
            alias, engine, wait=_estimate(waiter, blockers),
        ))
    if waiter.for_a_peer:
        # Its own work unknown leaves the whole trip unknown (L10): refused like an unknown wait.
        estimate = _estimate(waiter, blockers)
        if (not estimate["known"] or waiter.expected_s is None
                or estimate["wait_s"] > waiter.give_up_at - _clock()):
            raise _refuse(waiter, LeaseRefusedForPeer(
                f"this node's GPU is busy with another job right now, {_peer_wait_words(estimate['wait_s'])}",
                wait=estimate,
            ))
    elif waiter.give_up_at <= _clock():
        if not (yields_idle and _idle_holders_may_be_let_go(waiter, blockers)):
            raise _refusal_at_the_deadline(waiter, blockers)
        # A start by hand over idle holders only: it waits for them to go, as a queued request would.
        waiter.wait_s = max(float(_sup.swap_policy()[1]), BY_HAND_YIELD_S)
        waiter.give_up_at = _clock() + waiter.wait_s
    waiter.event = asyncio.Event()
    _LEASE_LINE.append(waiter)
    try:
        while True:
            if waiter.granted:
                return waiter.use
            if waiter.out is not None:
                raise waiter.out
            waiter.event.clear()
            if check is not None:
                await check()
            _settle()
            if waiter.granted:
                return waiter.use
            if waiter.out is not None:
                raise waiter.out
            blockers = _blockers(waiter)
            if not blockers:  # `_settle` grants a waiter nothing keeps; only a refused one is left
                continue
            refusal = _refusal_for_a_self_wait(waiter, blockers)
            if refusal is not None:
                raise refusal
            if _shared_holders(waiter, blockers):
                blockers = await _try_beside(waiter, blockers)
                if waiter.granted:
                    return waiter.use
                if waiter.out is not None:
                    raise waiter.out
                if not blockers:
                    continue
            now = _clock()
            if now >= waiter.give_up_at:
                raise _refusal_at_the_deadline(waiter, blockers)
            _ask_idle_holders(waiter, blockers)
            _swap_idle_engines(waiter, blockers)
            _tell(waiter, blockers, on_wait)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(waiter.event.wait(), min(_LEASE_POLL_S, waiter.give_up_at - now))
    except BaseException:
        _leaves(waiter)
        raise


def _chat_leaves_the_lease_line(conversation_id: str) -> int:
    """A person's Stop in this chat: each lease waiter asked under a hold of that chat leaves the
    line now and meets `LeaseTakenOut` (rule 12). Returns how many.

    A mark it made is not cleared here: a holder it asked keeps `draining` until its yield ends, and
    `_run_yield` ends the mark whatever the outcome. Clearing it mid-yield would admit a use onto a
    model being unloaded (L5, L6). The Stop only ends the asking: nothing asks again for this waiter."""
    if not conversation_id:
        return 0
    taken = 0
    for waiter in list(_LEASE_LINE):
        hold = waiter.hold
        if (waiter.granted or waiter.out is not None or hold is None
                or getattr(hold, "conversation_id", None) != conversation_id):
            continue
        hold.taken_out = True
        waiter.out = LeaseTakenOut(
            f"{waiter.lease.display}: a person's Stop took this request ({waiter.what}) out of the line "
            "for the card",
            waiter.lease.alias, waiter.lease.ENGINE,
        )
        _LEASE_LINE.remove(waiter)
        if waiter.event is not None:
            waiter.event.set()
        taken += 1
    if taken:
        _settle()
        _wake()
    return taken


def close_the_line(why: str) -> None:
    """The backend stops: every waiter is refused with `LeaseClosed`, and so is every later request.
    Grants stay with their clients, which release on their own way down."""
    _on_the_owner_loop_now(_close_now, why)


def _close_now(why: str) -> None:
    global _CLOSED
    _CLOSED = why
    for waiter in list(_LEASE_LINE):
        if waiter.granted:
            continue
        waiter.out = LeaseClosed(f"{waiter.lease.display}: the backend is stopping ({why})")
        if waiter.event is not None:
            waiter.event.set()


# --- the hooks the supervisor module reads ---------------------------------------------------------


def _lease_holders() -> List[Tuple[CardLease, str]]:
    """(lease, state) of every granted lease, in the order they were granted."""
    return [(lease, lease.state) for lease in _granted_leases()]


def _lease_waiters() -> List[Tuple[int, CardLease, bool]]:
    """(arrival, lease, for_a_peer) of every lease waiter, in line order, for the card queue's side of
    L5a (S2): a supervisor's place stands behind a lease waiter with a lower arrival that the lease
    keeps from going (`lease.alias` names it), but never behind a peer's waiter when the place is an
    owner's (rule 7: an owner goes ahead of a peer). Nothing in the supervisor reads it in S1."""
    return [(waiter.arrival, waiter.lease, waiter.for_a_peer)
            for waiter in sorted(_LEASE_LINE, key=_line_key) if not waiter.granted]


def _lease_queue_rows() -> List[Dict[str, Any]]:
    """The lease waiters for `card_queue` (L7), after the card queue's own rows. `until_cancelled`
    says what a `null` `give_up_at` means for a lease: not "coming back from a description"."""
    now, clock = time.time(), _clock()
    return [
        {
            "alias": waiter.lease.alias,
            "kind": waiter.lease.kind,
            "what": waiter.what,
            "queued_at": waiter.queued_at,
            "give_up_at": None if math.isinf(waiter.give_up_at) else now + waiter.give_up_at - clock,
            "until_cancelled": math.isinf(waiter.give_up_at),
            "wait": _estimate(waiter),
        }
        for waiter in sorted(_LEASE_LINE, key=_line_key) if not waiter.granted
    ]


def _install_hooks() -> None:
    _sup._LEASE_HOLDERS = _lease_holders
    _sup._LEASE_WAITERS = _lease_waiters
    _sup._LEASE_QUEUE_ROWS = _lease_queue_rows
    _sup._LEASE_CHAT_LEAVES = _chat_leaves_the_lease_line


_install_hooks()
