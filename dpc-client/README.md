# D-PC Messenger Client

> **Cross-platform desktop application with WebRTC P2P connectivity**

The D-PC Messenger Client is a privacy-first desktop application that enables secure, AI-powered communication with automatic NAT traversal via WebRTC.

---

## 🎯 Features

### Core Functionality
- ✅ **WebRTC P2P Connections** - Connect to peers anywhere with automatic NAT traversal
- ✅ **Direct TLS Connections** - Local network connections without Hub
- ✅ **Local AI Integration** - Ollama, OpenAI, Anthropic support
- ✅ **Context Firewall** - Granular control over data sharing
- ✅ **Hub Integration** - OAuth authentication and peer discovery
- ✅ **End-to-End Encryption** - All communications use DTLS

### Platform Support
- ✅ **Windows** - Full support (x64)
- ✅ **macOS** - Full support (Intel & Apple Silicon)
- ✅ **Linux** - Full support (Ubuntu, Fedora, Arch)

---

## 📦 Architecture

```
┌─────────────────────────────────────────────────────────┐
│              D-PC Client Architecture                    │
└─────────────────────────────────────────────────────────┘

┌──────────────────────┐
│   Tauri Desktop App  │  ← User Interface (SvelteKit)
│   (UI Frontend)      │
└──────────┬───────────┘
           │ WebSocket (ws://localhost:9999)
           ▼
┌──────────────────────────────────────────────────┐
│   Core Service (Python Backend)                  │
│                                                   │
│  ┌──────────────┐  ┌──────────────┐             │
│  │ P2PManager   │  │ HubClient    │             │
│  │ - WebRTC     │  │ - OAuth      │             │
│  │ - Direct TLS │  │ - Signaling  │             │
│  └──────────────┘  └──────────────┘             │
│                                                   │
│  ┌──────────────┐  ┌──────────────┐             │
│  │ LLMManager   │  │ Firewall     │             │
│  │ - Ollama     │  │ - .dpc_access│             │
│  │ - OpenAI     │  │ - Rules      │             │
│  │ - Anthropic  │  │              │             │
│  └──────────────┘  └──────────────┘             │
└──────────────────────────────────────────────────┘
           │
           ▼
┌──────────────────────┐
│  ~/.dpc/             │  ← User Configuration
│  - node.key/.crt     │    (Local Storage)
│  - providers.toml    │
│  - .dpc_access       │
│  - personal.json     │
└──────────────────────┘
```

---

## 🚀 Quick Start

### Prerequisites

1. **Python 3.12+** with Poetry 1.2+
2. **Node.js 18+** with npm
3. **Rust** - Install via [rustup.rs](https://rustup.rs/)
4. **(Optional) Ollama** - For local AI: [ollama.ai](https://ollama.ai/)

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
- `~/.dpc/node.key` - Private key (keep secret!)
- `~/.dpc/node.crt` - Public certificate
- `~/.dpc/node.id` - Your node ID
- `~/.dpc/providers.toml` - AI provider settings
- `~/.dpc/.dpc_access` - Context firewall rules
- `~/.dpc/personal.json` - Your personal context

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

## 🔧 Configuration

### AI Providers (`~/.dpc/providers.toml`)

```toml
# Default provider (used if not specified)
default_provider = "ollama_local"

# Ollama (local AI)
[providers.ollama_local]
type = "ollama"
base_url = "http://localhost:11434"
model = "llama3.2:latest"

# OpenAI
[providers.openai]
type = "openai"
api_key = "sk-..."  # Your API key
model = "gpt-4"

# Anthropic Claude
[providers.anthropic]
type = "anthropic"
api_key = "sk-ant-..."  # Your API key
model = "claude-3-sonnet-20240229"
```

### Context Firewall (`~/.dpc/.dpc_access`)

Control what information peers can access:

```toml
# Hub access (public profile)
[hub]
public.json:name = "allow"
public.json:description = "allow"
public.json:expertise = "allow"

# Peer access (private connections)
[peer.dpc-node-friend123]
personal.json:projects = "allow"
personal.json:skills = "allow"

[peer.dpc-node-colleague456]
work.json:current_project = "allow"
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

## 🎮 Usage

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
# They enter it in the "Connect to Peer" dialog
```

#### Method 2: WebRTC via Hub (Internet)

```bash
# 1. Ensure Hub is running and accessible
# 2. Both users authenticate with Hub
# 3. Enter peer's node_id in the UI
# 4. Click "Connect via Hub"
# 5. WebRTC automatically establishes P2P connection
```

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

## 🛠️ Development

### Project Structure

```
dpc-client/
├── core/                      # Python backend
│   ├── dpc_client_core/
│   │   ├── service.py         # Main orchestrator
│   │   ├── p2p_manager.py     # WebRTC & Direct TLS
│   │   ├── webrtc_peer.py     # WebRTC peer connection
│   │   ├── hub_client.py      # Hub communication
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

## 🐛 Troubleshooting

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
curl https://your-hub-url.com/docs

# 2. Check firewall (allow UDP)
# Linux: sudo ufw allow 8888/udp
# Windows: Check Windows Firewall settings

# 3. For symmetric NAT, TURN server needed (future feature)
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

## 📊 Performance

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

## 🔒 Security

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
| Impersonation | ✅ Cryptographic node IDs |
| Hub Compromise | ✅ Hub never sees message content |
| Local Malware | ⚠️ Operating system security responsibility |

---

## 🚢 Deployment

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

## 📚 API Reference

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

## 🤝 Contributing

We welcome contributions! See the main [CONTRIBUTING.md](../CONTRIBUTING.md) for guidelines.

### Areas for Contribution

- 🐛 Bug fixes
- ✨ Feature implementations
- 📝 Documentation improvements
- 🧪 Test coverage
- 🎨 UI/UX enhancements
- 🌍 Internationalization

---

## 📄 License

This component is licensed under **GPL v3**. See [LICENSE](../LICENSE.md) for details.

**TL;DR:** You can use, modify, and distribute this software. If you distribute it, you must share your modifications under the same license.

---

## 📞 Support

- **Issues:** [GitHub Issues](https://github.com/mikhashev/dpc-messenger/issues)
- **Discussions:** [GitHub Discussions](https://github.com/mikhashev/dpc-messenger/discussions)
- **Email:** legoogmiha@gmail.com
- **Documentation:** [Main README](../README.md)

---

<div align="center">

**[⬅️ Back to Main README](../README.md)** | **[📖 WebRTC Setup Guide](../docs/WEBRTC_SETUP_GUIDE.md)** | **[🚀 Quick Start](../docs/QUICK_START.md)**

*Part of the D-PC Messenger project*

</div>