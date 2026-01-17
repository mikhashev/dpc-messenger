# D-PC Messenger Development Roadmap

> **Last Updated:** January 2026 | **Current Version:** 0.15.1 | **Current Phase:** Phase 2.1 - Decentralized Infrastructure

---

## Overview

D-PC Messenger follows a three-phase development roadmap:

1. **Phase 1: Federated MVP - COMPLETE (v0.8.0)** - Proven P2P messaging with AI collaboration
2. **Phase 2: Team Collaboration + Disaster Resilience - IN PROGRESS (Q1-Q3 2026)** - Small teams (2-20 people) with decentralized infrastructure
3. **Phase 3: Scaling & Mass Decentralization - PLANNED (2026-2027)** - Mobile clients, global scale, 100% decentralization

---

## Phase 1: Federated MVP - COMPLETE (v0.8.0)

**Status:** Production Ready
**Timeline:** Completed November 2025
**Scope:** Individual users + 1:1 peer collaboration

### Core P2P Infrastructure

| Feature | Status | Evidence | Notes |
|---------|--------|----------|-------|
| **Direct TLS Connections** | Complete | `dpc-client/core/dpc_client_core/p2p_manager.py:242-301` | Local network, self-signed certs |
| **IPv6 Dual-Stack Support** | Complete | Commit `58d1c1c`, CHANGELOG v0.8.0 | IPv4 + IPv6 automatic detection |
| **WebRTC NAT Traversal** | Complete | `dpc-client/core/dpc_client_core/webrtc_peer.py` | STUN/TURN, aiortc library |
| **Federation Hub** | Complete | `dpc-hub/dpc_hub/main.py` | OAuth, WebSocket signaling |
| **Cryptographic Node Identity** | Complete | `dpc-protocol/dpc_protocol/crypto.py` | RSA keys, X.509 certificates |
| **OAuth Authentication** | Complete | `dpc-hub/dpc_hub/auth.py` | Google + GitHub providers |
| **Token Blacklist & Logout** | Complete | `dpc-hub/dpc_hub/crud.py` | JWT revocation support |

### Privacy & Security

| Feature | Status | Evidence | Notes |
|---------|--------|----------|-------|
| **Encrypted Local Backups** | Complete | `dpc-client/core/dpc_client_core/backup_manager.py` | AES-256-GCM, PBKDF2 600k iterations |
| **Context Firewall** | Complete | `dpc-client/core/dpc_client_core/firewall.py` | Granular access control per file/field |
| **Firewall UI Editor** | Complete | `dpc-client/ui/src/lib/components/FirewallEditor.svelte` | 7 tabs: Hub, Node Groups, File Groups, AI Scopes, Device Sharing, Compute, Peers |
| **AI Scope Filtering** | Complete | `firewall.py:332-395`, UI selector in `+page.svelte` | Local AI context filtering (work/personal modes) |
| **Rule Precedence Fix** | Complete | v0.10.2 wildcard override bug fix | Specific deny rules now override wildcard allow rules |
| **No Message Persistence** | Complete | Architecture design | Transactional communication |
| **Offline Mode** | Complete | `docs/OFFLINE_MODE.md` | Works without Hub, cached tokens |

### AI Collaboration

| Feature | Status | Evidence | Notes |
|---------|--------|----------|-------|
| **Local AI Integration** | Complete | `dpc-client/core/dpc_client_core/llm_manager.py` | Ollama, OpenAI, Anthropic |
| **Remote Inference** | Complete | Protocol messages, `service.py:2411-2481` | Borrow peer's compute |
| **Model Discovery** | Complete | `GET_PROVIDERS` message, auto-discovery | Shows peer AI models |
| **Usage Tracking** | Complete | `effectiveness_tracker.py`, token fields | Token counts, effectiveness metrics |
| **Streaming Responses** | Partial | Local AI only | Remote inference doesn't stream yet |
| **Collaborative Knowledge Building** | Complete | `consensus_manager.py` | Multi-peer consensus voting |
| **Devil's Advocate Mechanism** | Complete | `consensus_manager.py:92-94` | Required dissenter for 3+ participants |
| **Knowledge Commit System** | Complete | `conversation_monitor.py`, `commit_integrity.py` | Git-like versioned knowledge |
| **Personal Context Model v2.0** | Complete | `docs/PERSONAL_CONTEXT_V2_IMPLEMENTATION.md` | Modular file system, 80% size reduction |
| **Conversation History Optimization** | Complete | `service.py`, hash-based tracking | 60-80% token savings |
| **Cryptographic Commit Integrity** | Complete | `commit_integrity.py`, 22 tests passing | Hash-based IDs, multi-signature |
| **Markdown Rendering** | Complete | `MarkdownMessage.svelte` | 50-200x performance, caching |
| **Bias Mitigation** | Complete | `knowledge_commit.py`, cultural perspectives | Multi-perspective analysis |

