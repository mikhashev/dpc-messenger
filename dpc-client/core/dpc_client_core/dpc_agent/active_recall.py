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
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional

from .hybrid_search import SearchResult
from .index_keys import L5_PREFIX as L5_KEY_PREFIX, L6_PREFIX as L6_KEY_PREFIX
from .tool_ledger import EvidenceReadFailed, is_outcome
from .utils import utc_now_iso

log = logging.getLogger(__name__)

CONTEXT_THRESHOLD_HINTS_ONLY = 0.5
CONTEXT_THRESHOLD_SKIP = 0.7
DECAY_FLOOR = 0.1
# A document nobody has touched yet is not a document nobody wants — it is one that
# has not been offered. Without this it lands on DECAY_FLOOR, 0.1, while a document
# shown once and ignored is the busiest in its own result set and scores 1.0: a
# tenfold penalty for being new against being uninteresting.
#
# Measured in days, not sessions: there is no session counter at this layer, and the
# constant that named one sat unused for months partly because nothing could have
# incremented it. Age comes from the file's mtime via `source_path`, which every layer
# now carries — a stat() per candidate, and no index change to record what the
# filesystem already knows.
GRACE_PERIOD_DAYS = 7

# What an injection is worth next to a read. Bounded strictly below 1.0, the value of
# a single read, so no amount of showing a document can outrank one act of opening it.
# Saturation is where extra showings stop adding anything: past it the only way up is
# to be read.
#
# The bound is on the counter, and the counter is a multiplier on the fusion score —
# so what it really buys is a margin. At 0.9 the margin was 11%: a document shown 5000
# times and never opened beat one read once as soon as search liked it 12% better,
# which is ordinary noise between two candidates. At 0.3 a read is worth more than any
# history of showing until search prefers the other by more than 3.3×, and that is a
# difference of kind rather than of noise.
#
# Both numbers are placeholders. They were chosen by argument, not from data, and the
# data that would settle them — how often a shown-and-never-read document turns out to
# be worth showing — starts accruing only now that the address works. Revisit with the
# first weeks of follow-rate, not before.
INJECTION_MAX_CREDIT = 0.3
INJECTION_SATURATION = 20


def is_shared_layer(meta: Dict) -> bool:
    """Does this document belong to the shared human layer, by key or by label?"""
    return (str(meta.get("source_file", "")).startswith(f"{L6_KEY_PREFIX}/")
            or meta.get("source_layer") == "L6")


