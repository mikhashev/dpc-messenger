"""
Llama Server Supervisor — one DPC-owned `llama-server` child per alias.

Route (b2) of ADR-040: DPC starts the server with per-alias flags, watches
`/health`, captures `/props` as the capability record, and stops it on drain
or shutdown. One process, keyed by alias — no queue, no priorities, no model
rotation; those are D4-β and land only after gates G3–G5.

Two standing constraints from the 2026-08-19 window are encoded here rather
than remembered:

- `terminate()` is never called on the child. On Windows it kills the process
  with its stdio buffers unflushed, which is why a week of server logs came
  out empty. The child is spawned with CREATE_NEW_PROCESS_GROUP and asked to
  stop with CTRL_BREAK_EVENT (SIGINT elsewhere), which flushes.
- `GGML_BACKEND_PATH` is set whenever a CUDA backend ships beside the binary,
  because without it the server silently runs on the CPU.
"""

import asyncio
import contextlib
import contextvars
import functools
import itertools
import json
import logging
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
import weakref
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, NoReturn, Optional, Tuple

from .. import process_registry as _procreg
from . import llama_server_fetcher as _fetcher
from .llama_server_fetcher import (
    DPC_HOME,
    LLAMA_CPP_TAG,
    find_cuda_backend,
    install_pin,
    pins_dir,
    resolve_binary,
    server_binary_name,
)
from .local_engine_pin_fetcher import (
    _is_pin,
    _same_path,
    _start_passed_at,
    installed_pins,
    note_start_passed,
    prune_pins,
    read_marker,
    refusal_text,
    retained_pin,
)

logger = logging.getLogger(__name__)

# The child needs its own process group for CTRL_BREAK_EVENT to reach it —
# and only that group, so the signal does not take the backend down with it.
_CREATION_NEW_PROCESS_GROUP = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0

DEFAULTS: Dict[str, Any] = {
    "n_ctx": 262144,
    # None means AUTO: the ladder in _launch_auto_kv steps q8_0 -> q4_0 (f16
    # is not a rung, see KV_LADDER), admitting each by the card's VRAM
    # arithmetic and then reading the child's own out-of-memory verdict from
    # its log tail. An alias that names a type gets exactly that type, one
    # attempt.
    "cache_type_k": None,
    "cache_type_v": None,
    # Measured 2026-08-19 on b10472 at 139 490 tokens: without an explicit
    # -ngl the server left one context at 0/66 layers on the GPU-less side of
    # the split and the draft contexts at 57-59/66, disabling fused Gated
    # Delta Net there; every context fully on the GPU was worth +11.3 %
    # (704.5 -> 784.4 tok/s) and the layer-0 warnings went 5 -> 0. An alias
    # can still override this for a card that cannot hold all layers.
    "n_gpu_layers": 999,
    # None, not False: the binary's own default is `auto`, and False is what the
    # form writes when its owner picks "off". Holding False here made those two
    # the same value, so "off" could not be expressed at all.
    "flash_attn": None,
    "mmproj": None,
    "spec_type": "draft-mtp",
    # 3 came from one synthetic prompt on 2026-08-19 comparing acceptance
    # RATES (0.686 against 4's 0.578) — the wrong quantity: what a turn gets is
    # acceptance x n, and 7 547 production tasks give 0.592 at n=3 against
    # 0.589 at n=4, i.e. 2.78 tokens per target pass against 3.35. The value
    # stays until its owner picks; the aliases already override it with 4.
    "spec_draft_n_max": 3,
    # None = the server's own choice (4 unified slots on b10472; unmeasured on
    # b11146, where the fleet sets 2 and the child logs n_slots = 2). An explicit
    # value is ALWAYS sent, so -np 1 is expressible — the old guard ate it and
    # the config said 1 while the server ran 4.
    "n_parallel": None,
    # Micro-batch. None = the build's own 512. Measured 2026-08-20 on this pin
    # and card (llama-bench, logs in ~/.dpc/logs/bench-2026-08-20-prefill): at a
    # 60 000-token prefill 1024 is worth +5.6 % over 512 and 2048 a further
    # +2.8 %, while at 512-8192 tokens the same flag moves nothing (-0.6 % …
    # +0.5 %). The effect needs depth, which is where this fleet lives, and it
    # costs VRAM: 512 -> 1024 measured +683 MiB on the production child. n_batch
    # is exposed beside it because the two are read together; the build's
    # default is 2048 and micro-batches are cut from it.
    "n_batch": None,
    "n_ubatch": None,
    # How many context checkpoints the child keeps per slot, and how far apart.
    # None = the build's own 32 / 8192, which is what every install had. The
    # reason to name them is size, not availability: a checkpoint is a snapshot
    # of the recurrent state and grows with depth (~150 MiB near the start to
    # ~920 MiB at ~196K tokens, qwen3.8-27B on b11146, 2026-09-24..28), so at
    # the build's 32 a parked deep conversation is mostly checkpoints, not
    # attention KV. With the host cache holding whole conversations, the
    # checkpoint count decides how many of them fit.
    # The trade is where the engine can resume from after a prefix divergence;
    # with the stable-prefix layout the divergence sits in the turn tail, which
    # is exactly what the surviving checkpoints cover.
    "ctx_checkpoints": None,
    "checkpoint_min_step": None,
    "kv_unified": True,
    # The minimum chunk the child will try to reuse from a cached prefix by
    # shifting KV instead of re-reading it. None = the build's own 0, which is
    # off: today any divergence inside the prefix throws away everything behind
    # it. Ours sit early — the knowledge index and the scratchpad are rebuilt
    # ahead of the whole history — so this is the one flag that survives them.
    # 0 is expressible and means off, which is why the guard tests None.
    "cache_reuse": None,
    "cache_ram_mib": None,
    # The most tokens one image may take in the vision encoder; None = the flag
    # is not sent and the child reads the limit from the model file. On b11146
    # with the Qwen3.8 projector that default is 4082 tokens, one token being a
    # 32x32 px cell (patch 16 x merge 2), so anything above ~4.2 MP reaches the
    # model downsized (measured 2026-09-30, board card
    # AN-IMAGE-REACHES-THE-VISION-MODEL-THROUGH-FOUR-DOORS). Named after the
    # flag, `--image-max-tokens`. The floor, `--image-min-tokens`, is not a key
    # here: it has its own card and nothing measured it yet.
    "image_max_tokens": None,
    # What a loaded context costs beyond weights and attention-KV, in MiB. None
    # uses the figure below, which was measured on one model: every term in it
    # is model-shaped, so an alias serving a different model owns its own.
    "vram_overhead_mib": None,
    "slot_save_path": None,
    "jinja": True,
    "start_timeout_s": 300.0,
    "extra_args": [],
}


# A supervisor's child, watcher, start lock and shielded stops are loop-bound, and
# agent tools run on a per-call loop that closes when the call returns
# (`ToolRegistry.execute`). The service binds its own loop here at start; a call
# arriving on another running loop is run there instead, as
# `LocalApiServer._send_on_owner_loop` reroutes a send. Unbound or not running:
# the caller's loop, as before.
_OWNER_LOOP: Optional[asyncio.AbstractEventLoop] = None


def bind_owner_loop(loop: Optional[asyncio.AbstractEventLoop]) -> None:
    """Make `loop` the one every supervisor runs its lifecycle on; None unbinds."""
    global _OWNER_LOOP
    _OWNER_LOOP = loop


# The consent service a missing pin is asked for through (`ModelDownloadService`).
# Unbound (tests, scripts): the owner's start installs the pin itself, in a thread.
_MODEL_DOWNLOAD_SERVICE: Optional[Any] = None


def bind_model_download_service(service: Optional[Any]) -> None:
    """Make `service` the one a missing pin is offered through; None unbinds."""
    global _MODEL_DOWNLOAD_SERVICE
    _MODEL_DOWNLOAD_SERVICE = service


# The settings the idle swap reads (`[local_engines] swap_when_idle`, read at each look, and
# `swap_wait_s`, read once at a request's acceptance). Unbound (tests, scripts that run no
# service): no swap, today's refusal. Bound by the service at start, as the consent service is.
_LOCAL_ENGINE_SETTINGS: Optional[Any] = None


def bind_local_engine_settings(settings: Optional[Any]) -> None:
    """Make `settings` the one the idle swap is read from; None unbinds."""
    global _LOCAL_ENGINE_SETTINGS
    _LOCAL_ENGINE_SETTINGS = settings


def swap_policy() -> Tuple[bool, int]:
    """(swap_when_idle, swap_wait_s) as the settings hold them now; (False, 0) unbound
    or unreadable. A change needs no restart: it reaches the next request."""
    settings = _LOCAL_ENGINE_SETTINGS
    if settings is None:
        return False, 0
    try:
        return bool(settings.get_swap_when_idle()), int(settings.get_swap_wait_s())
    except Exception:
        logger.warning("the idle-swap settings are unreadable; no swap", exc_info=True)
        return False, 0


def _caller_idle_s() -> int:
    """`caller_idle_s` as the settings hold it now; 0, no window, unbound or unreadable.
    Read at each use, so a change needs no restart."""
    settings = _LOCAL_ENGINE_SETTINGS
    if settings is None:
        return 0
    try:
        return int(settings.get_caller_idle_s())
    except Exception:
        logger.warning("the caller window setting is unreadable; no window", exc_info=True)
        return 0


# ADR-044 point 9: set while a call is served to a peer, holding its node id.
# A ContextVar because the mark has to reach `ensure_running` and `call_slot`
# through LLMManager and the provider without a parameter on every signature;
# it follows the awaits of the task that set it and nothing else. Read by both
# local engines; `strata_supervisor` re-exports it.
_PEER_CALL: contextvars.ContextVar = contextvars.ContextVar("dpc_strata_peer_call", default=None)


@contextlib.contextmanager
def serving_a_peer(peer_id: str):
    """Mark the calls made inside as served to `peer_id`."""
    token = _PEER_CALL.set(peer_id or "peer")
    try:
        yield
    finally:
        _PEER_CALL.reset(token)


def peer_being_served() -> Optional[str]:
    """The peer the current call is served to, or None for the owner's own call."""
    return _PEER_CALL.get()


# ADR-044, the card queue, rule 10: set while a background call is made, holding its name. A
# ContextVar for the reason `_PEER_CALL` is one. It is asked at `ensure_running`, and only while
# `swap_when_idle` is on: such a call neither swaps nor queues, it runs on a child already on the
# card, and otherwise it is skipped and `last_background_skip` says so. Nothing tries a skip again:
# each name carries what follows it instead.
_BACKGROUND_CALLS = {
    "self_reflection_confirmation": "The extraction goes on and treats this conversation as general.",
    "skill_reflection": "This reflection ends, and the next long task that uses a skill reflects.",
}
_BACKGROUND_CALL: contextvars.ContextVar = contextvars.ContextVar("dpc_background_call", default=None)


@contextlib.contextmanager
def a_background_call(what: str):
    """Mark the calls made inside as the background call `what`. A skip raises
    `BackgroundCallSkipped` out of the body, which the caller handles as any failed call."""
    if what not in _BACKGROUND_CALLS:
        raise ValueError(f"unknown background call: {what!r}")
    token = _BACKGROUND_CALL.set(what)
    try:
        yield
    finally:
        _BACKGROUND_CALL.reset(token)


def _foreign_owner_loop() -> Optional[asyncio.AbstractEventLoop]:
    """The owner loop when the caller runs on another one and the owner is alive."""
    owner = _OWNER_LOOP
    if owner is None or owner.is_closed() or not owner.is_running():
        return None
    try:
        here = asyncio.get_running_loop()
    except RuntimeError:
        return None
    return None if here is owner else owner


def on_the_owner_loop(fn):
    """Run the coroutine function `fn` on the owner loop when called from another.

    The caller awaits the result there. Cancelling the caller cancels the
    owner-side task (`wrap_future` cancels the concurrent future, which cancels
    the task through `call_soon_threadsafe`), and that task runs its own cleanup
    on a loop that lives; the caller does not wait for it. Contextvars travel:
    `run_coroutine_threadsafe` schedules from the caller's context, so a peer
    mark (`serving_a_peer`) reaches the owner-side call."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        owner = _foreign_owner_loop()
        if owner is None:
            return await fn(*args, **kwargs)
        future = asyncio.run_coroutine_threadsafe(fn(*args, **kwargs), owner)
        return await asyncio.wrap_future(future)

    wrapper.runs_on_the_owner_loop = True
    return wrapper


class LlamaServerError(RuntimeError):
    """A supervisor failure, carrying the child's last log lines when it has any."""

    def __init__(self, message: str, log_lines: Optional[List[str]] = None):
        tail = "".join(f"\n  | {line}" for line in (log_lines or [])[-12:])
        super().__init__(f"{message}{tail}")
        self.log_lines = log_lines or []


class CardHeldError(LlamaServerError):
    """Another local engine holds the card (ADR-044 point 4); never retried."""

    def __init__(self, message: str, holder_alias: str, holder_engine: str):
        super().__init__(message)
        self.holder_alias = holder_alias
        self.holder_engine = holder_engine


class CardQueueRefusal(CardHeldError):
    """The card queue let this request go without the card (ADR-044, amendment 2026-11-02, the
    card queue, rules 5, 7 and 12): its deadline passed, a peer met a waiting place, or its place
    was taken away. `provider_unavailable` at both doors; never retried and never tried again by
    another path, which would only stand in the same line."""


class TakenOutOfTheCardQueue(CardQueueRefusal):
    """A person's Stop took this request out of the line (rule 12): a stopped turn, not a
    failed one."""


class BackgroundCallSkipped(CardQueueRefusal):
    """A background call found its alias not on the card, or a place waiting (rule 10). Nothing
    was started, marked or queued, and it is in `last_background_skip`. A `CardQueueRefusal` for
    its "never retried": it never stood in the queue, and nothing tries it again."""


class EngineDrainingError(LlamaServerError):
    """A new call or start met a supervisor that is draining. Its own class so that the peer door
    reads it as `provider_unavailable` (rule 15) and not as a failure mid-call."""


class PinNotInstalledError(LlamaServerError):
    """The engine's pin is not on disk and this start did not fetch it: the owner
    is asked through the model-download consent flow, a peer is not. Never retried."""

    def __init__(self, message: str, download_name: str):
        super().__init__(message)
        self.download_name = download_name


# --- the retained pin (B2 step 3) --------------------------------------------
# `pin` is the constant a build names (LLAMA_CPP_TAG, STRATA_RELEASE_TAG); the
# running pin is the folder a child was actually started from (`_pin_root`), which
# is the retained pin while the new one is not downloaded, or after it failed its
# first start. The rollback is DPC's own rule, not Strata's `--rollback-engine`.


def _build_fault(error: BaseException) -> BaseException:
    """Mark `error` as the build's own failure at start (the child died or never
    answered /health or /props, the version check refused, the smoke failed): only
    such a failure of a new pin's first start rolls back to the retained pin."""
    error._build_fault = True
    return error


# The pin a first start rolled back from -> {"engine", "from", "to", "reason", "at",
# "to_root"}, keyed by `_same_path` of the failed root. In memory: later starts run
# `to_root` until a backend restart, or Strata's Reset (`reset_failure`).
_ROLLBACKS: Dict[str, Dict[str, Any]] = {}
# Roots whose passed start this process has already recorded (and pruned after).
_PASSES_NOTED: set = set()
# Every supervisor made, for the roots that must survive a prune.
_SUPERVISORS: "weakref.WeakSet[LlamaServerSupervisor]" = weakref.WeakSet()


def _roots_in_use() -> List[Path]:
    """Pin roots a live child runs from, and the folder of every configured binary."""
    roots: List[Path] = []
    for sup in list(_SUPERVISORS):
        proc = sup._proc
        if sup._pin_root is not None and proc is not None and proc.returncode is None:
            roots.append(sup._pin_root)
        configured = sup.config.get("binary_path")
        if configured:
            roots.append(Path(str(configured)).parent)
    return roots


async def _toast(title: str, message: str) -> None:
    """The existing `error_toast` event, through the local API the consent service
    holds (bound by the service); nothing when none is bound."""
    api = getattr(_MODEL_DOWNLOAD_SERVICE, "local_api", None)
    if api is None:
        return
    try:
        await api.broadcast_event("error_toast", {"title": title, "message": message, "duration": 10000})
    except Exception:
        logger.debug("broadcast_event(error_toast) failed", exc_info=True)


def _pins_of(fetcher: Any, tag: str, engine: str) -> Dict[str, Any]:
    """One engine's block of `local_engine_status` `pins`. Reads the pin markers: run it in a thread.
    `rolled_back_to` is the latest rollback on this engine and `running_pin` one live
    child's folder, whichever alias: with two aliases on one engine they name one of them."""
    folder = fetcher.pins_dir()
    retained = retained_pin(folder, tag)
    running = next((
        sup._pin_root.parent.name for sup in list(_SUPERVISORS)
        if sup.ENGINE == engine and sup._pin_root is not None
        and sup._proc is not None and sup._proc.returncode is None
    ), None)
    rolled = [r for r in _ROLLBACKS.values() if r["engine"] == engine]
    last = max(rolled, key=lambda r: r["at"]) if rolled else None
    installed = []
    for p in installed_pins(folder):
        when = _start_passed_at(p["marker"])
        installed.append({
            "tag": p["tag"], "platform": p["platform"],
            "start_passed_at": when.isoformat() if when else None,
        })
    return {
        "pin": tag,
        "retained_pin": retained["tag"] if retained else None,
        "running_pin": running,
        "rolled_back_to": {k: last[k] for k in ("from", "to", "reason", "at")} if last else None,
        "installed_pins": installed,
    }


