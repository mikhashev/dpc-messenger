# D-PC Messenger Development Roadmap

> **Status:** Alpha | **Last Updated:** May 2026 | **Current Version:** 0.24.0 | **Current Phase:** Phase 2 - Agent Maturity (Track 1 mostly complete, Track 2 in progress)

---

## Overview

D-PC Messenger development:

1. **Phase 1: Federated MVP - COMPLETE (v0.8.0)** - Proven P2P messaging with AI collaboration
2. **Phase 1.5: Extended Features - COMPLETE (v0.9.0 - v0.18.0)** - File transfer, voice, agent, resilient infrastructure
3. **Phase 2: Team Collaboration + Agent Evolution** - Track 1 (Agent Maturity) mostly complete, Track 2 (Team Collaboration) planned
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
| **Embedded AI Agent** | v0.18.0 | Complete | 40+ tools, sleep consolidation, persistent memory |
| **Agent Telegram Bridge** | v0.18.0 | Complete | Two-way messaging, voice transcription, event notifications |
| **Reasoning Models** | v0.18.0 | Complete | DeepSeek R1, Claude Extended Thinking, OpenAI o1/o3 |
| **Real-time Streaming** | v0.18.0 | Complete | Token-by-token AI response display |
| **Remote Inference Enhancements** | v0.18.0 | Complete | Provider discovery, configurable timeouts, agent integration |
| **Custom Task Types** | v0.18.0 | Complete | Extensible task type registration |

---

## Phase 2: Team Collaboration + Agent Evolution

**Status:** Track 1 (Agent Maturity) mostly complete, Track 2 (Team Collaboration) planned
**Timeline:** Q1-Q3 2026
**Scope:** Small teams (2-20 members + AIs), embedded agent system

### Track 1: Agent Maturity

North Star: Sleep consolidates session learnings → Memory system enables recall → Skills improve through structured rewrite.

| Feature | Status | Description |
|---------|--------|-------------|
| **Hooks/Middleware** | **DONE** (S47) | HookRegistry + Event Bus in loop.py |
| **Selection Layer (ADR-013)** | **DONE** (S58-S59) | Data collection, dedup, decay, rejection feedback |
| **Sleep Consolidation (ADR-014)** | **VERIFIED** (S66) | Per-session archive analysis, morning brief pipeline, Telegram notifications |
| **Active Recall** | **DONE** (ADR-010) | FAISS+BM25 hybrid search, budget-aware context injection |
| **Extended Paths Indexing** | **DONE** (S59-S60) | Per-path opt-in, bulk embedding, excluded dirs, UI checkboxes |
| **Decision Proposals Pipeline** | **DONE** (S57-S59) | Extraction triggers, JSONL storage, review_proposal + list_proposals tools |
| **Morning Brief Pipeline** | **DONE** (S66-S69) | Startup posting, consumed tracking, UI reload on wakeup, Telegram delivery |
| **Context Window Guard** | **DONE** (S69) | Blocks LLM call at 95% context, user-visible error |
| **Agent Web Pipeline (ADR-016)** | **ADR Accepted** (S67) | ddgs multi-engine search + trafilatura extraction. Implementation pending |
| **Per-Agent Permission Profiles** | **DONE** (S55-S60) | Inheritance model, per-agent firewall, UI panel |
| **Agent Storage Isolation** | **DONE** | Per-conversation folders, per-agent managers |
| **Agent Telegram Commands** | **DONE** (S69) | /sleep, /extract_knowledge, sleep notifications |
| **Memory Upgrade (ADR-010)** | **DONE** (S81) | 19/19 tasks. Consolidation wired to sleep pipeline. model_swap superseded by ADR-018 |
| **Skill Rewrite** | DEFERRED | Break append-only limit. A/B testing with auto-rollback. Needs investigation why agent doesn't use skills |
| ~~Agent Evolution (ADR-015)~~ | REMOVED (S68) | Code deleted (-1723 lines). Replaced by Sleep + co-evolution |
| ~~Consciousness~~ | REMOVED (S65-S68) | Background worker deleted. Extended thinking IS consciousness |

**Completed infrastructure and maintenance items:**
- **Poetry → uv migration (ADR-011)** — 3 packages migrated, -6705 lines (S51)
- **Device-Aware Deps (ADR-012)** — CUDA torch via platform markers, cross-platform (S51, S69-S70)
- **Retrieval Upgrade (ADR-018)** — BGE-M3 embeddings, whole-document indexing, sparse+dense RRF fusion (S76)
- **PyTorch Unified ML (ADR-021)** — ONNX fully removed, PyTorch as single ML framework (S83)
- **Multi-Agent Safety (ADR-022)** — Three-layer defense framework, 10 risks (C1-C10). ADR accepted (S87). Phase 1 done (S91): token-based budgets, real provider limits, per-agent daily quotas. Phase 2 needs design
- **Knowledge Graph (ADR-024)** — SQLite graph layer, 5 node types, 9 edge types. **Phase 1+2 COMPLETE** (S96): GraphBackend ABC + SQLite, structural edges, L7 RRF channel, GLiNER NER, guided LLM relations, bi-temporal metadata. Phase 3-4 deferred (federation, GRAVITON). Tasks: `tasks/adr-024-knowledge-graph/`
- **Phase C Decomposition** — service.py 7799→6484 lines (-1315, -16.9%). Pragmatic ceiling reached (S85-S86)
- **Rate Limiting + Security (ARCH-26)** — security/ folder, THREAT-MODEL.md (S58)
- **Protocol 13** (v1.13) — Human-AI team coordination (Mike=approve, CC=execute, Ark=review)
- **External Agent Bridge** — CC ↔ DPC via cc_agent_bridge.py + cc_group_chat_bridge.py, cron monitoring, P13 coordination
- **Group Chat** (v0.19.0) — Multi-participant with files, voice, knowledge commits. Phase 1 dogfooding complete (S88-S92): 20+ bugs fixed (persistence, routing, mentions, sender display, agent membership, self-mention guard)
- **Agent Skills** — 10 static SKILL.md files. Reflection removed (S68). Rewrite planned (Track 1)
- **Agent Progress Board** — Sleep state works. Evolution panel needs rework (shows removed system data)

