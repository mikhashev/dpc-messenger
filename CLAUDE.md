# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**D-PC Messenger** is a privacy-first, peer-to-peer messaging platform enabling collaborative AI intelligence through secure sharing of personal contexts. The project implements a novel "transactional communication" paradigm with end-to-end encryption and no server-stored messages.

**Architecture:** Multi-package monorepo with Python backend services and Tauri + SvelteKit desktop frontend.

---

## Repository Structure

```
dpc-messenger/
├── dpc-protocol/         # Shared protocol library (LGPL v3)
├── dpc-client/
│   ├── core/             # Python backend service
│   └── ui/               # Tauri + SvelteKit frontend
├── dpc-hub/              # Federation Hub server (AGPL v3)
├── specs/                # Protocol specifications
└── docs/                 # Documentation
```

---

## Common Commands

### Client Development

**Backend (Python):**
```bash
cd dpc-client/core
poetry install                    # Install dependencies
poetry run python run_service.py  # Run backend service (ports 8888, 9999)
poetry run pytest                 # Run tests
poetry run pytest --cov=dpc_client_core  # Run with coverage
```

**Frontend (Tauri + SvelteKit):**
```bash
cd dpc-client/ui
npm install                       # Install dependencies
npm run dev                       # Vite dev server only (port 1420)
npm run tauri dev                 # Full Tauri development mode
npm run build                     # Build frontend
npm run tauri build               # Build production desktop app
npm run check                     # TypeScript type checking
```

**Build Outputs:**
- Windows: `dpc-client/ui/src-tauri/target/release/dpc-messenger.exe`
- Installers: `dpc-client/ui/src-tauri/target/release/bundle/`

### Hub Development

```bash
cd dpc-hub
docker-compose up -d              # Start PostgreSQL
cp .env.example .env              # Configure environment
poetry install                    # Install dependencies
poetry run alembic upgrade head   # Run database migrations
poetry run uvicorn dpc_hub.main:app --reload  # Start dev server

# Database migrations
poetry run alembic revision --autogenerate -m "description"  # Create migration
poetry run alembic upgrade head   # Apply migrations
poetry run alembic downgrade -1   # Rollback last migration

# Testing and linting
poetry run pytest                 # Run tests
poetry run pytest --cov=dpc_hub   # Run with coverage
poetry run black dpc_hub/         # Format code
poetry run flake8 dpc_hub/        # Lint
poetry run mypy dpc_hub/          # Type checking
```

### Protocol Library

```bash
cd dpc-protocol
poetry install                    # Install dependencies
poetry run pytest                 # Run tests
```

---

## Architecture Overview

### Connection Types

1. **Direct TLS** (Local Network)
   - Server listens on port 8888
   - Uses self-signed X.509 certificates for node identity
   - Location: `dpc-client/core/dpc_client_core/p2p_manager.py`
   - Lowest latency, requires network visibility

2. **WebRTC** (Internet-Wide)
   - NAT traversal via STUN/TURN
   - Hub provides signaling only (no message routing)
   - Location: `dpc-client/core/dpc_client_core/webrtc_peer.py`
   - Uses aiortc library

### Key Components

**Client Backend (`dpc-client/core`):**
- `service.py` - Main orchestrator (CoreService)
- `p2p_manager.py` - Unified P2P connection manager (TLS + WebRTC)
- `webrtc_peer.py` - WebRTC peer wrapper (aiortc)
- `hub_client.py` - Federation Hub communication (OAuth, WebSocket signaling)
- `llm_manager.py` - AI provider integration (Ollama, OpenAI, Anthropic)
- `firewall.py` - Context access control system
- `local_api.py` - WebSocket API for UI (localhost:9999)
- `settings.py` - Configuration management

**Client Frontend (`dpc-client/ui`):**
- Built with SvelteKit 5.0 + Tauri 2.x
- Entry point: `src/routes/+page.svelte`
- Backend communication: `src/lib/coreService.ts` (WebSocket client)
- SSG mode with adapter-static (SPA fallback)

