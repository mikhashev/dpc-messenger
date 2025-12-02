# Changelog

All notable changes to D-PC Messenger will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

---

## [0.9.1] - 2025-12-02

### Fixed
- **Windows dual-stack binding** - Fixed `TypeError: a coroutine was expected, got <_GatheringFuture pending>`
  - Removed incorrect `asyncio.create_task()` wrapper around `asyncio.gather()`
  - Windows dual-stack listeners now start without errors
  - Service functions correctly despite previous error messages

---

## [0.9.0] - 2025-12-02

### Changed
- **Logging migration complete** - All print statements converted to Python standard library logging
  - run_service.py startup/shutdown messages now use logger
  - Version information added to logs for debugging
- **Centralized version management** - Single-source version tracking
  - Created VERSION file in client and Hub
  - Version auto-imported from VERSION file (no hardcoded versions)
  - Startup logs show "D-PC Messenger v0.9.0 initializing..."

### Added

**Phase 1: Peer Connection Diagnostics and Reliability**

- **Network-Resilient STUN Discovery** - STUN now works reliably even when service starts before network is ready
  - Internet connectivity checks before STUN attempts (8.8.8.8, 1.1.1.1, OpenDNS)
  - Retry logic with exponential backoff (0s, +5s, +15s)
  - Periodic re-discovery every 5 minutes to detect IP changes
  - Fully async implementation (no blocking sockets)
  - IPv4 link-local address filtering (169.254.x.x APIPA addresses excluded)
  - Fixes root cause of "STUN: Error during binding request [Errno 11001] getaddrinfo failed"

- **Enhanced Connection Diagnostics** - Actionable error messages for troubleshooting connection failures
  - **WebRTC diagnostics**: Analyzes ICE candidate types (host/srflx/relay)
    - Provides specific failure reasons (STUN failed, TURN not configured, NAT traversal stuck)
    - Includes prioritized recommendations for troubleshooting
    - Example: "❌ STUN failed - No server reflexive candidates found. Check internet connectivity."
  - **Direct TLS pre-flight checks**: Tests port connectivity before SSL handshake (5s timeout)
    - Catches connection errors early (before cryptic WinError 121)
    - Clear error messages about port accessibility
    - Separates pre-flight timeout from full connection timeout

