# Team Compute Pools — Feature Specification

**Status:** 🔲 Planned (Phase 2.3)
**Target Version:** v0.21.0
**Timeline:** Month 5–7 2026
**Complexity:** Medium
**Depends On:** Feature #1 (Persistent Team Management), Feature #2 (Group Chat ✅)

---

## Overview

Team Compute Pools extends the existing Remote Inference MVP (v0.6.1) with automatic peer selection, capability advertisement, and load balancing across team members. Instead of manually choosing a compute host from a dropdown, users can target a **team pool** and let the system pick the best available peer.

**Core promise:** "Your team's GPUs are your GPU — transparently."

---

## Problem Statement

### How it works today (v0.19.1)

When a user wants to run inference on a peer's hardware:

1. User opens the **"AI Host"** dropdown in `ProviderSelector.svelte` and selects a connected peer
2. UI sends `query_remote_providers` → backend sends `GET_PROVIDERS` to that peer
3. Host filters its local `providers.json` models by firewall rules (`compute.enabled`, `allow_nodes`, `allowed_models`) and replies with `PROVIDERS_RESPONSE`
4. UI populates a second dropdown with the host's permitted models (prefixed `remote:{node_id}:{alias}`)
5. User picks a model; UI sends `execute_ai_query` with `compute_host` + `provider` fields
6. Backend sends `REMOTE_INFERENCE_REQUEST` → host runs `llm_manager.query()` → returns `REMOTE_INFERENCE_RESPONSE`

The host fully controls what models are advertised — the remote user only sees what the host's firewall permits. Provider metadata (alias, model name, type, vision/voice support) is fetched on-demand and cached in `peer_metadata[peer_id]["providers"]`.

### Current limitations

| Problem | Impact |
|---------|--------|
| Manual peer selection required | User must pick a specific peer from the dropdown; no automatic routing |
| Model list fetched on-demand, not pre-advertised | First dropdown open triggers a `GET_PROVIDERS` round-trip (10s timeout) |
| No failover — timeout on busy/offline peer | Silent 60s wait on `REMOTE_INFERENCE_REQUEST`, then error |
| No load visibility | Can't tell if Anna is already running 3 inference jobs before sending |
| Peer selection resets on disconnect | If Anna goes offline, `selectedComputeHost` resets to "local" silently |

These are tolerable for 1:1 compute sharing. They become friction in a team of 5–20 where multiple members have capable hardware and should be interchangeable compute sources.

---

## Design Goals

1. **Transparent selection** — user targets a team pool, not a specific peer
2. **Capability matching** — only route to peers that have the requested model
3. **Automatic failover** — if top candidate is busy or times out, try next in ranked list
4. **Privacy-preserving** — capability ads carry no sensitive data; existing firewall controls are respected
5. **Zero new infrastructure** — no coordinator node, no central broker; built on DHT + existing P2P

### Non-goals

- Real-time queue synchronization (overkill for 2–20 person teams)
- Preemptive scheduling or job queuing
- Cost accounting / fairness tracking (Phase 3 consideration)
- Sub-second scheduling latency (inference takes seconds, scheduling overhead is noise)

---

## Architecture

### New Component: `ComputePoolManager`

**File:** `dpc-client/core/dpc_client_core/managers/compute_pool_manager.py`

Responsibilities:
- Maintain a local `peer_capability_cache`: `{peer_id → PeerCapabilityAd}`
- Send `COMPUTE_ADVERTISE` on startup and on change (model loaded/unloaded, VRAM freed)
- Receive and cache `COMPUTE_ADVERTISE` from connected peers
- Expose `select_peer(team_id, model, provider)` — ranked selection with failover

### Capability Advertisement

A lightweight, privacy-safe struct peers broadcast on connect and periodically (every 60s):

```python
@dataclass
class PeerCapabilityAd:
    node_id: str
    team_ids: List[str]           # which teams this ad is visible to (firewall-gated)
    available_models: List[str]   # e.g. ["llama3:8b", "llama3:70b"]
    free_vram_gb: float           # approximate, rounded to nearest 0.5 GB
    queue_depth: int              # active inference jobs (0–N)
    avg_latency_ms: int           # rolling average of recent inference times
    timestamp: float              # unix timestamp of last update
```

**What is NOT included:** prompt content, context data, user identity beyond `node_id`.

**New protocol message:**

```json
{
  "command": "COMPUTE_ADVERTISE",
  "payload": {
    "team_ids": ["team-abc123"],
    "available_models": ["llama3:8b", "llama3:70b"],
    "free_vram_gb": 10.5,
    "queue_depth": 1,
    "avg_latency_ms": 4200,
    "timestamp": 1748000000.0
  }
}
```

