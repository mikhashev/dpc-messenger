"""The run gate: a lock that keeps the list of who waits at it (ADR-046 point 8).

Two locks decide when an agent's run starts. The agent's own lets one run at a
time through for that agent, whichever door it comes in by (a message from any
surface, `DpcAgentManager.process_message`; a queued task,
`DpcAgent._execute_task`). The room's lets one agent at a time answer in a group
(`CoreService._invoke_agent_in_group_serialized`). A bare `asyncio.Lock` does
not show its queue, so a chat waiting behind another chat looked exactly like a
chat nobody was answering. A `RunGate` is that lock with a registry beside it:
an entry is put in before the acquire and taken out in a `finally` after it, so
a waiter that is cancelled or times out leaves no trace. A person's Stop in a
chat that waits takes its message out of line the same way (`stop_waiting`),
and the run it waited for never starts.

What the screen reads is the agent's snapshot, `agent_turn_snapshot`:

- `turn` — the agent's current run in one conversation, or None when idle:
  `conversation_id`, `conversation_title`, `started_at`, `round`,
  `provider_alias`, `tool`, `wait_reason`.
- `waiting` — the chats waiting for this agent, in arrival order: each with
  `conversation_id`, `conversation_title`, `waiting_since`, `wait_reason` and
  `held_by`.

`wait_reason` is a closed list of four. On a turn only `model_call` (waiting for
the model) or `approval` (waiting for a person's approval of a tool), or None.
On a waiting item only `other_chat` (the agent is in another chat's turn) or
`room_busy` (the group's room lock is held by another agent's turn). `held_by`
names the turn that holds the lock, one level and no further: its
`conversation_id` and `wait_reason`, and for `room_busy` also its `agent_id`.

A task that already holds a gate passes through it again without waiting. The
Telegram bridge takes the agent's gate itself before it knows what the run will
carry, so the messages that arrive while it waits go into that run, and the
`process_message` it then calls must not wait for its own caller.

Every change is pushed whole through the listener (`set_turn_listener`), which
the service turns into the `agent_turn_updated` event. All state lives on the
event loop's thread; an approval waits on an executor thread, and its update is
handed to the loop with `call_soon_threadsafe`.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import itertools
import logging
import threading
import weakref
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from .utils import utc_now_iso

log = logging.getLogger(__name__)

#: The closed list. Which place carries which is in the module docstring.
TURN_WAIT_REASONS = ("model_call", "approval")
WAITING_WAIT_REASONS = ("other_chat", "room_busy")

# The fields of `turn` as the snapshot gives them; anything else on the record
# is the gate's own bookkeeping.
_TURN_FIELDS = (
    "conversation_id", "conversation_title", "started_at", "round",
    "provider_alias", "tool", "wait_reason",
)

# Arrival order across the agent's own gate and every room it waits at.
_ARRIVAL = itertools.count()

# The agent gate held by this task, with the turn it opened: (gate, turn). The
# turn rides along so that a task created inside a run and outliving it cannot
# write into the next run's turn.
_HELD: contextvars.ContextVar[Optional[Tuple["RunGate", Dict[str, Any]]]] = contextvars.ContextVar(
    "dpc_agent_run_gate_held", default=None,
)

_listener: Optional[Callable[[Dict[str, Any]], None]] = None


class StoppedWhileWaiting(Exception):
    """A person's Stop took this waiter out of line: its run never started."""


