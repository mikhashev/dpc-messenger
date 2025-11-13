# D-PC Messenger: Decentralized Peer-to-Peer Collaborative Intelligence

> **Status:** MVP Ready | **License:** Multi-License (GPL/LGPL/AGPL/CC0) | **Version:** 0.6.0

**D-PC Messenger** (Decentralized Personal Context) is a privacy-first, peer-to-peer messaging platform that enables **collaborative intelligence** through secure sharing of personal AI contexts between trusted peers, without relying on centralized servers for communication.

**Philosophy:** [Digital Self-Sovereignty](./docs/USER_SOVEREIGNTY.md) - Your data, your keys, your control. No backdoors, no data mining, no compromises.

---

## 🌟 Key Features

### For End Users
- 🔒 **True Privacy** - Messages never touch servers, only peers
- 👤 **User Sovereignty** - You own your data, identity, and encryption keys ([read more](./docs/USER_SOVEREIGNTY.md))
- 💾 **Encrypted Backups** - AES-256-GCM encrypted backups with no backdoors ([guide](./docs/BACKUP_RESTORE.md))
- 🤝 **Collaborative AI** - Share context with trusted peers for better answers
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
┌─────────────────────────────────────────────────────────┐
│                   System Architecture                   │
└─────────────────────────────────────────────────────────┘

┌──────────────┐                            ┌──────────────┐
│   Client A   │ ◄──── P2P Encrypted ──────►│   Client B   │
│              │       Connection           │              │
│  • Local AI  │                            │  • Local AI  │
│  • Context   │                            │  • Context   │
│  • Firewall  │                            │  • Firewall  │
└──────┬───────┘                            └──────┬───────┘
       │                                           │
       │            ┌─────────────────┐            │
       └────────────►  Federation Hub  ◄───────────┘
                    │                 │
                    │  • Discovery    │
                    │  • Signaling    │
                    │  • OAuth        │
                    │  • NO Messages  │
                    └─────────────────┘
```

### Two Connection Methods

1. **Direct TLS** (Local Network)
   - Fastest, lowest latency
   - Requires network visibility
   - Uses cryptographic node certificates

2. **WebRTC** (Internet-Wide)
   - Works across NAT/firewalls
   - Automatic NAT traversal via STUN/TURN
   - Hub only for initial signaling

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
├── whitepaper.md         # Project vision & philosophy
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
- 🛡️ Context firewall (.dpc_access) for granular permissions
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

### Architecture & Design
- **[KNOWLEDGE_ARCHITECTURE.md](./docs/KNOWLEDGE_ARCHITECTURE.md)** - Knowledge management architecture with cognitive bias mitigation
- **[whitepaper.md](./whitepaper.md)** - Project vision & philosophy
- **[specs/hub_api_v1.md](./specs/hub_api_v1.md)** - Hub API specification

### Legal
- **[LICENSE.md](./LICENSE.md)** - Licensing explained

---

## 🛣️ Roadmap

### Phase 1: Federated MVP ✅ (Current - v0.6.0)
- ✅ Direct TLS P2P connections
- ✅ WebRTC with NAT traversal
- ✅ Federation Hub for discovery
- ✅ OAuth authentication (Google + GitHub)
- ✅ Cryptographic node identity system
- ✅ Token blacklist and logout
- ✅ **Encrypted local backups** (AES-256-GCM with user-controlled passphrases)
- ✅ Local AI integration
- ✅ Offline mode with graceful degradation
- ⏳ Remote inference (in progress)

### Phase 2: Enhanced Federation (Q1-Q2 2026)
- 🔲 Multi-hub federation
- 🔲 Advanced context firewall
- 🔲 **Knowledge Commit System** - Git-like versioned knowledge with bias mitigation ([architecture](./docs/KNOWLEDGE_ARCHITECTURE.md))
- 🔲 Remote inference MVP
- 🔲 **Hub-assisted backup** (encrypted backup storage on Hub)
- 🔲 **QR code backup transfer** (for mobile devices)
- 🔲 Mobile clients (Android, iOS)
- 🔲 Dedicated TURN server deployment

### Phase 3: True P2P (2026-2027)
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

1. **Read the Vision** - Start with our [Whitepaper](./whitepaper.md)
2. **Sign the CLA** - Required for code contributions ([CLA.md](./CLA.md))
3. **Find an Issue** - Check [GitHub Issues](https://github.com/mikhashev/dpc-messenger/issues)
4. **Submit a PR** - Follow our contribution guidelines

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
| **Desktop Client** | 🚧 Beta |
| **Mobile Clients** | 🔲 Planned |
| **Test Coverage** | 🚧 In Progress |
| **Documentation** | ✅ Good |

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