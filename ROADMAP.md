# D-PC Messenger Development Roadmap

> **Last Updated:** April 2026 | **Current Version:** 0.21.0 | **Current Phase:** Phase 2 - Team Collaboration + Agent Evolution

---

## Overview

D-PC Messenger follows a three-phase development roadmap:

1. **Phase 1: Federated MVP - COMPLETE (v0.8.0)** - Proven P2P messaging with AI collaboration
2. **Phase 1.5: Extended Features - COMPLETE (v0.9.0 - v0.18.0)** - File transfer, voice, agent, resilient infrastructure
3. **Phase 2: Team Collaboration + Agent Evolution - IN PROGRESS** - Small teams, embedded AI agent, group chat
4. **Phase 3: Scaling & Mass Decentralization - FUTURE IDEAS** - Mobile clients, global scale, 100% decentralization

---

## Phase 1: Federated MVP - COMPLETE (v0.8.0)

**Status:** PoC / Experimental
**Timeline:** Completed November 2025
**Scope:** Individual users + 1:1 peer collaboration

### Core P2P Infrastructure

| Feature | Status | Evidence | Notes |
|---------|--------|----------|-------|
| **Direct TLS Connections** | Complete | `p2p_manager.py` | Local network, self-signed certs |
| **IPv6 Dual-Stack Support** | Complete | CHANGELOG v0.8.0 | IPv4 + IPv6 automatic detection |
| **WebRTC NAT Traversal** | Complete | `webrtc_peer.py` | STUN/TURN, aiortc library |
| **Federation Hub** | Complete | `dpc-hub/` | OAuth, WebSocket signaling |
| **Cryptographic Node Identity** | Complete | `dpc-protocol/crypto.py` | RSA keys, X.509 certificates |
| **OAuth Authentication** | Complete | `dpc-hub/auth.py` | Google + GitHub providers |

### Privacy & Security

| Feature | Status | Evidence |
|---------|--------|----------|
| **Encrypted Local Backups** | Complete | AES-256-GCM, PBKDF2 600k iterations |
| **Context Firewall** | Complete | Granular access control per file/field, 7-tab UI editor |
| **AI Scope Filtering** | Complete | Work/personal mode context isolation |
| **Offline Mode** | Complete | Works without Hub, cached tokens |

### AI Collaboration

| Feature | Status | Evidence |
|---------|--------|----------|
| **Local AI Integration** | Complete | Ollama, OpenAI, Anthropic, Z.AI |
| **Remote Inference** | Complete | Borrow peer's compute |
| **Knowledge Commit System** | Complete | Git-like versioned knowledge with bias mitigation |
| **Personal Context Model v2.0** | Complete | Modular file system, 80% size reduction |
| **Cryptographic Commit Integrity** | Complete | Hash-based IDs, multi-signature, 22 tests |
| **Markdown Rendering** | Complete | 50-200x performance caching |

### Test Coverage

- **Backend:** 61 tests passing
- **Frontend:** 0 errors (svelte-check)
- **Protocol Library:** 22 commit integrity tests

---

## Phase 1.5: Extended Features - COMPLETE (v0.9.0 - v0.18.0)

**Status:** Complete
**Timeline:** December 2025 - February 2026
**Scope:** Features built beyond original Phase 1 scope, including disaster-resilient infrastructure and AI agent capabilities

These features were developed organically as the product matured, significantly expanding the platform's capabilities.

### Decentralized Infrastructure (v0.9.5 - v0.10.2)

| Feature | Version | Status | Description |
|---------|---------|--------|-------------|
| **DHT-Based Peer Discovery** | v0.9.5 | Complete | Kademlia DHT, 73 tests, internet-wide validated |
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

### What's Next (Team Features)

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

### Success Metrics

**Operational Metrics:**
- All 6 connection tiers tested and operational
- Agent consciousness uptime (thoughts per session, no recursive loops)
- Knowledge commits per session (extraction + voting pipeline)
- P2P mesh stability over 24h (2+ nodes)
- DHT lookup success rate >95% (peer discovery without Hub)

---

## Future Ideas