class RunGate:
    """One run at a time, and a list of who waits for it.

    `agent_id` names an agent's own gate; `room` (a group id) names a room's.
    `async with gate:` still works and takes the gate with an anonymous entry,
    which the snapshot shows as a held gate with no turn.
    """

    def __init__(self, agent_id: Optional[str] = None, room: Optional[str] = None):
        self._lock = asyncio.Lock()
        self.agent_id = agent_id
        self.room = room
        self._waiters: List[Dict[str, Any]] = []
        # An agent gate's current turn; a room gate's holder entry.
        self.turn: Optional[Dict[str, Any]] = None
        self.holder: Optional[Dict[str, Any]] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[int] = None
        self._anonymous: List[Any] = []
        # The task inside the hold; only that task passes through a second hold.
        self._holder_task: Optional[asyncio.Task] = None
        _ALL_GATES.add(self)

    # -- the lock ----------------------------------------------------------

    def locked(self) -> bool:
        return self._lock.locked()

    @property
    def waiters(self) -> List[Dict[str, Any]]:
        """The entries waiting now, oldest first (a copy)."""
        return list(self._waiters)

    @contextlib.asynccontextmanager
    async def hold(self, entry: Dict[str, Any], stoppable: bool = False):
        """Wait for the gate with `entry` registered, then hold it.

        `entry` names the waiter: `conversation_id` and `conversation_title`,
        and on a room gate also `agent_id`. Removed from the waiters in a
        `finally`, whether the acquire succeeded, was cancelled or timed out.

        A `stoppable` waiter is one a person's Stop in its chat may take out of
        line (`stop_waiting`): the hold then raises `StoppedWhileWaiting` and
        its body never runs.

        The task already holding the gate passes straight through: no entry,
        no wait, the turn stays the outer hold's. Another task, even one
        created inside the run, still waits.
        """
        task = asyncio.current_task()
        if task is not None and self._holder_task is task:
            yield self
            return
        self._bind_loop()
        entry = dict(entry)
        entry.setdefault("waiting_since", utc_now_iso())
        entry["_arrival"] = next(_ARRIVAL)
        if stoppable:
            entry["_task"] = asyncio.current_task()
        self._waiters.append(entry)
        waited = self._lock.locked()
        if waited:
            self._changed(entry)
        acquired = False
        stopped = False
        try:
            try:
                await self._lock.acquire()
                acquired = True
            except asyncio.CancelledError:
                # The Stop cancelled this task to wake it; that cancel is taken
                # back here. One from anyone else is still pending and goes on.
                if not entry.get("_stopped") or entry["_task"].uncancel() > 0:
                    raise
                stopped = True
        finally:
            self._waiters = [w for w in self._waiters if w is not entry]
            if not acquired and waited:
                self._changed(entry)
        if stopped:
            raise StoppedWhileWaiting(entry.get("conversation_id"))
        token = None
        try:
            if self.room is None:
                self.turn = {
                    "conversation_id": entry.get("conversation_id"),
                    "conversation_title": entry.get("conversation_title") or "",
                    "started_at": utc_now_iso(),
                    "round": None,
                    "provider_alias": None,
                    "tool": None,
                    "wait_reason": None,
                }
                token = _HELD.set((self, self.turn))
            self.holder = entry
            self._holder_task = task
            self._changed(entry)
            yield self
        finally:
            if token is not None:
                _HELD.reset(token)
            self.turn = None
            self.holder = None
            self._holder_task = None
            self._lock.release()
            self._changed(entry)

    async def __aenter__(self) -> "RunGate":
        cm = self.hold({"conversation_id": None, "conversation_title": ""})
        await cm.__aenter__()
        self._anonymous.append(cm)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        cm = self._anonymous.pop()
        return bool(await cm.__aexit__(exc_type, exc, tb))

    def stop_waiting(self, conversation_id: str) -> List[Dict[str, Any]]:
        """Take the stoppable waiters of one chat out of line; returns them.

        Each one's task is woken with a cancel its `hold` takes back, so the
        entry leaves the list in the same `finally` as any other waiter."""
        stopped = []
        for w in list(self._waiters):
            task = w.get("_task")
            if (task is None or w.get("_stopped") or task.done()
                    or (w.get("conversation_id") or self.room) != conversation_id):
                continue
            w["_stopped"] = True
            task.cancel()
            stopped.append(w)
        return stopped

    # -- the turn ----------------------------------------------------------

    def update_turn(self, conversation_id: Optional[str] = None,
                    only_if: Optional[Tuple[str, Any]] = None, **fields: Any) -> None:
        """Set fields of the current turn, from any thread.

        `conversation_id`, when given, must be the turn's: an update from a run
        that is not the one holding the gate is dropped. `only_if` is
        `(field, value)`: the update applies only while that field still holds
        that value, which is how a wait is cleared without clearing a later one.
        """
        if self._on_loop_thread():
            self._apply(conversation_id, only_if, fields, None)
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._apply, conversation_id, only_if, fields, None)
        except RuntimeError:  # the loop closed between the check and the call
            pass

    def _apply(self, conversation_id, only_if, fields, turn) -> None:
        current = self.turn
        if current is None or (turn is not None and current is not turn):
            return
        if conversation_id is not None and current.get("conversation_id") != conversation_id:
            return
        if only_if is not None and current.get(only_if[0]) != only_if[1]:
            return
        changed = False
        for key, value in fields.items():
            if key in _TURN_FIELDS and current.get(key) != value:
                current[key] = value
                changed = True
        if changed:
            self._changed(self.holder)

    # -- plumbing ----------------------------------------------------------

    def _bind_loop(self) -> None:
        try:
            self._loop = asyncio.get_running_loop()
            self._loop_thread = threading.get_ident()
        except RuntimeError:
            pass

    def _on_loop_thread(self) -> bool:
        return self._loop_thread is None or threading.get_ident() == self._loop_thread

    def _changed(self, entry: Optional[Dict[str, Any]]) -> None:
        agents = set()
        if self.room is None:
            if self.agent_id:
                agents.add(self.agent_id)
                # A room waiter's held_by reads this agent's turn.
                for room in _room_gates():
                    if room.holder and room.holder.get("agent_id") == self.agent_id:
                        agents.update(w.get("agent_id") for w in room._waiters)
        else:
            for e in [entry, self.holder, *self._waiters]:
                if e and e.get("agent_id"):
                    agents.add(e["agent_id"])
        for agent_id in sorted(a for a in agents if a):
            _notify(agent_id)