### Key Achievements Beyond Original Roadmap

**Phase 1 exceeded expectations with significant overdelivery:**

- Personal Context Model v2.0 (not originally scoped)
- Conversation history optimization (added based on user feedback)
- IPv6 support (future-proofing)
- Markdown rendering (UX improvement)
- Cryptographic commit integrity (security enhancement)
- Device context collection (structured hardware/software info)
- Complete Firewall UI editor (7 tabs including AI Scopes, File Groups, Device Sharing)
- AI Scope filtering for local AI (work/personal mode context isolation)
- Critical security fix (wildcard override bug affecting all filtering methods)

### Test Coverage

- **Backend:** 61 tests passing (including 30 firewall tests with AI scope + wildcard precedence coverage)
- **Frontend:** 0 errors (svelte-check)
- **Protocol Library:** 22 commit integrity tests
- **Firewall Tests:** Comprehensive coverage for AI scopes, peer filtering, device context, rule precedence

---

## Phase 2: Team Collaboration + Disaster Resilience - IN PROGRESS (Q1-Q3 2026)

**Status:** Planning Complete, Implementation Pending
**Timeline:** 6-9 months (Q1-Q3 2026)
**Scope:** Small teams (2-20 members + AIs), resilience in challenging network conditions
**Team Size:** 1-2 developers

### Strategic Goals

**Phase 2 combines two critical objectives:**

1. **Team Collaboration** - Build the best platform for small teams to collaborate with AI-augmented intelligence
2. **Disaster Resilience** - Ensure D-PC works in disaster scenarios and challenging network conditions (infrastructure failures, natural disasters, network outages)

### Feature Roadmap

#### Phase 2.1: Foundations + Decentralized Infrastructure (Months 1-3)

**Team Collaboration:**

| # | Feature | Complexity | Description |
|---|---------|------------|-------------|
| 1 | Persistent Team Management | Medium | Team objects (`~/.dpc/teams.json`), UI sidebar, auto-firewall integration |
| 2 | Group Chat UI | Medium | Multi-participant chat, presence indicators, typing notifications |
| 3 | Team Firewall Presets | Low | Pre-configured policies (Work Team, Study Group, Full Trust) |
| 4 | Team Invite & Onboarding | Medium | Short-lived invite codes, QR sharing, auto-connect workflow |

**Resilient Infrastructure (CRITICAL):**

