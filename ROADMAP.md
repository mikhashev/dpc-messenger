# D-PC Messenger Development Roadmap

> **Status:** Alpha | **Last Updated:** April 2026 | **Current Version:** 0.22.0-dev | **Current Phase:** Phase 2 - Agent Evolution (Track 1 focus, Track 2 deferred)

---

## Overview

D-PC Messenger development:

1. **Phase 1: Federated MVP - COMPLETE (v0.8.0)** - Proven P2P messaging with AI collaboration
2. **Phase 1.5: Extended Features - COMPLETE (v0.9.0 - v0.18.0)** - File transfer, voice, agent, resilient infrastructure
3. **Phase 2: Team Collaboration + Agent Evolution - IN PROGRESS** - Small teams, embedded AI agent, group chat
4. **Where This Leads** - Autonomous agents, network effects, local-first sovereignty

---

## Phase 1: Federated MVP - COMPLETE (v0.8.0)

**Status:** PoC / Experimental
**Timeline:** Completed November 2025
**Scope:** Individual users + 1:1 peer collaboration

**Features:** Direct TLS connections (IPv4/IPv6), WebRTC NAT traversal, Federation Hub (OAuth, signaling), cryptographic node identity (RSA/X.509), encrypted backups (AES-256-GCM), context firewall, AI scope filtering, offline mode, local AI integration (Ollama, OpenAI, Anthropic, Z.AI), remote inference (P2P compute sharing), knowledge commit system (git-like versioning with bias mitigation), personal context model v2.0

---

## Phase 1.5: Extended Features - COMPLETE (v0.9.0 - v0.18.0)

**Status:** Complete
**Timeline:** December 2025 - February 2026
**Scope:** Features built beyond original Phase 1 scope, including disaster-resilient infrastructure and AI agent capabilities

These features were developed organically as the product matured, significantly expanding the platform's capabilities.

### Decentralized Infrastructure (v0.9.5 - v0.10.2)

| Feature | Version | Status | Description |
|---------|---------|--------|-------------|
| **DHT-Based Peer Discovery** | v0.9.5 | Complete | Kademlia DHT, internet-wide validated |
| **6-Tier Connection Fallback** | v0.10.0 | Complete | ConnectionOrchestrator: IPv6 → IPv4 → WebRTC → hole punch → relay → gossip |
| **UDP Hole Punching** | v0.10.0 | Complete | DTLS 1.2 encryption, 60-70% NAT success |
| **Volunteer Relay Nodes** | v0.10.0 | Complete | DHT quality scoring, privacy-preserving |
| **Gossip Store-and-Forward** | v0.10.2 | Complete | Hybrid AES-GCM + RSA-OAEP encryption, eventual delivery |

### File & Media Features (v0.11.0 - v0.15.0)

| Feature | Version | Status | Description |
|---------|---------|--------|-------------|
| **File Transfer** | v0.11.0 | Complete | Chunked 64KB, SHA256 verification, progress tracking |
| **Session Management** | v0.11.3 | Complete | Mutual approval voting, multi-party coordination |
| **Chat History Sync** | v0.11.3 | Complete | Automatic sync on reconnect, page refresh support |
| **Vision & Image Support** | v0.12.0 | Complete | Screenshot sharing, remote vision inference, P2P image transfer |
| **Voice Messages** | v0.13.0 | Complete | Cross-platform recording, playback, Whisper transcription |
| **Cross-Platform Audio** | v0.15.0 | Complete | Rust cpal backend for Linux ALSA/PipeWire support |

### Telegram Integration (v0.14.0)

| Feature | Version | Status | Description |
|---------|---------|--------|-------------|
| **Telegram Bot** | v0.14.0 | Complete | Voice transcription, messaging bridge, whitelist access |

### DPC Agent (v0.18.0)

