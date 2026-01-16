# DPTP Specification: D-PC Transfer Protocol v1.2

**Version:** 1.2
**Status:** Stable
**Date:** December 2025
**License:** CC0 1.0 Universal (Public Domain)

## 1. Overview

The **D-PC Transfer Protocol (DPTP)** is a binary-framed, JSON-based messaging protocol designed for peer-to-peer communication in the D-PC Messenger ecosystem. DPTP enables secure, direct exchange of text messages, personal context data, and AI computation requests between cryptographically-identified nodes.

### Design Principles

- **Simplicity**: 10-byte fixed header + JSON payload
- **Extensibility**: JSON allows arbitrary message types and fields
- **Security**: Designed to run over TLS (Direct) or DTLS (WebRTC)
- **Asynchronous**: Built for async I/O with asyncio StreamReader/StreamWriter

### Transport Layers

DPTP messages are transmitted over three encrypted transport mechanisms, part of a 6-tier connection fallback architecture:

1. **Direct TLS** - TCP connections secured with TLS 1.2+
   - **IPv6 Direct**: Internet-wide (40%+ networks, no NAT required)
   - **IPv4 Direct**: Internet-wide with port-forwarding, or local network
   - Uses X.509 certificates for node identity
   - Lowest latency when direct connectivity available

2. **WebRTC Data Channels** - Encrypted with DTLS
   - Hub-assisted NAT traversal via STUN/TURN
   - Internet-wide connections across firewalls
   - Hub provides signaling only (no message routing)

3. **UDP DTLS** - DTLS 1.2 over UDP (v0.10.1+)
   - DHT-coordinated hole punching (no Hub required)
   - 60-70% NAT success rate for cone NAT
   - Volunteer relay fallback for 100% coverage
   - Gossip store-and-forward for disaster scenarios

**Note:** Higher-level connection strategies (relay, gossip) use these base transports for message delivery.

## 2. Message Format

### Binary Frame Structure

All DPTP messages follow a fixed binary framing format:

```
┌──────────────────┬────────────────────────┐
│   10-byte ASCII  │   JSON Payload (UTF-8) │
│   Length Header  │   Variable Length      │
└──────────────────┴────────────────────────┘
```

### Header Format

- **Length**: 10 bytes, ASCII-encoded decimal number
- **Format**: Zero-padded, e.g., `0000000156` for 156-byte payload
- **Encoding**: ASCII (bytes 48-57, characters '0'-'9')

### Payload Format

- **Encoding**: UTF-8 JSON
- **Structure**: Object with `command` or `status` field
- **Maximum Size**: Unlimited (implementation may impose limits)

### Example Wire Format

For a HELLO message:
```json
{"command":"HELLO","payload":{"node_id":"dpc-node-[32 hex characters]","name":"Alice"}}
```

Wire representation (example):
```
0000000088{"command":"HELLO","payload":{"node_id":"dpc-node-[32 hex characters]","name":"Alice"}}
```

## 3. Message Types

### 3.1 Connection Establishment

#### HELLO

Sent immediately after connection establishment to identify the remote peer.

**Format:**
```json
{
  "command": "HELLO",
  "payload": {
    "node_id": "dpc-node-[32 hex characters]",
    "name": "Alice"  // Optional display name
  }
}
```

**Fields:**
- `node_id` (string, required): Cryptographic node identifier (format: `dpc-node-[32 hex characters]`)
- `name` (string, optional): Human-readable display name

**Response:** None (connection is bidirectional; both peers send HELLO)

---

### 3.2 Text Messaging

#### SEND_TEXT

Sends a text message to the peer.

**Format:**
```json
{
  "command": "SEND_TEXT",
  "payload": {
    "text": "Hello, how are you?"
  }
}
```

**Fields:**
- `text` (string, required): Message content

**Response:** None (fire-and-forget)

---

### 3.3 Context Sharing

#### REQUEST_CONTEXT

Requests the peer's personal context data (subject to firewall rules).

**Format:**
```json
{
  "command": "REQUEST_CONTEXT"
}
```

**Response:** CONTEXT_RESPONSE message

---

#### CONTEXT_RESPONSE

Sends personal context data in response to REQUEST_CONTEXT or proactively.

**Format:**
```json
{
  "command": "CONTEXT_RESPONSE",
  "payload": {
    "profile": {
      "name": "Alice",
      "description": "AI researcher",
      "values": ["privacy", "transparency"]
    },
    "knowledge": {
      "topics": [...]
    }
  }
}
```

**Fields:**
- `payload` (object, required): Filtered personal context (structure defined by Personal Context Model v2.0)

**Note:** The actual data sent is filtered by the sender's firewall rules (see `~/.dpc/privacy_rules.json`).

---

### 3.4 Remote AI Inference

#### REMOTE_INFERENCE_REQUEST

Requests the peer to execute an AI inference query using their local compute resources.

**Format:**
```json
{
  "command": "REMOTE_INFERENCE_REQUEST",
  "payload": {
    "request_id": "550e8400-e29b-41d4-a716-446655440000",
    "prompt": "What is the capital of France?",
    "model": "llama3.1:70b",  // Optional
    "provider": "ollama"      // Optional
  }
}
```

**Fields:**
- `request_id` (string, required): UUID for request/response correlation
- `prompt` (string, required): AI query text
- `model` (string, optional): Specific model to use
- `provider` (string, optional): AI provider (ollama, openai, anthropic)

**Response:** REMOTE_INFERENCE_RESPONSE message

**Security:** Peer may reject request based on firewall rules (`privacy_rules.json` → `compute.enabled`)

---

#### REMOTE_INFERENCE_RESPONSE

Returns the result of a remote inference request.

**Success Format:**
```json
{
  "command": "REMOTE_INFERENCE_RESPONSE",
  "payload": {
    "request_id": "550e8400-e29b-41d4-a716-446655440000",
    "status": "success",
    "response": "The capital of France is Paris.",
    "tokens_used": 156,
    "prompt_tokens": 12,
    "response_tokens": 144,
    "model_max_tokens": 128000
  }
}
```

**Error Format:**
```json
{
  "command": "REMOTE_INFERENCE_RESPONSE",
  "payload": {
    "request_id": "550e8400-e29b-41d4-a716-446655440000",
    "status": "error",
    "error": "Model not available"
  }
}
```

**Fields (Success):**
- `request_id` (string, required): Matches request UUID
- `status` (string, required): `"success"`
- `response` (string, required): AI-generated response text
- `tokens_used` (integer, optional): Total tokens consumed
- `prompt_tokens` (integer, optional): Tokens in prompt
- `response_tokens` (integer, optional): Tokens in response
- `model_max_tokens` (integer, optional): Model's context window size

**Fields (Error):**
- `request_id` (string, required): Matches request UUID
- `status` (string, required): `"error"`
- `error` (string, required): Human-readable error message

---

### 3.4.1 Remote Audio Transcription

#### REMOTE_TRANSCRIPTION_REQUEST

Requests the peer to transcribe audio using their local Whisper model.

**Format:**
```json
{
  "command": "REMOTE_TRANSCRIPTION_REQUEST",
  "payload": {
    "request_id": "550e8400-e29b-41d4-a716-446655440000",
    "audio_base64": "SGVsbG8gV29ybGQh...",
    "mime_type": "audio/webm",
    "model": "openai/whisper-large-v3",  // Optional
    "provider": "local_whisper",         // Optional
    "language": "auto",                   // Optional
    "task": "transcribe"                  // Optional: "transcribe" or "translate"
  }
}
```

