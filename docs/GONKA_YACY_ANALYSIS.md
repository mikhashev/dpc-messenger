# Gonka and YaCy Analysis — Alignment with DPC Philosophy

**Analyzed:** 2026-03-19

---

## TL;DR

| Project | Philosophy alignment | Practical value |
|---|---|---|
| **Gonka** | Low — blockchain-first GPU marketplace, privacy is secondary roadmap item | None direct; conceptually complementary but different product |
| **YaCy** | High — 20-year-old privacy-first P2P open source search engine, GPL, hub-optional, DHT-native | Several concrete patterns directly applicable to DPC's DHT, gossip, and peer discovery layers |

---

## Gonka

**Repo:** https://github.com/gonka-ai/gonka
**Tagline:** "Bitcoin for AI"
**License:** Mixed (components under MIT, LGPL-3, GPL-3)
**Stack:** Go (Cosmos-SDK/Tendermint), Python (vLLM, PyTorch, CUDA), Kotlin (tests)

### What it is

A decentralized GPU compute marketplace. GPU owners ("Hosts") contribute compute power; developers submit AI training/inference jobs. The blockchain (Cosmos L1) handles coordination, payment, and proof-of-work. All compute happens off-chain; the chain records proofs and payment artifacts.

Key mechanism: **Proof of Work 2.0 / Sprint Mechanism** — instead of hash-based PoW, consensus weight is earned by running actual transformer inference. Nodes compete in time-bound "Sprints." Nearly 100% of GPU cycles go to real AI tasks, not cryptographic waste.

### Why it's not philosophically aligned with DPC

| Dimension | Gonka | DPC |
|---|---|---|
| Privacy | Inference payloads **visible to node operators**. Confidential computing listed as a near-term roadmap item — not yet shipped. | End-to-end encrypted throughout. Server never sees messages or context. |
| Personal data | No concept. Stateless API requests. | Personal Context Model is the core product feature. |
| Human messaging | Not a messaging platform. | Central feature. |
| P2P model | Cosmos/Tendermint blockchain P2P for consensus. No direct user-to-user communication. | Direct TLS/WebRTC/UDP between users. |
| Decentralization | Marketplace governance is decentralized; inference execution is centralized per-request (whole model on one node). | Connections are decentralized; Hub is optional. |
| Philosophy | "Monetize GPU idle time" | "Your context never leaves your device" |

Gonka solves *who runs the GPU*. DPC solves *who sees your conversation*. The threat models are perpendicular.

### What can be noted (not reused)

The **Sprint Mechanism** (PoW via real inference rather than hash computation) is an intellectually interesting economic design. If DPC ever introduces a voluntary compute-sharing reward mechanism, the principle of "prove work by doing real inference" avoids the waste of hash-based PoW. But this is a product-level decision, not a technical one to implement now.

---

## YaCy

**Repo:** https://github.com/yacy/yacy_search_server
**Tagline:** "Free Distributed Web Search"
**License:** GPL 2.0+
**Stack:** Java 11+, Apache Solr/Lucene (local index), custom Kelondro DHT store
**Maturity:** 20+ years in production, 14,800+ commits, 3,800+ stars, 1 billion+ documents indexed across the freeworld network

### What it is

A fully decentralized peer-to-peer web search engine. No central server. Every user runs their own node that crawls, indexes, and participates in a shared global DHT index. Censorship is structurally impossible — no one controls what gets indexed.

Two modes: **Global P2P** (participates in the `freeworld` DHT network) and **Private/intranet** (local-only, never shares). Hub-optional by design from day one.

### Why it's philosophically aligned with DPC

- Privacy-first: search term hashing, no centralized query logs, structural impossibility of query history reconstruction
- Fully open source (GPL 2.0+), no commercial dependency
- Hub-optional: global network is optional; private/intranet mode works without any central server
- P2P-native: DHT-based peer discovery and data distribution, not blockchain coordination
- 20 years of solving the exact problems DPC faces: NAT traversal, peer discovery without a central server, distributed data storage, eventual consistency

---

## YaCy Patterns Directly Applicable to DPC

### 1. Tiered Peer Roles (Junior / Senior / Principal)

YaCy formalizes peer capability as a named status tier:

| Tier | Capability |
|---|---|
| Junior | Behind NAT. Can push data out but cannot receive remote queries. Must contact others proactively. |
| Senior | Publicly reachable. Full DHT participant — receives index fragments, answers remote queries. |
| Principal | Senior that also publishes a live seed list at a public HTTP URL. Acts as bootstrap for new peers. |

