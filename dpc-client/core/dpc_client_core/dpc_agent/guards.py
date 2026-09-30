"""GuardMiddleware implementations for the agent loop (ADR-007).

Five guards that used to live as inline if/elif blocks in
``loop.py::_process_agent_loop``. Each one owns its own state and
exposes a ``stop_message()`` that the loop reads via
:attr:`HookRegistry.last_triggered` after :meth:`HookRegistry.fire`.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from .hooks import GuardMiddleware, HookAction, HookContext

log = logging.getLogger(__name__)


class RoundLimitGuard(GuardMiddleware):
    """Stop the loop after a fixed number of LLM rounds.

    Uses strict ``>``: round indices are 1-based counts of rounds
    executed, so ``max_rounds`` itself is the last allowed round.
    """

    def __init__(self, max_rounds: int = 200) -> None:
        self._max_rounds = max_rounds

    async def between_rounds(self, ctx: HookContext) -> Optional[HookAction]:
        if ctx.round_idx > self._max_rounds:
            log.warning(
                "RoundLimitGuard: round_idx=%d exceeded max_rounds=%d",
                ctx.round_idx,
                self._max_rounds,
            )
            return HookAction.STOP_LOOP
        return None

    def stop_message(self) -> str:
        return (
            f"[ROUND_LIMIT] Task exceeded MAX_ROUNDS ({self._max_rounds}). "
            "Consider breaking into smaller tasks."
        )


class ToolLimitGuard(GuardMiddleware):
    """Stop if the LLM emits too many tool calls in a single turn.

    A burst this large in one response means the model has fanned out
    and will not converge on a text answer.
    """

    def __init__(self, max_per_turn: int = 25) -> None:
        self._max_per_turn = max_per_turn
        self._last_count = 0

    async def after_llm_call(self, ctx: HookContext) -> Optional[HookAction]:
        self._last_count = ctx.tool_calls_this_turn
        if ctx.tool_calls_this_turn > self._max_per_turn:
            log.warning(
                "ToolLimitGuard: %d tool calls in one turn exceeded max=%d",
                ctx.tool_calls_this_turn,
                self._max_per_turn,
            )
            return HookAction.STOP_LOOP
        return None

    def stop_message(self) -> str:
        return (
            f"[TOOL_LIMIT] You generated {self._last_count} tool calls in a "
            f"single turn, which exceeds the limit of {self._max_per_turn}. "
            "Stop calling tools. Summarise what you know and give your "
            "final answer now."
        )


# Tools whose result counts as progress when the CALL is new to this task:
# paging a document (read_document pages, read_file offset) or visiting a URL
# nobody visited yet. The key is the tool name plus the args that identify
# the piece read; a repeat of the same key is not new ground.
#
# Read-only exploration tools (search, listing, repository history, session
# archive) are in the set too: each call with new args reads something new, and
# a repeat does not count. run_shell is OUT on purpose. A shell loop is the very
# spiral this guard exists for, and command strings do not normalise reliably
# (one command spelled two ways is two "new" calls), so keying on them would
# turn every rephrased retry into progress.
_NEW_CALL_PROGRESS_TOOLS = frozenset({
    "read_document", "read_file", "extended_path_read",
    "browse_page", "browser_navigate", "fetch_json",
    "search_in_file", "search_files", "list_dir",
    "git_log", "git_diff", "git_show",
    "read_session_detail", "search_session_archives",
})
# A tool whose answer is JSON that is not an error string even when nothing was
# read: read_document with every page failed still returns its envelope, with
# the per-page failures inside it. For it, "non-error output" is not enough;
# progress needs this named integer field to be above zero. The guard is coupled
# to that one field of read_document (`pages_with_text`, tools/document.py). An
# unparseable answer or a missing field is NOT progress (fails closed).
_JSON_PROGRESS_FIELD = {"read_document": "pages_with_text"}
# Tools that read live state with no identifying args: progress is a result
# that differs from that tool's previous one (the LoopGuard idea).
_NEW_OUTPUT_PROGRESS_TOOLS = frozenset({"browser_snapshot", "browser_extract"})
# For the URL tools the URL is the identity; size presets and the like are not.
_URL_KEYED_TOOLS = frozenset({"browse_page", "browser_navigate"})
# A refusal or error result starts with a warning sign or a cross mark.
_FAILURE_PREFIXES = ("⚠", "❌")


def _call_key(name: str, raw_args) -> str:
    """Identity of a call for progress purposes, stable across arg order."""
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args)
        except Exception:
            return f"{name}::{raw_args}"
    if isinstance(raw_args, dict):
        if name in _URL_KEYED_TOOLS:
            raw_args = {"url": raw_args.get("url", "")}
        return f"{name}::{json.dumps(raw_args, sort_keys=True, default=str)}"
    return f"{name}::{raw_args}"


class ResearchLimitGuard(GuardMiddleware):
    """Force a final answer after too many consecutive tool-only rounds
    that made no progress.

    Counter lives on the instance (per ADR-007: guard-specific state
    does not leak into :class:`HookContext`). Reset on any round that
    produced text, increment on a round that emitted only tool calls.
    Rounds with neither text nor tool calls are empty and ignored.

    Uses non-strict ``>=``: the counter is incremented before the check,
    so the Nth consecutive tool-only round triggers the stop.

    Progress reset: paging a long document is silent work, not a spiral. A
    round whose tool results show new ground resets the counter: a page
    range, offset or URL not seen before in this task with a successful
    result, or a browser_snapshot / browser_extract result that differs from
    the tool's previous one. A refusal or error (starts with a warning or
    cross mark) and a repeat of an earlier call are not progress.

    Timing: AFTER_LLM_CALL fires before the round's tools run, so it sees the
    calls of this round but the results of the previous one. The guard keeps
    the previous round's calls itself and pairs them with those results.

    Ceiling: progress cannot run forever. ``max_silent_total`` (60) tool-only
    rounds since the last text stop the run even when every one made progress.
    """

    def __init__(self, max_consecutive: int = 15, max_silent_total: int = 60) -> None:
        self._max = max_consecutive
        # A policy, not a measurement: past 60 silent rounds the agent must
        # write text, and any text resets both counters.
        self._max_total = max_silent_total
        # Valid for one task only: run_llm_loop builds a fresh guard per run
        # (loop.py:1019), so nothing here outlives the task it counted.
        self._counter = 0
        self._silent_total = 0
        self._ceiling_hit = False
        self._seen_calls: set[str] = set()
        self._last_output: dict[str, str] = {}
        self._prev_calls: list[dict] = []

    def _round_made_progress(self, results: list) -> bool:
        """Did the previous round's results show new ground?"""
        queues: dict[str, list] = {}
        for c in self._prev_calls:
            queues.setdefault(c.get("name", ""), []).append(c.get("args", {}))
        progress = False
        for res in results or []:
            if not isinstance(res, dict):
                continue
            name = res.get("name", "")
            out = res.get("output", "")
            out = out if isinstance(out, str) else str(out)
            failed = out.lstrip().startswith(_FAILURE_PREFIXES)
            args = queues[name].pop(0) if queues.get(name) else None
            if name in _NEW_OUTPUT_PROGRESS_TOOLS:
                prev = self._last_output.get(name)
                self._last_output[name] = out
                if not failed and prev != out:
                    progress = True
            elif name in _NEW_CALL_PROGRESS_TOOLS and args is not None:
                key = _call_key(name, args)
                field = _JSON_PROGRESS_FIELD.get(name)
                if field and not failed:
                    try:
                        failed = not (json.loads(out).get(field, 0) > 0)
                    except (ValueError, AttributeError, TypeError):
                        failed = True
                # Only a success closes the key: a failed fetch may be retried
                # and the retry is new ground if it works.
                if not failed:
                    if key not in self._seen_calls:
                        progress = True
                    self._seen_calls.add(key)
        return progress

    async def after_llm_call(self, ctx: HookContext) -> Optional[HookAction]:
        if self._prev_calls and self._round_made_progress(ctx.recent_tool_results):
            self._counter = 0
        self._prev_calls = [
            c for c in (ctx.recent_tool_args or []) if isinstance(c, dict)
        ]

        if ctx.last_response_has_text:
            self._counter = 0
            self._silent_total = 0
            self._prev_calls = []
        elif ctx.tool_calls_this_turn > 0:
            self._counter += 1
            self._silent_total += 1

        if self._silent_total >= self._max_total:
            self._ceiling_hit = True
            log.warning(
                "ResearchLimitGuard: %d tool-only rounds since the last text "
                "reached the ceiling %d",
                self._silent_total,
                self._max_total,
            )
            return HookAction.STOP_LOOP
        if self._counter >= self._max:
            log.warning(
                "ResearchLimitGuard: %d consecutive tool-only rounds without "
                "progress reached max=%d",
                self._counter,
                self._max,
            )
            return HookAction.STOP_LOOP
        return None

    def stop_message(self) -> str:
        if self._ceiling_hit:
            return (
                f"[RESEARCH_LIMIT] You have spent {self._silent_total} rounds "
                "calling tools without providing any text response to the "
                "user. Stop researching. Summarise your findings and give "
                "your answer now."
            )
        return (
            f"[RESEARCH_LIMIT] You have spent {self._counter} consecutive "
            "rounds calling tools without progress and without providing any "
            "text response to the user. Stop researching. Summarise your "
            "findings and give your answer now."
        )


