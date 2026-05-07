# ADR-026: Public Agent Guardrails and Context Architecture

**Status:** Proposed
**Date:** 2026-05-08
**Authors:** Ark (architecture), CC (implementation), Mike (decision)
**Depends on:** ADR-024 (Knowledge Graph), ADR-025 (Discord Integration)

## Context

ADR-025 Phase 1.5 delivered Iris — a community-facing agent responding to Discord @mentions. Current limitations:

1. **No tool restrictions by source** — Iris has the same tool access for Discord messages as for local chat. A Discord user could potentially trigger write_file, update_identity, or other destructive tools through prompt injection.
2. **No URL filtering** — user-submitted URLs pass through to the agent without validation.
3. **No conversation continuity** — each Discord message is an isolated request. No history, no memory, no context window management.
4. **No long-term memory** — when a user returns, Iris has no recall of previous interactions.

These are architectural decisions that apply to any future public-facing agent, not just Iris.

## Decision

### 1. Source-Based Tool Filtering

Agents receive a restricted tool set when the message originates from an external source (Discord, future Telegram public, etc.).

**Mechanism:**
- `discord_manager` tags each message with `source: "discord"` in metadata
- Agent tool list is built from the existing 3-layer firewall whitelist
- A source-based filter removes write/destructive tools when `source != "local"`
- Restricted tools (configurable, not hardcoded): write_file, update_identity, repo_delete, git_commit, repo_write, shell_exec

**Configuration:**
```ini
[discord.guardrails]
restricted_tools = write_file,update_identity,repo_delete,git_commit,repo_write,shell_exec
```

**Why code-level, not prompt-level:** Code enforcement cannot be bypassed by prompt injection. System prompt reinforcement ("never modify your configuration") serves as a secondary defense layer in Block1.

### 2. URL Whitelist for User-Submitted Content

URLs in Discord user messages are validated against a configurable whitelist before reaching the agent.

**Mechanism:**
- Extract URLs from incoming Discord message text
- Check domain against whitelist
- Replace non-whitelisted URLs with a placeholder: `[URL removed — not in whitelist]`
- Agent's own web search/browse tools are unrestricted (agent-initiated, not user-supplied)

**Configuration:**
```ini
[discord.guardrails]
url_whitelist = github.com/mikhashev/*,docs.dpc-messenger.org/*
```

### 3. Per-User Conversation Lifecycle

Each Discord user gets a dedicated conversation with TTL-based lifecycle management.

**Architecture — three layers:**

**Layer 1: Per-user conversation routing**
- `discord_manager` maps `discord_user_id → conversation_id`
- Reuses existing `ConversationMonitor` infrastructure (history.json, token counting, truncation)
- All messages from one Discord user go to one conversation

**Layer 2: Context window management**
- Existing token counting and truncation apply automatically
- TTL: conversation resets after configurable inactivity period (default: 30 min)
- Max messages per conversation: configurable limit (default: 50)
- On context limit approach: oldest messages trimmed

**Layer 3: Long-term memory via Knowledge Graph (ADR-024)**
- On conversation expiration (TTL or limit): extract key facts to knowledge graph
- Entity extraction via existing GLiNER + LLM pipeline
- `discord_user_id` stored as metadata on graph nodes/edges
- On next interaction: Active Recall loads relevant subgraph for that user
- Cross-user analytics: popular topics, FAQ patterns (aggregated, no PII)

**Configuration:**
```ini
[discord.conversations]
ttl_minutes = 30
max_messages = 50
extract_on_expiry = true
```

## Not in Scope

- Bidirectional bridge (ADR-025 Phase 2 — technical task, not architectural decision)
- Discord thread support (ADR-025 Phase 2)
- Rate limiting (future consideration)

## Implementation Priority

1. **Source-based tool filtering** — security first
2. **URL whitelist** — input sanitization
3. **Per-user conversation routing + TTL** — UX improvement
4. **KG integration for long-term memory** — depends on ADR-024 completion

## Security Considerations

- ADR describes mechanisms, not values. Concrete tool lists, whitelist domains, and TTL values live in config (not in public repo).
- System prompt (Block1) reinforces boundaries as secondary defense.
- Defense in depth, not security through obscurity — code-level enforcement works regardless of attacker knowledge.

## Consequences

- Public agents become safe to deploy on external platforms
- Pattern reusable for future integrations (Telegram public, web widget, etc.)
- Per-user conversations add disk storage (~1KB per conversation, auto-cleanup on TTL)
- KG integration adds compute cost per conversation (entity extraction on expiry)