**DPC parallel:** The `ConnectionOrchestrator` already tries strategies in fallback order, but peers have no formal identity tier. The connection mode is negotiated per-connection. Formalizing peer tiers would let DPC:
- Peers behind strict NAT self-identify as Junior and never advertise as relay candidates
- Publicly reachable peers self-identify as Senior and volunteer as relays more confidently
- The Hub is already a Principal — explicitly naming this in the protocol makes it clear the Hub is one bootstrap option among potentially many

This maps directly onto DPC's existing 6-tier connection hierarchy: the tier isn't about connection strategy, it's about what role a node can play for others.

### 2. Seed List Bootstrapping with Redundancy

YaCy's bootstrap chain:
1. Hardcoded fallback seed URLs in source (`network.unit.bootstrap.seedlist0 = http://...`)
2. Principal peers publish live seed lists at public HTTP URLs
3. After enough peer exchanges, a node can rejoin without seed servers at all

After each peer contact, the node sends its own seed data and receives recent seeds from the contacted peer. Over time, the local peer cache becomes self-sufficient.

**DPC parallel:** DPC's Hub is currently the only bootstrap path. YaCy's model suggests:
- The Hub is `seedlist0`
- Community-run Hub mirrors can be `seedlist1`, `seedlist2`
- `known_peers.json` already persists peer data — make it a local seed list, consulted first before hitting the Hub
- Peers with public IPs could optionally publish a seed file (a URI in their HELLO payload), making them Principal peers

This makes DPC progressively more Hub-independent the longer a peer has been active. New users still need the Hub; established users with a healthy `known_peers.json` can reconnect without it.

### 3. DHT for Data Placement, Not Just Peer Discovery

YaCy's DHT determines *where index fragments live* — each word hash maps to the peer(s) responsible for storing that word's entries. The data migrates to where the DHT says it should be. Retrieval goes directly to those peers.

**DPC parallel:** DPC already uses DHT for:
- Relay discovery (finding relay candidates)
- Certificate publication (`cert:<node_id>`)
- STUN-like endpoint discovery for hole punching

YaCy's model suggests extending deterministic DHT placement to:
- **Offline message queues** — instead of flooding gossip to 3 random peers, store at the DHT-determined peer for the recipient's node ID. Any peer knowing the recipient's node ID can query the same location.
- **Capability announcements** — `COMPUTE_ADVERTISE` results stored at deterministic DHT locations, discoverable without broadcasting

This complements the existing gossip store-and-forward (Priority 6) with a more structured, queryable store.

### 4. "Dissolvable" Local Data Once Replicated

YaCy's RWI index dissolves: once an index fragment is successfully replicated to the DHT-target peers (confirmed by `dhtredundancy` count), the local copy is deleted. The invariant is clean — data lives at exactly the right places.

**DPC parallel:** DPC's gossip store-and-forward uses TTL (24h) and max-hops (5) but keeps messages locally until TTL expires. A YaCy-style confirmation model for the gossip protocol would:
- Track acknowledgment from N DHT-target peers for an offline message
- Delete the local copy once N acks received
- Prevent the gossip store from growing unbounded on busy relay nodes

The current model is simpler and more resilient to peer churn; this would be an optimization for long-lived nodes with stable storage.

### 5. Compressed Abstracts for Multi-Key Intersection

For multi-word queries, YaCy avoids fetching thousands of full results and intersecting them locally. Instead:

1. Fetch compact "index abstracts" for each query word from respective DHT peers — compressed lists of URL hashes grouped by host hash
2. Intersect the abstracts locally (cheap, compact data)
3. Fetch full results only for the confirmed intersection

**DPC parallel:** When a peer issues `GET_PROVIDERS` seeking a node that has model X AND is in group Y AND has >8GB VRAM, DPC currently broadcasts the full query. A YaCy-style abstract mechanism would:
- Each capability attribute (model, group membership, VRAM tier) has a DHT location
- Fetch compact peer-hash lists for each attribute from respective DHT locations
- Intersect locally to find candidate nodes
- Fetch full `COMPUTE_ADVERTISE` details only for candidates

This is only valuable at scale (large teams). For now, it's an architectural pattern to keep in mind for the `GET_PROVIDERS` protocol design.

### 6. Query Hashing for DHT Privacy

YaCy sends **hashed word tokens** over the DHT, never plaintext queries. No single peer in the DHT path knows both the full query and the requester's identity.

