# D-PC Messenger Development Roadmap

> **Last Updated:** February 2026 | **Current Version:** 0.18.0 | **Current Phase:** Phase 2 - Team Collaboration + Decentralized Infrastructure

---

## Overview

D-PC Messenger follows a three-phase development roadmap:

1. **Phase 1: Federated MVP - COMPLETE (v0.8.0)** - Proven P2P messaging with AI collaboration
2. **Phase 1.5: Extended Features - COMPLETE (v0.9.0 - v0.18.0)** - File transfer, voice, agent, resilient infrastructure
3. **Phase 2: Team Collaboration - IN PROGRESS** - Small teams (2-20 people) with group chat and shared knowledge
4. **Phase 3: Scaling & Mass Decentralization - PLANNED (2027+)** - Mobile clients, global scale, 100% decentralization

---

## Phase 1: Federated MVP - COMPLETE (v0.8.0)

**Status:** Production Ready
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
**Scope:** Features built beyond original Phase 1 scope, including disaster-resilient infrastructure

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

## Phase 2: Team Collaboration - IN PROGRESS

**Status:** Infrastructure complete, team features pending
**Timeline:** Q1-Q3 2026
**Scope:** Small teams (2-20 members + AIs)

### Strategic Goals

Build the best platform for small teams to collaborate with AI-augmented intelligence, leveraging the decentralized infrastructure already completed.

### What's Done (Infrastructure)

The foundation for team collaboration is in place:
- DHT peer discovery (Hub-optional architecture)
- 6-tier connection fallback (near-universal connectivity)
- Gossip protocol (eventual delivery for offline members)
- Consensus voting mechanism (proven in knowledge commits)
- Firewall system (granular per-peer, per-group controls)
- DPC Agent (autonomous AI assistant for team workflows)

### What's Next (Team Features)

#### Phase 2.1: Team Foundations

| # | Feature | Complexity | Description |
|---|---------|------------|-------------|
| 1 | **Persistent Team Management** | Medium | Team objects (`~/.dpc/teams.json`), UI sidebar, auto-firewall integration |
| 2 | **Group Chat UI** | Medium | Multi-participant chat, presence indicators, typing notifications |
| 3 | **Team Firewall Presets** | Low | Pre-configured policies (Work Team, Study Group, Full Trust) |
| 4 | **Team Invite & Onboarding** | Medium | Short-lived invite codes, QR sharing, auto-connect workflow |

#### Phase 2.2: Knowledge Collaboration

| # | Feature | Complexity | Description |
|---|---------|------------|-------------|
| 5 | **Team Knowledge Repository** | High | Separate `~/.dpc/teams/{team_id}/knowledge/`, Git-like sync protocol |
| 6 | **Knowledge Commit Templates** | Low | Pre-defined formats (Meeting Notes, Decisions, Postmortems) |
| 7 | **Team AI Assistants** | Medium | AI queries with `team_id`, access to collective team knowledge |
| 8 | **Collaborative Context Editing** | High | Propose edits with diff generation, PR-like approval workflow |
| 9 | **Multilingual Knowledge Extraction** | Low | Language detection for knowledge proposals |

#### Phase 2.3: Polish & Advanced Features

| # | Feature | Complexity | Description |
|---|---------|------------|-------------|
| 10 | **Team Compute Pools** | Medium | Auto-discovery, load balancing, "Team Compute" panel |
| 11 | **Team Activity Feed** | Medium | Real-time event stream (joins, commits, compute sharing) |
| 12 | **DPC Agent Team Integration** | Medium | Agent tasks across team context, multi-peer coordination |

### Technical Architecture

**New Protocol Messages:**
```python
# Team Management
CREATE_TEAM, INVITE_TO_TEAM, ACCEPT_TEAM_INVITE, LEAVE_TEAM, TEAM_ROSTER_UPDATE

# Group Chat
GROUP_MESSAGE, TYPING_INDICATOR, MEMBER_PRESENCE

# Knowledge Sync
SYNC_TEAM_KNOWLEDGE, KNOWLEDGE_COMMIT_PUSH, KNOWLEDGE_DIFF_REQUEST
```

**New Backend Services:**
- `TeamManager` - Team lifecycle management
- `GroupChatManager` - Multi-peer message routing
- `TeamKnowledgeSync` - P2P knowledge syncing with conflict resolution
- `ComputePoolManager` - Resource discovery and load balancing

**Critical Files to Modify:**
1. `dpc-client/core/dpc_client_core/service.py` - Integrate new managers
2. `dpc-client/core/dpc_client_core/consensus_manager.py` - Extend for team knowledge
3. `dpc-client/ui/src/routes/+page.svelte` - Add team UI components
4. `dpc-protocol/dpc_protocol/protocol.py` - Add new protocol messages
5. `dpc-client/core/dpc_client_core/firewall.py` - Team-level permissions

