# D-PC Messenger: Privacy-First Platform for Human-AI Collaboration

> **Status:** MVP Ready | **License:** Multi-License (GPL/LGPL/AGPL/CC0) | **Version:** 0.10.1 (dev)
> **Platforms:** Windows | Linux | macOS
> **Note:** This software is for educational/research use. Please review the full [Legal Notice](#%EF%B8%8F-legal-notice--compliance) before use.

---

## The Vision: A Private Internet for Human-AI Collaboration

**The Immediate Problem:** AI is transforming how we work and think, but it's creating two critical failures:

1. **"Digital Debris"** — Endless chat histories where valuable insights get lost in noise. We need conversations that *extract permanent knowledge*, not create message archives.
2. **"Computational Inequality"** — Only people with powerful hardware (or expensive cloud subscriptions) can use advanced AI. We need *democratized access* through peer-to-peer compute sharing.

**D-PC Messenger solves both:**

- **Knowledge Commits** transform ephemeral conversations into structured, versioned knowledge (like git commits for your personal context)
- **P2P Compute Sharing** lets you borrow a friend's GPU to run powerful AI models — no cloud, no subscriptions, complete privacy
- **Context Collaboration** enables your AI assistants to work together by securely sharing relevant knowledge

**The Deeper Mission:** This isn't just productivity software. We're building infrastructure for **human-AI co-evolution** — ensuring humans can maintain cognitive parity as artificial general intelligence emerges.

Your personal context (conversations, insights, learned knowledge) should function like **"DNA for knowledge"** — portable across devices and lifetimes, evolvable through collaborative learning, owned and controlled by you (not extracted by corporations). In 10-20 years, when AI assistants from birth and brain-computer interfaces become mainstream, your accumulated "knowledge DNA" will determine your cognitive capacity in AI-augmented society.

We're racing against a closing window: the infrastructure being built TODAY will determine whether your future AI interactions are sovereign tools under your control, or rented products that extract and monetize your cognition.

**[Read the full vision →](./VISION.md)**

---

## Join the Mission: Seeking a Business Co-Founder

The core technology is built, but the mission to build a world-changing company has just begun.

I am a product-focused technical founder looking for a business-focused co-founder to be my partner. If you have a passion for privacy, a deep understanding of go-to-market strategy, and the drive to build a global community, I want to talk to you.

If this vision resonates with you, let's connect:
[legoogmiha@gmail.com](mailto:legoogmiha@gmail.com) | [LinkedIn](https://www.linkedin.com/in/mikemikhashev) | [X/Twitter](https://x.com/mikeshev4enko)

---

## What is D-PC Messenger?

**D-PC Messenger** (Decentralized Personal Context) is a **privacy-first, peer-to-peer platform for human-AI collaboration**.

**The Core Idea:** Imagine you're working with your personal AI assistant (ChatGPT, Claude, or local Ollama). Now imagine securely sharing relevant context with a trusted friend so *their* AI assistant can help too - without compromising privacy or relying on centralized servers.

**How it works:**
- **You + Your AI** - Chat with your personal AI assistant about anything
- **Collaborate with Humans** - Connect directly with trusted peers via encrypted P2P messaging
- **Share Context Securely** - Share conversation histories, documents, or knowledge with peers
- **Their AI Gets Smarter** - Your shared context helps their AI give better, more informed answers
- **Complete Privacy** - All data stays on your devices, encrypted end-to-end, with granular access control

**Real-world example:** You're researching a technical topic with your AI assistant. You share your conversation history with a colleague. Their AI can now see your research and provide complementary insights, creating a **collective intelligence** between humans and their AI assistants.

**Philosophy:** [Digital Self-Sovereignty](./docs/USER_SOVEREIGNTY.md) - Your data, your keys, your control. No backdoors, no data mining, no compromises.

---

## Key Features

### For End Users
- **True Privacy** - Messages never touch servers, only peers
- **User Sovereignty** - You own your data, identity, and encryption keys ([read more](./docs/USER_SOVEREIGNTY.md))
- **Encrypted Backups** - AES-256-GCM encrypted backups with no backdoors ([guide](./docs/BACKUP_RESTORE.md))
- **Human-AI Collaboration** - Work with your AI assistant, then share contexts with trusted peers so their AIs can contribute too
- **Conversation History** - Full conversational continuity with smart context optimization (60-80% token savings)
- **Rich Markdown Rendering** - AI responses display with GitHub-style formatting, intelligent caching (50-200x faster)
- **Knowledge Commits** - Git-like versioning for AI-extracted knowledge with bias mitigation ([architecture](./docs/KNOWLEDGE_ARCHITECTURE.md))
- **Local-First** - Your data stays on your device
- **Universal Connectivity** - Intelligent 6-tier connection orchestrator ensures connectivity in nearly any network condition (IPv6, IPv4, WebRTC, UDP hole punching, volunteer relays, gossip)
- **Hub-Optional Architecture** - Works seamlessly offline with direct connections, DHT-based hole punching, and volunteer relay nodes
- **Resilient Messaging** - Gossip store-and-forward protocol ensures message delivery even during infrastructure outages
- **Cryptographic Identity** - Self-sovereign node IDs based on public keys
- **Context Firewall** - Granular control over what data you share

### For Developers
- **Open Protocol** - Extensible [DPTP (D-PC Transfer Protocol)](./specs/dptp_v1.md)
- **Modular Design** - Clear separation of concerns
- **Knowledge Architecture** - Git-like knowledge commits with cognitive bias mitigation ([architecture doc](./docs/KNOWLEDGE_ARCHITECTURE.md))
- **Easy Integration** - Use any AI provider (Ollama, OpenAI, Claude)
- **Production Ready** - Docker deployment, OAuth, rate limiting

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│              Human-AI Collaborative Intelligence                │
└─────────────────────────────────────────────────────────────────┘

┌───────────────────┐                        ┌───────────────────┐
│    Human A        │                        │    Human B        │
│  ┌─────────────┐  │                        │  ┌─────────────┐  │
│  │ AI Assistant│  │  P2P Encrypted Context │  │ AI Assistant│  │
│  │  (GPT/Llama)│  │◄───── Sharing ────────►│  │  (Claude)   │  │
│  └─────────────┘  │                        │  └─────────────┘  │
│   • Chat History  │                        │   • Chat History  │
│   • Documents     │                        │   • Documents     │
│   • Context Store │                        │   • Context Store │
│   • Firewall      │                        │   • Firewall      │
└────────┬──────────┘                        └────────┬──────────┘
         │                                            │
         │            ┌─────────────────┐             │
         └────────────►  Federation Hub  ◄────────────┘
                      │  (Optional)     │
                      │  • Discovery    │
                      │  • Signaling    │
                      │  • OAuth        │
                      │  • NO Messages  │
                      │  • NO Context   │
                      └─────────────────┘
```

### Two Connection Methods

1. **Direct TLS** (Local Network)
   - Fastest, lowest latency
   - Requires network visibility
   - Uses cryptographic node certificates
   - **No Hub required** - Fully peer-to-peer

2. **WebRTC** (Internet-Wide)
   - Works across NAT/firewalls
   - Automatic NAT traversal via STUN/TURN
   - Hub only for initial signaling
   - **Messages never pass through Hub** - Direct P2P connection

### Important: True Peer-to-Peer Architecture

**D-PC Messenger is NOT a messaging service.** It is peer-to-peer communication software.

**What this means:**
- ✅ Messages are transmitted **directly between users** via encrypted P2P connections
- ✅ The creator does **NOT operate message relay infrastructure**
- ✅ The creator does **NOT store or transmit user messages**
- ✅ The creator does **NOT control user communications**

**Hub Architecture:**
- Provides WebRTC signaling for NAT traversal (like STUN/TURN servers)
- Messages flow directly P2P between clients (Hub never sees content)
- Direct TLS connections work independently of Hub for local networks
- Hub-free operation mode planned for Phase 3 (DHT-based discovery)

**Legal implication:** This software provides a communication tool, not a communication service. Users are solely responsible for their use of this software and any infrastructure they choose to deploy.

---

## Quick Start

### Prerequisites

- **Python** 3.12+ with Poetry
- **Node.js** 18+ with npm
- **Rust** (install via [rustup.rs](https://rustup.rs/))
- **Docker** (for Hub database)

### Option 1: Local Testing (No Hub)

Test Direct TLS connections on your local network:

```bash
# Terminal 1: Start Client 1
cd dpc-client/core
poetry install
poetry run python run_service.py

# Terminal 2: Start UI for Client 1
cd dpc-client/ui
npm install
npm run tauri dev

# Repeat for Client 2 on another machine (same network)
# Connect using dpc:// URI displayed in the app
```

### Option 2: Full Setup with Hub (WebRTC)

Enable internet-wide connections:

**1. Start the Hub:**
```bash
cd dpc-hub
docker-compose up -d              # Start PostgreSQL
cp .env.example .env              # Configure (edit SECRET_KEY)
poetry install
poetry run alembic upgrade head   # Run migrations
poetry run uvicorn dpc_hub.main:app --host 0.0.0.0
```

**2. Start the Client:**
```bash
cd dpc-client/core
poetry install
poetry run python run_service.py

# In another terminal
cd dpc-client/ui
npm run tauri dev
```

**3. Authenticate and Connect:**
- Login via OAuth (Google or GitHub) in the UI
- **NEW:** Client automatically registers cryptographic node identity
- Enter peer's `node_id` in the UI
- Click "Connect via Hub"
- WebRTC automatically establishes a direct P2P connection

**4. Secure Your Data (Recommended):**
```bash
# Create encrypted backup of your .dpc directory
cd dpc-client/core
poetry run python -m dpc_client_core.cli_backup create

# Your backup is saved to ~/dpc_backup_TIMESTAMP.dpc
# Store it on USB drive or encrypted cloud storage
```

**See [docs/QUICK_START.md](./docs/QUICK_START.md) for detailed instructions.**
**Backup guide: [docs/BACKUP_RESTORE.md](./docs/BACKUP_RESTORE.md)**
**GitHub OAuth: [docs/GITHUB_AUTH_SETUP.md](./docs/GITHUB_AUTH_SETUP.md)**

---

## Production Deployment

### Deploy Hub to VPS

For production use, deploy the Hub on a public server:

```bash
# On your VPS (Ubuntu 22.04+)
git clone https://github.com/mikhashev/dpc-messenger.git
cd dpc-messenger/dpc-hub

# Configure production settings
cp .env.example .env
nano .env  # Add production credentials (SECRET_KEY, OAuth, etc.)

# Deploy with Docker Compose
docker-compose -f docker-compose.prod.yml up -d

# Set up Nginx + SSL
# See docs/WEBRTC_SETUP_GUIDE.md for complete instructions
```

**Full guide: [docs/WEBRTC_SETUP_GUIDE.md](./docs/WEBRTC_SETUP_GUIDE.md)**

---

## Authentication & Security

### Authentication Flow (v0.6.0)

1. **OAuth Login** - User authenticates via **Google or GitHub** ([setup guide](./docs/GITHUB_AUTH_SETUP.md))
2. **Temporary Node ID** - Hub assigns temporary ID
3. **Cryptographic Registration** - Client registers public key & certificate
4. **Verified Identity** - Hub validates and marks node_id as verified
5. **JWT Token** - Client receives JWT for API access
6. **Logout Support** - Tokens can be blacklisted upon logout

### Security Features

- End-to-end encryption (DTLS in WebRTC, TLS in Direct)
- **Client-side encrypted backups** - AES-256-GCM with PBKDF2 (600k iterations)
- Cryptographic node identities (derived from RSA public keys)
- Context firewall (.dpc_access.json) for granular permissions
- JWT authentication with Hub (token blacklisting supported)
- No message persistence by default
- Node identity validation (certificates, public keys)
- **No backdoors** - If you lose your passphrase, data is permanently unrecoverable (by design)

---

## Documentation

### Getting Started
- **[QUICK_START.md](./docs/QUICK_START.md)** - 5-minute setup guide
- **[dpc-client/README.md](./dpc-client/README.md)** - Client setup & development
- **[dpc-hub/README.md](./dpc-hub/README.md)** - Hub deployment guide

### Security & Privacy
- **[USER_SOVEREIGNTY.md](./docs/USER_SOVEREIGNTY.md)** - Privacy philosophy & digital self-sovereignty
- **[BACKUP_RESTORE.md](./docs/BACKUP_RESTORE.md)** - Encrypted backup & restore guide
- **[GITHUB_AUTH_SETUP.md](./docs/GITHUB_AUTH_SETUP.md)** - GitHub OAuth authentication setup

### WebRTC & Networking
- **[WEBRTC_SETUP_GUIDE.md](./docs/WEBRTC_SETUP_GUIDE.md)** - Complete WebRTC setup
- **[README_WEBRTC_INTEGRATION.md](./docs/README_WEBRTC_INTEGRATION.md)** - Technical overview

### Configuration & Features
- **[CONFIGURATION.md](./docs/CONFIGURATION.md)** - Complete configuration guide
- **[OFFLINE_MODE.md](./docs/OFFLINE_MODE.md)** - Offline mode features & usage
- **[LOGGING.md](./docs/LOGGING.md)** - Logging system configuration & troubleshooting
- **Environment Variables** - All settings support env var overrides
- **Config File** - `~/.dpc/config.ini` for persistent settings

### Vision & Philosophy
- **[VISION.md](./VISION.md)** - Business vision, market opportunity, and mission
- **[PRODUCT_VISION.md](./PRODUCT_VISION.md)** - Product vision & technical philosophy
- **[USER_SOVEREIGNTY.md](./docs/USER_SOVEREIGNTY.md)** - Privacy philosophy & digital self-sovereignty

### Architecture & Design
- **[KNOWLEDGE_ARCHITECTURE.md](./docs/KNOWLEDGE_ARCHITECTURE.md)** - Knowledge management architecture with cognitive bias mitigation
- **[specs/hub_api_v1.md](./specs/hub_api_v1.md)** - Hub API specification

### Legal
- **[LICENSE.md](./LICENSE.md)** - Licensing explained

---

## Roadmap

### Phase 1: Federated MVP - COMPLETE (v0.10.1)
**Status:** Production Ready | **Timeline:** Completed

**Core Infrastructure:**
- Direct TLS P2P connections (local network + IPv6 dual-stack)
- WebRTC with NAT traversal (STUN/TURN)
- **6-Tier Connection Fallback** (IPv6 → IPv4 → WebRTC → UDP Hole Punch → Relay → Gossip)
- Federation Hub for discovery and OAuth (now optional!)
- Cryptographic node identity system
- Token blacklist and logout
- Offline mode with graceful degradation

**Privacy & Security:**
- **Encrypted local backups** (AES-256-GCM, PBKDF2 600k iterations)
- **Context firewall with granular access control**
  - Complete UI editor with 7 tabs (Hub, Node Groups, File Groups, AI Scopes, Device Sharing, Compute, Peers)
  - AI Scope filtering for local AI (work/personal mode context isolation)
  - Wildcard precedence fix (specific deny overrides wildcard allow)
- No message persistence (transactional communication)
- End-to-end encryption (DTLS/TLS for all 6 connection strategies)

**AI Collaboration:**
- Local AI integration (Ollama, OpenAI, Anthropic)
- **Remote inference** - Borrow compute from trusted peers ([guide](./docs/REMOTE_INFERENCE.md))
  - Model discovery (auto-discovery on connection)
  - Usage tracking (token counts, effectiveness metrics)
  - Streaming responses (planned for Phase 2)
- **Collaborative knowledge building** - Multi-peer consensus voting with devil's advocate
- **Knowledge Commit System** - Git-like versioned knowledge ([architecture](./docs/KNOWLEDGE_ARCHITECTURE.md))
  - Personal Context Model (PCM) v2.0 with modular file system
  - Conversation history optimization (60-80% token savings)
  - Multi-perspective bias mitigation
  - Cryptographic commit integrity (hash-based IDs, multi-signature support)
  - Markdown rendering with intelligent caching

### Phase 2: Team Collaboration + Disaster Resilience - IN PROGRESS (Q1-Q3 2026)
**Status:** Decentralized Infrastructure Complete | **Target:** Small teams (2-20 members + AIs)

**Resilient Infrastructure - COMPLETE ✅**
- ✅ **DHT-based peer discovery** (v0.9.5) - Kademlia DHT, 73 tests passing, internet-wide validated
- ✅ **6-Tier Connection Fallback** (v0.10.0) - IPv6, IPv4, WebRTC, UDP hole punch, relay, gossip
- ✅ **Hub-Optional Architecture** - Fully decentralized operation without Hub dependency
- ✅ **DTLS Encryption** (v0.10.1) - All 6 connection strategies now encrypted end-to-end
- ✅ **UDP Hole Punching** (v0.10.1) - DTLS 1.2 encrypted, production-ready, 60-70% NAT success
- ✅ **Volunteer Relay Nodes** (v0.10.0) - 100% NAT coverage, privacy-preserving
- ✅ **Gossip Protocol** (v0.10.0) - Store-and-forward for disaster scenarios, anti-entropy sync

**Team Collaboration Features - PLANNED (Q1-Q3 2026):**
- Persistent team management with roles
- Group chat UI with presence indicators
- Team knowledge repositories (shared, synchronized knowledge)
- Team AI assistants (access to collective team knowledge)
- Collaborative context editing (PR-like approval workflow)
- Team compute pools (auto-discovery and load balancing)
- Knowledge commit templates (Meeting Notes, Decisions, Postmortems)
- Team activity feed and analytics dashboard
- AI-facilitated team sessions (standups, brainstorming)

**Stretch Goals:**
- WiFi Mesh (802.11s) support (documentation - Direct TLS works over mesh)
- Meshtastic (LoRa) integration (offline text messaging)
- Hub-assisted backup (encrypted storage on Hub)

**See [ROADMAP.md](./ROADMAP.md) for detailed Phase 2 specifications.**

### Phase 3: Scaling & Mass Decentralization - PLANNED (2026-2027)
**Status:** Design Phase | **Target:** Global scale, 100% decentralization

**Scaling Features:**
- Mobile clients (iOS, Android)
- Multi-hub federation (large organizations with 100+ teams)
- Advanced DHT features (libp2p integration, cross-DHT discovery)
- Hub-free operation mode (100% decentralized)
- Dedicated TURN server infrastructure

**Advanced Security:**
- Social recovery (Shamir Secret Sharing for backup passphrases)
- Hardware wallet integration (Ledger, YubiKey, TPM)
- Blockchain-based identity (optional)
- QR code backup transfer (for mobile)

**Rationale:** Phase 2 proves small team collaboration model. Phase 3 scales to millions.

---

## Contributing

We welcome contributions of all kinds!

### How to Contribute

1. **Read the Vision** - Start with our [Vision Doc](./VISION.md) and [Product Vision](./PRODUCT_VISION.md)
2. **Review Guidelines** - See [CONTRIBUTING.md](./CONTRIBUTING.md) for branching workflow and code style
3. **Sign the CLA** - Required for code contributions ([CLA.md](./CLA.md))
4. **Find an Issue** - Check [GitHub Issues](https://github.com/mikhashev/dpc-messenger/issues)
5. **Submit a PR** - Follow the `dev` → `main` workflow (see [CONTRIBUTING.md](./CONTRIBUTING.md))

### Areas We Need Help

- Bug fixes and testing
- Documentation improvements
- Internationalization (i18n)
- UI/UX enhancements
- Security audits
- Protocol implementation

### Community

- **Discussions:** [GitHub Discussions](https://github.com/mikhashev/dpc-messenger/discussions)
- **Issues:** [GitHub Issues](https://github.com/mikhashev/dpc-messenger/issues)
- **Email:** legoogmiha@gmail.com

---

## Licensing

D-PC uses a **Progressive Copyleft** strategy:

| Component | License | Can I... |
|-----------|---------|----------|
| **Desktop Client** | GPL v3 | Use freely, must share modifications |
| **Protocol Libraries** | LGPL v3 | Use in proprietary apps, share modifications to libs |
| **Federation Hub** | AGPL v3 | Run as service, must share if modified |
| **Protocol Specs** | CC0 | Use freely, no restrictions |

**TL;DR for most users:**
- Use the app freely (no strings attached)
- Use protocol libraries in your app (LGPL is friendly)
- Run your own Hub (source required if modified)
- Can't create proprietary messenger fork (or buy Commercial License)

**See [LICENSE.md](./LICENSE.md) for detailed information.**

---

## Security

### Reporting Vulnerabilities

**Please DO NOT open public issues for security vulnerabilities.**

Email: legoogmiha@gmail.com

We take security seriously and will respond within 48 hours.

---

## Legal Notice

**License:** Multi-license (GPL/LGPL/AGPL/CC0) - see [LICENSE.md](./LICENSE.md)

**Disclaimer:** This software is provided 'AS IS' without warranty of any kind. Users are responsible for compliance with applicable laws in their jurisdiction, including encryption regulations, data protection laws, and telecommunications requirements.

**Security:** For vulnerability reports, contact legoogmiha@gmail.com (do not open public issues).

**Compliance:** See [docs/LEGAL_COMPLIANCE.md](./docs/LEGAL_COMPLIANCE.md) for detailed information on export controls, data protection requirements, and jurisdiction-specific considerations.

**No Warranty:** The creators accept no liability for use, misuse, or legal consequences. Consult legal counsel before deployment in regulated environments.

---

## Acknowledgments

D-PC Messenger builds on the shoulders of giants:

- **[aiortc](https://github.com/aiortc/aiortc)** - WebRTC implementation
- **[Tauri](https://tauri.app/)** - Desktop app framework
- **[FastAPI](https://fastapi.tiangolo.com/)** - Hub server framework
- **[Ollama](https://ollama.ai/)** - Local AI inference

Special thanks to all contributors and early testers!

---

## Project Status

| Metric | Status |
|--------|--------|
| **Architecture** | Stable |
| **Core Protocol** | v1.0 |
| **WebRTC** | Working |
| **Direct TLS** | Working |
| **Hub Server** | Production Ready |
| **Crypto Identity** | v0.5.0 |
| **Knowledge Architecture** | v2.0 (Phase 4.2 Complete) |
| **Desktop Client** | Beta (v0.9.4) |
| **Mobile Clients** | Planned (Phase 2) |
| **Test Coverage** | In Progress |
| **Documentation** | Good |

---

## Support Development

D-PC Messenger is developed with the assistance of AI-powered tools to accelerate innovation and maintain code quality. If you find this project valuable and would like to support its continued development and promotion, consider making a donation:

**Cryptocurrency Donations:**
- **Bitcoin:** `bc1qfev88vx2yem48hfj04udjgn3938afg5yvdr92x`
- **Ethereum:** `0xB019Ae32a98fd206881f691fFe021A2B2520Ce9d`
- **TON:** `UQDWa0-nCyNM1jghk1PBRcjBt4Lxvs86wflNGHHQtxfyx-8J`

Your support helps cover AI-assisted development costs and enables faster delivery of new features. Every contribution, no matter the size, is deeply appreciated and directly contributes to building privacy-first infrastructure for human-AI collaboration.

---

## Support & Contact

- **GitHub Issues:** [Report bugs](https://github.com/mikhashev/dpc-messenger/issues)
- **GitHub Discussions:** [Ask questions](https://github.com/mikhashev/dpc-messenger/discussions)
- **Email:** legoogmiha@gmail.com
- **Documentation:** [docs/](./docs/)

---

<div align="center">

**A small step for AI, a giant leap for all humanity.**

[Star on GitHub](https://github.com/mikhashev/dpc-messenger) | [Documentation](./docs/) | [Discussions](https://github.com/mikhashev/dpc-messenger/discussions)

</div>