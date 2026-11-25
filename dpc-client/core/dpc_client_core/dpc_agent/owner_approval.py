"""
DPC Agent — the owner approval gate (ADR-053, "who asked decides what an agent may do").

A tool marked `ToolEntry.owner_approval_for_others` runs as the profile says when the
owner asked for the turn, and goes to the owner as an approval card on every call when
anyone else did. The rule is checked in one place, `ToolRegistry.execute`, through
`open_gate`, so that a tool added later cannot skip it.

What a call gets, decided by the tool's optional `approval_check(ctx, args)` (D-d):

- `Refuse(text)` — refused before any card: a path closed to the turn (2.7), a request the
  tool would refuse anyway. Nothing is asked.
- `Exempt(why)` — runs with no card: the return to the agent's own music job (D-e), a call
  the tool refuses by itself without side effects.
- `ToolAsks()` — the tool raises its own card (run_shell Tier 1, check_back, music_propose_job)
  and that card carries the asker (D-b): one card per call, not two.
- `Ask(details, cost)` — the gate raises the card, with "what exactly" and, for a tool that
  spends the GPU card, the cost line (2.6).

Bounds (2.8, D-i): at most one foreign-request card pending per chat and two across the
process; a call past either is refused at once. The card is the schedule card's machinery
(`tools.core._await_schedule_approval`), so it reaches the same surfaces, is withdrawn by
the same Stop and Kill, and refuses at once when nobody could be asked (no interface client
and no Telegram origin). 60 s without an answer is a refusal that says the owner is not in
focus.

Per call, not per turn: an approval marks only the call it was given for (a contextvar set
around the handler), which is what the tools' second owner checks read
(`may_run_owner_tool`).
"""

from __future__ import annotations

import contextvars
import json
import logging
import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

# The card's lifetime is the schedule card's (60 s, ADR-053 2.4).
APPROVAL_TTL_SECONDS = 60

# 2.8 and D-i: pending foreign-request cards, per chat and across the process. The pool is
# four workers (loop._TOOL_WORKERS); two cards leave two for every other agent's tools.
MAX_PENDING_PER_CHAT = 1
MAX_PENDING_PER_PROCESS = 2

# The task_type of the gate's own card on the schedule machinery (tools.core words).
OWNER_APPROVAL_TASK_TYPE = "owner_approval"

# The cost line of a tool that spends the GPU card (2.6; measured 2026-11-25 in the live
# log: Strata start 11:27:31, smoke passed 11:27:56).
GPU_CARD_COST_LINE = (
    "Uses the GPU card: a local engine leaves the card and comes back in about 25 s, "
    "and the next turn may run cold."
)

# How much of "what exactly" a card shows.
DETAILS_LIMIT = 600


@dataclass(frozen=True)
class Refuse:
    text: str


@dataclass(frozen=True)
class Exempt:
    why: str = ""


@dataclass(frozen=True)
class ToolAsks:
    """The tool raises its own card, and puts the asker on it (D-b)."""


@dataclass(frozen=True)
class Ask:
    details: str = ""
    cost: str = ""


# ---------------------------------------------------------------------------
# Who asked
# ---------------------------------------------------------------------------

def _is_internal_call(ctx: Any) -> bool:
    """A context a builder marked as an internal call (`ToolContext.internal_call`), carrying
    no asker, no source and no class. Only such a context is not gated.

    The absence of the three fields alone used to be enough (Zcode's review of ADR-053,
    #154): a context that lost them on its way was waved through as the owner's. Now it
    fails closed, and the few builders that mean "no turn, nobody asked" say so. A real
    asker or source always wins over the marker. DpcAgent.process sets all three on every
    turn and never sets the marker."""
    return (getattr(ctx, "internal_call", False) is True
            and getattr(ctx, "asker", None) is None
            and getattr(ctx, "message_source", None) is None
            and getattr(ctx, "turn_class", None) is None)


def turn_asker(ctx: Any) -> Any:
    """The turn's agent.Asker; for a context that carries only a source, the door's
    (fail closed: a group with no asker and no source at all are foreign)."""
    from .agent import Asker, asker_for_source
    asker = Asker.from_record(getattr(ctx, "asker", None))
    if asker is not None:
        return asker
    return asker_for_source(getattr(ctx, "message_source", None))