def hint_address(meta: Dict, extended_read_enabled: bool = True,
                 shared_knowledge_enabled: bool = True) -> Optional[str]:
    """What to pass to read_file() to actually get this document, or None if nothing works.

    The index key names a document; it does not locate it. Only the agent's own
    knowledge layer is named by a path read_file can follow, because that layer lives
    inside the sandbox and its key is the sandbox-relative path. Everything else — the
    shared human layer, every indexed external root — resolves through the absolute
    path recorded at indexing time.

    The two outside layers answer to different permissions, and each is asked here in
    the same state read_file will ask it. The shared layer used to be treated as
    settled by its presence in the index — the document proved the knowledge gate when
    it was indexed. But a gate is a question about now, and revoking it does not
    reindex anything: the row stays, so the hint went on printing an address that
    read_file had already begun refusing.

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
    if is_shared_layer(meta):
        return source_path if shared_knowledge_enabled else None
    return source_path if extended_read_enabled else None


@dataclass
class RenderedHints:
    """The block, plus the addresses it actually printed.

    Attribution compares strings: an address the agent followed has to be matched
    against the address it was given. Recomputing that address at log time would
    compare our second guess with the agent's copy of the first, and the two drift
    the moment the renderer changes. So the code that prints them hands them over.

    `addresses` is aligned with the injected results, and carries None exactly where
    the hint printed a dead end — which is what makes "how often is a slot wasted"
    a count rather than an impression.
    """

    text: str
    addresses: List[Optional[str]]


def format_recall_hints(
    results: List[SearchResult],
    max_results: int = 3,
    extended_read_enabled: bool = True,
    shared_knowledge_enabled: bool = True,
) -> str:
    """Format search results as markdown hints for Block2 injection."""
    return render_recall_hints(results, max_results, extended_read_enabled,
                               shared_knowledge_enabled).text


def render_recall_hints(
    results: List[SearchResult],
    max_results: int = 3,
    extended_read_enabled: bool = True,
    shared_knowledge_enabled: bool = True,
) -> RenderedHints:
    """Format the hints and report which addresses went into them."""
    if not results:
        return RenderedHints(text="", addresses=[])

    hints = results[:max_results]
    addresses: List[Optional[str]] = []
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
        address = hint_address(meta, extended_read_enabled, shared_knowledge_enabled)
        addresses.append(address)
        if address:
            action = f'call read_file("{address}") for details'
        else:
            # An honest dead end beats a plausible one: the agent stops instead of
            # spending a round guessing at spellings of a path it cannot reach. Name
            # the gate that is actually shut — two layers reach this line, and telling
            # an agent to check the wrong toggle is its own kind of dead end.
            action = ("not readable from here — shared knowledge access is off"
                      if is_shared_layer(meta)
                      else "not readable from here — extended path read access is off")
        lines.append(f"[{source}] {filename}: {label} — {action}")
        if excerpt:
            lines.append(f"    {excerpt}")
    lines.append("--- END RECALL ---")
    lines.append("")
    return RenderedHints(text="\n".join(lines), addresses=addresses)


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


@dataclass
class RecallInjection:
    """What was actually put into the context, said by the code that put it there.

    The caller used to describe this from the outside: it logged the size of the
    candidate pool as the number of hints, re-derived the mode from the ratio, and
    printed the fusion scores from before decay re-ordered them. Three chances to
    describe something other than what happened, and it took all three — so the log
    we would check today's work against reported a different event.

    Reporting from here costs nothing, because this is the code that made the choice.
    """

    text: str
    mode: str
    injected: List[SearchResult]

    def summary(self) -> str:
        return ", ".join(
            f"{r.chunk_meta.get('source_file', '?')}({r.score:.3f})" for r in self.injected
        )

    def __bool__(self) -> bool:
        return bool(self.text)


def _has_something_to_offer(result: SearchResult, extended_read_enabled: bool,
                            shared_knowledge_enabled: bool = True) -> bool:
    """A slot must carry either a way in or something to read. This carries neither.

    Narrow on purpose. A hint with no address but with an excerpt is still worth a
    slot — that is the honest dead end an agent gets when extended read is off, and
    the excerpt is the whole point of saying so. What has nothing is the graph
    channel's meta: no `source_path` to build an address from and no `text` to quote,
    so the line prints a filename, a "(chunk 0)" that names a chunk nobody stored,
    and a reason that blames a toggle which is not involved.

    Dropping it here rather than in the channel keeps the candidate in the fused pool,
    where the question "is the graph channel worth its slot" is still being measured.

    The shared layer with its gate closed is the one case where an excerpt does not
    redeem a missing address: the index keeps 500 characters of every L6 document, and
    quoting 200 of them is handing over the content the gate was closed to withhold.
    Revocation blocks the read at once and cannot reach the index, so it has to be
    honoured here — a dead end for extended paths is a courtesy, and here it is the
    whole permission.
    """
    meta = result.chunk_meta
    if hint_address(meta, extended_read_enabled, shared_knowledge_enabled):
        return True
    if is_shared_layer(meta) and not shared_knowledge_enabled:
        return False
    return bool(meta.get("text"))


def get_recall_block(
    results: List[SearchResult],
    context_usage_ratio: float = 0.0,
    max_results: int = 3,
    agent_root: Optional[pathlib.Path] = None,
    extended_read_enabled: bool = True,
    task_id: str = "",
    shared_knowledge_enabled: bool = True,
) -> RecallInjection:
    """Get the appropriate recall block based on context budget and decay scoring."""
    mode = should_inject(context_usage_ratio)
    if mode == "skip":
        return RecallInjection(text="", mode=mode, injected=[])
    if agent_root and results:
        results = _apply_decay(results, agent_root)
    results = [r for r in results
               if _has_something_to_offer(r, extended_read_enabled, shared_knowledge_enabled)]
    injected = results[:max_results]
    # Render before logging: the addresses recorded are the ones printed, taken from
    # the render that printed them. The `hints` mode prints no addresses at all, so
    # it has none to record — and is excluded from the follow-rate denominator for
    # that reason rather than by convention.
    if mode == "hints":
        text = format_hints_only(results, max_results)
        addresses: List[Optional[str]] = []
    else:
        rendered = render_recall_hints(results, max_results, extended_read_enabled,
                                       shared_knowledge_enabled)
        text, addresses = rendered.text, rendered.addresses
    if agent_root and injected:
        _log_knowledge_access(injected, mode, agent_root, task_id=task_id,
                              addresses=addresses)
    return RecallInjection(text=text, mode=mode, injected=injected if text else [])


@dataclass
class AccessCounts:
    """How often each document was shown and how often it was read, kept apart.

    Two vocabularies, because the two sources name documents differently and neither
    can be translated into the other without guessing. The injection log records index
    keys, since that is what the index hands it. A read records whatever string the
    agent passed to read_file — the sandbox-relative key for its own layer, an
    absolute path for anything outside it. So keep both and let the lookup ask in both
    languages: a document knows its own key and its own path.

    And two kinds of event, because they are not the same evidence. Showing a document
    is something we did; reading it is something the agent chose. Adding them made the
    number that decides what to show rise by showing it — a file floated because it
    had been offered before, not because it had ever helped.

    So a read outweighs any number of injections *in this number*: injections earn a
    bounded credit that cannot reach the value of a single read. Stated on the final
    order the claim is weaker and worth stating honestly — the number multiplies the
    fusion score, so a document with enough of a search advantage still wins. The bound
    sets how much advantage that takes; see INJECTION_MAX_CREDIT.

    Within the bound injections still order documents nobody has read yet, which is the
    weak signal worth keeping rather than discarding. Turning "shown and never read"
    into an actual penalty is a different claim, and it needs evidence we do not have
    yet — the loop has been closed for hours, not weeks.
    """

    injections_by_key: Dict[str, int]
    reads_by_key: Dict[str, int]
    reads_by_path: Dict[str, int]
    # Reads that went to an address the same turn had printed. A subset of the two
    # above, kept apart because "the agent opened this" and "the hint worked" are
    # different questions, and one column answering both is where the one-in-four
    # figure came from. Empty for turns recorded before the read carried the field.
    follows_by_key: Dict[str, int] = field(default_factory=dict)
    follows_by_path: Dict[str, int] = field(default_factory=dict)

    def follows_for(self, meta: Dict) -> int:
        n = self.follows_by_key.get(meta.get("source_file", ""), 0)
        source_path = meta.get("source_path", "")
        if source_path:
            n += self.follows_by_path.get(_norm_path(source_path), 0)
        return n

    def reads_for(self, meta: Dict) -> int:
        n = self.reads_by_key.get(meta.get("source_file", ""), 0)
        source_path = meta.get("source_path", "")
        if source_path:
            n += self.reads_by_path.get(_norm_path(source_path), 0)
        return n

    def injections_for(self, meta: Dict) -> int:
        return self.injections_by_key.get(meta.get("source_file", ""), 0)

    def for_document(self, meta: Dict) -> float:
        """Evidence that this document is worth showing again, reads first."""
        shown = self.injections_for(meta)
        credit = min(1.0, shown / INJECTION_SATURATION) * INJECTION_MAX_CREDIT if shown else 0.0
        return self.reads_for(meta) + credit

    def __bool__(self) -> bool:
        return bool(self.injections_by_key or self.reads_by_key or self.reads_by_path)


def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _norm_key(path: str) -> str:
    """A relative address as the index would spell it.

    Index keys always use forward slashes (they are built with as_posix), while an
    agent on Windows may well type or copy a backslash. Only the native separator is
    rewritten: on POSIX a backslash is a legal character in a filename, not a
    separator, and rewriting it there would invent a different document.
    """
    key = path.replace(os.sep, "/")
    if os.altsep:
        key = key.replace(os.altsep, "/")
    return key[2:] if key.startswith("./") else key


def _read_log_paths(agent_root: pathlib.Path) -> List[pathlib.Path]:
    """Every file still holding reads, oldest rotation first.

    append_jsonl rotates tools.jsonl at 5 MB into `.1` and deletes whatever `.1` held
    before, so the read history on disk is two files and no more. Reading only the live
    one threw away the older of the two for nothing: measured on agent_001, reads
    covered 11 days while the file next to it held 13 more.
    """
    logs = agent_root / "logs"
    rotated = sorted(logs.glob("tools.jsonl.*"), reverse=True)  # .2, .1, then live
    return [p for p in [*rotated, logs / "tools.jsonl"] if p.exists()]


def _iter_jsonl(path: pathlib.Path):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return
    except OSError as exc:
        raise EvidenceReadFailed(f"{path}: {exc}") from exc
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue  # one torn line is not a reason to lose the file


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

    The two sources are held to one window. Injections are appended to a file that has
    never rotated and reads to one that rotates away, so the same number was built from
    103 days of showing against 11 days of reading on agent_001 — a document read every
    week in May and shown ever since arrives here looking like one that is only ever
    shown. The window is taken from the reads because that is the side that expires:
    injections older than the oldest surviving read are not counted, which makes the
    comparison like-for-like by construction and needs no date written down anywhere.
    A log with no reads at all is not a mismatch, only an absence, and is left whole.
    """
    injections: List[tuple] = []
    reads_by_key: Dict[str, int] = Counter()
    reads_by_path: Dict[str, int] = Counter()
    follows_by_key: Dict[str, int] = Counter()
    follows_by_path: Dict[str, int] = Counter()
    oldest_read = ""

    # Source 1: hints injected into context (knowledge_access.jsonl), recorded by key.
    for entry in _iter_jsonl(agent_root / "state" / "knowledge_access.jsonl"):
        injections.append((str(entry.get("ts", "")), entry.get("files", [])))

    # Source 2: reads the agent actually performed (tools.jsonl), recorded by address.
    for path in _read_log_paths(agent_root):
        for entry in _iter_jsonl(path):
            if entry.get("tool", "") != "read_file" or not is_outcome(entry):
                continue
            ts = str(entry.get("ts", ""))
            if ts and (not oldest_read or ts < oldest_read):
                oldest_read = ts
            path_val = str((entry.get("args") or {}).get("path", ""))
            if not path_val:
                continue
            # No filtering by whether the word "knowledge" appears in the path.
            # That stood in for "is this a knowledge file" and got it wrong both
            # ways. A path that names no indexed document simply matches nothing.
            followed = entry.get("via_hint") is True
            if os.path.isabs(path_val):
                reads_by_path[_norm_path(path_val)] += 1
                if followed:
                    follows_by_path[_norm_path(path_val)] += 1
            else:
                reads_by_key[_norm_key(path_val)] += 1
                if followed:
                    follows_by_key[_norm_key(path_val)] += 1

    injections_by_key: Dict[str, int] = Counter()
    for ts, files in injections:
        if oldest_read and ts and ts < oldest_read:
            continue
        for f in files:
            injections_by_key[f] += 1

    return AccessCounts(
        injections_by_key=dict(injections_by_key),
        reads_by_key=dict(reads_by_key),
        reads_by_path=dict(reads_by_path),
        follows_by_key=dict(follows_by_key),
        follows_by_path=dict(follows_by_path),
    )


