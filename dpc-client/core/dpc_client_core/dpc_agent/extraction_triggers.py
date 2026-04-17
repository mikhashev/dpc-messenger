"""Extraction triggers for episodic memory (ADR-010, MEM-4.1).

Detects when agent responses contain knowledge worth extracting:
tool calls, long responses, decision verbs, explicit knowledge claims.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List


DECISION_VERBS = re.compile(
    r"\b(решил[иа]?|утвердил[иа]?|принял[иа]?|одобри[лт]|"
    r"decided|approved|agreed|confirmed|concluded)\b",
    re.IGNORECASE,
)

KNOWLEDGE_MARKERS = re.compile(
    r"\b(потому что|причина|вывод|факт|правило|"
    r"because|conclusion|finding|rule|principle|lesson)\b",
    re.IGNORECASE,
)

RESPONSE_LENGTH_THRESHOLD = 500


@dataclass
class TriggerResult:
    should_extract: bool
    reasons: List[str]


def check_triggers(
    response_text: str,
    has_tool_calls: bool = False,
    participant_count: int = 1,
) -> TriggerResult:
    """Check if response triggers knowledge extraction."""
    reasons: List[str] = []

    if has_tool_calls:
        reasons.append("tool_call_in_response")

    if len(response_text) > RESPONSE_LENGTH_THRESHOLD:
        reasons.append("long_response")

    if DECISION_VERBS.search(response_text):
        reasons.append("decision_verb")

    if KNOWLEDGE_MARKERS.search(response_text):
        reasons.append("knowledge_claim")

    return TriggerResult(
        should_extract=len(reasons) > 0,
        reasons=reasons,
    )
