# ADR 001: service.py Domain Service Extraction

**Date:** 2026-03-28
**Status:** Accepted
**Branch:** refactor/grand

## Context

`service.py` (CoreService) grew to 9,630 lines (430KB) as features were added organically.
Phase 2 (Team Collaboration) requires adding `TeamManager`, `GroupChatManager`,
`TeamKnowledgeSync` — making an already-oversized file even harder to maintain.

## Decision

Extract 4 domain services from service.py, ordered by coupling (least first):

1. **VoiceService** — Whisper transcription lifecycle
2. **KnowledgeService** — Knowledge commits, consensus wiring
3. **TelegramService** — Telegram bot glue
4. **AgentService** — DPC agent lifecycle wiring

Then split remaining dispatch glue into:
- `handlers/ws_handlers.py` — WebSocket API commands
- `handlers/api_handlers.py` — Local API responses
- `handlers/p2p_handlers.py` — P2P message routing
- `core/service_state.py` — State management

Target: CoreService < 2,000 lines after all extractions.

## State Variables Per Service (Арх mapping)

**VoiceService** (4 vars, service.py lines 333-341):
- `_pending_transcription_requests` (Dict[str, asyncio.Future])
- `_voice_transcriptions` (Dict[str, Dict[str, Any]])
- `_transcription_locks` (Dict[str, asyncio.Lock])
- `_voice_transcription_settings` (Dict[str, bool])

**KnowledgeService** (4 vars):
- `pcm_core` (PCMCore, line 154)
- `consensus_manager` (ConsensusManager, line 210)
- `conversation_monitors` (Dict[str, ConversationMonitor], line 309)
- `auto_knowledge_detection_enabled` (bool, line 312)

**TelegramService** (2 vars, lines 278-279):
- `telegram_manager` (TelegramBotManager | None)
- `telegram_bridge` (TelegramBridge | None)

**AgentService** (2 vars, lines 330-336):
- `_pending_inference_requests` (Dict[str, asyncio.Future])
- `_pending_providers_requests` (Dict[str, asyncio.Future])

**CoreService keeps**: 38 vars — infrastructure, orchestrators, coordinators, lifecycle state.

## Key Constraints

- **No EventBus** — use existing callback pattern (50+ `set_on_*()` / `on_*=` in service.py)
- **ConversationMonitor stays in CoreService** — it's a mediator between KnowledgeService
  and AgentService; extracting it creates a circular dependency
- **@mention parsing stays in ws_handlers** — it's message dispatch, not knowledge logic
- **Background tasks move WITH their service** — no orphaned task references

## Inter-Service Communication Pattern

```python
# Correct: CoreService injects references at construction time
self.voice_service = VoiceService(self.llm_manager, self.settings)
self.knowledge_service = KnowledgeService(self.consensus_manager, self.pcm_core)

# Correct: callbacks for async events
self.knowledge_service.on_commit_applied = self._on_commit_applied_ui_notify
```

## Graceful Degradation

Services that fail to initialize are set to None; CoreService continues without them:
```python
try:
    self.telegram_service = TelegramService(...)
except Exception as e:
    logger.error("TelegramService init failed: %s", e)
    self.telegram_service = None
```

## API Compatibility

Old WebSocket API endpoints kept as 1-version deprecated aliases in ws_handlers.py.
Frontend continues to work without changes during refactor.

## Rationale

- Enables Phase 2 Team Collaboration features without adding to a 9,630-line file
- Extraction order by coupling prevents circular dependency creation
- Each service gets `get_state()` for runtime introspection (agent-readable)
- Tests written per service (40% coverage target vs ~0% today)