# Tools whose call signature does not identify the call, so repeats have to
# be judged by whether the OUTPUT advanced. Two shapes qualify: polling a
# long-running external task (the comfyui family, identical args for the
# whole render), and reading live external state (browser_snapshot takes no
# args at all, so five snapshots of five different pages are indistinguishable
# from one call repeated five times).
_OUTPUT_KEYED_TOOLS = frozenset({
    "comfyui_progress", "comfyui_wait", "comfyui_check", "browser_snapshot",
})

# Browser tools whose result reads the page, so a changed result means the
# page moved on. The action tools are absent on purpose: browser_click answers
# "Clicked @e15" every time, whatever the page did, so its own output says
# nothing. A site that reuses one ref for its Next button on every page makes
# the click fingerprint identical for the whole task.
_PAGE_STATE_TOOLS = frozenset({
    "browser_snapshot", "browser_navigate", "browser_extract",
    "browser_switch_tab", "browser_collect",
})
# Action tools whose repeat counters are reset when the page state advanced.
_BROWSER_ACTION_TOOLS = frozenset({
    "browser_click", "browser_fill", "browser_select", "browser_wait_for",
    "browser_scroll",
})


class LoopGuard(GuardMiddleware):
    """Stop if a single (tool, args) fingerprint repeats too many times.

    Session-scoped counter per ``name::json_sorted_args`` fingerprint.
    When any fingerprint hits the cap the agent is stuck in a repeat
    loop. Tool-call dicts are shaped ``{"name": str, "args": dict}``;
    ``args`` may arrive as a JSON string from some providers and is
    normalised before fingerprinting.

    Exception for :data:`_OUTPUT_KEYED_TOOLS`: when such a call's output
    advances vs the previous one (new output = new information), the repeat
    counter for that tool is reset, so monitoring a slow generation — or
    walking a browser session page by page — is not killed. Output that
    stops changing (done/stuck/same page) still trips the cap.

    Same idea for :data:`_BROWSER_ACTION_TOOLS`: when a page-reading browser
    result (:data:`_PAGE_STATE_TOOLS`) differs from that tool's previous one,
    the page advanced, and the action counters reset. The same click five
    times on a page that never changes still trips.
    """

    def __init__(self, max_duplicate_calls: int = 5) -> None:
        self._max = max_duplicate_calls
        self._counts: dict[str, int] = {}
        self._last_stuck: list[str] = []
        # Keyed by tool name (one entry per polling tool). Assumes a single
        # ComfyUI instance per agent; a multi-instance setup would need to key
        # by (name, api_url) to avoid cross-instance output clobbering.
        self._last_poll_output: dict[str, str] = {}
        self._last_page_output: dict[str, str] = {}

    @staticmethod
    def _fingerprint(call: dict) -> str:
        name = call.get("name", "?")
        raw_args = call.get("args", {})
        if isinstance(raw_args, str):
            try:
                raw_args = json.loads(raw_args)
            except Exception:
                pass
        if isinstance(raw_args, dict):
            args_key = json.dumps(raw_args, sort_keys=True)
        else:
            args_key = str(raw_args)
        return f"{name}::{args_key}"

    async def after_llm_call(self, ctx: HookContext) -> Optional[HookAction]:
        # An output-keyed tool whose output advanced since the last call
        # produced NEW information — reset its repeat counter so live
        # monitoring of a long task, or a walk across pages, is not mistaken
        # for a stuck loop.
        for res in (ctx.state.recent_tool_results or []):
            if not isinstance(res, dict):
                continue
            name = res.get("name", "")
            if name in _PAGE_STATE_TOOLS:
                page_out = res.get("output", "")
                if self._last_page_output.get(name) not in (None, page_out):
                    for k in list(self._counts):
                        if k.split("::", 1)[0] in _BROWSER_ACTION_TOOLS:
                            self._counts[k] = 0
                self._last_page_output[name] = page_out
            if name not in _OUTPUT_KEYED_TOOLS:
                continue
            out = res.get("output", "")
            if self._last_poll_output.get(name) not in (None, out):
                for k in list(self._counts):
                    if k.startswith(f"{name}::"):
                        self._counts[k] = 0
            self._last_poll_output[name] = out

        for call in ctx.recent_tool_args:
            if not isinstance(call, dict):
                continue
            key = self._fingerprint(call)
            self._counts[key] = self._counts.get(key, 0) + 1
            if self._counts[key] >= self._max:
                log.warning(
                    "LoopGuard: fingerprint %r hit %d repeats (max=%d)",
                    key,
                    self._counts[key],
                    self._max,
                )
                name = call.get("name", "?")
                if name not in self._last_stuck:
                    self._last_stuck.append(name)
                return HookAction.STOP_LOOP
        return None

    def stop_message(self) -> str:
        dedup = ", ".join(sorted(set(self._last_stuck))) or "?"
        return (
            f"[LOOP_GUARD] You have called the following tool(s) with "
            f"identical arguments {self._max} or more times without new "
            f"information: {dedup}. Stop repeating these calls. "
            "Summarise what you know so far and give your final answer now."
        )


