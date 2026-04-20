"""Decision proposals pipeline (ADR-010, MEM-4.2).

Structured extraction of decisions/knowledge from agent responses.
Writes to decision_proposals.jsonl (DRAFT status).
Reserved fields for ARCH-20 forward-compatibility.
"""

from __future__ import annotations

import json
import logging
import pathlib
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from .utils import utc_now_iso

if TYPE_CHECKING:
    from .llm_adapter import DpcLlmAdapter

log = logging.getLogger(__name__)


@dataclass
class DecisionEntry:
    ts: str = ""
    topic: str = ""
    decision: str = ""
    rationale: str = ""
    participants: List[str] = field(default_factory=list)
    source_session: str = ""
    status: str = "active"
    supersedes: Optional[str] = None
    parent_id: Optional[str] = None


@dataclass
class DecisionProposal:
    entries: List[DecisionEntry] = field(default_factory=list)
    created_at: str = ""
    trigger_reasons: List[str] = field(default_factory=list)
    status: str = "DRAFT"


def create_proposal(
    entries: List[dict],
    trigger_reasons: List[str],
    session_id: str = "",
) -> DecisionProposal:
    """Create a decision proposal from extracted entries."""
    parsed = []
    for e in entries:
        known = {k for k in DecisionEntry.__dataclass_fields__}
        entry = DecisionEntry(**{k: v for k, v in e.items() if k in known})
        if not entry.ts:
            entry.ts = utc_now_iso()
        if session_id and not entry.source_session:
            entry.source_session = session_id
        parsed.append(entry)

    return DecisionProposal(
        entries=parsed,
        created_at=utc_now_iso(),
        trigger_reasons=trigger_reasons,
    )


def save_proposal(proposal: DecisionProposal, proposals_path: pathlib.Path) -> None:
    """Append proposal to decision_proposals.jsonl (DRAFT)."""
    proposals_path.parent.mkdir(parents=True, exist_ok=True)
    with open(proposals_path, "a", encoding="utf-8") as f:
        data = asdict(proposal)
        f.write(json.dumps(data, ensure_ascii=False) + "\n")
    log.info("Saved decision proposal with %d entries", len(proposal.entries))


def load_proposals(proposals_path: pathlib.Path) -> List[DecisionProposal]:
    """Load all DRAFT proposals from decision_proposals.jsonl."""
    if not proposals_path.exists():
        return []
    proposals = []
    for line in proposals_path.read_text(encoding="utf-8").strip().split("\n"):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            entries = [
                DecisionEntry(**{k: v for k, v in e.items() if k in DecisionEntry.__dataclass_fields__})
                for e in data.get("entries", [])
            ]
            proposals.append(DecisionProposal(
                entries=entries,
                created_at=data.get("created_at", ""),
                trigger_reasons=data.get("trigger_reasons", []),
                status=data.get("status", "DRAFT"),
            ))
        except (json.JSONDecodeError, TypeError):
            continue
    return proposals


def approve_proposal(
    proposal: DecisionProposal,
    journal_path: pathlib.Path,
) -> int:
    """Move approved entries to decision_journal.jsonl (permanent)."""
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with open(journal_path, "a", encoding="utf-8") as f:
        for entry in proposal.entries:
            f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
            count += 1
    proposal.status = "APPROVED"
    log.info("Approved %d decision entries to journal", count)
    return count


EXTRACTION_PROMPT = """\
You are a decision extractor. Analyze the conversation below and extract \
concrete decisions, conclusions, or commitments that were made.

Return a JSON array of objects. Each object must have exactly these fields:
- "topic": short label (3-8 words)
- "decision": what was decided (1-2 sentences)
- "rationale": why it was decided (1 sentence, or empty string)
- "participants": list of participant names mentioned

Rules:
- Extract only DECISIONS (agreed actions, chosen approaches, confirmed facts).
- Skip questions, speculations, and unresolved discussions.
- Maximum 5 entries. If more exist, keep the most significant ones.
- Return [] if no clear decisions were made.
- Return ONLY the JSON array, no markdown fencing, no commentary.

Conversation:
{conversation}
"""

MAX_PROPOSALS_PER_SESSION = 5


def _dedup_entries(
    new_entries: List[dict], existing_proposals: List[DecisionProposal]
) -> tuple:
    """Remove entries whose topics already exist in prior proposals.

    Returns (unique_entries, suppressed_count).
    Uses both proposal-level checksum (catches fully duplicated batches)
    and entry-level topic matching (catches individual duplicate entries).
    """
    new_topics = frozenset(e.get("topic", "").lower().strip() for e in new_entries)
    for p in existing_proposals:
        existing_topics = frozenset(e.topic.lower().strip() for e in p.entries)
        if new_topics == existing_topics:
            return [], len(new_entries)

    existing_all_topics: set = set()
    for p in existing_proposals:
        for e in p.entries:
            existing_all_topics.add(e.topic.lower().strip())

    unique = [e for e in new_entries if e.get("topic", "").lower().strip() not in existing_all_topics]
    return unique, len(new_entries) - len(unique)


async def extract_decisions(
    llm: "DpcLlmAdapter",
    conversation_messages: List[Dict[str, Any]],
    trigger_events: List[dict],
    agent_root: pathlib.Path,
    session_id: str = "",
) -> Optional[DecisionProposal]:
    """Extract decisions from conversation via LLM and save as DRAFT proposal.

    Called asynchronously after the agent loop completes.
    Non-blocking — errors are caught and logged.
    """
    if not trigger_events:
        return None

    proposals_path = agent_root / "state" / "decision_proposals.jsonl"
    existing = load_proposals(proposals_path)
    session_count = sum(
        1 for p in existing
        if p.status == "DRAFT"
    )
    if session_count >= MAX_PROPOSALS_PER_SESSION:
        log.debug("Session proposal limit reached (%d), skipping extraction",
                   MAX_PROPOSALS_PER_SESSION)
        return None

    last_n = conversation_messages[-20:]
    conv_text = "\n".join(
        f"[{m.get('role', '?')}] {(m.get('content') or '')[:500]}"
        for m in last_n
        if m.get("role") in ("user", "assistant") and m.get("content")
    )
    if not conv_text.strip():
        return None

    prompt = EXTRACTION_PROMPT.format(conversation=conv_text)

    try:
        response, _ = await llm.chat(
            messages=[{"role": "user", "content": prompt}],
            tools=None,
            max_tokens=1500,
            background=True,
        )
        raw = (response or {}).get("content", "")
        if not raw or not raw.strip():
            return None

        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]

        entries = json.loads(raw)
        if not isinstance(entries, list) or not entries:
            return None

        trigger_reasons = []
        for te in trigger_events:
            trigger_reasons.extend(te.get("reasons", []))
        trigger_reasons = list(set(trigger_reasons))

        entries, suppressed = _dedup_entries(entries, existing)
        if not entries:
            log.info("All %d extracted entries were duplicates, skipping proposal", suppressed)
            return None

        proposal = create_proposal(entries, trigger_reasons, session_id)
        save_proposal(proposal, proposals_path)
        if suppressed:
            log.info("Extracted %d entries (%d duplicates suppressed)", len(entries), suppressed)
        else:
            log.info("Extracted %d decision entries from conversation", len(entries))
        return proposal

    except (json.JSONDecodeError, TypeError) as e:
        log.debug("Decision extraction parse error: %s", e)
    except Exception as e:
        log.warning("Decision extraction failed: %s", e)
    return None
