"""Sleep Consolidation Pipeline (ADR-014).

Reads session digests + archives, performs LLM retrospective analysis,
writes morning_brief.json + sleep_findings.json. Triggered by UI
toggle button via WebSocket command.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

MAX_ARCHIVES_PER_CYCLE = 20
SLEEP_STATE_FILE = "sleep_state.json"
LAST_SLEEP_FILE = "last_sleep.json"
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
- **last_session**: Extract from the MOST RECENT session only. List concrete \
carryover items — tasks mentioned but not completed, decisions deferred, \
things explicitly "pending" or "carryover".
- Focus on CROSS-SESSION patterns: what changed, what reversed, what repeated.
- Be factual. If sessions were unproductive, say so.
- Language: match the language of the sessions.

--- PER-SESSION FINDINGS ---
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


def _get_last_sleep_timestamp(conversation_dir: Path) -> Optional[str]:
    path = conversation_dir / LAST_SLEEP_FILE
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("last_sleep_at")
        except (json.JSONDecodeError, OSError):
            pass
    return None


def _collect_group_digests(
    conversations_dir: Path, agent_id: str, since: Optional[str]
) -> List[Dict[str, Any]]:
    """Collect group chat history segments where this agent participated."""
    digests = []
    if not conversations_dir.exists() or not agent_id:
        return digests
    for group_dir in conversations_dir.iterdir():
        if not group_dir.is_dir() or not group_dir.name.startswith("group-"):
            continue
        metadata_path = group_dir / "metadata.json"
        history_path = group_dir / "history.json"
        if not metadata_path.exists() or not history_path.exists():
            continue
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            agents_map = metadata.get("agents", {})
            agent_in_group = any(agent_id in ids for ids in agents_map.values())
            if not agent_in_group and agents_map:
                continue
            history = json.loads(history_path.read_text(encoding="utf-8"))
            messages = history.get("messages", [])
            if not messages:
                continue
            if since:
                messages = [m for m in messages if m.get("timestamp", "") > since]
            if not messages:
                continue
            digests.append({
                "archive_file": f"group:{group_dir.name}",
                "date": messages[0].get("timestamp", "")[:10],
                "message_count": len(messages),
                "duration_mins": 0,
                "source": "group",
                "group_id": metadata.get("group_id", group_dir.name),
            })
        except (json.JSONDecodeError, OSError):
            continue
    return digests


def _find_unprocessed_archives(conversation_dir: Path, since: Optional[str]) -> List[Dict[str, Any]]:
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

    if since:
        digests = [d for d in digests if d.get("date", "") > since]

    digests.sort(key=lambda d: d.get("date", ""))
    return digests[-MAX_ARCHIVES_PER_CYCLE:]


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


async def _analyze_single_session(
    digest: Dict, conversation_dir: Path, llm_manager,
    provider_alias: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    archive_file = digest.get("archive_file", "")

    # ADR-023 Task 10: group chat source — load from history.json directly
    if digest.get("source") == "group":
        group_dir = conversation_dir.parent / archive_file.replace("group:", "")
        history_path = group_dir / "history.json"
        if not history_path.exists():
            return None
        archive = json.loads(history_path.read_text(encoding="utf-8"))
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
    return finding


async def run_sleep(
    conversation_dir: Path, llm_manager, agent_id: str = "",
    force: bool = False, provider_alias: Optional[str] = None,
    progress_callback=None,
) -> Dict[str, Any]:
    state = _read_sleep_state(conversation_dir)
    if state.get("status") == "sleeping":
        return {"status": "already_sleeping"}

    _write_sleep_state(conversation_dir, {
        "status": "sleeping",
        "started_at": datetime.now(timezone.utc).isoformat(),
    })

    try:
        since = None if force else _get_last_sleep_timestamp(conversation_dir)
        digests = _find_unprocessed_archives(conversation_dir, since)

        # ADR-023 Task 10: include group chat sessions where this agent participates
        group_digests = _collect_group_digests(conversation_dir.parent, agent_id, since)
        if group_digests:
            digests.extend(group_digests)
            log.info("Sleep pipeline: added %d group chat segments", len(group_digests))

        if not digests:
            _write_sleep_state(conversation_dir, {"status": "awake"})
            return {"status": "no_new_sessions", "sessions_analyzed": 0}

        log.info("Sleep pipeline: analyzing %d sessions for %s", len(digests), agent_id or conversation_dir.name)

        results_dir = conversation_dir / "sleep_results"
        results_dir.mkdir(exist_ok=True)

        per_session_findings = []
        total = len(digests)
        for i, digest in enumerate(digests):
            log.info("Sleep: analyzing session %d/%d (%s)", i + 1, total, digest.get("archive_file", ""))
            if progress_callback:
                await progress_callback(i, total, "analyzing", digest.get("archive_file", ""))
            try:
                finding = await _analyze_single_session(digest, conversation_dir, llm_manager, provider_alias=provider_alias)
                if finding:
                    per_session_findings.append(finding)
                    result_path = results_dir / f"session_{i}.json"
                    result_path.write_text(json.dumps(finding, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as e:
                err_desc = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
                log.warning("Sleep: failed to analyze session %d: %s", i + 1, err_desc)
                per_session_findings.append({"error": err_desc, "archive_file": digest.get("archive_file", "")})

        if not per_session_findings:
            _write_sleep_state(conversation_dir, {"status": "awake"})
            return {"status": "no_analyzable_sessions", "sessions_analyzed": 0}

        # ADR-024 Phase 2: GLiNER entity extraction (before LLM synthesis)
        gliner_entities: list = []
        try:
            from .knowledge_graph import KnowledgeGraph
            from .utils import get_agent_root
            _agent_root = get_agent_root(agent_id) if agent_id else conversation_dir.parent.parent / "agents" / conversation_dir.name
            _kg = KnowledgeGraph(_agent_root)
            _ner_texts = []
            for f in per_session_findings:
                if "error" not in f:
                    summary = f.get("summary", "") or json.dumps(f, ensure_ascii=False)[:3000]
                    archive = f.get("archive_file", "")
                    _ner_texts.append({"source_id": f"sa:{Path(archive).stem.split('_')[0]}" if archive else "", "text": summary})
            if _ner_texts:
                gliner_entities = _kg.extract_entities_gliner(_ner_texts)
                if gliner_entities:
                    log.info("Sleep pipeline: GLiNER extracted %d entities from %d sessions", len(gliner_entities), len(_ner_texts))
        except Exception as e:
            log.debug("Sleep pipeline: GLiNER entity extraction skipped: %s", e)

        dates = [d.get("date", "") for d in digests if d.get("date")]
        period = f"{dates[0][:10]} to {dates[-1][:10]}" if len(dates) >= 2 else dates[0][:10] if dates else "unknown"

        findings_text = "\n\n".join(
            f"Session {i+1} ({f.get('digest_date', '')[:10]}):\n{json.dumps(f, ensure_ascii=False, indent=2)}"
            for i, f in enumerate(per_session_findings) if "error" not in f
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
                    props = {"auto": True, "llm_extracted": True}
                    if rel.get("needs_review"):
                        props["needs_review"] = True
                    _kg._add_edge_safe(src_id, tgt_id, edge_type, justification, now)
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
        (conversation_dir / LAST_SLEEP_FILE).write_text(
            json.dumps({"last_sleep_at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False),
            encoding="utf-8",
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