class BudgetLimitGuard(GuardMiddleware):
    """Stop when accumulated cost crosses a fraction of the budget.

    ``budget_remaining_usd`` of ``None`` or non-positive disables the
    guard — not every task carries a budget.
    """

    def __init__(
        self,
        budget_remaining_usd: Optional[float] = None,
        max_fraction: float = 0.5,
    ) -> None:
        self._budget = budget_remaining_usd
        self._max_fraction = max_fraction
        self._last_cost = 0.0

    async def between_rounds(self, ctx: HookContext) -> Optional[HookAction]:
        if self._budget is None or self._budget <= 0:
            return None
        self._last_cost = ctx.accumulated_cost_usd
        threshold = self._budget * self._max_fraction
        if ctx.accumulated_cost_usd > threshold:
            log.warning(
                "BudgetLimitGuard: %d tokens exceeded %.0f%% of budget %d tokens",
                int(ctx.accumulated_cost_usd),
                self._max_fraction * 100,
                int(self._budget),
            )
            return HookAction.STOP_LOOP
        return None

    def stop_message(self) -> str:
        return (
            f"[BUDGET_LIMIT] Task consumed {int(self._last_cost)} tokens (>"
            f"{self._max_fraction * 100:.0f}% of budget "
            f"{int(self._budget)} tokens). Give your final response now."
        )


