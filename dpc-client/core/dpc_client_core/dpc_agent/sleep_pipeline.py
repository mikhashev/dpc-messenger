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

        if not digests:
            _write_sleep_state(conversation_dir, {"status": "awake"})
            return {"status": "no_new_sessions", "sessions_analyzed": 0}

        log.info("Sleep pipeline: analyzing %d sessions for %s", len(digests), agent_id or conversation_dir.name)

        results_dir = conversation_dir / "sleep_results"
        results_dir.mkdir(exist_ok=True)

        per_session_findings = []
        for i, digest in enumerate(digests):
            log.info("Sleep: analyzing session %d/%d (%s)", i + 1, len(digests), digest.get("archive_file", ""))
            try:
                finding = await _analyze_single_session(digest, conversation_dir, llm_manager, provider_alias=provider_alias)
                if finding:
                    per_session_findings.append(finding)
                    result_path = results_dir / f"session_{i}.json"
                    result_path.write_text(json.dumps(finding, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as e:
                log.warning("Sleep: failed to analyze session %d: %s", i + 1, e)
                per_session_findings.append({"error": str(e), "archive_file": digest.get("archive_file", "")})

        if not per_session_findings:
            _write_sleep_state(conversation_dir, {"status": "awake"})
            return {"status": "no_analyzable_sessions", "sessions_analyzed": 0}

        dates = [d.get("date", "") for d in digests if d.get("date")]
        period = f"{dates[0][:10]} to {dates[-1][:10]}" if len(dates) >= 2 else dates[0][:10] if dates else "unknown"

        findings_text = "\n\n".join(
            f"Session {i+1} ({f.get('digest_date', '')[:10]}):\n{json.dumps(f, ensure_ascii=False, indent=2)}"
            for i, f in enumerate(per_session_findings) if "error" not in f
        )

        synthesis_prompt = SYNTHESIS_PROMPT.format(
            n=len(per_session_findings),
            period=period,
            findings=findings_text,
        )

        response = await llm_manager.query(synthesis_prompt, provider_alias=provider_alias)
        result = _parse_llm_json(response)

        morning_brief = result.get("morning_brief", {})
        sleep_findings = result.get("sleep_findings", {})

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

        log.info("Sleep pipeline complete: %d sessions analyzed, morning_brief.json written", len(digests))

        return {
            "status": "completed",
            "sessions_analyzed": len(digests),
            "morning_brief": morning_brief,
        }

    except Exception as e:
        log.error("Sleep pipeline failed: %s", e, exc_info=True)
        _write_sleep_state(conversation_dir, {
            "status": "awake",
            "last_error": str(e),
            "error_at": datetime.now(timezone.utc).isoformat(),
        })
        return {"status": "error", "error": str(e)}
