# D-PC Messenger: Privacy-First Platform for Human-AI Collaboration

> **Status:** MVP Ready | **License:** Multi-License (GPL/LGPL/AGPL/CC0) | **Version:** 0.8.0
> **Platforms:** Windows | Linux | macOS
> **Note:** This software is for educational/research use. Please review the full [Legal Notice](#%EF%B8%8F-legal-notice--compliance) before use.

---

## 🚀 The Vision: A Private Internet for Human-AI Collaboration

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

## 🤝 Join the Mission: Seeking a Business Co-Founder

The core technology is built, but the mission to build a world-changing company has just begun.

I am a product-focused technical founder looking for a business-focused co-founder to be my partner. If you have a passion for privacy, a deep understanding of go-to-market strategy, and the drive to build a global community, I want to talk to you.

If this vision resonates with you, let's connect:
📧 [legoogmiha@gmail.com](mailto:legoogmiha@gmail.com) | 💼 [LinkedIn](https://www.linkedin.com/in/mikemikhashev) | 🐦 [X/Twitter](https://x.com/mikeshev4enko)

---

## 🧠 What is D-PC Messenger?

**D-PC Messenger** (Decentralized Personal Context) is a **privacy-first, peer-to-peer platform for human-AI collaboration**.

**The Core Idea:** Imagine you're working with your personal AI assistant (ChatGPT, Claude, or local Ollama). Now imagine securely sharing relevant context with a trusted friend so *their* AI assistant can help too - without compromising privacy or relying on centralized servers.

**How it works:**
- 🤝 **You + Your AI** - Chat with your personal AI assistant about anything
- 💬 **Collaborate with Humans** - Connect directly with trusted peers via encrypted P2P messaging
- 📚 **Share Context Securely** - Share conversation histories, documents, or knowledge with peers
- 🧠 **Their AI Gets Smarter** - Your shared context helps their AI give better, more informed answers
- 🔒 **Complete Privacy** - All data stays on your devices, encrypted end-to-end, with granular access control

**Real-world example:** You're researching a technical topic with your AI assistant. You share your conversation history with a colleague. Their AI can now see your research and provide complementary insights, creating a **collective intelligence** between humans and their AI assistants.

**Philosophy:** [Digital Self-Sovereignty](./docs/USER_SOVEREIGNTY.md) - Your data, your keys, your control. No backdoors, no data mining, no compromises.

---

## 🌟 Key Features

### For End Users
- 🔒 **True Privacy** - Messages never touch servers, only peers
- 👤 **User Sovereignty** - You own your data, identity, and encryption keys ([read more](./docs/USER_SOVEREIGNTY.md))
- 💾 **Encrypted Backups** - AES-256-GCM encrypted backups with no backdoors ([guide](./docs/BACKUP_RESTORE.md))
- 🤝 **Human-AI Collaboration** - Work with your AI assistant, then share contexts with trusted peers so their AIs can contribute too
- 💬 **Conversation History** - Full conversational continuity with smart context optimization (60-80% token savings)
- 📝 **Rich Markdown Rendering** - AI responses display with GitHub-style formatting, intelligent caching (50-200x faster)
- 📚 **Knowledge Commits** - Git-like versioning for AI-extracted knowledge with bias mitigation ([architecture](./docs/KNOWLEDGE_ARCHITECTURE.md))
- 🏠 **Local-First** - Your data stays on your device
- 🌐 **Internet-Wide** - Connect to anyone, anywhere via WebRTC
- 📶 **Offline Mode** - Works seamlessly when Hub is unavailable with cached tokens and Direct TLS
- 🔐 **Cryptographic Identity** - Self-sovereign node IDs based on public keys
- 🛡️ **Context Firewall** - Granular control over what data you share

### For Developers
- 📖 **Open Protocol** - Extensible DPTP (D-PC Transfer Protocol)
- 🧩 **Modular Design** - Clear separation of concerns
- 🧠 **Knowledge Architecture** - Git-like knowledge commits with cognitive bias mitigation ([architecture doc](./docs/KNOWLEDGE_ARCHITECTURE.md))
- 🔧 **Easy Integration** - Use any AI provider (Ollama, OpenAI, Claude)
- 🚀 **Production Ready** - Docker deployment, OAuth, rate limiting

---

## 🏗️ Architecture

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

### ⚠️ Important: True Peer-to-Peer Architecture

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

## 📁 Project Structure

```
dpc-messenger/
│
├── dpc-hub/              # Federation Hub (AGPL v3)
│   ├── dpc_hub/
│   │   ├── main.py       # FastAPI app & routes
│   │   ├── auth.py       # JWT + OAuth authentication
│   │   ├── crypto_validation.py  # Node identity validation
│   │   ├── models.py     # Database models
│   │   ├── crud.py       # Database operations
│   │   └── alembic/      # Database migrations
│   └── README.md         # Hub setup guide
│
├── dpc-client/           # Desktop Client Application
│   ├── core/             # Python backend (WebRTC, P2P, AI)
│   │   ├── dpc_client_core/
│   │   │   ├── service.py        # Main orchestrator
│   │   │   ├── p2p_manager.py    # WebRTC & TLS connections
│   │   │   ├── webrtc_peer.py    # WebRTC peer connection
│   │   │   ├── hub_client.py     # Hub communication
│   │   │   ├── llm_manager.py    # AI provider integration
│   │   │   ├── backup_manager.py # Encrypted backup/restore
│   │   │   └── cli_backup.py     # Backup CLI commands
│   │   └── README.md
│   │
│   └── ui/               # Frontend (Tauri + SvelteKit)
│       ├── src/          # Svelte components
│       └── README.md
│
├── dpc-protocol/         # Shared protocol library (LGPL)
│   ├── dpc_protocol/
│   │   ├── crypto.py     # Identity & encryption
│   │   ├── protocol.py   # Message serialization
│   │   └── pcm_core.py   # Personal Context Model
│   └── README.md
│
├── specs/                # Protocol specifications (CC0)
│   ├── hub_api_v1.md
│   └── dptp_v1.md
│
├── docs/                 # Additional documentation
│   ├── QUICK_START.md           # 5-minute setup
│   ├── KNOWLEDGE_ARCHITECTURE.md # Knowledge management architecture
│   ├── WEBRTC_SETUP_GUIDE.md    # Production deployment
│   ├── USER_SOVEREIGNTY.md      # Privacy philosophy & vision
│   ├── BACKUP_RESTORE.md        # Encrypted backup guide
│   ├── GITHUB_AUTH_SETUP.md     # GitHub OAuth setup
│   └── README_WEBRTC_INTEGRATION.md
│
├── PRODUCT_VISION.md     # Product vision & technical philosophy
├── VISION.md             # Business vision & market opportunity
├── LICENSE.md            # Multi-license explanation
└── README.md             # This file
```

---

## 🚀 Quick Start

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

📖 **See [docs/QUICK_START.md](./docs/QUICK_START.md) for detailed instructions.**
📖 **Backup guide: [docs/BACKUP_RESTORE.md](./docs/BACKUP_RESTORE.md)**
📖 **GitHub OAuth: [docs/GITHUB_AUTH_SETUP.md](./docs/GITHUB_AUTH_SETUP.md)**

---

## 🌐 Production Deployment

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

📖 **Full guide: [docs/WEBRTC_SETUP_GUIDE.md](./docs/WEBRTC_SETUP_GUIDE.md)**

---

## 🔑 Authentication & Security

### Authentication Flow (v0.6.0)

1. **OAuth Login** - User authenticates via **Google or GitHub** ([setup guide](./docs/GITHUB_AUTH_SETUP.md))
2. **Temporary Node ID** - Hub assigns temporary ID
3. **Cryptographic Registration** - Client registers public key & certificate
4. **Verified Identity** - Hub validates and marks node_id as verified
5. **JWT Token** - Client receives JWT for API access
6. **Logout Support** - Tokens can be blacklisted upon logout

### Security Features

- 🔒 End-to-end encryption (DTLS in WebRTC, TLS in Direct)
- 💾 **Client-side encrypted backups** - AES-256-GCM with PBKDF2 (600k iterations)
- 🔑 Cryptographic node identities (derived from RSA public keys)
- 🛡️ Context firewall (.dpc_access.json) for granular permissions
- 🔐 JWT authentication with Hub (token blacklisting supported)
- ✅ No message persistence by default
- 🔍 Node identity validation (certificates, public keys)
- 🚫 **No backdoors** - If you lose your passphrase, data is permanently unrecoverable (by design)

---

## 📚 Documentation

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

## 🛣️ Roadmap

### Phase 1: Federated MVP ✅ (Completed - v0.8.0)
- ✅ Direct TLS P2P connections
- ✅ WebRTC with NAT traversal
- ✅ Federation Hub for discovery
- ✅ OAuth authentication (Google + GitHub)
- ✅ Cryptographic node identity system
- ✅ Token blacklist and logout
- ✅ **Encrypted local backups** (AES-256-GCM with user-controlled passphrases)
- ✅ Local AI integration
- ✅ Offline mode with graceful degradation
- ✅ **Remote inference** - Share compute power with trusted peers ([guide](./docs/REMOTE_INFERENCE.md))
- ✅ **Knowledge Commit System** - Git-like versioned knowledge with bias mitigation ([architecture](./docs/KNOWLEDGE_ARCHITECTURE.md))
  - ✅ Personal Context Model (PCM) v2.0
  - ✅ Knowledge topics with entries, tags, and confidence scoring
  - ✅ Automatic conversation monitoring and knowledge extraction
  - ✅ Multi-perspective bias mitigation (Western, Eastern, Indigenous viewpoints)
  - ✅ Knowledge commit proposals with approval workflow
  - ✅ Git-style versioning with commit history
  - ✅ User-controlled auto-detection toggle

### Phase 2: Enhanced Federation (Next - Q1-Q2 2026) ⏳
**Current Starting Point for Development**

- 🔲 Multi-hub federation
- 🔲 Advanced context firewall with tag-based sharing
- 🔲 **Peer-to-peer knowledge sharing** - Share knowledge commits between peers
- 🔲 **Collaborative knowledge building** - Multi-peer consensus on shared knowledge
- 🔲 **Remote inference enhancements** - Model discovery, streaming responses, usage tracking
- 🔲 **Hub-assisted backup** (encrypted backup storage on Hub)
- 🔲 **QR code backup transfer** (for mobile devices)
- 🔲 Mobile clients (Android, iOS)
- 🔲 Dedicated TURN server deployment

### Phase 3: True P2P (Future - 2026-2027)
- 🔲 DHT-based peer discovery
- 🔲 Hub-free operation mode
- 🔲 **Social recovery** (Shamir Secret Sharing for backup passphrases)
- 🔲 **Hardware wallet integration** (Ledger, YubiKey, TPM)
- 🔲 Blockchain-based identity (optional)
- 🔲 Full decentralization

---

## 🤝 Contributing

We welcome contributions of all kinds!

### How to Contribute

1. **Read the Vision** - Start with our [Vision Doc](./VISION.md) and [Product Vision](./PRODUCT_VISION.md)
2. **Review Guidelines** - See [CONTRIBUTING.md](./CONTRIBUTING.md) for branching workflow and code style
3. **Sign the CLA** - Required for code contributions ([CLA.md](./CLA.md))
4. **Find an Issue** - Check [GitHub Issues](https://github.com/mikhashev/dpc-messenger/issues)
5. **Submit a PR** - Follow the `dev` → `main` workflow (see [CONTRIBUTING.md](./CONTRIBUTING.md))

### Areas We Need Help

- 🐛 Bug fixes and testing
- 📝 Documentation improvements
- 🌍 Internationalization (i18n)
- 🎨 UI/UX enhancements
- 🔐 Security audits
- 🧪 Protocol implementation

### Community

- **Discussions:** [GitHub Discussions](https://github.com/mikhashev/dpc-messenger/discussions)
- **Issues:** [GitHub Issues](https://github.com/mikhashev/dpc-messenger/issues)
- **Email:** legoogmiha@gmail.com

---

## 📜 Licensing

D-PC uses a **Progressive Copyleft** strategy:

| Component | License | Can I... |
|-----------|---------|----------|
| **Desktop Client** | GPL v3 | Use freely, must share modifications |
| **Protocol Libraries** | LGPL v3 | Use in proprietary apps, share modifications to libs |
| **Federation Hub** | AGPL v3 | Run as service, must share if modified |
| **Protocol Specs** | CC0 | Use freely, no restrictions |

**TL;DR for most users:**
- ✅ Use the app freely (no strings attached)
- ✅ Use protocol libraries in your app (LGPL is friendly)
- ✅ Run your own Hub (source required if modified)
- ❌ Can't create proprietary messenger fork (or buy Commercial License)

📖 **See [LICENSE.md](./LICENSE.md) for detailed information.**

---

## 🔒 Security

### Reporting Vulnerabilities

**Please DO NOT open public issues for security vulnerabilities.**

Email: legoogmiha@gmail.com

We take security seriously and will respond within 48 hours.

---

## ⚖️ Legal Notice

**License:** Multi-license (GPL/LGPL/AGPL/CC0) - see [LICENSE.md](./LICENSE.md)

**Disclaimer:** This software is provided 'AS IS' without warranty of any kind. Users are responsible for compliance with applicable laws in their jurisdiction, including encryption regulations, data protection laws, and telecommunications requirements.

**Security:** For vulnerability reports, contact legoogmiha@gmail.com (do not open public issues).

**Compliance:** See [docs/LEGAL_COMPLIANCE.md](./docs/LEGAL_COMPLIANCE.md) for detailed information on export controls, data protection requirements, and jurisdiction-specific considerations.

**No Warranty:** The creators accept no liability for use, misuse, or legal consequences. Consult legal counsel before deployment in regulated environments.

---

## 🙏 Acknowledgments

D-PC Messenger builds on the shoulders of giants:

- **[aiortc](https://github.com/aiortc/aiortc)** - WebRTC implementation
- **[Tauri](https://tauri.app/)** - Desktop app framework
- **[FastAPI](https://fastapi.tiangolo.com/)** - Hub server framework
- **[Ollama](https://ollama.ai/)** - Local AI inference

Special thanks to all contributors and early testers!

---

## 📊 Project Status

| Metric | Status |
|--------|--------|
| **Architecture** | ✅ Stable |
| **Core Protocol** | ✅ v1.0 |
| **WebRTC** | ✅ Working |
| **Direct TLS** | ✅ Working |
| **Hub Server** | ✅ Production Ready |
| **Crypto Identity** | ✅ v0.5.0 |
| **Knowledge Architecture** | ✅ v2.0 (Phase 4.2 Complete) |
| **Desktop Client** | ✅ Beta (v0.8.0) |
| **Mobile Clients** | 🔲 Planned (Phase 2) |
| **Test Coverage** | 🚧 In Progress |
| **Documentation** | ✅ Good |

---

## 💝 Support Development

D-PC Messenger is developed with the assistance of AI-powered tools to accelerate innovation and maintain code quality. If you find this project valuable and would like to support its continued development and promotion, consider making a donation:

**Cryptocurrency Donations:**
- **Bitcoin:** `bc1qfev88vx2yem48hfj04udjgn3938afg5yvdr92x`
- **Ethereum:** `0xB019Ae32a98fd206881f691fFe021A2B2520Ce9d`
- **TON:** `UQDWa0-nCyNM1jghk1PBRcjBt4Lxvs86wflNGHHQtxfyx-8J`

Your support helps cover AI-assisted development costs and enables faster delivery of new features. Every contribution, no matter the size, is deeply appreciated and directly contributes to building privacy-first infrastructure for human-AI collaboration.

---

## 📞 Support & Contact

- **GitHub Issues:** [Report bugs](https://github.com/mikhashev/dpc-messenger/issues)
- **GitHub Discussions:** [Ask questions](https://github.com/mikhashev/dpc-messenger/discussions)
- **Email:** legoogmiha@gmail.com
- **Documentation:** [docs/](./docs/)

---

<div align="center">

**Made with ❤️ by the D-PC Community**

[⭐ Star on GitHub](https://github.com/mikhashev/dpc-messenger) | [📖 Documentation](./docs/) | [💬 Discussions](https://github.com/mikhashev/dpc-messenger/discussions)

</div>