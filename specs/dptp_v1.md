# DPTP Specification: D-PC Transfer Protocol v1.0

**Version:** 1.0
**Status:** Stable
**Date:** November 29, 2025
**License:** CC0 1.0 Universal (Public Domain)

## 1. Overview

The **D-PC Transfer Protocol (DPTP)** is a binary-framed, JSON-based messaging protocol designed for peer-to-peer communication in the D-PC Messenger ecosystem. DPTP enables secure, direct exchange of text messages, personal context data, and AI computation requests between cryptographically-identified nodes.

### Design Principles

- **Simplicity**: 10-byte fixed header + JSON payload
- **Extensibility**: JSON allows arbitrary message types and fields
- **Security**: Designed to run over TLS (Direct) or DTLS (WebRTC)
- **Asynchronous**: Built for async I/O with asyncio StreamReader/StreamWriter

### Transport Layers

DPTP messages are transmitted over two supported transport mechanisms:

1. **Direct TLS** - TCP connections secured with TLS 1.2+
   - Local network and IPv6 connections
   - Uses X.509 certificates for node identity
   - Lowest latency, requires network visibility

2. **WebRTC Data Channels** - Encrypted with DTLS
   - Internet-wide connections with NAT traversal
   - STUN/TURN servers for connection establishment
   - Hub provides signaling only (no message routing)

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
{"command":"HELLO","payload":{"node_id":"dpc-node-8b066c7f3d7eb627","name":"Alice"}}
```

Wire representation (82 bytes total):
```
0000000072{"command":"HELLO","payload":{"node_id":"dpc-node-8b066c7f3d7eb627","name":"Alice"}}
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
    "node_id": "dpc-node-8b066c7f3d7eb627",
    "name": "Alice"  // Optional display name
  }
}
```

**Fields:**
- `node_id` (string, required): Cryptographic node identifier
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

#### GET_CONTEXT

Requests the peer's personal context data (subject to firewall rules).

**Format:**
```json
{
  "command": "GET_CONTEXT"
}
```

**Response:** CONTEXT_DATA message

---

#### CONTEXT_DATA

Sends personal context data in response to GET_CONTEXT or proactively.

**Format:**
```json
{
  "command": "CONTEXT_DATA",
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

## 4. Node Identity System

### Node ID Format

Node IDs follow the format: `dpc-node-[16 hex characters]`

**Example:** `dpc-node-8b066c7f3d7eb627`

### Identity Generation

1. Generate RSA-2048 key pair
2. Create self-signed X.509 certificate with node_id as Common Name
3. Compute SHA256 hash of public key (PEM format)
4. Take first 16 hex characters of hash
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
     │  1. TCP + TLS Handshake               │
     │  (Verify cert CN matches node_id)     │
     ├───────────────────────────────────────>│
     │<──────────────────────────────────────┤
     │                                        │
     │  2. HELLO                              │
     ├───────────────────────────────────────>│
     │<──────────────────────────────────────┤
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
┌─────────┐         ┌─────────┐         ┌─────────┐
│  Alice  │         │   Hub   │         │   Bob   │
└────┬────┘         └────┬────┘         └────┬────┘
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

## 6. Security Considerations

### Encryption

- **Direct TLS**: All traffic encrypted with TLS 1.2+ (AES-256-GCM recommended)
- **WebRTC**: Data channels encrypted with DTLS (derived from SRTP)

### Authentication

- Nodes authenticate via X.509 certificates (Direct TLS)
- Certificate CN must match `node_id` in HELLO message
- No centralized PKI - trust established out-of-band

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
- **Privacy Rules Format**: Firewall configuration - See `~/.dpc/privacy_rules.json`

## 9. Changelog

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