def asker_is_owner(ctx: Any) -> bool:
    """Did the owner ask for this turn (ADR-053 2.2)? No context, or a context with no asker,
    no source and no class, is nobody's turn and not the owner (fail closed); only a context
    marked `internal_call` is not gated."""
    if ctx is None:
        return False
    if _is_internal_call(ctx):
        return True
    return bool(turn_asker(ctx).is_owner)


def asker_card_fields(ctx: Any) -> Optional[Dict[str, Any]]:
    """The asker's fields for a card, or None when the owner asked (nothing to name)."""
    if asker_is_owner(ctx):
        return None
    return turn_asker(ctx).card_fields()


# ---------------------------------------------------------------------------
# The call's mark: approved by the owner, or carried to the tool's own card
# ---------------------------------------------------------------------------

_CALL_MARK: contextvars.ContextVar[str] = contextvars.ContextVar("owner_approval_call_mark",
                                                                 default="")
MARK_APPROVED = "approved"
MARK_TOOL_ASKS = "tool_asks"


def owner_approved_this_call() -> bool:
    """The owner approved this very call through the gate's card."""
    return _CALL_MARK.get() == MARK_APPROVED


def tool_asks_this_call() -> bool:
    """This call goes to the tool's own card, which names the asker (D-b)."""
    return _CALL_MARK.get() == MARK_TOOL_ASKS


def may_run_owner_tool(ctx: Any) -> bool:
    """The second owner check of a former owner_only tool (ADR-053 Consequences): the owner
    asked, or the owner approved this call, or this call is on its way to the tool's own
    card. Per call: the mark lives only while the gate holds it."""
    return asker_is_owner(ctx) or _CALL_MARK.get() in (MARK_APPROVED, MARK_TOOL_ASKS)


# ---------------------------------------------------------------------------
# The call's deadline: an approval that comes after the loop gave up starts nothing
# ---------------------------------------------------------------------------

# The loop's `asyncio.wait_for` cancels the await, never the worker thread (loop.py,
# shutdown_shared_executor): a call the model was already told timed out keeps waiting for
# its card, and a late "yes" would run it with nobody reading the result (Zcode's review
# of ADR-053, D-c). The loop sets this deadline in the worker; every approval wait reads it
# after the answer and voids a late one.
_CALL_DEADLINE: contextvars.ContextVar[Optional[float]] = contextvars.ContextVar(
    "owner_approval_call_deadline", default=None)

LATE_APPROVAL_REASON = "the call's time ran out before the owner answered"


def run_with_call_deadline(deadline: Optional[float], fn, *args, **kwargs):
    """Run `fn` with this call's monotonic deadline set, on the calling (worker) thread."""
    token = _CALL_DEADLINE.set(deadline)
    try:
        return fn(*args, **kwargs)
    finally:
        try:
            _CALL_DEADLINE.reset(token)
        except ValueError:
            _CALL_DEADLINE.set(None)


def call_deadline_passed() -> bool:
    """Has the loop already given up on this call? False outside a loop."""
    import time
    deadline = _CALL_DEADLINE.get()
    return deadline is not None and time.monotonic() >= deadline


# ---------------------------------------------------------------------------
# Pending cards: one per chat, two per process
# ---------------------------------------------------------------------------

_slots_lock = threading.Lock()
_pending_by_chat: Dict[str, int] = {}


def _chat_key(ctx: Any) -> str:
    return getattr(ctx, "conversation_id", None) or getattr(ctx, "current_task_id", None) or "-"


def pending_cards() -> int:
    with _slots_lock:
        return sum(_pending_by_chat.values())