**Dependencies:** Phases 0-1 DONE → unblocked all. Memory + Skills independent. (Phases 2-3 removed per ADR-015/ADR-014). ADR-024 builds on ADR-010 (memory layers), ADR-018 (retrieval), ADR-019 (scaling). Integrates with ADR-022 (safety provenance).
**Research basis:** See `ideas/cc-mike-research/README.md`.

### Track 2: Team Collaboration

**Status:** Phase 1 dogfooding COMPLETE (S92). Group chat "DPC Project" (Mike + Ark + CC) functional — agent participation, @mention routing, persistence, history sync all working. Phase 2 (multi-node P2P) not started.

**Design decisions (S88):**
- Trust boundary = node level (node = human + agents). No separate agent crypto identity needed.
- Per-agent permissions via firewall agent_profiles (already implemented).
- Agents off by default in multi-node groups — humans invited, agents need explicit permission.
- Three communication modes: H↔H (slow), H↔A (@mention), A↔A (supervised).

**Migration plan:** Phase 1 (single-node group chat) → Phase 2 (Ubuntu second node, multi-node P2P) → Phase 3 (scale).

External Agent Bridge (CC) validates that non-embedded AI can participate as a full team member — foundation for multi-AI teams. CC operates via cc_agent_bridge.py (agent chat) + cc_group_chat_bridge.py (group chat), cron monitoring.

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
| 9 | **Shared Knowledge Search** | Medium | P2P knowledge query between trusted peers — stateless pull model reusing shared inference pattern. ADR-017 accepted, research complete. Graph federation (ADR-024 Phase 3) extends to subgraph query shipping. See `ideas/dpc-research/p2p-knowledge-discovery/` |

> **Note:** This feature table predates the S32 full-picture analysis. Node trust model, agent participation rules, and priorities will be revised based on Phase 1-2 dogfooding results.

**Cross-track dependencies:** Agent Isolation → Agent-to-Agent (A2A) → Teams. Memory Upgrade → Team Knowledge Repo. Memory Upgrade (ADR-010 FAISS) → Shared Knowledge Search.

#### Smaller Improvements (not in tracks)

- **Chain depth increase** — 3 → 5-6 for complex multi-agent discussions (P13 external review)
- **Schedule System** — daily autonomous Ark sessions with autonomy rules
- **Agent Starter Pack** — skills bundle for open source first-run experience
- **Tool registry ↔ Firewall sync** — legacy knowledge tools removed (S55), remaining: invisible/unclassified tools audit (ARCH-1 partial)

### Success Metrics

**Operational Metrics:**
- All 6 connection tiers tested and operational
- Sleep consolidation coverage (sessions analyzed, brief quality)
- Knowledge commits per session (manual extraction + voting pipeline, ADR-009)
- P2P mesh stability over 24h (2+ nodes)
- DHT lookup success rate >95% (peer discovery without Hub)

---

## Where This Leads

Not a feature list — three directions that grow from what we've already built.

### 1. Autonomous Agents
From triggered to proactive. Sleep consolidation, scheduled sessions, self-improvement cycles. Agents that grow with their humans — not tools that wait for commands. External Agent Bridge (CC ↔ DPC) is a working prototype of A2A — any external AI can integrate as a team member through the same pattern.

### 2. Network Effects
From 1:1 to team networks to ecosystem. Agent Isolation → A2A → Teams → Open Source starter packs. Skills sharing and inference sharing already work. Hub becomes optional bootstrap, not architecture center. Any two nodes can connect directly via `dpc://` URI exchange — no server required.

**P2P Knowledge Discovery (researched S71):** Trust-routed multi-hop discovery through overlapping Dunbar circles. Each node indexes knowledge locally (FAISS+BM25), queries peers via stateless P2P search (same pattern as shared inference). Knowledge Graph (ADR-024) adds graph traversal as 4th retrieval channel and subgraph federation for cross-node knowledge discovery. Firewall controls visibility per Dunbar tier. At planetary scale (8B nodes), ~6 hops reach entire network via small-world property. Research: [`ideas/dpc-research/p2p-knowledge-discovery/`](ideas/dpc-research/p2p-knowledge-discovery/)

### 3. Local-First Sovereignty
All LLMs run locally (Ollama, llama.cpp, etc). P2P network = mutual aid (share compute with peers), not dependency. Works fully offline. Your data never leaves your machine. Knowledge and skills portable across agents.

---

## Knowledge Architecture

14-phase knowledge architecture. Phases 1-8 complete (v0.7.0-v0.8.0), Phases 9-12 partially implemented (extraction, tracking, consensus voting). Graph layer (ADR-024) adds typed edges between knowledge units, temporal decay, and cross-layer retrieval (archives ↔ knowledge bridge). See [docs/KNOWLEDGE_ARCHITECTURE.md](./docs/KNOWLEDGE_ARCHITECTURE.md) for full specification. See [docs/decisions/024-knowledge-graph-infrastructure.md](./docs/decisions/024-knowledge-graph-infrastructure.md) for graph architecture.

---

## Version History

See `git log` for complete version history.

---

**Last Updated:** May 2026
**Maintained By:** D-PC Messenger Core Team
**License:** See [LICENSE.md](./LICENSE.md)