def _pins_status() -> Dict[str, Any]:
    """`local_engine_status` `pins`: per engine, its installed pins and which runs."""
    from . import strata_fetcher

    return {
        _fetcher.DOWNLOAD_NAME: _pins_of(_fetcher, LLAMA_CPP_TAG, "llama-server"),
        strata_fetcher.DOWNLOAD_NAME: _pins_of(strata_fetcher, strata_fetcher.STRATA_RELEASE_TAG, "strata"),
    }


# The idle swap (ADR-044, amendment 2026-11-02). The last one this process made, in
# `local_engine_status` as `last_swap`: {"from", "to", "from_engine", "to_engine",
# "at", "parked_dropped"}. In memory, as `_ROLLBACKS` is.
_LAST_SWAP: Optional[Dict[str, Any]] = None
# How often a swap looks at a busy holder's in-flight count, and how many holders one
# start swaps out before it gives up and takes the refusal (a holder that comes back
# each time the card is let go is a ping-pong, not a swap).
_SWAP_POLL_S = 0.1

# Whether a supervisor is still the one its alias is configured with: a settings save
# can retire it while a stop drains, and a retired supervisor must not be reopened,
# or a late provider could start a child nobody tracks. The provider module, which
# owns the registry, sets this when it loads; without it (tests that build bare
# supervisors) every supervisor counts as configured.
_still_configured: Callable[["LlamaServerSupervisor"], bool] = lambda _supervisor: True
_MAX_SWAP_ROUNDS = 4
_swap_clock = time.monotonic
# The toasts of swaps, held until sent: a task nobody references can be collected.
_SWAP_TOASTS: "set[asyncio.Future]" = set()


def last_swap() -> Optional[Dict[str, Any]]:
    """The last idle swap this process made, or None."""
    return dict(_LAST_SWAP) if _LAST_SWAP else None


# The last skip of each background call (ADR-044, the card queue, rule 10), in
# `local_engine_status` as `last_background_skip`: name, then {"alias", "reason", "what_follows",
# "at"}. One row per name, so one that skips often does not hide another. In memory, as
# `_LAST_SWAP` is.
_LAST_BACKGROUND_SKIP: Dict[str, Dict[str, Any]] = {}


def last_background_skip() -> Dict[str, Dict[str, Any]]:
    """The last skip of each background call this process made; empty before one."""
    return {what: dict(row) for what, row in _LAST_BACKGROUND_SKIP.items()}


# The stops of holders being swapped out, held until done. Each runs as its own task so that a
# request's deadline or cancel leaves it running to its end (ADR-044, the card queue, decisions
# 2 and 3); a task nobody references can be collected.
_SWAP_STOPS: "set[asyncio.Future]" = set()
# How long a request with no time left waits for what stands between it and the claim once
# nothing is busy: `_stop_child` asks a child for 20 s and then kills it. A request that came with
# no time left is one of `swap_wait_s` 0, or an old one that lost its alias between two rounds.
_STOP_WITH_NO_WAIT_LEFT_S = 60.0


# Supervisors holding the card, from the decision to start a child until its stop
# has finished: "claimed" or "stopping". Keyed by supervisor, not alias, because a
# retired child still draining has left `_ACTIVE_SUPERVISORS` and holds the card.
# One card: no local child here is given a GPU index, so every child on a GPU is
# on it; one started with `--device none` is not. Check and claim run with no
# await between them, so two concurrent starts cannot both pass.
_CARD_CLAIMS: Dict["LlamaServerSupervisor", str] = {}


def _live_claims() -> List[Tuple["LlamaServerSupervisor", str]]:
    """(supervisor, state) for each claim; a child that exited on its own holds
    nothing any more and is dropped here."""
    out: List[Tuple["LlamaServerSupervisor", str]] = []
    for sup, mark in list(_CARD_CLAIMS.items()):
        proc = sup._proc
        if proc is not None and proc.returncode is not None:
            _CARD_CLAIMS.pop(sup, None)
            continue
        if mark == "stopping":
            state = "stopping"
        elif sup._draining:
            state = "draining"
        elif proc is not None and sup.props:
            state = "running"
        else:
            state = "starting"
        out.append((sup, state))
    return out


def card_holders() -> List[Dict[str, Any]]:
    """Each supervisor holding the card: alias, engine, state, port."""
    return [
        {"alias": sup.alias, "engine": sup.ENGINE, "state": state, "port": sup.port}
        for sup, state in _live_claims()
    ]


# What stands on an alias, so that its holder is busy between its calls too (ADR-044,
# amendment 2026-11-02, the card queue, rules 1 and 14). Keyed by alias, not supervisor as
# `_CARD_CLAIMS` is: that one records which child is on the card, this one who still needs
# the alias, and a settings save replacing the supervisor changes the first and not the
# second. A hold makes a claim busy; it creates no claim, and a dead child holds nothing.
# Written only on the owner loop (`_now_on_the_owner_loop`).
_CARD_HOLD_KINDS = ("accepted_request",)
_CARD_HOLDS: Dict[str, List["_CardHold"]] = {}
# Which came first, a request or a place (rule 4); across aliases, so one counter.
_CARD_ARRIVAL = itertools.count(1)


class _CardHold:
    """One stand on one alias at a time: an `accepted_request`. `alias` None stands on
    nothing (a peer route or a vendor alias, for now). `live` goes false when the hold ends,
    so a tool thread or a task that outlives it cannot put this hold back on an alias.

    Acceptance fixes the request's one deadline: `swap_wait_s` is read here, once (rule 5).
    `admitted` says it is not new to any place: it has been through the line, or it is a call
    of a caller's open window."""

    __slots__ = ("kind", "agent_id", "conversation_id", "alias", "live", "arrival", "accepted_at", "wait_s",
                 "admitted")

    def __init__(self, kind: str, agent_id: Optional[str], conversation_id: Optional[str]):
        self.kind = kind
        self.agent_id = agent_id
        self.conversation_id = conversation_id
        self.alias: Optional[str] = None
        self.live = True
        self.arrival = next(_CARD_ARRIVAL)
        self.accepted_at = _swap_clock()
        self.wait_s = swap_policy()[1]
        self.admitted = False


# The hold of the request this code runs for. It follows the awaits of the task that set
# it, and a mark from a tool thread reaches `ensure_running` the way the peer mark does.
# A `_DaemonToolPool` thread inherits nothing, so it is carried there (`carrying_card_hold`).
_CARD_HOLD: contextvars.ContextVar = contextvars.ContextVar("dpc_card_hold", default=None)


def _now_on_the_owner_loop(fn: Callable[..., None], *args: Any) -> None:
    """Run the synchronous `fn` on the owner loop: here when this is that loop or none is
    bound, else handed over in call order, ahead of whatever this caller sends there next.
    It awaits nothing, so a Kill or a cancel cannot stop a request from letting go."""
    owner = _foreign_owner_loop()
    if owner is None:
        fn(*args)
        return
    try:
        owner.call_soon_threadsafe(fn, *args)
    except RuntimeError:  # the loop closed between the check and the call
        fn(*args)


def _hold_moves_to(hold: _CardHold, alias: Optional[str]) -> None:
    """Let go of the old alias and take the new one, in one synchronous stretch."""
    if not hold.live or hold.alias == alias:
        return
    holds = _CARD_HOLDS.get(hold.alias, [])
    if hold in holds:
        holds.remove(hold)
        if not holds:
            del _CARD_HOLDS[hold.alias]
    hold.alias = alias
    if alias is not None:
        _CARD_HOLDS.setdefault(alias, []).append(hold)


def _hold_ends(hold: _CardHold) -> None:
    _hold_moves_to(hold, None)
    hold.live = False


def _hold_enters(hold: _CardHold, alias: Optional[str], caller: Optional[str]) -> None:
    """The request stands on `alias`. A call of `caller`'s open window there is not new."""
    _hold_moves_to(hold, alias)
    if alias is not None and caller is not None and caller in _callers_in_their_window(alias):
        hold.admitted = True


@contextlib.asynccontextmanager
async def card_held_for(alias: Optional[str], kind: str, *, agent_id: Optional[str] = None,
                        conversation_id: Optional[str] = None, caller: Optional[str] = None):
    """Stand on `alias` for the body: its holder is busy until the body ends, however it
    ends. `agent_id` and `conversation_id`, where there are any, are what the busy refusal
    names. `caller` is a gateway client's name, whose open window makes this call not new."""
    if kind not in _CARD_HOLD_KINDS:
        raise ValueError(f"unknown kind of card hold: {kind!r}")
    hold = _CardHold(kind, agent_id, conversation_id)
    _now_on_the_owner_loop(_hold_enters, hold, alias, caller)
    token = _CARD_HOLD.set(hold)
    try:
        yield hold
    finally:
        # Letting go first: `reset` raises when a dying task is closed from another context.
        _now_on_the_owner_loop(_hold_ends, hold)
        _CARD_HOLD.reset(token)


def card_hold_follows(alias: Optional[str]) -> None:
    """The request this code runs for now runs on `alias`, or on no local alias: a switch
    between rounds moves its hold. Nothing without a hold."""
    hold = _CARD_HOLD.get()
    if hold is not None:
        _now_on_the_owner_loop(_hold_moves_to, hold, alias)


def current_card_hold() -> Optional[_CardHold]:
    """The hold to hand to a thread that does not inherit this context."""
    return _CARD_HOLD.get()


@contextlib.contextmanager
def carrying_card_hold(hold: Optional[_CardHold]):
    """Make `hold` this thread's for the body; the previous value comes back, so a pool
    worker carries nothing from one call to the next."""
    token = _CARD_HOLD.set(hold)
    try:
        yield
    finally:
        _CARD_HOLD.reset(token)


def _this_request_holds(alias: str) -> bool:
    """Whether the request this code runs for stands on `alias`: it would wait on itself.
    A hold that has ended stands on nothing."""
    hold = _CARD_HOLD.get()
    return hold is not None and hold.alias == alias


# When each caller last had a call served on an alias: alias, then caller, then `_swap_clock`
# time (ADR-044, amendment 2026-11-02, the card queue, rule 6). A peer's and a gateway client's
# run of calls has no end DPC can see, so its `caller_window` is a test of this clock and not a
# `_CardHold`: a hold is entered and left around a body, a window has no body to leave, and a
# timer to end one could only outlive what it stood for. By alias, as `_CARD_HOLDS` is, and on
# the same footing: it makes a claim busy, creates none, and a dead child holds nothing, because
# only a live claim is asked. Written only on the owner loop. Each note and each look drop every
# ended window, so it holds one entry per caller with a call inside the window, never more.
_last_caller_call_at: Dict[str, Dict[str, float]] = {}


def _drop_ended_windows() -> None:
    window, now = _caller_idle_s(), _swap_clock()
    for alias, callers in list(_last_caller_call_at.items()):
        for caller, at in list(callers.items()):
            if now - at >= window:
                del callers[caller]
        if not callers:
            del _last_caller_call_at[alias]


def _caller_call_noted(alias: str, caller: str, at: float) -> None:
    _last_caller_call_at.setdefault(alias, {})[caller] = at
    _drop_ended_windows()


def note_caller_call(alias: str, caller: str) -> None:
    """`caller` has just had a call served on the local alias `alias`: it counts as busy there
    for `caller_idle_s` seconds after this, and a later call inside them renews it."""
    _now_on_the_owner_loop(_caller_call_noted, alias, caller, _swap_clock())


def _callers_in_their_window(alias: str) -> List[str]:
    """Who counts as busy on `alias` for its last call. Asked on the owner loop."""
    _drop_ended_windows()
    return list(_last_caller_call_at.get(alias, ()))


def _wait_reason_of(hold: _CardHold) -> Optional[str]:
    """The `wait_reason` of the turn this hold lies inside, or None (rule 1). A hold of an owner's
    chat or of a gateway client has no agent, and so no turn to ask."""
    if not hold.agent_id:
        return None
    from ..dpc_agent.run_gate import agent_turn_snapshot

    turn = agent_turn_snapshot(hold.agent_id)["turn"]
    if turn is None or turn.get("conversation_id") != hold.conversation_id:
        return None
    return turn.get("wait_reason")


def _who_stands_on(alias: str) -> str:
    """Who holds `alias`, for a refusal to the owner's own request: agent and conversation, where
    set, what that agent's turn is waiting for, and the callers in their window; "" for no one
    named. Never for a peer: these are ids of the owner's agents and chats. No refusal a peer
    meets calls this, because a peer never waits in the card queue (rule 7)."""
    names = []
    for hold in _CARD_HOLDS.get(alias, []):
        if not _stands_before_the_line(hold):
            continue
        parts = [f"agent '{hold.agent_id}'"] if hold.agent_id else []
        if hold.conversation_id:
            parts.append(f"conversation '{hold.conversation_id}'")
        reason = _wait_reason_of(hold)
        if reason:
            parts.append(f"wait_reason '{reason}'")
        if parts:
            names.append(", ".join(parts))
    names.extend(f"caller '{caller}'" for caller in _callers_in_their_window(alias))
    return f" In use by {'; '.join(names)}." if names else ""


def runs_on_a_local_engine(provider: Any) -> bool:
    """Whether this provider's alias is a local engine's, so that a hold on it means
    something: a peer route or a vendor has no supervisor and no card to hold."""
    return isinstance(getattr(provider, "supervisor", None), LlamaServerSupervisor)


# --- the card queue (ADR-044, amendment 2026-11-02, rules 3, 4, 5, 7, 12 and 13) -----------------
#
# The line of requests waiting for the card, nothing being forced. One place per alias, oldest
# first; by alias and not by supervisor, so that a settings save replacing one leaves the line as
# it stood (rule 14). A place is taken before the alias's `_start_lock`, because a later request
# on the holder's own alias holding that lock would keep the first place's stop of it waiting.
# Only the first place acts, through its oldest request; the place passes to the next oldest when
# that one leaves, and is gone only when empty. Lives only while `swap_when_idle` is true. Written
# only on the owner loop, where `ensure_running` runs.


class _CardPlace:
    """One alias's place in the line. `arrival` says what came before it, and does not change
    when the request it was taken for leaves."""

    __slots__ = ("alias", "arrival", "requests")

    def __init__(self, alias: str):
        self.alias = alias
        self.arrival = next(_CARD_ARRIVAL)
        self.requests: List["_QueuedRequest"] = []


_CARD_QUEUE: List[_CardPlace] = []


def card_queue() -> List[Dict[str, Any]]:
    """The line for `local_engine_status`, each request in position order (the position is the
    order; there is no position field): its alias, who it runs for where set, `queued_at` and
    `give_up_at`, both wall-clock seconds as `last_swap`'s `at` is. For the owner only: these are
    ids of the owner's agents and chats, as in `_who_stands_on`."""
    now, clock = time.time(), _swap_clock()
    return [
        {
            "alias": place.alias,
            "agent_id": request.hold.agent_id if request.hold is not None else None,
            "conversation_id": request.hold.conversation_id if request.hold is not None else None,
            "queued_at": request.queued_at,
            "give_up_at": now + request.give_up_at - clock,
        }
        for place in _CARD_QUEUE
        for request in place.requests
    ]


class _QueuedRequest:
    """A request in a place: its one deadline (rule 5), and `out`, the refusal it meets when it
    is taken out of the line (rule 12). A request with a hold on this alias has the deadline it
    was accepted with; any other (a call with no hold, a cloud agent's call on a local alias) is
    accepted here. `came_late` is a request that had no time left when it came: it refuses a busy
    holder at once, and a holder that is not busy asks it for no wait. `queued_at` is the time it
    took its place, read only by `card_queue`; the clock of the wait is `accepted_at`."""

    __slots__ = ("place", "hold", "accepted_at", "queued_at", "wait_s", "give_up_at", "came_late",
                 "stop_bound", "out", "_turn_mark")

    def __init__(self, place: _CardPlace):
        self.place = place
        self.hold: Optional[_CardHold] = _CARD_HOLD.get()
        self.queued_at = time.time()
        now = _swap_clock()
        if self.hold is not None and self.hold.alias == place.alias:
            self.accepted_at, self.wait_s = self.hold.accepted_at, self.hold.wait_s
        else:
            self.accepted_at, self.wait_s = now, swap_policy()[1]
        self.give_up_at = self.accepted_at + self.wait_s
        self.came_late = self.give_up_at <= now
        self.stop_bound = now + _STOP_WITH_NO_WAIT_LEFT_S if self.came_late else self.give_up_at
        self.out: Optional[CardQueueRefusal] = None
        self._turn_mark: Any = None

    def is_first(self) -> bool:
        return bool(_CARD_QUEUE) and _CARD_QUEUE[0] is self.place and self.place.requests[0] is self

    def in_line(self) -> bool:
        return self in self.place.requests

    def check_still_in_line(self) -> None:
        if self.out is not None:
            raise self.out

    def waited(self) -> int:
        return int(round(_swap_clock() - self.accepted_at))

    def stop_limit(self) -> str:
        """What a refusal names when `stop_bound` of a `came_late` request ran out: it is not the
        setting, which that request had already used up."""
        return (f"the limit of {_STOP_WITH_NO_WAIT_LEFT_S:.0f} s on waiting for a stop (a request with no time "
                f"left has it in place of the Wait for a busy holder setting, {self.wait_s} s)")

    def says_it_waits(self) -> None:
        """The turn this request runs in reads `card_queue`, from the first real wait on: a
        start that passes straight through never shows it."""
        if self._turn_mark is None:
            from ..dpc_agent.run_gate import turn_waits_for

            self._turn_mark = turn_waits_for("card_queue")
            self._turn_mark.__enter__()

    def leaves(self) -> None:
        """Out of the line, however it ended, in one synchronous stretch."""
        if self.in_line():
            self.place.requests.remove(self)
            if not self.place.requests and self.place in _CARD_QUEUE:
                _CARD_QUEUE.remove(self.place)
        mark, self._turn_mark = self._turn_mark, None
        if mark is not None:
            mark.__exit__(None, None, None)

    def passes(self) -> None:
        """Through the line: from here on it stands on its alias for every place behind."""
        if self.hold is not None and self.hold.alias == self.place.alias:
            self.hold.admitted = True
        self.leaves()