_ALL_GATES: "weakref.WeakSet[RunGate]" = weakref.WeakSet()
_RUN_GATES: Dict[str, RunGate] = {}


def get_run_gate(agent_id: Optional[str]) -> RunGate:
    """Return the run gate for an agent identity, creating it on first ask."""
    key = agent_id or "__singleton__"
    gate = _RUN_GATES.get(key)
    if gate is None:
        gate = RunGate(agent_id=agent_id)
        _RUN_GATES[key] = gate
    return gate


def _room_gates() -> List[RunGate]:
    return [g for g in list(_ALL_GATES) if g.room is not None]


def turn_title(service: Any, conversation_id: Optional[str],
               chat_context: Optional[Dict[str, Any]] = None) -> str:
    """The caller's `chat_name` if it had one, else the service's name for the chat."""
    title = (chat_context or {}).get("chat_name") or ""
    if title or not conversation_id:
        return title
    resolve = getattr(service, "_conversation_display_name", None)
    if resolve is None:
        return ""
    try:
        return resolve(conversation_id) or ""
    except Exception:
        return ""


@contextlib.asynccontextmanager
async def hold(gate: Any, entry: Dict[str, Any], stoppable: bool = False):
    """`gate.hold(entry)` for a `RunGate`; a plain lock is simply taken.

    The plain lock is what a test double hands an agent built without the
    manager; it serialises the same way and registers nothing.
    """
    if isinstance(gate, RunGate):
        async with gate.hold(entry, stoppable=stoppable):
            yield gate
    else:
        async with gate:
            yield gate


def stop_waiting(conversation_id: str) -> List[str]:
    """Stop pressed in a chat that waits: every gate lets its waiters for it go.

    Both kinds of gate: an agent's (`other_chat`) and a room's (`room_busy`).
    Only the waiters registered as stoppable go — a person's message, not a
    queued task. Returns the agent ids of the waiters taken out of line
    (`""` for the singleton agent), each once, in arrival order.
    """
    if not conversation_id:
        return []
    stopped: List[Tuple[int, str]] = []
    for gate in list(_ALL_GATES):
        for w in gate.stop_waiting(conversation_id):
            agent_id = gate.agent_id if gate.room is None else w.get("agent_id")
            stopped.append((w["_arrival"], agent_id or ""))
    stopped.sort()
    seen: List[str] = []
    for _, agent_id in stopped:
        if agent_id not in seen:
            seen.append(agent_id)
    if seen:
        log.info("Stop took %d waiting turn(s) out of line in %s: %s",
                 len(stopped), conversation_id, seen)
    return seen


# -- what the screen reads -------------------------------------------------

