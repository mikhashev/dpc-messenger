"""
DPC Agent — music tools for the owner's agent (ADR-051 D42-D51; ADR-052 section 3.5).

Nine tools over the core music service: `music_capabilities`, `music_propose_job`,
`music_check`, `music_wait`, `music_cancel_job`, `music_list_takes`, `music_list_library`,
`music_get_lyrics`, `music_playlist_add` (`chat_post_file`, the tenth name of D42, is its own
module). Each is off by default. They were `owner_only` (D43); since ADR-053 each asks the
owner when someone other than the owner asked for the turn (`owner_approval_for_others`, the
registry's gate), and each checks again, per call, as a second line (`_not_owner`).

Every call goes to the CoreService command on the service loop (`ctx.agent_event_loop`), so
broadcasts, the job store's lock and the card lease stay in one place. The tools return text,
never bytes; a take is named by its `take_id` and reached through `get_music_take`, which
gives the take's own file and nothing of the job's request (D44).

- `music_propose_job` makes one job per call (D45), records who asked (`requested_by` with the
  agent, chat and turn, D46) and asks the person first through the approval machinery of
  `schedule_task` (D47). A refusal makes no job. A repeat in the same turn returns the turn's
  job and asks nothing.
- `music_wait` polls every `POLL_INTERVAL_S` and follows the self-wait rule (D51): when this
  turn holds a live card hold on a local alias and the job is `queued`, or the job's `card_wait`
  names that alias among its holders (a running job whose engine ended and waits for the card
  again), it answers at once, since the job cannot move before this turn ends. Otherwise it
  reports the job's real state.
  It reads the turn's Stop and Kill between polls (`turn_stop_event`, `turn_kill_event`, set on
  the context by the loop), and a turn has `TURN_WAIT_BUDGET_S` of waiting in all.
- No output carries elapsed seconds: they would make every poll look new to the loop guard
  (`guards._OUTPUT_KEYED_TOOLS` holds `music_wait` and `music_check`).

`check_back` is the way back to a job (D49): `schedule_task` with task_type `check_back` and
`music_job_id` in its task data asks no approval when that job is this agent's and still
queued or running (`own_music_job_to_return_to`); core then sets its delay
(`check_back_delay_s`) and its wake text (`music_return_text`). `with_check_back` is the
allowed-tools rule that keeps `schedule_task` available wherever a music tool is; a
`schedule_task` only that rule brings takes nothing but such a return
(`ToolContext.schedule_task_music_return_only`). `cancel_jobs_of_stopped_turn` is what the
loop calls when the person stops a turn (D48).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, Iterable, List, Optional

from ...managers.llama_server_supervisor import current_card_hold
from ...music import job_schemas as js
from ...process_registry import SPAWN_ORIGIN, get_registry
from .registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

MUSIC_TOOL_NAMES = frozenset({
    "music_capabilities", "music_propose_job", "music_check", "music_wait", "music_cancel_job",
    "music_list_takes", "music_list_library", "music_get_lyrics", "music_playlist_add",
})

# music_wait (D42): poll every 2 s; `timeout` 600 s by default, 900 s at most; the tool's own
# timeout is the longest wait plus 15 s.
POLL_INTERVAL_S = 2.0
WAIT_DEFAULT_S = 600
WAIT_MAX_S = 900
WAIT_MARGIN_S = 15
# How often a wait looks at the turn's Stop and Kill inside one poll interval.
STOP_CHECK_S = 0.1
# The whole turn's music_wait time (D51): a turn waits at most this long in all, then music_wait
# is refused for the rest of the turn.
TURN_WAIT_BUDGET_S = 900.0

# One service call from a tool thread; under every tool's own timeout.
SERVICE_CALL_TIMEOUT_S = 20.0
# The timeout of each tool that makes one or two service calls.
ONE_CALL_TOOL_TIMEOUT_S = 30
# music_propose_job waits for the person (the schedule approval's 60 s) and makes a few calls.
PROPOSE_TOOL_TIMEOUT_S = 100

# check_back's delay: the job's expected time plus Strata's cold start after it (about 23 s,
# ADR-052 section 3.5), rounded up. A job whose time is not known (acestep.cpp) counts this.
CHECK_BACK_COLD_START_S = 25
UNKNOWN_ETA_S = 180

LIBRARY_LIMIT_DEFAULT = 50
LIBRARY_LIMIT_MAX = 200

_FINISHED = frozenset({js.REVIEW}) | js.TERMINAL_STATES
_UNFINISHED = frozenset({js.QUEUED, js.RUNNING})


class _Unavailable(Exception):
    """The music service cannot be reached from this context."""


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------

def _not_owner(ctx: ToolContext, name: str) -> Optional[str]:
    """The second line (ADR-051 D43 as ADR-053 changed it): the registry's gate asks the owner
    first when someone else asked for the turn; this lets the call run only when the owner
    asked, or approved this very call, or the call is on its way to the tool's own card.
    Per call: an approval is never carried to the next call."""
    from ..owner_approval import may_run_owner_tool, turn_asker
    if may_run_owner_tool(ctx):
        return None
    return (f"⚠️ {name} runs on a request of {turn_asker(ctx).label()}, who is not the owner, "
            f"only after the owner approves it. Nothing was run.")


def _service_call(ctx: ToolContext, method: str, **kwargs: Any) -> Dict[str, Any]:
    """Run the CoreService command `method` on the service loop and return its answer."""
    service = getattr(ctx, "dpc_service", None)
    loop = getattr(ctx, "agent_event_loop", None)
    fn = getattr(service, method, None) if service is not None else None
    if fn is None or loop is None or loop.is_closed():
        raise _Unavailable("the music service is not available in this context")
    future = asyncio.run_coroutine_threadsafe(fn(**kwargs), loop)
    try:
        return future.result(timeout=SERVICE_CALL_TIMEOUT_S)
    except BaseException:
        future.cancel()
        raise


def _error_text(answer: Dict[str, Any], doing: str) -> Optional[str]:
    """The refusal text of a service answer, or None when it is ok."""
    if isinstance(answer, dict) and answer.get("status") == "ok":
        return None
    code = (answer or {}).get("code", "error") if isinstance(answer, dict) else "error"
    message = (answer or {}).get("message", "") if isinstance(answer, dict) else str(answer)
    return f"⚠️ {doing} failed: {code}: {message}"


def _agent_id(ctx: ToolContext) -> str:
    origin = SPAWN_ORIGIN.get()
    return (getattr(getattr(ctx, "agent_root", None), "name", "") or
            (origin.agent_id if origin is not None else "") or "")


def _turn_id() -> str:
    origin = SPAWN_ORIGIN.get()
    return (origin.turn_id if origin is not None else "") or ""


def _requested_by(ctx: ToolContext) -> Dict[str, Any]:
    """`requested_by` for this call: the agent, its chat and its turn (D46)."""
    origin = SPAWN_ORIGIN.get()
    who: Dict[str, Any] = {"kind": js.REQUESTER_AGENT, "agent_id": _agent_id(ctx)}
    conversation = getattr(ctx, "conversation_id", None) or (origin.conversation_id if origin else "")
    if conversation:
        who["conversation_id"] = conversation
    turn = _turn_id()
    if turn:
        who["turn_id"] = turn
    return who


def _ctx_dict(ctx: ToolContext, name: str) -> Dict[str, Any]:
    """A per-context record the music tools keep (the turn's wait spent, its proposals)."""
    value = getattr(ctx, name, None)
    if not isinstance(value, dict):
        value = {}
        setattr(ctx, name, value)
    return value


def _turn_stopped(ctx: ToolContext) -> bool:
    """Whether the person pressed Stop (or Kill) on this turn; the loop puts both events on
    the context. Reading an asyncio.Event's flag from a tool thread is safe. A Kill is also
    read from the process registry, which records the turn before it releases anything."""
    for name in ("turn_stop_event", "turn_kill_event"):
        event = getattr(ctx, name, None)
        try:
            if event is not None and event.is_set():
                return True
        except Exception:
            continue
    turn_id = _turn_id()
    if turn_id:
        try:
            return bool(get_registry().turn_was_killed(turn_id))
        except Exception:
            return False
    return False


def _local_alias_held() -> Optional[str]:
    """The local alias this turn stands on, or None. A hold on a cloud model stands on no alias
    (`alias` None), and a hold whose turn has ended is not live: neither makes a self-wait."""
    hold = current_card_hold()
    if hold is None or not getattr(hold, "live", False):
        return None
    alias = getattr(hold, "alias", None)
    return alias if isinstance(alias, str) and alias else None


def _eta_s(job: Dict[str, Any]) -> Optional[float]:
    try:
        return js.music_expected_s(job.get("params"))
    except Exception:
        return None


def check_back_delay_s(job: Dict[str, Any]) -> int:
    """When to come back to `job`: its expected time on the card plus the cold start after it."""
    eta = _eta_s(job)
    return int(round((eta if eta is not None else UNKNOWN_ETA_S) + CHECK_BACK_COLD_START_S))


def _job_lines(job: Dict[str, Any]) -> List[str]:
    """The job's state in words, with no clock in it (no elapsed seconds)."""
    lines = [f"job {job.get('id')}: {job.get('state')}"]
    reason = job.get("state_reason")
    if reason:
        lines.append(f"reason: {reason}")
    if job.get("state") == js.RUNNING:
        try:
            lines.append(f"progress: {int(round(float(job.get('progress') or 0) * 100))}%")
        except (TypeError, ValueError):
            pass
    card_wait = job.get("card_wait") or {}
    holders = card_wait.get("holders") if isinstance(card_wait, dict) else None
    if holders:
        named = ", ".join(f"{h.get('alias')}" + (f" ({h['engine']})" if h.get("engine") else "")
                          for h in holders if isinstance(h, dict))
        lines.append(f"waiting for the card, held by: {named}")
    if job.get("result_text"):
        lines.append(f"result: {job['result_text']}")
    return lines


def _take_line(take: Dict[str, Any]) -> str:
    parts = [f"take {take.get('take_id')}"]
    if take.get("variant") is not None:
        parts.append(f"variant {take['variant']}")
    if take.get("duration_s") is not None:
        parts.append(f"{float(take['duration_s']):.1f} s of audio")
    loud = take.get("loudness") or {}
    if isinstance(loud, dict) and loud.get("lufs_i") is not None:
        parts.append(f"{loud['lufs_i']} LUFS")
    if take.get("library_track_id"):
        parts.append(f"library track {take['library_track_id']}")
    parts.append(f"decision {take.get('state')}")
    path = take.get("path")
    parts.append(f"file {path}" if path and take.get("file_exists") else "file not present")
    return ", ".join(parts)


def _takes_lines(ctx: ToolContext, take_ids: Iterable[str]) -> List[str]:
    """Each take by its id, through core (D44): its own file and metadata, nothing of the request."""
    lines = []
    for take_id in take_ids:
        answer = _service_call(ctx, "get_music_take", take_id=take_id)
        refused = _error_text(answer, f"reading take {take_id}")
        lines.append(refused if refused else _take_line(answer["take"]))
    return lines


def _job_report(ctx: ToolContext, job: Dict[str, Any]) -> str:
    lines = _job_lines(job)
    if job.get("state") in _FINISHED and job.get("take_ids"):
        lines.append("takes:")
        lines.extend(_takes_lines(ctx, job["take_ids"]))
    return "\n".join(lines)


def _get_job(ctx: ToolContext, job_id: str) -> tuple:
    """(job, None) or (None, refusal text)."""
    answer = _service_call(ctx, "get_music_job", job_id=job_id)
    refused = _error_text(answer, f"reading job {job_id}")
    return (None, refused) if refused else (answer["job"], None)


def _guarded(name: str):
    """Owner check, then the body; a service that cannot be reached is said in words."""
    def wrap(fn):
        def run(ctx: ToolContext, *args: Any, **kwargs: Any) -> str:
            refused = _not_owner(ctx, name)
            if refused:
                return refused
            try:
                return fn(ctx, *args, **kwargs)
            except _Unavailable as exc:
                return f"⚠️ {name}: {exc}."
            except TimeoutError:
                return f"⚠️ {name}: the music service did not answer within {int(SERVICE_CALL_TIMEOUT_S)} s."
        run.__name__ = fn.__name__
        run.__doc__ = fn.__doc__
        run.__wrapped__ = fn  # the registry's alias resolver reads the real signature
        return run
    return wrap


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@_guarded("music_capabilities")
def music_capabilities(ctx: ToolContext) -> str:
    """What the music service can do now: engines, their state and limits, the job params."""
    answer = _service_call(ctx, "get_music_capabilities")
    refused = _error_text(answer, "reading the music capabilities")
    if refused:
        return refused
    report = answer.get("capabilities") or {}
    limits = (report.get("limits") or {}).get("generate_from_text")
    view = {
        "default_engine": report.get("default_engine"),
        "engines": report.get("engines"),
        "generate_from_text": (report.get("job_types") or {}).get(js.GENERATE_FROM_TEXT),
        "params_limits": limits,
        "engine_choice_rules": report.get("engine_choice_rules"),
    }
    return json.dumps(view, ensure_ascii=False, default=str)


# The style as the approval card quotes it; the card's whole line stays short enough to read.
APPROVAL_STYLE_CHARS = 80


def _eta_is_upper_bound(params: Dict[str, Any]) -> bool:
    """Whether the job's expected time is a cap-based estimate, read by the person as "up to":
    YuE2's is (`music_expected_s` never exceeds the duration cap)."""
    try:
        return js.engine_of(params) == js.ENGINE_YUE2
    except Exception:
        return False


def _approval_about(job_type: str, params: Dict[str, Any]) -> str:
    """What the approval card asks (D47), in the person's terms: run one job on the GPU, its type,
    style, number of takes, sung or instrumental, engine, and about how long it takes on the card."""
    style = " ".join(str(params.get("style") or "").split())
    if len(style) > APPROVAL_STYLE_CHARS:
        style = style[:APPROVAL_STYLE_CHARS - 1].rstrip() + "…"
    variants = params.get("variants") or js.VARIANTS_DEFAULT
    takes = f"{variants} take" + ("" if variants == 1 else "s")
    sung = "instrumental" if not str(params.get("lyrics") or "").strip() else "with lyrics"
    duration = _duration_s(params)
    long = f", {duration:g} s long" if duration else ""
    engine = params.get("engine") or "default engine"
    try:
        eta = js.music_expected_s(params)
    except Exception:
        eta = None
    # YuE2's time is a cap-based estimate (the song cannot outrun its duration cap), so the card
    # reads it as an upper bound; no other engine has an estimate (follow-up 13, Zcode P3).
    on_card = (f"up to {int(round(eta))} s on the GPU" if eta and _eta_is_upper_bound(params)
               else f"about {int(round(eta))} s on the GPU" if eta else "time on the GPU not known")
    return (f"Run a music job on the GPU: {job_type}, \"{style or '(no style)'}\", {takes}, {sung}{long}, "
            f"{engine}, {on_card}")


# The lyrics as the approval card quotes them: their start, so the person sees what is sung.
APPROVAL_LYRICS_CHARS = 120


def _duration_s(params: Dict[str, Any]) -> Optional[float]:
    try:
        value = params.get("duration_s")
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _approval_parts(job_type: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """The same request in parts, for the interface's card (`music` on schedule_approval_request):
    type, style, variants, instrumental and its twin has_lyrics, engine, duration_s, eta_seconds,
    eta_is_upper_bound (YuE2: the time is a cap-based estimate, "up to"), and lyrics_excerpt (the lyrics' first APPROVAL_LYRICS_CHARS characters, only when there are
    lyrics)."""
    try:
        eta = js.music_expected_s(params)
    except Exception:
        eta = None
    lyrics = " ".join(str(params.get("lyrics") or "").split())
    parts = {
        "type": job_type,
        "style": " ".join(str(params.get("style") or "").split()),
        "variants": params.get("variants") or js.VARIANTS_DEFAULT,
        "instrumental": not lyrics,
        "has_lyrics": bool(lyrics),
        "engine": params.get("engine") or None,
        "duration_s": _duration_s(params),
        "eta_seconds": int(round(eta)) if eta else None,
        # YuE2's time is an upper bound (cap-based), the card says "up to" (follow-up 13).
        "eta_is_upper_bound": bool(eta) and _eta_is_upper_bound(params),
    }
    if lyrics:
        parts["lyrics_excerpt"] = (lyrics if len(lyrics) <= APPROVAL_LYRICS_CHARS
                                   else lyrics[:APPROVAL_LYRICS_CHARS - 1].rstrip() + "…")
    return parts


def _turn_jobs(jobs: List[Dict[str, Any]], agent_id: str, turn_id: str) -> List[Dict[str, Any]]:
    out = []
    for job in jobs:
        rb = js.requested_by_of(job)
        # An `unsupported` job counts as well (follow-up 12): core's turn cap counts it too.
        if (rb.get("kind") == js.REQUESTER_AGENT and rb.get("agent_id") == agent_id
                and rb.get("turn_id") == turn_id):
            out.append(job)
    return out


def _active_jobs(jobs: List[Dict[str, Any]], agent_id: str) -> List[Dict[str, Any]]:
    return [j for j in jobs if j.get("state") in _UNFINISHED
            and js.requested_by_of(j).get("kind") == js.REQUESTER_AGENT
            and js.requested_by_of(j).get("agent_id") == agent_id]


def _limit_of(ctx: ToolContext, method: str) -> Optional[int]:
    fn = getattr(getattr(ctx, "dpc_service", None), method, None)
    try:
        return int(fn()) if fn is not None else None
    except Exception:
        return None


def _after_propose_hint(job: Dict[str, Any]) -> str:
    delay = check_back_delay_s(job)
    alias = _local_alias_held()
    if job.get("state") != js.QUEUED:
        return ""
    if alias:
        return (f"This turn runs on the local model '{alias}' and holds the card, so the job starts "
                f"after this turn ends. Do not wait for it now: schedule a check_back "
                f"(schedule_task, task_type check_back, task_data with \"music_job_id\": "
                f"\"{job.get('id')}\") in about {delay} s.")
    return (f"Use music_wait to wait for it, or schedule a check_back (task_data with "
            f"\"music_job_id\": \"{job.get('id')}\") in about {delay} s.")


@_guarded("music_propose_job")
def music_propose_job(ctx: ToolContext, type: str = js.GENERATE_FROM_TEXT, params: Any = None,
                      idempotency_key: Optional[str] = None) -> str:
    """Propose one music job, after the person approves it."""
    if type != js.GENERATE_FROM_TEXT:
        return f"⚠️ music_propose_job: type must be '{js.GENERATE_FROM_TEXT}'; nothing was proposed."
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except ValueError:
            return "⚠️ music_propose_job: params must be an object; nothing was proposed."
    if not isinstance(params, dict):
        return "⚠️ music_propose_job: params must be an object; nothing was proposed."
    who = _requested_by(ctx)
    turn_id = who.get("turn_id")
    if not who.get("agent_id") or not turn_id:
        return "⚠️ music_propose_job runs only inside an agent turn; nothing was proposed."

    # A repeat in this turn, or a cap already reached, is answered before anyone is asked:
    # a question whose "yes" cannot make a job is not put to the person (D45, D47).
    listed = _service_call(ctx, "list_music_jobs")
    refused = _error_text(listed, "reading the music jobs")
    if refused:
        return refused
    jobs = listed.get("jobs") or []
    if idempotency_key:
        same = [j for j in jobs if j.get("idempotency_key") == idempotency_key
                and js.requested_by_of(j).get("agent_id") == who["agent_id"]]
        if same:
            return ("This idempotency_key already names a job; no new job was made and nothing was asked.\n"
                    + "\n".join(_job_lines(same[0])))
    in_turn = _turn_jobs(jobs, who["agent_id"], turn_id)
    turn_max = _limit_of(ctx, "_music_turn_jobs_max")
    if turn_max is not None and len(in_turn) >= turn_max:
        latest = in_turn[-1]
        return (f"This turn already proposed {len(in_turn)} music job(s), at most {turn_max} per turn; "
                f"no new job was made and nothing was asked. The turn's job:\n"
                + "\n".join(_job_lines(latest)) + "\n" + _after_propose_hint(latest)).rstrip()
    queue_max = _limit_of(ctx, "_music_agent_queue_max")
    active = _active_jobs(jobs, who["agent_id"])
    if queue_max is not None and len(active) >= queue_max:
        return (f"⚠️ music_propose_job refused: requester_queue_full: {len(active)} of this agent's music "
                f"job(s) are queued or running, at most {queue_max} at once. Nothing was asked or proposed.")

    # Everything core would refuse, and a job core would make `unsupported`, is answered before
    # anyone is asked (follow-up 12, Zcode #68 P2): core's own checks, with no job made.
    checked = _service_call(ctx, "check_music_job", type=type, params=params,
                            idempotency_key=idempotency_key or None, requested_by=who)
    refused = _error_text(checked, "music_propose_job")
    if refused:
        return refused + " Nothing was asked or proposed."
    if checked.get("repeat"):
        return ("This request repeats a job already made; no new job was made and nothing was asked.\n"
                + "\n".join(_job_lines(checked["repeat"])))
    final = checked.get("params") if isinstance(checked.get("params"), dict) else params

    # The person's consent first, through schedule_task's machinery (D47). Stop while the card
    # is up withdraws it: a "yes" after Stop makes no job (follow-up 12, Zcode #68 P3).
    from .core import _await_schedule_approval
    from ..owner_approval import GPU_CARD_COST_LINE
    approved, reason = _await_schedule_approval(
        ctx, task_type="music_job", when="now", about=_approval_about(type, final),
        music=_approval_parts(type, final), void_on_stop=True,
        card={"cost": GPU_CARD_COST_LINE})  # ADR-053 2.6: the card states the cost
    if not approved:
        return f"⚠️ The music job was not proposed: {reason}. No job was made."
    # Stop or Kill pressed after the "yes" (while the answer was on its way, or in the moment
    # since) makes no job either (follow-up 13, Zcode P3).
    if _turn_stopped(ctx):
        return "⚠️ The music job was not proposed: the turn was stopped. No job was made."

    # What the loop's Stop reads (D48), written before the job exists: a Stop landing during
    # the call below still finds the job it made.
    _ctx_dict(ctx, "_music_turns")[turn_id] = who["agent_id"]
    answer = _service_call(ctx, "propose_music_job", type=type, params=params,
                           idempotency_key=idempotency_key or None, requested_by=who)
    refused = _error_text(answer, "music_propose_job")
    if refused:
        return refused + " No job was made."
    job = answer["job"]
    if _turn_stopped(ctx):
        # The Stop came during the call itself: take the job back at once, not only when the
        # loop ends the turn (this tool may outlive the loop's own cancel).
        try:
            _service_call(ctx, "cancel_music_job", job_id=job.get("id"),
                          reason="the turn was stopped while the job was being made", requested_by=who)
        except Exception:
            log.warning("music_propose_job: cancelling job %s of a stopped turn failed", job.get("id"),
                        exc_info=True)
        return ("⚠️ The turn was stopped while the music job was being made; the job was cancelled "
                f"({job.get('id')}).")
    lines = ["Proposed (approved by the person).", *_job_lines(job)]
    eta = _eta_s(job)
    if eta is not None:
        bound = "up to" if _eta_is_upper_bound(job.get("params") or {}) else "about"
        lines.append(f"expected time on the card: {bound} {int(round(eta))} s once it starts")
    hint = _after_propose_hint(job)
    if hint:
        lines.append(hint)
    return "\n".join(lines)


@_guarded("music_check")
def music_check(ctx: ToolContext, job_id: str) -> str:
    """One look at a job: its state, what holds the card it waits for, its takes when done."""
    job, refused = _get_job(ctx, job_id)
    return refused if refused else _job_report(ctx, job)


def _waits_behind_this_turn(job: Dict[str, Any], alias: str) -> bool:
    """Whether the job's `card_wait` names this turn's local alias among its holders: a running job
    whose engine child ended waits for the card again (ADR-052 S4) behind the holder of `alias`,
    which this turn holds until it ends. The runner names holders by alias (`holders_in_the_way`)."""
    if job.get("state") not in _UNFINISHED:
        return False
    card_wait = job.get("card_wait")
    holders = card_wait.get("holders") if isinstance(card_wait, dict) else None
    return any(isinstance(h, dict) and h.get("alias") == alias for h in holders or ())


def _self_wait_answer(job: Dict[str, Any], alias: Optional[str]) -> Optional[str]:
    """The self-wait rule (D51, ADR-052 3.5): with a live hold on the local alias `alias`, a job that
    cannot move before this turn ends is answered at once instead of waited for: a queued job, or
    a job of any unfinished state whose `card_wait` names this turn's alias. Else None."""
    if not alias:
        return None
    back = (f"Use check_back in about {check_back_delay_s(job)} s (schedule_task, task_type check_back, "
            f"task_data with \"music_job_id\": \"{job.get('id')}\").")
    if _waits_behind_this_turn(job, alias):
        return (f"job {job.get('id')}: {job.get('state')}; it waits for the card behind this turn (this turn "
                f"runs on the local model '{alias}' and holds the card), so check back after this turn "
                f"ends. {back}")
    if job.get("state") == js.QUEUED:
        return (f"job {job.get('id')}: queued; it starts after this turn ends (this turn runs on the local "
                f"model '{alias}' and holds the card). {back}")
    return None


def _sleep_watching(ctx: ToolContext, seconds: float) -> bool:
    """Sleep up to `seconds`, looking at Stop every STOP_CHECK_S; True when stopped."""
    end = time.monotonic() + max(0.0, seconds)
    while True:
        if _turn_stopped(ctx):
            return True
        left = end - time.monotonic()
        if left <= 0:
            return False
        time.sleep(min(STOP_CHECK_S, left))


@_guarded("music_wait")
def music_wait(ctx: ToolContext, job_id: str, timeout: int = WAIT_DEFAULT_S) -> str:
    """Wait for a job to finish, by the self-wait rule and within the turn's wait budget."""
    try:
        timeout_s = float(timeout)
    except (TypeError, ValueError):
        timeout_s = float(WAIT_DEFAULT_S)
    timeout_s = max(1.0, min(timeout_s, float(WAIT_MAX_S)))
    spent = _ctx_dict(ctx, "_music_wait_spent")
    turn = _turn_id()
    left = TURN_WAIT_BUDGET_S - spent.get(turn, 0.0)
    if left <= 0:
        return (f"⚠️ music_wait refused: this turn has used its wait budget ({int(TURN_WAIT_BUDGET_S)} s of "
                f"music_wait in all). Use music_check for the state, or schedule a check_back.")

    job, refused = _get_job(ctx, job_id)
    if refused:
        return refused
    # The self-wait rule (D51, ADR-052 3.5): a live hold on a local alias AND a queued job, or a
    # job whose card_wait names that alias, whatever its state.
    answer = _self_wait_answer(job, _local_alias_held())
    if answer:
        return answer

    limit = min(timeout_s, left)
    started = time.monotonic()
    try:
        while True:
            if job.get("state") in _FINISHED:
                return _job_report(ctx, job)
            if _turn_stopped(ctx):
                return "\n".join(_job_lines(job) + ["The wait ended: the turn was stopped."])
            remaining = limit - (time.monotonic() - started)
            if remaining <= 0:
                break
            if _sleep_watching(ctx, min(POLL_INTERVAL_S, remaining)):
                return "\n".join(_job_lines(job) + ["The wait ended: the turn was stopped."])
            job, refused = _get_job(ctx, job_id)
            if refused:
                return refused
            answer = _self_wait_answer(job, _local_alias_held())
            if answer:
                return answer
    finally:
        spent[turn] = spent.get(turn, 0.0) + (time.monotonic() - started)
    lines = _job_lines(job)
    if TURN_WAIT_BUDGET_S - spent.get(turn, 0.0) <= 0:
        lines.append("Still not finished, and this turn's wait budget is now spent: music_wait is refused "
                     f"for the rest of the turn. Schedule a check_back in about {check_back_delay_s(job)} s.")
    else:
        lines.append(f"Still not finished at the end of this wait. Call music_wait again, or schedule a "
                     f"check_back in about {check_back_delay_s(job)} s.")
    return "\n".join(lines)


@_guarded("music_cancel_job")
def music_cancel_job(ctx: ToolContext, job_id: str, reason: Optional[str] = None) -> str:
    """Cancel a job this agent proposed; another's is refused (D46)."""
    answer = _service_call(ctx, "cancel_music_job", job_id=job_id, reason=reason or None,
                           requested_by=_requested_by(ctx))
    refused = _error_text(answer, f"cancelling job {job_id}")
    if refused:
        return refused
    return "\n".join(_job_lines(answer["job"]))


@_guarded("music_list_takes")
def music_list_takes(ctx: ToolContext, job_id: str) -> str:
    """A job's takes by take_id, each with its own file and metadata (D44)."""
    answer = _service_call(ctx, "list_music_takes", job_id=job_id)
    refused = _error_text(answer, f"listing the takes of job {job_id}")
    if refused:
        return refused
    ids = [t.get("id") for t in answer.get("takes") or [] if t.get("id")]
    if not ids:
        return f"job {job_id} has no takes."
    return "\n".join(_takes_lines(ctx, ids))


@_guarded("music_list_library")
def music_list_library(ctx: ToolContext, query: Optional[str] = None,
                       limit: int = LIBRARY_LIMIT_DEFAULT) -> str:
    """The library's tracks (id, title, artist, album, length, origin) and the person's playlists."""
    try:
        limit = max(1, min(int(limit), LIBRARY_LIMIT_MAX))
    except (TypeError, ValueError):
        limit = LIBRARY_LIMIT_DEFAULT
    answer = _service_call(ctx, "list_music_library")
    refused = _error_text(answer, "listing the music library")
    if refused:
        return refused
    needle = (query or "").strip().lower()
    tracks = []
    for e in answer.get("entries") or []:
        if e.get("hidden"):
            continue
        row = {k: e.get(k) for k in ("id", "title", "artist", "album", "duration_s", "origin", "missing")
               if e.get(k) is not None}
        if needle and needle not in " ".join(str(row.get(k, "")) for k in ("title", "artist", "album")).lower():
            continue
        tracks.append(row)
    shown = tracks[:limit]
    playlists = []
    listed = _service_call(ctx, "list_music_playlists")
    if _error_text(listed, "") is None:
        playlists = [{"id": p.get("id"), "name": p.get("name"), "tracks": len(p.get("track_ids") or [])}
                     for p in listed.get("playlists") or [] if p.get("kind") == "user"]
    return json.dumps({"tracks": shown, "tracks_total": len(tracks), "playlists": playlists},
                      ensure_ascii=False, default=str)


@_guarded("music_get_lyrics")
def music_get_lyrics(ctx: ToolContext, track_id: str) -> str:
    """A library track's stored or local lyrics (no LRCLIB request, D50)."""
    answer = _service_call(ctx, "get_music_lyrics", track_id=track_id)
    refused = _error_text(answer, f"reading the lyrics of {track_id}")
    if refused:
        return refused
    lyrics = answer.get("lyrics")
    if not lyrics:
        return f"track {track_id} has no lyrics."
    lines = [str(line.get("text", "")) for line in lyrics.get("lines") or []]
    head = f"lyrics of {track_id} (source {lyrics.get('source')}"
    if lyrics.get("language"):
        head += f", language {lyrics['language']}"
    return head + "):\n" + "\n".join(lines)


@_guarded("music_playlist_add")
def music_playlist_add(ctx: ToolContext, playlist_id: str, track_ids: Optional[List[str]] = None,
                       take_id: Optional[str] = None) -> str:
    """Add library tracks, or a take's library track, to the end of one of the person's playlists."""
    wanted = [t for t in (track_ids or []) if isinstance(t, str) and t]
    if take_id:
        answer = _service_call(ctx, "get_music_take", take_id=take_id)
        refused = _error_text(answer, f"reading take {take_id}")
        if refused:
            return refused
        lib_id = answer["take"].get("library_track_id")
        if not lib_id:
            return f"⚠️ take {take_id} is not in the library yet; nothing was added."
        wanted.append(lib_id)
    if not wanted:
        return "⚠️ music_playlist_add: give track_ids or take_id; nothing was added."
    listed = _service_call(ctx, "list_music_playlists")
    refused = _error_text(listed, "reading the playlists")
    if refused:
        return refused
    playlist = next((p for p in listed.get("playlists") or [] if p.get("id") == playlist_id), None)
    if playlist is None or playlist.get("kind") != "user":
        return f"⚠️ {playlist_id} is not one of the person's playlists; nothing was added."
    current = list(playlist.get("track_ids") or [])
    added = [t for t in dict.fromkeys(wanted) if t not in current]
    if not added:
        return f"playlist {playlist_id} already has those tracks."
    answer = _service_call(ctx, "update_music_playlist", playlist_id=playlist_id, track_ids=current + added)
    refused = _error_text(answer, f"adding to playlist {playlist_id}")
    if refused:
        return refused
    return f"added {len(added)} track(s) to playlist '{answer['playlist'].get('name')}' ({playlist_id})."


# ---------------------------------------------------------------------------
# For schedule_task, the allowed tools and the loop's Stop
# ---------------------------------------------------------------------------

# A job id as it appears inside a sentence (job_schemas.JOB_ID_RE, unanchored).
_JOB_ID_IN_TEXT_RE = re.compile(r"(?<![0-9A-Za-z_])job_[0-9a-f]{16}(?![0-9A-Za-z_])")


def _own_unfinished_job(ctx: ToolContext, job_id: str) -> Optional[Dict[str, Any]]:
    """The job `job_id` when this agent proposed it and it is still queued or running, else
    None. Any doubt is None."""
    try:
        job, refused = _get_job(ctx, job_id)
    except Exception:
        log.debug("check_back: music job %s could not be read", job_id, exc_info=True)
        return None
    if refused or job is None:
        return None
    rb = js.requested_by_of(job)
    agent_id = _agent_id(ctx)
    if (bool(agent_id) and rb.get("kind") == js.REQUESTER_AGENT and rb.get("agent_id") == agent_id
            and job.get("state") in _UNFINISHED):
        return job
    return None


def own_music_job_to_return_to(ctx: ToolContext, data: Any) -> Optional[Dict[str, Any]]:
    """The music job a check_back's task data names when this agent proposed it and it is
    still queued or running (D49), else None. Any doubt is None.

    The job is named by `music_job_id`. Without that field, a job id written in the task's
    text fields counts when exactly one such id there is a job of this agent's that is still
    queued or running (an agent wrote «check job_… at 04:26» and core did not know its own
    job, so it asked for an approval). The own-and-unfinished check is the same either way."""
    if not isinstance(data, dict):
        return None
    if "music_job_id" in data:
        job_id = data.get("music_job_id")
        if not isinstance(job_id, str) or not job_id.strip():
            return None
        return _own_unfinished_job(ctx, job_id)
    named: List[str] = []
    for value in data.values():
        if isinstance(value, str):
            named.extend(m for m in _JOB_ID_IN_TEXT_RE.findall(value) if m not in named)
    found = [job for job in (_own_unfinished_job(ctx, job_id) for job_id in named) if job]
    return found[0] if len(found) == 1 else None


def is_return_to_own_music_job(ctx: ToolContext, data: Any) -> bool:
    """Whether a check_back's task data names a music job this agent proposed that is still
    queued or running (D49): such a return asks no approval. Anything else, or any doubt,
    is False, and the ordinary approval applies."""
    return own_music_job_to_return_to(ctx, data) is not None


def music_return_text(job: Dict[str, Any]) -> str:
    """The wake text of an approval-free return to `job` (D49): core's words, never the agent's.
    It names the job and the two tools that read it."""
    job_id = job.get("id")
    return (f"Check back on music job {job_id}, which you proposed: call music_check with job_id "
            f"\"{job_id}\" for its state, and music_list_takes with that job_id once it is done.")


# What a music-return-only schedule_task answers to anything but a return to the agent's own
# unfinished music job (D49, Zcode #67 P1). It guides the model to the one call it may make.
SCHEDULE_RETURN_ONLY_REFUSAL = (
    "⚠️ schedule_task is not enabled for this agent; only a return to your own music job is: "
    "task_type \"check_back\" with task_data {\"music_job_id\": \"<a job you proposed that is still "
    "queued or running>\"}. Core sets its time and its text. Nothing was scheduled.")


def with_check_back(allowed: Optional[set]) -> Optional[set]:
    """The allowed tools with `schedule_task` added wherever a music tool is allowed (D49):
    check_back is the only way back to a job. None (no gate) stays None."""
    if allowed is None or not (allowed & MUSIC_TOOL_NAMES):
        return allowed
    return allowed | {"schedule_task"}


_stop_tasks: set = set()


def cancel_jobs_of_stopped_turn(ctx: Optional[ToolContext], turn_id: str) -> Optional[asyncio.Task]:
    """The person stopped turn `turn_id` (D48): cancel the jobs this context proposed in it that
    are still queued. Called on the loop thread at the turn's end; returns the task, or None when
    this turn proposed nothing (the store is not opened for a turn that never used music)."""
    turns = getattr(ctx, "_music_turns", None) if ctx is not None else None
    if not turn_id or not isinstance(turns, dict) or turn_id not in turns:
        return None
    service = getattr(ctx, "dpc_service", None)
    cancel = getattr(service, "cancel_music_jobs_of_turn", None)
    if cancel is None:
        return None
    agent_id = turns[turn_id]

    async def run() -> None:
        try:
            answer = await cancel(turn_id=turn_id, agent_id=agent_id)
            done = (answer or {}).get("cancelled") or []
            if done:
                log.info("Stop of turn %s: cancelled %d queued music job(s): %s", turn_id, len(done),
                         ", ".join(j.get("id", "?") for j in done))
            elif (answer or {}).get("status") != "ok":
                log.warning("Stop of turn %s: cancelling its queued music jobs failed: %s", turn_id, answer)
        except Exception:
            log.warning("Stop of turn %s: cancelling its queued music jobs failed", turn_id, exc_info=True)

    try:
        task = asyncio.get_running_loop().create_task(run())
    except RuntimeError:
        return None
    _stop_tasks.add(task)
    task.add_done_callback(_stop_tasks.discard)
    return task


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def _entry(name: str, description: str, properties: Dict[str, Any], required: List[str], handler,
           timeout_sec: int = ONE_CALL_TOOL_TIMEOUT_S) -> ToolEntry:
    return ToolEntry(
        name=name,
        schema={"name": name, "description": description,
                "parameters": {"type": "object", "properties": properties, "required": required}},
        handler=handler,
        is_code_tool=False,
        timeout_sec=timeout_sec,
        is_core=False,
        default_enabled=False,
        owner_approval_for_others=True,  # ADR-053: was owner_only (ADR-051 D43)
    )


_JOB_ID = {"type": "string", "description": "The job id music_propose_job returned."}

# The music-return-only mode (ADR-051 D49): said in the music tools' own descriptions, so a model
# that was given music tools and not schedule_task learns the one call that is open to it.
CHECK_BACK_ONLY_LINE = (
    " If schedule_task is not granted to you, the only schedule_task call open to you is a return "
    "to your own music job that is still queued or running: task_type \"check_back\" with task_data "
    "{\"music_job_id\": ...}; it asks no approval, and core sets its delay and its text."
)


def get_tools() -> List[ToolEntry]:
    return [
        _entry("music_capabilities",
               "What DPC's music service can do now: the engines, whether each is installed, their "
               "limits, and the params a generate_from_text job takes.",
               {}, [], music_capabilities),
        _entry("music_propose_job",
               "Propose ONE music generation job (type generate_from_text). The person is asked to "
               "approve it first; refused, no job is made. One job per turn: a repeat in the same turn "
               "returns the turn's job. On a local model the job starts only after this turn ends: do "
               "not wait for it, schedule a check_back with task_data {\"music_job_id\": ...}." + CHECK_BACK_ONLY_LINE,
               {"type": {"type": "string", "enum": [js.GENERATE_FROM_TEXT],
                         "description": "Job type; only generate_from_text."},
                "params": {"type": "object",
                           "description": "style (text, required), lyrics (text with [verse]/[chorus] "
                                          "tags; null for an instrumental), variants (1-4), duration_s, "
                                          "engine (see music_capabilities), seed. Params core would "
                                          "refuse are refused before the person is asked."},
                "idempotency_key": {"type": "string",
                                    "description": "Optional; the default comes from the turn."}},
               ["params"], music_propose_job, timeout_sec=PROPOSE_TOOL_TIMEOUT_S),
        _entry("music_check",
               "One look at a music job: its state, what holds the GPU it waits for, and its takes "
               "(take_id, file) when it is done. To come back to a job later, use check_back." + CHECK_BACK_ONLY_LINE,
               {"job_id": _JOB_ID}, ["job_id"], music_check),
        _entry("music_wait",
               "Wait for a music job to finish (polls every 2 s). Returns the takes when done. If this "
               "turn holds the GPU on a local model and the job is queued, it answers at once: the job "
               "starts after this turn; use check_back. A turn has 900 s of waiting in all." + CHECK_BACK_ONLY_LINE,
               {"job_id": _JOB_ID,
                "timeout": {"type": "integer",
                            "description": f"Seconds to wait (default {WAIT_DEFAULT_S}, at most {WAIT_MAX_S})."}},
               ["job_id"], music_wait, timeout_sec=WAIT_MAX_S + WAIT_MARGIN_S),
        _entry("music_cancel_job",
               "Cancel a music job this agent proposed. Another's job is refused. A running job's "
               "engine stays on the GPU until its idle stop.",
               {"job_id": _JOB_ID, "reason": {"type": "string", "description": "Optional reason."}},
               ["job_id"], music_cancel_job),
        _entry("music_list_takes",
               "A music job's takes: take_id, variant, length, loudness, library track and the take's file.",
               {"job_id": _JOB_ID}, ["job_id"], music_list_takes),
        _entry("music_list_library",
               "The music library's tracks (id, title, artist, album, length, origin) and the person's "
               "playlists (id, name). Hidden tracks are left out.",
               {"query": {"type": "string", "description": "Optional text to match in title, artist or album."},
                "limit": {"type": "integer",
                          "description": f"At most this many tracks (default {LIBRARY_LIMIT_DEFAULT}, "
                                         f"at most {LIBRARY_LIMIT_MAX})."}},
               [], music_list_library),
        _entry("music_get_lyrics",
               "A library track's lyrics, from what DPC has stored or found locally. Never asks the internet.",
               {"track_id": {"type": "string", "description": "A library track id."}},
               ["track_id"], music_get_lyrics),
        _entry("music_playlist_add",
               "Add library tracks (track_ids) or a take (take_id, once it is in the library) to the end "
               "of one of the person's playlists (ids from music_list_library).",
               {"playlist_id": {"type": "string", "description": "The playlist id."},
                "track_ids": {"type": "array", "items": {"type": "string"},
                              "description": "Library track ids to add."},
                "take_id": {"type": "string", "description": "A take whose library track to add."}},
               ["playlist_id"], music_playlist_add),
    ]


# ADR-053: the music tools ask the owner when someone else asked for the turn (they were
# owner_only, ADR-051 D43). music_propose_job asks on its own card, which names the asker
# and states the cost; its budget already holds the card's 60 s (D-b, D-c).
def _music_propose_approval(ctx: ToolContext, args: Dict[str, Any]):
    from ..owner_approval import ToolAsks
    return ToolAsks()


_get_music_tools_unflagged = get_tools


def get_tools() -> List[ToolEntry]:  # noqa: F811
    from .registry import mark_owner_approval
    checks: Dict[str, Any] = {name: None for name in MUSIC_TOOL_NAMES}
    checks["music_propose_job"] = _music_propose_approval
    return mark_owner_approval(_get_music_tools_unflagged(), checks,
                               budget_holds_approval=("music_propose_job",))