### Key Architectural Decisions

**1. Hybrid DHT + Gossip for Team Discovery:**
- Kademlia DHT for efficient peer lookup (O(log n))
- Gossip protocol for resilience under network partitions
- Hub as optional bootstrap (not required)

**2. WebRTC Remains Primary Transport:**
- Widely supported, indistinguishable from mainstream traffic
- Proven in Phase 1 with STUN/TURN infrastructure

**3. DPC Agent as Team Assistant:**
- Agent leverages team context for smarter assistance
- Can coordinate tasks across team members
- Evolution system creates team-specific knowledge

### Success Metrics

**User Metrics:**
- 50+ active teams (5+ members, 10+ commits/month)
- 70% team retention after creating 5+ commits
- Teams create 2+ knowledge commits per week

**Technical Metrics:**
- 95%+ uptime for 20-node P2P mesh over 24 hours
- 99% of commits sync within 5 seconds to online members
- DHT lookup success rate >95% (peer discovery without Hub)

**Contingency Plan:**
- If 1 developer: Cut Features 11-12 (activity feed/agent team integration)
- Core deliverables: Team Management, Group Chat, Team Knowledge (Features 1-8)

---

## Phase 3: Scaling & Mass Decentralization - PLANNED (2027+)

**Status:** Design Phase
**Timeline:** 2027+
**Scope:** Global scale, mobile clients, 100% decentralization

### Strategic Goals

Phase 3 is about scaling to millions:
- Mass-market mobile adoption (iOS, Android)
- Large organization support (100+ teams, multi-hub federation)
- Complete Hub independence (100% decentralized operation)
- Advanced security features (hardware wallets, social recovery)

### Feature Categories

#### Scaling Features
- **Mobile clients** (iOS, Android) - React Native or Flutter
- **Multi-hub federation** - Large organizations with 100+ teams
- **Advanced DHT features** - libp2p integration, cross-DHT discovery
- **Hub-free operation mode** - 100% decentralized (no Hub at all)

#### Advanced Security
- **Social recovery** (Shamir Secret Sharing) - Recover backup passphrases via trusted peers
- **Hardware wallet integration** (Ledger, YubiKey, TPM) - Hardware-backed identity
- **Blockchain identity** (optional) - Decentralized identity verification

#### Stretch Goals
- **Meshtastic (LoRa) Integration** - Offline text messaging via LoRa radios
- **WiFi Mesh (802.11s) Support** - Direct TLS already works over mesh LANs
- **Compute Marketplace** - Monetized compute sharing with transaction fees

### Rationale for Phasing

**Why Phase 2 First?**
- Prove the team collaboration model works (2-20 people)
- Infrastructure foundation already built (DHT, 6-tier fallback, gossip)
- Gather user feedback before mass scaling

**Why Phase 3 Later?**
- Mobile development is expensive (2 platforms, ongoing maintenance)
- Mass-scale DHT is complex (requires Phase 2 experience)
- Complete Hub removal requires mature DHT
- Hardware wallets/blockchain add complexity (nice-to-have, not critical)

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

### Phases 9-14: PLANNED

9. Context retrieval & semantic search
10. Self-improvement tracking
11. Multi-hub federation support (knowledge sharing)
12. Collaborative knowledge building (team consensus) - **Requires Phase 2**
13. Mobile client knowledge sync - **Requires Phase 3**
14. Advanced bias mitigation refinements

**See [docs/KNOWLEDGE_ARCHITECTURE.md](./docs/KNOWLEDGE_ARCHITECTURE.md) for full specification.**

---

## Changelog Integration

### Major Version Milestones

| Version | Date | Milestone |
|---------|------|-----------|
| **v0.18.0** | Feb 2026 | DPC Agent, reasoning models, Telegram agent bridge |
| **v0.15.0** | Jan 2026 | Cross-platform audio (Rust cpal), VRAM management |
| **v0.13.0** | Jan 2026 | Voice messages (production-ready) |
| **v0.12.0** | Dec 2025 | Vision & image support |
| **v0.11.0** | Dec 2025 | File transfer, session management, chat history sync |
| **v0.10.0** | Dec 2025 | 6-tier connection fallback, ConnectionOrchestrator |
| **v0.9.5** | Dec 2025 | DHT peer discovery (Kademlia) |
| **v0.8.0** | Nov 2025 | Phase 1 Complete - Federated MVP |
| **v0.7.0** | Oct 2025 | Knowledge Architecture Phases 1-7 |

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

**Last Updated:** February 2026
**Maintained By:** D-PC Messenger Core Team
**License:** See [LICENSE.md](./LICENSE.md)
