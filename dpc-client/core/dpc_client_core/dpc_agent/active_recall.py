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
from dataclasses import dataclass
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


@dataclass
class AccessCounts:
    """How often each document was touched, in the two vocabularies we have.

    The two sources name documents differently and neither can be translated into
    the other without guessing. The injection log records index keys, because that is
    what the index hands it. A read records whatever string the agent passed to
    read_file — the sandbox-relative key for its own layer, an absolute path for
    anything outside it. So keep both and let the lookup ask in both languages: a
    document knows its own key and its own path.
    """

    by_key: Dict[str, int]
    by_path: Dict[str, int]

    def for_document(self, meta: Dict) -> int:
        n = self.by_key.get(meta.get("source_file", ""), 0)
        source_path = meta.get("source_path", "")
        if source_path:
            n += self.by_path.get(_norm_path(source_path), 0)
        return n

    def __bool__(self) -> bool:
        return bool(self.by_key or self.by_path)


def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _build_access_counts(agent_root: pathlib.Path) -> AccessCounts:
    """Count accesses per document, keyed by what identifies a document.

    It used to count by bare filename. Measured on the live agents before this
    change: `README.md` was one bucket holding 49 different files with 4109 accesses
    between them, and it set the normaliser for everything else — so 1791 of
    agent_001's 1855 documents sat on the decay floor and decay ranked nothing, it
    divided everything by ten. A file also inherited the standing of every namesake
    the moment it was created, which is not bias, it is the wrong number.

    Skill invocations are no longer counted here. They were, under a `skill:` key
    that no document can ever match, so their only effect was to raise the
    normaliser — the same defect in miniature.
    """
    by_key: Dict[str, int] = Counter()
    by_path: Dict[str, int] = Counter()

    # Source 1: hints injected into context (knowledge_access.jsonl), recorded by key.
    live_path = agent_root / "state" / "knowledge_access.jsonl"
    if live_path.exists():
        try:
            for line in live_path.read_text(encoding="utf-8").strip().split("\n"):
                if not line.strip():
                    continue
                entry = json.loads(line)
                for f in entry.get("files", []):
                    by_key[f] += 1
        except (json.JSONDecodeError, OSError):
            pass

    # Source 2: reads the agent actually performed (tools.jsonl), recorded by address.
    tools_path = agent_root / "logs" / "tools.jsonl"
    if tools_path.exists():
        try:
            for line in tools_path.read_text(encoding="utf-8", errors="replace").strip().split("\n"):
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry.get("tool", "") != "read_file":
                    continue
                path_val = str((entry.get("args") or {}).get("path", ""))
                if not path_val:
                    continue
                # No filtering by whether the word "knowledge" appears in the path.
                # That stood in for "is this a knowledge file" and got it wrong both
                # ways. A path that names no indexed document simply matches nothing.
                if os.path.isabs(path_val):
                    by_path[_norm_path(path_val)] += 1
                else:
                    by_key[path_val] += 1
        except (json.JSONDecodeError, OSError):
            pass

    return AccessCounts(by_key=dict(by_key), by_path=dict(by_path))


def _apply_decay(
    results: List[SearchResult], agent_root: pathlib.Path
) -> List[SearchResult]:
    """Re-rank results by access frequency. Unused files sink, used files float.

    ADR-013 S4: decay floor 0.1, grace period for new files.

    Normalised against the candidates being ranked, not against every number in the
    log. Decay decides an order within one result set, and only ratios inside that set
    can affect the order — while a global maximum lets a file nobody is considering
    push the whole set onto the floor, which is what a project README did.
    """
    counts = _build_access_counts(agent_root)
    if not counts:
        return results

    accesses = [counts.for_document(r.chunk_meta) for r in results]
    max_count = max(accesses, default=0)
    if max_count <= 0:
        return results

    scored = [
        (r, r.score * (max(DECAY_FLOOR, access / max_count) if access else DECAY_FLOOR))
        for r, access in zip(results, accesses)
    ]
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