### Peer Selection Algorithm

Mirrors the proven relay scoring pattern from `relay_manager.py`:

```python
def score_peer(ad: PeerCapabilityAd) -> float:
    # Capacity: penalise heavily when queue is full
    capacity = max(0.0, 1.0 - ad.queue_depth / MAX_QUEUE_DEPTH)  # MAX_QUEUE_DEPTH = 3

    # VRAM: normalise against requested model's minimum VRAM
    vram_ok = 1.0 if ad.free_vram_gb >= model_min_vram_gb else 0.0

    # Latency: normalise 0–30s to 0.0–1.0 (inference latency, not network)
    latency_score = max(0.0, 1.0 - ad.avg_latency_ms / 30_000)

    return (capacity * 0.5) + (vram_ok * 0.3) + (latency_score * 0.2)
```

**Selection flow in `select_peer()`:**

1. Get all cached ads for `team_id`
2. Filter: `model in ad.available_models` and firewall `can_request_inference(peer_id)`
3. Filter: `ad.timestamp` fresher than 90s (stale ad = peer treated as offline)
4. Sort by `score_peer()` descending
5. Return ordered candidate list (not just top-1) for failover

### Failover in `InferenceOrchestrator`

`_execute_remote_inference()` currently sends to one peer and raises on timeout. New behaviour:

```
candidates = pool_manager.select_peer(team_id, model)
for peer_id in candidates:
    try:
        result = await _request_inference_from_peer(peer_id, ..., timeout=60s)
        pool_manager.record_success(peer_id, latency)
        return result
    except (TimeoutError, ConnectionError):
        pool_manager.record_failure(peer_id)
        continue
raise NoAvailableComputeError(f"All {len(candidates)} pool members failed or unavailable")
```

Maximum candidates tried: 3 (avoids long cascading delays for the user).

---

## Protocol Changes

**New message type** added to `dpc-protocol/dpc_protocol/protocol.py`:

| Command | Direction | Purpose |
|---------|-----------|---------|
| `COMPUTE_ADVERTISE` | broadcast to connected peers | Announce capability on connect, every 60s, and on change |

**Existing messages unchanged** — `REMOTE_INFERENCE_REQUEST/RESPONSE` are reused as-is.

---

## Firewall Integration

No new firewall fields required. Existing `compute` section already controls who can request inference from a peer:

```json
{
  "compute": {
    "enabled": true,
    "allow_groups": ["team-alpha"],
    "allow_nodes": [],
    "allowed_models": ["llama3:8b", "llama3:70b"]
  }
}
```

`COMPUTE_ADVERTISE` messages are only sent to peers that pass `can_request_inference()` — capability data is not broadcast to untrusted connections.

---

## UI Changes

### "Team Compute" panel (new, Phase 2.3)

Location: Sidebar section below "Connected Peers", visible only when user is in ≥1 team.

Displays per-team:
- Member name + node ID (truncated)
- Model list badges
- Free VRAM indicator (e.g. `10.5 GB free`)
- Queue depth dot (●●○○ = 2/4 slots used)
- Last-seen timestamp

### Compute host dropdown update

Existing `ProviderSelector.svelte` dropdown gains a new option group:

```
🖥️ Compute Host
─────────────────
  Local
─────────────────
  Team Alpha Pool  ← NEW: targets ComputePoolManager
─────────────────
  Anna (llama3:70b, 10GB free)
  Bob  (llama3:8b, 4GB free)
```

Selecting a pool option routes through `ComputePoolManager.select_peer()` instead of hardcoding `peer_id`. Individual peer entries remain for manual override.

---

## Backend Files to Create / Modify

| File | Change |
|------|--------|
| `managers/compute_pool_manager.py` | **New** — `PeerCapabilityAd`, cache, scoring, selection |
| `message_handlers/compute_handler.py` | **New** — `ComputeAdvertiseHandler` |
| `inference_orchestrator.py` | **Modify** — failover loop, pool routing path |
| `service.py` | **Modify** — initialise `ComputePoolManager`, wire handler, periodic advertise task |
| `settings.py` | **Modify** — `[compute_pool]` config section |
| `dpc-protocol/dpc_protocol/protocol.py` | **Modify** — add `COMPUTE_ADVERTISE` command constant |

## Frontend Files to Modify

| File | Change |
|------|--------|
| `src/lib/components/ProviderSelector.svelte` | Add "Team Pool" option group |
| `src/lib/coreService.ts` | Handle `compute_pool_updated` event, expose pool store |
| `src/routes/+page.svelte` | Render Team Compute panel in sidebar |

---

## Configuration

New section in `~/.dpc/config.ini`:

