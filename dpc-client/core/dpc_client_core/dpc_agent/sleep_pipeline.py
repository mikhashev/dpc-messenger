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

RETROSPECTIVE_PROMPT = """\
You are performing a retrospective analysis of recent conversation sessions \
for an AI agent. You have access to structured session data below.

For each session you receive:
- Digest (metadata: duration, message count, tools used, participants)
- Full session archive (all messages including tool calls and decisions)

Analyze these sessions and produce a JSON object with two top-level keys:

"morning_brief": {{
  "sessions_analyzed": N,
  "period": "DATE to DATE",
  "key_decisions": [{{"decision": "...", "session": "S##", "rationale": "..."}}],
  "patterns_noticed": [{{"pattern": "...", "evidence": "S##, S##"}}],
  "unresolved": [{{"topic": "...", "context": "...", "session": "S##"}}],
  "summary": "2-3 sentence human-readable summary"
}}

"sleep_findings": {{
  "behavioral_observations": [{{"observation": "...", "frequency": "...", "significance": "low|medium|high"}}],
  "tool_usage_patterns": [{{"tool": "...", "observation": "..."}}],
  "recurring_topics": [{{"topic": "...", "sessions": ["S##"], "progress": "advancing|stalled|repeating"}}],
  "suggested_focus": ["area1", "area2"]
}}

Guidelines:
- Be factual, not flattering. If sessions were unproductive, say so.
- Reference session IDs for traceability.
- Focus on CHANGE: what is new, what shifted, what was decided differently.
- If a decision was reversed in a later session, flag it.
- Language: match the language of the sessions.
- Respond with ONLY the JSON object, no markdown fences.

--- SESSION DATA ---
{session_data}
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


def _build_session_data(digests: List[Dict], conversation_dir: Path) -> str:
    parts = []
    for i, digest in enumerate(digests):
        archive_file = digest.get("archive_file", "")
        archive = _load_archive(conversation_dir, archive_file) if archive_file else None

        part = f"\n=== Session {i+1} ===\n"
        part += f"Date: {digest.get('date', 'unknown')}\n"
        part += f"Messages: {digest.get('message_count', 0)}, Duration: {digest.get('duration_mins', 0)} min\n"
        part += f"Tools: {', '.join(digest.get('tool_stats', {}).keys()) or 'none'}\n"

        if archive:
            messages = archive.get("messages", [])
            for msg in messages[:100]:
                role = msg.get("role", "")
                sender = msg.get("sender_name", role)
                content = msg.get("content", "")[:500]
                if content:
                    part += f"[{sender}]: {content}\n"
        else:
            previews = digest.get("user_message_previews", [])
            if previews:
                part += f"Previews: {'; '.join(previews[:3])}\n"

        parts.append(part)

    return "\n".join(parts)


async def run_sleep(conversation_dir: Path, llm_manager, agent_id: str = "") -> Dict[str, Any]:
    state = _read_sleep_state(conversation_dir)
    if state.get("status") == "sleeping":
        return {"status": "already_sleeping"}

    _write_sleep_state(conversation_dir, {
        "status": "sleeping",
        "started_at": datetime.now(timezone.utc).isoformat(),
    })

    try:
        since = _get_last_sleep_timestamp(conversation_dir)
        digests = _find_unprocessed_archives(conversation_dir, since)

        if not digests:
            _write_sleep_state(conversation_dir, {"status": "awake"})
            return {"status": "no_new_sessions", "sessions_analyzed": 0}

        log.info("Sleep pipeline: analyzing %d sessions for %s", len(digests), agent_id or conversation_dir.name)

        session_data = _build_session_data(digests, conversation_dir)
        prompt = RETROSPECTIVE_PROMPT.format(session_data=session_data)

        response = await llm_manager.query(prompt)

        try:
            result = json.loads(response)
        except json.JSONDecodeError:
            cleaned = response.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0]
            result = json.loads(cleaned)

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