**Fields:**
- `request_id` (string, required): UUID for request/response correlation
- `audio_base64` (string, required): Base64-encoded audio data
- `mime_type` (string, required): Audio MIME type (e.g., `audio/webm`, `audio/opus`, `audio/wav`)
- `model` (string, optional): Specific Whisper model to use
- `provider` (string, optional): Transcription provider (e.g., `local_whisper`)
- `language` (string, optional): Language code (`auto` for automatic detection, `en`, `es`, etc.)
- `task` (string, optional): `transcribe` (default) or `translate` (to English)

**Response:** REMOTE_TRANSCRIPTION_RESPONSE message

**Security:** Peer may reject request based on firewall rules (`privacy_rules.json` → `transcription.enabled`)

**Size Limits:** Audio data should be reasonable size (recommended max: 10MB base64-encoded)

---

#### REMOTE_TRANSCRIPTION_RESPONSE

Returns the result of a remote transcription request.

**Success Format:**
```json
{
  "command": "REMOTE_TRANSCRIPTION_RESPONSE",
  "payload": {
    "request_id": "550e8400-e29b-41d4-a716-446655440000",
    "status": "success",
    "text": "Hello, this is a transcribed message.",
    "language": "en",
    "duration_seconds": 5.2,
    "provider": "local_whisper"
  }
}
```

**Error Format:**
```json
{
  "command": "REMOTE_TRANSCRIPTION_RESPONSE",
  "payload": {
    "request_id": "550e8400-e29b-41d4-a716-446655440000",
    "status": "error",
    "error": "Transcription not enabled in firewall rules"
  }
}
```

**Fields (Success):**
- `request_id` (string, required): Matches request UUID
- `status` (string, required): `"success"`
- `text` (string, required): Transcribed text
- `language` (string, optional): Detected language code
- `duration_seconds` (number, optional): Audio duration in seconds
- `provider` (string, optional): Provider used for transcription

**Fields (Error):**
- `request_id` (string, required): Matches request UUID
- `status` (string, required): `"error"`
- `error` (string, required): Human-readable error message

**Common Error Codes:**
- `Transcription not enabled in firewall rules` - Peer has disabled remote transcription
- `Model not available` - Requested Whisper model not installed
- `Invalid audio format` - Unsupported MIME type
- `Audio too large` - Exceeds peer's size limits
- `Transcription failed` - Processing error

---

### 3.5 Provider Discovery

#### GET_PROVIDERS

Requests a list of available AI providers/models from the peer.

**Format:**
```json
{
  "command": "GET_PROVIDERS"
}
```

**Response:** PROVIDERS_RESPONSE message

---

#### PROVIDERS_RESPONSE

Returns a list of AI providers available on the peer's system.

**Format:**
```json
{
  "command": "PROVIDERS_RESPONSE",
  "payload": {
    "providers": [
      {
        "alias": "Llama 3.1 70B (Ollama)",
        "model": "llama3.1:70b",
        "type": "ollama"
      },
      {
        "alias": "GPT-4 Turbo",
        "model": "gpt-4-turbo-preview",
        "type": "openai"
      }
    ]
  }
}
```

**Fields:**
- `providers` (array, required): List of provider objects
  - `alias` (string, required): Human-readable name
  - `model` (string, required): Model identifier
  - `type` (string, required): Provider type (ollama, openai, anthropic, etc.)

---

### 3.6 Generic Responses

#### OK Response

Indicates successful processing of a request.

**Format:**
```json
{
  "status": "OK",
  "message": "Request processed successfully"
}
```

---

#### ERROR Response

Indicates an error occurred during processing.

**Format:**
```json
{
  "status": "ERROR",
  "message": "Invalid request format"
}
```

---

### 3.7 Knowledge Commit Messages

#### PROPOSE_KNOWLEDGE_COMMIT

Proposes a new knowledge commit for collaborative knowledge building after multi-party AI discussion.

**Format:**
```json
{
  "command": "PROPOSE_KNOWLEDGE_COMMIT",
  "payload": {
    "proposal_id": "prop-abc123",
    "conversation_id": "conv-xyz789",
    "topic": "game_design",
    "summary": "Environmental storytelling principles",
    "entries": [
      {
        "content": "Environmental storytelling is powerful in games",
        "tags": ["game_design"],
        "confidence": 0.90,
        "cultural_specific": false,
        "requires_context": [],
        "alternative_viewpoints": ["Some games prioritize mechanics over narrative"]
      }
    ],
    "participants": ["dpc-node-alice", "dpc-node-bob", "dpc-node-charlie"],
    "cultural_perspectives": ["Western game design", "Eastern narrative traditions"],
    "alternatives": ["Alternative interpretation: mechanics-first design"],
    "devil_advocate": "Counter-argument: not all games need environmental storytelling",
    "avg_confidence": 0.90,
    "vote_deadline": "2025-12-05T10:30:00Z",
    "required_dissenter": "dpc-node-charlie",
    "extraction_model": "llama3.1:70b",
    "extraction_host": "dpc-node-alice"
  }
}
```

**Fields:**
- `proposal_id` (string, required): Unique proposal identifier
- `conversation_id` (string, required): Source conversation ID
- `topic` (string, required): Knowledge topic name
- `summary` (string, required): Brief description of proposal
- `entries` (array, required): Knowledge entries to commit
- `participants` (array, required): List of participant node_ids
- `cultural_perspectives` (array, optional): Cultural viewpoints considered
- `alternatives` (array, optional): Alternative interpretations
- `devil_advocate` (string, optional): Critical analysis for anti-groupthink
- `avg_confidence` (number, required): Average confidence score (0.0-1.0)
- `vote_deadline` (string, optional): ISO 8601 deadline for voting
- `required_dissenter` (string, optional): Node assigned as devil's advocate
- `extraction_model` (string, optional): AI model used for extraction
- `extraction_host` (string, optional): Node that performed extraction

**Response:** VOTE_KNOWLEDGE_COMMIT messages from each participant

**UI Behavior:** Opens voting dialog showing proposal details, bias mitigation info, and voting options (approve/reject/request_changes)

---

#### VOTE_KNOWLEDGE_COMMIT

Casts a vote on a knowledge commit proposal.

**Format:**
```json
{
  "command": "VOTE_KNOWLEDGE_COMMIT",
  "payload": {
    "proposal_id": "prop-abc123",
    "voter_node_id": "dpc-node-alice",
    "vote": "approve",
    "comment": "Looks good, captures our discussion well",
    "timestamp": "2025-12-05T10:15:00Z",
    "is_required_dissent": false
  }
}
```

**Fields:**
- `proposal_id` (string, required): Proposal identifier being voted on
- `voter_node_id` (string, required): Node ID of voter
- `vote` (string, required): Vote choice - `"approve"` | `"reject"` | `"request_changes"`
- `comment` (string, optional): Optional comment/feedback
- `timestamp` (string, required): ISO 8601 timestamp of vote
- `is_required_dissent` (boolean, required): True if voter is assigned devil's advocate

**Response:** KNOWLEDGE_COMMIT_RESULT (when all votes collected or deadline reached)

---

#### KNOWLEDGE_COMMIT_RESULT

Notifies all participants of voting outcome after all votes are collected or the deadline is reached.

