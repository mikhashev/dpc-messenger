"""Memory observer hook for extraction triggers (ADR-010, WIRE-4).

Observer middleware that checks extraction triggers after each LLM response.
Logs trigger events for session-close C3 gate processing.
"""

from __future__ import annotations

import logging
from typing import Optional

from .hooks import BaseMiddleware, HookAction, HookContext

log = logging.getLogger(__name__)


class ExtractionObserver(BaseMiddleware):
    """Observer that checks if LLM response triggers knowledge extraction."""

    def __init__(self):
        self.trigger_events: list[dict] = []

    async def after_llm_call(self, ctx: HookContext) -> Optional[HookAction]:
        try:
            from .extraction_triggers import check_triggers
            response_text = ctx.last_assistant_text or ""
            has_tools = ctx.tool_calls_this_round > 0

            result = check_triggers(response_text, has_tool_calls=has_tools)
            if result.should_extract:
                self.trigger_events.append({
                    "round": ctx.current_round,
                    "reasons": result.reasons,
                })
                log.debug("Extraction trigger: round %d, reasons: %s",
                          ctx.current_round, result.reasons)
        except Exception:
            pass

        return HookAction.CONTINUE
