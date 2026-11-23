# dpc_client_core/providers/loop_clients.py
# One async HTTP client per event loop, for providers that cache a client.

import asyncio
import logging
import threading
import weakref
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# A pooled connection belongs to the loop that opened it. An agent tool runs its
# async handler on a loop of its own in a worker thread (tools/registry.py), so a
# client built on the service loop and reused there writes the request, and the
# answer is never read: llama-server replied in 12 s, the tool timed out at 300 s
# (2026-11-23). Hence one client per running loop.

_REGISTRY: "weakref.WeakSet[LoopClients]" = weakref.WeakSet()
_CLOSE_TIMEOUT_SECONDS = 5.0


def _running_loop() -> Optional[asyncio.AbstractEventLoop]:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


class LoopClients:
    """The client for the event loop that is running right now.

    `get()` builds one with `factory` on first use in a loop and returns the same
    object to that loop afterwards. A client made with no loop running (a
    constructor, sync code) is adopted by the first loop that asks, so a client a
    caller configured before the service loop started is still the service loop's;
    sync code keeps reading that first client, whatever became of its loop.
    Clients of closed loops are dropped on the next lookup — their sockets cannot
    be closed from here. `pin()` makes one explicit client answer for every loop.
    """

    def __init__(self, factory: Callable[[], Any]):
        self._factory = factory
        self._lock = threading.Lock()
        self._by_loop: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Any]" = (
            weakref.WeakKeyDictionary()
        )
        self._unbound: Optional[Any] = None  # made with no loop running, not yet adopted
        self._primary: Optional[Any] = None  # the first client made: what sync code reads
        self._pinned: Optional[Any] = None
        _REGISTRY.add(self)

    def _drop_closed(self) -> None:
        for loop in [lp for lp in self._by_loop.keys() if lp.is_closed()]:
            del self._by_loop[loop]

    def get(self) -> Any:
        with self._lock:
            if self._pinned is not None:
                return self._pinned
            self._drop_closed()
            loop = _running_loop()
            if loop is None:
                if self._primary is None:
                    self._primary = self._unbound = self._factory()
                return self._primary
            client = self._by_loop.get(loop)
            if client is None:
                if self._unbound is not None:
                    client, self._unbound = self._unbound, None
                else:
                    client = self._factory()
                self._by_loop[loop] = client
                if self._primary is None:
                    self._primary = client
            return client

    def peek(self) -> Optional[Any]:
        """The client `get()` would return without building one; None if there is none."""
        with self._lock:
            if self._pinned is not None:
                return self._pinned
            loop = _running_loop()
            if loop is None:
                return self._primary
            return self._by_loop.get(loop, self._unbound)

    def pin(self, client: Any) -> None:
        """Every loop gets `client` from here on (a test double, a swapped-in client)."""
        with self._lock:
            self._pinned = client

    def reset(self) -> None:
        """Forget every client, pinned ones too; the next `get()` builds anew
        (the endpoint changed). Nothing is closed here — the owner of each loop
        can still reach its own client only through `close_this_loop()`."""
        with self._lock:
            self._pinned = None
            self._unbound = None
            self._primary = None
            self._by_loop.clear()

    async def _close_for(self, loop: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            client = self._by_loop.pop(loop, None)
        close = getattr(client, "close", None)
        if close is not None:
            await close()


async def close_this_loop() -> None:
    """Close, on the running loop, every client any `LoopClients` built for it.

    The registry calls this before it closes a tool's loop: what a loop opened it
    must close itself, afterwards nobody can (an open socket left in a dying
    proactor loop is what stalled shutdown for Ollama). It never raises and never
    waits past a bound."""
    loop = _running_loop()
    if loop is None:
        return
    for holder in list(_REGISTRY):
        try:
            await asyncio.wait_for(holder._close_for(loop), _CLOSE_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001 — cleanup of a loop that is ending
            logger.debug("closing a provider client of the ending loop failed: %s", exc)


def _no_factory() -> Any:
    raise AttributeError("no client was built or pinned on this provider")


def holder_of(owner: Any) -> LoopClients:
    """`owner._clients`, made on first touch so a provider built without its
    `__init__` (a test double) can still have a client pinned on it."""
    holder = owner.__dict__.get("_clients")
    if holder is None:
        holder = owner.__dict__["_clients"] = LoopClients(_no_factory)
    return holder


def client_property() -> property:
    """`provider.client`: reads the running loop's client, assignment pins one."""

    def getter(self: Any) -> Any:
        return holder_of(self).get()

    def setter(self: Any, value: Any) -> None:
        holder_of(self).pin(value)

    return property(getter, setter)
