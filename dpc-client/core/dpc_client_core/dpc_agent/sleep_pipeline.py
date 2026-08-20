"""Sleep Consolidation Pipeline (ADR-014).

Reads session digests + archives, performs LLM retrospective analysis,
writes morning_brief.json + sleep_findings.json. Triggered by UI
toggle button via WebSocket command.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

SLEEP_STATE_FILE = "sleep_state.json"
MORNING_BRIEF_FILE = "morning_brief.json"
SLEEP_FINDINGS_FILE = "sleep_findings.json"

# One sleep pipeline at a time, process-wide (= per device: one backend per
# machine). Two consolidations running together carry 92-170K-token prompts
# against llama-server's unified 262 144-token KV pool, and the second
# prefill eats the pool out from under the first — the server then kills
# both with "Context size has been exceeded" (2026-08-19: three failure
# waves, two agents lost their briefs). Chat turns don't take this lock:
# a chat alongside a sleep stays well inside the pool; only sleep+sleep
# overflows it. An asyncio lock serializes one event loop — both triggers
# (agent sleep and group sleep) enter through service.py's create_task on
# the backend's single loop; a second loop would silently un-serialize this.
_SLEEP_PIPELINE_LOCK = asyncio.Lock()

PER_SESSION_PROMPT = """\
Analyze this single conversation session and extract key information. \
Respond with ONLY a JSON object, no markdown fences.

{{
  "date": "session date",
  "decisions": ["decision 1", "decision 2"],
  "topics": ["topic 1", "topic 2"],
  "unresolved": ["open question 1"],
  "lessons": ["lesson learned"],
  "notable_events": ["anything surprising or important"],
  "productivity": "high|medium|low",
  "summary": "2-3 sentence summary of the session"
}}

Guidelines:
- Be factual, not flattering.
- Focus on decisions, lessons, and unresolved items.
- Language: match the language of the session.

--- SESSION ---
Date: {date}
Messages: {message_count}, Duration: {duration_mins} min
Tools used: {tools}

{messages}
"""

SYNTHESIS_PROMPT = """\
You have per-session analysis results from {n} recent sessions. \
Synthesize them into a retrospective report.

Respond with ONLY a JSON object with two keys:

"morning_brief": {{
  "sessions_analyzed": {n},
  "period": "{period}",
  "last_session": {{
    "date": "...",
    "what_was_done": ["item 1", "item 2"],
    "where_stopped": "What was in progress when the session ended",
    "pending_items": ["carryover task 1", "carryover task 2"]
  }},
  "key_decisions": [{{"decision": "...", "session": "...", "rationale": "..."}}],
  "patterns_noticed": [{{"pattern": "...", "evidence": "..."}}],
  "unresolved": [{{"topic": "...", "context": "..."}}],
  "summary": "2-3 sentence human-readable summary of what happened"
}}

"sleep_findings": {{
  "behavioral_observations": [{{"observation": "...", "significance": "low|medium|high"}}],
  "recurring_topics": [{{"topic": "...", "progress": "advancing|stalled|repeating"}}],
  "suggested_focus": ["area1", "area2"]
}}

Guidelines:
- **last_session**: Use the dedicated MOST RECENT SESSION block below. \
Do NOT pick from --- PER-SESSION FINDINGS ---; the most recent has been \
pre-selected for you. List concrete carryover items — tasks mentioned but \
not completed, decisions deferred, things explicitly "pending" or "carryover".
- Focus on CROSS-SESSION patterns: what changed, what reversed, what repeated.
- Be factual. If sessions were unproductive, say so.
- Language: match the language of the sessions.

--- MOST RECENT SESSION (use this for last_session) ---
{most_recent}

--- PER-SESSION FINDINGS (newest first) ---
{findings}
{entity_section}"""

ENTITY_RELATION_SECTION = """
--- ENTITY RELATION EXTRACTION ---
The following named entities were extracted from session texts by NER:
{entity_list}

For each PAIR of entities that are meaningfully related, add an entry to \
"extracted_relations" in the JSON response:

"extracted_relations": [
  {{"source": "entity_name_1", "target": "entity_name_2", "relation_type": "DEPENDS_ON|SUPPORTS|CONTRADICTS|RESPONDS_TO", "confidence": 0.0-1.0, "justification": "min 20 chars explaining WHY this relation exists"}}
]