```ini
[compute_pool]
# Advertise capability to team pools (requires compute.enabled = true in privacy_rules.json)
advertise_enabled = true

# How often to re-broadcast capability ad to connected peers (seconds)
advertise_interval = 60

# Treat capability ad as stale after this many seconds (peer considered offline)
stale_threshold = 90

# Maximum peers to try in failover sequence before giving up
max_failover_attempts = 3
```

---

## What This Does NOT Replace

| Existing feature | Status |
|-----------------|--------|
| Manual peer dropdown | Kept — individual peer entries remain for explicit override |
| `remote_peer` provider type in `providers.json` | Kept — static configs still work for power users |
| Firewall compute permissions | Unchanged — pool selection fully respects existing rules |
| 60s per-request timeout | Unchanged per attempt; failover adds up to `3 × 60s` worst case |

---

## Implementation Notes

### Why not use DHT for capability advertisement?

DHT is appropriate for offline/hub-free peer discovery (as used by relay and gossip). For compute pools, peers are **already connected** — broadcasting over existing P2P connections is lower-latency, requires no DHT writes, and avoids exposing capability data to non-team DHT nodes.

### Why cap failover at 3 attempts?

Worst case: `3 × 60s = 180s` user wait. Beyond that, the team simply doesn't have available compute right now and the user should know immediately rather than wait. The cap is configurable via `max_failover_attempts`.

### VRAM rounding

`free_vram_gb` is rounded to the nearest 0.5 GB before advertising — consistent with the privacy-tier approach used in `device_context.json` for RAM.

### Queue depth accuracy

Queue depth is a **local counter** (`len(self._active_inference_requests)`), not a globally synchronized value. It will be slightly stale by the time a request arrives. This is acceptable — perfect accuracy is not needed for a soft preference signal.

---

## Running Models Larger Than Any Single Peer's VRAM

Team Compute Pools solve load distribution (routing queries to the best single peer). A separate question is whether peers' GPUs can be **combined** to run a model too large for any one machine — e.g., a 70B model requiring 48 GB VRAM when the team's largest GPU is 24 GB.

Two approaches are realistic at team scale.

---

### Path A — Transparent multi-GPU host (works today, zero protocol changes)

A peer who owns multiple GPUs (or uses CPU/RAM offload) can run vLLM or llama.cpp with tensor parallelism **locally** and expose a standard OpenAI-compatible endpoint. DPC routes to this peer exactly like any other remote provider — no new protocol messages, no shard coordination.

**Example setup on Anna's machine:**

```
Anna's workstation:
  GPU 0: RTX 4090  24 GB  ─┐
  GPU 1: RTX 3090  24 GB  ─┤─ vLLM (tensor_parallel_size=2) → /v1/completions
  System RAM: 128 GB       ─┘  (llama.cpp can offload layers to RAM)
```

Anna configures her `providers.json`:

```json
{
  "alias": "llama3_70b_tp2",
  "type": "openai_compatible",
  "model": "llama3:70b",
  "base_url": "http://localhost:11434/v1",
  "context_window": 128000
}
```

Anna's firewall permits her team to request inference from this provider. When Bob selects "Team Alpha Pool" and the query requires `llama3:70b`, `ComputePoolManager.select_peer()` routes to Anna. Anna's vLLM handles the two-GPU split invisibly. Bob's experience is identical to any other remote inference request.

**Why this is the preferred approach:**
- No changes to DPC protocol, orchestrator, or firewall schema
- vLLM and llama.cpp already implement battle-tested tensor and pipeline parallelism
- Works today: Anna can set this up with the current v0.19.1 client
- `COMPUTE_ADVERTISE` in Phase 2.3 will advertise `llama3:70b` as available once vLLM loads it, making pool selection seamless

**Advertising multi-GPU capacity correctly:**

