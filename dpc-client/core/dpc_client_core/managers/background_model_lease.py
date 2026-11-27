"""
The card lease for the in-process background models: the embedding model (bge-m3, Active Recall,
`dpc_agent/memory.py`) and GLiNER (`dpc_agent/knowledge_graph.py`). ADR-052 section 3.4 (S6, S7); the
backlog card THE-EMBEDDER-GLINER-AND-SKILL-EMBEDDINGS-TAKE-THE-GPU-OUTSIDE-THE-CARD-LEASE-...; Mike's
"auto" rule of 2026-08-18: the card for a bulk reindex when it is free, the CPU in the steady state.

**Device settings keep their meaning.** `cpu` (and `mps`, which shares host memory and is no card)
never builds one of these and never asks for the card. `cuda`, and auto on a machine with CUDA, are
*lease-gated*: the model lives on the CPU and is on the card only inside a **stint**, which holds one
lease use. Nothing computes on the card outside one.

**A stint is bulk work** (the start-up index pass, a sleep pass's entity extraction): one use for the
batch, asked as a background call (`background=True`, `wait_s=0`; ADR-052 2.1, "granted only if the
card is free, otherwise CPU"), with an honest `expected_s` (items times a per-item card figure,
measured as it runs and conservative before, plus the two moves). It takes the card only when nothing
holds it and nobody waits: it never stands in the line, so it never asks an idle holder (a warm
Whisper, an idle music engine) to let go (L6) and never stands ahead of a later owner's request (L5a).
`LeaseSkipped`, any other refusal, a closed line, an error on the way or no owner loop (a script, a
test) leaves the batch on the CPU. A batch too small to repay the two moves (under `MIN_GPU_WORTH_S`
of CPU time) never asks.

**Yield.** A busy holder is never asked to let go (L6), so a stint lets go of its own accord: before
each compute call it reads whether an owner's request stands in either line
(`card_lease.an_owner_waits_for_the_card`; a background lease never waits in the line, so every waiter
it sees is a foreground request: Whisper, music, a local engine's start); when one does, the model is moved to the CPU under the
device lock, the use is closed and the grant released, and the rest of the batch continues on the CPU.
That is simpler than queueing again and never worse than the steady state. `eta_s` says the same in
numbers: a waiter has the card at the latest when the compute call running now ends and the model is
moved off. No idle grant is kept (every stint releases at its end); one left by a failed move answers
`on_yield` by moving to the CPU.

**Short calls** (one query embedding in a chat turn, one NER call) never start a stint: a move costs
about a second against tens of milliseconds of CPU work. They compute where the model is now, on the
card inside a running stint's use, else on the CPU, and never wait for the card; only for the device
lock, which every compute call takes.

**Moves are serialised with compute** by the device lock (an RLock the client holds around every
compute call), so no compute runs mid-move; and the lease is never asked for while the device lock
is held (ADR-052 Risks, "locks inside a client"). Taking the card at a stint's start and letting it
go at a stint's end are serialised by a second lock (`_gate`), so a stint that opens while the last
one closes asks for a new use only after the old one was closed and released (both are handed to the
owner loop in that order).

Pure Python and the same on Windows, Linux and macOS; torch is the client's concern, through its
`to_gpu` and `to_cpu` callbacks. A node without CUDA never builds one, so its path is unchanged.
"""

import asyncio
import contextlib
import logging
import threading
import time
from typing import Callable, Iterator, Optional, Tuple

logger = logging.getLogger(__name__)

# A bulk batch asks for the card only when the CPU would spend at least this long on it: below it, the
# two moves (onto the card and back) cost about as much as they save.
MIN_GPU_WORTH_S = 10.0


