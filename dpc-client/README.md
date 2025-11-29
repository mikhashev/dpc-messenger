# D-PC Client

> **Desktop application for private P2P messaging with collaborative AI**
>
> **Status:** Beta | **License:** GPL v3 | **Version:** 0.8.0

The D-PC Client is a desktop application built with Tauri (Rust) and SvelteKit that enables secure peer-to-peer communication with collaborative AI capabilities. It supports both Direct TLS connections (local network) and WebRTC connections (internet-wide).

---

## Features

- **End-to-End Encrypted P2P** - Direct connections between peers
- **WebRTC** - Connect across NAT/firewalls using STUN/TURN
- **IPv6 Support** - Dual-stack connectivity (IPv4 + IPv6)
- **Local AI** - Ollama, GPT, Claude integration
- **Context Sharing** - Collaborate using shared personal contexts
- **Knowledge Commits** - Git-like versioned knowledge with bias mitigation
- **Context Firewall** - Granular permission control
- **Cryptographic Identity** - Automatic node_id registration
- **Encrypted Backups** - AES-256-GCM with user-controlled passphrases
- **Lightweight** - ~150MB memory footprint

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                D-PC Client Architecture                 │
└─────────────────────────────────────────────────────────┘

┌────────────────────────┐
│    Tauri Frontend      │
│   (SvelteKit + TS)     │
│                        │
│  • Chat UI             │
│  • Connection Manager  │
│  • Settings            │
└───────────┬────────────┘
            │ WebSocket
            │ (localhost:9999)
            ▼
┌────────────────────────┐
│   Python Backend       │
│  (Core Service)        │
│                        │
│  • P2P Manager         │
│  • WebRTC Handler      │
│  • Hub Client          │
│  • AI Manager          │
│  • Context Firewall    │
└───┬────────────┬───────┘
    │            │
    │ P2P        │ HTTPS/WSS
    │            │
    ▼            ▼
┌─────────┐  ┌──────────┐
│  Peers  │  │   Hub    │
│         │  │          │
└─────────┘  └──────────┘
```

---

## Getting Started

### Prerequisites

- **Python 3.12+** with Poetry
- **Node.js 18+** with npm
- **Rust** (install via [rustup.rs](https://rustup.rs/))
- **(Optional) Ollama** - For local AI: [ollama.ai](https://ollama.ai/)

### Installation

#### 1. Backend Setup (Core Service)

```bash
cd dpc-client/core

# Install Python dependencies
poetry install

# First run will create config files in ~/.dpc/
poetry run python run_service.py
```

**Configuration Files Created:**
- `~/.dpc/node.key` - RSA private key (keep secret!)
- `~/.dpc/node.crt` - X.509 self-signed certificate
- `~/.dpc/node.id` - Your cryptographic node ID (e.g., dpc-node-e07fb59e46f34940)
- `~/.dpc/config.ini` - Client configuration (ports, STUN/TURN servers, OAuth settings)
- `~/.dpc/providers.json` - AI provider settings (Ollama, OpenAI, Anthropic)
- `~/.dpc/privacy_rules.json` - Context firewall rules (what peers can access)
- `~/.dpc/personal.json` - Your personal context (PCM v2.0 with knowledge commits)
- `~/.dpc/device_context.json` - Auto-collected hardware/software info (schema v1.1)
- `~/.dpc/instructions.json` - AI behavior instructions and bias mitigation settings
- `~/.dpc/known_peers.json` - Cached peer information

#### 2. Frontend Setup (UI)

```bash
cd dpc-client/ui

# Install Node.js dependencies
npm install

# Run in development mode
npm run tauri dev