- **Windows Dual-Stack Binding Fix** - Fixed Direct TLS server only listening on IPv6 on Windows
  - Root cause: Windows binding to [::] only listens on IPv6 (doesn't accept IPv4-mapped addresses)
  - Platform detection: Separate IPv4/IPv6 listeners on Windows, single [::] listener on Linux/macOS
  - Result: Both 0.0.0.0:8888 and [::]:8888 now active on Windows
  - Tested: Windows ↔ Ubuntu Direct TLS connection verified

### Added
- **Comprehensive DPTP protocol specification** - Formal documentation at `specs/dptp_v1.md`
  - Message format, types, and wire protocol details
  - Node identity system specification (RSA-2048, X.509 certificates)
  - Connection flows for Direct TLS and WebRTC
  - Security considerations and implementation guidelines
  - Complete reference for protocol implementers

- **Protocol library documentation** - Comprehensive README at `dpc-protocol/README.md`
  - Full API reference for crypto, protocol, and PCM modules
  - Installation and usage examples with code snippets
  - Testing guide and development instructions
  - Links to formal DPTP specification

### Changed
- **README.md cleanup** - Removed all emojis for more professional presentation
  - Updated footer with: "A small step for AI, a giant leap for all humanity"
  - Added DPTP specification link in "For Developers" section
  - Cleaner, more accessible documentation

- **CLAUDE.md updates** - Added references to new documentation
  - Link to DPTP specification (`specs/dptp_v1.md`)
  - Link to protocol library documentation (`dpc-protocol/README.md`)
  - Updated Important Documentation section

### Added
- **IPv6 support for direct P2P connections** - Full dual-stack (IPv4 + IPv6) connectivity
  - Dual-stack server binding: Listens on both IPv4 and IPv6 simultaneously
  - Configuration: Set `listen_host = dual` (default), `0.0.0.0` (IPv4 only), or `::` (IPv6 only) in `~/.dpc/config.ini`
  - Automatic IPv6 address detection (local and global addresses)
  - IPv6 URI support with bracket notation: `dpc://[2001:db8::1]:8888?node_id=...`
  - Teredo IPv6 support: Global Teredo addresses (`2001:0::/32`) automatically promoted to External section
  - Enables NAT-free connections for peers with native IPv6 or Teredo tunneling
  - Cross-platform IPv6 detection: Socket method, hostname resolution, and platform-specific commands
  - Smart filtering: Excludes link-local (`fe80::/10`) and ULA (`fc00::/7`) from external addresses

- **Connection timeout and diagnostics** - Better error handling for failed connections
  - Configurable connection timeout (default: 30 seconds for full connection, 10 seconds for port test)
  - New `test_port_connectivity()` method for pre-flight port accessibility checks
  - Exposed via WebSocket API as `test_port` command for UI integration
  - Fast failure detection instead of relying on OS default timeouts

- **External IP connection support in Connect to Peer field** - Single input field now supports all connection methods
  - Existing `dpc://` URI format works with external IPs: `dpc://203.0.113.5:8888?node_id=dpc-node-abc...`
  - Added connection help with examples for all three methods (WebRTC, Local Network, External IP)
  - Enables secure TLS connections when peers share external IPs via trusted channels (email, Signal, etc.)
  - Requires port forwarding on peer's router (default port 8888)
  - No Hub dependency for external IP connections - completely peer-to-peer with X.509 certificate validation
  - **Note:** Feature not yet tested with external IPs across internet

- **External IP discovery via STUN servers** - Your public IP address now displays automatically in the UI
  - New standalone STUN discovery module (`stun_discovery.py`) for detecting external IP without WebRTC connections
  - Background task runs on startup and refreshes every 5 minutes
  - External URIs displayed in new "External (Internet)" section with green badges
  - Discovers public IP from STUN servers for sharing with remote peers via email/messaging
  - Works immediately on startup - no WebRTC connection required
  - Automatic IP change detection and UI updates

- **All STUN/TURN servers now configurable in config.ini** - No hardcoded servers in production code
  - STUN servers configurable in `[webrtc] stun_servers` in `~/.dpc/config.ini`
  - TURN servers configurable in `[turn] servers` (with credentials) and `fallback_servers` (public)
  - Users can customize servers for regional availability and network conditions
  - Environment variable overrides: `DPC_WEBRTC_STUN_SERVERS`, `DPC_TURN_SERVERS`
  - Maximum flexibility for international users facing regional server blocks

- **Configurable max_tokens for AI providers** - Customize response length per provider
  - Anthropic providers now read `max_tokens` from `providers.json` configuration
  - Defaults to 4096 tokens if not specified
  - Set different limits for different providers (e.g., haiku=2048, sonnet=8192, opus=16384)
  - Omit or set to `null` to use model's maximum token limit
  - No more truncated AI responses with Anthropic Claude

### Changed
- **Context-aware AI system instructions** - AI now adapts instructions based on whether personal context is enabled
  - When context disabled: AI receives "Answer based on conversation history and general knowledge" instead of expecting `<CONTEXT>` tags
  - Eliminates confusing "I don't see any JSON data blobs in `<CONTEXT>` tags" responses
  - Cleaner user experience for context-free queries while preserving conversation history
  - Backend: `_build_bias_aware_system_instruction()` now accepts `include_full_context` parameter

- **Message ID format updated for group chat support** - Message IDs now include node ID prefix for global uniqueness
  - New format: `{node_id_short}-{command_id}-{user|ai}` (e.g., `46f34940-abc123-user`)
  - Enables tracking individual participants in future multi-user group chats
  - Prevents message ID collisions across different users and AI instances
  - Maintains backward compatibility with existing code

- **STUN discovery architecture** - Dual-source external IP detection
  - Priority: Standalone STUN discovery (always available) → WebRTC ICE candidates (when active)
  - Avoids duplicate IPs from multiple sources
  - Faster initial discovery (5 seconds vs waiting for WebRTC connection)
  - `P2PManager.get_external_ips()` aggregates from both WebRTC peers and standalone discovery

- **Connection timeout increased** - Direct TLS connection timeout extended from 10 to 30 seconds
  - Accommodates slower networks and long-distance connections
  - Port test timeout: 10 seconds (unchanged)
  - Prevents premature connection failures on high-latency networks

- **Improved error messages for connection failures** - More actionable diagnostics for users
  - Timeout errors now explain port forwarding requirements for external IP connections
  - ConnectionRefusedError provides firewall and service status guidance
  - Recommends WebRTC for NAT/firewall scenarios (no port forwarding needed)
  - Error messages distinguish between network issues, firewall blocks, and service unavailability

### Fixed
- **Knowledge extraction routing to wrong AI provider in multi-chat scenarios** - Critical bug where "End Session" button used wrong AI model
  - Root cause: Hardcoded `"local_ai"` conversation_id caused all AI chats to share same message buffer and provider settings
  - When Chat 1 (claude_haiku) and Chat 2 (ollama_local) were both active, Chat 2's provider settings overwrote Chat 1's
  - Clicking "End Session" in Chat 1 would incorrectly use ollama_local for knowledge extraction instead of claude_haiku
  - Fixed: Use actual `conversation_id` parameter instead of hardcoded string in 3 locations (monitor creation, user message, AI message)
  - Impact: Each AI chat now maintains independent message buffers and provider settings

- **Hardcoded user identity in knowledge commits** - User attribution was generic instead of using actual node ID and display name
  - Root cause: `sender_node_id="user"` and `sender_name="User"` hardcoded in ConvMessage creation
  - Knowledge commits showed generic "User (user)" instead of real identity like "Mike Windows 10 (dpc-node-e07fb59e46f34940)"
  - Fixed: Use `self.p2p_manager.node_id` and `self.p2p_manager.get_display_name()`
  - Impact: Proper attribution in knowledge commits, enables future multi-device support and knowledge sharing

- **Teredo IPv6 addresses not appearing in External section** - Global Teredo addresses were incorrectly classified as private
  - Root cause: Python's `ipaddress` module incorrectly marks Teredo (`2001:0::/32`) as `is_private=True`
  - Impact: Teredo addresses only showed in Local Network section instead of External (Internet)
  - Fixed: Added explicit Teredo network check in `_is_global_ipv6()` before relying on `is_global` flag
  - Global Teredo addresses now correctly promoted to External section for internet-wide connectivity
  - Enables NAT-free P2P connections via Teredo tunneling (IPv6-over-IPv4)

- **Settings validation** - STUN servers return empty list (not hardcoded defaults) if config missing
  - Clear warning message: "No STUN servers configured in config.ini"
  - Forces users to configure servers explicitly for their region

## [0.8.0] - 2025-11-28

### Added
- **Markdown rendering for AI responses** - AI messages now display with rich markdown formatting
  - Integrated `@humanspeak/svelte-markdown` (v0.8.13) with Svelte 5 runes support
  - GitHub-style formatting for headers, code blocks, lists, tables, blockquotes, and links
  - Built-in XSS protection - renders without `@html`, making it secure for LLM-generated content
  - Dynamic toggle button in chat header to switch between markdown and plain text views
  - User preference persists across sessions via localStorage (default: enabled)
  - 50-200x performance improvement with intelligent token caching for repeated content
  - Created reusable `MarkdownMessage` component for consistent styling
- **Remote inference for knowledge detection** - Knowledge auto-detection now supports remote inference for users without local LLMs
  - ConversationMonitor accepts `ai_query_func` callback for flexible inference routing
  - Automatically uses selected compute host/model for background knowledge analysis
  - Falls back to local LLM if no ai_query_func provided (backward compatible)
  - Enables knowledge detection for users without Ollama or other local LLMs installed

### Fixed
- **Manual knowledge extraction with auto-detection disabled** - Fixed "End Session" button not extracting knowledge when auto-detection toggle is OFF
  - Root cause: Messages were only buffered when auto-detection was enabled, leaving manual extraction with empty buffer
  - Added `auto_detect` parameter to ConversationMonitor to separate buffering from auto-proposal generation
  - Updated service.py to always buffer messages regardless of auto-detection setting
  - Auto-detection now only controls automatic proposal generation, not message buffering
  - Runtime toggle updates all existing conversation monitors' auto_detect flags
  - UI toggle now waits for backend confirmation with error handling and state rollback
  - Users can now disable auto-detection to avoid interruptions, then manually extract knowledge at end of session
- **Privacy leak in local AI conversations using remote inference** - Fixed knowledge from private AI chats being broadcasted to compute hosts
  - Root cause: Knowledge commit proposals were broadcasted to ALL connected peers, including compute hosts used only for remote inference
  - Local AI conversations are now truly private - knowledge proposals no longer sent to peers
  - Peer chats still broadcast knowledge proposals for collaborative consensus (unchanged)
  - Context update notifications (`CONTEXT_UPDATED`) still broadcasted to all peers so "Updated" badges work correctly
  - Fixed in both manual extraction (End Session) and automatic detection code paths
  - Impact: When using remote inference in Local AI Assistant, the compute host only executes queries and doesn't receive your private knowledge
- **Knowledge commit P2P protocol** - Fixed missing handlers for `PROPOSE_KNOWLEDGE_COMMIT` and `VOTE_KNOWLEDGE_COMMIT` messages
  - Added `handle_proposal_message()` and `handle_vote_message()` to ConsensusManager
  - Peers can now receive and process knowledge commit proposals and votes
  - Eliminates "Unknown P2P message command" errors during collaborative knowledge commits
- **Stale context after commit** - Fixed issue where peers couldn't see newly committed knowledge
  - Added `on_commit_applied` callback to reload `p2p_manager.local_context` after commit
  - Context requests now return latest knowledge immediately after commit
  - Automatically broadcasts `CONTEXT_UPDATED` to all peers after applying commit
  - Peers' cached contexts are invalidated, forcing fresh fetch on next query
- **Auto-detection toggle sync** - Fixed backend/frontend state mismatch where auto-detection would run even when UI toggle was OFF
  - Changed backend default from `True` to `False` to match UI default
  - Prevents unwanted knowledge proposals when user has explicitly disabled auto-detection
- **Knowledge commit hash verification** - Fixed false-positive "hash mismatch" warnings for all knowledge commits
  - Root cause #1: Content hash computed without topic title during commit creation, but with title during verification
  - Root cause #2: Windows CRLF line endings in files but LF used for hash computation, causing mismatch
  - Fixed `markdown_manager.py`: Strip topic title from parsed content before hashing
  - Fixed `markdown_manager.py`: Force LF line endings when writing files for cross-platform consistency
  - Fixed `markdown_manager.py`: Preserve trailing whitespace when parsing (changed `.strip()` to `.lstrip()`)
  - Fixed `commit_integrity.py`: Applied same fixes to duplicate `parse_markdown_with_frontmatter()` function
  - Impact: All knowledge commits now verify correctly on startup with no false warnings
- **P2PManager broadcast error** - Fixed `AttributeError: 'P2PManager' object has no attribute 'send_to_peer'` by using correct method name `send_message_to_peer()`
- **Unused CSS selector warning** - Removed unused `.link-btn` selector from `ContextViewer.svelte`

### Added

**Personal Context Schema v2.0 (Modular File System)**

#### Core Features
- **Minimal personal.json** - Profile + metadata only (~3-5 KB instead of 26 KB)
  - Removed legacy `instruction` field (now in `instructions.json`)
  - Removed embedded `entries` arrays (now in markdown files)
  - Added `metadata.external_files` reference system
  - Format version upgraded to `2.0`

- **Automatic Schema Migration** (`_cleanup_schema_v2`)
  - One-time migration on service startup
  - Idempotent operation (safe to run multiple times)
  - Four-step process:
    1. Fix instruction field (calls `migrate_instructions_from_personal_context`)
    2. Export knowledge to versioned markdown files
    3. Clear entries arrays from JSON
    4. Update format_version to 2.0
  - Creates `.json.v1_backup` before migration
  - Logs file size reduction (typically 80-85%)

- **Knowledge Loading from Markdown**
  - `get_personal_context()` loads entries from markdown files on demand
  - Verifies content integrity using SHA256 hashes
  - Warnings logged for hash mismatches
  - In-memory only (entries not persisted to JSON)

- **Fixed Instruction Migration Bug**
  - `migrate_instructions_from_personal_context()` now idempotent
  - Properly cleans up `instruction` field even when `instructions.json` exists
  - Adds `metadata.external_files.instructions` reference
  - Creates backup before modifying files

#### Backend Implementation

**Updated** - `dpc-protocol/dpc_protocol/pcm_core.py`:
- Fixed `migrate_instructions_from_personal_context()` (lines 435-561)
  - Now handles CASE 1 (instructions.json exists) correctly
  - Removes instruction field and adds external_files reference
  - Creates backups before modification
  - Returns True only if changes were made
- **Made Topic optional fields truly optional:**
  - Changed `key_books`, `preferred_authors`, `learning_strategies` to `Optional[List]` (default: None)
  - Core fields: `summary`, `entries`, `mastery_level`, `version`, timestamps, `markdown_file`, `commit_id`
  - Optional fields only included in JSON when user adds them
  - Updated `from_dict()` to only populate optional fields when present

**Updated** - `dpc-protocol/dpc_protocol/markdown_manager.py`:
- Added `build_markdown_with_frontmatter()` - builds markdown with frontmatter, returns string
- Added `parse_markdown_with_frontmatter()` - parses YAML frontmatter from markdown files
- Added `markdown_to_entries()` - converts markdown content back to KnowledgeEntry objects
- Refactored `write_markdown_with_frontmatter()` to use new build method
- All methods handle optional Topic fields gracefully (only render when not None)

**Updated** - `dpc-client/core/dpc_client_core/service.py`:
- Added `_cleanup_schema_v2()` method (lines 186-357)
  - Automatic one-time migration on startup
  - Exports all knowledge topics to versioned markdown files
  - Preserves commit integrity data (hashes, signatures)
  - Logs detailed progress and results
- Updated `get_personal_context()` (lines 1220-1270)
  - Loads knowledge from markdown files when `markdown_file` is set
  - Verifies content hashes for integrity
  - Populates `entries` in-memory for UI display
- Integrated cleanup into `start()` method (lines 424-429)
  - Runs before integrity check
  - Errors don't prevent startup

**Updated** - `dpc-client/ui/src/lib/components/ContextViewer.svelte`:
- Removed unused "View Markdown" button (was dispatching unhandled event)
- Removed `openMarkdownFile()` helper function
- Cleaner topic metadata display

#### File Structure Changes

**Before v2.0:**
```
~/.dpc/
├── personal.json (26 KB)
│   └── Contains:
│       - instruction field ❌
│       - huge entries arrays ❌
│
└── instructions.json (1 KB)
```

**After v2.0:**
```
~/.dpc/
├── personal.json (3-5 KB)
│   └── Profile + metadata only ✅
│
├── personal.json.v1_backup (26 KB)
├── instructions.json (1 KB)
│
└── knowledge/
    ├── astronomy_commit-a05808d8.md
    ├── astronomy_commit-def12345.md (newer version)
    └── alice_collaborative_ai_commit-2153be27.md
```

#### personal.json v2.0 Structure

```json
{
  "profile": {
    "name": "Mike Windows 10",
    "description": "My personal context for D-PC.",
    "core_values": ["Windows"]
  },

  "knowledge": {
    "astronomy": {
      "summary": "The Andromeda Galaxy...",
      "markdown_file": "knowledge/astronomy_commit-def12345.md",
      "commit_id": "commit-def12345",
      "mastery_level": "beginner",
      "version": 2,
      "entries": []  // Empty - loaded from markdown
    }
  },

  "metadata": {
    "format_version": "2.0",
    "external_files": {
      "instructions": {
        "file": "instructions.json",
        "description": "AI behavior instructions",
        "last_updated": "2025-11-26T..."
      }
    }
  }
}
```

#### Benefits
- **80-85% file size reduction** - personal.json shrinks from 26 KB to 3-5 KB
- **Git-friendly** - Markdown files are human-readable and diff-friendly
- **Modular** - Each topic version has its own file
- **Integrity** - Content hashes detect manual edits
- **Backup safety** - Original files backed up before migration

#### Multi-Device Sync
- Recommended method: Backup/Restore workflow
- Hash-based commit IDs ensure consistency
- All devices have identical v2.0 schema after sync

#### Documentation
- Added `docs/PERSONAL_CONTEXT_V2_IMPLEMENTATION.md` - comprehensive implementation guide
  - Prerequisites and architecture
  - Minimal schema design
  - Migration workflow
  - Testing plan
  - Multi-device considerations
- Added `docs/MANUAL_KNOWLEDGE_SAVE.md` - implementation guide for manual knowledge save feature
  - User flows (solo and collaborative modes)
  - Backend command handler design
  - Frontend dialog component specification
  - Consensus integration
  - Testing checklist

**Cryptographic Commit Integrity System (Phase 8)**

#### Core Features
- **Hash-Based Commit IDs** (Git-style content-addressable storage)
  - Commit ID = `commit-{SHA256_hash[:16]}` (e.g., `commit-a3f7b2c91d4e5f6a`)
  - Same content = same hash across all devices (deterministic)
  - 64-character SHA256 hash stored in `commit_hash` field
  - Replaces random UUID-based commit IDs

- **Multi-Signature Support**
  - RSA-PSS signatures with SHA256 for provably secure commits
  - Uses existing node RSA keys (~/.dpc/node.key)
  - All participants sign collaborative commits
  - Base64-encoded signatures stored in frontmatter
  - Automatic signing on commit creation

- **Versioned Markdown Storage with Frontmatter**
  - Format: `{topic_name}_{commit_id}.md`
  - YAML frontmatter with cryptographic verification fields:
    - `commit_hash` - Full SHA256 hash (verifies metadata integrity)
    - `content_hash` - Markdown content hash (detects manual tampering)
    - `signatures` - Multi-party cryptographic signatures (node_id -> base64 signature)
    - `parent_commit` - Git-style commit chain reference
    - `participants`, `approved_by`, `rejected_by` - Consensus tracking
    - `cultural_perspectives` - Bias mitigation metadata
    - `confidence_score` - AI confidence level
  - Separate content hash detects manual markdown edits

- **Startup Integrity Verification**
  - Automatic verification of all commits on service startup
  - 5 integrity checks per commit:
    1. Content hash matches actual markdown content
    2. Commit ID in filename matches frontmatter
    3. Recomputed commit hash matches stored `commit_hash`
    4. All signatures are valid (RSA verification)
    5. Parent commit exists (chain integrity)
  - Warnings broadcasted to UI via `integrity_warnings` event
  - Console output: `✓ Knowledge integrity verified (N commits)`

- **Chain of Trust** (Git-style commit history)
  - Each commit references parent commit via `parent_commit_id`
  - Linear commit history: `commit-1 → commit-2 → commit-3`
  - Tamper-evident: changing any commit breaks all descendants
  - `verify_commit_chain()` validates entire history

#### Backend Implementation

**New Module** - `dpc-protocol/dpc_protocol/commit_integrity.py`:
- `compute_commit_hash(commit)` - deterministic SHA256 hash computation
  - Includes: topic, summary, entries, participants, approved_by, timestamps
  - Excludes: conversation_id, commit_id (circular), signatures (added after hash)
  - Canonical JSON with sorted keys for determinism
- `verify_commit_hash(commit)` - recompute and verify hash
- `CommitSigner` class:
  - `sign_commit(commit_hash)` - RSA-PSS signature creation
  - `verify_signature(node_id, commit_hash, signature)` - RSA verification with peer certificates
- `parse_markdown_with_frontmatter(path)` - parse YAML frontmatter
- `compute_content_hash(content)` - SHA256 hash of markdown content
- `verify_markdown_integrity(path)` - comprehensive integrity check
- `verify_commit_chain(commits)` - validate commit chain
- Custom exceptions: `IntegrityError`, `SignatureError`, `AuthorizationError`, `ChainIntegrityError`

**Updated** - `dpc-protocol/dpc_protocol/knowledge_commit.py`:
- Added fields to `KnowledgeCommit`:
  - `commit_hash: Optional[str]` - Full SHA256 hash (64 chars)
  - `signatures: Dict[str, str]` - node_id → base64 signature mapping
- New methods:
  - `compute_hash()` - computes hash and sets `commit_id` and `commit_hash`
  - `sign(node_id, private_key)` - signs commit with RSA key
  - `verify_signatures()` - verifies all signatures in commit
  - `verify_hash()` - verifies commit hash matches content

**Updated** - `dpc-protocol/dpc_protocol/markdown_manager.py`:
- Added `topic_to_markdown_content(topic)` - generate content without frontmatter (for hashing)
- Added `write_markdown_with_frontmatter(filepath, frontmatter, content)`:
  - Writes YAML frontmatter with structured sections:
    - Commit Identification
    - Integrity Verification
    - Metadata
    - Consensus Tracking
    - Cryptographic Signatures
    - Cultural Context
  - Properly escapes signature strings in YAML

**Updated** - `dpc-client/core/dpc_client_core/consensus_manager.py`:
- Updated `_apply_commit()` to use hash-based commit IDs:
  1. Set parent commit ID from context (chain of trust)
  2. Compute hash-based commit ID (`commit.compute_hash()`)
  3. Sign commit with node's private RSA key
  4. Create versioned markdown file with frontmatter
  5. Compute content hash of markdown
  6. Write frontmatter with all cryptographic fields
  7. Update commit history with `commit_hash` and `signatures`
- Uses `markdown_manager.sanitize_filename()` for cross-platform compatibility

**Updated** - `dpc-client/core/dpc_client_core/service.py`:
- Added `_startup_integrity_check()` method:
  - Scans `~/.dpc/knowledge/` for `*_commit-*.md` files
  - Calls `verify_markdown_integrity()` on each file
  - Collects warnings for invalid commits
  - Broadcasts `integrity_warnings` event to UI
  - Prints summary to console
- Integrated into `start()` method (runs before starting servers)

#### Testing
- **Unit Tests** - `dpc-protocol/tests/test_commit_integrity.py`:
  - 22 tests, all passing ✓
  - Test coverage:
    - Hash computation (deterministic, tamper-evident, excludes volatile fields)
    - Signature creation and verification
    - Commit chain validation (valid chains, broken chains, missing parents)
    - Content hash computation
    - Filename and commit ID extraction
    - Full workflow integration (create, hash, sign, verify)
    - Tampering detection
    - Serialization with integrity fields

#### Security Properties
- **Collision-resistant**: SHA256 has 2^256 possible values (astronomically unlikely collision)
- **Tamper-evident**: Any modification to content changes the hash
- **Non-repudiable**: Cryptographic signatures prove agreement (can't deny participation)
- **Verifiable**: Anyone can recompute hashes and verify signatures independently
- **Chain integrity**: Git-style parent references prevent history rewriting

#### Threat Model
**Protected Against:**
- ✅ Accidental tampering (user manually edits markdown)
- ✅ Intentional tampering (malicious modification of commits)
- ✅ Repudiation ("I never agreed to that" - signatures prove it)
- ✅ Commit forgery (creating fake commits requires valid hash + signature)

**Not Protected Against:**
- ❌ Private key theft (if attacker gets `~/.dpc/node.key`, they can sign as you)
- ❌ Denial of Service (attacker can delete local markdown files)
- ❌ Man-in-the-Middle (mitigated by TLS, assumes TLS works correctly)

#### File Structure
```
~/.dpc/knowledge/
├── astronomy_commit-a3f7b2c91d4e5f6a.md  (versioned markdown)
├── astronomy_commit-b8c9d0e1f2a3b4c5.md  (newer version)
└── game_design_commit-c1d2e3f4a5b6c7d8.md
```

#### Example Markdown Output
```markdown
---
# Commit Identification
topic: astronomy
commit_id: commit-a3f7b2c91d4e5f6a
commit_hash: a3f7b2c91d4e5f6a8b9c0d1e2f3g4h5i...
parent_commit: commit-b8c9d0e1f2a3b4c5

# Integrity Verification
content_hash: f9e8d7c6b5a49382

# Cryptographic Signatures
signatures:
  dpc-node-alice-123: "MEUCIQDXvK...=="
  dpc-node-bob-456: "MEYCIQCqwL...=="
---

# Astronomy
**Summary:** The Andromeda Galaxy is approximately 2.5 million light-years away...
```

#### Documentation
- Added `docs/COMMIT_INTEGRITY_IMPLEMENTATION.md` - comprehensive implementation guide
  - Architecture overview
  - Hash computation algorithm
  - Signature workflow
  - Verification system
  - Testing plan
  - Security considerations
  - FAQ

**Conversation History & Context Optimization (Phase 7)**

#### Core Features
- **Full Conversation History Support**
  - AI now receives complete conversation history with every query (user/assistant messages)
  - Enables conversational continuity - AI can reference previous exchanges ("as I mentioned before...")
  - History stored per conversation tab with independent tracking
  - Messages formatted as `USER:` and `ASSISTANT:` in structured history section

- **Smart Context Optimization** (60-80% token savings)
  - Personal context (`personal.json` + `device_context.json`) sent **only when needed**:
    - First message in conversation
    - When user toggles "Include Personal Context" checkbox
    - When context files are modified (hash-based detection)
  - Subsequent messages use conversation history only (no context re-sending)
  - Peer contexts sent only on first collaborative message or when peer updates context

- **Hash-Based Change Detection**
  - SHA256 hashing of `personal.json` + `device_context.json` for automatic change detection
  - Per-peer context hash tracking for collaborative queries
  - Backend computes and broadcasts context hashes on save
  - Frontend tracks `currentContextHash` vs `lastSentContextHash` per conversation

- **Visual Status Indicators** ("Updated" Badges)
  - Green pulsing "UPDATED" badge on "📚 Include Personal Context" toggle when context modified
  - Green "UPDATED" badges on peer context checkboxes when peer contexts change
  - Badges automatically clear when context successfully sent to AI
  - CSS animations with subtle pulse effect (fades 80%-100% opacity)

- **Hard Context Window Limit Enforcement**
  - Backend blocks queries at 100% context window usage (raises `RuntimeError`)
  - Frontend disables textarea and send button at 100%
  - Placeholder text changes to: "Context window full - End session to continue"
  - Prevents overflow errors and guides users to knowledge extraction

- **"New Chat" Reset Functionality**
  - Backend command: `reset_conversation(conversation_id)`
  - Clears conversation history, context tracking, peer context hashes
  - Resets token counter to 0
  - Next message after reset includes full context again
  - Frontend clears `lastSentContextHash` and `lastSentPeerHashes` for conversation

#### Backend Implementation

**ConversationMonitor** (`conversation_monitor.py`):
- Added `message_history: List[Dict[str, str]]` - stores `{"role": "user/assistant", "content": "..."}`
- Added `context_included: bool` - flag for first-time context inclusion
- Added `context_hash: str` - SHA256 hash of last sent context
- Added `peer_context_hashes: Dict[str, str]` - per-peer hash tracking
- New methods:
  - `add_message(role, content)` - append to history
  - `get_message_history()` - retrieve full history
  - `mark_context_included(hash)` - set context sent flag
  - `has_context_changed(new_hash)` - compare hashes
  - `update_peer_context_hash(node_id, hash)` - track peer changes
  - `has_peer_context_changed(node_id, new_hash)` - check peer updates
  - `reset_conversation()` - clear all history/tracking for "New Chat"

**CoreService** (`service.py`):
- Added `_compute_context_hash()` - SHA256 of personal.json + device_context.json
- Added `_compute_peer_context_hash(context_obj)` - hash peer contexts
- Added `reset_conversation(conversation_id)` command handler
- Updated `execute_ai_query()`:
  - Checks context window limit early (blocks at 100%)
  - Computes context hash and determines if full context needed
  - Adds user message to history before assembling prompt
  - Adds AI response to history after receiving
  - Marks context as included with hash tracking
  - Tracks peer context hashes (re-sends only when changed)
- Updated `_assemble_final_prompt()`:
  - Accepts `message_history` and `include_full_context` parameters
  - Builds context blocks only when `include_full_context=True`
  - Adds `--- CONVERSATION HISTORY ---` section with all messages
  - Formats: `CONTEXTUAL DATA` (first message) + `CONVERSATION HISTORY` (always)
- Updated `save_personal_context()`:
  - Computes new context hash after saving
  - Broadcasts `personal_context_updated` event with hash
- Updated peer context fetching in `execute_ai_query()`:
  - Computes peer context hash
  - Compares with monitor's stored hash
  - Broadcasts `peer_context_updated` event when hash changes

#### Frontend Implementation

**coreService.ts**:
- Added stores: `contextUpdated`, `peerContextUpdated`
- Added event listeners for `personal_context_updated` and `peer_context_updated` events

**+page.svelte**:
- Added state variables:
  - `currentContextHash` - current hash from backend
  - `lastSentContextHash` - per-conversation tracking of last sent hash
  - `peerContextHashes` - per-peer current hashes from backend
  - `lastSentPeerHashes` - per-conversation, per-peer tracking
- Added reactive statements:
  - Listen for context update events, update hash maps
  - Calculate `localContextUpdated` (context changed but not sent)
  - Calculate `peerContextsUpdated` (Set of peers with changed contexts)
  - Check `isContextWindowFull` (disable UI at 100%)
- Updated `sendMessage()`:
  - Passes `conversation_id: activeChatId` to backend
  - Marks context/peer contexts as sent on successful query
- Updated `handleNewChat()`:
  - Clears `lastSentContextHash` and `lastSentPeerHashes`
  - Calls `reset_conversation` backend command
- UI updates:
  - "Updated" badges on context toggle and peer checkboxes
  - Disabled textarea/send button at 100% context usage
  - Placeholder text updates for context window full state
- CSS additions:
  - `.status-badge` and `.status-badge.updated` styles
  - `@keyframes pulse-badge` animation

#### Events & WebSocket API

**New Backend Events**:
- `personal_context_updated` - payload: `{message, context_hash}`
- `peer_context_updated` - payload: `{node_id, context_hash, conversation_id}`

**New Backend Commands**:
- `reset_conversation` - args: `{conversation_id}`, clears history and tracking

### Changed

#### Backend
- **conversation_monitor.py**
  - `__init__()` now initializes history tracking fields
  - `reset_conversation()` clears message_buffer and knowledge_score

- **service.py**
  - `execute_ai_query()` signature unchanged but behavior enhanced with history tracking
  - `_assemble_final_prompt()` signature extended with optional `message_history` and `include_full_context` parameters
  - Token counting now reflects actual current window usage (not just cumulative)

#### Frontend
- **+page.svelte**
  - Payload for `execute_ai_query` now includes `conversation_id`
  - Message handling updated to track context as sent
  - "New Chat" workflow enhanced with context tracking reset

### Performance
- **Token Savings**: ~60-80% reduction per query after first message
- **Memory**: Conversation history stored in-memory per conversation (cleared on "New Chat")
- **Network**: No additional requests - context hash included in existing events

### Developer Notes
- Context optimization is automatic - no configuration needed
- Hash computation uses Python's `hashlib.sha256()` with JSON serialization
- Frontend hash tracking is per-conversation and persists across tab switches
- "Updated" badges use CSS animations for smooth visual feedback
- Hard limit enforcement prevents context window overflow errors

### Changed
- **personal.json** - Now references external files via `metadata.external_files`
- **Knowledge storage** - Entries stored in markdown files, not JSON
- **Schema version** - Upgraded from v1.x to v2.0
- **UI cleanup** - Removed initial greeting messages from AI chats (both local and custom provider chats now start with empty history)
- **Hub login section** - Moved Hub login buttons to appear directly below Hub status in node-info card for better visual hierarchy

### Fixed (Additional)
- **Filename sanitization** - Cross-platform compatibility (removes colons and invalid characters)

---

## What's Next: Phase 2 - Team Collaboration + Disaster Resilience

**Phase 1 (Federated MVP) is now complete!** Version 0.8.0 marks the completion of the foundational infrastructure:

**Phase 1 Achievements:**
- Federated architecture with Hub-based discovery and WebRTC NAT traversal
- Direct TLS P2P connections with IPv6 dual-stack support
- AI collaboration (local/remote inference, collaborative knowledge building)
- Consensus voting system with devil's advocate mechanism
- Personal Context Model v2.0 with modular file system
- Cryptographic commit integrity (hash-based IDs, multi-signatures)
- Conversation history optimization (60-80% token savings)
- In-app configuration editors (no more manual file editing)

**Phase 2 Focus: Small Team Collaboration (2-20 Members + AIs)**

The next phase (Q1-Q3 2026) shifts focus from scaling to **deepening team collaboration features** for small teams:

**Team Collaboration Features (12 features, 6 months):**
- Group chats (3-20 participants with multi-AI support)
- Shared team knowledge base (markdown files, git-compatible)
- @-mention context retrieval (semantic search across team members)
- Async workflows (notifications, task management)
- Enhanced consensus (weighted voting, time-based approval)
- Team analytics (usage metrics, knowledge quality tracking)

**Resilient Infrastructure (7 features, CRITICAL):**
- DHT-based peer discovery (Kademlia + Gossip for decentralized signaling)
- Pluggable transport framework (auto-fallback: WebRTC → WebSocket-TLS → HTTP tunneling)
- Hub-optional bootstrap (start without Hub, discover peers via DHT)
- Offline mesh networking (WiFi Mesh, Meshtastic, Starlink)
- End-to-end encryption for all transports
- Protocol reliability enhancements for HTTP tunneling (HTTPS transport)

**Why Small Teams First?**
- Test and refine collaborative knowledge workflows at manageable scale
- Validate consensus mechanisms with 2-20 peers (not thousands)
- Build tooling for effective team communication
- Gather real-world feedback before mass-market scaling (Phase 3)

**Documentation Updates:**
- See [ROADMAP.md](ROADMAP.md) for complete Phase 2 implementation plan (15 features, 9-month timeline)
- See [docs/KNOWLEDGE_ARCHITECTURE.md](docs/KNOWLEDGE_ARCHITECTURE.md) for knowledge system roadmap integration
- Updated [README.md](README.md) roadmap section with revised Phase 1/2/3 structure

**Phase 3 (Mass-Market Scaling):**
Mobile clients, public discovery, enterprise features, and scaling to thousands of nodes will be addressed in Phase 3 (2027+).

---

## [0.7.0] - 2025-11-24

### Added - Knowledge Architecture (Phases 1-6) ⚠️ UNTESTED

#### Phase 1: Instructions.json Separation
- Separated AI instructions into standalone `instructions.json` file (Personal Context v2.0 schema)
- Added in-app **InstructionsEditor** component for editing AI instructions without manual file editing
- Personal context file now references `instructions.json` via `metadata.external_files`

#### Phase 2: Token Monitoring System
- **Token Counter Display** in chat header showing "used / limit tokens (percentage)"
  - Color-coded warning when usage exceeds 80% threshold
  - Live updates as conversation progresses
- **Toast Notification Component** (`Toast.svelte`)
  - Dismissible notifications with auto-dismiss timer
  - Type-based styling (info/warning/error)
  - Used for extraction failures and warnings
- Backend broadcasts `token_limit_warning` events when approaching context limit
- Frontend stores: `tokenWarning`, `extractionFailure`

#### Phase 3: Optional Cultural Perspectives
- Added `cultural_perspectives_enabled` configuration setting (default: `false`)
  - Location: `~/.dpc/config.ini` under `[knowledge]` section
- ConversationMonitor conditionally includes cultural perspective analysis in LLM prompts
- KnowledgeCommitDialog conditionally displays cultural perspectives section
- **Benefit**: Reduces prompt complexity and token usage when cultural analysis not needed

#### Phase 4: Robust JSON Extraction with Repair
- **JSON Repair System** (`_repair_json()` method) with 6 strategies:
  1. Remove markdown code blocks (```json ... ```)
  2. Fix trailing commas before closing braces/brackets
  3. Balance missing opening/closing braces
  4. Add missing commas between array/object elements
  5. Trim whitespace
  6. Count and fix bracket/brace mismatches
- **4-Level Fallback Parsing Strategy**:
  1. Attempt 1: Regex extract JSON object + parse
  2. Attempt 2: Regex extract + repair + parse
  3. Attempt 3: Parse entire response as JSON
  4. Attempt 4: Repair entire response + parse
- Backend broadcasts `knowledge_extraction_failed` event on extraction errors
- UI displays toast notification with error details

#### Phase 5: Inline Editing with Attribution
- Added `edited_by` (peer_id/node_id) and `edited_at` (ISO timestamp) fields to `KnowledgeEntry`
- **Full Edit Mode** in KnowledgeCommitDialog:
  - Edit/Save/Cancel button workflow
  - Textarea editing for knowledge entry content
  - Automatic attribution tracking on save
  - **Visual indicator**: Yellow "✏️ Edited" badge
  - Hover tooltip shows editor and timestamp
- Updated protocol serialization/deserialization to handle edit tracking fields
- **Use case**: Refine AI-extracted knowledge before committing

#### Phase 6: Provider-Specific Context Window Overrides
- Enhanced `get_context_window()` method with priority system:
  1. Check `context_window` field in `providers.json` (highest priority)
  2. Check hardcoded `MODEL_CONTEXT_WINDOWS` dict
  3. Fall back to default (4096 tokens)
- Updated default `providers.json` template with documentation and example
- **Use case**: Override context windows for custom models or provider-specific limits
- Example: `context_window = 131072` for custom Llama fine-tune

#### Phase 7: Configuration System Overhaul (TOML → JSON)
- **Migrated all configuration files to JSON format** for consistency:
  - `providers.toml` → `providers.json` (automatic migration with `.backup` files)
  - `.dpc_access.json` → `privacy_rules.json` (consistent naming without redundant dot prefix)
  - Backward compatibility: automatic one-time migration on service startup
- **In-app AI Providers Editor** (`ProvidersEditor.svelte`):
  - Tab-based UI (Providers List + Add Provider)
  - Edit/Save/Cancel workflow with unsaved changes detection
  - Type-specific forms (Ollama, OpenAI Compatible, Anthropic)
  - API key configuration: `api_key_env` (recommended) or `api_key` (plaintext, local only)
  - Set default provider, delete providers, context window overrides
  - Real-time validation and hot-reload (no service restart needed)
  - Accessibility fixes: dialog tabindex, form-wrapped password fields
- **Backend commands**:
  - `get_providers_config` - Load providers configuration for editor
  - `save_providers_config` - Validate and save with hot-reload
  - Added constants for all config filenames (DRY principle)
- **Comprehensive example files**:
  - `instructions.example.json` - AI instructions with field reference and examples
  - `providers.example.json` - Multi-provider setup examples with security notes
  - `privacy_rules.example.json` - Complete firewall rules guide in JSON format
  - Updated `personal_context_example.json` - Removed inline instruction block, added `metadata.external_files` reference

### Changed

#### Backend
- **conversation_monitor.py**
  - Added `settings` parameter to `__init__()`
  - Added `_repair_json()` helper method
  - Updated `_generate_commit_proposal()` to conditionally include cultural perspectives
  - Enhanced JSON parsing with multi-level fallback strategy
- **service.py**
  - Pass `settings` instance to ConversationMonitor
  - Broadcast `knowledge_extraction_failed` events on errors
  - Added `get_providers_config()` command for ProvidersEditor
  - Added `save_providers_config()` command with validation
  - Added constants for config filenames: `PROVIDERS_CONFIG`, `PRIVACY_RULES`, `PERSONAL_CONTEXT`, `KNOWN_PEERS`, `NODE_KEY`
- **settings.py**
  - Added `cultural_perspectives_enabled` to `[knowledge]` section
  - Added `get_cultural_perspectives_enabled()` method
- **llm_manager.py**
  - Migrated from TOML to JSON format (`providers.toml` → `providers.json`)
  - Added `_migrate_from_toml_if_needed()` for automatic migration
  - Added `save_config()` method for hot-reload without service restart
  - Updated `get_context_window()` to check `providers.json` first
  - Updated `_ensure_config_exists()` to create JSON template
- **firewall.py**
  - Added `_migrate_from_old_filename()` to migrate `.dpc_access.json` → `privacy_rules.json`
  - Updated docstrings to reference new filename

#### Protocol
- **pcm_core.py**
  - Added `edited_by: Optional[str]` field to `KnowledgeEntry`
  - Added `edited_at: Optional[str]` field to `KnowledgeEntry`
- **knowledge_commit.py**
  - Updated `KnowledgeCommitProposal.from_dict()` to deserialize edit tracking fields

#### Frontend
- **+page.svelte**
  - Added token counter display in chat header
  - Added extraction failure state and toast handling
  - Added "🤖 AI Providers" button in sidebar
  - Integrated ProvidersEditor modal component
- **coreService.ts**
  - Added `extractionFailure` writable store
  - Added event handler for `knowledge_extraction_failed`
  - Added `get_providers_config` and `save_providers_config` to `expectsResponse` array
- **KnowledgeCommitDialog.svelte**
  - Added edit mode state (`editMode`, `editedEntries`)
  - Added Edit/Save/Cancel button workflow
  - Added textarea editing for entry content
  - Added "✏️ Edited" badge with attribution details
  - Conditionally display cultural perspectives section
- **ProvidersEditor.svelte** (NEW)
  - Full-featured AI providers configuration editor
  - Tab-based interface (Providers List + Add Provider)
  - Type-specific forms with conditional fields
  - API key source selection (environment variable vs plaintext)
  - Real-time validation and save confirmation
  - Accessibility compliant (ARIA labels, keyboard navigation, form-wrapped password fields)

#### Documentation
- **KNOWLEDGE_ARCHITECTURE.md**
  - Updated with Phase 1-6 implementation details
- **CLAUDE.md**
  - Updated all config file references (`providers.toml` → `providers.json`, `.dpc_access.json` → `privacy_rules.json`)
- **Example Files**:
  - Created `instructions.example.json` - Comprehensive AI instructions template with field reference and use-case examples
  - Created `providers.example.json` - Multi-provider setup guide with security notes
  - Created `privacy_rules.example.json` - Complete firewall rules documentation in JSON format
  - Updated `personal_context_example.json` - Removed inline instruction block, added `metadata.external_files` reference
  - Removed `providers.example.toml` and `.dpc_access.example` (replaced with JSON versions)

### Fixed

**Phase 1: Instructions Editor**
- Fixed "Failed to load instructions: undefined" error on first panel open
- Root cause: InstructionsEditor.svelte incorrectly accessed `result.payload.status` instead of `result.status`
- Fix: Updated loadInstructions and saveChanges to match coreService.ts promise resolution pattern (resolves with payload only)
- Commit: d09a41b

**Phase 2: Token Counter**
- Fixed token counter not appearing after AI messages (only showed at 80% warning)
- Root cause: Frontend only updated tokenUsageMap on warning events, not on every AI response
- Fix: Extract tokens_used and token_limit from execute_ai_query responses immediately
- Also fixed: Missing tiktoken dependency in Poetry environment (was in pyproject.toml but not installed)
- Commit: c382b26
- Fixed token counter not resetting when "New Chat" button pressed
- Root cause: handleNewChat cleared chat history but didn't delete token usage from map
- Fix: Added tokenUsageMap.delete(chatId) to reset counter for new conversations
- Commit: 093f5d0
- Fixed token counter not displaying for remote inference (peer-to-peer compute sharing)
- Root cause: Token metadata not transmitted from host peer to requesting peer in REMOTE_INFERENCE_RESPONSE
- Fix: Added token metadata fields (tokens_used, prompt_tokens, response_tokens, model_max_tokens) to protocol message, host peer now calls llm_manager.query() with return_metadata=True, requesting peer extracts and displays token data
- Impact: Token counting now works identically for both local and remote inference with <1% host overhead
- Files changed: dpc-protocol/protocol.py, dpc-client/core/service.py (4 locations)

**Phase 6: Context Window for Claude 4.5 Models**
- Fixed token counter showing 247% warning due to incorrect context window
- Root cause: MODEL_CONTEXT_WINDOWS missing shorthand model names (claude-haiku-4-5, claude-opus-4-5)
- Fix: Added Claude 4.5 shorthand model names with 200K token context windows
- Commit: bb7c725

**Phase 7: ProvidersEditor Accessibility**
- Fixed Svelte a11y warnings in ProvidersEditor component
- Root causes:
  - Dialog element missing `tabindex="-1"` attribute
  - Modal overlay click handler needed ignore directives
  - Label element without associated control (section heading)
  - Password field not contained in form element
- Fixes:
  - Added `tabindex="-1"` to dialog element for keyboard accessibility
  - Added `svelte-ignore` comments for semantically correct modal backdrop
  - Changed `<label>` to `<strong class="form-label">` for section headings
  - Wrapped password input in `<form on:submit|preventDefault>` to suppress browser warning
- Commit: e7edd87

### Automated Testing Status

**✅ Backend Tests:** 58 passed, 2 skipped (pytest)
- All core functionality tests pass
- Conversation monitoring, firewall, device context, and local API tests verified

**✅ Frontend Type Check:** 0 errors, 0 warnings (svelte-check)
- All TypeScript types validated
- No compilation errors

**⚠️ Deprecation Warnings (Non-blocking):**
- `datetime.utcnow()` deprecated → migrate to `datetime.now(datetime.UTC)` in future
- `websockets.legacy` deprecated → upgrade to websockets v14+ API in future

### Manual Testing Required

Before merging to `main`, verify the following Phase 1-7 features:

- [ ] **Phase 2**: Token counter displays correctly and warns at 80%
- [ ] **Phase 2**: Toast notifications appear on extraction failures
- [ ] **Phase 3**: Cultural perspectives toggle works (on/off)
- [ ] **Phase 4**: JSON repair handles malformed LLM responses
- [ ] **Phase 5**: Inline editing saves with correct attribution
- [ ] **Phase 6**: Context window overrides work in providers.json
- [ ] **Phase 7**: TOML to JSON migration works automatically on first startup
- [ ] **Phase 7**: ProvidersEditor opens from sidebar button
- [ ] **Phase 7**: Add/edit/delete providers works correctly
- [ ] **Phase 7**: API key configuration (env var vs plaintext) saves correctly
- [ ] **Phase 7**: Set default provider functionality works
- [ ] **Phase 7**: Hot-reload applies changes without service restart
- [ ] **Phase 7**: Validation catches invalid configurations

**Manual Test Commands:**
```bash
# Start backend
cd dpc-client/core
poetry run python run_service.py

# Start frontend (separate terminal)
cd dpc-client/ui
npm run tauri dev

# Build production bundle
npm run build
npm run tauri build
```

---

## [0.1.0] - 2025-01-XX (Last Stable Release)

### Added
- Initial release of D-PC Messenger
- Peer-to-peer messaging with end-to-end encryption
- Direct TLS connections (local network)
- WebRTC connections (internet-wide with NAT traversal)
- Federation Hub for discovery and signaling
- OAuth 2.0 authentication (Google, GitHub)
- AI integration (Ollama, OpenAI-compatible, Anthropic)
- Personal Context Manager (PCM v1.0)
- Context Firewall for granular access control
- In-app configuration editors (Personal Context, Firewall Rules)
- Device context collection (hardware/software info)

### Documentation
- Quick Start Guide
- Configuration Reference
- WebRTC Setup Guide
- Device Context Specification
- GitHub Auth Setup Guide

---

## Version History

- **v0.2.0** (unreleased): Knowledge Architecture Phases 1-6
- **v0.1.0** (stable): Initial release with core P2P and AI features
