# Architecture

The component inventory of D-PC Messenger: which module does what, how the connection
tiers fall back, how a prompt is assembled. It moved here from `CLAUDE.md` on 2026-11-15 so
that the file every agent request loads stays short. A list of files drifts: when this page
and the code disagree, the code is right and this page is the one to fix.

Configuration keys are in [CONFIGURATION.md](CONFIGURATION.md), the wire in
[specs/dptp_v1.md](../specs/dptp_v1.md), words in [GLOSSARY.md](GLOSSARY.md), and the
decisions behind the structure in [decisions/](decisions/).

---

## Connection tiers (6-tier fallback hierarchy)

D-PC Messenger uses a 6-tier connection fallback hierarchy for near-universal P2P
connectivity (v0.10.1). The ConnectionOrchestrator (`coordinators/connection_orchestrator.py`)
tries each strategy in priority order until one succeeds, making the Hub optional.

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
   - **Status**: Experimental (PoC) with DTLS encryption; **disabled by default** (`enable_hole_punching = false` in `settings.py`) — opt-in
   - Location: `connection_strategies/udp_hole_punch.py`, `managers/hole_punch_manager.py`
   - Testing: See `archive/docs/MANUAL_TESTING_GUIDE.md` (archived)

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
   - **Status**: Experimental (PoC) (v0.10.2)
   - Location: `connection_strategies/gossip_store_forward.py`, `managers/gossip_manager.py`, `transports/gossip_connection.py`

**Key benefits:**
- **Hub-optional architecture**: System works without Hub using direct, DHT, relay, gossip fallback
- **Near-universal connectivity**: 6 fallback layers ensure connections in almost any network condition
- **Disaster resilience**: Gossip protocol provides eventual delivery during infrastructure outages
- **Zero infrastructure cost**: Volunteer relays replace expensive TURN servers