class BackgroundModelLease:
    """One in-process model's standing on the card, for a model that lives on the CPU and goes to the
    card only for bulk work. `to_gpu` and `to_cpu` move the client's loaded model and raise when they
    cannot; both are called under `device_lock`. The figures are seconds per item: `gpu_s_per_item`
    (a conservative start, replaced by what each compute call on the card measures),
    `cpu_s_per_item` (for the worth), and `move_s` for one move."""

    def __init__(self, holder: str, *, kind: str, engine: str, display: str, vram_mib: Optional[int],
                 to_gpu: Callable[[], None], to_cpu: Callable[[], None],
                 gpu_s_per_item: float, cpu_s_per_item: float, move_s: float):
        self.holder = holder
        self.kind = kind
        self.engine = engine
        self.display = display
        self.vram_mib = vram_mib
        self._to_gpu = to_gpu
        self._to_cpu = to_cpu
        self.cpu_s_per_item = float(cpu_s_per_item)
        self.move_s = float(move_s)
        self._gpu_s_per_item = float(gpu_s_per_item)
        # Held around every compute call and every move: no compute runs mid-move.
        self.device_lock = threading.RLock()
        # Guards the stint count; never held across a wait.
        self._state = threading.Lock()
        # Serialises taking the card at a stint's start with letting it go at a stint's end.
        self._gate = threading.Lock()
        self._on_gpu = False
        self._stints = 0
        # The open lease use (an ExitStack over `use_blocking`), or None.
        self._held: Optional[contextlib.ExitStack] = None
        # The compute call running on the card now: (since, items), for `eta_s`.
        self._computing: Optional[Tuple[float, int]] = None
        self._lease = None

    # -- what it is now --

    @property
    def on_gpu(self) -> bool:
        return self._on_gpu

    @property
    def device(self) -> str:
        """Where the model computes now: `cuda` inside a stint that holds the card, else `cpu`."""
        return "cuda" if self._on_gpu else "cpu"

    @property
    def in_stint(self) -> bool:
        return self._stints > 0

    def lease(self):
        """This model's card lease, built once (importing `card_lease` only here, on a CUDA node)."""
        if self._lease is None:
            from .card_lease import CardLease, lease_named

            known = lease_named(self.holder)
            if known is not None and (known.granted or known._uses or known._waiters()):
                # A replaced client of the same model left it standing: one lease per holder.
                known.rebind(self._on_yield)
                self._lease = known
            else:
                self._lease = CardLease(
                    self.holder, kind=self.kind, engine=self.engine, display=self.display,
                    vram_mib=self.vram_mib, yield_cost="cheap", on_yield=self._on_yield, eta_s=self._eta,
                )
        return self._lease

    def _eta(self) -> Optional[float]:
        """The lease's `eta_s`: the compute call running on the card now, what is left of it, plus one
        move off. That is when a waiter has the card, since the stint lets go before its next call
        when an owner's request waits. None (unknown) when nothing is on the card or moving there."""
        if not self._on_gpu and self._held is None:
            return None
        left = 0.0
        computing = self._computing
        if computing is not None:
            since, items = computing
            left = max(0.0, items * self._gpu_s_per_item - (time.monotonic() - since))
        return left + self.move_s

    # -- bulk work --

    def expected_s(self, items: int) -> float:
        """How long a stint of `items` holds the card when nobody waits: items on the card plus two moves."""
        return max(0, int(items)) * self._gpu_s_per_item + 2 * self.move_s

    def worth_the_card(self, items: int) -> bool:
        return max(0, int(items)) * self.cpu_s_per_item >= MIN_GPU_WORTH_S

    @contextlib.contextmanager
    def stint(self, what: str, items: int) -> Iterator[str]:
        """Bulk work of `items` (from a worker thread, never the owner loop's own): the first stint to
        open asks for one lease use and moves the model to the card when granted; stints that open
        while one runs join it; the last to close moves the model back and releases. Yields the device
        the batch starts on. `what` names the use in the card queue ("index rebuild"); it never carries
        chat content."""
        items = max(0, int(items))
        with self._gate:
            with self._state:
                self._stints += 1
                first = self._stints == 1
            if first and self.worth_the_card(items):
                self._take_the_card(what, items)
        try:
            yield self.device
        finally:
            with self._gate:
                with self._state:
                    self._stints -= 1
                    last = self._stints == 0
                if last:
                    with self.device_lock:
                        if self._held is not None or self._on_gpu:
                            self._let_go_locked(f"{what} done")

    def _take_the_card(self, what: str, items: int) -> None:
        """Called under `_gate` by the first stint: the card when it is free right now, else nothing."""
        from .llama_server_supervisor import CardQueueRefusal
        from .card_lease import an_owner_waits_for_the_card

        expected = self.expected_s(items)
        held = contextlib.ExitStack()
        try:
            lease = self.lease()
            held.enter_context(lease.use_blocking(what, wait_s=0, background=True, expected_s=expected))
        except CardQueueRefusal as exc:
            logger.info("%s: %s of %d items on the CPU: %s", self.display, what, items, exc)
            return
        except Exception as exc:
            # No owner loop (a script), its own thread, a cancel at shutdown: no card for this batch.
            logger.info("%s: %s of %d items on the CPU, the card lease could not be asked (%s: %s)",
                        self.display, what, items, type(exc).__name__, exc)
            return
        if not lease.granted or self._held is not None:
            # Not expected under `_gate`; the model never goes to the card without a grant of its own.
            held.close()
            logger.warning("%s: %s found the lease %s; it runs on the CPU", self.display, what,
                           "already held" if self._held is not None else "not granted")
            return
        if an_owner_waits_for_the_card(lease):
            # Granted, but an owner's request arrived meanwhile: let it go before moving anything.
            held.close()
            lease.release("an owner's request waits for the card")
            logger.info("%s: %s of %d items on the CPU, an owner's request waits for the card",
                        self.display, what, items)
            return
        with self.device_lock:
            self._held = held
            try:
                self._to_gpu()
            except Exception as exc:
                logger.warning("%s: the move to the card failed (%s); %s runs on the CPU",
                               self.display, exc, what)
                self._on_gpu = True  # where the weights are is not known: move them back first
                self._let_go_locked("the move to the card failed")
                return
            self._on_gpu = True
        logger.info("%s: %s of %d items on the card (about %.0f s expected)", self.display, what, items, expected)

    # -- compute --

    @contextlib.contextmanager
    def compute(self, items: int = 1) -> Iterator[str]:
        """Around one compute call of `items`: holds the device lock and yields the device the model
        is on. Inside a stint that holds the card, an owner's request waiting in either line makes it
        let go first (the batch goes on, on the CPU)."""
        with self.device_lock:
            if self._on_gpu and self._held is not None:
                from .card_lease import an_owner_waits_for_the_card

                if an_owner_waits_for_the_card(self._lease):
                    self._let_go_locked("yield for an owner's request waiting for the card")
            device = self.device
            since = time.monotonic()
            if device == "cuda":
                self._computing = (since, max(1, int(items)))
            try:
                yield device
            finally:
                self._computing = None
                if device == "cuda" and items > 0:
                    measured = (time.monotonic() - since) / items
                    # Half the last figure, half the new: a stable estimate that follows the card.
                    self._gpu_s_per_item = 0.5 * self._gpu_s_per_item + 0.5 * measured

    def model_dropped(self) -> None:
        """The client unloaded the model: nothing is on the card any more. A stint that holds the card
        keeps its use until it ends, and computes on the CPU meanwhile."""
        with self.device_lock:
            self._on_gpu = False

    # -- letting go --

    def _off_the_card_locked(self, why: str) -> bool:
        """Move the model to the CPU (the caller holds the device lock). False when the move failed:
        the weights are still on the card and the grant is kept (ADR-052 L6, `could_not_let_go`)."""
        if not self._on_gpu:
            return True
        try:
            self._to_cpu()
        except Exception as exc:
            logger.warning("%s: could not move off the card (%s, %s); the card is kept", self.display, why, exc)
            return False
        self._on_gpu = False
        return True

    def _let_go_locked(self, why: str) -> None:
        moved = self._off_the_card_locked(why)
        held, self._held = self._held, None
        if held is not None:
            held.close()
        if moved and self._lease is not None:
            self._lease.release(why)
            logger.info("%s: off the card (%s)", self.display, why)

    async def _on_yield(self, why: str) -> str:
        """The lease's `on_yield`, asked only of an idle grant (none is kept by design; one is left only
        by a move off the card that failed). The move runs in a thread, under the device lock."""
        return await asyncio.to_thread(self._yield_in_a_thread, why)

    def _yield_in_a_thread(self, why: str) -> str:
        with self.device_lock:
            if self._held is not None:
                return "refused"
            if not self._on_gpu:
                return "nothing_to_give"
            return "let_go" if self._off_the_card_locked(why) else "could_not_let_go"