def _is_within_grace(meta: Dict, now: Optional[float] = None) -> bool:
    """Was this document written recently enough that having no history means nothing?

    Answered from the file, not from the index: `source_path` is on every layer's meta
    since the store started keeping it, and mtime is the one date the filesystem is
    guaranteed to have. A path we cannot stat is treated as old — an unreadable file
    should not be promoted by its own unreadability.
    """
    source_path = meta.get("source_path", "")
    if not source_path:
        return False
    try:
        age_seconds = (now if now is not None else time.time()) - os.path.getmtime(source_path)
    except OSError:
        return False
    return 0 <= age_seconds < GRACE_PERIOD_DAYS * 86400


def _decay_multiplier(result: SearchResult, access: float, max_count: float) -> float:
    """What access history does to a candidate's score.

    Inside the grace window a new document is left alone (1.0) rather than floored: the
    absence of a history is not evidence against it, and one week is short enough that
    a document which never earns reads returns to the ordinary rule on its own. Note
    1.0 is level with the busiest candidate in the set, not above it — grace removes a
    penalty, it does not hand out a promotion.
    """
    if access:
        return max(DECAY_FLOOR, access / max_count)
    return 1.0 if _is_within_grace(result.chunk_meta) else DECAY_FLOOR


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
    try:
        counts = _build_access_counts(agent_root)
    except EvidenceReadFailed as exc:
        log.warning("Active Recall: ranking left undecayed, evidence unreadable — %s", exc)
        return results
    if not counts:
        return results

    accesses = [counts.for_document(r.chunk_meta) for r in results]
    max_count = max(accesses, default=0)
    if max_count <= 0:
        return results

    # The decayed score is the score: it is what decided the order, so it is what a
    # reader of the log needs to see. Keeping the pre-decay number on the result meant
    # the printed ranking and the printed numbers disagreed with each other.
    scored = [
        replace(r, score=r.score * _decay_multiplier(r, access, max_count))
        for r, access in zip(results, accesses)
    ]
    scored.sort(key=lambda r: -r.score)
    return scored


