# Knowledge Extraction Mechanism — External Review Brief

## Context
D-PC Messenger is a P2P messaging platform with embedded AI agents. During conversations between humans and AI agents, valuable knowledge emerges (decisions, facts, insights). The system extracts and persists this knowledge for future reference.

## Architecture

### Three Knowledge Stores (parallel)
1. **Human's store** (`~/.dpc/knowledge/`) — Human-initiated extraction only
2. **Review Agent's store** (agent sandbox) — Agent-initiated, saves locally
3. **Execute Agent's store** (session memory files) — Per-session persistence

### Two Extraction Paths

**Path 1: Manual (Human-initiated via "End Session" button)**
- Uses `full_conversation` (all messages in session, never cleared)
- Human clicks "End Session & Save Knowledge"
- LLM extracts structured knowledge entries (topic, summary, confidence, tags)
- Creates a proposal → participants vote (approve/reject)
- Approved proposals get crypto-signed commits with integrity verification
- Works reliably — Human sees all session topics

**Path 2: Automatic (ConversationMonitor background detection)**
- Uses `message_buffer` (incremental, cleared after approved extraction)
- Every 5 messages: LLM calculates `knowledge_score` (0.0-1.0)
  - Rubric: substantive(0.3) + perspectives(0.2) + consensus(0.2) + actionable(0.2) + novel(0.1)
  - Only last 10 messages analyzed
- If score > 0.7 AND `_detect_consensus()` passes → auto-extraction triggered
- `_detect_consensus()`: keyword matching (13 signal words like "agreed", "exactly", "let's do") in last 5 messages, threshold = participants × 0.6

### Voting System
- Solo participant: direct approval via UI panel
- 3+ participants: Devil's Advocate mechanism (one participant assigned as required dissenter)
- 75% approval threshold
- Timeout handling (configurable)
- Crypto-signed commits with SHA256 chain integrity

### Agent's extract_knowledge Tool
- Agent can explicitly call `extract_knowledge` during conversation
- Saves to agent's own knowledge directory (not Human's store)
- Does NOT trigger Human voting workflow (recently fixed)

## Known Weaknesses

1. **Keyword-based consensus detection** — `_detect_consensus()` uses 13 hardcoded keywords, not semantic analysis
2. **Narrow analysis window** — knowledge_score uses last 10 messages only; early session topics get missed by auto-detection
3. **Two data sources** — auto path uses `message_buffer` (incremental), manual uses `full_conversation` (complete) — different results possible
4. **Concurrency guard** — `_extracting` is a boolean flag, not a proper async lock
5. **Magic threshold** — 0.7 knowledge score threshold is empirically chosen, not validated

## What Works Well

1. Manual extraction path is reliable and complete
2. Crypto-signed commits with integrity chain — serious approach to knowledge provenance
3. Devil's Advocate for multi-party — forces critical review
4. Structured output (topic, entries, confidence, tags) — queryable knowledge base

## Questions for Review

1. Is the dual-path architecture (auto + manual) sound, or should it be unified?
2. Is keyword-based consensus detection adequate, or should it use LLM/embedding similarity?
3. How should the knowledge score threshold be determined/calibrated?
4. Is the Devil's Advocate mechanism adding real value or just mechanical dissent?
5. What would a production-grade version of this system look like?