**Format:**
```json
{
  "command": "KNOWLEDGE_COMMIT_RESULT",
  "payload": {
    "proposal_id": "prop-abc123",
    "topic": "game_design",
    "summary": "Environmental storytelling principles",
    "status": "approved",
    "vote_tally": {
      "approve": 3,
      "reject": 0,
      "request_changes": 1,
      "total": 4,
      "threshold": 0.75,
      "approval_rate": 0.75
    },
    "votes": [
      {
        "node_id": "dpc-node-alice",
        "vote": "approve",
        "comment": "Great!",
        "is_required_dissent": false,
        "timestamp": "2025-12-05T10:15:00Z"
      },
      {
        "node_id": "dpc-node-bob",
        "vote": "approve",
        "comment": null,
        "is_required_dissent": false,
        "timestamp": "2025-12-05T10:16:00Z"
      },
      {
        "node_id": "dpc-node-charlie",
        "vote": "approve",
        "comment": "Good, but we should document exceptions",
        "is_required_dissent": true,
        "timestamp": "2025-12-05T10:17:00Z"
      },
      {
        "node_id": "dpc-node-dave",
        "vote": "request_changes",
        "comment": "Add examples",
        "is_required_dissent": false,
        "timestamp": "2025-12-05T10:18:00Z"
      }
    ],
    "commit_id": "commit-xyz789",
    "timestamp": "2025-12-05T10:18:00Z"
  }
}
```

**Fields:**
- `proposal_id` (string, required): Proposal identifier
- `topic` (string, required): Knowledge topic
- `summary` (string, required): Brief summary of proposal
- `status` (string, required): Voting outcome - `"approved"` | `"rejected"` | `"revision_needed"` | `"timeout"`
- `vote_tally` (object, required): Vote statistics
  - `approve` (integer): Number of approve votes
  - `reject` (integer): Number of reject votes
  - `request_changes` (integer): Number of change requests
  - `total` (integer): Total votes received
  - `threshold` (number): Required approval threshold (e.g., 0.75)
  - `approval_rate` (number): Actual approval rate (approve/total)
- `votes` (array, required): All participant votes with details
  - `node_id` (string): Voter's node ID
  - `vote` (string): Vote choice
  - `comment` (string, nullable): Optional comment
  - `is_required_dissent` (boolean): Devil's advocate flag
  - `timestamp` (string): ISO 8601 vote timestamp
- `commit_id` (string, optional): Commit ID if status is "approved"
- `timestamp` (string, required): ISO 8601 timestamp of result finalization

**Response:** None (notification message)

**UI Behavior:** Shows toast notification with outcome (✅ approved, ❌ rejected, ⏱️ timeout) and optional detailed vote breakdown dialog

**Voting Rules:**
- **Approval**: Requires ≥75% of participants to vote "approve"
- **Rejection**: More "reject" votes than "request_changes"
- **Revision Needed**: More "request_changes" than "reject" votes
- **Timeout**: Deadline reached before all votes collected (finalizes with current votes)

---

### 3.8 Device Context Messages

#### REQUEST_DEVICE_CONTEXT

Requests peer's device/hardware information (GPU, RAM, OS, dev tools).

**Format:**
```json
{
  "command": "REQUEST_DEVICE_CONTEXT"
}
```

**Response:** DEVICE_CONTEXT_RESPONSE message

**Use Case:** Enables environment-aware AI assistance and compute-sharing decisions based on peer hardware capabilities.

---

#### DEVICE_CONTEXT_RESPONSE

Returns device context (subject to firewall rules).

**Format:**
```json
{
  "command": "DEVICE_CONTEXT_RESPONSE",
  "payload": {
    "hardware": {
      "gpu": {"model": "RTX 3060", "vram_gb": 12},
      "ram_gb": 24
    },
    "software": {
      "os": {"family": "Windows", "version": "10"}
    }
  }
}
```

**Fields:**
- `payload` (object, required): Filtered device context (structure defined by device_context.json schema v1.1)

**Note:** Privacy-sensitive. Firewall rules control which hardware/software details are shared.

---

### 3.9 Context Update Notifications

#### CONTEXT_UPDATED

Broadcasts when personal context changes, invalidates peer caches.

**Format:**
```json
{
  "command": "CONTEXT_UPDATED",
  "payload": {
    "node_id": "dpc-node-alice123",
    "context_hash": "a1b2c3d4...",
    "timestamp": "2025-12-11T10:30:00Z"
  }
}
```

**Fields:**
- `node_id` (string, required): Node that updated their context
- `context_hash` (string, required): SHA256 hash of new context for cache invalidation
- `timestamp` (string, required): ISO 8601 timestamp of update

**Use Case:** Phase 7 peer cache invalidation - notifies peers when context changes so they can refresh cached data.

---

### 3.10 Gossip Protocol Messages

#### GOSSIP_SYNC

Anti-entropy synchronization for message reconciliation.

**Format:**
```json
{
  "command": "GOSSIP_SYNC",
  "payload": {
    "vector_clock": {"dpc-node-alice123": 42, "dpc-node-bob456": 17},
    "message_ids": ["msg-abc123...", "msg-def456..."]
  }
}
```

**Fields:**
- `vector_clock` (object, required): Sender's vector clock state (node_id → counter)
- `message_ids` (array, required): Message IDs sender already has

**Behavior:**
1. Receiver compares sender's `message_ids` with own message store
2. Identifies messages receiver has that sender doesn't
3. Sends missing messages via GOSSIP_MESSAGE commands
4. Merges sender's vector clock with own clock

**Use Case:** Periodic sync (5-minute interval) to ensure all nodes eventually receive all messages. Identifies missing messages for forwarding.

---

#### GOSSIP_MESSAGE

Epidemic message routing for store-and-forward delivery with end-to-end encryption.

**Format:**
```json
{
  "command": "GOSSIP_MESSAGE",
  "payload": {
    "gossip_message": {
      "id": "msg-abc123...",
      "source": "dpc-node-alice123",
      "destination": "dpc-node-bob456",
      "payload": {
        "encrypted": "base64-encoded-encrypted-blob"
      },
      "hops": 2,
      "max_hops": 5,
      "ttl": 86400,
      "created_at": 1705329600.0,
      "already_forwarded": ["dpc-node-alice123", "dpc-node-charlie789"],
      "vector_clock": {"dpc-node-alice123": 5},
      "priority": "normal"
    }
  }
}
```

**Fields:**
- `id` (string, required): Unique message ID (generated by sender)
- `source` (string, required): Original sender node ID
- `destination` (string, required): Target recipient node ID
- `payload` (object, required): Message content
  - `encrypted` (string, required): Base64-encoded RSA-OAEP encrypted payload (E2E encryption)
- `hops` (integer, required): Current hop count (increments at each forward)
- `max_hops` (integer, required): Maximum allowed hops (default: 5)
- `ttl` (integer, required): Time-to-live in seconds (default: 86400 = 24 hours)
- `created_at` (float, required): Unix timestamp when message created
- `already_forwarded` (array, required): Node IDs that have forwarded this message
- `vector_clock` (object, required): Causality tracking (node_id → counter)
- `priority` (string, required): Message priority ("normal", "high", "low")

**Behavior:**

Receiver performs these checks in order:

1. **TTL check**: Drop if `current_time - created_at > ttl`
2. **Hop limit check**: Drop if `hops >= max_hops`
3. **Destination check**: If `destination == self.node_id` → decrypt and deliver locally
4. **Deduplication**: If message ID seen before → ignore (already processed)
5. **Already forwarded**: If `self.node_id` in `already_forwarded` → ignore (loop prevention)
6. **Forward**: Otherwise, forward to N=3 random connected peers (epidemic fanout)