_OFFERED_ADDRESSES: "OrderedDict[str, set]" = OrderedDict()
_OFFERED_TASKS_KEPT = 32


def _remember_offered(task_id: str, addresses: List[Optional[str]]) -> None:
    """Keep what a turn was offered, so a read in that turn can say it followed one."""
    if not task_id:
        return
    printed = {_norm_path(a) for a in addresses if a}
    if not printed:
        return
    _OFFERED_ADDRESSES[task_id] = printed
    _OFFERED_ADDRESSES.move_to_end(task_id)
    while len(_OFFERED_ADDRESSES) > _OFFERED_TASKS_KEPT:
        _OFFERED_ADDRESSES.popitem(last=False)


def followed_a_hint(task_id: str, path: str) -> Optional[bool]:
    """Did this read go to an address the same turn was shown?

    None when the turn was offered nothing — a read then is neither a follow nor a
    refusal to follow, and counting it either way is what made the old join wrong.
    The alternative to this field is joining the two logs afterwards by path and
    time, which credits a document the agent found on its own and misses the one it
    tried and failed to open.
    """
    offered = _OFFERED_ADDRESSES.get(task_id)
    if not offered:
        return None
    return _norm_path(path) in offered


def _log_knowledge_access(
    results: List[SearchResult],
    mode: str,
    agent_root: pathlib.Path,
    task_id: str = "",
    addresses: Optional[List[Optional[str]]] = None,
) -> None:
    """Log which knowledge files were injected into context (S1 feedback loop).

    Two fields carry the attribution. `task_id` is written with the same meaning
    `tools.jsonl` gives it, so the two logs join on equal strings instead of on a
    guess about which turn a read belongs to. `addresses` holds what the hint
    printed, position for position with `files`, so "the agent followed the hint"
    is a string comparison rather than an inference from timestamps — and a null
    there is a slot that was spent on a document the agent could not open.
    """
    access_path = agent_root / "state" / "knowledge_access.jsonl"
    access_path.parent.mkdir(parents=True, exist_ok=True)
    files = [r.chunk_meta.get("source_file", "unknown") for r in results]
    entry = {"ts": utc_now_iso(), "task_id": task_id, "mode": mode, "files": files,
             "addresses": list(addresses or []), "useful": None}
    _remember_offered(task_id, list(addresses or []))
    try:
        with open(access_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


ACCESS_LOG_ARCHIVE = "knowledge_access.archive.jsonl"


def compact_access_log(agent_root: pathlib.Path) -> int:
    """Move injections older than the counted window into an archive, and keep them.

    The counter reads the whole injection log on every user message — 1.48 MB on
    agent_001, next to 8.87 MB of read logs, growing with every turn and never
    shrinking, because this is the one log in the system that has no rotation. Deleting
    it is not an option: it is the only record of what the system offered over 103 days
    and the denominator of every question we have asked about the repair.

    So nothing is deleted. Lines the counter no longer counts — older than the oldest
    surviving read, the window the counter itself derives — are appended to an archive
    that is never opened at runtime. Written archive-first: a crash between the two
    steps repeats lines in a file nobody parses, where the alternative loses them.

    Returns how many lines moved.
    """
    live = agent_root / "state" / "knowledge_access.jsonl"
    if not live.exists():
        return 0
    boundary = ""
    for path in _read_log_paths(agent_root):
        for entry in _iter_jsonl(path):
            if entry.get("tool", "") != "read_file" or not is_outcome(entry):
                continue
            ts = str(entry.get("ts", ""))
            if ts and (not boundary or ts < boundary):
                boundary = ts
    if not boundary:
        return 0  # no reads means no window, so nothing is out of it

    try:
        lines = live.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0

    stale, current = [], []
    for line in lines:
        if not line.strip():
            continue
        try:
            ts = str(json.loads(line).get("ts", ""))
        except json.JSONDecodeError:
            current.append(line)  # unparsable: keep it where a human will find it
            continue
        (stale if ts and ts < boundary else current).append(line)

    if not stale:
        return 0
    try:
        with open(live.parent / ACCESS_LOG_ARCHIVE, "a", encoding="utf-8") as f:
            f.write("\n".join(stale) + "\n")
        tmp = live.with_suffix(".jsonl.compacting")
        tmp.write_text(("\n".join(current) + "\n") if current else "", encoding="utf-8")
        tmp.replace(live)
    except OSError:
        log.debug("access log compaction failed for %s", agent_root.name, exc_info=True)
        return 0
    log.info("Access log compacted for %s: %d lines archived, %d kept (window opens %s)",
             agent_root.name, len(stale), len(current), boundary[:19])
    return len(stale)


def _excerpt(meta: dict, max_chars: int = 200) -> str:
    text = meta.get("text", "")
    if not text:
        ci = meta.get("chunk_index", 0)
        return f"(chunk {ci})"
    return text[:max_chars].replace("\n", " ").strip()
