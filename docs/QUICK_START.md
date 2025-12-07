# D-PC Messenger Quick Start Guide

> **Get up and running in 5 minutes**

This guide will help you set up D-PC Messenger for the first time. Choose your setup method based on your needs:

- **Option A:** Local network testing (no Hub needed)
- **Option B:** Internet-wide connections (requires Hub)

---

## Prerequisites

Before you begin, install these tools:

### Required Software

| Tool | Version | Installation |
|------|---------|--------------|
| **Python** | 3.12+ | [python.org](https://www.python.org/) |
| **Poetry** | Latest | `curl -sSL https://install.python-poetry.org \| python3 -` |
| **Node.js** | 18+ | [nodejs.org](https://nodejs.org/) |
| **Rust** | Latest | [rustup.rs](https://rustup.rs/) |
| **Docker** | Latest | [docker.com](https://www.docker.com/) (for Hub only) |

### Verify Installation

```bash
python --version   # Should show 3.12+
poetry --version   # Should show poetry version
node --version     # Should show v18+
rustc --version    # Should show rust version
docker --version   # Should show docker version (if using Hub)
```

---

## Option A: Local Network Testing (Fastest)

**Use this if:** You want to test between computers on the same network.

**NEW in v0.10.0:** Direct connections now support 6-tier fallback hierarchy (IPv6 → IPv4 → WebRTC → UDP hole punching → Volunteer relays → Gossip) for near-universal connectivity!

### Step 1: Clone the Repository

```bash
git clone https://github.com/mikhashev/dpc-messenger.git
cd dpc-messenger
```

### Step 2: Start Client 1

```bash
# Terminal 1: Backend
cd dpc-client/core
poetry install
poetry run python run_service.py

# Terminal 2: Frontend (in new terminal)
cd dpc-client/ui
npm install
npm run tauri dev
```

**What happens:**
- First run creates `~/.dpc/` directory with:
  - `node.key` - Your private key
  - `node.crt` - Your certificate
  - `node.id` - Your node ID
  - `providers.json` - AI provider config
  - `privacy_rules.json` - Firewall rules
  - `personal.json` - Your context
  - `device_context.json` - Auto-generated device info
  - `instructions.json` - AI instruction customizations
  - `known_peers.json` - Known peer connections
  - `knowledge/` - Knowledge commits directory

**Note your connection URI** displayed in the terminal:
```
Your Direct TLS URI: dpc://192.168.1.100:8888/dpc-node-abc123...
```

### Step 3: Start Client 2

Repeat Step 2 on another computer on the same network.

### Step 4: Connect

1. In Client 2's UI, click "Connect to Peer"
2. Paste Client 1's `dpc://` URI
3. Click "Connect"
4. Connection established!

**Success!** You can now:
- Send messages
- Share context
- Query AI with peer's knowledge

---

## Option B: Internet-Wide Connections (Recommended)

**Use this if:** You want to connect to anyone, anywhere.

**NEW in v0.10.0:** Hub is now **optional**! The system can establish connections without Hub using DHT-based hole punching and volunteer relay nodes.

### Step 1: Clone the Repository

```bash
git clone https://github.com/mikhashev/dpc-messenger.git
cd dpc-messenger
```

### Step 2: Set Up the Hub

**Hub Requirements:**
- Public VPS or local machine with port forwarding
- Docker installed
- Domain name (optional but recommended)

```bash
cd dpc-hub

# 1. Start PostgreSQL
docker-compose up -d

# 2. Configure environment
cp .env.example .env
nano .env  # Edit the following:
```

**Minimum `.env` Configuration:**
```bash
# REQUIRED: Generate with: openssl rand -hex 32
SECRET_KEY="your_generated_secret_key_here"

# REQUIRED: Google OAuth credentials
GOOGLE_CLIENT_ID="your_client_id.apps.googleusercontent.com"
GOOGLE_CLIENT_SECRET="your_secret"

# Database (default values work for local testing)
DATABASE_URL="postgresql+asyncpg://user:password@localhost:5432/dpc_hub"
```

**TIP:** The client also has its own configuration at `~/.dpc/config.ini`.
You can override any setting with environment variables. See [CONFIGURATION.md](./CONFIGURATION.md) for details.

**Example:**
```bash
export DPC_HUB_URL=https://your-hub.com
```

**Get Google OAuth Credentials:**
1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create new project
3. Enable "Google+ API"
4. Create OAuth 2.0 Client ID (Web application)
5. Add redirect URI: `http://localhost:8000/auth/google/callback`
6. Copy Client ID and Secret to `.env`

```bash
# 3. Install dependencies and run migrations
poetry install
poetry run alembic upgrade head

# 4. Start the Hub
poetry run uvicorn dpc_hub.main:app --host 0.0.0.0 --reload
```

**Verify Hub is running:**
```bash
curl http://localhost:8000/health
# Should return: {"status":"healthy",...}
```

### Step 3: Start the Client

```bash
# Terminal 1: Backend
cd dpc-client/core
poetry install
poetry run python run_service.py

# Terminal 2: Frontend (in new terminal)
cd dpc-client/ui
npm install
npm run tauri dev
```

### Step 4: Authenticate with Hub

1. Click "Login with Google" in the UI
2. Complete OAuth flow in browser
3. Client automatically registers your node identity
4. You'll see: "✅ Node identity registered and verified!"

**NEW in v0.5.0:** Node registration is now automatic!

### Step 5: Connect to a Peer

1. Get your peer's `node_id` (they can share it via any channel)
   - Example: `dpc-node-8b066c7f3d7eb627`
2. In the UI, click "Connect via Hub"
3. Enter peer's `node_id`
4. Click "Connect"
5. WebRTC automatically establishes P2P connection!

**Connection Process:**
```
You → Hub → Peer (signaling)
  ↓
Direct P2P WebRTC channel established
  ↓
Hub no longer involved in communication
```

---

## Configure AI Providers

Edit `~/.dpc/providers.json` to add your AI providers:

### Ollama (Local AI - Recommended for Privacy)

```bash
# 1. Install Ollama
curl -fsSL https://ollama.com/install.sh | sh

# 2. Download a model
ollama pull llama3.2

# 3. Configure in providers.json
nano ~/.dpc/providers.json
```

```json
{
  "default_provider": "ollama_local",
  "providers": {
    "ollama_local": {
      "type": "ollama",
      "base_url": "http://localhost:11434",
      "model": "llama3.2:latest"
    }
  }
}
```

### OpenAI

```json
{
  "providers": {
    "openai": {
      "type": "openai",
      "api_key": "sk-your-api-key",
      "model": "gpt-4"
    }
  }
}
```

### Anthropic Claude

```json
{
  "providers": {
    "anthropic": {
      "type": "anthropic",
      "api_key": "sk-ant-your-api-key",
      "model": "claude-3-5-sonnet-20241022"
    }
  }
}
```

---

## Test Your Setup

### 1. Check Node Identity

```bash
cat ~/.dpc/node.id
# Should show: dpc-node-[16 hex chars]
```

### 2. Test Local AI

In the chat UI:
```
@ai What is the capital of France?
```

### 3. Test P2P Connection

1. Connect to a peer
2. Send a message
3. Both should see the message

### 4. Test Context Sharing

```
@ai [use_context:@peer_node_id] What projects are they working on?
```

---

## Common Issues

### "Module not found" errors

```bash
# Backend
cd dpc-client/core
poetry install

# Frontend
cd dpc-client/ui
npm install
```

### "Could not connect to Hub"

**This is OK if you're using Option A (local network).**

For Option B:
```bash
# Check Hub is running
curl http://localhost:8000/health

# Check Hub logs
cd dpc-hub
poetry run uvicorn dpc_hub.main:app --log-level debug
```

### "WebRTC connection timeout"

Common causes:
1. Hub not accessible
2. Firewall blocking UDP
3. Both peers behind symmetric NAT (rare)

Solutions:
```bash
# Allow UDP (Linux)
sudo ufw allow 8888/udp

# Test Hub accessibility
curl https://your-hub-url.com/health

# Check WebRTC logs
# Look for ICE connection state in client logs
```

### "OAuth redirect URI mismatch"

Fix in Google Cloud Console:
1. Go to Credentials
2. Edit OAuth 2.0 Client ID
3. Add exact redirect URI from error message
4. Save

### "Node identity registration failed"

```bash
# Regenerate identity
rm ~/.dpc/node.*
poetry run python run_service.py
# Will create new identity files
```

---

## Next Steps

### For Users

1. **Configure Context Firewall** - Edit `~/.dpc/privacy_rules.json`
2. **Add AI Providers** - Edit `~/.dpc/providers.json`
3. **Customize Profile** - Edit `~/.dpc/personal.json`
4. **Read the Whitepaper** - Understand the vision

### For Developers

1. **Read Technical Docs:**
   - [WebRTC Integration](./README_WEBRTC_INTEGRATION.md)
   - [Client README](../dpc-client/README.md)
   - [Hub README](../dpc-hub/README.md)

2. **Run Tests:**
   ```bash
   cd dpc-client/core
   poetry run pytest
   ```

3. **Contribute:**
   - [GitHub Issues](https://github.com/mikhashev/dpc-messenger/issues)
   - [Contributing Guide](../CONTRIBUTING.md)

### For Hub Operators

1. **Production Deployment:**
   - See [WebRTC Setup Guide](./WEBRTC_SETUP_GUIDE.md)
   - Set up Nginx + SSL
   - Configure firewall
   - Enable monitoring

2. **Security Hardening:**
   - Use strong `SECRET_KEY`
   - Enable rate limiting
   - Set up backup
   - Regular updates

---

## Frequently Asked Questions

### Do I need to run my own Hub?

**No.** You can:
- Use a public Hub (when available)
- Use local network only (Option A)
- Run your own Hub for privacy (Option B)

### Is my data private?

**Yes.** The Hub only stores:
- Your email (for OAuth)
- Your node_id (public key hash)
- Your public profile (name, expertise)

The Hub NEVER sees:
- Your messages
- Your personal context
- Your AI queries

### Can I use this for production?

**Current status: Beta**
- Core protocol: Stable
- WebRTC: Working
- Hub: Production ready
- Desktop client: Beta

Recommended for:
- Testing
- Research
- Early adoption

Not yet recommended for:
- Mission-critical applications
- Large-scale deployment
- Mobile devices (coming soon)

### How do I get help?

- **Documentation:** [docs/](../)
- **Issues:** [GitHub Issues](https://github.com/mikhashev/dpc-messenger/issues)
- **Discussions:** [GitHub Discussions](https://github.com/mikhashev/dpc-messenger/discussions)
- **Email:** legoogmiha@gmail.com

---

## Success Indicators

You know everything is working when:

- ✅ Client shows your `node_id`
- ✅ Local AI responds to `@ai` queries
- ✅ You can connect to peers
- ✅ Messages send/receive successfully
- ✅ Context sharing works
- ✅ Hub shows "healthy" status

---

## Summary

**You've learned:**
- How to set up D-PC Messenger
- Two connection methods (local and Hub)
- How to configure AI providers
- How to troubleshoot common issues

**What's next:**
- Invite friends to test with you
- Explore advanced features
- Join the community
- Contribute to development

---

<div align="center">

**[⬅️ Back to Main README](../README.md)** | **[📖 WebRTC Setup Guide](./WEBRTC_SETUP_GUIDE.md)** | **[🔧 Technical Overview](./README_WEBRTC_INTEGRATION.md)**

*Part of the D-PC Messenger project*

**Questions? Open an issue or discussion on GitHub!**

</div>