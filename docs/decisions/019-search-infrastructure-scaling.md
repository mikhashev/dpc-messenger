# ADR-019: Search Infrastructure Scaling — From Local to Federated

**Date:** 2026-04-28 (S77 Discussion)
**Status:** Accepted (architectural direction, implementation deferred to Track 2)
**Supersedes:** None
**Depends on:** ADR-010 (Memory Architecture), ADR-018 (Retrieval Upgrade), VISION.md, S32 Full Picture

## Context

During S77, a chain of discussions led from a specific technical question ("why are RRF scores 0.01-0.03?") to a fundamental architectural question ("how does search scale to 8 billion users?"). This document captures findings, decisions, and deferred questions from that discussion.

## Investigation: RRF Scoring

### Finding: Scores 0.01-0.03 are expected behavior

Active Recall uses Reciprocal Rank Fusion (RRF) to merge FAISS dense + sparse results:

```
RRF_score(d) = Σ weight / (k + rank_i(d) + 1)
```

With k=60 (standard from Cormack et al., SIGIR 2009):
- Best possible score: 1.5 / (60 + 0 + 1) = **0.0246**
- Typical range: 0.01-0.03

**Conclusion:** Low scores are mathematically correct. Relevance quality = ranking order, not absolute values. Not a bug.

### Reference
Cormack, G.V., Clarke, C.L.A., & Buettcher, S. (2009). "Reciprocal Rank Fusion Outperforms Condorcet and Individual Rank Learning Methods." SIGIR '09, 758-759. DOI: 10.1145/1571941.1572114

## Investigation: Active Recall Query Quality

### Finding: Topic dilution at 30-message window

**Test:** Mike asked "What do you know about BCI interfaces?" in a session 90% about ADR-018.
**Result:** 0/3 relevant hints. All returned ADR-018 files. Query vector was 90%+ ADR-018 topics.

**Root cause:** 30-message window concatenation averages semantic meaning. Last user message (topic switch) is drowned by 29 preceding messages about the previous topic.

**Analysis of 20 sessions (S43-S77):**
- Sessions are thematically coherent (~2 topic switches per session)
- Mike = topic switcher (short directives, 72% <100 chars)
- CC = topic continuer (technical reports, 46% of user-role messages)
- CC writes as `role: "user"` — indistinguishable from Mike without metadata

### Decision: Dual-query workaround (RECALL-QUERY)

**Implemented in commits 24b64ee, 2aa6370, f4e2724:**

- **Q1:** Last human message only (filter: `sender_node_id.startswith("dpc-node-")`)
- **Q2:** Last 10 messages (all senders)
- **Merge:** RRF with Q1 weight > Q2 weight
- **top_k:** 15 per query

**Verification:**
| Metric | Before (30-msg) | After (dual query) |
|--------|-----------------|-------------------|
| Topic switch (BCI) | 0/3 relevant | 0/15 (no BCI files in KB) |
| ADR-018 domination | 3/3 ADR-018 | 0/15 ADR-018 |
| Hints count | 7 | 15 |

**Deferred to Track 2:** Proper fix requires ADR-006 Step 3 (`participant_type` field) and/or group chat architecture where CC is not in agent chat.

### Finding: Extended paths indexed but not ranking

`personal-context-manager` IS in `indexed_paths` config and IS in FAISS index (513 EXT vectors). But `neurointerface-integration.md` doesn't appear in top-15 for "EEG" queries — ranking issue, not indexing issue.

**Deferred:** Increase top_k, verify embedding quality for specific documents.

## Architectural Analysis: Search at Scale

### BGE-M3 Capabilities

| Capability | Used? | Cross-node? | Notes |
|---|---|---|---|
| Dense (1024-dim) | YES | YES | Cosine similarity works across nodes |
| Sparse (lexical) | YES | NO | Corpus-dependent weights, local only |
| ColBERT (multi-vector) | NO | NO | ~100x storage, justified at 5000+ docs |