def _stands_before_the_line(hold: _CardHold) -> bool:
    """Whether a hold counts for the place that acts: it was there first, or it has been let
    through. A waiting start waits for what stood there when it queued, no more (rule 4)."""
    return hold.admitted or not _CARD_QUEUE or hold.arrival < _CARD_QUEUE[0].arrival


def _chat_leaves_the_line(conversation_id: str) -> None:
    taken = 0
    for place in list(_CARD_QUEUE):
        for request in list(place.requests):
            hold = request.hold
            if hold is None or not conversation_id or hold.conversation_id != conversation_id:
                continue
            request.out = TakenOutOfTheCardQueue(
                f"'{place.alias}': a person's Stop took this request out of the queue for the card",
                place.alias, "",
            )
            request.leaves()
            taken += 1
    if taken:
        logger.info("Stop took %d request(s) out of the queue for the card in %s", taken, conversation_id)


def leave_card_queue(conversation_id: str) -> None:
    """A person's Stop in this chat: its requests leave the line, and each meets
    `TakenOutOfTheCardQueue`. Beside `_CARD_QUEUE` and not in `run_gate.stop_waiting`, which only
    takes a chat out of the agent's own line (rule 12). `_interrupt_loop` is synchronous, and the
    Telegram `/stop` that reaches it runs on the loop its bridge was started on, so the line is
    changed on the owner loop, which is also why this answers nothing."""
    _now_on_the_owner_loop(_chat_leaves_the_line, conversation_id)


def take_card_queue_place(alias: str) -> None:
    """Stop engine on `alias`: a person has said it should not run, so its place goes, and its
    requests are refused (rule 12). Only the command does this. A swap stopping a holder that has
    a place of its own, further back in the line, leaves that place where it is."""
    for place in list(_CARD_QUEUE):
        if place.alias != alias:
            continue
        for request in list(place.requests):
            request.out = CardQueueRefusal(
                f"'{alias}' was stopped by hand (Stop engine), and that took its place in the "
                "queue for the card",
                alias, "",
            )
            request.leaves()


def _first_cause_of(error: Optional[BaseException], kinds: Any) -> Optional[Any]:
    """The first of `kinds` behind `error`, or None: read through the provider's
    `RuntimeError(...) from e` wrap and the router's above it, as the peer door reads."""
    seen: set = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, kinds):
            return error
        error = error.__cause__
    return None


def card_queue_refusal_in(error: Optional[BaseException]) -> Optional[CardQueueRefusal]:
    """The queue's refusal behind `error`, or None."""
    return _first_cause_of(error, CardQueueRefusal)


def engine_refusal_in(error: Optional[BaseException]) -> Optional[LlamaServerError]:
    """A local engine's refusal behind `error`, or None: the queue's, the `CardHeldError` of the
    swap off, or `EngineDrainingError`. No model was asked, so its own text says what happened,
    and the wrap's words about a provider that failed are not true of it."""
    return _first_cause_of(error, (CardHeldError, EngineDrainingError))


async def stop_by_hand(supervisor: "LlamaServerSupervisor", timeout: Optional[float] = None,
                       reopen: bool = True) -> None:
    """The Stop engine command's path (`stop_local_engine`) and the idle swap's.

    Behind the supervisor's start lock, so a start in progress finishes first rather
    than spawning after the stop; then the existing drain-then-stop. `timeout=None`
    lets a running generation finish. `reopen` makes the alias accept calls again:
    it stays configured, and its next call starts the child again. The registry is
    asked after the drain, not before it: a save that retired the supervisor while
    it drained leaves it closed."""
    async with supervisor._start_lock:
        await supervisor.drain(timeout=timeout)
        if reopen and _still_configured(supervisor):
            supervisor.reopen()


def _positive_int(name: str, value: Any) -> int:
    """A count the child would otherwise reject after a 30 GB load attempt.

    A bool is an int in Python and `str(True)` is not a number; a float or a
    string that is not a whole number would reach the binary as an argument
    error. Refuse them here, where the alias is named."""
    if isinstance(value, bool):
        raise LlamaServerError(f"{name} must be a positive whole number, got {value!r}")
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise LlamaServerError(f"{name} must be a positive whole number, got {value!r}") from None
    if n != value and str(n) != str(value).strip():
        raise LlamaServerError(f"{name} must be a positive whole number, got {value!r}")
    if n < 1:
        raise LlamaServerError(f"{name} must be a positive whole number, got {value!r}")
    return n


def _flash_attn_value(value: Any) -> Optional[str]:
    """`on`, `off`, or nothing at all, out of a field that used to be a bool.

    The binary takes `--flash-attn on|off|auto` and refuses it bare:

        error while handling argument "--flash-attn": unknown value ...

    `build_command` emitted it bare until 2026-08-22, so any alias that turned
    the switch on could not start — the child died on argv, before a backend
    was even loaded. Nothing short of handing the line to the binary shows it
    (Fable 5, 2026-08-22 unpushed-dev-batch review 1a).

    `False` means `off` and `None` means silence, which is why DEFAULTS holds
    None here rather than the False it held until 2026-08-22. With False as the
    default the two were indistinguishable: an alias nobody had touched and an
    alias whose owner had chosen "off" in the form arrived here as the same
    value, and mapping it either way was wrong for one of them. None is the
    absence, so False can now be the choice it was always displayed as.
    """
    if value is None:
        return None
    if value is True or value is False:
        return "on" if value else "off"
    text = str(value).strip().lower()
    if text in ("on", "off"):
        return text
    if text != "auto":
        logger.warning(
            "llama-server: flash_attn=%r is not on/off/auto; leaving the "
            "binary at its own default", value,
        )
    return None


_QUANTISED_KV_TYPES = frozenset({"q8_0", "q4_0", "q4_1", "iq4_nl", "q5_0", "q5_1"})


def _flash_attn_effective(value: Any, type_v: Any) -> str:
    """What the child will do, which silence here does not say.

    `auto` is the binary's default, and on a quantised V cache the pin
    resolves it to on and refuses to start when it is off
    (`src/llama-context.cpp:3698-3707`, b10809). So the one configuration
    production runs — no flag, `-ctv q4_0` — is the one a reader of the start
    line could not resolve without opening llama.cpp; the child's own log
    never names flash_attn at any verbosity.
    """
    explicit = _flash_attn_value(value)
    quantised_v = str(type_v or "").strip().lower() in _QUANTISED_KV_TYPES
    if explicit == "on":
        return "on"
    if explicit == "off":
        return "off (refused at start by the quantised V cache)" if quantised_v else "off"
    if quantised_v:
        return "on (auto, forced by the quantised V cache)"
    return "auto (build default)"


def _fmt_knob(value: Any) -> str:
    """How a knob reads in the start line: silence and an explicit 0 differ.

    `x or "build default"` prints "build default" for 0, and 0 is a choice for
    every knob here — no checkpoints, no cache reuse. Only None is silence.
    """
    return "build default" if value is None else str(value)


def window_outgrows_pool(window: Any, n_ctx: int) -> bool:
    """Whether one conversation is allowed to outgrow the pool every slot shares.

    `n_ctx` is one pool for all slots (the engine reports `kv_unified = true`);
    `context_window`, when an alias sets it, is what a single conversation may
    occupy. Nothing derives it from `n_ctx`, so they can disagree — and only this
    direction is silent, because the other merely wastes cells nobody fills.
    """
    return bool(window) and window > n_ctx


def _reraise_a_swallowed_cancel() -> None:
    """Raise the cancellation an awaited call returned through.

    httpcore runs each socket step under `anyio.fail_after`. A task.cancel()
    landing as that timeout fires is taken by anyio's `CancelScope.__exit__`
    for its own (`is_anyio_cancellation` reads the `__context__` chain) and
    swallowed; `uncancel()` drops only the scope's count. The request ends in
    ConnectTimeout and `cancelling()` stays above zero, so it is raised here."""
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# The KV-cache ladder for AUTO: the first rung that fits the card wins. f16 is
# deliberately NOT a rung — on 2026-08-19 an f16 load at 262 144 "succeeded"
# because WDDM does not refuse allocations, it spills them to shared memory:
# 5.2 GiB paged out, prefill collapsed from 784 to 47-51 tok/s, and the memo
# recorded the poisoned rung. f16 remains available as an explicit alias
# choice, where the supervisor warns from the arithmetic and loads anyway.
KV_LADDER = ("q8_0", "q4_0")

# Admission arithmetic, not the child's OOM verdict: bytes per KV element by
# cache type (q8_0 = 34/32 per block, q4_0 = 18/32, f16 = 2).
_KV_BYTES_PER_ELEM = {"f16": 2.0, "bf16": 2.0, "q8_0": 34 / 32, "q4_0": 18 / 32}

# Everything a loaded context costs beyond weights and attention-KV: compute
# buffers, MTP draft contexts, the hybrid blocks' recurrent state, CUDA
# context. Measured on qwen3.8-27B @ 262 144 on b10472 (ADR-040 table):
# 748 + 1360 + 1024 + 324 + 1136 = 4592, rounded up.
_DEFAULT_OVERHEAD_MIB = 4608

# The card is shared with the desktop; a load that consumes it all is the
# paging regime measured 2026-08-17 ("card busy computing nothing, window
# full"). The reserve keeps that from being re-created by arithmetic.
_DESKTOP_RESERVE_MIB = 2048

_OOM_MARKERS = ("out of memory", "failed to allocate", "cuda error")


def _device_probe(cmd: List[str], **kwargs: Any) -> "subprocess.CompletedProcess":
    """`subprocess.run(cmd, capture_output=True, **kwargs)`, registered as a
    `device_probe` (ADR-046 point 5); refused during the backend's stop unless
    `while_stopping` is passed (point 7)."""
    from ..dpc_agent.tools.process import run_probe

    return run_probe(cmd, launcher="device_probe", **kwargs)


