# DPTP Specification: D-PC Transfer Protocol v1.1

**Version:** 1.1
**Status:** In Progress
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

### v1.1 (December 2025) - IN PROGRESS
- **Node ID format updated**: 32 hex characters (was 16) for DHT compatibility
- **Renamed commands**: GET_CONTEXT → REQUEST_CONTEXT, CONTEXT_DATA → CONTEXT_RESPONSE
- **New message types**: REQUEST_DEVICE_CONTEXT, DEVICE_CONTEXT_RESPONSE (hardware/software context sharing)
- **New message types**: CONTEXT_UPDATED (peer cache invalidation)
- **New message types**: GOSSIP_MESSAGE (epidemic routing), GOSSIP_SYNC (anti-entropy sync)
- **Transport Layers section updated**: Added UDP DTLS, clarified IPv4 Direct is internet-wide, documented 6-tier fallback architecture
- **Connection Flow section updated**: Added UDP DTLS hole punch connection flow diagram
- **Security Considerations expanded**:
  - Authentication now covers Direct TLS, WebRTC DTLS, and UDP DTLS
  - Attack Mitigation includes Gossip attacks (replay, TTL, hop limits) and DHT security
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