### Cross-node Search Architecture

**Key insight: dense and sparse scale differently.**

Dense vectors are portable (one model = one vector space). Sparse/BM25 are corpus-dependent (IDF varies by collection). Cross-node federated search = **dense-only** for inter-node queries, dense+sparse for local precision.

**Federated query pattern:**

```
Query on Node A:
├── Local: FAISS dense + sparse (full precision)
├── Dunbar Layer 1 (5 intimates): dense query shipping, top-15 each
├── Dunbar Layer 2 (50): dense query shipping, top-5 each
├── Dunbar Layer 3 (150): metadata filter → selective query
└── Layer 4+ (500+): gossip summaries, no direct search
```

**Query shipping:** send query vector (1024 floats = 4KB), remote node searches locally, returns top-K filenames + scores. Merge via RRF locally.

### Scaling Bottlenecks (ordered by scale)

| Scale | Bottleneck | Mitigation |
|---|---|---|
| Current (1 user, 682 docs) | None | — |
| Team (~20 nodes) | Query latency (20 parallel requests) | Timeout + partial results |
| Dunbar group (~150) | Metadata propagation overhead | Gossip summaries |
| Full network (~1500) | Model tiering, bloom filter freshness | Tiered models, periodic gossip |
| 8B users | Governance, deduplication, model consistency | Dunbar routing by design |

### ONNX as Infrastructure Choice

- Runs on any hardware (GPU/CPU/NPU/mobile)
- Same model = same results cross-platform
- No Python runtime at deployment
- BGE-M3 model ~600MB, downloaded once per node
- Scale = O(1) per node (local inference, no central server)

### Known Gaps vs. ADR-010 / VISION / S32

1. **Incremental indexing** — ADR-010 specifies "embed on write", current implementation does full rebuild on startup. CC confirms: 682 docs = ~10 min startup. At 5000 docs = ~1 hour. Hard prerequisite for scale.

2. **Self-contained knowledge units** — S32 §3.5 requires each unit usable without sender context. Current Active Recall hints (filename + description) require `read_file()`. Not self-contained.

3. **Tiered visibility** — ADR-010 Component 4 (Active/Recent/Stale) not implemented. Without it, index grows monotonically — at 1M files, 90% stale content.

4. **Sparse index format** — `sparse_index.json` is single file (~5MB at 682 docs). At 5000 docs = ~35MB. Needs SQLite or binary format.

5. **Batch embedding** — current pipeline embeds one document at a time. GPU batch processing (32-64 docs) = significant speedup.

6. **Model tiering** — BGE-M3 requires 2.2GB VRAM (GPU). Mobile/weak devices can't run it. Need smaller model or API fallback for federated search compatibility.

## Decisions

1. **RRF k=60** — confirmed correct, documented with academic reference
2. **Dual-query workaround** — implemented for topic dilution, to be replaced when ADR-006 Step 3 / Track 2 arrives
3. **Dense-only for cross-node** — sparse/BM25 are local-only by design
4. **Query shipping over index sharing** — aligns with ADR-010 "share text, not vectors" and privacy principles
5. **ONNX for all inference** — cross-platform, no central dependency

## Deferred to Track 2

- ADR-006 Step 3 (participant_type) — resolves CC-in-agent-chat root cause
- Group chat architecture — proper multi-participant model
- Federated search protocol — query shipping + Dunbar routing
- Knowledge governance at scale — deduplication, attribution, commons
- Incremental indexing — prerequisite for production scale
- ColBERT evaluation — when precision is insufficient at 5000+ docs

## Consequences

- Active Recall works for personal use (current state)
- Dual-query workaround handles topic switches adequately
- Search infrastructure (FAISS + BGE-M3 + ONNX) is correct foundation for Track 2
- Scaling path is clear: local → federated → global, via Dunbar routing
- No architectural rewrites needed — implementation gaps, not design flaws