When the `ComputePoolManager` reads `free_vram_gb` from a peer running vLLM across two GPUs, it should report the **combined** free VRAM. The advertiser (Anna's node) is responsible for summing across devices before broadcasting:

```python
# In ComputePoolManager._build_capability_ad()
free_vram = sum(gpu.free_vram_gb for gpu in detected_gpus)  # 10.5 + 8.0 = 18.5 → rounded to 18.5
```

This is already consistent with how `device_context_collector.py` handles multi-GPU machines.

---

### Path B — Pipeline parallelism across separate peer machines (Phase 3, LAN only)

When no single peer has sufficient VRAM — even with multiple GPUs — it becomes theoretically possible to split model layers across **different machines**. Peer A holds layers 0–34, Peer B holds layers 35–69; activations flow from A to B during each forward pass.

**Why this is hard and deferred:**

| Constraint | Detail |
|------------|--------|
| **Bandwidth** | Activations for a 70B forward pass are ~140 GB at fp16. A gigabit home connection (125 MB/s) makes per-layer transfer take minutes. |
| **Latency** | Token generation is sequential — each token requires a full round-trip through all peers before the next token starts. 3 peers × 20ms network latency = 60ms overhead per token. |
| **LAN requirement** | Only viable on 10 GbE or faster local networks. Attempting this over the internet produces worse throughput than running a smaller model locally. |
| **Framework change** | Current `LLMManager` uses Ollama/OpenAI API — opaque, no layer-level control. Would require switching to direct vLLM or llama.cpp integration. |
| **Protocol additions** | Needs a binary activation streaming protocol (tensors are not JSON-serialisable at this scale); new shard assignment in firewall rules. |

**What would be required (tracked as a Phase 3 investigation):**

1. `LLMManager` gains a `vllm_direct` provider type that talks to a co-located vLLM process via its Python API (not HTTP)
2. New `DISTRIBUTED_INFERENCE_SHARD` protocol message with binary payload (msgpack or raw bytes, not JSON)
3. `DistributedInferenceOrchestrator` coordinates the multi-peer forward pass and reassembles the response
4. Firewall schema extension: `compute.shard_assignments` maps layer ranges to specific node IDs
5. LAN detection guard: DPC refuses to initiate pipeline parallelism if any peer is reachable only over WAN
6. `COMPUTE_ADVERTISE` extended with `shard_capable: bool` and `available_layer_budget: int`

**Triggering condition for Phase 3 work:** A team member requests `llama3:70b` (or larger), no single peer in the pool has sufficient VRAM (including multi-GPU Path A), and at least two peers are on the same LAN segment.

---

### Decision matrix

| Scenario | Approach | Status |
|----------|----------|--------|
| Anna has 2× RTX 4090 (48 GB total), runs vLLM | Path A — route to Anna via pool | Works today (v0.19.1) |
| No peer has >24 GB, team wants 70B | Path A — Anna uses llama.cpp RAM offload | Works today with performance trade-off |
| 3 peers on 10 GbE LAN, want to pool all VRAM | Path B — pipeline parallelism | Phase 3 investigation |
| 3 peers on internet, want to pool all VRAM | Not viable | Network bandwidth is the hard limit |

---

## Relationship to Dynamo/KVBM

NVIDIA Dynamo's KV Block Manager targets datacenter-scale disaggregated prefill across hundreds of workers with NVLink/InfiniBand interconnects. DPC's team compute pools target 2–20 trusted peers over commodity networks. The scoring algorithm above (`capacity × 0.5 + vram × 0.3 + latency × 0.2`) covers the entire scheduling problem at this scale. No external scheduler is warranted.

**Dynamo as a transparent backend (Path A variant):** If a team member runs a Dynamo + vLLM cluster behind an OpenAI-compatible endpoint, DPC routes to it exactly like any other provider — no protocol changes needed. The `COMPUTE_ADVERTISE` message will carry whatever models that cluster exposes. This makes DPC a natural front-end for teams that have access to shared datacenter resources alongside personal workstations.

---

## Testing Plan

### Unit tests

- `test_peer_scoring.py` — scoring formula edge cases (queue full, VRAM insufficient, stale ad)
- `test_compute_pool_manager.py` — selection, cache expiry, failover ordering
- `test_compute_advertise_handler.py` — firewall-gated ad acceptance

### Integration tests

```bash
# Two-peer pool: one busy, failover to second
poetry run pytest tests/test_team_compute_pool.py::test_failover_on_timeout -v

# Pool with no capable peers (all busy or wrong model)
poetry run pytest tests/test_team_compute_pool.py::test_no_available_compute -v
```

### Manual testing checklist

- [ ] Anna advertises llama3:70b → Bob's "Team Alpha Pool" shows Anna with correct VRAM
- [ ] Bob selects "Team Alpha Pool", sends query → routes to Anna automatically
- [ ] Anna's queue_depth = 3 (MAX) → Bob routes to Charlie instead
- [ ] Anna goes offline → ad goes stale after 90s → removed from pool candidates
- [ ] Bob selects Anna individually (manual override) → bypasses pool logic

---

## References

- `docs/REMOTE_INFERENCE.md` — existing MVP (v0.6.1)
- `dpc-client/core/dpc_client_core/managers/relay_manager.py` — scoring pattern to reuse
- `dpc-client/core/dpc_client_core/inference_orchestrator.py` — entry point for modifications
- `ROADMAP.md` Phase 2.3, Feature #10
- `PRODUCT_VISION.md` — "The only messenger that lets you borrow your friend's GPU"