**Security (End-to-End Encryption):**
- Payload encrypted with recipient's RSA public key (OAEP padding)
- Only sender and recipient can decrypt message content
- Intermediate hops see only: source, destination, TTL, hop count, encrypted blob
- Intermediate hops **cannot** decrypt message content (privacy-preserving)

**Use Case:**
- Last-resort fallback when all direct connections fail (Priority 6)
- Offline messaging (peer receives when comes online)
- Disaster scenarios (infrastructure outages)
- Eventual delivery guarantee (multi-hop epidemic routing)

**Performance:**
- Not real-time (expect 5-60 second latency for 2-3 hop delivery)
- Suitable for knowledge commits, offline messages, disaster communication

---

### 3.11 File Transfer Messages (v0.11.0)

Peer-to-peer file sharing with chunked transfers, progress tracking, and SHA256 verification.

#### FILE_OFFER

Offers a file to the peer for transfer.

**Format:**
```json
{
  "command": "FILE_OFFER",
  "payload": {
    "transfer_id": "550e8400-e29b-41d4-a716-446655440000",
    "filename": "report.pdf",
    "size_bytes": 2048000,
    "hash": "sha256:a1b2c3d4e5f6...",
    "mime_type": "application/pdf",
    "chunk_size": 65536,
    "chunk_hashes": ["a1b2c3d4", "e5f6g7h8", ...]
  }
}
```

**Fields:**
- `transfer_id` (string, required): UUID for transfer tracking
- `filename` (string, required): Original filename
- `size_bytes` (integer, required): Total file size in bytes
- `hash` (string, required): SHA256 hash for integrity verification (format: `sha256:hex_string`)
- `mime_type` (string, required): MIME type of the file
- `chunk_size` (integer, required): Bytes per chunk (typically 65536 = 64KB)
- `chunk_hashes` (array of strings, optional): CRC32 hashes per chunk (v0.11.1+, 8-char hex strings)
- `image_metadata` (object, optional): Metadata for image transfers (v0.12.0)
  - `dimensions` (object, required): Image dimensions
    - `width` (integer): Width in pixels
    - `height` (integer): Height in pixels
  - `thumbnail_base64` (string, required): Data URL of thumbnail (max 100KB)
  - `source` (string, required): Source of image (e.g., "clipboard", "file", "screenshot")
  - `captured_at` (string, optional): ISO 8601 timestamp when image was captured
- `voice_metadata` (object, optional): Metadata for voice message transfers (v0.13.0)
  - `duration_seconds` (number, required): Recording duration in seconds
  - `sample_rate` (integer, required): Audio sample rate in Hz (e.g., 48000)
  - `channels` (integer, required): Number of audio channels (1 = mono, 2 = stereo)
  - `codec` (string, required): Audio codec used (e.g., "opus", "aac")
  - `recorded_at` (string, required): ISO 8601 timestamp when voice was recorded

**Response:** FILE_ACCEPT (if accepted) or FILE_CANCEL (if rejected)

**Security:** Peer may reject based on firewall rules (`privacy_rules.json` → `file_transfer.allow_nodes/allow_groups`)

**UI Behavior:** Opens accept/reject dialog showing filename, size, sender, and hash for user approval

---

#### FILE_ACCEPT

Accepts a file transfer offer.

**Format:**
```json
{
  "command": "FILE_ACCEPT",
  "payload": {
    "transfer_id": "550e8400-e29b-41d4-a716-446655440000"
  }
}
```

**Fields:**
- `transfer_id` (string, required): Matches FILE_OFFER transfer ID

**Response:** FILE_CHUNK messages (sender begins chunked transfer)

---

#### FILE_CHUNK

Transfers a single chunk of file data.

**Format:**
```json
{
  "command": "FILE_CHUNK",
  "payload": {
    "transfer_id": "550e8400-e29b-41d4-a716-446655440000",
    "chunk_index": 0,
    "total_chunks": 32,
    "data": "base64_encoded_chunk_data..."
  }
}
```

**Fields:**
- `transfer_id` (string, required): Transfer identifier
- `chunk_index` (integer, required): Zero-based chunk index
- `total_chunks` (integer, required): Total number of chunks in transfer
- `data` (string, required): Base64-encoded chunk data (typically 64KB when decoded)

**Behavior:**
- Receiver stores chunks to temporary buffer
- **Per-chunk verification (v0.11.1+):** If `chunk_hashes` provided in FILE_OFFER, receiver verifies CRC32 of each chunk
- **Retry on failure (v0.11.1+):** If chunk verification fails, sends FILE_CHUNK_RETRY (max 3 retries per chunk)
- Progress tracking: `(chunk_index + 1) / total_chunks * 100%`
- Receiver sends FILE_COMPLETE after receiving all chunks

---

#### FILE_COMPLETE

Signals successful file transfer completion with hash verification.

**Format:**
```json
{
  "command": "FILE_COMPLETE",
  "payload": {
    "transfer_id": "550e8400-e29b-41d4-a716-446655440000",
    "hash": "sha256:a1b2c3d4e5f6..."
  }
}
```

**Fields:**
- `transfer_id` (string, required): Transfer identifier
- `hash` (string, required): SHA256 hash of complete file for verification

**Behavior:**
- Receiver verifies hash matches FILE_OFFER hash
- If hash mismatch: sends FILE_CANCEL with reason `hash_mismatch`
- If hash matches: saves file to `~/.dpc/conversations/{peer_id}/files/{filename}`
- Adds file metadata to conversation history

**Storage:**
- Files stored per-peer: `~/.dpc/conversations/{peer_id}/files/`
- Conversation history stores metadata only (filename, size, hash, MIME type)
- Token usage: ~20 tokens per file (metadata-only reference)

---

#### FILE_CANCEL

Cancels an in-progress or pending file transfer.

**Format:**
```json
{
  "command": "FILE_CANCEL",
  "payload": {
    "transfer_id": "550e8400-e29b-41d4-a716-446655440000",
    "reason": "user_cancelled"
  }
}
```

**Fields:**
- `transfer_id` (string, required): Transfer identifier
- `reason` (string, required): Cancellation reason
  - `user_cancelled` - User manually cancelled
  - `timeout` - Transfer timed out
  - `hash_mismatch` - SHA256 verification failed
  - `chunk_verification_failed` - Chunk CRC32 verification failed after max retries (v0.11.1+)
  - `permission_denied` - Firewall rejected transfer
  - `size_limit_exceeded` - File exceeds peer's size limit

**Behavior:**
- Both sender and receiver can send FILE_CANCEL
- Receiver cleans up partial transfer data
- Sender stops sending chunks
- Transfer marked as failed in UI

---

#### FILE_CHUNK_RETRY

Requests re-transmission of a specific chunk that failed verification (v0.11.1+).

**Format:**
```json
{
  "command": "FILE_CHUNK_RETRY",
  "payload": {
    "transfer_id": "550e8400-e29b-41d4-a716-446655440000",
    "chunk_index": 42
  }
}
```

**Fields:**
- `transfer_id` (string, required): Transfer identifier
- `chunk_index` (integer, required): Zero-based index of chunk to retry

**Behavior:**
- Sent by receiver when chunk CRC32 verification fails
- Sender re-transmits the specific chunk via FILE_CHUNK
- Maximum 3 retry attempts per chunk
- After max retries: receiver sends FILE_CANCEL with reason `chunk_verification_failed`