def _public_turn(turn: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if turn is None:
        return None
    return {k: turn.get(k) for k in _TURN_FIELDS}


def agent_turn_snapshot(agent_id: str) -> Dict[str, Any]:
    """`{agent_id, turn, waiting, ts}` for one agent, as the event carries it."""
    gate = _RUN_GATES.get(agent_id)
    turn = gate.turn if gate is not None else None
    items: List[Tuple[int, Dict[str, Any]]] = []
    if gate is not None:
        for w in gate._waiters:
            held_by = None
            if turn is not None:
                held_by = {"conversation_id": turn.get("conversation_id"),
                           "wait_reason": turn.get("wait_reason")}
            items.append((w["_arrival"], {
                "conversation_id": w.get("conversation_id"),
                "conversation_title": w.get("conversation_title") or "",
                "waiting_since": w.get("waiting_since"),
                "wait_reason": "other_chat",
                "held_by": held_by,
            }))
    for room in _room_gates():
        for w in room._waiters:
            if w.get("agent_id") != agent_id:
                continue
            held_by = None
            holder = room.holder
            if holder is not None:
                held_by = {"conversation_id": room.room,
                           "wait_reason": _holder_wait_reason(holder.get("agent_id"), room.room),
                           "agent_id": holder.get("agent_id")}
            items.append((w["_arrival"], {
                "conversation_id": w.get("conversation_id") or room.room,
                "conversation_title": w.get("conversation_title") or "",
                "waiting_since": w.get("waiting_since"),
                "wait_reason": "room_busy",
                "held_by": held_by,
            }))
    items.sort(key=lambda pair: pair[0])
    return {
        "agent_id": agent_id,
        "turn": _public_turn(turn),
        "waiting": [item for _, item in items],
        "ts": utc_now_iso(),
    }


def _holder_wait_reason(agent_id: Optional[str], conversation_id: str) -> Optional[str]:
    """What the agent holding a room is waiting for, one level down.

    Its turn's reason when the turn is in that room. An agent that holds the
    room and has not yet got its own gate is in another chat's turn, which is
    the waiting item's `other_chat`."""
    gate = _RUN_GATES.get(agent_id or "")
    if gate is None:
        return None
    if gate.turn is not None and gate.turn.get("conversation_id") == conversation_id:
        return gate.turn.get("wait_reason")
    if any(w.get("conversation_id") == conversation_id for w in gate._waiters):
        return "other_chat"
    return None


def agent_turn_fields(agent_id: str) -> Dict[str, Any]:
    """The `turn` and `waiting` an agent's row in `list_agents` carries."""
    snap = agent_turn_snapshot(agent_id)
    return {"turn": snap["turn"], "waiting": snap["waiting"]}


def set_turn_listener(listener: Optional[Callable[[Dict[str, Any]], None]]) -> None:
    """The one callable every snapshot is handed to, on the event loop's thread."""
    global _listener
    _listener = listener


def _notify(agent_id: str) -> None:
    if _listener is None:
        return
    try:
        _listener(agent_turn_snapshot(agent_id))
    except Exception as e:  # the screen is never worth failing a run over
        log.debug("agent_turn_updated for %s not delivered: %s", agent_id, e)


# -- the turn's own updates, from inside the run ---------------------------

def note_turn(**fields: Any) -> None:
    """Set fields of the turn this task runs in; nothing outside a turn."""
    held = _HELD.get()
    if held is None:
        return
    gate, turn = held
    if gate._on_loop_thread():
        gate._apply(None, None, fields, turn)


@contextlib.contextmanager
def turn_waits_for(reason: str, *, gate: Any = None,
                   conversation_id: Optional[str] = None, **fields: Any) -> Iterator[None]:
    """The turn's `wait_reason` is `reason` for the duration, then None again.

    Inside the run's own task the gate is found by itself. A tool waiting on
    an executor thread passes its agent's `gate` and the `conversation_id` it
    runs in, so an approval from a run outside the gate never marks another
    run's turn.
    """
    if reason not in TURN_WAIT_REASONS:
        raise ValueError(f"not a turn's wait_reason: {reason!r}")
    turn = None
    if gate is None:
        held = _HELD.get()
        if held is not None:
            gate, turn = held
    if not isinstance(gate, RunGate):
        yield
        return
    if turn is not None and gate._on_loop_thread():
        gate._apply(None, None, dict(fields, wait_reason=reason), turn)
    else:
        gate.update_turn(conversation_id=conversation_id, wait_reason=reason, **fields)
    try:
        yield
    finally:
        if turn is not None and gate._on_loop_thread():
            gate._apply(None, ("wait_reason", reason), {"wait_reason": None}, turn)
        else:
            gate.update_turn(conversation_id=conversation_id,
                             only_if=("wait_reason", reason), wait_reason=None)
