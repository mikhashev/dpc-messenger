# Remote AI Provider Selection - Feature Documentation

## Overview

This feature enables users to discover and select specific AI models from connected peers for remote inference. When Alice connects to Bob, she can see all AI models Bob has made available (respecting Bob's firewall settings) and choose which model to use for her queries.

## Architecture

### Protocol Layer

**New Messages (`dpc-protocol/dpc_protocol/protocol.py`):**

```python
def create_get_providers_message() -> Dict[str, Any]:
    """Request peer's available AI providers"""
    return {"command": "GET_PROVIDERS"}

def create_providers_response(providers: list) -> Dict[str, Any]:
    """Return available AI providers

    Args:
        providers: List of dicts with keys: alias, model, type
    """
    return {"command": "PROVIDERS_RESPONSE", "payload": {"providers": providers}}
```

### Backend Implementation

**Auto-Discovery (`dpc-client/core/dpc_client_core/p2p_manager.py`):**
- Lines 190-196 (Direct TLS connections)
- Lines 460-466 (WebRTC connections)

When a peer connection is established, the client automatically sends `GET_PROVIDERS` to discover available models.

**Firewall-Aware Provider Response (`dpc-client/core/dpc_client_core/service.py`):**
- Lines 1048-1099: `_handle_get_providers_request()`

Security checks:
1. ✅ Is `compute.enabled = true` in requester's `privacy_rules.json`?
2. ✅ Is requester in `allow_nodes` or `allow_groups`?
3. ✅ Filter by `allowed_models` (if specified)

**Provider Storage (`dpc-client/core/dpc_client_core/service.py`):**
- Lines 1101-1118: `_handle_providers_response()`

Stores peer providers in `peer_metadata` and broadcasts to UI.

### Frontend Implementation

**Store (`dpc-client/ui/src/lib/coreService.ts`):**
- Line 19: `export const peerProviders = writable<Map<string, any[]>>(new Map())`
- Lines 172-179: Event handler for `peer_providers_updated`

**UI Components (`dpc-client/ui/src/routes/+page.svelte`):**

1. **Compute Host Selector** (Lines 750-779)
   - Shows available models next to each peer name
   - Example: `Bob - llama3.1:8b, gpt-4`

2. **Model Selector Dropdown** (Lines 781-794)
   - Appears when remote peer selected
   - Lists all available models from that peer
   - Auto-selects first model when switching hosts

3. **Query Execution** (Lines 133-148)
   - Local inference: sends `provider` parameter
   - Remote inference: sends `compute_host` and `model` parameters

---

## Firewall Configuration

### Alice's Perspective (Sharing Her Compute)

**File:** `~/.dpc/privacy_rules.json`

```json
{
  "node_groups": {
    "friends": ["dpc-node-bob-456", "dpc-node-charlie-789"],
    "colleagues": ["dpc-node-dave-abc"]
  },
  "compute": {
    "_comment": "Enable compute sharing",
    "enabled": true,
    "allow_groups": ["friends"],
    "allow_nodes": ["dpc-node-eve-xyz"],
    "allowed_models": ["llama3.1:8b", "llama3-70b"]
  }
}
```

**Security Behavior:**

| Setting | Behavior |
|---------|----------|
| `enabled = false` | No one can access your compute (returns empty provider list) |
| `enabled = true, no allow_*` | No one can access (must specify who) |
| `allow_groups = friends` | Bob and Charlie can access (they're in friends group) |
| `allow_nodes = dpc-node-eve-xyz` | Eve can access regardless of groups |
| `allowed_models = llama3.1:8b` | Only advertise and allow this model |
| `allowed_models` not specified | All models available |

**Example Scenarios:**

**Scenario 1: Full Access to All Models**
```json
{
  "compute": {
    "enabled": true,
    "allow_nodes": ["dpc-node-bob-456"],
    "allowed_models": []
  }
}
// No allowed_models means Bob can use all your models
```
→ Bob sees: `llama3.1:8b, llama3-70b, gpt-4` (all your models)

**Scenario 2: Restricted Model Access**
```json
{
  "compute": {
    "enabled": true,
    "allow_nodes": ["dpc-node-bob-456"],
    "allowed_models": ["llama3.1:8b"]
  }
}
```
→ Bob sees: `llama3.1:8b` (only this model)

**Scenario 3: Group-Based Access**
```json
{
  "node_groups": {
    "friends": ["dpc-node-bob-456", "dpc-node-charlie-789"]
  },
  "compute": {
    "enabled": true,
    "allow_groups": ["friends"],
    "allowed_models": ["llama3.1:8b", "llama3-70b"]
  }
}
```
→ Bob and Charlie see: `llama3.1:8b, llama3-70b`
→ Dave (not in friends) sees: nothing

**Scenario 4: Compute Disabled**
```json
{
  "compute": {
    "enabled": false
  }
}
```
→ Everyone sees: nothing (compute sharing off)

---

## User Experience Flow

### 1. Connection Established

```
Alice → Bob (WebRTC via Hub)
  ↓
Bob receives GET_PROVIDERS request
  ↓
Bob checks firewall (privacy_rules.json):
  - compute.enabled = true ✅
  - Alice in allow_nodes ✅
  - allowed_models = llama3.1:8b, gpt-4 ✅
  ↓
Bob sends PROVIDERS_RESPONSE:
  [
    {alias: "ollama_local", model: "llama3.1:8b", type: "ollama"},
    {alias: "openai_gpt4", model: "gpt-4", type: "openai_compatible"}
  ]
  ↓
Alice's UI updates:
  - Compute Host dropdown shows: "Bob - llama3.1:8b, gpt-4"
```

### 2. Alice Selects Remote Compute

**UI State:**
1. Alice selects **Compute Host**: "Bob"
2. Model dropdown appears with: `ollama_local (llama3.1:8b)`, `openai_gpt4 (gpt-4)`
3. Alice selects **Model**: "gpt-4"
4. Alice types query: "Explain quantum computing"
5. Alice clicks Send

**Backend Flow:**
```javascript
// Frontend sends
{
  command: "execute_ai_query",
  payload: {
    prompt: "Explain quantum computing",
    compute_host: "dpc-node-bob-456",
    model: "gpt-4"
  }
}

// Backend routes to Bob
REMOTE_INFERENCE_REQUEST {
  request_id: "uuid-123",
  prompt: "Explain quantum computing",
  model: "gpt-4"
}

// Bob executes inference
Bob's LLMManager.query(prompt, provider_alias=None)
  → Uses gpt-4 model (matches model name in providers)

// Bob sends result back
REMOTE_INFERENCE_RESPONSE {
  request_id: "uuid-123",
  response: "Quantum computing is...",
  status: "success"
}

// Alice receives result
Alice's UI displays response
```

### 3. Firewall Rejection Scenario

```
Charlie → Alice (requests providers)
  ↓
Alice checks firewall:
  - compute.enabled = true ✅
  - Charlie NOT in allow_nodes ❌
  - Charlie NOT in allow_groups ❌
  ↓
Alice sends PROVIDERS_RESPONSE: []
  ↓
Charlie's UI shows: "Alice (no models available)"
```

---

## Testing Scenarios

### Test 1: Basic Remote Provider Selection

**Setup:**
- Two clients running (Alice, Bob)
- Bob's `~/.dpc/privacy_rules.json`:
  ```json
  {
    "compute": {
      "enabled": true,
      "allow_nodes": ["<alice-node-id>"]
    }
  }
  ```
- Bob has Ollama running with `llama3.1:8b`

**Test Steps:**
1. ✅ Alice connects to Bob via WebRTC
2. ✅ Check backend logs for "Requested AI providers from dpc-node-bob-..."
3. ✅ Check backend logs for "Received 1 providers from dpc-node-bob-..."
4. ✅ In Alice's UI, check Compute Host dropdown shows "Bob - llama3.1:8b"
5. ✅ Select Bob as compute host
6. ✅ Verify Model dropdown appears with "ollama_local (llama3.1:8b)"
7. ✅ Send AI query
8. ✅ Verify Bob's backend shows "Handling inference request from alice..."
9. ✅ Verify Alice receives response

**Expected Logs:**

*Alice's backend:*
```
✅ WebRTC connection established with dpc-node-bob-456
  - Requested AI providers from dpc-node-bob-456
  - Received 1 providers from dpc-node-bob-456
  - Requesting inference from peer: dpc-node-bob-456
  - Received inference result from dpc-node-bob-456
```

*Bob's backend:*
```
  - Handling GET_PROVIDERS request from dpc-node-alice-123
  - Sending 1 providers to dpc-node-alice-123 (filtered from 1 total)
  - Handling inference request from dpc-node-alice-123 (request_id: uuid-...)
  - Running inference for dpc-node-alice-123 (model: llama3.1:8b, provider: default)
  - Inference completed successfully for dpc-node-alice-123
```

---

### Test 2: Firewall Model Filtering

**Setup:**
- Bob has 3 providers: `llama3.1:8b`, `llama3-70b`, `gpt-4`
- Bob's firewall restricts models:
  ```json
  {
    "compute": {
      "enabled": true,
      "allow_nodes": ["<alice-node-id>"],
      "allowed_models": ["llama3.1:8b", "gpt-4"]
    }
  }
  ```

**Test Steps:**
1. ✅ Alice connects to Bob
2. ✅ Check Alice's UI shows only 2 models: "Bob - llama3.1:8b, gpt-4"
3. ✅ Verify `llama3-70b` is NOT shown
4. ✅ Try to select `llama3.1:8b` → should work
5. ✅ Try to select `gpt-4` → should work

**Expected Backend Logs:**
```
Bob's backend:
  - Handling GET_PROVIDERS request from dpc-node-alice-123
  - Sending 2 providers to dpc-node-alice-123 (filtered from 3 total)
```

---

### Test 3: Compute Access Denied

**Setup:**
- Bob's firewall:
  ```json
  {
    "compute": {
      "enabled": true,
      "allow_nodes": ["dpc-node-charlie-789"]
    }
  }
  // Alice is NOT in the allowed list
  ```

**Test Steps:**
1. ✅ Alice connects to Bob
2. ✅ Check Alice's UI shows: "Bob (no models available)"
3. ✅ Alice cannot select Bob for remote compute (no models to choose)

**Expected Backend Logs:**
```
Bob's backend:
  - Handling GET_PROVIDERS request from dpc-node-alice-123
  - Access denied: dpc-node-alice-123 cannot access compute resources
  - Sending 0 providers to dpc-node-alice-123 (filtered from 3 total)
```

---

### Test 4: Compute Sharing Disabled

**Setup:**
- Bob's firewall:
  ```json
  {
    "compute": {
      "enabled": false
    }
  }
  ```

**Test Steps:**
1. ✅ Alice connects to Bob
2. ✅ Check Alice's UI shows: "Bob (no models available)"

**Expected Backend Logs:**
```
Bob's backend:
  - Handling GET_PROVIDERS request from dpc-node-alice-123
  - Access denied: dpc-node-alice-123 cannot access compute resources
  - Sending 0 providers to dpc-node-alice-123 (filtered from 3 total)
```

---

### Test 5: Group-Based Access

**Setup:**
- Bob's firewall:
  ```json
  {
    "node_groups": {
      "friends": ["dpc-node-alice-123", "dpc-node-dave-xyz"]
    },
    "compute": {
      "enabled": true,
      "allow_groups": ["friends"]
    }
  }
  ```

**Test Steps:**
1. ✅ Alice connects to Bob → sees models ✅
2. ✅ Dave connects to Bob → sees models ✅
3. ✅ Charlie connects to Bob → sees "no models available" ❌

---

## Code References

| Component | File | Lines |
|-----------|------|-------|
| Protocol Messages | [dpc-protocol/dpc_protocol/protocol.py](../dpc-protocol/dpc_protocol/protocol.py) | 50-60 |
| GET_PROVIDERS Handler | [dpc-client/core/dpc_client_core/service.py](../dpc-client/core/dpc_client_core/service.py#L1048-L1099) | 1048-1099 |
| PROVIDERS_RESPONSE Handler | [dpc-client/core/dpc_client_core/service.py](../dpc-client/core/dpc_client_core/service.py#L1101-L1118) | 1101-1118 |
| Auto-Discovery (TLS) | [dpc-client/core/dpc_client_core/p2p_manager.py](../dpc-client/core/dpc_client_core/p2p_manager.py#L190-L196) | 190-196 |
| Auto-Discovery (WebRTC) | [dpc-client/core/dpc_client_core/p2p_manager.py](../dpc-client/core/dpc_client_core/p2p_manager.py#L460-L466) | 460-466 |
| Firewall Model Filtering | [dpc-client/core/dpc_client_core/firewall.py](../dpc-client/core/dpc_client_core/firewall.py#L296-L315) | 296-315 |
| Frontend Store | [dpc-client/ui/src/lib/coreService.ts](../dpc-client/ui/src/lib/coreService.ts#L19) | 19, 172-179 |
| UI - Compute Host Selector | [dpc-client/ui/src/routes/+page.svelte](../dpc-client/ui/src/routes/+page.svelte#L750-L779) | 750-779 |
| UI - Model Selector | [dpc-client/ui/src/routes/+page.svelte](../dpc-client/ui/src/routes/+page.svelte#L781-L794) | 781-794 |
| UI - Query Execution | [dpc-client/ui/src/routes/+page.svelte](../dpc-client/ui/src/routes/+page.svelte#L133-L148) | 133-148 |

---

## Security Considerations

### 1. Firewall is Mandatory
- Users MUST configure `privacy_rules.json` to share compute
- Default is deny-all (secure by default)
- Fine-grained control: per-node, per-group, per-model

### 2. Model-Level Authorization
- `allowed_models` setting restricts which models peers can use
- Prevents unauthorized access to expensive models (e.g., GPT-4)
- Empty list = all models allowed (for trusted peers)

### 3. No Provider Credential Exposure
- Peer only receives model names, not API keys or provider configs
- Inference executed on provider's machine with their credentials
- Requester never sees provider implementation details

### 4. Request Validation
- Every inference request checks firewall permissions
- Model parameter validated against `allowed_models`
- Access denied if peer removed from allowed list

---

## Future Enhancements

### 1. Provider Metadata Enrichment
- Add cost per token
- Add estimated latency
- Add GPU/CPU info
- Add max context length

### 2. Hub-Based Discovery
- Register available models with Hub
- Search for peers with specific models: `GET /discovery/compute?model=llama3.1:8b`
- Discover before connecting

### 3. Usage Tracking & Rate Limiting
- Track compute usage per peer
- Rate limiting: max N requests per hour
- Usage quotas in firewall config

### 4. Streaming Responses
- Stream tokens back to requester
- Better UX for long responses
- Requires protocol update to support chunked responses

### 5. Multi-Peer Load Balancing
- If multiple peers offer same model, distribute load
- Health checking and fallback

---

## Troubleshooting

### Issue: Peer shows "no models available" but should have access

**Check:**
1. Bob's `privacy_rules.json` has `compute.enabled = true`
2. Alice's node_id is in `allow_nodes` or she's in an `allow_groups` group
3. Bob's backend logs show "Sending N providers" where N > 0
4. Alice's backend logs show "Received N providers" where N > 0

**Debug:**
```bash
# On Bob's machine
cd dpc-client/core
poetry run python run_service.py

# Watch for:
#   - Handling GET_PROVIDERS request from dpc-node-alice-...
#   - Sending N providers (filtered from M total)
```

---

### Issue: Inference fails with "Access denied"

**Cause:** Firewall blocks inference request even though providers were advertised.

**Reason:** The `model` parameter in the inference request doesn't match `allowed_models`.

**Fix:**
```json
{
  "compute": {
    "allowed_models": ["llama3.1:8b"]
  }
}
// Ensure model is in allowed list - must match exactly
```

---

### Issue: Provider list not updating after changing `privacy_rules.json`

**Cause:** Providers are cached from initial connection.

**Fix:**
1. Disconnect and reconnect peers
2. OR restart both clients
3. Provider discovery happens during connection handshake

---

## Summary

This feature enables:
- ✅ Automatic discovery of peer AI models
- ✅ Firewall-based access control (node, group, model level)
- ✅ User-friendly model selection in UI
- ✅ Secure remote inference with authorization
- ✅ No credential exposure

All changes respect the privacy-first design philosophy of DPC Messenger.