| # | Feature | Complexity | Status | Description |
|---|---------|------------|--------|-------------|
| 5 | DHT-Based Peer Discovery | High | ✅ **COMPLETE** (v0.9.5) | Kademlia DHT, decentralized signaling, eliminates Hub dependency |
| 6 | Fallback Logic & Hybrid Mode | High | ✅ **COMPLETE** (v0.10.0) | 6-tier connection hierarchy fully integrated (IPv6, IPv4, WebRTC, hole punch, relay, gossip), ConnectionOrchestrator operational, Hub-optional architecture |
| 7 | Pluggable Transport Framework | Medium | ⏭️ Deferred (replaced by #6) | Auto-fallback architecture, transport abstraction |
| 8 | WebSocket-over-TLS Transport | Medium | ⏭️ Deferred | HTTPS transport, fallback when WebRTC unavailable |

**DHT Implementation Details (v0.9.5):**
- ✅ Core DHT data structures (XOR distance, 128 k-buckets, routing table)
- ✅ UDP RPC layer (PING/PONG, FIND_NODE, STORE, FIND_VALUE)
- ✅ DHT manager (bootstrap, iterative lookup, announce, maintenance)
- ✅ P2P integration (DHT-first connection strategy, WebSocket API)
- ✅ Internet-wide testing validated (cross-continent peer discovery)
- ✅ 73 unit tests (100% passing coverage)
- ✅ NAT hairpinning fix, bootstrap retry, dynamic IP announcement
- Files created: [dht/distance.py](dpc-client/core/dpc_client_core/dht/distance.py), [dht/routing.py](dpc-client/core/dpc_client_core/dht/routing.py), [dht/rpc.py](dpc-client/core/dpc_client_core/dht/rpc.py), [dht/manager.py](dpc-client/core/dpc_client_core/dht/manager.py)

**Fallback Logic Implementation Details (v0.10.0):**
- ✅ ConnectionOrchestrator - Intelligent strategy coordinator with 6-tier fallback
- ✅ DHT schema enhancement - IPv4/IPv6/relay/punch metadata
- ✅ Priority 1-2: IPv6/IPv4 direct connection strategies
- ✅ Priority 3: Hub WebRTC integration (existing STUN/TURN)
- ✅ Priority 4: UDP hole punching (DHT-coordinated, 60-70% NAT success)
- ✅ Priority 5: Volunteer relay nodes (100% NAT coverage, privacy-preserving)
- ✅ Priority 6: Gossip store-and-forward (epidemic spreading, disaster resilience)
- ✅ VectorClock - Lamport timestamps for distributed causality
- ✅ Anti-entropy sync - Periodic reconciliation (5-minute interval)
- ✅ Configuration: 4 new config sections (connection, hole_punch, relay, gossip), 23 getter methods
- Files created: [coordinators/connection_orchestrator.py](dpc-client/core/dpc_client_core/coordinators/connection_orchestrator.py), [managers/hole_punch_manager.py](dpc-client/core/dpc_client_core/managers/hole_punch_manager.py), [managers/relay_manager.py](dpc-client/core/dpc_client_core/managers/relay_manager.py), [managers/gossip_manager.py](dpc-client/core/dpc_client_core/managers/gossip_manager.py), plus 12 supporting files

**Files Deferred to Phase 2.2:**
- `dpc-client/core/dpc_client_core/pluggable_transports.py` - Transport manager
- `dpc-client/core/dpc_client_core/websocket_tls_transport.py` - HTTPS stealth transport

#### Phase 2.2: Knowledge Collaboration (Months 4-6)

| # | Feature | Complexity | Description |
|---|---------|------------|-------------|
| 8 | Team Knowledge Repository | High | Separate `~/.dpc/teams/{team_id}/knowledge/`, Git-like sync protocol |
| 9 | Knowledge Commit Templates | Low | Pre-defined formats (Meeting Notes, Decisions, Postmortems) |
| 10 | Team AI Assistants | Medium | AI queries with `team_id`, access to collective team knowledge |
| 11 | Collaborative Context Editing | High | Propose edits with diff generation, PR-like approval workflow |
| 12 | **Multilingual Knowledge Extraction** | Low | **Language detection for knowledge proposals** - Uses Whisper's built-in language detection for voice + non-ASCII heuristic for text, adds language metadata to KnowledgeEntry |

#### Phase 2.3: Polish & Offline Mesh (Months 7-9)

**Team Features:**

| # | Feature | Complexity | Description |
|---|---------|------------|-------------|
| 12 | Team Compute Pools | Medium | Auto-discovery, load balancing, "Team Compute" panel |
| 13 | Team Activity Feed | Medium | Real-time event stream (joins, commits, compute sharing) |
| 14 | Team Analytics Dashboard | Medium | Knowledge growth, contributions, AI usage (privacy-preserving) |
| 15 | Team Session Management | Medium | AI-facilitated sessions (standups, brainstorming, retrospectives) |

**Offline Mesh Features (Stretch Goals):**

| # | Feature | Complexity | Description |
|---|---------|------------|-------------|
| 16 | WiFi Mesh (802.11s) Support | Low | Documentation only - Direct TLS already works over mesh LANs |
| 17 | Meshtastic (LoRa) Integration | Medium | Offline text messaging via LoRa radios ($30-80 hardware) |
| 18 | Starlink Backup | Zero | Already works - document that WebRTC works over satellite |

### Implementation Timeline

| Month | Focus | Deliverables | Dependencies |
|-------|-------|--------------|--------------|
| **1** | Foundations + DHT | Team Management (2w), Firewall Presets (1w), DHT Discovery (3w), Testing harness (1w) | None |
| **2** | Group Chat + Transports | Group Chat UI (2w), Pluggable Transports (2w), WebSocket-TLS (1w), Team Invites (1w) | Month 1 |
| **3** | Testing & Polish | Bug fixes (2w), Load testing 20 nodes + DHT stress test (1w), Documentation (1w) | Month 2, alpha testing |
| **4** | Team Knowledge | Full month dedicated to team knowledge syncing (complex P2P sync protocol) | Month 3 |
| **5** | Knowledge Tooling | Templates (2w), Team AI (2w), Beta testing with 10 teams | Month 4 |
| **6** | Collaborative Editing | Knowledge editing + diff generation (3w), Bug fixes from beta (1w) | Month 5 |
| **7** | Compute & Awareness | Compute Pools (2w), Activity Feed (2w) | Month 6 |
| **8** | Polish & Optional Mesh | Analytics Dashboard (2w), Session Management (1w), **BONUS:** Meshtastic (1w) | Month 7 |
| **9** | Release Prep | Bug fixes (2w), Documentation (1w), **BONUS:** WiFi mesh docs (1w) | Month 8 |

**Contingency Plan:**
- If 1 developer or part-time: Cut Features 14-15 (analytics/sessions) + skip Meshtastic
- If ahead of schedule: Implement Meshtastic in Month 8 (stretch goal)
- Core deliverables: DHT, Pluggable Transports, Team Collaboration (Features 1-13)

### Technical Architecture

**New Protocol Messages:**
```python
# Team Management
CREATE_TEAM, INVITE_TO_TEAM, ACCEPT_TEAM_INVITE, LEAVE_TEAM, TEAM_ROSTER_UPDATE

# Group Chat
GROUP_MESSAGE, TYPING_INDICATOR, MEMBER_PRESENCE

# Knowledge Sync
SYNC_TEAM_KNOWLEDGE, KNOWLEDGE_COMMIT_PUSH, KNOWLEDGE_DIFF_REQUEST

# Compute Pool
GET_AVAILABLE_MODELS, COMPUTE_POOL_STATUS

# DHT Discovery (Disaster Resilience)
DHT_ANNOUNCE, DHT_LOOKUP, DHT_BOOTSTRAP

# Transport Negotiation (Disaster Resilience)
TRANSPORT_PROBE, TRANSPORT_FALLBACK_OFFER
```

**New Backend Services:**
- `TeamManager` - Team lifecycle management
- `GroupChatManager` - Multi-peer message routing
- `TeamKnowledgeSync` - P2P knowledge syncing with conflict resolution
- `ComputePoolManager` - Resource discovery and load balancing
- `DHTDiscovery` - Kademlia DHT for decentralized peer discovery
- `TransportManager` - Pluggable transport abstraction
- `WebSocketTLSTransport` - HTTPS masquerade transport
- `MeshtasticTransport` - LoRa mesh adapter (stretch goal)

**Critical Files to Modify:**

*Team Collaboration:*
1. `dpc-client/core/dpc_client_core/service.py` - Integrate new managers
2. `dpc-client/core/dpc_client_core/consensus_manager.py` - Extend for team knowledge
3. `dpc-client/ui/src/routes/+page.svelte` - Add team UI components
4. `dpc-protocol/dpc_protocol/protocol.py` - Add new protocol messages
5. `dpc-client/core/dpc_client_core/firewall.py` - Team-level permissions

*Disaster Resilience:*
6. `dpc-client/core/dpc_client_core/p2p_manager.py` - Integrate TransportManager
7-10. New files listed above

### Key Architectural Decisions

**1. Hybrid DHT + Gossip for Discovery:**
- **Primary:** Kademlia DHT for efficient peer lookup (O(log n))
- **Redundancy:** Gossip protocol for DHT poisoning resistance
- **Rationale:** DHT for normal operations, gossip ensures resilience under attack

**2. Keep Hub as Optional Bootstrap:**
- Hub remains as ONE OF MANY bootstrap methods
- DHT becomes primary discovery mechanism
- Hub provides fallback when DHT partition occurs
- **Rationale:** Pragmatic balance of decentralization + reliability

**3. WebRTC Remains Primary Transport:**
- Widely supported across networks and platforms
- Used by privacy-focused systems for reliable connectivity
- Indistinguishable from Google Meet/WhatsApp traffic
- **Rationale:** Best reliability and network resilience vs. QUIC/Iroh alternatives

**4. IPv6 Native Support:**
- Already tested and working (commit 58d1c1c)
- Dual-stack (IPv4 + IPv6) out of box
- **Advantage:** Future-proof networking with broader connectivity options

### Technical Challenges & Mitigations

| Challenge | Mitigation Strategy | Decision Point |
|-----------|---------------------|----------------|
| **P2P Mesh Scalability** (20 nodes) | Hub relay mode fallback, lazy connections (5-7 active), load test Month 3 | Month 3 |
| **Knowledge Sync Conflicts** | Last-write-wins with commit IDs, conflict detection UI, immutable history | Month 4 |
| **Partial Connectivity** (offline members) | Gossip protocol, store-and-forward buffering, optional Hub backup | Month 2 |
| **Privacy vs Team Sharing** | Granular per-member firewall, team-specific contexts, audit logs | Month 1 |
| **DHT Security & Poisoning** | Cryptographic node verification, redundant queries, gossip backup, reputation system | Month 2 |

### Success Metrics

**User Metrics (Team Collaboration):**
- 50+ active teams (5+ members, 10+ commits/month) by end of Phase 2
- 70% team retention after creating 5+ commits
- Teams create 2+ knowledge commits per week on average

**Technical Metrics (Reliability):**
- 95%+ uptime for 20-node P2P mesh over 24 hours
- 99% of commits sync within 5 seconds to online members
- <10% failure rate for remote inference (excluding network issues)
- DHT lookup success rate >95% (peer discovery without Hub)

**Resilience Metrics (Critical):**
- **Transport fallback success:** 99%+ connection success rate via auto-fallback
- **DHT decentralization:** 80%+ of connections established without Hub
- **Network robustness:** Reliable operation across diverse network conditions
- **Pluggable transports:** WebSocket-TLS works in environments where WebRTC is slow/unreliable

### Why Phase 2 is Achievable in 6-9 Months

**Strong Foundation (Phase 1 Complete):**
- WebRTC P2P already working
- IPv6 dual-stack tested
- Consensus voting mechanism proven
- Firewall system for privacy control

**Proven Technologies:**
- DHT: Kademlia (BitTorrent-proven, O(log n) lookups)
- WebRTC: Privacy-focused systems (reliable, actively used)
- Meshtastic: Open-source LoRa mesh, active community
- WiFi Mesh: 802.11s IEEE standard, OpenWRT support

**Pragmatic Scope:**
- Focus on 2-20 person teams (not scaling to millions)
- Desktop-first (mobile deferred to Phase 3)
- Stretch goals clearly marked (Meshtastic, WiFi mesh)
- Willing to cut analytics/sessions if needed

**Risk Mitigation:**
- High-risk items front-loaded (DHT Month 1, Pluggable Transports Month 2)
- Load testing in Month 3 (validates architecture early)
- Parallel development possible (team features vs. resilience features independent)
- Fallback positions defined (DHT fails → Keep Hub, Mesh unstable → Hub relay)

---

## Phase 3: Scaling & Mass Decentralization - PLANNED (2026-2027)

**Status:** Design Phase
**Timeline:** 2026-2027
**Scope:** Global scale, mobile clients, 100% decentralization

### Strategic Goals

**Phase 3 is about scaling to millions:**
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
- **Dedicated TURN server deployment** - Production-grade relay infrastructure

#### Advanced Security
- **Social recovery** (Shamir Secret Sharing) - Recover backup passphrases via trusted peers
- **Hardware wallet integration** (Ledger, YubiKey, TPM) - Hardware-backed identity
- **Blockchain identity** (optional) - Decentralized identity verification
- **QR code backup transfer** - Mobile-to-mobile backup sharing

#### DHT Evolution: Phase 2 vs. Phase 3

**Phase 2 DHT (Basic):**
- Kademlia implementation for peer discovery
- Eliminates Hub as single point of failure
- Works for small teams (2-20 nodes)
- Hub remains as optional bootstrap

**Phase 3 DHT (Advanced):**
- libp2p integration for interoperability
- Cross-DHT discovery (connect across different DHT networks)
- 100% hub-free operation (no Hub dependency at all)
- Support for 1000s of nodes (mass-scale federation)

### Rationale for Phasing

**Why Phase 2 First?**
- Prove the team collaboration model works (2-20 people)
- Build decentralized infrastructure foundation (DHT, pluggable transports)
- Test disaster resilience in real-world environments
- Gather user feedback before mass scaling

**Why Phase 3 Later?**
- Mobile development is expensive (2 platforms, ongoing maintenance)
- Mass-scale DHT is complex (requires Phase 2 experience)
- Complete Hub removal requires mature DHT (can't rush decentralization)
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
7. Configuration system overhaul (TOML → JSON)
8. Cryptographic commit integrity (Git-style hashes, multi-signature)

### Phases 9-14: PLANNED

9. Context retrieval & semantic search
10. Self-improvement tracking
11. Multi-hub federation support (knowledge sharing)
12. Collaborative knowledge building (team consensus)
13. Mobile client knowledge sync
14. Advanced bias mitigation refinements

**See [docs/KNOWLEDGE_ARCHITECTURE.md](./docs/KNOWLEDGE_ARCHITECTURE.md) for full specification.**

---

## Changelog Integration

### What Changed in Recent Versions

**v0.8.0 (November 2025)** - Phase 1 Complete
- Personal Context Model v2.0 (modular file system)
- Cryptographic commit integrity (hash-based IDs, multi-signature)
- Conversation history optimization (60-80% token savings)
- Markdown rendering with intelligent caching
- IPv6 dual-stack support

**v0.7.0 (October 2025)** - Knowledge Architecture Phases 1-7
- Instructions.json separation
- Token monitoring system
- Cultural perspectives toggle
- JSON extraction with fallback strategies
- Configuration system overhaul (TOML → JSON)

**See [CHANGELOG.md](./CHANGELOG.md) for complete version history.**

---

## How to Contribute to Roadmap Development

### Current Opportunities

**Phase 2 Planning (Now - Q1 2026):**
- Review Phase 2 feature specifications
- Provide feedback on DHT architecture
- Suggest team collaboration workflows
- Test alpha releases with your team

**Phase 2 Development (Q1-Q3 2026):**
- Backend development (Python)
- Frontend development (SvelteKit + Tauri)
- Protocol implementation (DPTP extensions)
- Testing and QA (multi-peer scenarios)

**See [CONTRIBUTING.md](./CONTRIBUTING.md) for contribution guidelines.**

---

## External References

### Research & Inspiration

**Network Resilience:**
- [Privacy-Enhanced Communication Systems](https://gfw.report/publications/usenixsecurity23/en/)
- [Tor's Snowflake - WebRTC for Privacy Protection](https://snowflake.torproject.org/)
- [Network Transport Security Research](https://upb-syssec.github.io/blog/)

**Decentralized Systems:**
- [Kademlia DHT Paper](https://pdos.csail.mit.edu/~petar/papers/maymounkov-kademlia-lncs.pdf)
- [libp2p Specifications](https://github.com/libp2p/specs)
- [IPFS & DHT](https://docs.ipfs.tech/concepts/dht/)

**Mesh Networks:**
- [Meshtastic - LoRa Mesh Networking](https://meshtastic.org/)
- [OpenWRT 802.11s Mesh Guide](https://www.tekovic.com/blog/openwrt-80211s-mesh-networking/)

---

## Questions & Feedback

**Have questions about the roadmap?**
- [GitHub Discussions](https://github.com/mikhashev/dpc-messenger/discussions)
- [GitHub Issues](https://github.com/mikhashev/dpc-messenger/issues)
- Email: legoogmiha@gmail.com

**Want to suggest a feature?**
- Open an issue with `[Feature Request]` label
- Propose it in GitHub Discussions
- Contribute to planning documents

---

**Last Updated:** November 2025
**Maintained By:** D-PC Messenger Core Team
**License:** See [LICENSE.md](./LICENSE.md)