# Or build for production
npm run tauri build
```

---

## Configuration

### AI Providers (`~/.dpc/providers.json`)

```json
{
  "default_provider": "ollama_local",
  "providers": {
    "ollama_local": {
      "type": "ollama",
      "base_url": "http://localhost:11434",
      "model": "llama3.2:latest"
    },
    "openai_gpt4": {
      "type": "openai",
      "api_key": "sk-...",
      "model": "gpt-4",
      "max_tokens": 4096
    },
    "claude_sonnet": {
      "type": "anthropic",
      "api_key": "sk-ant-...",
      "model": "claude-3-5-sonnet-20241022",
      "max_tokens": 8192
    }
  }
}
```

### Context Firewall (`~/.dpc/privacy_rules.json`)

Control what information peers can access:

```json
{
  "hub": {
    "personal.json:profile.name": "allow",
    "personal.json:profile.description": "allow"
  },
  "node_groups": {
    "friends": ["dpc-node-alice-123", "dpc-node-bob-456"],
    "coworkers": ["dpc-node-charlie-789"]
  },
  "groups": {
    "friends": {
      "personal.json:profile.*": "allow",
      "personal.json:knowledge.*": "allow"
    }
  },
  "nodes": {
    "dpc-node-alice-123": {
      "personal.json:*": "allow",
      "device_context.json:*": "allow"
    }
  },
  "compute": {
    "enabled": false,
    "allow_nodes": [],
    "allow_groups": [],
    "allowed_models": []
  }
}
```

### Hub Connection (`core/dpc_client_core/service.py`)

```python
# For local Hub
self.hub_client = HubClient(api_base_url="http://127.0.0.1:8000")

# For production Hub
self.hub_client = HubClient(api_base_url="https://hub.yourdomain.com")

# For ngrok testing
self.hub_client = HubClient(api_base_url="https://abc123.ngrok.io")
```

---

## Usage

### Running the Application

**Option 1: Development Mode**

```bash
# Terminal 1: Start backend
cd dpc-client/core
poetry run python run_service.py

# Terminal 2: Start frontend
cd dpc-client/ui
npm run tauri dev
```

**Option 2: Production Build**

```bash
cd dpc-client/ui
npm run tauri build

# Built application will be in:
# Windows: ui/src-tauri/target/release/dpc-messenger.exe
# macOS: ui/src-tauri/target/release/bundle/macos/
# Linux: ui/src-tauri/target/release/bundle/appimage/
```

### Connecting to Peers

#### Method 1: Direct Connection (Local Network)

```bash
# Get your connection URI from the app
# Example: dpc://192.168.1.100:8888/dpc-node-abc123

# Share this URI with your peer
# Click "Connect"
```

#### Method 2: WebRTC via Hub (Internet)

**NEW in v0.5.0: Automatic Node Registration**

```bash
# 1. Ensure Hub is running and accessible
# 2. Click "Login with Google/GitHub" in the UI
# 3. Client automatically:
#    - Completes OAuth flow
#    - Registers cryptographic node identity
#    - Verifies identity with Hub
# 4. Enter peer's node_id
# 5. Click "Connect"
# 6. WebRTC automatically establishes P2P connection
```

**Authentication Flow:**
1. OAuth authentication with Hub
2. Automatic registration of node_id, public key, and certificate
3. Hub validates and verifies identity
4. JWT token issued for subsequent API calls
5. Ready to connect to peers

### Chatting with AI

```bash
# In the chat window, type:
@ai What are the best practices for React hooks?

# To query with peer context:
@ai [use_context:@peer_node_id] How should we approach this problem?

# To use remote compute:
@ai [compute_host:@peer_node_id] [model:llama3-70b] Analyze this data
```

---

## Development

### Project Structure

```
dpc-client/
├── core/                      # Python backend
│   ├── dpc_client_core/
│   │   ├── service.py         # Main orchestrator
│   │   ├── p2p_manager.py     # WebRTC & Direct TLS
│   │   ├── webrtc_peer.py     # WebRTC peer connection
│   │   ├── hub_client.py      # Hub communication (with auto-registration)
│   │   ├── llm_manager.py     # AI provider management
│   │   ├── firewall.py        # Context access control
│   │   ├── local_api.py       # WebSocket API for UI
│   │   └── context_cache.py   # Context caching
│   │
│   ├── tests/                 # Unit tests
│   ├── run_service.py         # Entry point
│   ├── pyproject.toml         # Python dependencies
│   └── README.md
│
└── ui/                        # Tauri + SvelteKit frontend
    ├── src/
    │   ├── routes/
    │   │   └── +page.svelte   # Main UI
    │   └── lib/
    │       ├── coreService.ts # Backend communication
    │       └── stores.ts      # Svelte stores
    │
    ├── src-tauri/             # Tauri configuration
    │   ├── src/
    │   │   └── main.rs        # Rust entry point
    │   └── tauri.conf.json    # App configuration
    │
    ├── package.json           # Node.js dependencies
    └── README.md