| Feature | Version | Status | Description |
|---------|---------|--------|-------------|
| **Embedded AI Agent** | v0.18.0 | Complete | 40+ tools, background consciousness, evolution system |
| **Agent Telegram Bridge** | v0.18.0 | Complete | Two-way messaging, voice transcription, event notifications |
| **Reasoning Models** | v0.18.0 | Complete | DeepSeek R1, Claude Extended Thinking, OpenAI o1/o3 |
| **Real-time Streaming** | v0.18.0 | Complete | Token-by-token AI response display |
| **Remote Inference Enhancements** | v0.18.0 | Complete | Provider discovery, configurable timeouts, agent integration |
| **Custom Task Types** | v0.18.0 | Complete | Extensible task type registration |

---

## Phase 2: Team Collaboration + Agent Evolution - IN PROGRESS

**Status:** Agent evolution active, team features planned
**Timeline:** Q1-Q3 2026
**Scope:** Small teams (2-20 members + AIs), embedded agent system

### What's Done

| # | Feature | Version | Status | Description |
|---|---------|---------|--------|-------------|
| 1 | **Group Chat** | v0.19.0 | Complete | Multi-participant chat with text, files, voice, screenshots, transcription, knowledge commits, session management |
| 2 | **DPC Agent** | v0.18.0 | Complete | Embedded autonomous AI with 40+ tools (now 59), persistent memory, task queue |
| 3 | **Agent Consciousness** | v0.20.0 | Complete | Background self-reflection, structured thoughts, scratchpad writes |
| 4 | **Agent Evolution** | v0.20.0 | Complete | Periodic self-improvement proposals (identity, skills, knowledge) |
| 5 | **Agent Skills** | v0.20.0 | Complete | 10 skills (code-analysis, memory-hygiene, web-research, etc.), skill reflection |
| 6 | **Agent Progress Board** | v0.20.0 | Complete | UI for evolution proposals, consciousness logs, task history |
| 7 | **Protocol 13** | v0.21.0 | Complete | Human-AI team coordination protocol (Mike=approve, CC=execute, Ark=review) |
| 8 | **Agent Telegram Bridge** | v0.18.0 | Complete | Two-way messaging with agent via Telegram |
| 9 | **External Agent Bridge** | v0.20.0 | Complete | External AI integration pattern (CC ↔ DPC via cc_agent_bridge.py, cron monitoring, Protocol 13 coordination) |

### What's Next

Two parallel tracks. Agent Maturity is research-backed (9 independent sources converge). Team Collaboration builds on agent infrastructure.

#### Track 1: Agent Maturity (~1,990 lines)

North Star: Consciousness observes → Evolution proposes → Verification measures → Cycle repeats with real learning. Phase 0 (Hooks) unlocks the cycle.

| Phase | Feature | Scope | Priority | Status | Description |
|-------|---------|-------|----------|--------|-------------|
| **0** | **Hooks/Middleware** | ~510 lines | ENABLER | **DONE** (S47) | HookRegistry + Event Bus in loop.py. 6 commits, 21 tests pass |
| **0.5** | **Selection Layer (ADR-013)** | ~200 lines | HIGH | **DONE** (S58-S59) | S1-S9 data collection, dedup, decay, rejection feedback, robustness |
| **1** | **Consciousness Tools** | ~240 lines | HIGH | **PARTIAL** (S61) | Multi-round tool access implemented. Remaining: adaptive timing |
| **2** | **Evolution Verification** | ~280 lines | CRITICAL | **PARTIAL** (S61) | Outcome tracking + rolling metrics (2A+2B). Remaining: rollback, metric gating |
| **3** | **Sleep Consolidation** | ~460 lines | HIGH | **PARTIAL** | L1 partial, L2 partial, L3 not started. Depends on P1+P2 completion |
| **4** | **Memory Upgrade** | ~240 lines | MEDIUM | **PARTIAL** (ADR-010) | Phase 1-3 DONE+WIRED (14/19 tasks). Phase 4 wiring incomplete |
| **5** | **Skill Rewrite** | ~260 lines | MEDIUM | NOT STARTED | Break append-only limit. A/B testing for skill rewrites with auto-rollback |

