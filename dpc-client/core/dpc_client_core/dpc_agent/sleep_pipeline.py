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
    finding = _parse_llm_json(response)
    finding["archive_file"] = archive_file
    finding["digest_date"] = digest.get("date", "")
    finding["source"] = digest.get("source", "1:1")
    return finding


async def run_sleep(
    conversation_dir: Path, llm_manager, agent_id: str = "",
    force: bool = False, provider_alias: Optional[str] = None,
    progress_callback=None, group_id: Optional[str] = None,
) -> Dict[str, Any]:
    SLEEP_TIMEOUT_MINUTES = 30
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

        findings_text = "\n\n".join(
            f"Session {i+1} ({f.get('digest_date', '')[:10]}):\n{json.dumps(f, ensure_ascii=False, indent=2)}"
            for i, f in enumerate(findings_sorted_desc)
        )

        if progress_callback:
            await progress_callback(total, total, "synthesizing", "")

        entity_section = ""
        if gliner_entities:
            unique_entities = sorted({e["entity"] for e in gliner_entities})
            entity_section = ENTITY_RELATION_SECTION.format(
                entity_list=", ".join(unique_entities)
            )

        synthesis_prompt = SYNTHESIS_PROMPT.format(
            n=len(per_session_findings),
            period=period,
            most_recent=most_recent_text,
            findings=findings_text,
            entity_section=entity_section,
        )

        response = await llm_manager.query(synthesis_prompt, provider_alias=provider_alias)
        result = _parse_llm_json(response)

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
                    _kg._add_edge_safe(src_id, tgt_id, edge_type, justification, now, props)
                    added += 1
                if added:
                    log.info("Sleep pipeline: LLM extracted %d relations (from %d candidates)", added, len(extracted_relations))
            except Exception as e:
                log.debug("Sleep pipeline: LLM relation extraction failed: %s", e)

        morning_brief["generated_at"] = datetime.now(timezone.utc).isoformat()
        morning_brief["consumed"] = False
        sleep_findings["generated_at"] = datetime.now(timezone.utc).isoformat()

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