```

### Running Tests

```bash
cd dpc-client/core

# Run all tests
poetry run pytest

# Run with coverage
poetry run pytest --cov=dpc_client_core

# Run specific test
poetry run pytest tests/test_p2p_manager.py
```

### Code Style

```bash
# Python (using ruff)
cd core
poetry run ruff check .
poetry run ruff format .

# JavaScript/TypeScript (using eslint)
cd ui
npm run lint
npm run format
```

---

## API Reference

### WebSocket API (UI ↔ Backend)

The backend exposes a WebSocket server at `ws://127.0.0.1:9999`.

**Command Format:**
```json
{
  "id": "unique-id",
  "command": "command_name",
  "payload": { /* command-specific data */ }
}
```

**Available Commands:**

| Command | Payload | Description |
|---------|---------|-------------|
| `get_status` | `{}` | Get node ID, Hub status, connected peers |
| `login_to_hub` | `{"provider": "google"}` | Login via OAuth (auto-registers node) |
| `connect_to_peer` | `{"uri": "dpc://..."}` | Connect via Direct TLS |
| `connect_to_peer_by_id` | `{"node_id": "..."}` | Connect via WebRTC |
| `disconnect_from_peer` | `{"node_id": "..."}` | Close connection |
| `send_p2p_message` | `{"target_node_id": "...", "text": "..."}` | Send message |
| `execute_ai_query` | `{"query": "...", "use_context_from": [...]}` | AI query |

**Response Format:**
```json
{
  "id": "unique-id",
  "command": "command_name",
  "status": "OK",
  "payload": { /* response data */ }
}
```

**Event Format (broadcasts):**
```json
{
  "event": "event_name",
  "payload": { /* event data */ }
}
```

---

## Troubleshooting

### Backend Issues

**Problem: `ModuleNotFoundError: No module named 'aiortc'`**

Solution: Install dependencies:
```bash
cd core
poetry install
```

**Problem: "Could not connect to Hub"**

This is expected if Hub is not running. The client will work in "Direct TLS only" mode.

```bash
# To enable WebRTC:
# 1. Start Hub (see dpc-hub/README.md)
# 2. Restart client
```

**Problem: "Node identity registration failed"**

Solution: Check that:
```bash
# 1. Identity files exist
ls -la ~/.dpc/node.{key,crt,id}

# 2. If missing, regenerate:
rm ~/.dpc/node.*
poetry run python run_service.py  # Will regenerate

# 3. Check Hub logs for validation errors
```

### Frontend Issues

**Problem: White screen / JavaScript errors**

Solution: Check app console (F12) for errors.

```bash
# Clear cache and rebuild
cd ui
rm -rf node_modules package-lock.json
npm install
npm run tauri dev
```

**Problem: "WebSocket connection failed"**

Solution: Ensure backend is running first.

```bash
# Start backend before frontend
cd core
poetry run python run_service.py

# Then start frontend in another terminal
cd ui
npm run tauri dev
```

### WebRTC Issues

**Problem: "WebRTC connection timeout"**

Causes:
1. Hub is not accessible
2. Firewall blocking UDP
3. Both peers behind symmetric NAT (rare)

Solutions:
```bash
# 1. Check Hub accessibility
curl https://your-hub-url.com/health

# 2. Check firewall (allow UDP)
# Linux: sudo ufw allow 8888/udp
# Windows: Check Windows Firewall settings

# 3. For symmetric NAT, TURN server needed
# Check WebRTC logs for ICE connection state
```

**Problem: "ICE connection failed"**

```bash
# Check STUN/TURN server configuration
# In webrtc_peer.py, verify:
# - STUN servers are reachable
# - TURN server credentials are correct

# Test STUN connectivity:
curl -v stun:stun.l.google.com:19302
```

### Port Conflicts

**Problem: "Address already in use: 9999"**

Solution: Change the port in `core/dpc_client_core/local_api.py`:

