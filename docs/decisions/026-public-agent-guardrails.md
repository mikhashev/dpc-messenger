# ADR-026: Public Agent Guardrails and Context Architecture

**Status:** Partially Implemented
**Date:** 2026-05-08
**Authors:** Ark (architecture), CC (implementation), Iris (agent perspective), Mike (decision)
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
- For external sources: only **whitelisted read-only tools** are available (whitelist approach, not blacklist — new tools are blocked by default)
- Read-only tools allowed from Discord: memory_search, browse_page, search_web, read_file, list_my_tools, knowledge_list
- Write/mutating tools (write_file, update_identity, repo_delete, git_commit, etc.) are excluded for external sources

**Configuration:**
```ini
[discord.guardrails]
allowed_tools = memory_search,browse_page,search_web,read_file,list_my_tools,knowledge_list
```

**Defense in depth:** The agent firewall already restricts dangerous tools for Iris (drive_list, import_skill_from_agent, repo_commit_push, run_shell are disabled). Source-based filtering is an additional layer — even if firewall config drifts, Discord-sourced messages still can't trigger write tools. Code enforcement cannot be bypassed by prompt injection. System prompt reinforcement ("never modify your configuration") serves as a third defense layer in Block1.

### 2. URL Whitelist for User-Submitted Content

URLs in Discord user messages are validated against a configurable whitelist before reaching the agent.

**Mechanism:**
- Extract URLs from incoming Discord message text
- Check domain against whitelist
- For non-whitelisted URLs: agent responds "I can't follow external links directly, but I can search for information about [topic]" and uses search_web instead
- Agent's own web search/browse tools are unrestricted (agent-initiated, not user-supplied)
- Additional domains can be added to whitelist via config as community grows

**Configuration:**
```ini
[discord.guardrails]
url_whitelist = github.com/mikhashev/*
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
- Entity extraction is lightweight (GLiNER NER, not full LLM call) to keep cost manageable at scale
- On expiry: save `last_topic` (user's last message) as context node — enables "I remember you asked about X" on return
- `discord_user_id` stored as metadata on graph nodes/edges
- On next interaction: Active Recall loads relevant subgraph for that user
- Cross-user analytics: popular topics, FAQ patterns (aggregated, no PII)
- TTL cleanup physically deletes expired conversation files

**Configuration:**
```ini
[discord.conversations]
ttl_minutes = 30
max_messages = 50
extract_on_expiry = true
cleanup_expired = true
```

### 4. Rate Limiting

Protect the LLM provider and prevent abuse by throttling requests.

**Per-user rate limit:** max N messages per M minutes (default: 5 / 10 min). Prevents single user from monopolizing agent compute.

**Global rate limit:** max X agent invocations per hour across all users. Prevents burst overload on subscription-based providers with concurrency limits.

**Response delay:** minimum pause before agent reply (default: 3 seconds). Prevents bot-like rapid-fire responses.

**Exceeded behavior:** short message "Please wait a moment" instead of silence.

**Configuration:**
```ini
[discord.guardrails]
rate_limit_per_user = 5/10m
rate_limit_global = 60/1h
response_delay_seconds = 3
```

### 5. Graceful Fallback

**LLM provider down:** agent stays silent (no error spam to Discord channel). Log error internally.

**Rate limit exceeded:** short polite message with cooldown hint.

**Context window full:** agent responds "Let me start a fresh conversation" and auto-resets per-user conversation.

## Not in Scope

- Bidirectional bridge (ADR-025 Phase 2 — technical task, not architectural decision)

## Implementation Priority

1. **Source-based tool filtering** — security first
2. **Rate limiting + response delay** — abuse prevention
3. **URL whitelist** — input sanitization
4. **Graceful fallback** — reliability
5. **Per-user conversation routing + TTL** — UX improvement (004d DONE)
6. **KG integration for long-term memory** — depends on ADR-024 completion

## Security Considerations

- ADR describes mechanisms, not values. Concrete tool lists, whitelist domains, and TTL values live in config (not in public repo).
- System prompt (Block1) reinforces boundaries as secondary defense.
- Defense in depth, not security through obscurity — code-level enforcement works regardless of attacker knowledge.

## Implementation Status

**Tasks:** [tasks/adr-026-guardrails/](../../tasks/adr-026-guardrails/)

| Item | Status | Task |
|---|---|---|
| Per-user conversation routing (Layer 1) | DONE (004d, `71e53d4`) | ADR-025 Task 004d |
| Discord threads (004e) | DONE (`f11622f`) | ADR-025 Task 004e |
| Mention cleanup + echo (004c) | DONE (`440302e`) | ADR-025 Task 004c |
| Source-based tool filtering | DONE (`5556a15`) | [001](../../tasks/adr-026-guardrails/001-source-tool-filtering.md) |
| Rate limiting + response delay | TODO | [002](../../tasks/adr-026-guardrails/002-rate-limiting.md) |
| Output content filtering | DONE (`pending`) | [003](../../tasks/adr-026-guardrails/003-output-sanitization.md) |
| URL whitelist | TODO | [004](../../tasks/adr-026-guardrails/004-url-whitelist.md) |
| Graceful fallback | TODO | [005](../../tasks/adr-026-guardrails/005-graceful-fallback.md) |
| TTL + context management (Layer 2) | TODO | [006](../../tasks/adr-026-guardrails/006-ttl-context-management.md) |
| Mention sanitization | TODO | [007](../../tasks/adr-026-guardrails/007-mention-sanitization.md) |
| KG long-term memory (Layer 3) | TODO (depends ADR-024) | — |

## Open Questions (S102 Review)

**From Iris:**
1. `read_file` in tool whitelist — restrict to whitelisted directories (knowledge/, docs/) for external sources? Otherwise prompt injection could leak arbitrary files.
2. GLiNER extraction on conversation expiry — quality without LLM verification? May produce noisy entities.
3. Thread auto-archive (1h) should sync with conversation TTL (30 min) — which takes precedence?

**From Ark:**
4. Output content filtering — ADR covers input filtering (tools, URLs) but not output. Agent should not post PII, internal file paths, or sandbox URLs to public Discord. Need output sanitization.
5. Mention sanitization — @agent in code blocks, quotes, or technical text triggers routing. Parser should only match plain-text mentions.
6. Priority items need concrete backlog task IDs.

## Future: Discord Curation Model

Ark can be added to the same DPC group chat as Iris (e.g., `group-3a5c50f5024b` / "DPC Discord General") to serve as a backstage curator.

**How it works:**
- Iris answers Discord @mentions publicly in the Discord channel
- Ark participates in the internal DPC group chat only — providing context, corrections, and warnings to Iris
- Discord channel shows only Iris's responses; Ark's contributions stay internal
- No new infrastructure required — existing group chat routing already separates DPC-internal messages from Discord-bridged output

**Use cases:**
- Ark corrects a factual error before Iris posts (if pre-send gate is not yet implemented)
- Ark provides deeper context on a topic Iris is unsure about
- Ark flags a prompt injection attempt visible in the group chat history

**Constraints:**
- Ark must not echo to Discord — only Iris has Discord bridge output
- Group chat token budget applies to both agents combined
- This is a future consideration, not immediate implementation — depends on multi-agent group chat stability

## Consequences

- Public agents become safe to deploy on external platforms
- Pattern reusable for future integrations (Telegram public, web widget, etc.)
- Per-user conversations add disk storage (~1KB per conversation, auto-cleanup on TTL)
- KG integration adds compute cost per conversation (entity extraction on expiry)