Ideas for future development. Not committed — direction, not promises.

### Scaling
- **Mobile clients** (iOS, Android)
- **Multi-hub federation** for large organizations
- **Hub-free operation mode** — 100% decentralized (no Hub at all)

### Advanced Security
- **Social recovery** (Shamir Secret Sharing) — recover backup passphrases via trusted peers
- **Hardware wallet integration** (Ledger, YubiKey, TPM) — hardware-backed identity

### Experimental
- **Meshtastic (LoRa) Integration** — offline text messaging via LoRa radios
- **Compute Marketplace** — monetized compute sharing
- **A2A (Agent-to-Agent)** — P2P agent communication, task delegation across peers

---

## Knowledge Architecture Sub-Roadmap

D-PC has a detailed **Knowledge Architecture** roadmap spanning 14 phases:

### Phases 1-8: COMPLETE (v0.7.0 - v0.8.0)

1. Instructions.json separation
2. Token monitoring system
3. Optional cultural perspectives
4. Robust JSON extraction (6 repair strategies)
5. Inline editing with attribution
6. Provider-specific context windows
7. Configuration system overhaul (TOML -> JSON)
8. Cryptographic commit integrity (Git-style hashes, multi-signature)

### Phases 9-14: IN PROGRESS / PLANNED

9. Context retrieval & semantic search - **Partial** (knowledge extraction voting pipeline)
10. Self-improvement tracking - **Partial** (tools.jsonl + evolution data pipeline)
11. Multi-hub federation support (knowledge sharing)
12. Collaborative knowledge building (team consensus) - **Partial** (consensus voting works for 1:1 and agent)
13. Mobile client knowledge sync
14. Advanced bias mitigation refinements

**See [docs/KNOWLEDGE_ARCHITECTURE.md](./docs/KNOWLEDGE_ARCHITECTURE.md) for full specification.**

---

## Changelog Integration

### Major Version Milestones

| Version | Date | Milestone |
|---------|------|-----------|
| **v0.21.0** | Apr 2026 | Doc audit, Protocol 13 v1.8, tool data pipeline |
| **v0.20.0** | Mar 2026 | Agent Progress Board, security hardening, knowledge integrity |
| **v0.19.0** | Feb 2026 | Group Chat — multi-participant messaging |
| **v0.18.0** | Feb 2026 | DPC Agent, reasoning models, Telegram agent bridge |
| **v0.15.0** | Jan 2026 | Cross-platform audio (Rust cpal), VRAM management |
| **v0.12.0** | Dec 2025 | Vision & image support |
| **v0.11.0** | Dec 2025 | File transfer, session management, chat history sync |
| **v0.10.0** | Dec 2025 | 6-tier connection fallback, ConnectionOrchestrator |
| **v0.9.5** | Dec 2025 | DHT peer discovery (Kademlia) |
| **v0.8.0** | Nov 2025 | Phase 1 Complete — Federated MVP |
| **v0.7.0** | Nov 2025 | Knowledge Architecture Phases 1-7 |

**See [CHANGELOG.md](./CHANGELOG.md) for complete version history.**

---

## External References

### Research & Inspiration

**Network Resilience:**
- [Privacy-Enhanced Communication Systems](https://gfw.report/publications/usenixsecurity23/en/)
- [Tor's Snowflake - WebRTC for Privacy Protection](https://snowflake.torproject.org/)

**Decentralized Systems:**
- [Kademlia DHT Paper](https://pdos.csail.mit.edu/~petar/papers/maymounkov-kademlia-lncs.pdf)
- [libp2p Specifications](https://github.com/libp2p/specs)

**Mesh Networks:**
- [Meshtastic - LoRa Mesh Networking](https://meshtastic.org/)

---

## Questions & Feedback

- [GitHub Discussions](https://github.com/mikhashev/dpc-messenger/discussions)
- [GitHub Issues](https://github.com/mikhashev/dpc-messenger/issues)
- Email: legoogmiha@gmail.com

---

**Last Updated:** April 2026
**Maintained By:** D-PC Messenger Core Team
**License:** See [LICENSE.md](./LICENSE.md)
