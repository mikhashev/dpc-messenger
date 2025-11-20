# Remote Inference - Share Compute Power with Trusted Peers

**Status:** ✅ MVP Implemented (v0.6.1)
**Feature Type:** Collaborative Computing

## Overview

Remote Inference enables users to **share their AI computational power** with trusted peers over secure P2P connections. A user with a weak device can request AI inference from a peer with a powerful GPU, while keeping all data private within the end-to-end encrypted P2P channel.

This is one of the **dual killer features** of D-PC Messenger, enabling users to pool their computational resources within a trusted network.

## Use Case Example

**Scenario:** Game Dev Team Brainstorming

- **Anna** has a powerful PC with a large AI model (Llama3-70B)
- **Bob** has a laptop with no local AI
- They're both working on a game design document

**With Remote Inference:**
1. Bob connects to Anna via P2P (Direct TLS or WebRTC)
2. Bob enables compute sharing for Anna in his firewall config
3. Bob asks a complex question about game mechanics
4. Bob selects "Anna" as the compute host in the UI
5. The query runs on Anna's powerful model
6. Bob gets a high-quality response without needing his own GPU

**Result:** Bob contributes his creative knowledge, Anna contributes her computational power. The entire interaction remains private and encrypted.

---

## Architecture

### Protocol Layer

**New Message Types:**
- `REMOTE_INFERENCE_REQUEST` - Request inference from a peer
- `REMOTE_INFERENCE_RESPONSE` - Return inference result or error

**Message Format:**
```json
{
  "command": "REMOTE_INFERENCE_REQUEST",
  "payload": {
    "request_id": "uuid-here",
    "prompt": "What are some creative quest ideas for...",
    "model": "llama3-70b",  // optional
    "provider": "ollama_local"  // optional
  }
}
```

```json
{
  "command": "REMOTE_INFERENCE_RESPONSE",
  "payload": {
    "request_id": "uuid-here",
    "status": "success",
    "response": "Here are three creative quest ideas..."
  }
}
```

### Firewall Integration

**Compute Sharing Permissions** (`~/.dpc/.dpc_access.json`):
```json
{
  "compute": {
    "enabled": true,
    "allow_groups": ["friends"],
    "allow_nodes": ["dpc-node-alice-123"],
    "allowed_models": ["llama3.1:8b", "llama3-70b"]
  }
}
```

**Permission Checks:**
- `can_request_inference(node_id, model)` - Check if peer can request inference
- `get_available_models_for_peer(node_id)` - List models peer can use

### Service Layer

**New Methods in CoreService:**
- `send_ai_query(prompt, compute_host, model, provider)` - Public API for AI queries
- `_request_inference_from_peer(peer_id, prompt, ...)` - Request remote inference
- `_handle_inference_request(peer_id, request_id, ...)` - Handle incoming requests
- `execute_ai_query(...)` - Updated to support compute_host parameter

**Request-Response Pattern:**
- Uses `asyncio.Future` for async request matching
- Tracks pending requests in `_pending_inference_requests` dict
- 60-second timeout for inference operations

### UI Layer

**Compute Host Selector:**
- Dropdown in AI chat shows "Local" + all connected peers
- Only visible in `local_ai` chat mode
- Automatically populates from `peer_info` in node status
- Persists selection across messages

---

## Configuration

### Enabling Compute Sharing

Edit `~/.dpc/.dpc_access.json` to enable compute sharing:

```json
{
  "_comment": "Firewall access control configuration",
  "compute": {
    "_comment": "Compute sharing settings (Remote Inference)",
    "enabled": true,
    "allow_groups": ["friends", "colleagues"],
    "allow_nodes": ["dpc-node-alice-abc123"],
    "allowed_models": ["llama3.1:8b", "llama3-70b"]
  },
  "node_groups": {
    "friends": ["dpc-node-alice-abc123", "dpc-node-bob-def456"],
    "colleagues": ["dpc-node-charlie-ghi789"]
  }
}
```

### Security Considerations

**Access Control:**
- Compute sharing is **disabled by default**
- Must explicitly enable and specify allowed peers
- Can restrict which models peers can use
- All requests go through firewall permission checks

**Privacy:**
- All prompts are sent over encrypted P2P connections
- No data passes through the Hub
- Compute host only sees the final assembled prompt (not raw context data)

**Resource Management:**
- Inference timeout: 60 seconds (configurable)
- Failed requests return clear error messages
- No automatic retries (user must retry manually)

---

## Usage

### From the UI

1. **Enable Compute Sharing** (Host Side):
   - Edit `~/.dpc/.dpc_access.json`
   - Add `"compute"` section with permissions
   - Restart the client

2. **Connect to Peer**:
   - Use Direct TLS (`dpc://...`) or WebRTC (node_id)
   - Ensure peer appears in "Connected Peers" list

3. **Select Compute Host**:
   - Click on "🤖 Local AI Assistant" chat
   - Use dropdown: "🖥️ Compute Host"
   - Select peer from list

4. **Send Query**:
   - Type your question
   - Press Enter or click "Send"
   - Query runs on selected peer's hardware

### From Python API

```python
# Local inference
result = await core_service.send_ai_query(
    prompt="What is the capital of France?"
)

# Remote inference
result = await core_service.send_ai_query(
    prompt="Generate three creative quest ideas...",
    compute_host="dpc-node-alice-abc123",
    model="llama3-70b"
)
```