def _reserve(ctx: Any, tool: str) -> Optional[str]:
    """Take a pending-card slot for this chat, or say why not (2.8, D-i)."""
    chat = _chat_key(ctx)
    with _slots_lock:
        if _pending_by_chat.get(chat, 0) >= MAX_PENDING_PER_CHAT:
            return (f"⚠️ {tool} was not run: another approval is already waiting for the owner "
                    f"in this chat. Only one waits per chat; say what you wanted to do and "
                    f"ask again after the owner has answered.")
        if sum(_pending_by_chat.values()) >= MAX_PENDING_PER_PROCESS:
            return (f"⚠️ {tool} was not run: another approval is already waiting for the owner "
                    f"(at most {MAX_PENDING_PER_PROCESS} wait at once across all chats). Say "
                    f"what you wanted to do; nothing was asked.")
        _pending_by_chat[chat] = _pending_by_chat.get(chat, 0) + 1
    return None


def _release(ctx: Any) -> None:
    chat = _chat_key(ctx)
    with _slots_lock:
        left = _pending_by_chat.get(chat, 0) - 1
        if left > 0:
            _pending_by_chat[chat] = left
        else:
            _pending_by_chat.pop(chat, None)


def _reset_for_tests() -> None:
    with _slots_lock:
        _pending_by_chat.clear()


# ---------------------------------------------------------------------------
# The card
# ---------------------------------------------------------------------------

def default_details(args: Dict[str, Any]) -> str:
    """What exactly, for a tool with no approval_check: its arguments as given."""
    if not args:
        return "(no arguments)"
    try:
        text = json.dumps(args, ensure_ascii=False, default=str, sort_keys=True)
    except Exception:
        text = str(args)
    return clip(text)


def clip(text: str, limit: int = DETAILS_LIMIT) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _owner_name(ctx: Any) -> str:
    p2p = getattr(getattr(ctx, "dpc_service", None), "p2p_manager", None)
    try:
        return (p2p.get_display_name() if p2p is not None else "") or ""
    except Exception:
        return ""


def refusal_for(tool: str, reason: str, ctx: Any) -> str:
    """The tool result for a card that was not approved, in words the agent can repeat:
    nobody answered means the owner is not in focus (2.4), not that he refused. Every one
    starts with the warning mark, which is what the loop reads as a refusal."""
    owner = _owner_name(ctx)
    who = f"the owner ({owner})" if owner else "the owner"
    asker = turn_asker(ctx).label()
    if reason.startswith("nobody answered"):
        return (f"⚠️ {tool} was not run: it needs {who}'s approval because {asker} is not the "
                f"owner, and nobody answered within {APPROVAL_TTL_SECONDS} s. {who[0].upper() + who[1:]} "
                f"is not in focus now. Say so in your answer; do not ask again at once.")
    if reason.startswith("no UI client is connected"):
        return (f"⚠️ {tool} was not run: it needs {who}'s approval because {asker} is not the "
                f"owner, and nobody could be asked: no interface client is connected and this "
                f"run did not come from Telegram. Say so in your answer.")
    if reason == LATE_APPROVAL_REASON:
        return (f"⚠️ {tool} was not run: the owner's answer came after this call had already "
                f"timed out, so it was not used.")
    if reason == "the user declined":
        return f"⚠️ {tool} was not run: {who} refused it for {asker}."
    return f"⚠️ {tool} was not run: the owner's approval for {asker} was not given ({reason})."


def await_owner_approval(ctx: Any, tool: str, details: str, cost: str = "") -> tuple:
    """Raise the gate's card and wait for the owner: (approved, reason)."""
    from .tools.core import _await_schedule_approval
    asker = turn_asker(ctx)
    about = f"{asker.label()} asks to run {tool}: {details}"
    if cost:
        about += f" — {cost}"
    return _await_schedule_approval(
        ctx, task_type=OWNER_APPROVAL_TASK_TYPE, when="now", about=clip(about, 300),
        void_on_stop=True,
        card={"tool": tool, "details": clip(details), "cost": cost},
    )


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

class Gate:
    """One call's passage through the gate: `refusal` when it may not run; `close()` after
    the handler, whatever happened."""

    __slots__ = ("refusal", "_token", "_slot_ctx")

    def __init__(self, refusal: Optional[str] = None):
        self.refusal = refusal
        self._token = None
        self._slot_ctx = None

    def close(self) -> None:
        if self._token is not None:
            try:
                _CALL_MARK.reset(self._token)
            except ValueError:
                _CALL_MARK.set("")
            self._token = None
        if self._slot_ctx is not None:
            _release(self._slot_ctx)
            self._slot_ctx = None