**Hub Server (`dpc-hub`):**
- `main.py` - FastAPI application and routes
- `auth.py` - OAuth 2.0 + JWT authentication
- `crypto_validation.py` - Node identity validation
- `models.py` - SQLAlchemy database models
- `crud.py` - Database operations
- `connection_manager.py` - WebSocket signaling manager
- Database: PostgreSQL with Alembic migrations

**Protocol Library (`dpc-protocol`):**
- `crypto.py` - Node identity, RSA keys, X.509 certificates
- `protocol.py` - Message serialization (10-byte header + JSON)
- `pcm_core.py` - Personal Context Model data structures

### Message Protocol (DPTP)

Messages use binary framing: 10-byte ASCII length header + JSON payload

**Example Message Types:**
```python
{"command": "HELLO", "payload": {"node_id": "...", "display_name": "..."}}
{"command": "GET_CONTEXT", "payload": {"tags": [...]}}
{"command": "SEND_TEXT", "payload": {"text": "..."}}
{"command": "AI_QUERY", "payload": {"query": "...", "use_context": [...]}}
```

### Node Identity System

- Node IDs: `dpc-node-[16 hex chars]` (SHA256 hash of RSA public key)
- Cryptographic files stored in `~/.dpc/`:
  - `node.key` - RSA private key (2048-bit)
  - `node.crt` - X.509 self-signed certificate
  - `node.id` - Node identifier
- Hub validates node identity via certificate verification

### Configuration System

**Configuration Hierarchy (highest priority first):**
1. Environment variables (e.g., `DPC_HUB_URL`, `DPC_OAUTH_CALLBACK_PORT`)
2. Config file (`~/.dpc/config.ini`)
3. Built-in defaults

**Configuration Files:**
- Windows: `C:\Users\<username>\.dpc\config.ini`
- Linux/Mac: `~/.dpc/config.ini`
- Providers: `~/.dpc/providers.toml` (AI provider configs)
- Firewall: `~/.dpc/.dpc_access` (context sharing rules)

---

## Development Workflow

### Starting Full Stack Locally

**Terminal 1 - Hub (optional for Direct TLS only):**
```bash
cd dpc-hub
docker-compose up -d
poetry run alembic upgrade head
poetry run uvicorn dpc_hub.main:app --reload
```

**Terminal 2 - Client Backend:**
```bash
cd dpc-client/core
poetry run python run_service.py
```

**Terminal 3 - Client Frontend:**
```bash
cd dpc-client/ui
npm run tauri dev
```

### Testing

**Client Core:**
```bash
cd dpc-client/core
poetry run pytest -v
poetry run pytest tests/test_local_api.py  # Run specific test
```

**Hub:**
```bash
cd dpc-hub
poetry run pytest -v --cov=dpc_hub
```

**Coverage Reports:**
- HTML: `htmlcov/index.html` (generated after running with --cov)

---

## Key Architectural Patterns

### Async/Await Throughout

All I/O operations use asyncio. Key patterns:
- Background tasks tracked in task sets
- Graceful shutdown with signal handlers (Unix) or KeyboardInterrupt (Windows)
- Connection pooling for database (asyncpg)

### Service-Oriented Backend

CoreService orchestrates independent components:
- P2PManager for connections
- HubClient for federation
- LLMManager for AI
- ContextFirewall for access control
- LocalApiServer for UI communication

### Event-Driven UI Communication

UI connects via WebSocket (localhost:9999) for:
- Sending commands to backend
- Receiving real-time events (new messages, connection status)

### Security-First Design

- Private keys never leave client device
- Hub never sees message contents or personal contexts
- TLS 1.2+ for all connections
- Certificate validation required
- JWT tokens with short expiration and blacklist support

---

## Special Considerations

### Cross-Platform Compatibility

