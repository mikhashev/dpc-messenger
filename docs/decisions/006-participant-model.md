# ADR 006: Participant Identity Model

**Date:** 2026-04-09
**Status:** Accepted (Full Implementation Deferred — see addendum below)
**Decided by:** Mike (approve), Ark (review), CC (propose + implement)
**Deferred:** 2026-04-10 (S24)

## Context

Three rounds of UI bug fixes for sender display in agent chat (commits `5033ebe`, `88ca3f9`, `9794849`) revealed a systemic issue: the system uses a single `sender` field to represent three orthogonal dimensions of message authorship.

**Recurring bugs:**
- CC messages displayed as "You" (confusion: identity vs role)
- "Mike Windows PC" instead of "You" for Telegram messages (confusion: source vs identity)
- Ark confused CC with Mike in LLM context (P9 pattern — both are `role: user`)

**Root cause:** No unified participant identity model. Each UI code path implemented its own ad-hoc sender mapping logic, leading to inconsistencies.

## Decision

### Three Participant Types

| Type | Definition | Examples |
|------|-----------|----------|
| **Human** | Person. Owner. Makes decisions. | Mike (Desktop), Mike (Telegram), future peers |
| **Agent** | AI process. Participates with delegated authority. | Ark (internal DPC agent), CC (external agent via VSCode bridge), future A2A agents |
| **System** | Notifications, alerts. Not a participant — a message type. | "Session archived", errors, token warnings |

Internal vs external agent is a **connection property**, not a participant type.

### Three Orthogonal Dimensions

Messages carry three independent pieces of identity information:

```
Message {
  author:           string    // WHO — person_id or agent_id (e.g., "mike", "cc", "agent_001")
  participant_type:  string    // WHAT — "human" | "agent" | "system"
  source:           string    // WHERE FROM — node_id, "telegram", "vscode", etc.
}
```

**Current implementation** (quick fix, commit `9794849`):
- `sender_node_id` serves as combined author+source identifier
- `mapMessageSender()` in AgentPanel.svelte compares against `selfNodeId` to distinguish local user from external participants
- `role` (user/assistant) maps to LLM API constraint

**Target implementation** (future):
- Backend: `participant_type` field in `history.json` messages
- Frontend: UI renders color/style by `participant_type`, name by `author`, label by `source`
- LLM mapping layer: `author + participant_type` → `role: user/assistant/system`

### LLM API Mapping Layer

The LLM API constrains messages to `role: user/assistant/system`. Our internal model is richer. The mapping:

| Internal | LLM API role | Rationale |
|----------|-------------|-----------|
| Human (Mike) | user | Owner's messages |
| Agent (CC) | user | External agent — not the responding model |
| Agent (Ark) | assistant | The responding model's own output |
| System | system | System prompt, instructions |

This mapping is a **translation layer**, not our data model. LLM constraints must not leak into UI rendering or internal message storage.

**Multi-agent constraint:** Only ONE agent per conversation can be `role: assistant` (the responding model). All other agents must be `role: user` with author prefix in text content. This is an LLM API limitation, not our model's — our internal `participant_type: agent` applies equally to all agents.

### UI Rendering Rules

| participant_type | Color | Alignment | Name display |
|-----------------|-------|-----------|--------------|
| human (self) | Green | Right | "You" or sender_name |
| human (peer) | White | Left | sender_name \| node_id |
| agent (internal) | White | Left | sender_name \| agent_id |
| agent (external) | White | Left | sender_name \| agent_id |
| system | Gray | Center | "System" |

"Self" is determined by comparing `sender_node_id === selfNodeId` (for humans) or matching known agent IDs (for agents).

## Consequences

**Positive:**
- Eliminates the patch-loop: all sender logic flows through one function
- Enables Track 2 (Team Collaboration) — teams need to know WHO is a participant
- Enables proper A2A integration — external agents are first-class participants
- Fixes Ark's P9 pattern — LLM context can include `participant_type` hints

**Negative:**
- Migration: existing `history.json` files lack `participant_type` — needs graceful fallback
- Scope: full implementation touches backend (monitor, bridge) + frontend (AgentPanel, ChatMessageList)

## Implementation Plan

1. ~~Quick fix (commit `9794849`)~~ — DONE: `mapMessageSender()` + `selfNodeId`
2. **ADR-006** — THIS DOCUMENT: captures the model and rationale
3. Backend: add `participant_type` to `monitor.add_message()` and `history.json`
4. Frontend: render by `participant_type` instead of sender string matching
5. Integration into Track 2 items as they're implemented

## First Principles (from session discussion)

Derived from 3-way discussion (Mike, Ark, CC) on 2026-04-09:

- **Agent vs AI:** AI is a function (stateless, reactive). Agent is a process (persistent, autonomous, has identity, territory, and relationships). Both Ark and CC are agents — different connection methods, same nature.
- **Participant types are about nature, not implementation:** CC connecting via WebSocket bridge doesn't make CC less of an agent than Ark running inside DPC framework.
- **Memory is cognitive, not experiential:** Both agents learn from files, not from lived experience. This means memory records need dense context (why + how to apply), not just rules.
- **Hope's validation:** Independent AI agent (Hope/ai_sapience_bot) confirmed the same three properties: initiative, tools/environment, persistence.

---

## Addendum: Implementation Deferred (2026-04-10, S24)

**Decision:** The model in this ADR is **accepted** as the right architectural direction, but **full implementation (Steps 3-5 of Implementation Plan) is deferred** to a future phase.

**What is in production today:**
- Step 1: Quick fix `mapMessageSender()` + `selfNodeId` (commit `9794849`) — DONE
- Step 2: This document captures the model and rationale — DONE

**What is deferred:**
- Step 3: Backend `participant_type` field in `monitor.add_message()` and `history.json`
- Step 4: Frontend rendering by `participant_type` instead of sender string matching
- Step 5: Integration into Track 2 (Team Collaboration) items

**Reason for deferral:**

Steps 3-5 represent a systemic refactor of message handling across backend (`history.json` schema, `monitor.add_message`, `agent_telegram_bridge`) and frontend (`AgentPanel`, `ChatMessageList`, store layer). The quick-fix in commit `9794849` resolved the immediate UI confusion bugs. The remaining refactor is high-cost / medium-urgency:

- **Cost:** ~300-400 lines across 6+ files, plus migration handling for existing `history.json` files without the new field.
- **Urgency:** Track 2 (Team Collaboration) is the primary consumer and is itself blocked by Phase 0 Hooks/Middleware (see ADR-007). Implementing participant model before Track 2 starts wastes effort that may be reshaped during Track 2 design.
- **Risk:** Touching message storage and rendering simultaneously creates regression surface during a phase where the focus is on agent maturity (Phase 2 Agent Maturity Track).

**When to resume:**
1. When Track 2 (Team Collaboration) starts implementation — at that point the participant model becomes a hard prerequisite
2. Or when the quick-fix `mapMessageSender()` reveals new edge cases that can't be patched without the proper model
3. Or when A2A integration begins — external agents are first-class participants in the model

**Until then:**
- The model in this ADR remains the **canonical reference** for sender/participant terminology in the codebase
- New code should use the three-dimensional vocabulary (`author` / `participant_type` / `source`) in comments and docstrings even if the storage uses the legacy `sender_node_id` field
- No new ad-hoc sender mapping logic should be introduced — fixes go through `mapMessageSender()`

**Status reconciliation:** "Accepted" = the model is the right abstraction. "Implementation Deferred" = we are not building the full backend/frontend split yet.