Rules:
- Only use entities from the list above (do NOT invent new ones)
- Only add relations with confidence >= 0.7
- justification MUST be at least 20 characters
- If a relation involves a Decision (ADR, protocol rule), add "needs_review": true
- If no meaningful relations found, return empty list
"""


SYNTHESIS_BUDGET_FACTOR = 0.85
# The room reserved for the answer, and — since 2026-08-20 — the ceiling the call
# actually asks for. One quantity, used at both ends on purpose: it used to be
# two, 4000 subtracted from the input budget and never sent, while the request
# went out at the provider default of 8192.
#
# Measured on the run that produced no brief (local model, 124 sessions): prompt
# 178 731 of a 262 144 window, completion 8 192 of 8 192, `finish=length`, of
# which 4 921 tokens were the think block and 3 271 the answer — a real,
# well-formed brief cut mid-array. The brief needs more than 3 271 and ~83 K of
# window sat unused.
#
# A fraction rather than a flat number because the fleet's windows differ by two
# orders of magnitude: a flat 16 384 would leave a 16 000-token alias with a
# negative input budget, which is not a reserve but a refusal.
SYNTHESIS_OUTPUT_RESERVE_FRACTION = 0.10
SYNTHESIS_OUTPUT_RESERVE_MIN = 4000
SYNTHESIS_OUTPUT_RESERVE_MAX = 16384
# What the think block may take of that room. The ceiling is the deterministic
# half — it gives the brief space whatever the model does; this is the other
# half, because the run above spent 60 % of its ceiling thinking and a larger
# ceiling alone would have been partly eaten again. Three quarters stay with the
# answer. Providers that do not read a per-request budget ignore it; they are
# also the ones whose alias already carries a 65 536-token ceiling.
SYNTHESIS_THINKING_SHARE = 0.25

# How long a sleep may hold its own lock before a later trigger calls it stuck
# and starts over the top of it.
SLEEP_TIMEOUT_MINUTES = 30

# One unattended call over the whole archive, sized against the provider default
# of 300s that is meant as headroom for a model's first VRAM load. A local
# thinking model reading a hundred archives exceeded that and took the entire
# morning brief with it, because nothing here retries. Nobody is waiting on this
# call, so the cost of it being generous is only that a genuinely stuck daemon is
# noticed later; the cost of it being short is a night with no brief.
#
# Derived from the lock window rather than chosen beside it. The first version of
# this constant was 1800.0 — exactly the window — so a synthesis that spent its
# whole budget would cross the stuck threshold at the same instant, and the next
# trigger would reset a run that was still working. One step of the pipeline
# cannot be allowed to consume the whole allowance the pipeline is given.
SYNTHESIS_TIMEOUT_SECONDS = SLEEP_TIMEOUT_MINUTES * 60 / 2


def _synthesis_output_reserve(context_window: int) -> int:
    """Room for the answer: a tenth of the window, floored and capped.

    Single source of truth — the input budget subtracts this and the request
    asks for exactly this, so the two cannot drift into different numbers again.
    """
    return min(
        SYNTHESIS_OUTPUT_RESERVE_MAX,
        max(SYNTHESIS_OUTPUT_RESERVE_MIN, int(context_window * SYNTHESIS_OUTPUT_RESERVE_FRACTION)),
    )


def _synthesis_request_limits(context_window: int) -> Dict[str, int]:
    """What the synthesis call asks the provider for, on this window."""
    reserve = _synthesis_output_reserve(context_window)
    return {
        "max_tokens": reserve,
        "reasoning_budget_tokens": max(1, int(reserve * SYNTHESIS_THINKING_SHARE)),
    }


def _compute_synthesis_budget(context_window: int, template_overhead_tokens: int) -> int:
    """Tokens available for findings_text after reserving output + template overhead.

    Single source of truth for the budget formula — both the truncation
    helper and the observability site read from this. If the factor or
    reserve change, only this function needs editing.
    """
    return (int(context_window * SYNTHESIS_BUDGET_FACTOR)
            - _synthesis_output_reserve(context_window)
            - template_overhead_tokens)


def _render_finding_block(seq_index: int, finding: Dict[str, Any]) -> str:
    """Render one finding into its `Session N (date): {json}` block.

    Kept as a helper so the budget accounting and the final findings_text
    rendering use the exact same text — token count == render size.
    """
    date_prefix = finding.get("digest_date", "")[:10]
    return f"Session {seq_index} ({date_prefix}):\n{json.dumps(finding, ensure_ascii=False, indent=2)}"


def _select_findings_within_budget(
    findings_sorted_desc: List[Dict[str, Any]],
    llm_manager,
    target_model: Optional[str],
    context_window: int,
    template_overhead_tokens: int,
) -> tuple[List[Dict[str, Any]], int]:
    """Select findings newest-to-oldest until adaptive synthesis budget is hit.

    Budget = SYNTHESIS_BUDGET_FACTOR * context_window minus output reserve
    minus the synthesis prompt template + most_recent block (passed in as
    template_overhead_tokens).

    Always includes at least one finding when input is non-empty, even if
    a single finding would exceed budget — degraded but non-empty synthesis
    is better than empty findings_text.

    Returns (selected_findings, tokens_consumed_by_findings).
    """
    budget = _compute_synthesis_budget(context_window, template_overhead_tokens)
    selected: List[Dict[str, Any]] = []
    tokens_used = 0

    for f in findings_sorted_desc:
        finding_text = _render_finding_block(len(selected) + 1, f)
        if target_model:
            finding_tokens = llm_manager.count_tokens(finding_text, target_model)
        else:
            finding_tokens = len(finding_text) // 4  # rough fallback

        if selected and tokens_used + finding_tokens > budget:
            break
        selected.append(f)
        tokens_used += finding_tokens

    return selected, tokens_used


def _read_sleep_state(conversation_dir: Path) -> Dict[str, Any]:
    path = conversation_dir / SLEEP_STATE_FILE
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"status": "awake"}


def _write_sleep_state(conversation_dir: Path, state: Dict[str, Any]) -> None:
    path = conversation_dir / SLEEP_STATE_FILE
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


#: How many automatic dumps to keep per agent — seven **sleeps**, not seven nights.
#: Nothing in this system schedules sleep: a human runs it, from `/sleep` in the 1:1
#: chat or the UI button, and only on an empty chat. So the backup cadence is exactly
#: the cadence of that ritual, and a quiet week takes no copies at all. Seven is enough
#: to reach back past a bad run nobody noticed the same day, and costs about 18 MB on
#: the largest agent. Hand-taken dumps are kept by naming them something other than the
#: pattern below — rotation only ever touches its own.
#: (The first version of this comment said "every night on its own". Ark and GLM 5.2
#: caught it independently: there is no night in the code.)
GRAPH_EXPORT_KEEP = 7
GRAPH_EXPORT_DIR = "knowledge_graph_export"
_NIGHTLY_SUFFIX = "-nightly.jsonl"


def _export_graph_snapshot(agent_id: Optional[str], conversation_dir: Path) -> Optional[Path]:
    """Write the agent's graph out, and keep the last few nights of them.

    Returns the dump path, or None when there is no graph to dump. Called from the
    tail of a completed sleep — see the call site for why there and not elsewhere.
    """
    from .knowledge_graph import KnowledgeGraph
    from .utils import get_agent_root

    if agent_id:
        agent_root = get_agent_root(agent_id)
    else:
        agent_root = conversation_dir.parent.parent / "agents" / conversation_dir.name
    if not agent_root.is_dir():
        return None

    # This builds its own facade rather than borrowing the service's, and that is safe
    # only because the grafeo backend keys a singleton on the resolved store path — both
    # end up on one handle. A *second process* cannot open a live `.grafeo` at all, and
    # a second handle in this one would hit the same wall. Two consequences worth
    # stating rather than inheriting: never call `close()` on this facade (it would shut
    # the store the service is still serving from), and if that singleton ever goes
    # away, this line has to become the cached lookup instead. Ark asked whether this
    # path was ever measured; `test_two_facades_on_one_root_share_the_live_handle` now
    # holds it.
    kg = KnowledgeGraph(agent_root)
    if kg.backend.node_count() == 0:
        return None

    previous = sorted((agent_root / GRAPH_EXPORT_DIR).glob(f"*{_NIGHTLY_SUFFIX}"))
    irreplaceable_before = _llm_relation_count(previous[-1]) if previous else 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = agent_root / GRAPH_EXPORT_DIR / f"{stamp}{_NIGHTLY_SUFFIX}"
    written = kg.export_to(target)

    # Rotation must never be the thing that finishes off a damaged graph. The standing
    # symptom on this fleet is a store that reopens smaller; if that ever reaches the
    # class nothing rebuilds, the graph is damaged-but-not-empty, sleep still completes,
    # and seven more successful backups would quietly evict the last copy that still
    # had those edges in it. So: a shrink means keep everything and say so. Raised by
    # Fable 5 in review as the thing to fix here before anything else.
    irreplaceable_now = kg.snapshot().get("edges_by_source", {}).get("llm_relation", 0)
    if previous and irreplaceable_now < irreplaceable_before:
        log.warning(
            "Sleep: knowledge graph backed up, but llm_relation fell from %d to %d — "
            "keeping every previous dump instead of rotating, because one of them is "
            "the last copy that still has those edges",
            irreplaceable_before, irreplaceable_now,
        )
    else:
        nightly = sorted(target.parent.glob(f"*{_NIGHTLY_SUFFIX}"))
        for old in nightly[:-GRAPH_EXPORT_KEEP]:
            try:
                old.unlink()
            except OSError as e:
                log.debug("Could not rotate out %s: %s", old.name, e)

    log.info("Sleep: knowledge graph backed up — %d nodes, %d edges, %d irreplaceable → %s",
             written["nodes"], written["edges"], irreplaceable_now, target.name)
    return target


def _llm_relation_count(dump: Path) -> int:
    """How many irreplaceable edges a previous dump holds, read from its own header."""
    try:
        with dump.open(encoding="utf-8") as fh:
            first = fh.readline()
        header = json.loads(first) if first.strip() else {}
    except (OSError, json.JSONDecodeError):
        return 0
    return ((header.get("snapshot") or {}).get("edges_by_source") or {}).get("llm_relation", 0)


def _collect_group_archive_digests(group_dir: Path, agent_id: str) -> List[Dict[str, Any]]:
    """Collect archived session digests from a specific group's archive/.

    Returns ALL group archives where this agent was a member. Reuse-detection
    happens later via per-archive sha256 in `sleep_results/result_*.json`.
    """
    digests = []
    archive_dir = group_dir / "archive"
    if not archive_dir.exists():
        return digests
    metadata_path = group_dir / "metadata.json"
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            agents_map = metadata.get("agents", {})
            if agents_map and not any(agent_id in ids for ids in agents_map.values()):
                return digests
        except (json.JSONDecodeError, OSError):
            pass
    if metadata_path.exists():
        try:
            _meta = json.loads(metadata_path.read_text(encoding="utf-8"))
            group_id = _meta.get("group_id", group_dir.name)
        except (json.JSONDecodeError, OSError):
            group_id = group_dir.name
    else:
        group_id = group_dir.name
    for archive_path in sorted(archive_dir.rglob("*.json")):
        try:
            data = json.loads(archive_path.read_text(encoding="utf-8"))
            messages = data.get("messages", [])
            if not messages:
                continue
            archive_date = messages[0].get("timestamp", "")
            digests.append({
                "archive_file": f"group_archive:{group_dir.name}:{archive_path.name}",
                "date": archive_date,
                "message_count": len(messages),
                "duration_mins": 0,
                "source": "group_archive",
                "group_id": data.get("conversation_id", group_id),
                "archive_path": str(archive_path),
            })
        except (json.JSONDecodeError, OSError):
            continue
    return digests


def _find_archive_digests(conversation_dir: Path) -> List[Dict[str, Any]]:
    """Read all 1:1 session digests from digest.jsonl.

    Returns ALL digests in file order. Caller `run_sleep` is responsible for
    sorting after merging with group archive digests — a single sort site
    keeps the chronological invariant in one place.
    """
    digest_path = conversation_dir / "digest.jsonl"
    if not digest_path.exists():
        return []

    digests = []
    with open(digest_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    digests.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    return digests


def _load_archive(conversation_dir: Path, archive_filename: str) -> Optional[Dict[str, Any]]:
    for archive_path in (conversation_dir / "archive").rglob(archive_filename):
        try:
            return json.loads(archive_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return None


def _format_archive_messages(archive: Dict[str, Any]) -> str:
    messages = archive.get("messages", [])
    parts = []
    for msg in messages:
        sender = msg.get("sender_name", msg.get("role", ""))
        content = msg.get("content", "")
        if content:
            parts.append(f"[{sender}]: {content}")
    return "\n".join(parts)


def _parse_llm_json(response: str) -> Dict[str, Any]:
    try:
        return json.loads(response)
    except json.JSONDecodeError:
        cleaned = response.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0]
        return json.loads(cleaned)


def _last_usage_of(llm_manager, provider_alias: Optional[str]) -> Dict[str, Any]:
    """What the provider reported for its most recent call, or nothing.

    Best-effort on every step: this feeds a diagnostic, and a diagnostic that
    can raise turns one failure into two.
    """
    try:
        providers = getattr(llm_manager, "providers", None) or {}
        alias = provider_alias or getattr(llm_manager, "default_provider", None)
        provider = providers.get(alias) if alias else None
        if provider is None or not hasattr(provider, "get_last_usage"):
            return {}
        return provider.get_last_usage() or {}
    except Exception:
        return {}


def _capture_unparsable(
    response: str,
    err: json.JSONDecodeError,
    *,
    label: str,
    usage: Optional[Dict[str, Any]] = None,
    dump_dir: Optional[Path] = None,
) -> Optional[Path]:
    """Record what could not be parsed, before the exception leaves.

    The 2026-08-20 synthesis failure destroyed its own evidence: the
    exception carried a position and nothing else, so «the model wrote a
    brief and was cut» and «the model wrote a stub and then prose» stayed
    indistinguishable — and those two need opposite repairs. The head tells
    which of the two it was; the tail tells whether the text stops
    mid-word; `finish_reason` and the completion count say whether the
    ceiling was reached. The full body goes to disk because this log
    rotates by size, so a noisy day can delete the evidence within hours.
    """
    usage = usage or {}
    head = response[:500]
    tail = response[-200:] if len(response) > 500 else "(shorter than the head window)"

    dump_path: Optional[Path] = None
    if dump_dir is not None:
        try:
            dump_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            dump_path = dump_dir / f"failed_{label}_{stamp}.txt"
            # errors="replace", and the guard catches ValueError beside OSError:
            # a lone surrogate makes strict encoding raise UnicodeEncodeError,
            # which is a ValueError — so a narrow guard would let the instrument
            # destroy the very evidence it exists to keep, and mask the parse
            # error on the way out.
            dump_path.write_text(response, encoding="utf-8", errors="replace")
        except (OSError, ValueError) as dump_err:
            log.warning("Unparsable %s: the dump could not be written: %s", label, dump_err)
            dump_path = None

    log.error(
        "Unparsable %s response: %s at char %d | chars=%d completion_tokens=%s/%s "
        "finish=%s | dump=%s\n--- first 500 ---\n%s\n--- last 200 ---\n%s",
        label, err.msg, err.pos, len(response),
        # The ceiling beside the count: `completion_tokens=8192` only means
        # "cut at the cap" to a reader who already knows the cap is 8192, and
        # this is the fallback signal for the day `finish_reason` comes back
        # empty from the pinned server.
        usage.get("completion_tokens", "?"), usage.get("max_tokens", "?"),
        usage.get("finish_reason", "?"),
        dump_path or "-", head, tail,
    )
    return dump_path


def _build_session_source_id(archive: str) -> tuple[str, str]:
    """Build (source_id, display_label) for a sleep session.

    Returns the canonical KG node id used as `source_id` on MENTIONS edges
    in ADR-024 Phase 2. The source node itself must exist in the graph
    before persist_extracted_entities runs, or the FK violates and the
    MENTIONS edge is skipped by L5a's skip-orphan guard.

    Formats handled (matching what _collect_group_archive_digests /
    _find_archive_digests produce):

    - 1:1 archive filename ("2026-04-01T17-27-00_reset_session.json")
      → sa:2026-04-01T17-27-00 — matches the format extract_structural_edges
        creates via _extract_archive_edges on the agent's archive_dir.
    - Live group chat ("group:<group_id>")
      → sa:<group_id>:live — stable per group across sleeps; same node
        accumulates MENTIONS over the group's lifetime.
    - Group archive ("group_archive:<group_id>:<filename>")
      → sa:<group_id>:<timestamp> — per-session node for past resets.

    Returns ("", "") for empty input. Callers must `_ensure_node` the
    returned id on the KG before passing it to persist_extracted_entities,
    since 1:1 sa: nodes are auto-created by extract_structural_edges but
    group sa: nodes are not.
    """
    if not archive:
        return "", ""
    if archive.startswith("group:"):
        group_id = archive[len("group:"):]
        return f"sa:{group_id}:live", group_id
    if archive.startswith("group_archive:"):
        rest = archive[len("group_archive:"):]
        if ":" in rest:
            group_id, filename = rest.split(":", 1)
            timestamp = Path(filename).stem.split("_")[0]
            return f"sa:{group_id}:{timestamp}", f"{group_id}/{timestamp}"
        return f"sa:{rest}", rest
    timestamp = Path(archive).stem.split("_")[0]
    return f"sa:{timestamp}", timestamp


def _result_filename(archive_file: str) -> str:
    """Stable result filename derived from `archive_file`.

    1:1 archive: `result_<archive_stem>.json` (e.g. `result_2026-05-13T17-28-25_reset_session.json`).
    Group archive: `result_group_archive--<group_id>--<archive_stem>.json` — `:`
    replaced by `--` for Windows path safety (drive separator). `_` is reserved
    for timestamp segments inside the archive name and group ids may contain
    underscores, so `--` is unambiguous as a namespace separator.
    """
    sanitized = archive_file.replace(":", "--")
    if sanitized.endswith(".json"):
        sanitized = sanitized[:-5]
    return f"result_{sanitized}.json"


def _compute_archive_hash(digest: Dict[str, Any], conversation_dir: Path) -> str:
    """sha256 hex of the raw archive bytes referenced by `digest`.

    Returns "" if the archive file cannot be located (orphan digest, missing
    file, read error). Callers treat "" as "skip cache, always re-analyze".
    """
    source = digest.get("source")
    archive_file = digest.get("archive_file", "")
    if source == "group_archive":
        path_str = digest.get("archive_path", "")
        path = Path(path_str) if path_str else None
    else:
        # 1:1 archive — search via rglob (matches what _load_archive does).
        path = None
        if archive_file:
            for p in (conversation_dir / "archive").rglob(archive_file):
                path = p
                break
    if path is None or not path.exists():
        return ""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def _migrate_legacy_results(results_dir: Path) -> int:
    """One-time rename: `session_<N>.json` → `result_<stem>.json`.

    Reads the `archive_file` field inside each legacy file to derive the new
    stable name. Idempotent: no-op when no `session_*.json` files exist, or
    when the corresponding `result_*.json` already exists (delete legacy).
    Returns the count of files renamed or deleted.
    """
    if not results_dir.exists():
        return 0
    migrated = 0
    for legacy_path in results_dir.glob("session_*.json"):
        stem = legacy_path.stem
        # match session_<digits> exactly — avoid stomping any unrelated file.
        if not (stem.startswith("session_") and stem[8:].isdigit()):
            continue
        try:
            data = json.loads(legacy_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        archive_file = data.get("archive_file", "")
        if not archive_file:
            continue
        new_path = results_dir / _result_filename(archive_file)
        try:
            if new_path.exists():
                legacy_path.unlink()
            else:
                legacy_path.rename(new_path)
            migrated += 1
        except OSError as e:
            log.warning("Sleep migration: %s → %s failed: %s", legacy_path.name, new_path.name, e)
    if migrated:
        log.info("Sleep migration: %d legacy session_*.json files mapped to result_*.json", migrated)
    return migrated


async def _analyze_single_session(
    digest: Dict, conversation_dir: Path, llm_manager,
    provider_alias: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    archive_file = digest.get("archive_file", "")

    if digest.get("source") == "group_archive":
        archive_path_str = digest.get("archive_path", "")
        if not archive_path_str or not Path(archive_path_str).exists():
            return None
        archive = json.loads(Path(archive_path_str).read_text(encoding="utf-8"))
    else:
        archive = _load_archive(conversation_dir, archive_file) if archive_file else None
    if not archive:
        return None

    messages_text = _format_archive_messages(archive)
    tools = ", ".join(digest.get("tool_stats", {}).keys()) or "none"

    prompt = PER_SESSION_PROMPT.format(
        date=digest.get("date", "unknown"),
        message_count=digest.get("message_count", 0),
        duration_mins=digest.get("duration_mins", 0),
        tools=tools,
        messages=messages_text,
    )

    response = await llm_manager.query(prompt, provider_alias=provider_alias)
    if not response or not response.strip():
        raise ValueError(
            "LLM returned empty response (extended thinking may have consumed all output tokens)"
        )
    try:
        finding = _parse_llm_json(response)
    except json.JSONDecodeError as err:
        _capture_unparsable(
            response, err,
            label="session-analysis",
            usage=_last_usage_of(llm_manager, provider_alias),
            dump_dir=conversation_dir / "sleep_results",
        )
        raise
    finding["archive_file"] = archive_file
    finding["digest_date"] = digest.get("date", "")
    finding["source"] = digest.get("source", "1:1")
    return finding


async def run_sleep(
    conversation_dir: Path, llm_manager, agent_id: str = "",
    force: bool = False, provider_alias: Optional[str] = None,
    progress_callback=None, group_id: Optional[str] = None,
) -> Dict[str, Any]:
    state = _read_sleep_state(conversation_dir)
    if state.get("status") == "sleeping":
        started = state.get("started_at")
        if started:
            try:
                started_dt = datetime.fromisoformat(started)
                if started_dt.tzinfo is None:
                    started_dt = started_dt.replace(tzinfo=timezone.utc)
                elapsed = (datetime.now(timezone.utc) - started_dt).total_seconds() / 60
                if elapsed > SLEEP_TIMEOUT_MINUTES:
                    log.warning("Stuck sleep detected for %s (%.0f min), resetting", agent_id, elapsed)
                else:
                    return {"status": "already_sleeping"}
            except (ValueError, TypeError):
                log.warning("Sleep state has invalid started_at for %s, resetting", agent_id)
        else:
            log.warning("Sleep state missing started_at for %s, resetting", agent_id)

    _write_sleep_state(conversation_dir, {
        "status": "sleeping",
        "started_at": datetime.now(timezone.utc).isoformat(),
    })

    # State is already "sleeping", so a duplicate trigger for THIS agent is
    # still deduplicated while we wait; the queue is only ever between
    # different agents. The "queued" report is advisory — checked before
    # acquire, so a race can miss it; the lock itself is the guarantee.
    if _SLEEP_PIPELINE_LOCK.locked():
        log.info("Sleep pipeline for %s queued behind a running one",
                 agent_id or conversation_dir.name)
        if progress_callback:
            try:
                await progress_callback(0, 0, "queued", "")
            except Exception:
                pass
    await _SLEEP_PIPELINE_LOCK.acquire()
    try:
        results_dir = conversation_dir / "sleep_results"
        results_dir.mkdir(exist_ok=True)
        # One-shot rename of legacy `session_<N>.json` → `result_<stem>.json`.
        # Idempotent — no-op after the first cycle that runs the new code path.
        _migrate_legacy_results(results_dir)

        if group_id:
            # Group-only mode: read only from this group's archives
            conversations_dir = conversation_dir.parent
            group_dir = None
            for d in conversations_dir.iterdir():
                if d.is_dir() and d.name.startswith(group_id):
                    group_dir = d
                    break
            if not group_dir:
                _write_sleep_state(conversation_dir, {"status": "awake"})
                return {"status": "error", "message": f"Group {group_id} not found"}
            digests = _collect_group_archive_digests(group_dir, agent_id)
        else:
            digests = _find_archive_digests(conversation_dir)

            # Group archives — immutable, dedup'd by sha256 below.
            # Live group history is intentionally NOT analyzed: it grows on every
            # message, so the per-archive hash key never converges. Group analysis
            # happens at reset_session points via group_archive entries.
            conversations_dir = conversation_dir.parent
            for group_dir in conversations_dir.iterdir():
                if group_dir.is_dir() and group_dir.name.startswith("group-"):
                    archive_digests = _collect_group_archive_digests(group_dir, agent_id)
                    if archive_digests:
                        digests.extend(archive_digests)
                        log.info("Sleep pipeline: added %d group archive sessions from %s", len(archive_digests), group_dir.name)

        if not digests:
            _write_sleep_state(conversation_dir, {"status": "awake"})
            return {"status": "no_new_sessions", "sessions_analyzed": 0}

        # Sort ascending by `date` so downstream code can rely on chronological
        # order: `period` uses dates[0]/dates[-1], and `most_recent_finding`
        # below picks max(digest_date). Empty/missing dates sort first
        # (= oldest treatment).
        digests.sort(key=lambda d: d.get("date", ""))

        log.info("Sleep pipeline: %d candidate sessions for %s", len(digests), agent_id or conversation_dir.name)

        per_session_findings = []
        cached_count = 0
        analyzed_count = 0
        total = len(digests)
        for i, digest in enumerate(digests):
            archive_file = digest.get("archive_file", "")
            archive_hash = _compute_archive_hash(digest, conversation_dir)
            result_path = results_dir / _result_filename(archive_file) if archive_file else None

            # Hash-skip path: result exists with matching archive_hash → reuse,
            # no LLM call. Empty hash (missing archive bytes) bypasses cache and
            # re-analyzes; force=True also bypasses cache (manual re-run after
            # prompt change).
            if (not force and result_path is not None and result_path.exists()
                    and archive_hash):
                try:
                    cached = json.loads(result_path.read_text(encoding="utf-8"))
                    if cached.get("archive_hash") == archive_hash:
                        per_session_findings.append(cached)
                        cached_count += 1
                        if progress_callback:
                            await progress_callback(i, total, "cached", archive_file)
                        continue
                except (json.JSONDecodeError, OSError):
                    pass  # fall through to re-analyze

            log.info("Sleep: analyzing session %d/%d (%s)", i + 1, total, archive_file)
            if progress_callback:
                await progress_callback(i, total, "analyzing", archive_file)
            try:
                finding = await _analyze_single_session(digest, conversation_dir, llm_manager, provider_alias=provider_alias)
                if finding:
                    if archive_hash:
                        finding["archive_hash"] = archive_hash
                    per_session_findings.append(finding)
                    analyzed_count += 1
                    if result_path is not None:
                        result_path.write_text(json.dumps(finding, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as e:
                err_desc = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
                log.warning("Sleep: failed to analyze session %d: %s", i + 1, err_desc)
                per_session_findings.append({"error": err_desc, "archive_file": archive_file})

        log.info("Sleep pipeline: %d sessions ready (%d analyzed, %d cached)",
                 len(per_session_findings), analyzed_count, cached_count)

        if not per_session_findings:
            _write_sleep_state(conversation_dir, {"status": "awake"})
            return {"status": "no_analyzable_sessions", "sessions_analyzed": 0}

        # ADR-024 Phase 2: GLiNER entity extraction (before LLM synthesis)
        gliner_entities: list = []
        try:
            from .knowledge_graph import KnowledgeGraph, NodeType
            from .utils import get_agent_root
            _agent_root = get_agent_root(agent_id) if agent_id else conversation_dir.parent.parent / "agents" / conversation_dir.name
            _kg = KnowledgeGraph(_agent_root)
            _ner_texts = []
            _session_nodes = []  # (source_id, label) for SessionArchive nodes to ensure
            for f in per_session_findings:
                if "error" not in f:
                    summary = f.get("summary", "") or json.dumps(f, ensure_ascii=False)[:3000]
                    archive = f.get("archive_file", "")
                    source_id, label = _build_session_source_id(archive)
                    if source_id:
                        _session_nodes.append((source_id, label))
                    _ner_texts.append({"source_id": source_id, "text": summary})
            # L5b: group sessions don't go through extract_structural_edges, so
            # their sa: nodes don't exist in the graph by default. Ensure them
            # here so persist_extracted_entities can attach MENTIONS edges
            # instead of dropping them via skip-orphan.
            for source_id, label in _session_nodes:
                _kg._ensure_node(source_id, NodeType.SESSION_ARCHIVE, label)
            if _ner_texts:
                # Run GLiNER inference in a worker thread — GLiNER.from_pretrained()
                # may trigger a synchronous HF model download on first use that
                # blocks the event loop for minutes and stalls Discord
                # heartbeats / WebSocket auth (S111 incident). Subsequent
                # calls (model cached) are fast but still CPU-bound, so
                # offloading keeps the loop responsive either way.
                #
                # SQLite writes must run on the main thread (the connection's
                # owner), so persist_extracted_entities() is called here, not
                # inside the worker.
                gliner_entities = await asyncio.to_thread(_kg.extract_entities_gliner, _ner_texts)
                if gliner_entities:
                    edges_added = _kg.persist_extracted_entities(gliner_entities)
                    log.info("Sleep pipeline: GLiNER extracted %d entities (%d edges) from %d sessions",
                             len(gliner_entities), edges_added, len(_ner_texts))
        except Exception as e:
            log.debug("Sleep pipeline: GLiNER entity extraction skipped: %s", e)

        dates = [d.get("date", "") for d in digests if d.get("date")]
        period = f"{dates[0][:10]} to {dates[-1][:10]}" if len(dates) >= 2 else dates[0][:10] if dates else "unknown"

        # Pre-compute most-recent finding deterministically so the LLM only
        # formats it, not picks it. Without this, the LLM picks `last_session`
        # by position in the prompt rather than chronology, and the result
        # was a 3-day-stale trivial group session over fresh 1:1 work.
        non_error_findings = [f for f in per_session_findings if "error" not in f]
        findings_sorted_desc = sorted(
            non_error_findings,
            key=lambda f: f.get("digest_date", ""),
            reverse=True,
        )
        most_recent_finding = findings_sorted_desc[0] if findings_sorted_desc else None
        most_recent_text = (
            json.dumps(most_recent_finding, ensure_ascii=False, indent=2)
            if most_recent_finding else "(no analyzable session)"
        )

        if progress_callback:
            await progress_callback(total, total, "synthesizing", "")

        entity_section = ""
        if gliner_entities:
            unique_entities = sorted({e["entity"] for e in gliner_entities})
            entity_section = ENTITY_RELATION_SECTION.format(
                entity_list=", ".join(unique_entities)
            )

        # Adaptive synthesis budget (S119): pack findings newest-to-oldest until
        # SYNTHESIS_BUDGET_FACTOR * model context_window is reached. Bounded by
        # the actual provider model, not a magic constant — caps growth at
        # ~200-350 sessions without crashing into context limits.
        target_alias = provider_alias or llm_manager.default_provider
        target_provider = llm_manager.providers.get(target_alias) if target_alias else None
        target_model = target_provider.model if target_provider else None

        if target_model:
            context_window = llm_manager.get_context_window(target_model)
        else:
            context_window = 128000  # safe default for unknown providers
            log.warning(
                "Sleep synthesis: target model unknown (alias=%s), using default context_window=%d",
                target_alias, context_window,
            )

        # Estimate template overhead — SYNTHESIS_PROMPT formatted with empty
        # findings + the most_recent block + entity_section. Anything we
        # already commit to including before the variable findings_text.
        template_skeleton = SYNTHESIS_PROMPT.format(
            n=len(per_session_findings),
            period=period,
            most_recent=most_recent_text,
            findings="",
            entity_section=entity_section,
        )
        if target_model:
            template_overhead_tokens = llm_manager.count_tokens(template_skeleton, target_model)
        else:
            template_overhead_tokens = len(template_skeleton) // 4

        selected_findings, tokens_used = _select_findings_within_budget(
            findings_sorted_desc,
            llm_manager,
            target_model,
            context_window,
            template_overhead_tokens,
        )

        budget_available = _compute_synthesis_budget(context_window, template_overhead_tokens)
        synthesis_budget_info = {
            "total_findings": len(findings_sorted_desc),
            "included_findings": len(selected_findings),
            "oldest_included_date": (
                selected_findings[-1].get("digest_date", "")[:10] if selected_findings else ""
            ),
            "budget_tokens_used": tokens_used,
            "budget_tokens_available": budget_available,
        }

        log.info(
            "Sleep synthesis budget: %d/%d findings, %d/%d tokens (context_window=%d, model=%s)",
            len(selected_findings), len(findings_sorted_desc),
            tokens_used, budget_available, context_window, target_model or "unknown",
        )

        findings_text = "\n\n".join(
            _render_finding_block(i + 1, f) for i, f in enumerate(selected_findings)
        )

        synthesis_prompt = SYNTHESIS_PROMPT.format(
            n=len(selected_findings),
            period=period,
            most_recent=most_recent_text,
            findings=findings_text,
            entity_section=entity_section,
        )

        response = await llm_manager.query(
            synthesis_prompt,
            provider_alias=provider_alias,
            timeout=SYNTHESIS_TIMEOUT_SECONDS,
            # The ceiling this call reserved for itself, and the cap that keeps the
            # think block from spending it. A provider that does not read either
            # ignores them; the local one reads both, and it is the one that was
            # writing half a brief.
            **_synthesis_request_limits(context_window),
        )
        if not response or not response.strip():
            raise ValueError(
                "LLM returned empty response (extended thinking may have consumed all output tokens)"
            )
        try:
            result = _parse_llm_json(response)
        except json.JSONDecodeError as err:
            _capture_unparsable(
                response, err,
                label="synthesis",
                usage=_last_usage_of(llm_manager, provider_alias),
                dump_dir=results_dir,
            )
            raise

        morning_brief = result.get("morning_brief", {})
        sleep_findings = result.get("sleep_findings", {})

        extracted_relations = result.get("extracted_relations", [])
        if extracted_relations and gliner_entities:
            try:
                from .knowledge_graph import KnowledgeGraph, GraphEdge, EdgeType, NodeType, _utc_now
                from .utils import get_agent_root
                _agent_root = get_agent_root(agent_id) if agent_id else conversation_dir.parent.parent / "agents" / conversation_dir.name
                _kg = KnowledgeGraph(_agent_root)
                now = _utc_now()
                added = 0
                for rel in extracted_relations:
                    conf = rel.get("confidence", 0)
                    justification = rel.get("justification", "")
                    if conf < 0.7 or len(justification) < 20:
                        continue
                    source = rel.get("source", "").lower().replace(" ", "_")
                    target = rel.get("target", "").lower().replace(" ", "_")
                    rel_type = rel.get("relation_type", "SUPPORTS")
                    try:
                        edge_type = EdgeType(rel_type)
                    except ValueError:
                        edge_type = EdgeType.SUPPORTS
                    src_id = f"e:{source}"
                    tgt_id = f"e:{target}"
                    _kg._ensure_node(src_id, NodeType.ENTITY, rel.get("source", source))
                    _kg._ensure_node(tgt_id, NodeType.ENTITY, rel.get("target", target))
                    props = {"source": "llm_relation"}
                    if rel.get("needs_review"):
                        props["needs_review"] = True
                    if _kg._add_edge_safe(src_id, tgt_id, edge_type, justification, now, props):
                        added += 1
                if added or extracted_relations:
                    log.info("Sleep pipeline: LLM relations — %d new of %d proposed", added, len(extracted_relations))
            except Exception as e:
                # WARNING, not debug. This is the only writer of the only edge class
                # with no source outside the graph — 40.8% of the fleet's edges and
                # growing. If it dies (a provider alias rots, the response shape
                # changes) everything around it still reports success: sleep completes,
                # the brief renders, the backup dumps a graph that has quietly stopped
                # growing. GLM 5.2 called this the single point where this whole
                # architecture can fail invisibly forever.
                log.warning("Sleep pipeline: LLM relation extraction failed — the "
                            "irreplaceable edge class gained nothing this cycle: %s", e,
                            exc_info=True)

        morning_brief["generated_at"] = datetime.now(timezone.utc).isoformat()
        morning_brief["consumed"] = False
        morning_brief["synthesis_budget"] = synthesis_budget_info
        sleep_findings["generated_at"] = datetime.now(timezone.utc).isoformat()

        if not group_id:
            (conversation_dir / MORNING_BRIEF_FILE).write_text(
                json.dumps(morning_brief, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        (conversation_dir / SLEEP_FINDINGS_FILE).write_text(
            json.dumps(sleep_findings, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        _write_sleep_state(conversation_dir, {
            "status": "awake",
            "last_completed": datetime.now(timezone.utc).isoformat(),
            "sessions_analyzed": len(digests),
        })

        try:
            from .consolidation import tier1_consolidate
            agent_name = agent_id or conversation_dir.name
            knowledge_dir = conversation_dir.parent.parent / "agents" / agent_name / "knowledge"
            if knowledge_dir.is_dir():
                consolidation_result = tier1_consolidate(knowledge_dir)
                log.info("Sleep: tier1 consolidation — %d stale of %d files", consolidation_result.get("stale_marked", 0), consolidation_result.get("total", 0))
        except Exception as e:
            log.warning("Sleep: tier1 consolidation failed (non-fatal): %s", e)

        # The graph's backup, taken where it belongs: right after the one writer that
        # produces edges nothing can rebuild. A dump only ever taken by hand is a dump
        # that exists until someone forgets, and this class is 40.8% of the fleet's
        # edges — 57.2% on warren — with no source outside the store.
        #
        # Sleep is the moment: the run has just finished writing, no pass is clearing
        # structural edges underneath, and the agent is by definition not busy.
        # Non-fatal by construction — a failed backup must never cost a night's work.
        try:
            _export_graph_snapshot(agent_id, conversation_dir)
        except Exception as e:
            log.warning("Sleep: knowledge graph export failed (non-fatal): %s", e)

        log.info("Sleep pipeline complete: %d sessions analyzed, morning_brief.json written", len(digests))

        return {
            "status": "completed",
            "sessions_analyzed": len(digests),
            "morning_brief": morning_brief,
        }

    except Exception as e:
        err_desc = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        log.error("Sleep pipeline failed: %s", err_desc, exc_info=True)
        _write_sleep_state(conversation_dir, {
            "status": "awake",
            "last_error": err_desc,
            "error_at": datetime.now(timezone.utc).isoformat(),
        })
        return {"status": "error", "error": err_desc}
    finally:
        _SLEEP_PIPELINE_LOCK.release()