def open_gate(entry: Any, ctx: Any, args: Dict[str, Any]) -> Gate:
    """Decide one call of `entry` (ADR-053 Decision 2.5). Never raises: a check that fails
    asks the owner, it does not wave the call through."""
    gate = Gate()
    if not getattr(entry, "owner_approval_for_others", False) or asker_is_owner(ctx):
        return gate
    name = getattr(entry, "name", "tool")
    check = getattr(entry, "approval_check", None)
    decision: Any = None
    if check is not None:
        try:
            decision = check(ctx, args or {})
        except Exception as e:
            log.warning("approval_check of %s failed (%s); the owner is asked", name, e,
                        exc_info=True)
            decision = None
    if decision is None:
        decision = Ask(default_details(args))
    asker = turn_asker(ctx)

    if isinstance(decision, Refuse):
        log.info("Tool %s refused before any card for %s: %s", name, asker.label(), decision.text)
        gate.refusal = decision.text
        return gate
    if isinstance(decision, Exempt):
        log.info("Tool %s runs with no card for %s: %s", name, asker.label(), decision.why)
        return gate

    busy = _reserve(ctx, name)
    if busy is not None:
        log.info("Tool %s refused for %s: %s", name, asker.label(), busy)
        gate.refusal = busy
        return gate

    if isinstance(decision, ToolAsks):
        # The slot is held while the tool's own card is up: it is the call's card.
        gate._slot_ctx = ctx
        gate._token = _CALL_MARK.set(MARK_TOOL_ASKS)
        return gate

    details = decision.details or default_details(args)
    try:
        approved, reason = await_owner_approval(ctx, name, details, decision.cost)
    except Exception as e:
        log.warning("Owner approval for %s could not be asked: %s", name, e, exc_info=True)
        approved, reason = False, f"could not deliver the approval request: {e}"
    finally:
        _release(ctx)
    if not approved:
        log.info("Tool %s not approved for %s: %s", name, asker.label(), reason)
        gate.refusal = refusal_for(name, reason, ctx)
        return gate
    log.info("Tool %s approved by the owner for %s, this call only", name, asker.label())
    gate._token = _CALL_MARK.set(MARK_APPROVED)
    return gate


def approval_wait_for(entry: Any, ctx: Any) -> int:
    """Seconds the loop adds to a flagged call's budget on a foreign turn (D-c): the card's
    wait, unless the tool's own budget already holds it."""
    if entry is None or not getattr(entry, "owner_approval_for_others", False):
        return 0
    if getattr(entry, "budget_holds_approval", False) or asker_is_owner(ctx):
        return 0
    return APPROVAL_TTL_SECONDS


def telegram_chat_for_card(ctx: Any) -> str:
    """The Telegram chat an approval card may be offered in, or "".

    The chat the run came from, as today, unless someone other than the owner asked for the
    turn and the agent's bridge has no `telegram_owner_user_id`: without it a press counts
    in any private chat with the presser (`AgentTelegramBridge._is_owner_press`), so the
    stranger who asked could approve his own request (Zcode's review of ADR-053). Then the
    card is not offered on Telegram at all.
    """
    chat_id = getattr(ctx, "reply_telegram_chat_id", "") or ""
    if not chat_id or asker_is_owner(ctx):
        return chat_id
    service = getattr(ctx, "dpc_service", None)
    agent_id = getattr(getattr(ctx, "agent_root", None), "name", "") or ""
    bridge = None
    getter = getattr(service, "_get_agent_telegram_bridge", None)
    if callable(getter):
        try:
            bridge = getter(agent_id)
        except Exception:
            bridge = None
    if bridge is not None and getattr(bridge, "_owner_user_id", None) is not None:
        return chat_id
    log.info("Approval card for %s not offered on Telegram chat %s: the asker is not the owner "
             "and no telegram_owner_user_id is set, so a press there could be the asker's own",
             agent_id or "an agent", chat_id)
    return ""