def _free_vram_mib(while_stopping: bool = False) -> Optional[int]:
    """Free VRAM of the first NVIDIA GPU in MiB, or None when there is no
    signal. The same nvidia-smi the device-context collector already uses.

    `while_stopping` lets the query run during the backend's stop; only
    Strata's VRAM release check passes it, the stop's own reading of the card
    (ADR-046 point 7). Any other caller is refused then (`ProcessRegistryClosed`)."""
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = _device_probe(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            text=True, timeout=10, while_stopping=while_stopping,
        )
        return int(out.stdout.strip().splitlines()[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _total_vram_mib() -> Optional[int]:
    """Physical VRAM of the first NVIDIA GPU in MiB, or None without a signal.

    Admission is budgeted against physical memory, not free memory: free
    moves with the desktop and says nothing about what a load will do to it,
    and the desktop's share is what the reserve constant is for."""
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = _device_probe(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            text=True, timeout=10,
        )
        return int(out.stdout.strip().splitlines()[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


_GGUF_VALUE_SIZES = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}


_GGUF_INT_FORMATS = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 10: "<Q", 11: "<q"}
_gguf_int_cache: Dict[Tuple[str, int, int, Tuple[str, ...]], Dict[str, int]] = {}


def gguf_integer_keys(path: str, keys: Iterable[str]) -> Dict[str, int]:
    """The integer metadata `keys` a GGUF header holds, by key; a key it lacks is absent.

    Header only, no tensor is read. An unreadable file answers `{}`. Cached per
    path, mtime, size and key set, because a provider asks on every image."""
    wanted = tuple(sorted(set(keys)))
    try:
        info = os.stat(path)
    except OSError:
        return {}
    cache_key = (path, info.st_mtime_ns, info.st_size, wanted)
    if cache_key in _gguf_int_cache:
        return _gguf_int_cache[cache_key]
    found: Dict[str, int] = {}
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"GGUF" or struct.unpack("<I", f.read(4))[0] < 2:
                return {}
            _, n_kv = struct.unpack("<QQ", f.read(16))

            def read_str():
                n = struct.unpack("<Q", f.read(8))[0]
                return f.read(n).decode("utf-8", errors="replace")

            for _ in range(n_kv):
                key = read_str()
                t = struct.unpack("<I", f.read(4))[0]
                if t in _GGUF_INT_FORMATS:
                    fmt = _GGUF_INT_FORMATS[t]
                    value = struct.unpack(fmt, f.read(struct.calcsize(fmt)))[0]
                    if key in wanted:
                        found[key] = value
                elif t == 8:
                    read_str()
                elif t == 9:
                    et = struct.unpack("<I", f.read(4))[0]
                    cnt = struct.unpack("<Q", f.read(8))[0]
                    if et == 8:
                        for _ in range(cnt):
                            read_str()
                    elif et in _GGUF_VALUE_SIZES:
                        f.seek(_GGUF_VALUE_SIZES[et] * cnt, 1)
                    else:
                        break
                elif t in _GGUF_VALUE_SIZES:
                    f.seek(_GGUF_VALUE_SIZES[t], 1)
                else:
                    break
                if len(found) == len(wanted):
                    break
    except Exception as e:
        logger.warning("GGUF header unreadable in %s: %s", path, e)
        return {}
    _gguf_int_cache[cache_key] = found
    return found


_GGUF_KEYS_WALKED_AT_MOST = 4096
_GGUF_STRING_STEPS_AT_MOST = 200_000
_GGUF_STRING_BYTES_AT_MOST = 1 << 16


def gguf_string_key(path: str, key: str) -> Optional[str]:
    """The string metadata `key` a GGUF header holds, or None.

    Header only, with bounded reads: at most `_GGUF_KEYS_WALKED_AT_MOST` keys are
    walked, a string array is stepped over by length without being read (at most
    `_GGUF_STRING_STEPS_AT_MOST` strings in all), and the wanted value is read only
    up to `_GGUF_STRING_BYTES_AT_MOST` bytes. A file that is not GGUF, is older than
    version 2, is cut short, or lacks the key answers None; nothing is raised.
    """
    try:
        size = os.stat(path).st_size
        with open(path, "rb") as f:
            if f.read(4) != b"GGUF" or struct.unpack("<I", f.read(4))[0] < 2:
                return None
            _, n_kv = struct.unpack("<QQ", f.read(16))
            steps = 0

            def read_len():
                return struct.unpack("<Q", f.read(8))[0]

            def skip(n):
                if n < 0 or f.tell() + n > size:
                    raise EOFError("a length runs past the end of the file")
                f.seek(n, 1)

            for _ in range(min(n_kv, _GGUF_KEYS_WALKED_AT_MOST)):
                n = read_len()
                if n > _GGUF_STRING_BYTES_AT_MOST:
                    return None
                found = f.read(n).decode("utf-8", errors="replace") == key
                t = struct.unpack("<I", f.read(4))[0]
                if t == 8:
                    n = read_len()
                    if found:
                        if n > _GGUF_STRING_BYTES_AT_MOST:
                            return None
                        data = f.read(n)
                        return data.decode("utf-8", errors="replace") if len(data) == n else None
                    skip(n)
                elif found:
                    return None  # the key is there but is not a string
                elif t == 9:
                    et = struct.unpack("<I", f.read(4))[0]
                    cnt = read_len()
                    if et == 8:
                        steps += cnt
                        if steps > _GGUF_STRING_STEPS_AT_MOST:
                            return None
                        for _ in range(cnt):
                            skip(read_len())
                    elif et in _GGUF_VALUE_SIZES:
                        skip(_GGUF_VALUE_SIZES[et] * cnt)
                    else:
                        return None
                elif t in _GGUF_VALUE_SIZES:
                    skip(_GGUF_VALUE_SIZES[t])
                else:
                    return None
    except Exception as e:
        logger.debug("GGUF string key %s unreadable in %s: %s", key, path, e)
    return None


_SHARD_NAME = re.compile(r"^(?P<stem>.+)-(?P<index>\d{5})-of-(?P<count>\d{5})\.gguf$", re.IGNORECASE)


def model_files_in(folder: Path) -> List[Dict[str, Any]]:
    """The GGUF files of a model files folder, as the `list_model_files` rows.

    Walks `.gguf` files directly in `folder` and in its subfolders, one level and
    no deeper, skipping the subfolder `strata`. A split model (`...-00001-of-0000N.gguf`)
    is one row for its first part, with the summed size of the parts found beside it
    and `shard_count` N; the other parts are not rows. `architecture` is the header's
    `general.architecture` (None when the header cannot be read) and `is_mmproj` is
    `architecture == "clip"`, never the file name. A folder that is missing or
    unreadable gives an empty list; nothing is created.
    """
    rows: List[Dict[str, Any]] = []

    def scan(directory: Path, label: str) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError as e:
            logger.warning("model files folder %s cannot be listed: %s", directory, e)
            return
        sizes: Dict[str, int] = {}
        for entry in entries:
            try:
                if entry.name.lower().endswith(".gguf") and entry.is_file():
                    sizes[entry.name] = entry.stat().st_size
            except OSError:
                continue
        for name, size in sizes.items():
            shard = _SHARD_NAME.match(name)
            count = 1
            if shard:
                count = int(shard.group("count"))
                if int(shard.group("index")) != 1:
                    continue  # a later part of a split model: counted in its first part
                size = 0
                for other, other_size in sizes.items():
                    m = _SHARD_NAME.match(other)
                    if m and m.group("stem") == shard.group("stem") and int(m.group("count")) == count:
                        size += other_size
            path = Path(os.path.abspath(Path(directory) / name))
            architecture = gguf_string_key(str(path), "general.architecture")
            rows.append({
                "path": str(path),
                "folder": label,
                "size_bytes": size,
                "architecture": architecture,
                "is_mmproj": architecture == "clip",
                "shard_count": count,
            })

    root = Path(folder)
    scan(root, "")
    try:
        subfolders = sorted(e.name for e in os.scandir(root) if e.name != "strata" and e.is_dir())
    except OSError:
        subfolders = []
    for name in subfolders:
        scan(root / name, name)
    rows.sort(key=lambda r: (r["folder"].casefold(), r["folder"], Path(r["path"]).name.casefold(), r["path"]))
    return rows


def run_configs_in(folder: Path) -> List[str]:
    """Absolute paths of `strata-*.json` in `<folder>/strata/run-configs/`; empty when it is absent."""
    directory = Path(folder) / "strata" / "run-configs"
    try:
        names = sorted(e.name for e in os.scandir(directory)
                       if e.name.startswith("strata-") and e.name.endswith(".json") and e.is_file())
    except OSError:
        return []
    return [str(Path(os.path.abspath(directory / n))) for n in names]


def gguf_effort_dictionary(path: str) -> Optional[Tuple[Tuple[str, ...], Optional[str]]]:
    """(words the chat template accepts, the word it defaults to), or None.

    The template is the authority on effort words and it ships inside the model:
    `tokenizer.chat_template` holds the same jinja the server reports at /props,
    so the dictionary can be read without starting a child. A template that
    guards the value names its own vocabulary in the guard.
    """
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"GGUF":
                return None
            if struct.unpack("<I", f.read(4))[0] < 2:
                return None
            _, n_kv = struct.unpack("<QQ", f.read(16))

            def read_str():
                n = struct.unpack("<Q", f.read(8))[0]
                return f.read(n).decode("utf-8", errors="replace")

            template = None
            for _ in range(n_kv):
                key = read_str()
                t = struct.unpack("<I", f.read(4))[0]
                if t == 8:
                    value = read_str()
                    if key == "tokenizer.chat_template":
                        template = value
                        break
                elif t == 9:
                    et = struct.unpack("<I", f.read(4))[0]
                    cnt = struct.unpack("<Q", f.read(8))[0]
                    if et == 8:
                        for _ in range(cnt):
                            read_str()
                    elif et in _GGUF_VALUE_SIZES:
                        f.seek(_GGUF_VALUE_SIZES[et] * cnt, 1)
                    else:
                        return None
                elif t in _GGUF_VALUE_SIZES:
                    f.seek(_GGUF_VALUE_SIZES[t], 1)
                else:
                    return None
    except Exception as e:
        logger.warning("effort dictionary unreadable in %s: %s", path, e)
        return None
    if not template:
        logger.warning("no chat template in %s — effort words fall back to the table", path)
        return None
    words = effort_dictionary_of(template)
    if words is None:
        logger.warning(
            "the chat template in %s does not guard reasoning_effort — "
            "effort words fall back to the table", path,
        )
    return words


_EFFORT_GUARD = re.compile(r"reasoning_effort\s+not\s+in\s*\(([^)]*)\)")
_EFFORT_DEFAULT = re.compile(r"reasoning_effort\s*\|\s*default\(\s*'([^']+)'")


def effort_dictionary_of(template: str) -> Optional[Tuple[Tuple[str, ...], Optional[str]]]:
    """The guard's own tuple and the default beside it, or None when unguarded."""
    guard = _EFFORT_GUARD.search(template)
    if not guard:
        return None
    words = tuple(w.strip().strip("'\"") for w in guard.group(1).split(",") if w.strip())
    if not words:
        return None
    default = _EFFORT_DEFAULT.search(template)
    return words, (default.group(1) if default else None)


def _gguf_attention_kv_dims(path: str) -> Optional[Tuple[int, int]]:
    """(attention layer count, kv width per layer) from a GGUF's tensor directory.

    The attn_k weight of every attention block is 2-D (n_embd, n_kv_heads x
    head_dim); the second dim is what the KV cache stores per token per layer.
    Hybrid blocks (Gated DeltaNet here) carry no attn_k and contribute no
    attention KV — their state is fixed size and counted in the overhead
    constant. Returns None when the file cannot be read or has no attention.
    """
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"GGUF":
                return None
            version = struct.unpack("<I", f.read(4))[0]
            if version < 2:
                return None
            n_tensors, n_kv = struct.unpack("<QQ", f.read(16))

            def read_str():
                n = struct.unpack("<Q", f.read(8))[0]
                return f.read(n).decode("utf-8", errors="replace")

            def skip_value(t):
                if t == 8:
                    read_str()
                elif t == 9:
                    et = struct.unpack("<I", f.read(4))[0]
                    cnt = struct.unpack("<Q", f.read(8))[0]
                    if et == 8:
                        for _ in range(cnt):
                            read_str()
                    elif et in _GGUF_VALUE_SIZES:
                        f.seek(_GGUF_VALUE_SIZES[et] * cnt, 1)
                    else:
                        raise ValueError(f"unknown gguf array type {et}")
                elif t in _GGUF_VALUE_SIZES:
                    f.seek(_GGUF_VALUE_SIZES[t], 1)
                else:
                    raise ValueError(f"unknown gguf value type {t}")

            for _ in range(n_kv):
                read_str()
                skip_value(struct.unpack("<I", f.read(4))[0])

            pat = re.compile(r"^blk\.(\d+)\.attn_k\.weight$")
            nextn_pat = re.compile(r"^blk\.(\d+)\.nextn\.")
            layers = set()
            draft_layers = set()
            kv_width = 0
            for _ in range(n_tensors):
                name = read_str()
                n_dims = struct.unpack("<I", f.read(4))[0]
                dims = struct.unpack(f"<{n_dims}Q", f.read(8 * n_dims))
                f.read(4 + 8)  # type + offset
                m = pat.match(name)
                if m and n_dims == 2:
                    layers.add(int(m.group(1)))
                    kv_width = dims[1]
                nm = nextn_pat.match(name)
                if nm:
                    draft_layers.add(int(nm.group(1)))
            # A block carrying nextn tensors is the MTP draft layer: the loader
            # marks its attn_* unused (blk.64 on the production model) and
            # allocates no KV for it. Counting it was exactly the +6 % the
            # first formula run carried against the loader's own 8704 MiB.
            layers -= draft_layers
            if not layers:
                return None
            return len(layers), kv_width
    except (OSError, ValueError, struct.error):
        return None


def _kv_cache_mib(n_attn_layers: int, kv_width: int, n_ctx: int, cache_type: str) -> int:
    """Attention-KV bytes for one rung: 2 (K and V) x layers x width x ctx x
    bytes-per-element. Predicts exactly the loader's 8704 MiB for q8_0 @
    262 144 on the production model (16 attention layers — the MTP draft
    block is excluded; counting it was the first version's +6 % bias, in the
    safe direction but unexplained until the load log named blk.64 unused)."""
    per_elem = _KV_BYTES_PER_ELEM.get(cache_type)
    if not per_elem:
        return 0
    return int(2 * n_attn_layers * kv_width * per_elem * n_ctx / (1024 * 1024))


def _looks_like_oom(log_lines: List[str]) -> bool:
    joined = "\n".join(log_lines or []).lower()
    return any(marker in joined for marker in _OOM_MARKERS)


# The four lines the engine writes when a parked prefix cannot be laid back
# into the pool. Taken from this machine's own child logs rather than from a
# quote: 2 blocks in the current alias file, 6 in the pre-rename one.
# Matched on bytes, because the position they are remembered by is a byte
# offset into the log: decoding first would drift the two apart on any
# non-ASCII line the child ever writes.
_RESTORE_CELLS_RE = re.compile(rb"state_read_meta: failed to find (\d+) available cells in kv cache")
_RESTORE_SIZE_RE = re.compile(rb"load: failed to restore state with size (\d+)")
_RESTORE_SLOT_RE = re.compile(rb"prompt_load: id\s+(\d+) \| task \S+ \| failed to load prompt from cache")

# How far past the anchor line the companions are looked for. The four arrive
# within two milliseconds of each other; the window only has to survive another
# slot interleaving a line between them.
_RESTORE_BLOCK_BYTES = 4000


def format_restore_refusal(refusal: Dict[str, Any], n_ctx: int) -> str:
    """The sentence the engine does not write: the arithmetic behind a refusal.

    A restore that wanted W cells out of a P-cell pool failed because fewer
    than W were free, so at least P-W were held by other work. When W exceeds
    the pool the parked state could never have fitted it at all.
    """
    wanted = refusal["cells"]
    where = f"slot {refusal['slot']}" if refusal.get("slot") is not None else "an unnamed slot"
    size = f"{refusal['bytes'] / (1024 * 1024):.0f} MiB" if refusal.get("bytes") else "unknown size"
    if wanted >= n_ctx:
        arithmetic = (
            f"the parked state alone wants {wanted} of the {n_ctx}-cell pool, "
            "so it cannot fit whatever else is running"
        )
    else:
        arithmetic = (
            f"{wanted} cells wanted against a {n_ctx}-cell pool, so at least "
            f"{n_ctx - wanted} were held by other conversations"
        )
    return (
        f"host-cache restore refused on {where} ({size} parked): {arithmetic}. "
        "The turn re-reads that prefix from zero; the usage line's reuse% is what it cost."
    )


# `mean len` is tokens emitted per target forward pass, not draft length: it
# equals acceptance × n_max + 1, which reproduces every line in this box's log
# to two decimals. That identity is also the only place a finished child says
# which n_max it ran with — neither the command line nor the log records it.
_DRAFT_RE = re.compile(
    r"draft acceptance = ([\d.]+) \(\s*(\d+) accepted /\s*(\d+) generated\), "
    r"mean len =\s*([\d.]+)"
)


def _parse_draft(tail: str) -> Dict[str, Any]:
    """Speculation counters of the last finished task, or {} when there are none.

    Empty rather than zero: an alias with no `--spec-type` writes no such line,
    and a reader that cannot tell "not drafting" from "drafted nothing" is the
    defect this project already paid for once.
    """
    found = _DRAFT_RE.findall(tail)
    if not found:
        return {}
    rate, accepted, generated, mean_len = found[-1]
    rate, mean_len = float(rate), float(mean_len)
    out: Dict[str, Any] = {
        "draft_acceptance": rate,
        "draft_accepted": int(accepted),
        "draft_generated": int(generated),
        "draft_tokens_per_pass": mean_len,
    }
    if rate > 0:
        n_est = (mean_len - 1) / rate
        if abs(n_est - round(n_est)) <= 0.08 and 1 <= round(n_est) <= 16:
            out["draft_n_max"] = round(n_est)
    return out


def _gguf_mib(path: str) -> int:
    try:
        return os.path.getsize(path) // (1024 * 1024)
    except OSError:
        return 0


class LlamaServerSupervisor:
    """Owns the `llama-server` child process for one alias.

    Responsibilities and nothing else: spawn with flags, health-poll to a
    deadline, capture /props, read /slots through, drain, stop.
    """

    # The defaults this class fills under an alias's config. A subclass that
    # starts a different child (ADR-044's Strata launcher) names its own.
    DEFAULTS: Dict[str, Any] = DEFAULTS

    # The engine's name in what the card-holder check says (ADR-044 point 4).
    ENGINE = "llama-server"
    # Whether a child of this engine refuses the card to every other alias,
    # its own engine's included. Two llama-server aliases are left to the
    # admission arithmetic, as before the check existed.
    EXCLUSIVE_ON_THE_CARD = False
    # The process registry's launcher word for this engine (ADR-046 point 3).
    PROCESS_LAUNCHER = "llamacpp_server"
    # A stop DPC asks for outside its own shutdown and outside a failed start is a
    # person's action in the UI (the stop button, a settings save that retires the
    # child). `idle_unload` has no path of its own in the code (no child is stopped
    # for being idle), and a switch to the other engine reaches here as the same stop
    # button, so there is no word for it.
    DEFAULT_STOP = "ui_stop"

    @classmethod
    def merge_config(cls, config: Dict[str, Any]) -> Dict[str, Any]:
        """The alias config over this class's defaults; a None value is an absence."""
        return {**cls.DEFAULTS, **{k: v for k, v in config.items() if v is not None}}

    def __init__(self, alias: str, config: Dict[str, Any]):
        self.alias = alias
        self.config = self.merge_config(config)
        self.port: Optional[int] = None
        self.props: Optional[Dict[str, Any]] = None
        self._proc: Optional[asyncio.subprocess.Process] = None
        # Why the next stop is being asked for, set by the path that asks and
        # consumed by `_stop_child` (the process registry's stop word).
        self._stop_why: Optional[str] = None
        self._log_path = DPC_HOME / "logs" / f"llama-server-{alias}.log"
        self._log_fh = None
        self._draining = False
        # Set with `_draining` while an owner's start of a rival alias is freeing the
        # card (the idle swap): the alias that start is for. New calls are then refused
        # with that reason, and the mark is cleared if the swap gives up.
        self._swap_for: Optional[str] = None
        # A superseded child holds the whole model until it has finished
        # draining, and this card cannot hold two. Set by the provider when this
        # supervisor replaces a live one; `ensure_running` waits on it before
        # spending VRAM, so draining the old child cannot turn into two children.
        self._predecessor: Optional["asyncio.Task"] = None
        self._in_flight = 0
        # The most slots held at once since the last telemetry read. Nothing
        # serialises traffic to the child, so this is what says whether the
        # last timing block in the shared log belongs to the caller asking.
        self._peak_in_flight = 0
        # Serialises starts only: `_proc` and `props` also change outside it, when
        # an abandoned stop (`_abandoned_stop`) finishes after its start has gone.
        self._start_lock = asyncio.Lock()
        # The stop of a child whose start was interrupted, while it runs. It runs
        # as its own task so a second cancellation cannot leave it half done, and
        # the next start waits for it: the old child still holds the port.
        self._abandoned_stop: Optional["asyncio.Task"] = None
        # Where the restore-refusal scan has already looked. The log is opened
        # "ab" and outlives restarts, so None means "not primed yet": the first
        # scan starts at the end of the file rather than reporting a previous
        # child's refusal as this one's.
        self._restore_scan_offset: Optional[int] = None
        # Offsets already reported that the cursor deliberately sits behind, so
        # the rewind that lets a straddling anchor complete cannot also make a
        # whole one arrive twice.
        self._reported_refusals: set = set()
        # The pin folder the child runs from (its running pin), or None for a
        # configured binary or install; set by each start.
        self._pin_root: Optional[Path] = None
        # The prune after the current pin's first pass this process, while it runs.
        self._prune_task: Optional["asyncio.Future"] = None
        _SUPERVISORS.add(self)

    # --- command assembly, pure and table-testable -------------------------

    def build_command(self, binary: Path, port: int, cache_type: Optional[str] = None) -> List[str]:
        c = self.config
        cmd: List[str] = [str(binary)]
        cmd += ["-m", str(c["gguf_path"])]
        cmd += ["-c", str(c["n_ctx"])]
        ctk = cache_type or c["cache_type_k"]
        ctv = cache_type or c["cache_type_v"]
        if ctk:
            cmd += ["-ctk", str(ctk)]
        if ctv:
            cmd += ["-ctv", str(ctv)]
        # `is not None` and not truthiness, on all three of these: 0 is a
        # sentence in each — no layers on the card, no draft tokens, no host
        # cache — and the guard twenty lines below records this same class
        # being found and fixed for `-np` before it was repeated here.
        if c["n_gpu_layers"] is not None:
            cmd += ["-ngl", str(c["n_gpu_layers"])]
        flash_attn = _flash_attn_value(c["flash_attn"])
        if flash_attn is not None:
            cmd += ["--flash-attn", flash_attn]
        if c["mmproj"]:
            cmd += ["--mmproj", str(c["mmproj"])]
        if c["spec_type"] and c["spec_type"] != "none":
            cmd += ["--spec-type", str(c["spec_type"])]
            if c["spec_draft_n_max"] is not None:
                cmd += ["--spec-draft-n-max", str(c["spec_draft_n_max"])]
        # -np is sent whenever it is set, so an explicit 1 reaches the child —
        # the old `> 1` guard ate it and the server fell back to its own 4.
        # --kv-unified only means something above one slot; at one slot
        # unified and split are the same pool.
        if c["ctx_checkpoints"] is not None:
            cmd += ["--ctx-checkpoints", str(c["ctx_checkpoints"])]
        if c["checkpoint_min_step"] is not None:
            cmd += ["--checkpoint-min-step", str(c["checkpoint_min_step"])]
        if c["n_batch"]:
            cmd += ["-b", str(c["n_batch"])]
        if c["n_ubatch"]:
            cmd += ["-ub", str(c["n_ubatch"])]
        if c["n_parallel"]:
            cmd += ["-np", str(c["n_parallel"])]
        # Said out loud in both directions, because the binary's own default is
        # conditional — "enabled if number of slots is auto" — so silence means
        # one thing with `-np` set and the opposite without it. The old guard
        # emitted nothing below two slots, which left an alias that asked for a
        # unified pool alongside an explicit `-np` running a split one.
        if c["kv_unified"] is not None:
            cmd += ["--kv-unified" if c["kv_unified"] else "--no-kv-unified"]
        if c["cache_reuse"] is not None:
            cmd += ["--cache-reuse", str(c["cache_reuse"])]
        if c["cache_ram_mib"] is not None:
            cmd += ["--cache-ram", str(c["cache_ram_mib"])]
        if c["image_max_tokens"] is not None:
            cmd += ["--image-max-tokens", str(_positive_int("image_max_tokens", c["image_max_tokens"]))]
        if c["slot_save_path"]:
            cmd += ["--slot-save-path", str(c["slot_save_path"])]
        # `--jinja` is the binary's default, so emitting nothing for False left
        # the engine with jinja on and the control unable to express the only
        # thing it was added for. The negative form exists; use it.
        if c["jinja"] is not None:
            cmd += ["--jinja" if c["jinja"] else "--no-jinja"]
        cmd += ["--host", "127.0.0.1", "--port", str(port)]
        cmd += [str(a) for a in c["extra_args"]]
        return cmd

    def build_env(self, binary: Path) -> Dict[str, str]:
        env = dict(os.environ)
        backend = find_cuda_backend(binary.parent)
        if backend:
            env["GGML_BACKEND_PATH"] = str(backend)
        return env

    # --- the card (ADR-044 point 4) -------------------------------------------

    def on_the_card(self) -> bool:
        """False only when `extra_args` keep the child off the GPU (`--device none`)."""
        args = [str(a).strip().lower() for a in (self.config.get("extra_args") or [])]
        for i, a in enumerate(args):
            if a in ("--device=none", "-dev=none"):
                return False
            if a in ("--device", "-dev") and i + 1 < len(args) and args[i + 1] == "none":
                return False
        return True

    def _excludes(self, other: "LlamaServerSupervisor") -> bool:
        return (
            other.ENGINE != self.ENGINE
            or self.EXCLUSIVE_ON_THE_CARD
            or other.EXCLUSIVE_ON_THE_CARD
        )

    def _claim_card(self) -> None:
        """Refuse when another alias's child of an excluding engine holds the card;
        otherwise hold it. Synchronous on purpose: see `_CARD_CLAIMS`."""
        if not self.on_the_card():
            return
        what = _BACKGROUND_CALL.get()
        if what is not None and swap_policy()[0]:
            # Past the queue's door with a child up, which then went: it takes no card (rule 10).
            raise self._background_call_skipped(what)
        held = self._card_held_error()
        if held is not None:
            raise held
        _CARD_CLAIMS[self] = "claimed"

    def _blocking_holder(self) -> Optional[Tuple["LlamaServerSupervisor", str]]:
        """(supervisor, state) of the first live claim that excludes this one, or None."""
        if not self.on_the_card():
            return None
        for other, state in _live_claims():
            if other is self or other.alias == self.alias or not self._excludes(other):
                continue
            return other, state
        return None

    def _card_held_error(self) -> Optional[CardHeldError]:
        """The refusal `_claim_card` would raise now, or None. Claims nothing,
        so a policy can ask before it decides to start (ADR-044 point 9)."""
        found = self._blocking_holder()
        if found is not None:
            other, state = found
            # Said where it hurts (Mike, 2026-11-02): the switch that would have swapped.
            swap_on = swap_policy()[0]
            hint = ("" if swap_on else
                    " Or tick 'Swap local models when the card holder is idle' in Providers > "
                    "Local engines, and DPC stops an idle holder for you.")
            why = (f"this request is running on '{other.alias}' and would wait on itself"
                   if swap_on and _this_request_holds(other.alias) else
                   f"swapping is off, the start serves a peer, or '{other.alias}' is already stopping")
            return CardHeldError(
                f"{self.ENGINE}[{self.alias}] cannot load: the card is held by "
                f"{other.ENGINE} alias '{other.alias}' ({state}). "
                f"{self.ENGINE} and {other.ENGINE} cannot hold the card together and "
                f"DPC did not swap them here ({why}). Stop '{other.alias}' from the providers "
                f"settings to free the card, or switch this agent to '{other.alias}'.{hint}",
                other.alias, other.ENGINE,
            )
        if self.on_the_card() and self not in _CARD_CLAIMS and swap_policy()[0]:
            # The rule 7 check of a start that has no place: the card may be empty mid-swap, and
            # a start begun for a peer must not take it. `_launch` claims again, so a supervisor
            # holding a claim is no stranger; nor is the first place's own alias, which is here
            # to claim.
            if _CARD_QUEUE and _CARD_QUEUE[0].alias != self.alias:
                return self._a_place_waits_refusal()
        return None

    def _a_place_waits_refusal(self) -> CardQueueRefusal:
        """What a start with no place meets while one waits, a peer's above all. It names no
        alias of the place and nobody who stands anywhere: a peer reads this."""
        return CardQueueRefusal(
            f"{self.ENGINE}[{self.alias}] cannot take this now: a request on this node is waiting "
            "for the card, and this does not go ahead of it",
            self.alias, self.ENGINE,
        )

    def _background_call_skipped(self, what: str) -> "BackgroundCallSkipped":
        """Rule 10's named skip: into the record with its reader and the log, and the refusal
        that ends the call. Nothing here starts, marks or queues."""
        if _CARD_QUEUE:
            reason = f"a request is waiting in the queue for the card for '{_CARD_QUEUE[0].alias}'"
        elif self._draining:
            reason = f"'{self.alias}' is stopping"
        else:
            reason = f"'{self.alias}' is not on the card"
        what_follows = _BACKGROUND_CALLS[what]
        logger.info("%s[%s]: background call %s skipped: %s. %s",
                    self.ENGINE, self.alias, what, reason, what_follows)
        _LAST_BACKGROUND_SKIP[what] = {"alias": self.alias, "reason": reason, "what_follows": what_follows,
                                       "at": time.time()}
        return BackgroundCallSkipped(
            f"{self.ENGINE}[{self.alias}]: background call {what} skipped: {reason}",
            self.alias, self.ENGINE,
        )

    # --- the idle swap (ADR-044, amendment 2026-11-02) --------------------------

    # Whether stopping this engine drops what it parked for conversations.
    DROPS_PARKED_ON_STOP = False

    def _start_is_for_a_peer(self) -> bool:
        """A call served to a peer never swaps (ADR-044 amendment, rule 2)."""
        return peer_being_served() is not None

    def _draining_error(self) -> LlamaServerError:
        """The refusal of a new call or start while this supervisor drains. A background call
        that passed the queue's door before the drain began meets its skip here (rule 10)."""
        what = _BACKGROUND_CALL.get()
        if what is not None and swap_policy()[0] and self.on_the_card():
            return self._background_call_skipped(what)
        if self._swap_for is not None:
            return EngineDrainingError(f"'{self.alias}' is stopping to free the card for '{self._swap_for}'")
        return EngineDrainingError(f"{self.ENGINE}[{self.alias}] is draining; new calls refused")

    def _busy_for_the_card(self, state: str) -> bool:
        """The card queue's rule 1: a child is starting, a call is in flight, a request
        stands on this alias, or a caller is in its window there. A request that came after the
        acting place was taken does not count, by rule 4: it waits behind that place, and it
        must not keep the holder busy for it. `state` is this claim's in `_live_claims`."""
        return (state == "starting" or self._in_flight > 0
                or any(_stands_before_the_line(hold) for hold in _CARD_HOLDS.get(self.alias, ()))
                or bool(_callers_in_their_window(self.alias)))

    def _begin_swap_drain(self, new: "LlamaServerSupervisor") -> None:
        """Mark this holder as draining for `new`. Synchronous, and called in the same
        stretch as the last check that it is not busy: a call arriving after it is
        refused, not started under a stopping engine."""
        self._swap_for = new.alias
        self._draining = True

    def _end_swap_drain(self) -> None:
        """The swap gave up: the holder serves again."""
        self._swap_for = None
        self._draining = False

    def _swap_busy_refusal(self, holder: "LlamaServerSupervisor", state: str, stopping: bool,
                           request: _QueuedRequest) -> CardQueueRefusal:
        """Rule 5: the deadline passed before any mark. It names the holder, why it is busy and
        the setting; the holder is untouched. Decision 1's wait for a stop that never ended
        names the holder's state instead."""
        setting = f"at most {request.wait_s} s (Settings: Wait for a busy holder)"
        if stopping:
            return CardQueueRefusal(
                f"{self.ENGINE}[{self.alias}] cannot load: the card is held by {holder.ENGINE} alias "
                f"'{holder.alias}' ({state}), which has not let go after {request.waited()} s; a request "
                f"waits for the card {setting}.",
                holder.alias, holder.ENGINE,
            )
        return CardQueueRefusal(
            f"{self.ENGINE}[{self.alias}] cannot load: the card is held by {holder.ENGINE} alias "
            f"'{holder.alias}', busy for {request.waited()} s; a swap waits for a busy holder "
            f"{setting}. '{holder.alias}' serves as before."
            f"{_who_stands_on(holder.alias)}",
            holder.alias, holder.ENGINE,
        )

    def _behind_a_place_refusal(self, request: _QueuedRequest) -> CardQueueRefusal:
        """The deadline passed before this request was first in line."""
        ahead = _CARD_QUEUE[0].alias if _CARD_QUEUE else "nobody"
        found = self._blocking_holder()
        holder = (f" The card is held by {found[0].ENGINE} alias '{found[0].alias}'."
                  f"{_who_stands_on(found[0].alias)}" if found is not None else "")
        # A `came_late` request is refused here as it comes: it waited for nothing, and its age is
        # no wait. The bound here is the setting in both cases; `stop_bound` plays no part.
        waited = (f"it came {request.waited()} s after its acceptance, with no time left to wait for the card"
                  if request.came_late else f"it waited {request.waited()} s for the card")
        return CardQueueRefusal(
            f"{self.ENGINE}[{self.alias}] cannot load: {waited} behind "
            f"'{ahead}', and a request waits at most {request.wait_s} s (Settings: Wait for a busy "
            f"holder).{holder}",
            found[0].alias if found is not None else ahead,
            found[0].ENGINE if found is not None else self.ENGINE,
        )

    def _ran_out_refusal(self, request: _QueuedRequest) -> CardQueueRefusal:
        """The deadline passed with nothing busy in the way: this alias's own stop, or the
        child a settings save superseded, had not ended."""
        if request.came_late:
            return CardQueueRefusal(
                f"{self.ENGINE}[{self.alias}] cannot load: this alias's earlier child did not let go within "
                f"{request.stop_limit()}.",
                self.alias, self.ENGINE,
            )
        return CardQueueRefusal(
            f"{self.ENGINE}[{self.alias}] cannot load: it waited {request.waited()} s for this alias's "
            f"earlier child to let go, and a request waits at most {request.wait_s} s (Settings: Wait "
            "for a busy holder).",
            self.alias, self.ENGINE,
        )

    def _child_is_up(self) -> bool:
        """The fast path of a start: a child that answers and is not being stopped."""
        return (self._proc is not None and self._proc.returncode is None and bool(self.props)
                and not self._draining)

    def _this_request_is_not_new(self) -> bool:
        """Rule 4: a later round of a running request, or one accepted before the place that now
        acts, on this alias with a child on the card, starting included: a request that waited
        behind a place would be waiting for its own hold. A call of an open window is `admitted`
        at acceptance at the gateway and asked by name at the peer door."""
        hold = _CARD_HOLD.get()
        return (hold is not None and hold.alias == self.alias and _stands_before_the_line(hold)
                and self in _CARD_CLAIMS and not self._draining)

    def _this_request_stands_elsewhere_on_the_card(self) -> bool:
        """Its hold is on another alias that has a child on the card. A place that acts may be
        waiting for exactly that hold, so a wait in line would be a wait on itself, rule 9's
        reasoning for an alias that does not exclude its own."""
        hold = _CARD_HOLD.get()
        return (hold is not None and hold.alias is not None and hold.alias != self.alias
                and any(sup.alias == hold.alias for sup in list(_CARD_CLAIMS)))

    def _will_not_reopen(self) -> bool:
        """Draining, with no stop on its way to `reopen`: retired by a settings save, or its
        provider closed. `stop_by_hand` holds the start lock through to its `reopen`, and a swap's
        mark says one is coming, and those are waited for (decision 1); this one no wait ends."""
        return self._draining and (not _still_configured(self)
                                   or (self._swap_for is None and not self._start_lock.locked()))

    def _check_a_wait_can_end(self, request: _QueuedRequest) -> None:
        request.check_still_in_line()
        if self._will_not_reopen():
            raise self._draining_error()

    async def _wait_in_the_card_queue(self) -> Optional[_QueuedRequest]:
        """Take a place and wait to be first in line (rules 3, 4, 5, 7 and 13), before anything
        of this alias is locked. None when this request goes on without one, and the start's own
        path then says the rest: the swap off (today's refusal), a child that takes no card, a
        peer's call on an open window, a start begun for a peer and rule 9, whose claims refuse,
        and what rule 4 says is not new. A background call never takes a place: it passes on a
        child already up with nobody waiting, and is skipped otherwise (rule 10)."""
        if not swap_policy()[0] or not self.on_the_card():
            return None
        what = _BACKGROUND_CALL.get()
        if what is not None and (_CARD_QUEUE or not self._child_is_up()):
            raise self._background_call_skipped(what)
        peer = peer_being_served()
        if peer is not None:
            # A peer never takes a place, and a new one does not go ahead of one (rule 7).
            if _CARD_QUEUE and not (self._child_is_up() and peer in _callers_in_their_window(self.alias)):
                raise self._a_place_waits_refusal()
            return None
        if self._start_is_for_a_peer():
            return None
        found = self._blocking_holder()
        if found is not None and _this_request_holds(found[0].alias):
            return None
        if _CARD_QUEUE:
            if self._this_request_is_not_new() or self._this_request_stands_elsewhere_on_the_card():
                return None
        elif self._child_is_up():
            return None
        if self._will_not_reopen():
            raise self._draining_error()
        place = next((p for p in _CARD_QUEUE if p.alias == self.alias), None)
        if place is None:
            place = _CardPlace(self.alias)
            _CARD_QUEUE.append(place)
        request = _QueuedRequest(place)
        place.requests.append(request)
        try:
            await self._wait_to_be_first(request)
        except BaseException:
            request.leaves()
            raise
        return request if request.in_line() else None

    async def _wait_to_be_first(self, request: _QueuedRequest) -> None:
        """Wait until this request is first in line, or leave. Only the first place acts."""
        told = False
        while True:
            self._check_a_wait_can_end(request)
            if not swap_policy()[0]:
                # The switch was thrown during the wait: the claim says today's refusal.
                request.leaves()
                return
            if request.is_first():
                return
            now = _swap_clock()
            if now >= request.give_up_at:
                raise self._behind_a_place_refusal(request)
            if not told:
                told = True
                logger.info(
                    "%s[%s]: waiting behind '%s' in the queue for the card, up to %d s from acceptance",
                    self.ENGINE, self.alias, _CARD_QUEUE[0].alias, request.wait_s,
                )
                request.says_it_waits()
            await asyncio.sleep(min(_SWAP_POLL_S, request.give_up_at - now))

    async def _within(self, request: Optional[_QueuedRequest], what: Awaitable[Any]) -> Any:
        """`what`, which stands between a first place and its claim, bounded for the request:
        it leaves when taken out of the line or out of time, and `what` is cancelled. None is a
        start with no place, which waits as it always did."""
        if request is None:
            return await what
        task = asyncio.ensure_future(what)
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=_SWAP_POLL_S)
                if done:
                    return task.result()
                self._check_a_wait_can_end(request)
                if _swap_clock() >= request.stop_bound:
                    raise self._ran_out_refusal(request)
        except BaseException:
            task.cancel()
            raise

    @contextlib.asynccontextmanager
    async def _start_lock_for(self, request: Optional[_QueuedRequest]):
        """`_start_lock` for a start, taken for a first place so that it can still leave. Only a
        stop on the other side of the lock counts against its deadline: a start of its own alias
        that holds the lock is bounded by `start_timeout_s`, and it is what this request wants."""
        lock = self._start_lock
        if request is None or not lock.locked():
            await lock.acquire()
        else:
            task = asyncio.ensure_future(lock.acquire())
            try:
                while True:
                    done, _ = await asyncio.wait({task}, timeout=_SWAP_POLL_S)
                    if done:
                        task.result()
                        break
                    self._check_a_wait_can_end(request)
                    if self._draining and _swap_clock() >= request.stop_bound:
                        raise self._ran_out_refusal(request)
            except BaseException:
                if task.done() and not task.cancelled() and task.exception() is None:
                    lock.release()
                else:
                    task.cancel()
                raise
        try:
            yield
        finally:
            lock.release()

    async def _free_the_card(self, request: Optional[_QueuedRequest]) -> None:
        """This alias's start, first in line, with an excluding holder on the card: stop the
        holder once it is no longer busy, within the request's one deadline (ADR-044, amendments
        of 2026-11-02: the idle swap, and the card queue's rules 1, 2, 5, 9 and 13). A busy
        holder is not marked and not stopped: it serves what it has. Returns when no excluding
        holder is left, or when the rules say no swap (the caller's `_claim_card` then raises
        today's refusal); raises the busy refusal when the deadline passes.

        Called right before `_claim_card`. The last holder check, the busy test and the
        drain mark are one synchronous stretch, and so are the last check and the
        return: nothing awaits between this method's end and the claim, and nothing can come
        ahead of this request in line in that stretch, because places only join at the back."""
        if request is None:
            return
        for _ in range(_MAX_SWAP_ROUNDS):
            told_about = None
            while True:
                request.check_still_in_line()
                if self._draining:
                    raise self._draining_error()  # retired while it waited: no child nobody holds
                if not swap_policy()[0]:
                    return  # the switch was thrown during the wait: the claim says today's refusal
                found = self._blocking_holder()
                if found is None:
                    return  # freed, by us or by someone else's stop: not this swap's doing
                holder, state = found
                if _this_request_holds(holder.alias):
                    return  # rule 9: it would wait on itself
                now = _swap_clock()
                # A holder stopping for another reason is waited for like a busy one (decision 1).
                stopping = state == "stopping" or holder._draining
                in_time = now < request.give_up_at or request.came_late
                if not stopping and in_time and not holder._busy_for_the_card(state):
                    break
                if now >= request.give_up_at:
                    raise self._swap_busy_refusal(holder, state, stopping, request)
                if holder is not told_about:
                    told_about = holder
                    logger.info(
                        "%s[%s]: '%s' (%s) is %s; waiting up to %d s from acceptance before "
                        "stopping it to free the card", self.ENGINE, self.alias, holder.alias,
                        holder.ENGINE, state if stopping else "busy", request.wait_s,
                    )
                request.says_it_waits()
                await asyncio.sleep(min(_SWAP_POLL_S, request.give_up_at - now))
            holder._begin_swap_drain(self)  # no await since the last check above
            request.says_it_waits()
            await self._wait_for_the_stop(holder, request)
        if self._blocking_holder() is not None:
            # Out of rounds: `_claim_card` names whoever holds the card now. FIFO is what closes
            # the ping-pong, so reaching this means a holder keeps coming back past the line.
            logger.warning(
                "%s[%s]: the safety net of %d holders swapped out for one start was reached; "
                "'%s' holds the card now", self.ENGINE, self.alias, _MAX_SWAP_ROUNDS,
                self._blocking_holder()[0].alias,
            )

    async def _wait_for_the_stop(self, holder: "LlamaServerSupervisor", request: _QueuedRequest) -> None:
        """The stop of a marked holder, as its own task, and this request's wait for it, bounded
        by what is left of its deadline (decisions 2 and 3). The remainder is not `drain`'s
        timeout, which reads a timeout as how long to wait before it stops the child anyway; by
        rule 2 nothing is in flight here, and a stop is never hurried. So a request that runs out
        of time, is cancelled or is taken out leaves the stop running to its end, and that stop
        clears the mark."""
        stop = asyncio.ensure_future(self._stop_for_the_swap(holder, request))
        _SWAP_STOPS.add(stop)
        stop.add_done_callback(_SWAP_STOPS.discard)
        # Retrieved here too, for the request that left before it ended.
        stop.add_done_callback(lambda task: task.cancelled() or task.exception())
        while not stop.done():
            request.check_still_in_line()
            if _swap_clock() >= request.stop_bound:
                ran_out = (request.stop_limit() if request.came_late
                           else f"the wait of {request.wait_s} s (Settings: Wait for a busy holder)")
                raise CardQueueRefusal(
                    f"{self.ENGINE}[{self.alias}] cannot load: '{holder.alias}' is already stopping to "
                    f"free the card for it, and {ran_out} ran out. The stop goes on, and the card goes "
                    "to the next in line.",
                    holder.alias, holder.ENGINE,
                )
            await asyncio.wait({stop}, timeout=_SWAP_POLL_S)
        stop.result()

    async def _stop_for_the_swap(self, holder: "LlamaServerSupervisor", request: _QueuedRequest) -> None:
        """`stop_by_hand`, and the mark gone however it ended: a stop that failed leaves the holder
        serving, and a retired one closed but marked for nobody. The swap is said here, as it is
        done whoever is still waiting for it."""
        try:
            await stop_by_hand(holder)
        except BaseException:
            holder._end_swap_drain()
            raise
        holder._swap_for = None
        self._note_swap(holder, gave_up=not request.in_line())

    def _note_swap(self, holder: "LlamaServerSupervisor", gave_up: bool = False) -> None:
        """Say a swap happened: the INFO line, `last_swap`, one toast. Nothing awaits
        here, so the card is claimed in the stretch that follows. `gave_up`: the request it was
        for left during the stop, which ran to its end, and a stopped Strata's parked
        conversations are gone all the same, so it is said in those words."""
        global _LAST_SWAP
        dropped = holder.DROPS_PARKED_ON_STOP
        tail = "; its parked conversations are dropped" if dropped else ""
        message = (f"stopped '{holder.alias}' to free the card for '{self.alias}', which stopped waiting{tail}"
                   if gave_up else f"swapped '{holder.alias}' out for '{self.alias}'{tail}")
        logger.info("%s", message)
        _LAST_SWAP = {
            "from": holder.alias, "to": self.alias,
            "from_engine": holder.ENGINE, "to_engine": self.ENGINE,
            "at": time.time(), "parked_dropped": dropped,
        }
        try:
            task = asyncio.get_running_loop().create_task(_toast("Local engine swapped", message))
        except RuntimeError:
            return
        _SWAP_TOASTS.add(task)
        task.add_done_callback(_SWAP_TOASTS.discard)

    def _release_card_unless_live(self) -> None:
        """A start that failed before leaving a live child holds nothing. A child
        still being stopped holds the card until its stop lets go of it."""
        if self._abandoned_stop is not None and not self._abandoned_stop.done():
            return
        if self._proc is None or self._proc.returncode is not None:
            _CARD_CLAIMS.pop(self, None)

    def reopen(self) -> None:
        """Accept calls again after a manual stop: the alias is still configured,
        unlike a retired one, and its next call may start the child again."""
        self._draining = False
        self._swap_for = None

    # --- lifecycle -----------------------------------------------------------

    @on_the_owner_loop
    async def ensure_running(self) -> Dict[str, Any]:
        """The server is up and its /props are returned; starts it if needed, in its turn at
        the card (ADR-044, amendment 2026-11-02, the card queue, rule 3)."""
        request = await self._wait_in_the_card_queue()
        try:
            props = await self._start_if_needed(request)
        except BaseException:
            if request is not None:
                request.leaves()
            raise
        if request is not None:
            request.passes()
        return props

    async def _start_if_needed(self, request: Optional[_QueuedRequest]) -> Dict[str, Any]:
        """What `ensure_running` does in its turn; each engine has its own (the Strata override).

        A cache type the alias set explicitly is started once, as configured.
        Without one, the KV ladder in `_launch_auto_kv` picks the rung that
        fits the free VRAM the card has right now."""
        async with self._start_lock_for(request):
            if self._draining:
                raise self._draining_error()
            if self._proc and self._proc.returncode is None and self.props:
                return self.props
            # Before the card is claimed, and never a fetch on this loop: a missing
            # pin is offered to the owner and refused, or installed in a thread when
            # no consent service is bound. A retained pin that passed a start runs
            # meanwhile (Mike's call, 2026-11-01): the owner is asked with "degrade".
            self._pin_root = None
            binary = await asyncio.to_thread(resolve_binary, self.config)
            fallback: Optional[Path] = None
            if not self.config.get("binary_path"):
                plan = await asyncio.to_thread(
                    self._plan_pin_start, Path(binary).parent if binary is not None else None
                )
                if binary is None and plan["start"] is not None and not (
                    peer_being_served() is None and _MODEL_DOWNLOAD_SERVICE is None
                ):
                    if peer_being_served() is None:
                        await self._offer_new_pin(_fetcher, plan["start"])
                elif binary is None:
                    if peer_being_served() is None and _MODEL_DOWNLOAD_SERVICE is None:
                        binary = await asyncio.to_thread(install_pin)
                        plan = {"start": Path(binary).parent, "fallback": plan["retained"]}
                    else:
                        await self._refuse_missing_pin()
                fallback = plan["fallback"]
                if plan["start"] is not None and _is_pin(plan["start"]):
                    self._pin_root = plan["start"]
                    binary = plan["start"] / server_binary_name()
            if self._draining:
                raise self._draining_error()
            await self._within(request, self._clear_unfinished_start())
            await self._within(request, self._await_predecessor())
            # Before the ladder: its free-VRAM refusal would otherwise name the
            # model's size instead of the engine that holds the card.
            await self._free_the_card(request)
            self._claim_card()
            if request is not None:
                request.passes()  # in the stretch of the claim: the next place reads it as busy
            try:
                self.port = self.port or _free_port()

                async def launch() -> Dict[str, Any]:
                    child = self._pin_root / server_binary_name() if self._pin_root is not None else binary
                    if self.config.get("cache_type_k") is not None or self.config.get("cache_type_v") is not None:
                        self._warn_if_explicit_type_exceeds_budget()
                        return await self._launch(child)
                    return await self._launch_auto_kv(child)

                return await self._launch_pin(launch, fallback)
            except BaseException:
                self._release_card_unless_live()
                raise

    # --- the running pin: retained, rolled back to, passed (B2 step 3) ---------

    def _pin_fetcher(self) -> Any:
        return _fetcher

    def _pin_tag(self) -> str:
        return LLAMA_CPP_TAG

    def _pin_usable(self, root: Path) -> bool:
        """A pin other than the current one this start may run (no network)."""
        return _is_pin(root) and (root / server_binary_name()).is_file()

    def _plan_pin_start(self, current: Optional[Path]) -> Dict[str, Optional[Path]]:
        """What a pin-mode start runs. `current` is the current pin's root when it is
        installed. Returns {"start", "retained", "fallback"}: the root to start (the
        retained pin while the current is missing or after a rollback), the retained
        pin, and the root a failed first start rolls back to — only the retained pin,
        never an older one, and only for a pin that never passed a start. Blocking."""
        found = retained_pin(self._pin_fetcher().pins_dir(), self._pin_tag())
        retained = found["root"] if found and self._pin_usable(found["root"]) else None
        if current is None:
            return {"start": retained, "retained": retained, "fallback": None}
        record = _ROLLBACKS.get(_same_path(current))
        if record is not None and self._pin_usable(record["to_root"]):
            return {"start": record["to_root"], "retained": retained, "fallback": None}
        marker = read_marker(current) or {}
        first = _is_pin(current) and _start_passed_at(marker) is None
        return {"start": current, "retained": retained, "fallback": retained if first else None}

    async def _offer_new_pin(self, fetcher: Any, running: Path) -> None:
        """Ask the owner for the current pin while the retained one runs: never a
        refusal, so the dialog's decline only degrades (Mike's call, 2026-11-01)."""
        logger.info("%s[%s]: the pin %s is not installed; running the retained pin %s meanwhile",
                    self.ENGINE, self.alias, self._pin_tag(), running.parent.name)
        service = _MODEL_DOWNLOAD_SERVICE
        if service is None:
            return
        try:
            await service.maybe_emit_required(
                fetcher.DOWNLOAD_NAME,
                purpose=fetcher.PURPOSE,
                consequence_if_declined=(
                    f"{self.ENGINE} keeps running the retained pin {running.parent.name}, which "
                    f"passed a start on this node, instead of {self._pin_tag()}."
                ),
                on_decline="degrade",
                provider_alias=self.alias,
            )
        except Exception:
            logger.warning("could not offer the %s download", fetcher.DOWNLOAD_NAME, exc_info=True)

    def _fault_is_oom(self, error: BaseException) -> bool:
        return _looks_like_oom(getattr(error, "log_lines", None) or [])

    def _rollback_due(self, error: BaseException) -> bool:
        """Whether `error` is the build's fault, and not one of the failures a
        retained pin would meet too: the card held, an admission refusal, a config
        error, out of memory, a cancellation (not an Exception) or a drain."""
        if not getattr(error, "_build_fault", False):
            return False
        if isinstance(error, (CardHeldError, PinNotInstalledError)):
            return False
        return not self._fault_is_oom(error) and not self._draining

    def _rollback_lasts(self) -> str:
        return "until the backend restarts"

    async def _roll_back(self, error: BaseException, to_root: Path) -> None:
        """Record the rollback, say it once (ERROR log, `error_toast`), switch to `to_root`."""
        from_root = self._pin_root
        reason = (str(error).splitlines() or [type(error).__name__])[0][:300]
        record = {
            "engine": self.ENGINE, "from": from_root.parent.name, "to": to_root.parent.name,
            "reason": reason, "at": time.time(), "to_root": to_root,
        }
        _ROLLBACKS[_same_path(from_root)] = record
        lasts = self._rollback_lasts()
        logger.error(
            "%s[%s]: the new pin %s failed its first start (%s); starting the retained pin %s "
            "once instead, and later starts run it %s",
            self.ENGINE, self.alias, record["from"], reason, record["to"], lasts,
        )
        await _toast(
            "Local engine rolled back",
            f"{self.ENGINE} pin {record['from']} failed its first start for '{self.alias}': {reason}. "
            f"Running the retained pin {record['to']} instead, {lasts}.",
        )
        self._pin_root = to_root

    async def _launch_pin(
        self, launch: Callable[[], Awaitable[Dict[str, Any]]], fallback: Optional[Path]
    ) -> Dict[str, Any]:
        """`launch()`, and once more on `fallback` when a new pin's first start failed
        for the build's fault; then the pass is recorded. Under the start lock, with
        the card claimed."""
        try:
            props = await launch()
        except Exception as e:
            if fallback is None or self._pin_root is None or not self._rollback_due(e):
                raise
            await self._roll_back(e, fallback)
            self._release_card_unless_live()
            if self._draining:
                raise self._draining_error() from e
            self._claim_card()
            props = await launch()
        await self._note_pass()
        return props

    def _is_current_pin(self, root: Path) -> bool:
        try:
            return _same_path(root) == _same_path(self._pin_fetcher().install_root())
        except ValueError:
            return False

    async def _note_pass(self) -> None:
        """Mark the running pin's passed start, once per root per process, off the
        loop; after the current pin's, prune the others in a thread."""
        root = self._pin_root
        if root is None or _same_path(root) in _PASSES_NOTED:
            return
        _PASSES_NOTED.add(_same_path(root))
        try:
            await asyncio.to_thread(note_start_passed, root)
        except Exception as e:
            logger.warning("%s[%s]: could not record the passed start in %s: %s", self.ENGINE, self.alias, root, e)
            return
        if self._is_current_pin(root):
            self._prune_task = asyncio.ensure_future(asyncio.to_thread(self._prune_after_pass, root))

    def _prune_after_pass(self, current: Path) -> List[Dict[str, Any]]:
        """Keep the current pin and the retained one; remove the other pins. Nothing
        goes while no other pin has passed a start: the old pins wait for a retained
        one to exist (Mike's call, 2026-11-01). Blocking; never raises."""
        try:
            folder = self._pin_fetcher().pins_dir()
            retained = retained_pin(folder, self._pin_tag())
            if retained is None:
                logger.info("%s: no other pin has passed a start; every older pin is kept", self.ENGINE)
                return []
            return prune_pins(folder, keep=[current, retained["root"]], in_use=_roots_in_use())
        except Exception:
            logger.warning("%s: pruning the older pins failed", self.ENGINE, exc_info=True)
            return []

    async def _refuse_missing_pin(
        self, fetcher: Any = None, tag: str = LLAMA_CPP_TAG, for_peer: Optional[str] = None,
        sized_by_alias: bool = False,
    ) -> NoReturn:
        """Raise `PinNotInstalledError` for a start with no pin to run. A peer's call
        (or a start begun `for_peer`) neither asks nor fetches; the owner's asks once
        per session through the consent service and is worded by what that service
        now knows. `fetcher` is the engine's fetcher module, llama.cpp's by default;
        `sized_by_alias` when its sizes depend on this alias's config (Strata's engine)."""
        fetcher = fetcher or _fetcher
        name = fetcher.DOWNLOAD_NAME
        if sized_by_alias:
            size = fetcher.size_bytes(self.config)
            at_most = fetcher.size_source(self.config) != "package"
        else:
            size = fetcher.size_bytes()
            at_most = getattr(fetcher, "SIZE_SOURCE", None) is not None
        if for_peer is not None or peer_being_served() is not None:
            reason, percent, error = "peer", None, None
        else:
            service = _MODEL_DOWNLOAD_SERVICE
            try:
                await service.maybe_emit_required(
                    name,
                    purpose=fetcher.PURPOSE,
                    consequence_if_declined=fetcher.CONSEQUENCE_IF_DECLINED,
                    on_decline="block",
                    provider_alias=self.alias,
                )
            except Exception:
                logger.warning("could not offer the %s download", name, exc_info=True)
            status = service.get_status(name)
            state = status.get("state")
            reason = state if state in ("downloading", "declined", "failed") else "asked"
            percent = status.get("progress_percent")
            error = status.get("error")
        logger.info("%s[%s]: not started, the %s pin %s is not installed (%s)",
                    self.ENGINE, self.alias, name, tag, reason)
        raise PinNotInstalledError(
            refusal_text(name, self.alias, size, reason, percent=percent, at_most=at_most,
                         error=error if reason == "failed" else None), name
        )

    # --- the KV ladder --------------------------------------------------------

    def _fit_build(self) -> str:
        """Which build the memo entry is for: a rung that fit one build is no word
        about another. The pin is its tag; a configured binary is its resolved
        path with size and mtime, so a rebuild in place re-runs the ladder once."""
        configured = self.config.get("binary_path")
        if not configured:
            return self._pin_root.parent.name if self._pin_root is not None else LLAMA_CPP_TAG
        path = Path(configured).resolve()
        try:
            st = path.stat()
        except OSError:
            return str(path)
        return f"{path}@{st.st_size}@{st.st_mtime_ns}"

    def _fit_key(self) -> str:
        c = self.config
        return ":".join(str(x) for x in (
            self._fit_build(), _gguf_mib(c["gguf_path"]), c["n_ctx"], c["n_gpu_layers"],
            c["n_parallel"], c["spec_draft_n_max"],
        ))

    @staticmethod
    def _fit_memo_path() -> Path:
        # Beside the pins, not in one: a pin folder holds only its install and marker.
        # The old <pin>/.dpc-kv-fit.json is not read; its keys lack the build, so none would match.
        return pins_dir() / ".dpc-kv-fit.json"

    def _read_fit_memo(self) -> Dict[str, Any]:
        try:
            return json.loads(self._fit_memo_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write_fit_memo(self, key: str, cache_type: str, free_mib: Optional[int]) -> None:
        # Best-effort: a memo that cannot be written only costs the ladder a
        # re-run, never a start.
        try:
            memo = self._read_fit_memo()
            memo[key] = {"type": cache_type, "free_mib": free_mib or 0, "ts": int(time.time())}
            path = self._fit_memo_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(memo), encoding="utf-8")
        except (OSError, ValueError):
            logger.debug("llama-server[%s]: could not write the KV fit memo", self.alias, exc_info=True)

    def _overhead_mib(self) -> int:
        """The alias's own overhead when it names one, the measured default
        otherwise. Whichever it is, the arithmetic below prints it."""
        configured = self.config.get("vram_overhead_mib")
        try:
            return int(configured) if configured is not None else _DEFAULT_OVERHEAD_MIB
        except (TypeError, ValueError):
            logger.warning(
                "llama-server[%s]: vram_overhead_mib %r is not a number — using %d",
                self.alias, configured, _DEFAULT_OVERHEAD_MIB,
            )
            return _DEFAULT_OVERHEAD_MIB

    def _admission(self) -> Optional[Tuple[int, int, int]]:
        """(budget_mib, weights_mib, kv_dims) for the arithmetic, or None when
        the card or the model cannot be sized and the ladder must fall back to
        listening for the child's own failures. Physical VRAM minus the desktop
        reserve is the budget: free memory moves with the desktop and predicts
        nothing about what the load will do to it."""
        total = _total_vram_mib()
        if total is None:
            return None
        dims = _gguf_attention_kv_dims(self.config["gguf_path"])
        weights = _gguf_mib(self.config["gguf_path"])
        if dims is None or not weights:
            return None
        return total - _DESKTOP_RESERVE_MIB, weights, dims

    def _predicted_total_mib(self, admission: Tuple[int, int, int], cache_type: str) -> int:
        _, weights, (layers, width) = admission
        return weights + _kv_cache_mib(layers, width, self.config["n_ctx"], cache_type) + self._overhead_mib()

    def _warn_if_explicit_type_exceeds_budget(self) -> None:
        """An alias that names a KV type owns the consequence — the arithmetic
        warns, it does not refuse."""
        admission = self._admission()
        if admission is None:
            return
        budget, _, _ = admission
        cache_type = self.config.get("cache_type_k")
        predicted = self._predicted_total_mib(admission, cache_type)
        if predicted > budget:
            logger.warning(
                "llama-server[%s]: KV %s explicitly configured — arithmetic predicts "
                "%d MiB against a %d MiB budget; loading anyway (explicit choice "
                "overrides arithmetic)",
                self.alias, cache_type, predicted, budget,
            )

    async def _launch_auto_kv(self, binary: Path) -> Dict[str, Any]:
        """Start at the best rung the arithmetic and the card both clear.

        Two gates per rung, in order: admission arithmetic (weights + KV bytes
        for the rung + fixed overhead + desktop reserve <= physical VRAM) and,
        for what passes, the child's own out-of-memory verdict — which under
        WDDM is a late and unreliable signal, since Windows pages instead of
        refusing (2026-08-19: an f16 rung "fit" that way and prefill collapsed
        16x). A rung that fit is memoised with the free-VRAM level it fit at,
        and reused only while the card has at least that much free again — a
        busier card re-runs the ladder. A memoised rung the arithmetic now
        refuses falls through to the full ladder and is allowed to rewrite
        the memo."""
        key = self._fit_key()
        free = _free_vram_mib()
        memo = self._read_fit_memo().get(key)
        memo_hit = bool(memo and free is not None and free >= (memo.get("free_mib") or 0))
        admission = self._admission()
        if memo_hit:
            candidates = [memo["type"]]
        else:
            gguf_mib = _gguf_mib(self.config["gguf_path"])
            if free is not None and gguf_mib and free < gguf_mib + 4096:
                raise LlamaServerError(
                    f"llama-server[{self.alias}]: {free} MiB free on the GPU against a "
                    f"{gguf_mib} MiB model — no KV type will fit; free the card or point "
                    "the alias at a smaller model"
                )
            candidates = list(KV_LADDER)
        if admission is not None:
            budget, weights, _ = admission
            for rung in KV_LADDER:
                if rung not in candidates:
                    candidates.append(rung)
            for rung in list(candidates):
                predicted = self._predicted_total_mib(admission, rung)
                if predicted > budget:
                    logger.warning(
                        "llama-server[%s]: KV %s refused by arithmetic — predicted %d MiB "
                        "(weights %d + kv %d + overhead %d) against a %d MiB budget",
                        self.alias, rung, predicted, weights,
                        predicted - weights - self._overhead_mib(), self._overhead_mib(),
                        budget,
                    )
                    candidates.remove(rung)
                    memo_hit = memo_hit and rung != memo.get("type")
            if not candidates:
                raise LlamaServerError(
                    f"llama-server[{self.alias}]: no KV rung fits the arithmetic — "
                    "the model is too large for this card at n_ctx "
                    f"{self.config['n_ctx']}"
                )
        for cache_type in candidates:
            try:
                props = await self._launch(binary, cache_type=cache_type)
            except LlamaServerError as e:
                if not _looks_like_oom(e.log_lines) or cache_type == candidates[-1]:
                    raise
                logger.warning(
                    "llama-server[%s]: KV %s did not fit (out of memory); stepping down",
                    self.alias, cache_type,
                )
                continue
            if not memo_hit:
                self._write_fit_memo(key, cache_type, free)
            return props
        raise LlamaServerError(f"llama-server[{self.alias}] unreachable ladder state")

    def log_start(
        self,
        cache_type: Optional[str],
        env: Dict[str, str],
        binary: Optional[Path] = None,
    ) -> None:
        """What the child was actually given, in one line, plus the one
        disagreement between two of those numbers that fails silently.

        The prompt cache is why the line exists at all: its size can arrive as
        LLAMA_ARG_CACHE_RAM from the environment, where it is invisible in the
        command line, in the config and in the child's own captured log — three
        readers of that log reached three different answers about it in one
        afternoon (2026-08-20).

        The speculation pair is here for the third time the same thing happened.
        On 2026-08-23 the owner changed `spec_draft_n_max` from 4 to 3 to compare
        the acceptance the two produce — and neither this line nor the child's own
        log records which value a child ran with, so the comparison would have
        rested on someone remembering what the config said at the time. A knob
        worth changing is a knob worth reading back.

        `binary` is here for the same reason, one reader later. The child log is
        one file per alias and carries no line naming the executable that wrote
        it, so two builds' starts are indistinguishable in it — and on
        2026-08-22 a reviewer who did the right thing and read the whole file
        still drew the opposite conclusion from the right lines, because the
        failures were the pin and the successes a hand-built binary. Since
        `binary_path` reached the provider form, two builds side by side is an
        ordinary state rather than a laboratory one.
        """
        _cache = c_ram if (c_ram := self.config.get("cache_ram_mib")) else (
            f"{env.get('LLAMA_ARG_CACHE_RAM')} (from environment)"
            if env.get("LLAMA_ARG_CACHE_RAM") else "build default"
        )
        n_ctx = self.config["n_ctx"]
        window = self.config.get("context_window")
        logger.info(
            "llama-server[%s] starting on :%s (binary=%s, n_ctx=%s, context_window=%s, "
            "kv=%s, flash_attn=%s, cache_ram=%s, ctx_checkpoints=%s, checkpoint_min_step=%s, "
            "n_ubatch=%s, cache_reuse=%s, spec_type=%s, spec_draft_n_max=%s, "
            "image_max_tokens=%s, vram_overhead=%s)",
            self.alias, self.port, binary if binary is not None else "unknown",
            n_ctx, _fmt_knob(window),
            cache_type or "configured",
            _flash_attn_effective(
                self.config.get("flash_attn"),
                cache_type or self.config.get("cache_type_v"),
            ),
            _cache,
            _fmt_knob(self.config.get("ctx_checkpoints")),
            _fmt_knob(self.config.get("checkpoint_min_step")),
            _fmt_knob(self.config.get("n_ubatch")),
            _fmt_knob(self.config.get("cache_reuse")),
            _fmt_knob(self.config.get("spec_type")),
            _fmt_knob(self.config.get("spec_draft_n_max")),
            _fmt_knob(self.config.get("image_max_tokens")),
            f"{self._overhead_mib()} MiB"
            + ("" if self.config.get("vram_overhead_mib") else " (measured elsewhere)"),
        )
        if window_outgrows_pool(window, n_ctx):
            # The direction that fails without a word: the agent fills to a
            # limit the engine never allocated, and the guard meant to refuse
            # before the model dies is calibrated on the larger number.
            logger.warning(
                "llama-server[%s]: context_window %s is larger than the KV pool "
                "n_ctx %s — one conversation may grow past everything the child "
                "allocated for all of its slots together",
                self.alias, window, n_ctx,
            )

    async def _launch(self, binary: Path, cache_type: Optional[str] = None) -> Dict[str, Any]:
        # A second child on the same port dies at bind, and assigning it would
        # lose the only handle on the first (2026-10-22: the orphan on :64276).
        live = self._proc
        if live is not None and live.returncode is None:
            raise LlamaServerError(
                f"{self.ENGINE}[{self.alias}]: a child (pid {getattr(live, 'pid', None)}) "
                f"is still running on :{self.port}; refusing to start a second over it"
            )
        if live is not None:
            self._end_child(live)  # it died on its own and nobody looked until now
        # Again per rung: a failed rung's stop released the card, and another
        # engine may have taken it since.
        self._claim_card()
        cmd = self.build_command(binary, self.port, cache_type=cache_type)
        env = self.build_env(binary)
        # What the child was actually given, in one line. The prompt cache is the
        # reason this exists: its size can arrive as LLAMA_ARG_CACHE_RAM from the
        # environment, where it is invisible in the command line, in the config
        # and in the child's own captured log — three readers of that log reached
        # three different answers about it in one afternoon (2026-08-20).
        self.log_start(cache_type, env, binary)
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_fh = open(self._log_path, "ab")
        self.prime_restore_scan()
        proc = self._proc = await self._spawn(cmd, env)
        try:
            await self._wait_healthy()
            self._answered_by(proc, "/health")
            try:
                props = await self._get("/props")
            except Exception as e:
                raise _build_fault(e)
            _reraise_a_swallowed_cancel()
            self._answered_by(proc, "/props")
            self._check_build_info(props)
            self.props = props
            return self.props
        except BaseException:
            # BaseException, not Exception: a caller cancelled mid-start (an agent
            # killed mid-call) raises CancelledError, and that path used to leave
            # the child running with no props and nobody holding it.
            await self._stop_the_spawned(proc)
            raise

    def _check_build_info(self, props: Any) -> None:
        """llama.cpp's version check (Mike's call, 2026-11-01): the running pin's build
        number is a whole token of `/props` `build_info` (`b11669`, `build 11669`); a
        configured binary is never checked. The raw text is logged on every start."""
        build = props.get("build_info") if isinstance(props, dict) else None
        logger.info("%s[%s]: /props build_info %r", self.ENGINE, self.alias, build)
        root = self._pin_root
        if root is None:
            return
        tag = root.parent.name
        number = tag[1:] if tag[:1] == "b" and tag[1:].isdigit() else None
        if number is None:
            logger.warning("%s[%s]: pin tag %s carries no build number; build_info not checked",
                           self.ENGINE, self.alias, tag)
            return
        if isinstance(build, str) and re.search(rf"(?<![0-9A-Za-z])b?{number}(?![0-9A-Za-z])", build):
            return
        raise _build_fault(LlamaServerError(
            f"{self.ENGINE}[{self.alias}]: /props build_info {build!r} does not name build "
            f"{number}, the pin {tag} this child was started from",
            self.tail_log(),
        ))

    def _answered_by(self, proc: Any, path: str) -> None:
        """Refuse an answer `proc` cannot have given: /health and /props are read
        from the port, not from the process, and a child that is dead or no longer
        `_proc` did not answer them — whatever still holds the port did (an old
        launcher still exiting). Props taken from it would make this child's later
        death look like a started engine's (ADR-044 point 3). Catches a death the
        loop has already registered; one still in flight passes."""
        if self._proc is proc and proc.returncode is None:
            return
        self._end_child(proc)
        state = (
            f"exited with {proc.returncode}" if proc.returncode is not None
            else "is no longer this supervisor's child"
        )
        message = (
            f"{self.ENGINE}[{self.alias}]: the child (pid {getattr(proc, 'pid', None)}) "
            f"{state} by the time {path} answered on :{self.port}, so another process "
            f"answered it — that start's failure, not an engine death"
        )
        logger.warning("%s", message)
        raise LlamaServerError(message, self.tail_log())

    async def _stop_the_spawned(self, proc: Any) -> None:
        """Stop `proc`, the child an unfinished start spawned, and nothing else."""
        if proc is None or self._proc is not proc:
            return
        await self._stop_shielded("failed_start")

    @on_the_owner_loop
    async def _stop_shielded(self, why: Optional[str] = None) -> None:
        """`stop()` as its own task under a shield: a second cancellation that
        arrives while the child is asked to stop leaves the stop running, and the
        next start waits for it (`_await_abandoned_stop`). A stop already in
        flight is joined, not doubled: a second one would find `_proc` cleared,
        release the card and stand in for the first while that one still runs.
        Forwarded by itself: the task is made on the owner loop whoever calls."""
        task = self._abandoned_stop
        if task is None or task.done():
            task = self._abandoned_stop = asyncio.ensure_future(self._stop_logged(why))
        await asyncio.shield(task)

    async def _stop_logged(self, why: Optional[str] = None) -> None:
        # Never raises: an error here would replace the start's own exception,
        # a cancellation included, in the handler that called it.
        if why is not None:
            self._stop_why = why
        try:
            await self.stop()
        except Exception as exc:
            logger.warning(
                "%s[%s]: could not stop the child of an unfinished start: %s",
                self.ENGINE, self.alias, exc,
            )

    @on_the_owner_loop
    async def _await_abandoned_stop(self) -> None:
        """Wait for the abandoned stop, and forget it only once it has finished.
        A waiter cancelled mid-wait leaves it in place for the next start: it is
        the only handle on a child that may still hold the port."""
        while True:
            task = self._abandoned_stop
            if task is None:
                return
            if not task.done():
                # `wait`, not `await task`: a cancelled waiter leaves the stop running.
                await asyncio.wait({task})
            if self._abandoned_stop is task:
                self._abandoned_stop = None

    async def _clear_unfinished_start(self) -> None:
        """Under the start lock: a live child without /props belongs to a start
        nobody is running any more. Stop it before spawning another on its port."""
        await self._await_abandoned_stop()
        proc = self._proc
        if proc is not None and proc.returncode is None and not self.props:
            logger.warning(
                "%s[%s]: a child (pid %s) of an unfinished start is still running on "
                ":%s; stopping it before starting again",
                self.ENGINE, self.alias, getattr(proc, "pid", None), self.port,
            )
            await self._stop_the_spawned(proc)
            await self._await_abandoned_stop()

    async def _spawn(self, cmd: List[str], env: Dict[str, str]) -> asyncio.subprocess.Process:
        self._refuse_while_the_backend_stops()
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            stdout=self._log_fh,
            stderr=asyncio.subprocess.STDOUT,
            creationflags=_CREATION_NEW_PROCESS_GROUP,
        )
        self._register_child(proc, cmd)
        return proc

    # --- the process registry (ADR-046) ---------------------------------------

    def _refuse_while_the_backend_stops(self) -> None:
        """No child is born once the backend's stop has begun (point 7, step 1)."""
        try:
            _procreg.get_registry().check_open()
        except _procreg.ProcessRegistryClosed as e:
            raise LlamaServerError(f"{self.ENGINE}[{self.alias}]: {e}") from None

    def _register_child(self, proc: Any, cmd: List[str]) -> None:
        """Record the child as a subsystem process. A stand-in with no integer pid
        (a test's fake) has nothing to record."""
        pid = getattr(proc, "pid", None)
        if not isinstance(pid, int):
            return
        try:
            _procreg.get_registry().register(
                pid,
                launcher=self.PROCESS_LAUNCHER,
                launcher_kind="subsystem",
                command=cmd,
                log=logger,
                detail=f"alias {self.alias}, port {self.port}",
                log_paths=self._output_log_paths(),
            )
        except _procreg.ProcessRegistryClosed as e:
            # The stop began between the check and the spawn: the child exists and
            # nothing would end it.
            try:
                proc.kill()
            except Exception:
                pass
            raise LlamaServerError(f"{self.ENGINE}[{self.alias}]: {e}") from None

    def _output_log_paths(self) -> List[Path]:
        """The files the child writes its output to; their newest mtime is its
        `last_output_at` in the registry."""
        return [p for p in (getattr(self, "_log_path", None),) if p is not None]

    def _note_stop(self, proc: Any) -> None:
        """Announce to the registry the stop about to be sent to `proc`, with the
        word the path that asked gave (else `DEFAULT_STOP`)."""
        why, self._stop_why = self._stop_why or self.DEFAULT_STOP, None
        pid = getattr(proc, "pid", None)
        if isinstance(pid, int):
            _procreg.get_registry().note_stop_sent(pid, "stopped", why)

    def _end_child(self, proc: Any) -> None:
        """Record that `proc` has ended, with the code the loop read. A child still
        running is not an end."""
        pid = getattr(proc, "pid", None)
        code = getattr(proc, "returncode", None)
        if isinstance(pid, int) and code is not None:
            _procreg.get_registry().end(pid, code)

    async def _wait_healthy(self) -> None:
        deadline = time.monotonic() + float(self.config["start_timeout_s"])
        while time.monotonic() < deadline:
            if self._proc and self._proc.returncode is not None:
                self._end_child(self._proc)
                raise _build_fault(LlamaServerError(
                    f"{self.ENGINE}[{self.alias}] exited with {self._proc.returncode} before "
                    f"becoming healthy",
                    self.tail_log(),
                ))
            healthy = await self._health_ok()
            _reraise_a_swallowed_cancel()
            if healthy:
                return
            await asyncio.sleep(0.5)
        raise _build_fault(LlamaServerError(
            f"{self.ENGINE}[{self.alias}] did not become healthy within "
            f"{self.config['start_timeout_s']}s",
            self.tail_log(),
        ))

    async def _health_ok(self) -> bool:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"http://127.0.0.1:{self.port}/health")
                return resp.status_code == 200
        except Exception:
            return False

    async def _get(self, path: str) -> Any:
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"http://127.0.0.1:{self.port}{path}")
            resp.raise_for_status()
            return resp.json()

    @on_the_owner_loop
    async def slots(self) -> Dict[str, Any]:
        """Live slot occupancy, read through without interpretation."""
        if not self.port:
            raise LlamaServerError(f"{self.ENGINE}[{self.alias}] is not running")
        return await self._get("/slots")

    def _read_tail(self, window: int = 65536) -> Optional[Tuple[bytes, int]]:
        """(the last `window` bytes, the absolute offset they start at).

        Bytes rather than text: one of the two readers remembers where it
        stopped, and that position has to mean the same thing as the seek.
        """
        try:
            with open(self._log_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                start = max(0, size - window)
                f.seek(start)
                return f.read(), start
        except OSError:
            return None

    _SCAN_CHUNK = 8 * 1024 * 1024

    def _read_from(self, offset: int) -> Optional[Tuple[bytes, int]]:
        """(bytes from `offset` forward, the offset they start at).

        Forward from the cursor, not backward from EOF. A tail read loses every
        refusal a busy window has already pushed out of it — measured: a refusal
        with 70 KiB of log after it was reported as nothing, and stayed nothing,
        which is exactly the noisy start this mechanism exists for. A read is
        capped, and the cursor stops short of the cap so the next call picks up
        a match that straddled the boundary.
        """
        try:
            with open(self._log_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                start = 0 if offset > size else max(0, offset)
                f.seek(start)
                return f.read(self._SCAN_CHUNK), start
        except OSError:
            return None

    def prime_restore_scan(self) -> None:
        """Watch from here on. Called at launch, so the previous child's lines
        in this append-only file are never reported as this one's."""
        try:
            self._restore_scan_offset = self._log_path.stat().st_size
        except OSError:
            self._restore_scan_offset = 0
        self._reported_refusals = set()

    def new_restore_refusals(self) -> List[Dict[str, Any]]:
        """Restore refusals appended since the last scan, oldest first.

        The engine logs a prompt-cache load only when it FAILS, so this is the
        one place the host cache is visible at all. Keyed by the anchor line's
        absolute offset: the window moves forward, the offset does not go back.
        """
        if self._restore_scan_offset is None:
            self.prime_restore_scan()
            return []
        already = self._reported_refusals
        read = self._read_from(self._restore_scan_offset)
        if read is None:
            return []
        data, start = read
        end = start + len(data)
        if start < self._restore_scan_offset:
            # The file shrank — rotated or truncated under us. What we counted
            # is gone; watch what is there now, including its first byte.
            self._restore_scan_offset = start
        out: List[Dict[str, Any]] = []
        for m in _RESTORE_CELLS_RE.finditer(data):
            offset = start + m.start()
            if offset < self._restore_scan_offset or offset in already:
                continue
            block = data[m.end():m.end() + _RESTORE_BLOCK_BYTES]
            size_m = _RESTORE_SIZE_RE.search(block)
            slot_m = _RESTORE_SLOT_RE.search(block)
            out.append({
                "cells": int(m.group(1)),
                "bytes": int(size_m.group(1)) if size_m else None,
                "slot": int(slot_m.group(1)) if slot_m else None,
                "offset": offset,
            })
        # To the end of what was read, never to the last hit: a cursor that stops
        # at the last refusal re-reads the same window for ever and loses the
        # event once the window has moved past it.
        capped = len(data) == self._SCAN_CHUNK
        self._restore_scan_offset = max(
            self._restore_scan_offset,
            end - _RESTORE_BLOCK_BYTES if capped else end,
        )
        cursor = self._restore_scan_offset
        self._reported_refusals = {
            r["offset"] for r in list(out) + [{"offset": o} for o in already]
            if r["offset"] >= cursor
        }
        return out

    def log_restore_refusals(self) -> int:
        """Say what the child said, with the arithmetic it does not carry."""
        refusals = self.new_restore_refusals()
        for refusal in refusals:
            logger.warning(
                "llama-server[%s]: %s",
                self.alias, format_restore_refusal(refusal, int(self.config["n_ctx"])),
            )
        return len(refusals)

    def tail_log(self, n: int = 12) -> List[str]:
        try:
            with open(self._log_path, "rb") as f:
                lines = f.read().decode("utf-8", errors="replace").splitlines()
            return lines[-n:]
        except OSError:
            return []

    def supersedes(self, drain_task: Optional["asyncio.Task"]) -> None:
        """Record the drain of the child this supervisor replaces."""
        self._predecessor = drain_task

    async def _await_predecessor(self) -> None:
        """Do not start until the superseded child has actually let go.

        Without this, draining the old child only trades one failure for
        another: the old one keeps its ~25 GB while a call that arrives during
        the drain starts a second child on a card that cannot hold both.

        The wait re-checks itself, because the world moves while it waits. Two
        saves inside one turn make this supervisor a predecessor in its turn:
        it is retired and set draining while it is still queued behind the child
        it was replacing. Reading `_draining` once before the wait — which is
        what `ensure_running` does — cannot see that, and the supervisor would
        wake up and start a child nobody wants, on a card that by then holds
        another. A second predecessor arriving during the wait is the same
        story from the other side, and the loop covers both.

        `wait`, and the handle kept until the drain is done: a cancelled waiter
        must neither cancel the drain nor leave the next start nothing to wait on.
        """
        while True:
            task = self._predecessor
            if task is None or task.done():
                if self._predecessor is task:
                    self._predecessor = None
                break
            logger.info(
                "%s[%s]: waiting for the superseded child to finish its "
                "in-flight work and release the card before starting",
                self.ENGINE, self.alias,
            )
            await asyncio.wait({task})
            if self._predecessor is task:
                self._predecessor = None
            if task.cancelled():
                raise asyncio.CancelledError()
            exc = task.exception()
            if exc is not None:  # a failed drain must not block the successor
                logger.warning(
                    "%s[%s]: the superseded child's drain ended badly (%s); "
                    "starting anyway", self.ENGINE, self.alias, exc,
                )
            if self._draining:
                raise LlamaServerError(
                    f"{self.ENGINE}[{self.alias}] was itself superseded while waiting "
                    "for its predecessor; refusing to start a child nobody holds"
                )

    def call_slot(self):
        """Claim one in-flight call; refuses while draining, so drain can wait.

        Deliberately synchronous: callers use it as `async with call_slot():`
        and an `async def` here would hand them a coroutine instead of the
        slot — the exact TypeError Johnny hit live on 2026-08-19 21:26, which
        every test missed because the fakes had the right shape and the one
        real-supervisor test awaited the coroutine instead."""
        if self._draining:
            raise self._draining_error()
        self._in_flight += 1
        self._peak_in_flight = max(self._peak_in_flight, self._in_flight)
        return self._Slot(self)

    class _Slot:
        def __init__(self, supervisor: "LlamaServerSupervisor"):
            self._supervisor = supervisor

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            self._supervisor._in_flight -= 1
            return False

    @on_the_owner_loop
    async def drain(self, timeout: Optional[float] = 120.0) -> None:
        """Refuse new work, let in-flight calls finish, then stop.

        `timeout=None` waits as long as the work takes. That is Mike's rule for
        a configuration change, 2026-08-22: a save must never cut a generation
        that is already running, and an agent turn on 40-170K context can run
        for minutes. Shutdown does not come through here — it calls `stop`
        directly — so an unbounded wait cannot hang the process exit.
        """
        self._draining = True
        try:
            if timeout is None:
                await self._wait_idle()
            else:
                await asyncio.wait_for(self._wait_idle(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "%s[%s]: drain timed out with %d call(s) still in flight",
                self.ENGINE, self.alias,
                self._in_flight,
            )
        await self.stop()

    async def _wait_idle(self) -> None:
        # A silent wait is the shape this project keeps rediscovering: the work
        # is happening, nothing says so, and the operator concludes it hung. One
        # line every 15 s costs nothing and answers "is it stuck or is it busy".
        waited = 0.0
        while self._in_flight > 0:
            await asyncio.sleep(0.2)
            waited += 0.2
            if waited % 15 < 0.2:
                logger.info(
                    "%s[%s]: draining, %d call(s) still in flight after %.0fs",
                    self.ENGINE, self.alias,
                    self._in_flight,
                    waited,
                )

    @on_the_owner_loop
    async def stop(self) -> None:
        """Stop the child, and only then let go of the card."""
        if self in _CARD_CLAIMS:
            _CARD_CLAIMS[self] = "stopping"
        try:
            await self._stop_child()
        finally:
            if self._proc is not None and self._proc.returncode is None:
                _CARD_CLAIMS[self] = "claimed"  # `_kill_or_keep` could not stop it: it still holds the card
            else:
                _CARD_CLAIMS.pop(self, None)

    async def _stop_child(self) -> None:
        """Ask the child to stop with a signal that lets it flush, then wait.

        terminate() is banned in this file: on Windows it discards the
        child's stdio buffers, which is how a week of server logs were lost.
        """
        proc, self._proc = self._proc, None
        self.props = None
        if proc is None or proc.returncode is not None:
            self._stop_why = None
            if proc is not None:
                self._end_child(proc)  # it was already dead: an exit, not a stop
            self._close_log()
            return
        self._note_stop(proc)
        try:
            try:
                proc.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT)
            except ProcessLookupError:
                raise
            except OSError as exc:
                # Not delivered (access denied, no console to share): kill now,
                # before any await, so a loop that closes here leaves no child.
                logger.warning(
                    "%s[%s]: could not signal the child (pid %s) to stop (%s); killing it",
                    self.ENGINE, self.alias, getattr(proc, "pid", None), exc,
                )
                self._kill_or_keep(proc)
            await asyncio.wait_for(proc.wait(), timeout=20.0)
        except (ProcessLookupError, asyncio.TimeoutError):
            self._kill_or_keep(proc)
            await proc.wait()
        finally:
            self._close_log()
            self._end_child(proc)
            if self._proc is not proc:
                logger.info("%s[%s] stopped (rc=%s)", self.ENGINE, self.alias, proc.returncode)

    def _kill_or_keep(self, proc: Any) -> None:
        """`proc.kill()`; when even that fails, the child is put back in `_proc`
        so the next start sees it and refuses to spawn over it, and the stop
        raises instead of reporting a child it could not stop as gone."""
        try:
            proc.kill()
        except ProcessLookupError:
            return
        except OSError as exc:
            if proc.returncode is None and self._proc is None:
                self._proc = proc
            raise LlamaServerError(
                f"{self.ENGINE}[{self.alias}]: the child (pid {getattr(proc, 'pid', None)}) "
                f"could not be stopped ({exc}); it still holds :{self.port}"
            ) from exc

    def last_task_timings(self) -> Optional[Dict[str, Any]]:
        """Exact prefill/decode rates of the most recently finished task, read
        from the child's own print_timing lines - the engine's numbers, not an
        estimate. Gives the phase split to non-streaming callers (the agents'
        tools path has no first-chunk boundary to time it by itself).

        Not attributable under concurrency, and it says so rather than
        guessing. The caveat here used to rest on "the supervisor's serialized
        traffic", which the same file refutes: call_slot increments a counter
        and refuses only while draining - there is no semaphore, no queue and
        no cap. So when more than one slot has been held since the last read,
        the last block in the shared log belongs to whichever task finished
        last and this returns None instead of handing it to the caller."""
        import re
        peak, self._peak_in_flight = self._peak_in_flight, self._in_flight
        if peak > 1:
            logger.debug(
                "%s[%s]: %d slots in flight since the last read, so the "
                "engine's timing block is not this caller's", self.ENGINE, self.alias, peak,
            )
            return None
        read = self._read_tail()
        if read is None:
            return None
        tail = read[0].decode("utf-8", errors="replace")
        # [\d.]+ in the rate capture: the rate is fractional ("438.86 tokens
        # per second") and a \d+ capture eats only its tail ("86").
        pre = re.findall(
            r"prompt eval time =\s+[\d.]+ ms /\s+(\d+) tokens \(.*?([\d.]+) tokens per second\)",
            tail,
        )
        # (?<!prompt ) — "prompt eval time" lines contain "eval time" as a
        # substring; without the lookbehind the decode capture lands on the
        # true eval line only by line ORDER inside a block (review: works-by-
        # order is the class this codebase burns out). The negative lookbehind
        # identifies decode lines by the absence of the marker, not by position.
        dec = re.findall(
            r"(?<!prompt )eval time =\s+[\d.]+ ms /\s+(\d+) tokens \(.*?([\d.]+) tokens per second\)",
            tail,
        )
        if not pre or not dec:
            return None
        n_prompt, prefill_rate = pre[-1]
        n_gen, decode_rate = dec[-1]
        out = {
            "prefill_tok_s": int(float(prefill_rate)),
            "decode_tok_s": int(float(decode_rate)),
            "engine_prompt_tokens": int(n_prompt),
            "engine_gen_tokens": int(n_gen),
        }
        out.update(_parse_draft(tail))
        return out

    def _close_log(self) -> None:
        if self._log_fh:
            try:
                self._log_fh.close()
            except OSError:
                pass
            self._log_fh = None