**Dependencies:** Phase 0 DONE → unblocked all. Phases 1+2 in progress (parallel). Phase 3 needs 1+2 completion. Phases 4+5 parallel after Phase 3.
**Research basis:** See `ideas/cc-mike-research/README.md` (consolidated from 9 sources) and `ideas/cc-mike-research/enumerated-strolling-seahorse.md` (detailed implementation plan).

#### Track 2: Team Collaboration

External Agent Bridge (CC) validates that non-embedded AI can participate as a full team member — foundation for multi-AI teams. Current implementation is cron-based polling; future direction: webhook or event-driven.

| # | Feature | Complexity | Description |
|---|---------|------------|-------------|
| 1 | **Persistent Team Management** | Medium | Team objects (`~/.dpc/teams.json`), UI sidebar, auto-firewall integration |
| 2 | **Team Firewall Presets** | Low | Pre-configured policies (Work Team, Study Group, Full Trust) |
| 3 | **Team Invite & Onboarding** | Medium | Short-lived invite codes, QR sharing, auto-connect workflow |
| 4 | **Team Knowledge Repository** | High | Separate `~/.dpc/teams/{team_id}/knowledge/`, Git-like sync protocol |
| 5 | **Knowledge Commit Templates** | Low | Pre-defined formats (Meeting Notes, Decisions, Postmortems) |
| 6 | **Team AI Assistants** | Medium | AI queries with `team_id`, access to collective team knowledge |
| 7 | **Team Compute Pools** | Medium | Auto-discovery, load balancing, "Team Compute" panel |
| 8 | **DPC Agent Team Integration** | Medium | Agent tasks across team context, multi-peer coordination |

**Cross-track dependencies:** Agent Isolation → A2A → Teams. Phase 0-2 → Team AI Assistants. Memory Upgrade → Team Knowledge Repo.

#### Smaller Improvements (not in tracks)

- **Consciousness dedup** — skip duplicate observations within 1h (~15 lines)
- **Chain depth increase** — 3 → 5-6 for complex multi-agent discussions (P13 external review)
- **Schedule System** — daily autonomous Ark sessions with autonomy rules
- **Agent Starter Pack** — skills bundle for open source first-run experience
- **Tool registry ↔ Firewall sync** — legacy knowledge tools removed (S55), remaining: invisible/unclassified tools audit (ARCH-1 partial)

### Success Metrics

**Operational Metrics:**
- All 6 connection tiers tested and operational
- Agent consciousness uptime (thoughts per session, no recursive loops)
- Evolution proposal success rate (applied vs regressed)
- Knowledge commits per session (extraction + voting pipeline)
- P2P mesh stability over 24h (2+ nodes)
- DHT lookup success rate >95% (peer discovery without Hub)

---

## Where This Leads

Not a feature list — three directions that grow from what we've already built.

### 1. Autonomous Agents
From triggered to proactive. Sleep consolidation, scheduled sessions, self-improvement cycles. Agents that grow with their humans — not tools that wait for commands. External Agent Bridge (CC ↔ DPC) is a working prototype of A2A — any external AI can integrate as a team member through the same pattern.

### 2. Network Effects
From 1:1 to team networks to ecosystem. Agent Isolation → A2A → Teams → Open Source starter packs. Skills sharing and inference sharing already work. Hub becomes optional bootstrap, not architecture center. Any two nodes can connect directly via `dpc://` URI exchange — no server required.

### 3. Local-First Sovereignty
All LLMs run locally (Ollama, llama.cpp, etc). P2P network = mutual aid (share compute with peers), not dependency. Works fully offline. Your data never leaves your machine. Knowledge and skills portable across agents.

---

## Knowledge Architecture

14-phase knowledge architecture. Phases 1-8 complete (v0.7.0-v0.8.0), Phases 9-12 partially implemented (extraction, tracking, consensus voting). See [docs/KNOWLEDGE_ARCHITECTURE.md](./docs/KNOWLEDGE_ARCHITECTURE.md) for full specification.

---

## Version History

See `git log` for complete version history.

---

**Last Updated:** April 2026
**Maintained By:** D-PC Messenger Core Team
**License:** See [LICENSE.md](./LICENSE.md)