**Use Case:**
- Large file transfers over unreliable connections
- Detect corruption immediately (don't waste time receiving remaining chunks)
- Only retry corrupted chunks (efficient bandwidth usage)

**Performance:**
- For 159 MB file (2,541 chunks): 10 KB overhead for CRC32 hashes vs 80 KB for SHA256 per-chunk
- CRC32 verification ~10x faster than SHA256
- Prevents wasting time on corrupted transfers (detect at chunk-level, not file-level)

---

**File Transfer Configuration:**

Firewall rules (`~/.dpc/privacy_rules.json`):
```json
{
  "file_transfer": {
    "allow_nodes": ["dpc-node-alice-123"],
    "allow_groups": ["friends"],
    "max_size_mb": 1000,
    "allowed_mime_types": ["*"]
  }
}
```

Client configuration (`~/.dpc/config.ini`):
```ini
[file_transfer]
enabled = true
chunk_size = 65536
background_threshold_mb = 50
max_concurrent_transfers = 3
verify_hash = true
```

**Security:**
- All file transfers encrypted end-to-end (inherited from connection layer: TLS, DTLS, WebRTC)
- SHA256 hash verification prevents tampering
- Firewall rules required for transfer (default: deny)
- Per-peer storage isolation
- No server-side storage (pure P2P)

**Large File Handling:**
- Files >50MB use background transfer process
- Files >100MB prefer Direct TLS connection (fallback to WebRTC/relay for smaller files)
- Progress tracking with pause/resume support (future enhancement)

---

### 3.12 Voice Transcription Messages (v0.13.2)

Distributed voice message transcription with automatic deduplication to prevent duplicate work in group chats.

**Overview:**
When a voice message is received, participants coordinate to ensure only ONE peer transcribes the audio and shares the result with all others. This prevents wasteful duplicate transcription in group conversations.

**Distributed Transcription Flow:**
1. Alice sends voice message → Bob & Charlie receive
2. Bob waits 3 seconds (recipient delay), checks if transcription exists
3. If Bob has Whisper capability: transcribes and broadcasts VOICE_TRANSCRIPTION to all participants
4. Charlie waits 3 seconds, sees Bob already transcribed, skips transcription
5. Charlie receives VOICE_TRANSCRIPTION from Bob and displays result

**Deduplication:**
- Uses `transfer_id` (from FILE_OFFER) as unique key
- Per-transfer locks prevent race conditions
- First transcription wins (subsequent messages ignored)

#### VOICE_TRANSCRIPTION

Broadcasts transcription result to all participants in a voice message conversation.

**Format:**
```json
{
  "command": "VOICE_TRANSCRIPTION",
  "payload": {
    "transfer_id": "550e8400-e29b-41d4-a716-446655440000",
    "transcription_text": "Hello, this is a test voice message.",
    "transcriber_node_id": "dpc-node-bob-abc123",
    "provider": "local_whisper",
    "confidence": 0.95,
    "language": "en",
    "timestamp": "2026-01-06T12:34:56Z",
    "remote_provider_node_id": "dpc-node-charlie-xyz789"
  }
}
```

**Fields:**
- `transfer_id` (string, required): Matches FILE_OFFER transfer_id for the voice message
- `transcription_text` (string, required): Transcribed text content
- `transcriber_node_id` (string, required): Node ID of orchestrator who initiated transcription
- `provider` (string, required): Transcription provider used (e.g., "local_whisper", "openai", "remote:charlie:local_whisper")
- `confidence` (float, required): Transcription confidence score (0.0 to 1.0)
- `language` (string, required): Detected language code (e.g., "en", "es", "zh")
- `timestamp` (string, required): ISO 8601 timestamp when transcription was created
- `remote_provider_node_id` (string, optional): Node ID of remote compute provider (if using remote Whisper)

**Response:** None (broadcast message, no acknowledgment required)

**Behavior:**
- Receiver checks for duplicate (via `transfer_id` in registry)
- If duplicate: ignores message (transcription already stored)
- If new: stores transcription data in registry
- Updates conversation history (attaches transcription to voice message attachment)
- Broadcasts `voice_transcription_received` event to UI

**Remote Whisper Scenario:**
When Bob uses Charlie's remote Whisper to transcribe Alice's voice message:
- `transcriber_node_id`: Bob (orchestrator who decided to transcribe)
- `provider`: "remote:charlie:local_whisper" (shows remote usage)
- `remote_provider_node_id`: Charlie (compute provider who did actual work)
- UI attribution (if enabled): "Transcribed by Bob using Charlie's local_whisper"

**Privacy:**
- Transcription text stored locally in conversation history
- No Hub involvement (pure P2P broadcast)
- Only participants who received original voice message get transcription

---

**Voice Transcription Configuration:**

Client configuration (`~/.dpc/config.ini`):
```ini
[voice_transcription]
enabled = true
sender_transcribes = false
recipient_delay_seconds = 3
provider_priority = local_whisper,openai
show_transcriber_name = false
cache_transcriptions = true
fallback_to_openai = true
```

**Settings:**
- `enabled`: Master toggle for auto-transcription
- `sender_transcribes`: Whether sender should transcribe their own voice messages (default: false)
- `recipient_delay_seconds`: Wait time before recipients attempt transcription (default: 3 seconds)
- `provider_priority`: Comma-separated list of providers to try (default: local_whisper,openai)
- `show_transcriber_name`: Show attribution in UI (default: false for privacy)
- `cache_transcriptions`: Store transcriptions in conversation history (default: true)
- `fallback_to_openai`: Use OpenAI API if local Whisper unavailable (default: true)

**UI Display:**
- Transcription shown **below** voice player (original audio always visible)
- Checkmark icon indicates transcription status
- Optional attribution shows transcriber and provider
- Warning icon (⚠️) for low confidence transcriptions (<0.8)

**Edge Cases:**
- **No Whisper available**: No transcription occurs (graceful degradation, voice player only)
- **Multiple simultaneous recipients**: Recipient delay + locks prevent duplicate work
- **Offline peer**: Transcription happens when peer reconnects and receives voice message

**Token Usage:**
- Transcription metadata: ~50 tokens per voice message (text + attribution)
- Stored in conversation history for persistent display

---

### 3.13 Relay Protocol Messages (v0.10.0)

Server-side relay functionality enabling NAT traversal for 100% peer connectivity. Volunteer relay nodes forward encrypted messages between peers without decrypting content.

**Privacy Note:** Relays see peer IDs, message sizes, and timing, but cannot decrypt message content (end-to-end encryption maintained).

#### RELAY_REGISTER

Client requests to establish a relay session through a volunteer relay node.

**Format:**
```json
{
  "command": "RELAY_REGISTER",
  "payload": {
    "peer_id": "dpc-node-target-peer-123",
    "timeout": 30.0
  }
}
```

**Fields:**
- `peer_id` (string, required): Node ID of target peer to connect through relay
- `timeout` (float, optional): Registration timeout in seconds (default: 30.0)

**Responses:**

**RELAY_WAITING** (if waiting for other peer):
```json
{
  "command": "RELAY_WAITING",
  "payload": {
    "message": "Waiting for peer dpc-node-target-pee to register",
    "timeout": 30.0
  }
}
```

**RELAY_READY** (if both peers registered):
```json
{
  "command": "RELAY_READY",
  "payload": {
    "session_id": "550e8400-e29b-41d4-a716-446655440000",
    "peer_id": "dpc-node-target-peer-123"
  }
}
```

**ERROR** (if relay not volunteering or invalid request):
```json
{
  "command": "ERROR",
  "payload": {
    "error": "not_volunteering",
    "message": "This node is not volunteering as a relay"
  }
}
```

**Protocol Flow:**
1. Peer A sends RELAY_REGISTER(peer_id=Peer B)
2. Relay responds with RELAY_WAITING (waiting for Peer B)
3. Peer B sends RELAY_REGISTER(peer_id=Peer A)
4. Relay creates session and sends RELAY_READY to both peers

**Security:**
- Only volunteering nodes accept RELAY_REGISTER requests
- Session created only when both peers register for each other (mutual consent)

---

#### RELAY_MESSAGE

Forwards an encrypted message through an established relay session.

**Format:**
```json
{
  "command": "RELAY_MESSAGE",
  "payload": {
    "from": "dpc-node-sender-123",
    "to": "dpc-node-receiver-456",
    "session_id": "550e8400-e29b-41d4-a716-446655440000",
    "message": {
      "command": "SEND_TEXT",
      "payload": {"text": "Hello via relay!"}
    }
  }
}
```

**Fields:**
- `from` (string, required): Sender node ID (must match connection identity)
- `to` (string, required): Receiver node ID
- `session_id` (string, required): Active relay session identifier
- `message` (object, required): Encrypted DPTP message to forward (any command type)

**Behavior:**
- Relay verifies sender matches connection identity
- Relay verifies session exists and sender is participant
- Relay forwards message to receiver via P2P connection
- Relay cannot decrypt message content (E2E encryption maintained)

**Responses:**

**ERROR** (on validation failure):
```json
{
  "command": "ERROR",
  "payload": {
    "error": "forward_failed",
    "message": "Failed to forward message (session not found or rate limited)"
  }
}
```

**Security:**
- Relay cannot see message content (E2E encrypted payload)
- Relay can see: peer IDs, message size, timing metadata
- Sender identity verified against connection

---

#### RELAY_DISCONNECT

Closes an active relay session and cleans up relay state.

**Format:**
```json
{
  "command": "RELAY_DISCONNECT",
  "payload": {
    "peer": "dpc-node-other-peer-456",
    "session_id": "550e8400-e29b-41d4-a716-446655440000",
    "reason": "connection_closed"
  }
}
```

**Fields:**
- `peer` (string, optional): Other peer node ID (for logging)
- `session_id` (string, required): Session identifier to close
- `reason` (string, optional): Disconnection reason
  - `connection_closed` - Connection lost
  - `user_request` - User manually disconnected
  - `timeout` - Session timed out

**Response:**

**RELAY_DISCONNECT_ACK**:
```json
{
  "command": "RELAY_DISCONNECT_ACK",
  "payload": {
    "session_id": "550e8400-e29b-41d4-a716-446655440000",
    "status": "cleaned_up"
  }
}
```

**Status Values:**
- `cleaned_up` - Session successfully removed
- `not_found` - Session already cleaned up or never existed

**Behavior:**
- Relay removes session from active sessions
- Relay cleans up peer-to-session mappings
- Both peers can reconnect or fall back to gossip protocol

**Security:**
- Only session participants can send RELAY_DISCONNECT
- Non-participants receive ERROR response with `not_authorized`

---

**Relay Configuration:**

Client configuration (`~/.dpc/config.ini`):
```ini
[relay]
# CLIENT MODE: Use relays for outbound connections
enabled = true
prefer_region = global
cache_timeout = 300

# SERVER MODE: Volunteer as relay (opt-in)
volunteer = false
max_peers = 10
bandwidth_limit_mbps = 10.0
region = global
```

Firewall rules (automatic for relays):
- Relays accept RELAY_REGISTER, RELAY_MESSAGE, RELAY_DISCONNECT from all peers
- Relays enforce bandwidth limits and max concurrent sessions
- Relays can be disabled by setting `relay.volunteer = false`

**Quality Scoring (DHT):**

Relays announce quality metrics to DHT:
- **Geographic region**: `us-west`, `eu-central`, `ap-southeast`, `global`
- **Uptime score**: 0.0 to 1.0 (calculated from start time)
- **Latency**: Round-trip time in milliseconds
- **Capacity**: Available bandwidth and concurrent sessions

Clients select relays using weighted scoring:
- Uptime: 50% weight
- Capacity: 30% weight
- Latency: 20% weight

**Use Cases:**
- Symmetric NAT traversal (when UDP hole punching fails)
- Backup connection when Hub unavailable
- Geographic proximity optimization (lower latency)
- Community-driven infrastructure (no centralized TURN servers)

---

### 3.13 Vision & Image Messages (v0.12.0)

Remote vision inference and image analysis capabilities.

#### SEND_IMAGE

Requests remote vision inference on one or more images.

**Format:**
```json
{
  "command": "SEND_IMAGE",
  "payload": {
    "request_id": "550e8400-e29b-41d4-a716-446655440000",
    "prompt": "What's in this screenshot?",
    "images": [
      {
        "path": "screenshot_20251225_103045.png",
        "base64": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg...",
        "mime_type": "image/png"
      }
    ],
    "model": "llava:13b",
    "provider": "ollama"
  }
}
```

**Fields:**
- `request_id` (string, required): UUID for request/response correlation
- `prompt` (string, required): Text prompt for vision model
- `images` (array, required): List of image objects
  - `path` (string, optional): Original filename
  - `base64` (string, required): Base64-encoded image data (data URL format)
  - `mime_type` (string, required): MIME type (e.g., image/png, image/jpeg)
- `model` (string, optional): Specific vision model to use (e.g., llava:13b, gpt-4-vision)
- `provider` (string, optional): AI provider (ollama, openai, anthropic)

**Response:** REMOTE_INFERENCE_RESPONSE (reuses existing message type)

**Use Cases:**
- Screenshot analysis and OCR
- Diagram/chart interpretation
- Photo classification
- Visual question answering
- Remote GPU utilization for image processing

**Security:** Peer may reject based on firewall rules (`privacy_rules.json` → `compute.enabled`)

---

### 3.14 Session Management Messages (v0.12.0)

Collaborative session reset with voting to prevent accidental data loss in multi-party conversations.

#### PROPOSE_NEW_SESSION

Proposes ending current conversation and starting fresh session.

**Format:**
```json
{
  "command": "PROPOSE_NEW_SESSION",
  "payload": {
    "proposal_id": "prop-abc123",
    "conversation_id": "conv-xyz789",
    "proposer_node_id": "dpc-node-alice-123",
    "timestamp": "2025-12-25T10:30:00Z"
  }
}
```

**Fields:**
- `proposal_id` (string, required): Unique proposal identifier
- `conversation_id` (string, required): Conversation to reset
- `proposer_node_id` (string, required): Node ID of proposer
- `timestamp` (string, required): ISO 8601 timestamp of proposal

**Response:** VOTE_NEW_SESSION from each participant

**UI Behavior:** Opens voting dialog showing proposal details and vote status from all participants

---

#### VOTE_NEW_SESSION

Casts a vote on a session reset proposal.

**Format:**
```json
{
  "command": "VOTE_NEW_SESSION",
  "payload": {
    "proposal_id": "prop-abc123",
    "voter_node_id": "dpc-node-bob-456",
    "vote": "approve",
    "timestamp": "2025-12-25T10:31:00Z"
  }
}
```

**Fields:**
- `proposal_id` (string, required): Proposal being voted on
- `voter_node_id` (string, required): Node ID of voter
- `vote` (string, required): Vote choice - `"approve"` | `"reject"`
- `timestamp` (string, required): ISO 8601 timestamp of vote

**Response:** NEW_SESSION_RESULT (when all votes collected)

---

#### NEW_SESSION_RESULT

Notifies all participants of voting outcome.

**Format:**
```json
{
  "command": "NEW_SESSION_RESULT",
  "payload": {
    "proposal_id": "prop-abc123",
    "status": "approved",
    "votes": [
      {"node_id": "dpc-node-alice-123", "vote": "approve"},
      {"node_id": "dpc-node-bob-456", "vote": "approve"}
    ],
    "timestamp": "2025-12-25T10:32:00Z"
  }
}
```

**Fields:**
- `proposal_id` (string, required): Proposal identifier
- `status` (string, required): Result - `"approved"` | `"rejected"`
- `votes` (array, required): All participant votes
  - `node_id` (string): Voter's node ID
  - `vote` (string): Vote choice
- `timestamp` (string, required): ISO 8601 timestamp of finalization

**Behavior:**
- **Unanimous approval required**: All participants must vote "approve"
- **If approved**: All participants clear conversation history
- **If rejected**: Conversation continues unchanged

**UI Behavior:** Opens voting dialog showing proposal and real-time vote status from all participants

---

### 3.15 Chat History Synchronization (v0.12.0)

Enables peers to synchronize conversation history after reconnection or page refresh.

#### REQUEST_CHAT_HISTORY

Requests conversation history from peer.

**Format:**
```json
{
  "command": "REQUEST_CHAT_HISTORY",
  "payload": {
    "conversation_id": "conv-xyz789",
    "since_timestamp": "2025-12-25T10:00:00Z"
  }
}
```

**Fields:**
- `conversation_id` (string, required): Conversation ID to sync
- `since_timestamp` (string, optional): Only return messages after this timestamp (ISO 8601)

**Response:** CHAT_HISTORY_RESPONSE

---

#### CHAT_HISTORY_RESPONSE

Returns conversation history to requesting peer.

**Format:**
```json
{
  "command": "CHAT_HISTORY_RESPONSE",
  "payload": {
    "conversation_id": "conv-xyz789",
    "messages": [
      {
        "role": "user",
        "text": "Hello!",
        "timestamp": "2025-12-25T10:15:00Z",
        "sender_node_id": "dpc-node-alice-123"
      },
      {
        "role": "assistant",
        "text": "Hi there!",
        "timestamp": "2025-12-25T10:15:05Z"
      }
    ]
  }
}
```

**Fields:**
- `conversation_id` (string, required): Conversation ID
- `messages` (array, required): List of message objects
  - `role` (string, required): `"user"` | `"assistant"`
  - `text` (string, required): Message content
  - `timestamp` (string, required): ISO 8601 timestamp
  - `sender_node_id` (string, optional): Sender node ID (for user messages)

**Use Cases:**
- **Automatic sync on reconnect**: Restore conversation after temporary disconnection
- **Backend→frontend sync**: Reload chat history after page refresh
- **Partial sync**: Request only recent messages using `since_timestamp`

**Security:** History shared based on firewall rules (can be restricted per-peer)

---

## 4. Node Identity System

### Node ID Format

Node IDs follow the format: `dpc-node-[32 hex characters]`

**Example:** `dpc-node-a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6`

### Identity Generation

1. Generate RSA-2048 key pair
2. Create self-signed X.509 certificate with node_id as Common Name
3. Compute SHA256 hash of public key (PEM format)
4. Take first 32 hex characters of hash (128 bits for Kademlia DHT compatibility)
5. Prepend `dpc-node-` prefix

**Implementation:** See `dpc-protocol/dpc_protocol/crypto.py`

### Certificate Validation

For Direct TLS connections:
- Peer's certificate Common Name (CN) must match their claimed `node_id` in HELLO message
- Certificate must be valid (not expired)
- Certificate signature must be valid

**Security Note:** DPTP uses self-signed certificates (no central Certificate Authority). Trust is established through out-of-band verification of node IDs.

### Identity Storage

Per-node identity files stored in `~/.dpc/`:
- `node.key` - RSA private key (2048-bit, PEM format)
- `node.crt` - X.509 self-signed certificate (PEM format)
- `node.id` - Node identifier (text file)

## 5. Connection Flow

### Direct TLS Connection

```
┌─────────┐                              ┌─────────┐
│  Alice  │                              │   Bob   │
└────┬────┘                              └────┬────┘
     │                                        │
     │  1. TCP + TLS Handshake                │
     │  (Verify cert CN matches node_id)      │
     ├───────────────────────────────────────>│
     │<───────────────────────────────────────┤
     │                                        │
     │  2. HELLO                              │
     ├───────────────────────────────────────>│
     │<───────────────────────────────────────┤
     │  3. HELLO                              │
     │                                        │
     │  Connection established                │
     │                                        │
     │  4. SEND_TEXT, GET_CONTEXT, etc.       │
     │<──────────────────────────────────────>│
     │                                        │
```

### WebRTC Connection

```
┌─────────┐         ┌─────────┐          ┌─────────┐
│  Alice  │         │   Hub   │          │   Bob   │
└────┬────┘         └────┬────┘          └────┬────┘
     │                   │                    │
     │  1. WebSocket     │                    │
     │  /ws/signal       │  2. WebSocket      │
     ├──────────────────>│<───────────────────┤
     │                   │                    │
     │  3. offer         │  4. offer          │
     ├──────────────────>├───────────────────>│
     │  5. answer        │  6. answer         │
     │<──────────────────┤<───────────────────┤
     │                   │                    │
     │  7. ICE candidates exchanged           │
     │<──────────────────────────────────────>│
     │        (via Hub signaling)             │
     │                   │                    │
     │  8. Direct DTLS connection established │
     │        (Hub no longer involved)        │
     │<──────────────────────────────────────>│
     │                   │                    │
     │  9. HELLO         │                    │
     ├──────────────────────────────────────->│
     │<───────────────────────────────────────┤
     │  10. HELLO        │                    │
     │                   │                    │
     │  11. DPTP messages over data channel   │
     │<──────────────────────────────────────>│
     │                                        │
```

**Note:** Hub only facilitates signaling (steps 1-7). All DPTP messages flow directly peer-to-peer (steps 9-11).

### UDP Hole Punch Connection (DTLS)

```
┌─────────┐         ┌─────────┐         ┌─────────┐
│  Alice  │         │DHT Peers│         │   Bob   │
└────┬────┘         └────┬────┘         └────┬────┘
     │                   │                   │
     │  1. Query DHT for Bob's node_id       │
     ├──────────────────>│                   │
     │  2. Bob's endpoints                   │
     │<──────────────────┤                   │
     │                   │                   │
     │  3. Send reflexive address to Alice   │
     │<──────────────────┤                   │
     │                   │  4. Query DHT for Alice's node_id
     │                   │<──────────────────┤
     │                   │  5. Alice's endpoints
     │                   ├──────────────────>│
     │                   │  6. Send reflexive address to Bob
     │                   ├──────────────────>│
     │                   │                   │
     │  7. Simultaneous UDP send (hole punch)│
     ├──────────────────────────────────────>│
     │<──────────────────────────────────────┤
     │                   │                   │
     │  8. DTLS 1.2 handshake over UDP       │
     ├──────────────────────────────────────>│
     │<──────────────────────────────────────┤
     │                   │                   │
     │  9. HELLO (over DTLS)                 │
     ├──────────────────────────────────────>│
     │<──────────────────────────────────────┤
     │  10. HELLO                            │
     │                   │                   │
     │  11. DPTP messages over DTLS          │
     │<─────────────────────────────────────>│
     │                                       │
```

**Note:** DHT-coordinated hole punching uses birthday paradox (simultaneous send from both sides). Works for cone NAT (60-70% success rate), gracefully falls back to relay for symmetric NAT. No Hub involvement - completely decentralized.

## 6. Security Considerations

### Encryption

- **Direct TLS**: All traffic encrypted with TLS 1.2+ (AES-256-GCM recommended)
- **WebRTC**: Data channels encrypted with DTLS (derived from SRTP)
- **UDP DTLS**: DHT-coordinated hole punch connections encrypted with DTLS 1.2 (v0.10.1+)

### Authentication

- **Direct TLS**: Nodes authenticate via X.509 certificates
  - Certificate CN must match `node_id` in HELLO message
  - No centralized PKI - trust established out-of-band (TOFU)

- **WebRTC DTLS**: Certificate fingerprints exchanged via Hub signaling
  - Hub verifies node identity before relaying fingerprints
  - DTLS handshake validates certificates match signaled fingerprints

- **UDP DTLS**: X.509 certificate-based authentication (same as Direct TLS)
  - Certificate verification during DTLS handshake
  - Node ID validation against certificate CN

### Privacy & Firewall

- Peers control data sharing via `~/.dpc/privacy_rules.json`
- Firewall rules filter:
  - Personal context fields (e.g., `personal.json:profile.name` → `allow`)
  - Compute sharing (e.g., `compute.enabled` → `false`)
  - Per-peer or per-group permissions

**Example Rule:**
```json
{
  "nodes": {
    "dpc-node-alice-123": {
      "personal.json:*": "allow"
    }
  },
  "compute": {
    "enabled": false
  }
}
```

### Attack Mitigation

- **Message Flooding**: Implementations should rate-limit incoming messages
- **Resource Exhaustion**: Limit maximum payload size, enforce timeouts
- **MITM Attacks**: Certificate pinning on first connection (TOFU - Trust On First Use)
- **Gossip Attacks**:
  - Message replay prevention via vector clocks and message IDs
  - TTL enforcement (24h max) prevents infinite propagation
  - Max hop count (5) limits amplification attacks
- **DHT Security**:
  - Peer verification via X.509 certificates
  - Bootstrap seed configuration prevents malicious DHT injection
  - Rate limiting on DHT queries

## 7. Implementation Notes

### Reference Implementation

The reference implementation is in Python using asyncio:

**Reading messages:**
```python
import asyncio
import json
from dpc_protocol.protocol import read_message

async def handle_connection(reader, writer):
    while True:
        message = await read_message(reader)
        if message is None:
            break  # Connection closed

        if message.get("command") == "HELLO":
            # Handle HELLO message
            pass
```

**Writing messages:**
```python
from dpc_protocol.protocol import write_message, create_hello_message

async def send_hello(writer, node_id, name):
    msg = create_hello_message(node_id, name)
    await write_message(writer, msg)
```

### Error Handling

Implementations must handle:
- **Incomplete reads**: Connection closed mid-message
- **Invalid JSON**: Malformed payload
- **Invalid header**: Non-numeric length field
- **Connection errors**: Network failures, timeouts

See `dpc-protocol/dpc_protocol/protocol.py` lines 83-103 for reference error handling.

### Extensions

DPTP is designed to be extensible. New commands can be added by:
1. Defining new `command` values
2. Documenting payload structure
3. Implementing handlers in client/server code

**Backward Compatibility:** Unknown commands should be logged and ignored (not cause connection termination).

## 8. Related Specifications

- **Hub API v1.0**: WebRTC signaling, OAuth, discovery - [hub_api_v1.md](./hub_api_v1.md)
- **Personal Context Model v2.0**: Structure of context data - See `dpc-protocol/dpc_protocol/pcm_core.py`
- **Device Context Schema v1.1**: Hardware/software context structure - [docs/DEVICE_CONTEXT_SPEC.md](../docs/DEVICE_CONTEXT_SPEC.md)
- **Privacy Rules Format**: Firewall configuration - See `~/.dpc/privacy_rules.json`

## 9. Changelog

### v1.3 (January 2026)
- **FILE_OFFER enhancement:**
  - Added `voice_metadata` field for voice message transfers (v0.13.0)
  - Includes duration_seconds, sample_rate, channels, codec, recorded_at timestamp
  - Enables audio preview with playback controls before accepting transfer
  - Reuses file transfer infrastructure (FILE_OFFER/FILE_CHUNK/FILE_COMPLETE)

### v1.2 (December 2025)
- **New message types: Vision & Image Support (v0.12.0)**
  - SEND_IMAGE - Remote vision inference with image payload
  - Reuses REMOTE_INFERENCE_RESPONSE for vision responses
  - Enables screenshot analysis, OCR, diagram interpretation, visual Q&A
- **New message types: Session Management (v0.12.0)**
  - PROPOSE_NEW_SESSION, VOTE_NEW_SESSION, NEW_SESSION_RESULT
  - Collaborative session reset with unanimous voting
  - Prevents accidental data loss in multi-party conversations
- **New message types: Chat History Synchronization (v0.12.0)**
  - REQUEST_CHAT_HISTORY, CHAT_HISTORY_RESPONSE
  - Automatic history sync on reconnect
  - Backend→frontend sync for page refresh scenarios
- **FILE_OFFER enhancement:**
  - Added `image_metadata` field for image transfers (v0.12.0)
  - Includes thumbnail, dimensions, source, captured_at timestamp
  - Enables rich image preview before accepting transfer

### v1.1 (December 2025)
- **Node ID format updated**: 32 hex characters (was 16) for DHT compatibility
- **Renamed commands**: GET_CONTEXT → REQUEST_CONTEXT, CONTEXT_DATA → CONTEXT_RESPONSE
- **New message types**: REQUEST_DEVICE_CONTEXT, DEVICE_CONTEXT_RESPONSE (hardware/software context sharing)
- **New message types**: CONTEXT_UPDATED (peer cache invalidation)
- **New message types**: GOSSIP_MESSAGE (epidemic routing), GOSSIP_SYNC (anti-entropy sync)
- **New message types**: FILE_OFFER, FILE_ACCEPT, FILE_CHUNK, FILE_COMPLETE, FILE_CANCEL (peer-to-peer file transfer with chunked delivery, v0.11.0)
- **Transport Layers section updated**: Added UDP DTLS, clarified IPv4 Direct is internet-wide, documented 6-tier fallback architecture
- **Connection Flow section updated**: Added UDP DTLS hole punch connection flow diagram
- **Security Considerations expanded**:
  - Authentication now covers Direct TLS, WebRTC DTLS, and UDP DTLS
  - Attack Mitigation includes Gossip attacks (replay, TTL, hop limits) and DHT security
  - File Transfer security (end-to-end encryption, SHA256 verification, firewall-gated)
- **Related Specifications updated**: Added Device Context Schema v1.1 reference

### v1.0 (November 29, 2025)
- Initial stable release
- Message types: HELLO, SEND_TEXT, GET_CONTEXT, CONTEXT_DATA
- Remote inference: REMOTE_INFERENCE_REQUEST, REMOTE_INFERENCE_RESPONSE
- Provider discovery: GET_PROVIDERS, PROVIDERS_RESPONSE
- Node identity system documented
- Connection flows for Direct TLS and WebRTC

## 10. License

This specification is released under **CC0 1.0 Universal (Public Domain)**.

You are free to implement DPTP in any programming language, for any purpose, without restrictions.

---

**Maintained by:** The D-PC Messenger Project
**Contact:** https://github.com/mikhashev/dpc-messenger