- **Windows:** Signal handlers not supported, uses KeyboardInterrupt
- **Unix/Linux:** Proper SIGINT/SIGTERM handling
- Use `pathlib.Path` for file paths

### WebRTC NAT Traversal

- STUN servers for public IP discovery (Google STUN)
- TURN server fallback for symmetric NAT (OpenRelay)
- ICE candidate gathering and connectivity checks
- Connection state monitoring in `webrtc_peer.py`

### Database Migrations (Hub)

- Alembic for schema versioning
- Always test migrations before deployment:
  ```bash
  poetry run alembic upgrade head    # Apply
  poetry run alembic downgrade -1    # Test rollback
  poetry run alembic upgrade head    # Reapply
  ```

### Context Firewall Rules

Access control file format (`~/.dpc/.dpc_access`):
```ini
[groups]
friends = alice,bob
trusted = charlie

[rules]
allow_all = false
allow_for = group:friends
tags_to_share = public,work
tags_to_hide = private,personal
```

---

## Debugging Tips

### Backend Issues

**Check logs:**
```bash
# Backend prints to stdout
cd dpc-client/core
poetry run python run_service.py
```

**Common issues:**
- Port 8888 or 9999 already in use
- Certificate files missing in `~/.dpc/`
- AI provider not configured in `~/.dpc/providers.toml`

### Frontend Issues

**Check Tauri dev tools:**
- Right-click in app → "Inspect Element"
- Console shows WebSocket connection status

**Common issues:**
- Backend not running (WebSocket connection fails)
- SvelteKit HMR port conflicts (port 1420)

### Hub Issues

**Check database connection:**
```bash
docker-compose ps  # Ensure PostgreSQL is running
poetry run alembic current  # Check migration status
```

**Common issues:**
- PostgreSQL not started
- Missing environment variables in `.env`
- OAuth credentials not configured

### WebRTC Connection Issues

**Test STUN/TURN connectivity:**
```bash
cd dpc-client/core
poetry run pytest tests/test_turn_connectivity.py
```

**Check signaling:**
- Hub WebSocket endpoint: `ws://localhost:8000/ws/signal` (dev)
- Both clients must be logged in and connected to Hub

---

## File Locations Reference

### Client Backend Entry Point
- `dpc-client/core/run_service.py` - Starts CoreService

### Frontend Entry Point
- `dpc-client/ui/src/routes/+page.svelte` - Main chat interface
- `dpc-client/ui/src-tauri/src/main.rs` - Tauri entry point

### Hub Entry Point
- `dpc-hub/dpc_hub/main.py` - FastAPI application

### Configuration Files
- `~/.dpc/config.ini` - Client configuration
- `~/.dpc/providers.toml` - AI provider settings
- `~/.dpc/.dpc_access` - Context firewall rules
- `~/.dpc/node.{key,crt,id}` - Node identity files

### Important Documentation
- `README.md` - Project overview
- `docs/QUICK_START.md` - 5-minute setup guide
- `docs/WEBRTC_SETUP_GUIDE.md` - Production deployment
- `docs/CONFIGURATION.md` - Complete configuration reference
- `whitepaper.md` - Project vision and philosophy

---

## Technology Stack

### Backend
- Python 3.12+ with Poetry
- FastAPI (Hub), WebSockets (Client)
- aiortc (WebRTC implementation)
- SQLAlchemy + asyncpg + Alembic (Hub database)
- cryptography library (PKI)
- ollama, openai, anthropic (AI SDKs)

### Frontend
- SvelteKit 5.0 (Svelte 5 with runes)
- Tauri 2.x (Rust)
- TypeScript 5.6
- Vite 6.0
- adapter-static (SPA mode)

### Infrastructure
- Docker + Docker Compose
- PostgreSQL
- Nginx (production)

---

## License Structure

- Desktop Client: GPL v3
- Protocol Libraries: LGPL v3
- Federation Hub: AGPL v3
- Protocol Specs: CC0

See `LICENSE.md` for details.
