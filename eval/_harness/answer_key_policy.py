"""What a benchmark agent may not fetch: the published answers to the benchmark.

The run of 2026-09-23 (`20260923-0543`) searched for «GAIA benchmark …» by
name, opened pages that quote the dataset row (`gaia-benchmark/GAIA/
discussions/26`, `Kevin355/Who_and_When`, `harbor-datasets/…/gaia/<id>/`,
`hal.cs.princeton.edu/…/gaia/analysis/`), copied the answer and scored it.
Nothing local was touched, so the canary could not see it.

This wraps the handlers of the web tools the benchmark enables, at the
registry, for one run only — production agents are untouched. A refused call
returns a plain tool error the agent can read; a search result pointing at an
answer list is dropped before the agent sees it; a browser page that is one is
withheld. Every refusal is recorded (tool, url or query, task).

What it cannot do: `run_shell` is refused only on what the command text says.
A script written to a file and then run fetches whatever it likes — the report's
exposure scan over the ledger is what catches that.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import inspect
import re
import threading
from typing import Any, Callable, Dict, List, Optional

POLICY_VERSION = "2026-09-23.1"

# Shapes of pages that carry GAIA answers. The named ones are what runs on this
# machine opened (A-MIRROR-PAGE-FULL-OF-GOLD…, A-GAIA-AGENT-GOES-LOOKING…); the
# last branch is the shape rule, because a list of mirrors is always short.
# Local attachments (`file://`, `127.0.0.1`, `…/dpc-gaia-*/gaia-files/`) never
# match: the shape rule wants http(s) to a remote host.
DENY_URL_RE = re.compile(
    r"huggingface\.co/(?:api/)?(?:datasets|spaces)/\S*gaia"
    r"|gaia-benchmark|Who_and_When|harbor-(?:datasets|index)"
    r"|hal\.cs\.princeton\.edu/\S*gaia|huggingface\.co/\S*final_assignment"
    r"|cmriat/gaia|bstraehle/gaia|MinorJerry/WebVoyager|MCP-1st-Birthday"
    r"|enlatics/Enlatics_benchmarking|lauspectrum/\S*gaia|Intelligent-Internet/\S*gaia"
    r"|ChromaFlow9897|chromaflow-gaia"
    r"|https?://(?!(?:127\.0\.0\.1|localhost|\[::1\])[:/])(?=\S*gaia)"
    r"\S*(?:jsonl|validation|metadata|benchmark|leaderboard|answer)",
    re.I,
)

# «GAIA» as a word. Letters on either side make it another word; digits,
# `_`, `-`, `+` and `%20` do not, so `GAIA_benchmark` and `q=foo%20gaia` count.
GAIA_WORD_RE = re.compile(r"(?<![A-Za-z])gaia(?![A-Za-z])", re.I)
# The same word inside a search URL's query string.
GAIA_IN_URL_QUERY_RE = re.compile(
    r"[?&#](?:q|query|search_query|p|text|wd)=[^&#\s\"']*(?<![A-Za-z])gaia(?![A-Za-z])", re.I)

# What a dataset row looks like when it reaches a tool result. Case matters:
# «final answer» and «FINAL ANSWER» are the task's own vocabulary.
GOLD_MARKER_RE = re.compile(r"groundtruth|\"Final answer\"|Expected answer")
# The two that are a dataset row and nothing else: a JSON key. Output carrying
# one is withheld, not only recorded.
GOLD_ROW_RE = re.compile(r"\"(?:Final answer|groundtruth)\"\s*:", re.I)

BLOCK_PREFIX = "⚠️ BLOCKED_IN_BENCHMARK"

URL_TOOLS = frozenset({"browse_page", "fetch_json", "check_url", "browser_navigate"})
SEARCH_TOOLS = frozenset({"search_web"})
SHELL_TOOLS = frozenset({"run_shell"})
# Browser tools that act on whatever page the session holds; the page's own URL
# is checked before and after, because a click can land on an answer list.
PAGE_TOOLS = frozenset({
    "browser_click", "browser_fill", "browser_select", "browser_scroll",
    "browser_wait_for", "browser_snapshot", "browser_extract", "browser_screenshot",
    "browser_collect", "browser_download", "browser_switch_tab",
})
WRAPPED_TOOLS = URL_TOOLS | SEARCH_TOOLS | SHELL_TOOLS | PAGE_TOOLS

_SEARCH_ITEM_SPLIT = re.compile(r"\n\n(?=\s*\d+\.\s)")
_URL_TOKEN_RE = re.compile(r"https?://\S+")


def is_answer_key_url(url: Any) -> bool:
    """A URL (or any text) the benchmark refuses to fetch."""
    if not isinstance(url, str) or not url:
        return False
    return bool(DENY_URL_RE.search(url) or GAIA_IN_URL_QUERY_RE.search(url))


def names_gaia(query: Any) -> bool:
    return isinstance(query, str) and bool(GAIA_WORD_RE.search(query))


def describe() -> Dict[str, Any]:
    """What provenance records: which policy, by version and by digest."""
    body = "\n".join([
        POLICY_VERSION, DENY_URL_RE.pattern, GAIA_WORD_RE.pattern,
        GAIA_IN_URL_QUERY_RE.pattern, GOLD_MARKER_RE.pattern, GOLD_ROW_RE.pattern,
        ",".join(sorted(WRAPPED_TOOLS)),
    ])
    return {
        "version": POLICY_VERSION,
        "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "deny_url_pattern": DENY_URL_RE.pattern,
        "gaia_query_pattern": GAIA_WORD_RE.pattern,
        "tools_wrapped": sorted(WRAPPED_TOOLS),
        "not_covered": "run_shell is refused on its command text only; a script "
                       "file that fetches a mirror is caught by the exposure scan, "
                       "not prevented",
    }


def _refusal(tool: str, what: str) -> str:
    return (f"{BLOCK_PREFIX} ({tool}): answer-key source — {what}. This benchmark "
            "refuses published GAIA answers and their mirrors; find the facts the "
            "task asks about in primary sources.")


def _task_of(ctx: Any) -> Optional[str]:
    root = getattr(ctx, "agent_root", None)
    return getattr(root, "name", None) if root is not None else None


async def _browser_page_url(ctx: Any) -> Optional[str]:
    """The URL the agent's browser session is on, or None.

    Read on the session's own thread: Playwright's sync objects are
    thread-affine. Any failure is None — a check that cannot read the page
    does not break the tool.
    """
    try:
        from dpc_client_core.dpc_agent.tools import browser as _browser
        session = _browser._active_browser_sessions.get(ctx.agent_root.name)
        page = getattr(session, "_page", None)
        if session is None or page is None:
            return None
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(session._get_executor(), lambda: page.url), timeout=10)
    except Exception:
        return None


class AnswerKeyPolicy:
    """One per run; `install` it on every task's registry."""

    def __init__(self, page_url: Optional[Callable[[Any], Any]] = None):
        self.events: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        # Injected by tests; the real reader asks the live browser session.
        self._page_url = page_url or _browser_page_url

    # -- record ------------------------------------------------------------

    def _record(self, ctx: Any, tool: str, kind: str, **fields: Any) -> None:
        row = {"task": _task_of(ctx), "tool": tool, "kind": kind}
        row.update({k: (v[:300] if isinstance(v, str) else v) for k, v in fields.items()})
        with self._lock:
            self.events.append(row)

    def summary(self) -> Dict[str, Any]:
        with self._lock:
            events = list(self.events)
        counts: Dict[str, int] = {}
        for e in events:
            counts[e["kind"]] = counts.get(e["kind"], 0) + 1
        refused = sum(v for k, v in counts.items() if k.startswith(("refused", "withheld")))
        return {**describe(), "refusals": refused, "by_kind": counts, "events": events}

    # -- install -----------------------------------------------------------

    def install(self, registry: Any) -> List[str]:
        """Wrap the web tools this registry holds. Returns the names wrapped."""
        entries = getattr(registry, "_entries", None)
        if not entries:
            return []
        wrapped = []
        for name in sorted(WRAPPED_TOOLS):
            entry = entries.get(name)
            if entry is None or getattr(entry.handler, "_answer_key_policy", None) is self:
                continue
            registry.override_handler(name, self.wrap(name, entry.handler))
            wrapped.append(name)
        return wrapped

    def wrap(self, name: str, handler: Callable) -> Callable:
        if inspect.iscoroutinefunction(handler):
            async def wrapped(ctx, *args, **kwargs):
                refusal = self._before(name, ctx, kwargs)
                if refusal is None and name in PAGE_TOOLS:
                    refusal = self._page_refusal(name, ctx, await self._page_url(ctx), "before")
                if refusal is not None:
                    return refusal
                result = await handler(ctx, *args, **kwargs)
                if name in PAGE_TOOLS or name == "browser_navigate":
                    after = self._page_refusal(name, ctx, await self._page_url(ctx), "after")
                    if after is not None:
                        return after
                return self._after(name, ctx, kwargs, result)
        else:
            def wrapped(ctx, *args, **kwargs):
                refusal = self._before(name, ctx, kwargs)
                if refusal is not None:
                    return refusal
                result = handler(ctx, *args, **kwargs)
                if inspect.iscoroutine(result):
                    async def _finish():
                        return self._after(name, ctx, kwargs, await result)
                    return _finish()
                return self._after(name, ctx, kwargs, result)
        # The registry reads the signature to resolve argument aliases, and
        # `inspect.signature` follows `__wrapped__`.
        functools.update_wrapper(wrapped, handler)
        wrapped._answer_key_policy = self
        return wrapped

    # -- decide ------------------------------------------------------------

    def _before(self, name: str, ctx: Any, kwargs: Dict[str, Any]) -> Optional[str]:
        if name in URL_TOOLS:
            url = kwargs.get("url")
            if is_answer_key_url(url):
                self._record(ctx, name, "refused_url", url=url)
                return _refusal(name, str(url))
        elif name in SEARCH_TOOLS:
            query = kwargs.get("query")
            if names_gaia(query):
                self._record(ctx, name, "refused_query", query=query)
                return _refusal(name, "a search naming the benchmark")
        elif name in SHELL_TOOLS:
            command = kwargs.get("command")
            if is_answer_key_url(command):
                self._record(ctx, name, "refused_command", command=command)
                return _refusal(name, "a command that fetches an answer list")
        return None

    def _page_refusal(self, name: str, ctx: Any, url: Any, when: str) -> Optional[str]:
        if not is_answer_key_url(url):
            return None
        self._record(ctx, name, "refused_page" if when == "before" else "withheld_page",
                     url=url)
        return _refusal(name, f"the browser is on {url}; navigate elsewhere or close it")

    def _after(self, name: str, ctx: Any, kwargs: Dict[str, Any], result: Any) -> Any:
        if not isinstance(result, str):
            return result
        if name in SEARCH_TOOLS:
            result = self._filter_search(name, ctx, kwargs, result)
        if GOLD_ROW_RE.search(result):
            self._record(ctx, name, "withheld_output", marker=GOLD_ROW_RE.search(result).group(0),
                         url=kwargs.get("url") or kwargs.get("command") or kwargs.get("query"))
            return _refusal(name, "the result carries dataset answer rows")
        seen = GOLD_MARKER_RE.search(result)
        if seen:
            self._record(ctx, name, "gold_marker_seen", marker=seen.group(0),
                         url=kwargs.get("url") or kwargs.get("command") or kwargs.get("query"))
        return result

    def _filter_search(self, name: str, ctx: Any, kwargs: Dict[str, Any], result: str) -> str:
        """Drop result items that point at an answer list or quote a row."""
        parts = _SEARCH_ITEM_SPLIT.split(result)
        if len(parts) < 2:
            return result
        head, items = parts[0], parts[1:]
        kept, dropped = [], []
        for item in items:
            if is_answer_key_url(item) or GOLD_MARKER_RE.search(item):
                dropped.append(item)
            else:
                kept.append(item)
        if not dropped:
            return result
        for item in dropped:
            url = _URL_TOKEN_RE.search(item)
            self._record(ctx, name, "withheld_result", query=kwargs.get("query"),
                         url=url.group(0) if url else None)
        note = (f"({len(dropped)} result(s) withheld in the benchmark: answer-key source)")
        if not kept:
            return f"{head.rstrip()}\n\n{note}"
        return "\n\n".join([head] + kept + [note])