**Configuration:** `[connection]`, `[hole_punch]`, `[relay]`, `[gossip]` in
[CONFIGURATION.md](CONFIGURATION.md#phase-6-connection-strategy-configuration-v0100).

**WebRTC NAT traversal:** STUN servers for public IP discovery (Google STUN); TURN relay for
symmetric NAT is opt-in with nothing configured by default (see [TURN_SETUP.md](TURN_SETUP.md));
ICE candidate gathering and connectivity checks; connection state monitoring in
`webrtc_peer.py`.

---

## Key components

Paths below are relative to `dpc-client/core/dpc_client_core/` unless they say otherwise.

### Client backend (`dpc-client/core`)

**Service layer:**
- `service.py` - Main orchestrator (CoreService)
  - Lifecycle management (startup/shutdown)
  - Component initialization
  - Configuration loading
  - WebSocket API server coordination
  - MessageRouter integration for command dispatch

**Extracted domain services (refactor/grand, v0.21.0, [ADR-001](decisions/001-service-split.md)):**
- `voice_service.py` - Whisper model lifecycle and voice message handling
- `knowledge_service.py` - Knowledge commit flows (wired to ConversationMonitor via callbacks)
- `telegram_service.py` - Telegram bot initialization glue (managers in `managers/`)
- `agent_service.py` - Agent lifecycle wiring (LLMManager, AgentManager, KnowledgeService)

**Message routing system (v0.8.0+):**
- `message_router.py` - Command dispatcher for P2P messages
  - Routes incoming messages to appropriate handlers
  - Handler registry pattern for extensibility
  - Supports handler registration/unregistration at runtime
- `message_handlers/hello_handler.py` - HELLO command (initial handshake)
- `message_handlers/text_handler.py` - SEND_TEXT command (chat messages)
- `message_handlers/context_handler.py` - Context request/response handlers
  - RequestContextHandler - REQUEST_CONTEXT requests
  - ContextResponseHandler - CONTEXT_RESPONSE handling
  - RequestDeviceContextHandler - REQUEST_DEVICE_CONTEXT requests
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
- `message_handlers/group_handler.py` - Group chat message routing
- `message_handlers/skill_handler.py` - Agent skill invocation requests
- `message_handlers/relay_register_handler.py` - Relay node registration
- `message_handlers/relay_message_handler.py` - Relay message forwarding
- `message_handlers/relay_response_handler.py` - Relay response handling
- `message_handlers/relay_disconnect_handler.py` - Relay disconnect cleanup
- `message_handlers/transcription_handler.py` - Transcription request/response
- `message_handlers/voice_transcription_handler.py` - Voice message transcription sharing
- `message_handlers/session_handler.py`, `message_handlers/chat_history_handlers.py` - see
  [Session and chat management](#session-and-chat-management) below
- File transfer handlers (v0.11.0) — split into individual files:
  - `message_handlers/file_offer_handler.py` - FILE_OFFER (incoming offer, firewall check, user prompt)
  - `message_handlers/file_accept_handler.py` - FILE_ACCEPT (start chunked transfer)
  - `message_handlers/file_chunk_handler.py` - FILE_CHUNK (receive and reassemble chunks)
  - `message_handlers/file_chunk_retry_handler.py` - FILE_CHUNK_RETRY (retransmit failed chunk)
  - `message_handlers/file_complete_handler.py` - FILE_COMPLETE (verify hash, finalize transfer)
  - `message_handlers/file_cancel_handler.py` - FILE_CANCEL (cleanup on cancellation)

**Coordinators (v0.10.0+):**
- `coordinators/connection_orchestrator.py` - 6-tier connection fallback coordinator (v0.10.0)
  - Fully implemented and integrated in CoreService
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
- `coordinators/telegram_coordinator.py` - Telegram ↔ DPC message bridge and routing
- `skill_coordinator.py` - Agent skill discovery, routing, and result delivery

**Embedded agent system (`dpc_agent/`, v0.18.0+):**
- `dpc_agent/agent.py` - Core agent class (tool dispatch, memory, task execution)
- `dpc_agent/loop.py` - Autonomous agent run loop
- `dpc_agent/memory.py` - Per-agent persistent memory
- `dpc_agent/sleep_pipeline.py` - Sleep consolidation, morning brief generation
- `dpc_agent/task_queue.py` - Async task queue with priority scheduling
- `dpc_agent/budget.py` - Token/compute budget enforcement
- `dpc_agent/skill_store.py` - Dynamic skill registry and versioning
- `dpc_agent/tools/` - Agent tool implementations (shell, editor, browser, etc.)
- See [agent/DPC_AGENT_GUIDE.md](agent/DPC_AGENT_GUIDE.md) for usage

**Retrieval backend (`dpc_agent/retrieval/`, ADR-024 §1.6, 2026-05-18):**

A composable abstraction over vector + text + fusion channels, so the graph backend
(SQLite / Grafeo via ADR-024 §1.5) and the retrieval backend are picked independently per
agent.

- `dpc_agent/retrieval/base.py` — `VectorIndex`, `TextIndex`, `HybridFuser`
  ABCs + `RetrievalBackend` composite dataclass
- `dpc_agent/retrieval/native.py` — `NativeVectorIndex` / `NativeTextIndex`
  / `NativeHybridFuser` wrappers over the existing `FaissIndex` +
  `BM25Index` + `reciprocal_rank_fusion` code (default in production)
- `dpc_agent/retrieval/grafeo.py` — `GrafeoVectorIndex` /
  `GrafeoTextIndex` / `GrafeoHybridFuser` using Grafeo native HNSW + BM25
  (opt-in via config flag)
- `dpc_agent/retrieval/factory.py` — `make_backend_for_agent(agent_root)`
  reads `<agent_root>/config.json` `retrieval_*` keys and dispatches.
  Production call-sites use this helper, never `FaissIndex(...)` directly.

The per-agent flags (`retrieval_vector`, `retrieval_text`, `retrieval_fusion`), their
defaults and the rollback are in
[agent/DPC_AGENT_GUIDE.md](agent/DPC_AGENT_GUIDE.md) and
[ADR-024](decisions/024-knowledge-graph-infrastructure.md). Rollback in one line: remove the
keys and restart; the FAISS state at `<agent_root>/state/memory_index/{vectors.faiss,bm25/}`
is untouched.

**Knowledge and consensus system (v0.9.0+):**
- `consensus_manager.py` - Multi-party knowledge voting
  - Devil's advocate mechanism (required dissenter for 3+ participants)
  - Voting session management with configurable thresholds
  - Timeout handling and vote aggregation
  - **Known issue:** No locking during proposal edits (documented in code)
- `conversation_monitor.py` - Background knowledge detection
  - Real-time conversation analysis
  - AI-powered knowledge extraction
  - Configurable detection threshold
  - Supports local and remote inference
- Full design: [KNOWLEDGE_ARCHITECTURE.md](KNOWLEDGE_ARCHITECTURE.md)

**Managers:**
- `p2p_manager.py` - Low-level P2P connection manager (TLS + WebRTC)
- `managers/agent_manager.py` - Agent lifecycle, task execution, tool dispatch
- `managers/agent_telegram_bridge.py` - Bridge between agent system and Telegram
- `managers/group_manager.py` - Group chat membership, history, metadata
- `managers/instruction_manager.py` - Per-peer custom AI instruction management
- `managers/prompt_manager.py` - Prompt template assembly and caching
- `managers/token_count_manager.py` - Token counting and context window tracking
- `managers/hole_punch_manager.py` - UDP hole punching coordinator (v0.10.0)
  - DHT-based endpoint discovery (reflexive address from 3 random DHT peers)
  - NAT type detection (cone vs symmetric)
  - Coordinated simultaneous UDP send
- `managers/relay_manager.py` - Volunteer relay management (v0.10.0)
  - Client mode: find_relay() via DHT, quality scoring, connect_via_relay()
  - Server mode: announce_relay_availability(), handle_relay_register(), handle_relay_message()
  - Privacy-preserving (forwards encrypted payloads only)
- `managers/gossip_manager.py` - Epidemic gossip protocol (v0.10.0)
  - send_gossip(), handle_gossip_message(), _forward_message() (fanout=3)
  - Anti-entropy sync loop (5-minute interval, vector clock reconciliation)
  - Message deduplication, TTL enforcement
- `managers/file_transfer_manager.py` - File transfer coordination (v0.11.0)
  - Chunked file transfer (64KB chunks, SHA256 verification; the file is read once for both digests)
  - Receiving writes chunks at their offsets into a `.tmp` file beside the final one, renamed on completion; after 30 s with no chunk the receiver re-asks up to 64 missing indices, and after 3 empty rounds cancels with `missing_chunks`
  - Active transfer tracking (upload/download progress)
  - Background process for large files (>50MB)
  - Hash verification and cleanup on cancel/error
  - Per-peer storage: `~/.dpc/conversations/{peer_id}/files/`
- `managers/llama_server_supervisor.py`, `managers/strata_supervisor.py` - the two supervised
  local engines (see [AI providers](CONFIGURATION.md#ai-providers-providersjson))
- `webrtc_peer.py` - WebRTC peer wrapper (aiortc)
- `hub_client.py` - Federation Hub communication (OAuth, WebSocket signaling)
- `llm_manager.py` - AI provider registry and routing (implementations in `providers/`,
  listed under [AI providers](CONFIGURATION.md#ai-providers-providersjson))
- `firewall.py` - Context access control system
- `local_api.py` - WebSocket API for UI (localhost:9999)
- `settings.py` - Configuration management
- `device_context_collector.py` - Device/system info collector (generates device_context.json)

**Caching systems:**
- `context_cache.py` - In-memory personal/device context cache
- `peer_cache.py` - Known peers cache (persisted to `~/.dpc/peer_cache.json`, `p2p_manager.py`;
  this page said `known_peers.json` until 2026-11-15)
- `token_cache.py` - OAuth token cache for offline mode
- `connection_status.py` - Connection state tracking with operation mode (online/offline/degraded)

### Session and chat management

**Session manager (v0.11.3+):**
- `session_manager.py` - Collaborative session management
  - Mutual approval voting system for session resets
  - Multi-party session coordination
  - Prevents accidental data loss in group conversations
  - Tracks voting state and participant approvals

**Message handlers:**
- `message_handlers/session_handler.py` - Session protocol handlers
  - PROPOSE_NEW_SESSION - Initiate session reset vote
  - VOTE_NEW_SESSION - Cast approval/rejection vote
  - NEW_SESSION_RESULT - Distribute voting results
  - Handles history clearing for all participants
- `message_handlers/chat_history_handlers.py` - Chat history synchronization
  - REQUEST_CHAT_HISTORY - Request peer's conversation history
  - CHAT_HISTORY_RESPONSE - Send conversation history to peer
  - Automatic sync on reconnect
  - Backend→frontend sync for page refresh scenarios
  - See [CHAT_HISTORY_SYNC_DESIGN.md](../archive/docs/CHAT_HISTORY_SYNC_DESIGN.md) for the full specification (archived)

(The command names above were `NEW_SESSION_PROPOSAL`, `NEW_SESSION_VOTE` and
`CHAT_HISTORY_REQUEST` in `CLAUDE.md`; the handlers and [DPTP §3.15–3.16](../specs/dptp_v1.md)
use the names given here.)

**Frontend component:** `NewSessionDialog.svelte` - UI for session reset voting: displays the
vote status from all participants, real-time vote tracking, approval/rejection actions.

### Client frontend (`dpc-client/ui`)

- Built with SvelteKit 5.0 + Tauri 2.x
- Entry point: `src/routes/+page.svelte` (layout shell; domain logic in `src/lib/panels/`);
  Tauri entry point `src-tauri/src/main.rs`
- Backend communication: `src/lib/coreService.ts` (WebSocket client, thin bootstrapper)
- SSG mode with adapter-static (SPA fallback)
- **Domain panels** (`src/lib/panels/`): ChatPanel, AgentPanel, VoicePanel, GroupPanel, TelegramPanel, KnowledgeEventsPanel, ModelDownloadPanel, HistorySyncPanel, ChatHistorySyncPanel, SessionEventsPanel, MessageRouterPanel, PersistencePanel, AgentManagementPanel, GroupManagementPanel, AddAIChatPanel
- **Domain services** (`src/lib/services/`): messaging.ts, agents.ts, connection.ts, voice.ts, knowledge.ts, telegram.ts, groups.ts, fileTransfer.ts, session.ts, providers.ts, modelDownload.ts, blockedPeers.ts, scheduleApproval.ts, shellApproval.ts
- Store strategy: [ADR-003](decisions/003-frontend-stores.md); theming tokens: [ADR-035](decisions/035-frontend-theming-tokens.md)
- How a UI component stays in sync with `privacy_rules.json`:
  [CONFIGURATION.md — UI integration pattern for new firewall fields](CONFIGURATION.md#ui-integration-pattern-for-new-firewall-fields)

**Notification system (v0.11.3+):** `src/lib/notificationService.ts` - native desktop
notifications: OS permission request integration, window focus tracking, background event
notifications, Tauri integration for platform-native notifications, firewall-controlled
notification settings (`notifications` in `privacy_rules.json`).

### Hub server (`dpc-hub`)

- `main.py` - FastAPI application and routes
- `auth.py` - OAuth 2.0 + JWT authentication
- `crypto_validation.py` - Node identity validation
- `models.py` - SQLAlchemy database models
- `crud.py` - Database operations
- `connection_manager.py` - WebSocket signaling manager
- Database: PostgreSQL with Alembic migrations (commands and the migration test cycle in
  [dpc-hub/README.md](../dpc-hub/README.md))

### Protocol library (`dpc-protocol`)

- `crypto.py` - Node identity, RSA keys, X.509 certificates
- `protocol.py` - Message serialization (10-byte header + JSON)
- `pcm_core.py` - Personal Context Model data structures
- `knowledge_commit.py` - Knowledge commit data structures and signing
- `commit_integrity.py` - Commit hash verification and chain integrity
- `markdown_manager.py` - Markdown rendering utilities
- See [dpc-protocol/README.md](../dpc-protocol/README.md) for the library documentation

---

## Voice messages (v0.13.0–v0.15.0)

Voice messages use the file transfer path (FILE_OFFER/FILE_CHUNK/FILE_COMPLETE) with an extra
`voice_metadata` object in the payload; its fields are in
[DPTP §3.11](../specs/dptp_v1.md#311-file-transfer-messages-v0110). Voice files are stored in
`~/.dpc/conversations/{peer_id}/files/` and shown in the chat with audio player controls.
Configuration: `[voice_messages]` in [CONFIGURATION.md](CONFIGURATION.md).

**Recording:**
- **Format**: WAV (16-bit PCM, 48kHz mono) via Tauri Rust backend (`audio_recorder.rs`)
- **Cross-platform**:
  - Windows: Native via Edge WebView2
  - Linux: Rust cpal library with ALSA/PipeWire support (v0.15.0+)
  - macOS: Requires Info.plist permissions

**Transcription:**
- **Local Whisper**: `LocalWhisperProvider` in `providers/whisper_provider.py`
- **GPU acceleration**:
  - MLX (Apple Silicon M1/M2/M3/M4) - Optional via `uv sync --extra mlx --inexact`
  - CUDA (NVIDIA GPUs) - Auto-detected
  - MPS (macOS Metal) - Fallback
  - CPU - Universal fallback
- **Features**:
  - Lazy loading (model loads on first use)
  - Adaptive batch_size (reduces VRAM for long messages)
  - VRAM management with `unload_model_async()`
  - Retroactive transcription (v0.13.1+)
  - Offline mode with interactive download (v0.13.1+)

**VRAM management (v0.15.0):**
- Models unload when auto-transcribe disabled
- `torch.cuda.empty_cache()` called after unloading
- Unload-before-load enforced in `providers/whisper_provider.py` — VRAM conflict fixed in Sprint 1
- See `providers/whisper_provider.py` (`_unload_model`, `unload_model_async`) for implementation

**Frontend components:**
- `VoiceRecorder.svelte` - Recording UI with MediaRecorder/Tauri backend integration
- `VoicePlayer.svelte` - Audio playback with play/pause, seek, volume, speed control
- `ChatPanel.svelte` - Renders voice attachments using VoicePlayer

**Backend commands:**
- `send_voice_message` (WebSocket) - Send voice message via file transfer
- `VOICE_TRANSCRIPTION` (DPTP) - Share transcriptions with peers

The Telegram bridge (voice transcription, two-way messaging, whitelist) is in
[TELEGRAM_SETUP.md](TELEGRAM_SETUP.md): `managers/telegram_manager.py`,
`coordinators/telegram_coordinator.py`, `message_handlers/telegram_handler.py`.

---

## Conversation history and context inclusion (Phase 7+)

*This describes an AI chat. An agent sees the personal and device context only through its
`get_dpc_context` tool, whatever the chat's checkbox says — see
[agent/DPC_AGENT_GUIDE.md](agent/DPC_AGENT_GUIDE.md).*

D-PC Messenger uses a simple approach: contexts are included in every message when the
checkbox is checked. Information is never lost and the user controls token usage directly.

**Key features:**
- **Full conversation history**: All user/assistant messages sent with every query
- **Simple context inclusion**: When checkbox checked → contexts always included; unchecked → never included
- **User control**: Checkbox directly controls token usage (no hidden optimization)
- **Visual status indicators**: "Updated" badges show when context has changed (for peer cache invalidation)
- **Hard limit enforcement**: Blocks queries at 100% context window usage

**First message in a conversation:**
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

**Subsequent messages (checkbox still checked):**
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

**When contexts are included:**
- **Checkbox checked**: Contexts included in EVERY message (personal, device, peer)
- **Checkbox unchecked**: Contexts never included (privacy/token-saving mode)
- **Simple rule**: Checkbox state = Context inclusion (no hidden optimization)

**What gets stored in conversation history:**
- Only USER messages (prompts) and ASSISTANT messages (AI responses)
- Context data is NOT stored: it is sent as a separate "CONTEXTUAL DATA" section, controlled by the checkbox
- Context is ephemeral: each message includes context fresh based on the current checkbox state
- Why this design:
  - Gives users per-message control over token usage (context can be large)
  - Allows privacy control (turn off context for sensitive questions)
  - Context data can change over time, so it's always sent fresh rather than cached
  - Conversation history remains lightweight (only prompts and responses)

**Example:**
- Message 1 (context ON): Sends context + conversation history → AI gets full context
- Message 2 (context OFF): Sends only conversation history → AI only knows what was said before, not your personal data
- Message 3 (context ON again): Sends context + conversation history → AI gets fresh context again

So if context is on for one message and off for the next, the AI "forgets" the personal
context details but still remembers the conversation flow.

**Backend implementation:**
- **ConversationMonitor** tracks:
  - `message_history` - full conversation (user/assistant messages)
  - `peer_context_hashes` - per-peer context tracking (for cache invalidation)
- **CoreService** methods:
  - `_compute_context_hash()` - SHA256 hashing (for UI badges and peer cache)
  - `_assemble_final_prompt()` - builds prompt with context (if checkbox checked) + history
  - `reset_conversation()` - clears history for "New Chat" button

**Frontend implementation:**
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
- `personal_context_updated` - broadcast when the user saves personal context (includes the new hash for peer cache invalidation)
- `peer_context_updated` - broadcast when a peer context change is detected (includes node_id, hash)

**Commands:**
- `reset_conversation(conversation_id)` - clears history

**Rationale for the simple approach:**
- Modern AI models have large context windows (128K+ tokens)
- 10-message conversation with contexts always included: ~160KB (still << 128K limit)
- Simple checkbox control is more predictable and reliable than automatic optimization
- Fixes the edge case where context info is "lost" if the AI doesn't mention it in a response
- The user can uncheck the checkbox anytime to save tokens in long conversations

---

## Key architectural patterns

**Async/await throughout.** All I/O operations use asyncio:
- Background tasks tracked in task sets
- Graceful shutdown with signal handlers (Unix) or KeyboardInterrupt (Windows)
- Connection pooling for database (asyncpg)

**Service-oriented backend.** CoreService orchestrates independent components via
coordinators and managers.
- Coordinators (high-level): InferenceOrchestrator - AI query execution and prompt assembly;
  ContextCoordinator - context sharing with firewall integration; P2PCoordinator - P2P
  connection lifecycle and messaging
- Managers (low-level): P2PManager - socket-level connection management; HubClient - Federation
  Hub communication; LLMManager - AI provider API integration; ContextFirewall - access control
  rules engine; LocalApiServer - WebSocket API for UI
- Pattern: coordinators provide clean APIs for high-level operations and delegate to managers
  for the low-level implementation — easier testing, separation of concerns, cleaner extension
  points.

**Event-driven UI communication.** The UI connects via WebSocket (localhost:9999) to send
commands to the backend and receive real-time events (new messages, connection status).

**Security-first design:**
- Private keys never leave the client device
- The Hub never sees message contents or personal contexts
- TLS 1.2+ for all connections
- Certificate validation required
- JWT tokens with short expiration and blacklist support

---

## Cross-platform compatibility

- **Windows:** Signal handlers not supported, uses KeyboardInterrupt
- **Unix/Linux:** Proper SIGINT/SIGTERM handling
- Use `pathlib.Path` for file paths

**Before suggesting a library, check it on each platform** (this was the "Context7 plugin
integration" section of `CLAUDE.md`: an agent suggesting a library fetches its current docs,
for example through the Context7 plugin, and checks platform support first):
- **Windows**: WebView2 (Edge), MSVC compatibility, WASAPI for audio
- **Linux**: WebKitGTK limitations, ALSA/PipeWire for audio
- **macOS**: WKWebView limitations, Core Audio for audio, Metal for GPU
- **Tauri constraints**: No `getUserMedia` on macOS WKWebView

Example: "How do I add video recording?" → detect Tauri 2.x → suggest a Tauri plugin → fetch
its current API docs → give examples from them. Platform notes: [MACOS_BUILD.md](MACOS_BUILD.md),
[LINUX_WEBRTC_LIMITATIONS.md](LINUX_WEBRTC_LIMITATIONS.md).

---

## Technology stack

**Backend**
- Python 3.12+ with uv
- FastAPI (Hub), WebSockets (Client)
- aiortc (WebRTC implementation)
- SQLAlchemy + asyncpg + Alembic (Hub database)
- cryptography library (PKI)
- ollama, openai, anthropic (AI SDKs; Z.AI goes through the openai SDK, and `zai-sdk` is no longer a dependency)
- transformers, torch, librosa, soundfile (Whisper transcription; the optional `voice-memory` extra)
- python-telegram-bot 21.x (Telegram integration)

**Frontend**
- SvelteKit 5.0 (Svelte 5 with runes)
- Tauri 2.x (Rust)
- TypeScript 5.6
- Vite 6.0
- adapter-static (SPA mode)
- **Testing**: Vitest (unit tests), Playwright (E2E tests - in development)
- **Audio**: cpal (Rust) for cross-platform recording

**Infrastructure**
- Docker + Docker Compose
- PostgreSQL
- Nginx (production)

---

## File locations

- Client backend entry point: `dpc-client/core/run_service.py` (starts CoreService)
- Frontend entry point: `dpc-client/ui/src/routes/+page.svelte`; Tauri: `dpc-client/ui/src-tauri/src/main.rs`
- Hub entry point: `dpc-hub/dpc_hub/main.py`
- Build outputs: `dpc-client/ui/src-tauri/target/release/dpc-messenger.exe` (Windows),
  installers in `dpc-client/ui/src-tauri/target/release/bundle/`
- Configuration files under `~/.dpc/` (`config.ini`, `providers.json`, `privacy_rules.json`,
  `personal.json`, `device_context.json`, `node.{key,crt,id}`, received files in
  `conversations/{peer_id}/files/`): [QUICK_START.md — What's in .dpc](../QUICK_START.md#whats-in-dpc)
  and [CONFIGURATION.md](CONFIGURATION.md)
