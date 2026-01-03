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

### OAuth Configuration (Hub)

The Hub supports multiple OAuth providers for authentication:

**Supported Providers:**
- **Google OAuth** (required) - Primary authentication provider
- **GitHub OAuth** (optional) - Developer-friendly alternative

**Setup:**
1. Copy `.env.example` to `.env` in `dpc-hub/`
2. Add OAuth credentials:
   ```bash
   # Google OAuth (required)
   GOOGLE_CLIENT_ID="your_google_client_id"
   GOOGLE_CLIENT_SECRET="your_google_client_secret"

   # GitHub OAuth (optional)
   GITHUB_CLIENT_ID="your_github_client_id"
   GITHUB_CLIENT_SECRET="your_github_client_secret"
   ```

**Creating OAuth Apps:**
- **Google**: [Google Cloud Console](https://console.cloud.google.com/apis/credentials)
  - Callback URL: `http://localhost:8000/auth/google/callback` (dev)
- **GitHub**: [GitHub Developer Settings](https://github.com/settings/developers)
  - Callback URL: `http://localhost:8000/auth/github/callback` (dev)

**Client Usage (Backend):**
```python
# Login with Google (default)
await hub_client.login(provider="google")

# Login with GitHub
await hub_client.login(provider="github")
```

**Client UI:**
The UI displays two separate login buttons in the sidebar:
- **Google** button (blue) - Primary authentication
- **GitHub** button (black) - Alternative authentication

Users can choose their preferred provider before connecting to the Hub.

**Provider Switching:**
Users can authenticate with either provider using the same email. The Hub automatically updates the account's provider field to reflect the most recently used authentication method. No data is lost when switching providers.

**Configuration (Optional):**
Set OAuth preferences in `~/.dpc/config.ini`:
```ini
[hub]
auto_connect = true  # Auto-connect to Hub on startup (default: true)

[oauth]
default_provider = github  # Provider for auto-connect: 'google' or 'github' (default: google)
```

**Auto-Connect Behavior:**
- When `auto_connect = true`: Client automatically connects to Hub on startup using `default_provider`
- When `auto_connect = false`: Client starts in offline mode; users must click a login button manually
- Default: Auto-connects with Google on startup

**Example Configurations:**
```ini
# Auto-connect with GitHub
[hub]
auto_connect = true
[oauth]
default_provider = github

# Start offline, require manual login
[hub]
auto_connect = false
```

See [docs/GITHUB_AUTH_SETUP.md](docs/GITHUB_AUTH_SETUP.md) for detailed GitHub setup instructions.

---

## Architecture Overview

### Connection Types (6-Tier Fallback Hierarchy - v0.10.1)

D-PC Messenger uses an intelligent 6-tier connection fallback hierarchy for near-universal P2P connectivity. The ConnectionOrchestrator tries each strategy in priority order until one succeeds, making the Hub completely optional.

**Priority 1: IPv6 Direct** (40%+ networks, no NAT)
   - Direct connection over IPv6 (no NAT required)
   - Lowest latency, best performance
   - Server listens on port 8888 (default)
   - **IPv6 URIs**: Use bracket notation: `dpc://[2001:db8::1]:8888?node_id=...`
   - Timeout: 60s (increased in v0.10.1 for mobile/CGNAT support)
   - Location: `connection_strategies/ipv6_direct.py`

**Priority 2: IPv4 Direct** (Local network / port forward)
   - Direct TLS connection over IPv4
   - Uses self-signed X.509 certificates for node identity
   - **Dual-stack support**: IPv4 and IPv6
   - **Configuration**: Set `listen_host` in config.ini:
     - `dual` (default) - Listens on both IPv4 and IPv6
     - `0.0.0.0` - IPv4 only
     - `::` - IPv6 only
   - Timeout: 60s (increased in v0.10.1 for mobile/CGNAT support)
   - Location: `connection_strategies/ipv4_direct.py`, `p2p_manager.py`

**Priority 3: Hub WebRTC** (When Hub available)
   - NAT traversal via STUN/TURN (existing infrastructure)
   - Hub provides signaling only (no message routing)
   - Uses aiortc library
   - Timeout: 30s (configurable via connection.webrtc_timeout)
   - Location: `connection_strategies/hub_webrtc.py`, `webrtc_peer.py`

**Priority 4: UDP Hole Punching** (60-70% NAT, Hub-independent)
   - DHT-coordinated simultaneous UDP send (birthday paradox)
   - **DTLS 1.2 encryption** (v0.10.1+) - End-to-end encrypted UDP transport
   - STUN-like endpoint discovery via DHT peers
   - NAT type detection (cone vs symmetric)
   - Success rate: 60-70% for cone NAT (fails gracefully for symmetric NAT)
   - Timeout: 15s
   - **Status**: Production-ready with DTLS encryption (enabled by default in v0.10.1)
   - Location: `connection_strategies/udp_hole_punch.py`, `managers/hole_punch_manager.py`
   - Testing: See `docs/MANUAL_TESTING_GUIDE.md`

**Priority 5: Volunteer Relay** (100% NAT coverage, Hub-independent)
   - Relay discovery via DHT quality scoring (uptime 50%, capacity 30%, latency 20%)
   - End-to-end encryption maintained (relay sees only encrypted payloads)
   - Privacy-preserving: relay cannot decrypt message content
   - Optional server mode: volunteer as relay (opt-in via `relay.volunteer = true`)
   - Timeout: 20s
   - Location: `connection_strategies/volunteer_relay.py`, `managers/relay_manager.py`, `transports/relayed_connection.py`

**Priority 6: Gossip Store-and-Forward** (Disaster fallback, eventual delivery)
   - Multi-hop epidemic routing (fanout=3 random peers)
   - **End-to-end hybrid encryption (AES-GCM + RSA-OAEP)** (v0.10.2)
     - No payload size limit (replaces ~190 byte RSA limit)
     - AES-256-GCM for data encryption (authenticated, detects tampering)
     - RSA-OAEP for encrypting AES keys
     - Forward secrecy: random AES key per message
   - **DHT certificate discovery** (v0.10.2)
     - Certificates published to DHT on startup
     - Fallback: cache → active connections → DHT query
     - Key format: `cert:<node_id>`
   - Vector clocks for distributed causality tracking
   - Anti-entropy sync (5-minute interval for message reconciliation)
   - TTL: 24 hours, max hops: 5
   - Use cases: offline messaging, infrastructure outages, disaster scenarios
   - Not real-time (eventual delivery guarantee)
   - Timeout: 5s (before falling back to gossip)
   - **Status**: Production-ready (v0.10.2)
   - Location: `connection_strategies/gossip_store_forward.py`, `managers/gossip_manager.py`, `transports/gossip_connection.py`

**Key Benefits:**
- **Hub-optional architecture**: System works without Hub using direct, DHT, relay, gossip fallback
- **Near-universal connectivity**: 6 fallback layers ensure connections in almost any network condition
- **Disaster resilience**: Gossip protocol provides eventual delivery during infrastructure outages
- **Zero infrastructure cost**: Volunteer relays replace expensive TURN servers

**Configuration:** See `settings.py` for `[connection]`, `[hole_punch]`, `[relay]`, `[gossip]` config sections

### Key Components

**Client Backend (`dpc-client/core`):**

**Service Layer:**
- `service.py` - Main orchestrator (CoreService, ~3,270 lines)
  - Lifecycle management (startup/shutdown)
  - Component initialization
  - Configuration loading
  - WebSocket API server coordination
  - MessageRouter integration for command dispatch

**Message Routing System (v0.8.0+):**
- `message_router.py` - Command dispatcher for P2P messages
  - Routes incoming messages to appropriate handlers
  - Handler registry pattern for extensibility
  - Supports handler registration/unregistration at runtime
- `message_handlers/hello_handler.py` - HELLO command (initial handshake)
- `message_handlers/text_handler.py` - SEND_TEXT command (chat messages)
- `message_handlers/context_handler.py` - Context request/response handlers
  - RequestContextHandler - GET_CONTEXT requests
  - ContextResponseHandler - CONTEXT_RESPONSE handling
  - RequestDeviceContextHandler - GET_DEVICE_CONTEXT requests
  - DeviceContextResponseHandler - DEVICE_CONTEXT_RESPONSE handling
- `message_handlers/inference_handler.py` - AI inference handlers
  - RemoteInferenceRequestHandler - REMOTE_INFERENCE_REQUEST
  - RemoteInferenceResponseHandler - REMOTE_INFERENCE_RESPONSE
- `message_handlers/provider_handler.py` - Provider discovery handlers
  - GetProvidersHandler - GET_PROVIDERS requests
  - ProvidersResponseHandler - PROVIDERS_RESPONSE handling
- `message_handlers/knowledge_handler.py` - Knowledge commit handlers
  - ContextUpdatedHandler - CONTEXT_UPDATED broadcast
  - ProposeKnowledgeCommitHandler - PROPOSE_KNOWLEDGE_COMMIT
  - VoteKnowledgeCommitHandler - VOTE_KNOWLEDGE_COMMIT
  - KnowledgeCommitResultHandler - KNOWLEDGE_COMMIT_RESULT
- `message_handlers/gossip_handler.py` - Gossip protocol handlers
  - GossipSyncHandler - GOSSIP_SYNC anti-entropy reconciliation
  - GossipMessageHandler - GOSSIP_MESSAGE epidemic routing (v0.10.2 target)
- `message_handlers/file_transfer_handlers.py` - File transfer handlers (v0.11.0)
  - FileOfferHandler - FILE_OFFER (incoming file offer, firewall check, user prompt)
  - FileAcceptHandler - FILE_ACCEPT (start chunked transfer)
  - FileChunkHandler - FILE_CHUNK (receive and reassemble chunks)
  - FileCompleteHandler - FILE_COMPLETE (verify hash, finalize transfer)
  - FileCancelHandler - FILE_CANCEL (cleanup on cancellation)

**Coordinators (v0.10.0+):**
- `connection_orchestrator.py` - 6-tier connection fallback coordinator (v0.10.0)
  - **Integration Status:** Fully implemented and integrated in CoreService
  - Tries IPv6, IPv4, WebRTC, hole punch, relay, gossip in priority order
  - Per-strategy timeout configuration
  - Connection statistics tracking (success rate, strategy usage)
  - Dynamic strategy enable/disable
- `inference_orchestrator.py` - AI inference coordination (local/remote)
  - Executes queries on local LLM or remote peer
  - Assembles prompts with contexts and history
  - Tracks token usage and model metadata
- `context_coordinator.py` - Context request/response coordination
  - Manages personal and device context sharing
  - Applies firewall rules before responding
  - Tracks pending requests with asyncio.Future
- `p2p_coordinator.py` - P2P connection lifecycle coordination
  - Direct TLS connections (via dpc:// URIs)
  - WebRTC connections (via Hub signaling)
  - Peer messaging and broadcasting

**Knowledge & Consensus System (v0.9.0+):**
- `consensus_manager.py` - Multi-party knowledge voting
  - Devil's advocate mechanism (required dissenter for 3+ participants)
  - Voting session management with configurable thresholds
  - Timeout handling and vote aggregation
  - **Known Issue:** No locking during proposal edits (documented in code)
- `conversation_monitor.py` - Background knowledge detection
  - Real-time conversation analysis
  - AI-powered knowledge extraction
  - Configurable detection threshold
  - Supports local and remote inference

**Managers:**
- `p2p_manager.py` - Low-level P2P connection manager (TLS + WebRTC)
- `hole_punch_manager.py` - UDP hole punching coordinator (v0.10.0)
  - DHT-based endpoint discovery (reflexive address from 3 random DHT peers)
  - NAT type detection (cone vs symmetric)
  - Coordinated simultaneous UDP send
- `relay_manager.py` - Volunteer relay management (v0.10.0)
  - Client mode: find_relay() via DHT, quality scoring, connect_via_relay()
  - Server mode: announce_relay_availability(), handle_relay_register(), handle_relay_message()
  - Privacy-preserving (forwards encrypted payloads only)
- `gossip_manager.py` - Epidemic gossip protocol (v0.10.0)
  - send_gossip(), handle_gossip_message(), _forward_message() (fanout=3)
  - Anti-entropy sync loop (5-minute interval, vector clock reconciliation)
  - Message deduplication, TTL enforcement
- `file_transfer_manager.py` - File transfer coordination (v0.11.0)
  - Chunked file transfer (64KB chunks, SHA256 verification)
  - Active transfer tracking (upload/download progress)
  - Background process for large files (>50MB)
  - Hash verification and cleanup on cancel/error
  - Per-peer storage: `~/.dpc/conversations/{peer_id}/files/`
- `webrtc_peer.py` - WebRTC peer wrapper (aiortc)
- `hub_client.py` - Federation Hub communication (OAuth, WebSocket signaling)
- `llm_manager.py` - AI provider integration (Ollama, OpenAI, Anthropic)
- `firewall.py` - Context access control system
- `local_api.py` - WebSocket API for UI (localhost:9999)
- `settings.py` - Configuration management
- `device_context_collector.py` - Device/system info collector (generates device_context.json)

**Caching Systems:**
- `context_cache.py` - In-memory personal/device context cache
- `peer_cache.py` - Known peers cache (persisted to known_peers.json)
- `token_cache.py` - OAuth token cache for offline mode
- `connection_status.py` - Connection state tracking with operation mode (online/offline/degraded)

### Session & Chat Management (v0.11.3+)

**Session Manager:**
- `session_manager.py` - Collaborative session management
  - Mutual approval voting system for session resets
  - Multi-party session coordination
  - Prevents accidental data loss in group conversations
  - Tracks voting state and participant approvals

**Message Handlers:**
- `message_handlers/session_handler.py` - Session protocol handlers
  - NEW_SESSION_PROPOSAL - Initiate session reset vote
  - NEW_SESSION_VOTE - Cast approval/rejection vote
  - NEW_SESSION_RESULT - Distribute voting results
  - Handles history clearing for all participants

- `message_handlers/chat_history_handlers.py` - Chat history synchronization
  - CHAT_HISTORY_REQUEST - Request peer's conversation history
  - CHAT_HISTORY_RESPONSE - Send conversation history to peer
  - Automatic sync on reconnect
  - Backend→frontend sync for page refresh scenarios
  - See [CHAT_HISTORY_SYNC_DESIGN.md](docs/CHAT_HISTORY_SYNC_DESIGN.md) for full specification

**Frontend Components:**
- `NewSessionDialog.svelte` - UI for session reset voting
  - Displays vote status from all participants
  - Real-time vote tracking
  - Approval/rejection actions

**Client Frontend (`dpc-client/ui`):**
- Built with SvelteKit 5.0 + Tauri 2.x
- Entry point: `src/routes/+page.svelte`
- Backend communication: `src/lib/coreService.ts` (WebSocket client)
- SSG mode with adapter-static (SPA fallback)

**Notification System (v0.11.3+):**
- `notificationService.ts` - Native desktop notifications
  - OS permission request integration
  - Window focus tracking
  - Background event notifications
  - Tauri integration for platform-native notifications
  - Firewall-controlled notification settings

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
- See [dpc-protocol/README.md](dpc-protocol/README.md) for comprehensive library documentation

### Message Protocol (DPTP)

Messages use binary framing: 10-byte ASCII length header + JSON payload

**Formal specification:** [specs/dptp_v1.md](specs/dptp_v1.md)

**Example Message Types:**
```python
{"command": "HELLO", "payload": {"node_id": "...", "display_name": "..."}}
{"command": "GET_CONTEXT", "payload": {"tags": [...]}}
{"command": "SEND_TEXT", "payload": {"text": "..."}}
{"command": "AI_QUERY", "payload": {"query": "...", "use_context": [...]}}

# File Transfer (v0.11.0)
{"command": "FILE_OFFER", "payload": {"transfer_id": "...", "filename": "...", "size_bytes": 1024, "hash": "sha256:...", "mime_type": "..."}}
{"command": "FILE_ACCEPT", "payload": {"transfer_id": "..."}}
{"command": "FILE_CHUNK", "payload": {"transfer_id": "...", "chunk_index": 0, "total_chunks": 10, "data": "base64..."}}
{"command": "FILE_COMPLETE", "payload": {"transfer_id": "...", "hash": "sha256:..."}}
{"command": "FILE_CANCEL", "payload": {"transfer_id": "...", "reason": "user_cancelled|timeout|hash_mismatch"}}
```

### Conversation History & Context Inclusion (Phase 7+)

**Overview:**
D-PC Messenger uses a simple, reliable approach: contexts are included in every message when the checkbox is checked. This ensures information is never lost and gives users direct control over token usage.

**Key Features:**
- **Full Conversation History**: All user/assistant messages sent with every query
- **Simple Context Inclusion**: When checkbox checked → contexts always included; unchecked → never included
- **User Control**: Checkbox directly controls token usage (no hidden optimization)
- **Visual Status Indicators**: "Updated" badges show when context has changed (for peer cache invalidation)
- **Hard Limit Enforcement**: Blocks queries at 100% context window usage

**How It Works:**

**First Message in Conversation:**
```
[System Instruction]

--- CONTEXTUAL DATA ---
<CONTEXT source="local">
{personal.json content}
</CONTEXT>
<DEVICE_CONTEXT source="local">
{device_context.json content}
</DEVICE_CONTEXT>
--- END OF CONTEXTUAL DATA ---

--- CONVERSATION HISTORY ---
USER: What GPU programming frameworks should I use?
--- END OF CONVERSATION HISTORY ---
```

**Subsequent Messages (checkbox still checked):**
```
[System Instruction]

--- CONTEXTUAL DATA ---
<CONTEXT source="local">
{personal.json content}  ← STILL INCLUDED
</CONTEXT>
<DEVICE_CONTEXT source="local">
{device_context.json content}  ← STILL INCLUDED
</DEVICE_CONTEXT>
--- END OF CONTEXTUAL DATA ---

--- CONVERSATION HISTORY ---
USER: What GPU programming frameworks should I use?
ASSISTANT: [previous response about CUDA, OpenCL, etc.]
USER: Which one is best for my RTX 3060?
--- END OF CONVERSATION HISTORY ---
```

**When Contexts are Included:**
- **Checkbox checked**: Contexts included in EVERY message (personal, device, peer)
- **Checkbox unchecked**: Contexts never included (privacy/token-saving mode)
- **Simple rule**: Checkbox state = Context inclusion (no hidden optimization)

**What Gets Stored in Conversation History:**
- **Conversation history stores**: Only USER messages (prompts) and ASSISTANT messages (AI responses)
- **Context data is NOT stored**: Context data is sent as separate "CONTEXTUAL DATA" section, controlled by checkbox state
- **Context is ephemeral**: Each message includes context fresh based on current checkbox state
- **Why this design:**
  - Gives users per-message control over token usage (context can be large)
  - Allows privacy control (turn off context for sensitive questions)
  - Context data can change over time, so it's always sent fresh rather than cached
  - Conversation history remains lightweight (only prompts and responses)

**Example:**
- Message 1 (context ON): Sends context + conversation history → AI gets full context
- Message 2 (context OFF): Sends only conversation history → AI only knows what was said before, not your personal data
- Message 3 (context ON again): Sends context + conversation history → AI gets fresh context again

This means if you enable context for one message and disable it for the next, the AI will "forget" your personal context details but still remember the conversation flow.

**Backend Implementation:**
- **ConversationMonitor** tracks:
  - `message_history` - full conversation (user/assistant messages)
  - `peer_context_hashes` - per-peer context tracking (for cache invalidation)
- **CoreService** methods:
  - `_compute_context_hash()` - SHA256 hashing (for UI badges and peer cache)
  - `_assemble_final_prompt()` - builds prompt with context (if checkbox checked) + history
  - `reset_conversation()` - clears history for "New Chat" button

**Frontend Implementation:**
- **State tracking**:
  - `currentContextHash` - current hash from backend (updated on save)
  - `peerContextHashes` - per-peer current hashes
- **Visual indicators**:
  - Green "UPDATED" badge on context toggle when hash mismatch detected (peer cache)
  - Green "UPDATED" badges on peer checkboxes when peer contexts changed
- **Hard limit UI**:
  - Textarea/send button disabled at 100% context usage
  - Placeholder text: "Context window full - End session to continue"

**Events:**
- `personal_context_updated` - broadcasted when user saves personal context (includes new hash for peer cache invalidation)
- `peer_context_updated` - broadcasted when peer context change detected (includes node_id, hash)

**Commands:**
- `reset_conversation(conversation_id)` - clears history

**Rationale for Simplified Approach:**
- Modern AI models have large context windows (128K+ tokens)
- 10-message conversation with contexts always included: ~160KB (still << 128K limit)
- Simple checkbox control is more predictable and reliable than automatic optimization
- Fixes edge case where context info is "lost" if AI doesn't mention it in response
- User can uncheck checkbox anytime to save tokens in long conversations

### Node Identity System

- Node IDs: `dpc-node-[16 hex chars]` (SHA256 hash of RSA public key)
- Cryptographic files stored in `~/.dpc/`:
  - `node.key` - RSA private key (2048-bit)
  - `node.crt` - X.509 self-signed certificate
  - `node.id` - Node identifier
- Hub validates node identity via certificate verification

**Important Limitation: Single Device Per User**

The current Hub implementation supports **one device per user account** (one device per email address):
- Each device generates a unique cryptographic identity (`node_id`)
- Hub database schema: Users table has one `node_id` field per user (not one-to-many)
- When logging in from a second device, the Hub overwrites the first device's `node_id`
- This "orphans" the first device from Hub services (WebRTC signaling won't work)
- Direct P2P (TLS) connections still work without Hub

**Database Schema (dpc-hub/dpc_hub/models.py:15-49):**
```python
class User(Base):
    email = Column(String, unique=True)  # User identity
    node_id = Column(String, unique=True)  # ⚠️ Only ONE node_id per user
    provider = Column(String)  # 'google' or 'github'
```

**Workarounds:**
- Use different email addresses for each device
- Direct TLS P2P connections don't require Hub registration

**Future Enhancement:**
Multi-device support would require:
- Database schema change: One-to-many relationship (User → multiple Devices)
- UI for device management
- Signaling updates to route to specific devices

See [docs/CONFIGURATION.md](docs/CONFIGURATION.md#device-identity-and-multi-device-considerations) for detailed explanation.

### Configuration System

**Configuration Hierarchy (highest priority first):**
1. Environment variables (e.g., `DPC_HUB_URL`, `DPC_OAUTH_CALLBACK_PORT`)
2. Config file (`~/.dpc/config.ini`)
3. Built-in defaults

**Configuration Files:**
- Windows: `C:\Users\<username>\.dpc\config.ini`
- Linux/Mac: `~/.dpc/config.ini`
- Providers: `~/.dpc/providers.json` (AI provider configs)
- Privacy Rules: `~/.dpc/privacy_rules.json` (context sharing rules)
- Personal Context: `~/.dpc/personal.json` (auto-generated)
- Device Context: `~/.dpc/device_context.json` (auto-generated, structured hardware/software info)
- File Storage: `~/.dpc/conversations/{peer_id}/files/` (received files, per-peer isolation)

**Automatic Device Context Collection:**

The client automatically collects device and system information on startup and stores it in a **separate `device_context.json` file** with structured fields. This enables environment-aware AI assistance without repeatedly asking "What GPU do you have?" or "How much RAM?"

**Storage Architecture:**
- **device_context.json**: Structured JSON file (`~/.dpc/device_context.json`) with hardware/software details
- **personal.json**: Contains reference to device_context.json in metadata.external_contexts
- **Auto-updates**: Refreshes on every client startup (detects driver updates, new tools, etc.)

**What's collected:**

*Hardware (with structured fields):*
```json
{
  "hardware": {
    "cpu": {
      "cores_physical": 4,
      "cores_logical": 4,
      "architecture": "AMD64"
    },
    "memory": {
      "ram_gb": 23.95,
      "ram_tier": "32GB"
    },
    "gpu": {
      "type": "nvidia",
      "model": "NVIDIA GeForce RTX 3060",
      "vram_gb": 12.0,
      "vram_mib": 12288,
      "driver_version": "576.28",
      "cuda_version": "12.8"
    },
    "storage": {
      "free_gb": 150.25,
      "total_gb": 500.0,
      "free_tier": "100GB+",
      "type": "SSD"
    }
  }
}
```

*Software (with structured fields):*
```json
{
  "software": {
    "os": {
      "family": "Windows",
      "version": "10",
      "architecture": "64bit"
    },
    "runtime": {
      "python": {
        "version": "3.12.0",
        "major": 3,
        "minor": 12
      }
    },
    "shell": {"type": "bash.exe"},
    "dev_tools": {
      "git": "2.47",
      "docker": "28.4",
      "node": "22.14"
    },
    "package_managers": ["pip", "poetry", "npm", "winget"]
  }
}
```

*Timestamps (for tracking):*
```json
{
  "created_at": "2025-11-17T07:00:00.000000Z",
  "last_updated": "2025-11-17T08:30:00.000000Z",
  "collection_timestamp": "2025-11-17T08:30:00.000000Z"
}
```

*Special Instructions (schema v1.1+):*
```json
{
  "schema_version": "1.1",
  "special_instructions": {
    "interpretation": {
      "privacy_tiers": "ram_tier and free_tier are privacy-rounded values...",
      "capability_inference": "Map GPU VRAM to model sizes: 12GB → 13B models...",
      "version_compatibility": "Match CUDA version with PyTorch/TensorFlow...",
      "platform_specificity": "Consider OS family when suggesting commands..."
    },
    "privacy": {
      "sensitive_paths": "Never share executable paths - only versions...",
      "optional_fields": "ai_models requires opt-in (collect_ai_models=true)...",
      "default_sharing": "Only software.os and dev_tools by default..."
    },
    "update_protocol": {
      "auto_refresh": "Refreshes on client startup...",
      "opt_in_features": "Enable ai_models with collect_ai_models=true...",
      "staleness_check": "Recommend refresh if >7 days old..."
    },
    "usage_scenarios": {
      "local_inference": "Consider GPU VRAM and installed models...",
      "remote_inference": "Match peer GPU capabilities...",
      "dev_environment": "Prioritize user's package managers...",
      "cross_platform": "Provide platform-native instructions..."
    }
  }
}
```

The `special_instructions` block (added in schema v1.1) provides comprehensive guidelines for AI systems on how to interpret and use device context data. This includes privacy rules (which fields to filter), capability inference rules (mapping GPU specs to model sizes), and platform-specific guidance (suggesting appropriate commands for Windows/Linux/macOS). See [docs/DEVICE_CONTEXT_SPEC.md](docs/DEVICE_CONTEXT_SPEC.md) for full specification.

**Cross-Platform Support:**
- **NVIDIA GPUs**: Full details via nvidia-smi (Windows/Linux/macOS)
- **AMD GPUs**: rocm-smi (Linux)
- **Apple GPUs**: Metal detection (macOS)
- **Intel GPUs**: lspci parsing (Linux)

**Privacy Features:**
- Both exact RAM (23.95GB) and privacy tier (32GB) stored
- Tool versions rounded to major.minor
- No serial numbers, MAC addresses, or hostnames
- Separate file = easy to exclude from sharing
- Firewall controls what gets shared

**Configuration:**
```ini
[system]
auto_collect_device_info = true   # Master toggle
collect_hardware_specs = true     # CPU/RAM/disk/GPU details
collect_dev_tools = true          # Git, Docker, Node, etc.
collect_ai_models = false         # Ollama models (opt-in for compute-sharing)
```

**Example AI Assistance** (powered by special_instructions):
- **GPU-aware**: "Your RTX 3060 (12GB VRAM) can run llama3:13b comfortably"
- **Driver-aware**: "CUDA 12.8 detected - use PyTorch 2.5+ for compatibility"
- **Resource-aware**: "You have 24GB RAM - can run 2 models simultaneously"
- **Platform-specific**: "Windows 10 detected - use WSL2 for better Linux compatibility"
- **Compute-sharing**: "Alice has RTX 4090 (24GB) - offload training to her GPU"
- **Privacy-aware**: "Sharing only OS version and dev tools, hardware specs require explicit allow rules"

### In-App Configuration Editors

The client provides in-app editors for key configuration files, eliminating the need to manually edit files or restart the service.

**Personal Context Editor:**
- **Location**: Click **"📚 View Personal Context"** button in sidebar
- **Features**:
  - Edit mode with live preview
  - Editable Profile fields (name, description)
  - Editable AI Instructions (primary instructions, bias mitigation settings)
  - Real-time validation
  - Save changes without restart
  - Changes apply immediately to active sessions
- **Editable Fields**:
  - Profile: Name, description, core values
  - AI Instructions: Primary instruction text
  - Bias Mitigation: Multi-perspective analysis, challenge status quo, cultural sensitivity
- **Read-Only Fields**:
  - Knowledge topics (managed via Knowledge Commit system)
  - Commit history
  - Metadata (version, timestamps)

**Firewall Rules Editor:**
- **Location**: Click **"🛡️ Firewall Rules"** button in sidebar
- **Format**: JSON (stored as `~/.dpc/privacy_rules.json`)
- **UI Style**: Form-based interface matching Personal Context editor (DRY principle)
- **Features**:
  - Tab-based navigation (Hub Sharing, Node Groups, Compute Sharing, Peer Permissions)
  - Native HTML form elements (no Monaco editor)
  - Edit/Save/Cancel workflow with unsaved changes detection
  - Real-time validation on save
  - Hot-reload: Changes apply immediately without restarting service
- **Tabs**:
  - **Hub Sharing**: Control what the Hub can see for discovery
  - **Node Groups**: Define groups of nodes with add/remove functionality
  - **Compute Sharing**: Enable/configure remote inference with checkboxes and textareas
  - **Peer Permissions**: View per-node and per-group access rules (read-only for now)
- **Validation**:
  - Checks JSON structure
  - Validates node ID format (`dpc-node-*`)
  - Validates rule values (`allow`, `deny`)
  - Validates compute settings (boolean, lists)
  - Validates node group memberships

**Backend Commands** (available via WebSocket API):
- `get_personal_context` - Load current personal context
- `save_personal_context` - Save updated personal context
- `reload_personal_context` - Reload from disk
- `get_firewall_rules` - Load current firewall rules as JSON dict
- `save_firewall_rules` - Save and validate new firewall rules (accepts JSON dict)

**Hot-Reload Mechanism:**
- Firewall rules reload automatically when saved via UI
- Personal context updates propagate to active P2P connections
- No service restart required for configuration changes
- WebSocket events notify UI of successful updates

**UI Integration Pattern for New Firewall Fields:**
When adding new UI components that display data from `privacy_rules.json` (e.g., node groups dropdown, compute settings toggle):
1. Create a writable store in `coreService.ts` (e.g., `export const firewallRulesUpdated = writable<any>(null)`)
2. Add event handler in `coreService.ts` for `firewall_rules_updated` event to update the store
3. In UI component, create a load function with a guard flag (like `aiScopesLoaded`) to prevent infinite reactive loops
4. Add reactive statement to reload data when `$firewallRulesUpdated` changes and connection is active
5. This ensures UI stays in sync with `privacy_rules.json` without requiring page refresh
- **Example**: AI scopes dropdown (`+page.svelte:499-543`) reloads immediately after user saves firewall rules
- **Pattern documented in**: `firewall.py:75-84`, `coreService.ts:32-39`, `+page.svelte:499-543`

**Example Workflow** (Firewall Rules):
1. Click "🛡️ Firewall Rules" button
2. Click "Edit" button in header
3. Use tabs to navigate to desired section (Hub Sharing, Node Groups, etc.)
4. Edit rules using form elements (selects, inputs, textareas, checkboxes)
5. Click "Save" button
6. Backend validates and applies rules
7. Changes take effect immediately for all new requests
8. Success message shown in UI, automatically exits edit mode after 2 seconds

**Manual File Editing** (Still Supported):
- Files can still be edited manually in external editors
- Use "Reload from Disk" button to apply external changes
- Backend validates before applying
- Invalid changes are rejected with error details

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

CoreService orchestrates independent components via coordinators and managers:

**Coordinators (High-Level):**
- InferenceOrchestrator - AI query execution and prompt assembly
- ContextCoordinator - Context sharing with firewall integration
- P2PCoordinator - P2P connection lifecycle and messaging

**Managers (Low-Level):**
- P2PManager - Socket-level connection management
- HubClient - Federation Hub communication
- LLMManager - AI provider API integration
- ContextFirewall - Access control rules engine
- LocalApiServer - WebSocket API for UI

**Pattern:** Coordinators provide clean APIs for high-level operations, delegating to managers for low-level implementation. This enables easier testing, better separation of concerns, and cleaner extension points for Phase 2 features.

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

Access control file format (`~/.dpc/privacy_rules.json`):
```json
{
  "hub": {
    "personal.json:profile.name": "allow",
    "personal.json:profile.description": "allow"
  },
  "node_groups": {
    "friends": ["dpc-node-alice-123", "dpc-node-bob-456"],
    "trusted": ["dpc-node-charlie-789"]
  },
  "groups": {
    "friends": {
      "personal.json:profile.*": "allow",
      "personal.json:knowledge.*": "allow"
    }
  },
  "nodes": {
    "dpc-node-alice-123": {
      "personal.json:*": "allow"
    }
  },
  "compute": {
    "enabled": false,
    "allow_nodes": [],
    "allow_groups": [],
    "allowed_models": []
  },
  "file_transfer": {
    "allow_nodes": ["dpc-node-alice-123"],
    "allow_groups": ["friends"],
    "max_size_mb": 1000,
    "allowed_mime_types": ["*"]
  },
  "notifications": {
    "enabled": true,
    "show_when_focused": false,
    "allowed_events": ["message_received", "file_received", "peer_connected"]
  }
}
```

---

## Debugging Tips

### Backend Issues

**Check logs:**
```bash
# View real-time logs (console output in development)
cd dpc-client/core
poetry run python run_service.py

# View log file (production logs)
tail -f ~/.dpc/logs/dpc-client.log

# Enable DEBUG logging for specific modules
export DPC_LOG_MODULE_LEVELS="dpc_client_core.p2p_manager:DEBUG,dpc_client_core.webrtc_peer:DEBUG"
poetry run python run_service.py
```

**Logging System:**
- All components use Python standard library `logging`
- Logs stored in `~/.dpc/logs/dpc-client.log` (auto-rotates at 10MB)
- Per-module log levels configurable via `config.ini` or env vars
- See [docs/LOGGING.md](docs/LOGGING.md) for complete guide

**Common issues:**
- Port 8888 or 9999 already in use
- Certificate files missing in `~/.dpc/`
- AI provider not configured in `~/.dpc/providers.json`

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
- `~/.dpc/providers.json` - AI provider settings
- `~/.dpc/privacy_rules.json` - Context firewall rules
- `~/.dpc/node.{key,crt,id}` - Node identity files

### Important Documentation
- `README.md` - Project overview
- `docs/QUICK_START.md` - 5-minute setup guide
- `docs/WEBRTC_SETUP_GUIDE.md` - Production deployment
- `docs/CONFIGURATION.md` - Complete configuration reference
- `docs/LOGGING.md` - Logging system configuration and troubleshooting
- `docs/DEVICE_CONTEXT_SPEC.md` - Device context schema and special instructions specification
- `docs/GITHUB_AUTH_SETUP.md` - GitHub OAuth setup and testing
- `specs/dptp_v1.md` - DPTP (D-PC Transfer Protocol) formal specification
- `specs/hub_api_v1.md` - Hub API specification
- `dpc-protocol/README.md` - Protocol library documentation and usage examples
- `VISION.md` - Business vision, market opportunity, and mission (investor/co-founder focused)
- `PRODUCT_VISION.md` - Product vision and technical philosophy

---

## AI Providers

D-PC Messenger supports multiple AI providers for local and cloud-based inference:

### Supported Providers
- **Ollama**: Local models (llama3, mistral, qwen, etc.) via Ollama API
- **OpenAI Compatible**: OpenAI and compatible APIs (OpenAI, LM Studio, etc.)
- **Anthropic**: Claude models (Claude 3.5 Sonnet, Claude Opus, etc.)
- **Z.AI**: GLM models (GLM-4.7, GLM-4.6, GLM-4.5, etc.)

### Z.AI Setup

Z.AI provides access to the GLM series of language models, including both text and vision capabilities.

**Installation:**
```bash
cd dpc-client/core
poetry install  # Installs zai-sdk automatically
```

**Configuration:**
1. Get API key from [docs.z.ai](https://docs.z.ai)
2. Set environment variable:
   ```bash
   export ZAI_API_KEY="your_key_here"
   ```
3. Add to `~/.dpc/providers.json`:
   ```json
   {
     "alias": "zai_glm47",
     "type": "zai",
     "model": "glm-4.7",
     "api_key_env": "ZAI_API_KEY",
     "context_window": 128000
   }
   ```

**Available Models:**
- **Text Models:** `glm-4.7`, `glm-4.6`, `glm-4.5`, `glm-4.5-air`, `glm-4.5-airx`, `glm-4.5-flash`, `glm-4-plus`, `glm-4-128-0414-128k`
- **Vision Models:** `glm-4.6v-flash`, `glm-4.5v`, `glm-4.0v`

**Rate Limits:**
Z.AI uses concurrency-based rate limiting (not token-based):
- GLM-4.7: 2 concurrent requests
- GLM-4.6: 3 concurrent requests
- GLM-4.5: 10 concurrent requests
- GLM-4-Plus: 20 concurrent requests

**Example Usage:**
```json
{
  "default_provider": "zai_glm47",
  "providers": [
    {
      "alias": "zai_glm47",
      "type": "zai",
      "model": "glm-4.7",
      "api_key_env": "ZAI_API_KEY"
    },
    {
      "alias": "zai_vision",
      "type": "zai",
      "model": "glm-4.6v-flash",
      "api_key_env": "ZAI_API_KEY"
    }
  ]
}
```

**See also:** `dpc-client/providers.example.json` for complete configuration examples

---

## Technology Stack

### Backend
- Python 3.12+ with Poetry
- FastAPI (Hub), WebSockets (Client)
- aiortc (WebRTC implementation)
- SQLAlchemy + asyncpg + Alembic (Hub database)
- cryptography library (PKI)
- ollama, openai, anthropic, zai-sdk (AI SDKs)

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
