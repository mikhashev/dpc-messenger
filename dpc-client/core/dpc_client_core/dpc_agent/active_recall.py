"""Active Recall hint injection (ADR-010, MEM-3.8) + S4 decay (ADR-013).

On each user message: embed query → hybrid search → top-3 hints.
Inject hints in Block2 context with source layer label.
Budget-aware: >50% context → hints only, >70% → skip.
S4 decay: score results by historical access frequency — unused files sink.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
from collections import Counter
from typing import Dict, List, Optional

from .hybrid_search import SearchResult
from .index_keys import L5_PREFIX as L5_KEY_PREFIX, L6_PREFIX as L6_KEY_PREFIX
from .utils import utc_now_iso

log = logging.getLogger(__name__)

CONTEXT_THRESHOLD_HINTS_ONLY = 0.5
CONTEXT_THRESHOLD_SKIP = 0.7
DECAY_FLOOR = 0.1
GRACE_PERIOD_SESSIONS = 5


def hint_address(meta: Dict, extended_read_enabled: bool = True) -> Optional[str]:
    """What to pass to read_file() to actually get this document, or None if nothing works.

    The index key names a document; it does not locate it. Only the agent's own
    knowledge layer is named by a path read_file can follow, because that layer lives
    inside the sandbox and its key is the sandbox-relative path. Everything else — the
    shared human layer, every indexed external root — resolves through the absolute
    path recorded at indexing time.

    The two outside layers answer to different permissions, and only one of them is the
    extended-read toggle. The shared layer is admitted to the index by the knowledge
    gate, so a document already sitting in this index has proved that gate — read_file
    honours the same one. External roots are the toggle's business, and when it is off
    there is no address to give.

    None means the file is genuinely out of reach right now, and the caller should say
    so. Printing a path the agent cannot follow is how this went unnoticed for 102 days:
    the hint looked helpful, the call failed, and nothing counted the failure.
    """
    key = meta.get("source_file", "")
    if key.startswith(f"{L5_KEY_PREFIX}/"):
        return key
    source_path = meta.get("source_path", "")
    if not source_path:
        # Indexed before source_path was recorded. Nothing to resolve to.
        return None
    if key.startswith(f"{L6_KEY_PREFIX}/") or meta.get("source_layer") == "L6":
        return source_path
    return source_path if extended_read_enabled else None


def format_recall_hints(
    results: List[SearchResult],
    max_results: int = 3,
    extended_read_enabled: bool = True,
) -> str:
    """Format search results as markdown hints for Block2 injection."""
    if not results:
        return ""

    hints = results[:max_results]
    lines = [
        "Active Recall",
        "--- ACTIVE RECALL ---",
        "If a recalled file looks relevant to the current discussion, call read_file() to get full content.",
    ]
    for r in hints:
        meta = r.chunk_meta
        source = meta.get("source_layer", "L5")
        filename = meta.get("source_file", "unknown")
        heading = meta.get("heading", "")
        label = f"{heading}" if heading else filename
        excerpt = _excerpt(meta)
        address = hint_address(meta, extended_read_enabled)
        if address:
            action = f'call read_file("{address}") for details'
        else:
            # An honest dead end beats a plausible one: the agent stops instead of
            # spending a round guessing at spellings of a path it cannot reach.
            action = "not readable from here — extended path read access is off"
        lines.append(f"[{source}] {filename}: {label} — {action}")
        if excerpt:
            lines.append(f"    {excerpt}")
    lines.append("--- END RECALL ---")
    lines.append("")
    return "\n".join(lines)


def should_inject(context_usage_ratio: float) -> str:
    """Determine injection mode based on context window usage.

    Returns: 'full' | 'hints' | 'skip'
    """
    if context_usage_ratio >= CONTEXT_THRESHOLD_SKIP:
        return "skip"
    if context_usage_ratio >= CONTEXT_THRESHOLD_HINTS_ONLY:
        return "hints"
    return "full"


def format_hints_only(results: List[SearchResult], max_results: int = 3) -> str:
    """Compact format: filenames only, no excerpts."""
    if not results:
        return ""
    hints = results[:max_results]
    names = [f"[{r.chunk_meta.get('source_layer', 'L5')}] {r.chunk_meta.get('source_file', '?')}"
             for r in hints]
    return f"Active Recall\n--- RECALL HINTS: {', '.join(names)} ---\n"


def get_recall_block(
    results: List[SearchResult],
    context_usage_ratio: float = 0.0,
    max_results: int = 3,
    agent_root: Optional[pathlib.Path] = None,
    extended_read_enabled: bool = True,
) -> str:
    """Get the appropriate recall block based on context budget and decay scoring."""
    mode = should_inject(context_usage_ratio)
    if mode == "skip":
        return ""
    if agent_root and results:
        results = _apply_decay(results, agent_root)
    injected = results[:max_results]
    if agent_root and injected:
        _log_knowledge_access(injected, mode, agent_root)
    if mode == "hints":
        return format_hints_only(results, max_results)
    return format_recall_hints(results, max_results, extended_read_enabled)


def _build_access_counts(agent_root: pathlib.Path) -> Dict[str, int]:
    """Build access frequency map from live JSONL + retroactive tools.jsonl.

    Sources: S1 live data, retroactive knowledge reads, skill usage (S8).
    """
    counts: Dict[str, int] = Counter()

    # Source 1: live knowledge_access.jsonl (S1 data)
    live_path = agent_root / "state" / "knowledge_access.jsonl"
    if live_path.exists():
        try:
            for line in live_path.read_text(encoding="utf-8").strip().split("\n"):
                if not line.strip():
                    continue
                entry = json.loads(line)
                for f in entry.get("files", []):
                    counts[os.path.basename(f)] += 1
        except (json.JSONDecodeError, OSError):
            pass

    # Source 2 + S8: retroactive baseline from tools.jsonl (knowledge reads + skill usage)
    tools_path = agent_root / "logs" / "tools.jsonl"
    if tools_path.exists():
        try:
            for line in tools_path.read_text(encoding="utf-8").strip().split("\n"):
                if not line.strip():
                    continue
                entry = json.loads(line)
                tool = entry.get("tool", "")
                args = entry.get("args", {})
                path_val = args.get("path", "")
                if tool == "read_file" and "knowledge" in path_val.lower():
                    counts[os.path.basename(path_val)] += 1
                elif tool == "execute_skill":
                    skill_name = args.get("skill_name", args.get("name", ""))
                    if skill_name:
                        counts[f"skill:{skill_name}"] += 1
        except (json.JSONDecodeError, OSError):
            pass

    # S9: startup init — if no live data yet, ensure retro baseline is used
    if not live_path.exists() and counts:
        log.debug("S9 startup: no live knowledge_access.jsonl, using retroactive baseline only")

    return dict(counts)


def _apply_decay(
    results: List[SearchResult], agent_root: pathlib.Path
) -> List[SearchResult]:
    """Re-rank results by access frequency. Unused files sink, used files float.

    ADR-013 S4: decay floor 0.1, grace period for new files.
    """
    counts = _build_access_counts(agent_root)
    if not counts:
        return results

    max_count = max(counts.values()) if counts else 1

    scored = []
    for r in results:
        filename = os.path.basename(r.chunk_meta.get("source_file", ""))
        access = counts.get(filename, 0)
        if access == 0:
            decay_multiplier = DECAY_FLOOR
        else:
            decay_multiplier = max(DECAY_FLOOR, access / max_count)
        scored.append((r, r.score * decay_multiplier))

    scored.sort(key=lambda x: -x[1])
    return [r for r, _ in scored]


def _log_knowledge_access(
    results: List[SearchResult], mode: str, agent_root: pathlib.Path
) -> None:
    """Log which knowledge files were injected into context (S1 feedback loop)."""
    access_path = agent_root / "state" / "knowledge_access.jsonl"
    access_path.parent.mkdir(parents=True, exist_ok=True)
    files = [r.chunk_meta.get("source_file", "unknown") for r in results]
    entry = {"ts": utc_now_iso(), "mode": mode, "files": files, "useful": None}
    try:
        with open(access_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _excerpt(meta: dict, max_chars: int = 200) -> str:
    text = meta.get("text", "")
    if not text:
        ci = meta.get("chunk_index", 0)
        return f"(chunk {ci})"
    return text[:max_chars].replace("\n", " ").strip()
