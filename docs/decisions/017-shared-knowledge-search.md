# ADR-017: Shared Knowledge Search

**Status:** Accepted
**Date:** 2026-04-25
**Session:** S71
**Authors:** Mike (direction, shared inference insight), Ark (architecture, privacy analysis, scenario flow), CC (technical feasibility, code analysis)

## Context

DPC nodes accumulate knowledge through signed knowledge commits stored locally. Currently, there is no mechanism for one node to search another node's knowledge base. P2P-KNOW-1 research (S71) evaluated YaCy (No-Go — Java monolith, privacy misalignment) and DPC's Kademlia DHT (not designed for content discovery).

Mike's insight: reuse the existing **shared inference pattern** (remote LLM query) for **shared knowledge search** (remote FAISS+BM25 query). Same architecture, different payload.

## Decision

### Protocol: Stateless Pull Model

Two new DPTP message types over existing TLS P2P connections:

**KNOWLEDGE_SEARCH_REQUEST:**
```json
{
  "command": "KNOWLEDGE_SEARCH_REQUEST",
  "payload": {
    "query": "distributed consensus algorithms",
    "max_results": 5
  }
}
```

**KNOWLEDGE_SEARCH_RESPONSE:**
```json
{
  "command": "KNOWLEDGE_SEARCH_RESPONSE",
  "payload": {
    "results": [
      {
        "topic": "Byzantine Fault Tolerance",
        "summary": "Analysis of BFT protocols...",
        "confidence": 0.87,
        "timestamp": "2026-03-15",
        "commit_hash": "a1b2c3",
        "access": "summary"
      }
    ],
    "total_local": 7,
    "returned": 5
  }
}
```

### Embedding Strategy: B (Text Query + Local Embedding)

- Sender sends **plaintext query text** (not embedding)
- Receiver computes embedding locally with own model
- Receiver searches own FAISS+BM25 index (guaranteed compatible)
- No model negotiation required

**Rationale:** For Track 2 (Team Collaboration), peers are trusted — plaintext query visibility is acceptable. This eliminates all embedding model compatibility issues.

**Future (Phase 3+):** Strategy C — embedding-based queries with privacy tiers per Dunbar layer. Deferred until multi-hop discovery beyond trusted peers.

### Access Control: Firewall-Filtered Results

Receiver's firewall determines what to return based on requester's Dunbar tier:
- Layer 5 (intimates): full content
- Layer 15 (best friends): metadata + summaries
- Layer 50 (good friends): topic names + confidence
- Layer 150 (friends): topic names only
- Beyond: not served (no connection)

### Content Retrieval: Existing File Transfer

If requester wants full knowledge commit content, uses existing `KNOWLEDGE_REQUEST` → `FILE_OFFER` → chunked transfer flow. Human approval gate preserved.

## Flow

```
Node A → KNOWLEDGE_SEARCH_REQUEST {query, max_results} → Node B (TLS)
  Node B: firewall check → embed locally → FAISS+BM25 search → filter by Dunbar tier
Node B → KNOWLEDGE_SEARCH_RESPONSE {results} → Node A (TLS)
  Node A: reviews results → optionally requests full content
Node A → FILE_OFFER flow (existing) for full content retrieval
  Node B: firewall approves → FILE_OFFER → chunked transfer (existing protocol)
```

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|-------------|
| YaCy integration | Java monolith, DHT incompatible, privacy misaligned |
| Extend Kademlia DHT | Wrong tool — exact-key lookup, no content search, no access control |
| Catalog exchange (push model) | Stateful, stale metadata, proactive disclosure violates sovereignty |
| Fixed embedding model | Lock-in, migration = rebuild all indexes |
| Model negotiation | Premature complexity for MVP |

## Scope

~80-100 lines, 2-3 files:
- `message_handlers/knowledge_search_handler.py` (~40 lines — request + response handlers)
- Firewall integration (~10 lines in `firewall.py`)
- FAISS+BM25 wiring (~30 lines in `knowledge_service.py`)
- Handler registration (~2 lines in `service.py`)

Template: `RemoteInferenceRequestHandler` + `RemoteInferenceResponseHandler` pattern.

## ROADMAP Placement

Track 2: Team Collaboration #9. Multi-hop relay = "Where This Leads → Network Effects" (Phase 3+).

## Dependencies

- ADR-010 (Agent Memory Architecture) — FAISS index must be operational on receiver
- ADR-018 (Retrieval Infrastructure Upgrade) — BGE-M3 recommended embedding model with whole-document indexing; BM25 retained as CPU-only fallback
- Existing P2P TLS connections — transport layer
- Existing firewall rules — access control

## Research

Full research documentation: `ideas/dpc-research/p2p-knowledge-discovery/` (5 documents, S71).

## Consequences

**Positive:**
- Nodes can search each other's knowledge bases over encrypted P2P connections
- Each node controls what it shares via existing firewall rules
- No new infrastructure or dependencies required
- Knowledge stays local — only metadata/summaries travel over the wire
- Human approval gate preserved for full content access

**Negative / Trade-offs:**
- Plaintext query visible to receiving peer (acceptable for trusted peers in Track 2; mitigated by Strategy C embedding-based queries in Phase 3+)
- Receiver must have FAISS+BM25 index operational (ADR-010 dependency)
- No result quality guarantee — different embedding models produce different relevance rankings across nodes