---

## Error Handling

**Common Errors:**

1. **Access Denied**
   - Error: "Access denied: You are not authorized to request inference"
   - Solution: Host must add your node_id to `allow_nodes` or `allow_groups`

2. **Peer Not Connected**
   - Error: "Peer dpc-node-xxx is not connected"
   - Solution: Establish P2P connection first

3. **Timeout**
   - Error: "Inference request timed out after 60s"
   - Solution: Check if peer's AI model is running and responsive

4. **Model Not Available**
   - Error: "Model 'llama3-70b' not found"
   - Solution: Peer must have the specified model installed

---

## Performance

**Latency:**
- Direct TLS (LAN): ~50-200ms overhead
- WebRTC (Internet): ~100-500ms overhead
- Inference time: Depends on model and prompt complexity

**Bandwidth:**
- Request: ~1-10 KB (prompt + metadata)
- Response: ~1-50 KB (depends on response length)

**Comparison:**
- **Local inference**: 0ms network overhead
- **Remote inference**: Small network overhead, but access to powerful models
- **Cloud API**: Higher cost, privacy concerns, vendor lock-in

---

## Implementation Status

### ✅ Completed (v0.6.1)

- [x] Protocol message types (`REMOTE_INFERENCE_REQUEST/RESPONSE`)
- [x] Firewall compute permissions (`[compute]` section)
- [x] Service layer handlers and request/response matching
- [x] Public API (`send_ai_query`, `execute_ai_query`)
- [x] UI compute host dropdown selector
- [x] Error handling and timeout management

### 🔲 Future Enhancements (v0.7+)

- [ ] Hub-advertised compute resources (model discovery)
- [ ] Model capability negotiation (which models peer has)
- [ ] Streaming responses (for long-form generation)
- [ ] Usage tracking and rate limiting
- [ ] Cost-sharing mechanisms (for commercial deployments)
- [ ] Multi-hop inference (chain multiple peers)
- [ ] Inference result caching

---

## Testing

### Manual Testing

**Test 1: Basic Remote Inference**
```bash
# Terminal 1: Start Host (powerful PC)
cd dpc-client/core
# Edit ~/.dpc/.dpc_access.json to enable compute sharing
poetry run python run_service.py

# Terminal 2: Start Requestor (weak laptop)
cd dpc-client/core
poetry run python run_service.py

# UI:
# 1. Connect both clients to each other (Direct TLS or WebRTC)
# 2. In requestor's UI, select host from compute dropdown
# 3. Send a query - should run on host's hardware
```

**Test 2: Access Denied**
```json
// Host: Disable compute sharing in ~/.dpc/.dpc_access.json
{
  "compute": {
    "enabled": false
  }
}

// Requestor: Try to request inference
// Expected: Error "Access denied: You are not authorized..."
```

**Test 3: Timeout Handling**
```bash
# Host: Stop LLM service (e.g., stop Ollama)
# Requestor: Try to request inference
# Expected: Error "Inference request timed out after 60s"
```

### Automated Testing

```bash
# Run remote inference tests
cd dpc-client/core
poetry run pytest tests/test_remote_inference.py -v
```

---

## Troubleshooting

### "Access denied" errors

**Check firewall config on host:**
```bash
cat ~/.dpc/.dpc_access.json
# Ensure "compute" section exists and enabled = true
# Ensure requestor is in allow_nodes or allow_groups
```

### Requests timeout

**Check host's LLM service:**
```bash
# For Ollama:
ollama list  # List available models
ollama serve  # Ensure service is running
```

### Peer not in dropdown

**Check P2P connection:**
- Verify peer appears in "Connected Peers" sidebar
- Check connection type (Direct TLS or WebRTC)
- Try reconnecting if connection was dropped

---

## Security Best Practices

1. **Only enable compute sharing for trusted peers**
   - Remote inference exposes your GPU/CPU to peer's prompts
   - Use `allow_nodes` or tight `allow_groups` restrictions

2. **Monitor resource usage**
   - Check CPU/GPU usage when providing compute to peers
   - Set up resource limits at OS level if needed

3. **Review allowed models**
   - Restrict expensive models to close collaborators
   - Use `allowed_models` to limit which models peers can access

4. **Regular firewall audits**
   - Review `.dpc_access.json` periodically
   - Remove nodes that are no longer trusted

---

## Contributing

The remote inference feature is open for community contributions:

- **Feature requests:** [GitHub Issues](https://github.com/mikhashev/dpc-messenger/issues)
- **Bug reports:** Include logs from both host and requestor
- **Enhancements:** PRs welcome (see `CONTRIBUTING.md`)

---

## References

- **Product Vision:** [PRODUCT_VISION.md](../PRODUCT_VISION.md) - Original vision for compute sharing
- **Firewall Guide:** [CONFIGURATION.md](./CONFIGURATION.md) - Complete firewall configuration
- **Protocol Spec:** [specs/dptp_v1.md](../specs/dptp_v1.md) - Message protocol details
- **Architecture:** [README_WEBRTC_INTEGRATION.md](./README_WEBRTC_INTEGRATION.md) - P2P architecture

---

**Made with ❤️ by the D-PC Community**

*Remote Inference: Your friend's GPU is your GPU (with permission).*