```python
class LocalApiServer:
    def __init__(self, core_service, host="127.0.0.1", port=9998):  # Changed
        # ...
```

---

## Security

### Best Practices

1. **Keep node.key private** - Never share this file
2. **Review .dpc_access** - Control what peers can access
3. **Use strong Hub passwords** - If deploying your own Hub
4. **Update regularly** - Security patches are important
5. **Verify peer identities** - Check node_ids before connecting

### Threat Model

| Threat | Mitigation |
|--------|------------|
| MITM Attack | ✅ TLS/DTLS encryption |
| Data Exfiltration | ✅ Context firewall |
| Impersonation | ✅ Cryptographic node IDs (verified by Hub) |
| Hub Compromise | ✅ Hub never sees message content |
| Local Malware | ⚠️ Operating system security responsibility |

### Node Identity Security

**v0.5.0 Improvements:**
- Node identities verified by Hub using cryptographic validation
- Public keys and certificates validated before registration
- Node_id derivation from public key ensures authenticity
- Prevents node_id spoofing and impersonation

---

## Performance

### Resource Usage (Typical)

| Component | CPU | Memory | Notes |
|-----------|-----|--------|-------|
| Core Service | ~5% | ~150MB | Idle state |
| Core Service | ~15-25% | ~200-300MB | During AI query (local) |
| Core Service | ~5-10% | ~180MB | During WebRTC connection |
| UI (Tauri) | ~2-5% | ~120MB | Idle |
| UI (Tauri) | ~5-10% | ~150MB | Active chat |

### Optimizations

- WebRTC data channels are more efficient than WebSockets
- Context caching reduces redundant AI queries
- Ephemeral messages don't accumulate in memory
- Direct TLS is faster than WebRTC for local network

---

## Deployment

### Desktop Distribution

```bash
cd dpc-client/ui

# Build for your platform
npm run tauri build

# Artifacts location:
# Windows: target/release/bundle/msi/*.msi
# macOS: target/release/bundle/dmg/*.dmg
# Linux: target/release/bundle/appimage/*.AppImage
```

### Enterprise Deployment

For organizations deploying to multiple machines:

```bash
# 1. Pre-configure providers.toml with company AI endpoints
# 2. Distribute via software management (SCCM, Jamf, etc.)
# 3. Set up internal Hub for corporate network
# 4. Configure firewall rules for VPN access
```

---

## Contributing

We welcome contributions! See the main [CONTRIBUTING.md](../CONTRIBUTING.md) for guidelines.

### Areas for Contribution

- Bug fixes
- Feature implementations
- Documentation improvements
- Test coverage
- UI/UX enhancements
- Internationalization

---

## Documentation

### Protocol & Architecture
- **[DPTP Specification](../specs/dptp_v1.md)** - Formal protocol documentation
- **[Protocol Library Docs](../dpc-protocol/README.md)** - API reference for dpc-protocol
- **[Knowledge Architecture](../docs/KNOWLEDGE_ARCHITECTURE.md)** - Knowledge commit system

### Guides
- **[Quick Start](../docs/QUICK_START.md)** - 5-minute setup
- **[WebRTC Setup](../docs/WEBRTC_SETUP_GUIDE.md)** - Production deployment
- **[Configuration Guide](../docs/CONFIGURATION.md)** - Complete config reference
- **[Backup & Restore](../docs/BACKUP_RESTORE.md)** - Encrypted backups

---

## License

This component is licensed under **GPL v3**. See [LICENSE](../LICENSE.md) for details.

**TL;DR:** You can use, modify, and distribute this software. If you distribute it, you must share your modifications under the same license.

---

## Support

- **Issues:** [GitHub Issues](https://github.com/mikhashev/dpc-messenger/issues)
- **Discussions:** [GitHub Discussions](https://github.com/mikhashev/dpc-messenger/discussions)
- **Email:** legoogmiha@gmail.com
- **Documentation:** [Main README](../README.md)

---

<div align="center">

**[Back to Main README](../README.md)** | **[WebRTC Setup Guide](../docs/WEBRTC_SETUP_GUIDE.md)** | **[Quick Start](../docs/QUICK_START.md)**

*Part of the D-PC Messenger project*

</div>