**DPC parallel:** DPC already hashes context with SHA-256 for cache invalidation. Extending query hashing to DHT capability lookups:
- A node searching for a peer with `model:llama3:70b` sends the hash of `model:llama3:70b` to the DHT, not the string
- The DHT peer holding that location cannot determine what model was requested without knowing the pre-image
- Capability ads are stored under their hash; matching is exact

This is privacy-preserving for capability discovery without any protocol-level encryption overhead.

### 7. Peer DNA for Quality-Aware Selection

YaCy's peer seed contains compact performance self-reporting:
- `ISpeed` — indexing speed (pages/minute)
- `RSpeed` — retrieval speed (queries/minute)
- `Uptime` — total seconds online
- `LCount` — stored link count (capacity proxy)
- `ICount` — indexed word count (contribution proxy)

These are used for quality-aware peer selection — faster, more available peers preferred for DHT queries.

**DPC parallel:** DPC's `relay_manager.py` already uses quality scoring (uptime 50%, capacity 30%, latency 20%). YaCy's model suggests including self-reported performance metrics in the `HELLO` handshake payload so peers can make better routing decisions without separate measurement rounds. A `PeerCapabilityHint` in HELLO:

```python
{
  "command": "HELLO",
  "payload": {
    "node_id": "...",
    "display_name": "...",
    "capabilities": {
      "relay": true,
      "compute": true,
      "uptime_seconds": 86400,
      "avg_latency_ms": 45,
      "queue_depth": 2          # current relay load
    }
  }
}
```

This is cheap to add and immediately improves relay/compute peer selection.

### 8. The MCP Bootstrap Pattern (YaCy Grid)

YaCy Grid's Master Connect Program (MCP) — a bootstrap broker that tells microservices where actual infrastructure lives, then steps out of the data path — is architecturally identical to what DPC's Hub does for WebRTC signaling.

YaCy Grid validates the design choice DPC already made: the Hub is a signaling coordinator, not a message router. Once peers know each other's addresses and have established keys, the Hub is irrelevant. YaCy's 20-year production experience confirms this architecture works at scale.

---

## Comparison Summary

| Pattern | YaCy | DPC (current) | DPC (opportunity) |
|---|---|---|---|
| Peer tiers | Junior/Senior/Principal | Implicit in connection strategy | Formalize in HELLO payload |
| Bootstrap | Seed URLs + peer exchange | Hub only | Hub as `seedlist0` + known_peers fallback |
| DHT data placement | Index fragments at deterministic locations | cert + relay discovery only | Offline message queues, capability ads |
| Data dissolution | Delete local after N ack | TTL-based expiry | Ack-based deletion for gossip |
| Multi-key intersection | Abstract + intersect + fetch | Full broadcast query | Future GET_PROVIDERS optimization |
| Query hashing | Word hashes over DHT | SHA-256 for cache only | Hash capability keys for DHT lookups |
| Peer self-description | DNA with performance metrics | node_id + display_name only | Add capability hints to HELLO |
| Hub role | MCP (bootstrap only) | Signaling + OAuth | Validated as correct architecture |

---

## Recommended Near-Term Action: Capability Hints in HELLO

The lowest-friction change with immediate benefit is adding an **optional** `capabilities` field to the `HELLO` message. This costs nothing for existing peers (field is absent = no capabilities) and enables:

1. Relay candidates can self-identify instead of requiring a separate DHT lookup
2. Compute peers advertise rough VRAM tier without a full `COMPUTE_ADVERTISE` round-trip
3. Uptime and queue depth let the `ConnectionOrchestrator` make smarter fallback decisions

This is YaCy's Peer DNA pattern applied to DPC's handshake protocol. One dict field, backward-compatible, immediate value.

---

## References

- **YaCy repository:** https://github.com/yacy/yacy_search_server (GPL 2.0)
- **YaCy community:** https://community.searchlab.eu
- **Gonka repository:** https://github.com/gonka-ai/gonka
- **Gonka tokenomics:** https://github.com/gonka-ai/gonka/blob/main/docs/tokenomics.md
- **DPC internal:**
  - `connection_orchestrator.py` — tier formalization target
  - `peer_cache.py` / `known_peers.json` — seed list candidate
  - `managers/relay_manager.py` — quality scoring (extend with self-reported metrics)
  - `managers/gossip_manager.py` — dissolution / ack-based deletion target
  - `message_handlers/hello_handler.py` — capability hints addition target
  - `dpc-protocol/dpc_protocol/protocol.py` — HELLO payload schema