__all__ = [
    "RoundLimitGuard",
    "ToolLimitGuard",
    "ResearchLimitGuard",
    "LoopGuard",
    "BudgetLimitGuard",
]


class ContextLimitGuard(GuardMiddleware):
    """Stop before a round the window cannot hold.

    The sixth guard, and the loop ran without it until 2026-08-23: rounds, tools,
    research, loop-detection and budget were all watched, and the one resource an
    agent actually exhausts on a long task was watched by nothing. Compaction was
    the only defence, and compaction is not a limit — it is a best effort that
    reaches only tool results outside the recent rounds and reduces each of them
    once.

    A ceiling rather than a predictor, deliberately. It reads the previous
    round's real input size, which is the same number compaction triggers on, and
    fires only when that already sits above `ratio` of the window. By then
    compaction has had every round to work and the size is still there, so what
    is left is the part it cannot reach. Stopping here ends the turn with its work
    intact and a named reason, instead of discovering the limit inside the engine
    — where on this platform the likelier answer is not an error but the driver
    paging and prefill collapsing, which reads as an agent that became slow.

    Default 0.95: high enough that a run compaction can still rescue is never
    interrupted, low enough to leave room for one more round's growth.
    """

    def __init__(self, ratio: float = 0.95) -> None:
        self._ratio = ratio
        self._seen_ratio: Optional[float] = None

    async def between_rounds(self, ctx: HookContext) -> Optional[HookAction]:
        window = ctx.context_window
        used = ctx.last_prompt_tokens
        # Round one has no previous size, and a window nobody could resolve is
        # not a limit to enforce — silence there, not a guess.
        if window <= 0 or used <= 0:
            return None
        ratio = used / window
        if ratio < self._ratio:
            return None
        self._seen_ratio = ratio
        log.warning(
            "ContextLimitGuard: last prompt %d tokens is %.1f%% of the %d-token "
            "window (limit %.0f%%) — stopping before the round that would not fit",
            used, ratio * 100, window, self._ratio * 100,
        )
        return HookAction.STOP_LOOP

    def stop_message(self) -> str:
        seen = f"{self._seen_ratio * 100:.0f}%" if self._seen_ratio else "the limit"
        return (
            f"[CONTEXT_LIMIT] The conversation reached {seen} of the model's context "
            "window and compaction could not reduce it further. Stopping with the work "
            "so far rather than failing inside the engine. Start a new task, or raise "
            "the agent's context_window if the model has room."
        )
