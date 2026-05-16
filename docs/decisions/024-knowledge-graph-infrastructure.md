# ADR-024: Knowledge Graph as Unified Infrastructure

**Status:** Accepted (Phase 1 implemented, Phase 2 partial, Grafeo migration Phase 2 started S123)
**Date:** 2026-05-05
**Session:** S93
**Authors:** CC (draft, code analysis, web research), Ark (prior art research, GRAVITON mapping, dotmd analysis), Mike (direction, scale framing, decision)
**Depends on:** ADR-010 (Agent Memory Architecture), ADR-018 (Retrieval Upgrade), ADR-019 (Search Scaling), ADR-022 (Multi-Agent Safety)
**Supersedes:** None (extends ADR-019 Phase 3)

## Context

Six independent research tracks in `ideas/dpc-research/` converged on the same architectural gap: DPC's flat-file knowledge storage lacks structural relationships between knowledge units, memory layers, and participants. Each track proposed ad-hoc solutions (summary bridges, staleness heuristics, metadata propagation). This ADR proposes a unified graph layer that serves all six tracks.

### Trigger

S93 discussion: a-a-k criticism on Karpathy gist identified two gaps in DPC — cascade updates and cross-layer retrieval. Independent research by CC and Ark both converged on graph as the structural solution. Mike's question "how does this scale from 1 to 8B?" reframed the scope from single-node fix to architectural direction.

### Six Research Tracks Unified by Graph

| Track | Problem | Graph Solution |
|---|---|---|
| Knowledge Freshness (S84) | Map-territory gap, stale knowledge | Temporal edges with bi-temporal validity, decay scoring |
| Multi-Agent Safety (ADR-022) | Governance audit trail | Provenance edges (`decided_by`, `approved_by`), consequence walk |
| Multi-Agent Coordination (S87-91) | Shared knowledge between agents | Typed edges (`shared_with`, `consensus_reached`), graph schema as coordination protocol |
| P2P Knowledge Discovery (ADR-017) | Cross-node knowledge search | Subgraph federation, schema descriptors for routing |
| Search Infrastructure (ADR-018/019) | Retrieval quality + scaling | Graph traversal as 4th RRF channel, hop-bounded expansion |
| Perceptron Predictor (S90) | Agent wake prediction at scale | Graph community detection as feature source |

## Prior Art

### Production Systems

**Graphiti/Zep** (arxiv:2501.13956, Apache 2.0, github.com/getzep/graphiti, 14K+ stars):
- Temporal knowledge graph with bi-temporal model (`t_event` + `t_transaction`)
- Edge invalidation instead of deletion — history preserved
- Hybrid retrieval: semantic + BM25 + graph traversal
- Backend options: Neo4j, FalkorDB, LadybugDB (embedded)
- P95 latency <300ms in production

**Mem0** (arxiv:2504.19413, Apache 2.0):
- Dual-store: vector DB + knowledge graph
- ADD-only extraction (non-destructive, aligns with DPC conversation-as-source)
- Multi-signal retrieval: semantic + BM25 + entity matching, fused
- LoCoMo 91.6, LongMemEval 93.4, <7K tokens per retrieval

**dotmd** (github.com/inventivepotter/dotmd, MIT):
- Markdown knowledgebase: semantic + BM25 + graph traversal → RRF fusion → cross-encoder rerank
- Storage: LanceDB (vectors) + LadybugDB (graph) + SQLite (metadata)
- Entity extraction: structural (headings, wikilinks, tags) + GLiNER NER (zero-shot)
- Reference implementation closest to DPC's architecture

**Keppi** (github.com/jgoldfed/keppi, Python, 12 stars):
- Weighted directed graph over Obsidian vault — closest architectural analog to ADR-024
- 5 edge types with explicit weights: wikilink(1.0), embed(1.5), related_to(2.0), tag_overlap(0–0.5 Jaccard), folder_proximity(0.3)
- SQLite + sqlite-vec, BFS with relevance decay (`relevance = parent_relevance × edge_weight`)
- blast-radius (impact analysis), context-pack (token-budgeted assembly for AI), gaps (missing connections), communities (Louvain)
- Production validated: 1,471 notes, 267,581 edges
- MCP server (19 tools)
- Source: Karpathy gist comment by author (jgoldfed), README verified S97

**Lore** (re-cinq.com/blog/lore-recinqs-sw-agent-factory):
- Org-scale shared context MCP server for Claude Code (15+ developers, GKE)
- PostgreSQL + pgvector, HNSW + BM25 + RRF fusion (same pattern as ADR-018)
- Temporal fact invalidation via embedding similarity (0.92 threshold)
- Episode ingestion: enforced workflow (assemble_context → search_memory → write_episode)
- Key lesson: wrapping black-box agents failed; direct API calls + structured outputs succeeded
- Source: re-cinq blog post, verified S97

**obra/knowledge-graph** (github.com/obra/knowledge-graph, Jesse Vincent):
- TypeScript + better-sqlite3 + sqlite-vec (384-dim, MiniLM-L6-v2, 22MB quantized)
- Unweighted edges (wiki links only), FTS5 full-text search
- graphology: Louvain communities, betweenness centrality, PageRank, BFS
- MCP server for Claude Code (10 tools), incremental indexing by mtime
- Source: GitHub, verified S97

**All-Mem** (arxiv:2603.19595):
- Visible surface + hop-bounded expansion model
- DPC knowledge = visible surface, archives = deep evidence

**EMem** (arxiv:2511.17208):
- EDUs (enriched elementary discourse units) — non-compressive decomposition
- Graph-based associative recall over atomic event-like propositions

**grafeo-memory** (github.com/GrafeoDB/grafeo-memory, Apache 2.0, S123 research):
- AI memory layer for LLM applications: fact extraction, deduplication, semantic search
- Reconciliation loop: Extract → Search existing → Reconcile (LLM decides ADD/UPDATE/DELETE) → Execute
- Borrowing candidates: `reconciliation_threshold`, `agreement_bonus`, `topology_boost_factor`
- Limitations vs DPC: LLM-dependent (not offline), single-user, no provenance chain, no sovereignty model
- Reference architecture for reconciliation patterns, not drop-in replacement

### Internal Prior Art

**GRAVITON** (agent sandbox, April 2026):
- Knowledge graph spec synthesized by agents (GR, JARVIS, Hope)
- 6-axis scoring, 8-level confidence, exponential decay, exempt nodes, consequence walk
- Reference document, one input among several

## Decision

### 1. Adopt graph as unified knowledge infrastructure

Graph is the structural layer connecting memory layers, knowledge units, agents, and nodes.

### 2. Graph backend selection — Grafeo selected (S123)

**Decision:** Grafeo selected as graph backend. Migration started S123 on branch `feature/grafeo-backend`.

**Rationale:** HNSW + BM25 + RRF native (3 systems → 1), Cypher support, cross-platform PyPI wheels, Apache-2.0, active development.

**Grafeo v0.5.42 Source-Code Verification (S115)**

Full source-code review + live GitHub issue/PR verification.

**Code quality:** Rust workspace, `unsafe_code = "deny"`, MVCC transactions, WAL checkpoint recovery, columnar storage with zone maps.

**Resolved since S94:** #335 fixed (TopK operator), #318 = enhancement not bug, bus factor grew to 4+ contributors.

**Feature verification:**
- HNSW + BM25 + RRF native
- Bi-temporal properties + time travel
- Backup/restore (full, incremental, point-in-time)
- Python API: PyO3 bindings, NetworkX bridge, CDC streaming, MCP server
- Cross-platform wheels (Windows/macOS/Linux/x64/aarch64/musl)

**Encryption at rest — CORRECTION (S123):**

Grafeo Rust workspace has `encryption` Cargo feature (AES-256-GCM, Argon2id). However:

1. **Python binding does not expose encryption.** PyPI wheel has no encryption parameter.
2. **Grafeo Security Best Practices docs** explicitly state: "No encryption at rest — Database files are not encrypted" and recommend application-level Fernet.
3. **Conclusion:** Encryption at rest is NOT a currently available Grafeo capability for Python users.

**DPC encryption strategy (decided S123):**
- **A_v2:** Defer DB-level encryption until Grafeo adds Python binding support.
- **F:** Recommend OS-level full-disk encryption (BitLocker/LUKS/FileVault) in deployment docs.
- Application-level Fernet available as future option.
- `encryption_key` parameter removed from `GrafeoGraphBackend.__init__`.

**Migration design decisions (S123):**

| Decision | Choice | Rationale |
|---|---|---|
| D1: node_type mapping | Label = node_type.value (A) | Canonical LPG, enables label index |
| D2: properties storage | Opaque JSON string (a) | Parity with SQLite, no field collisions |
| D3: node_id uniqueness | Simple INSERT (upsert → 2.5) | Tests don't push duplicates |
| Query language | Cypher for queries, direct API for CRUD | Portability + clean CRUD |
| Node identity | node_id as property, MATCH lookup | Grafeo generates internal IDs |

**Candidates eliminated (S94):** LadybugDB (throughput collapse), SparrowDB/ocpg (fabricated), FalkorDB Lite (no Windows), Neo4j (too heavy), NetworkX (no persistence).

### 3. Graph layer supplements existing retrieval

Extended pipeline: `query → [FAISS + BM25 + Graph] → RRF fusion → Active Recall hints`

### 4. Graph schema

**Node types:** KnowledgeFile, SessionArchive, Entity, Decision, Agent

**Edge types:** DERIVED_FROM, DEPENDS_ON, RESPONDS_TO, CONTRADICTS, SUPPORTS, DECIDED_BY, SHARED_WITH, MENTIONS, TEMPORAL_NEXT

**Edge properties:** t_created, t_invalidated, confidence, justification (min 20 chars), edge_weight

**Node properties:** exempt (boolean), source_layer (L5/L6/L7/EXT)

**Temporal decay:** Phase 1-2 = bi-temporal recording only. Phase 4+ = query-time decay (Cypher WHERE on age).

### 5. Edge extraction pipeline

Three-stage: structural (zero-cost, deterministic) → GLiNER (NER, opt-in) → guided LLM (during sleep consolidation). GLiNER feeds entity list to LLM as scaffolding ("Guided Relation Extraction").

**Persistence invariants (S112):**
- Skip-orphan policy for MENTIONS edges
- Transactional safety via backend-internal `with self._conn:`
- Group archive coverage gap (policy decision needed)

### 6. Scaling model

| Scale | Graph behavior |
|---|---|
| 1 user, 120 docs | Local embedded Grafeo, <3ms queries |
| Team (~20) | Query shipping + consent gate |
| Dunbar (~150) | Schema descriptors as gossip summaries |
| 8B | Dunbar routing, sovereign subgraphs |

### 7. Integration with existing ADRs

**ADR-010:** Graph edges bridge memory layers. **ADR-018:** L7 as 4th RRF channel. **ADR-019:** Grafeo + Cypher + federation. **ADR-022:** Provenance edges for audit trail.

## Implementation Phases

### Phase 0: Smoke Test — DONE (S115)
- [x] Grafeo source-code review
- [x] Issue/PR verification
- [x] Cross-platform wheel verification
- [x] Encryption capability audit (S123: NOT available in Python)
- [ ] Live benchmark on DPC-scale data (post-Release/0.5.43)

### Phase 1: Foundation — DONE
- [x] `knowledge_graph.py` with abstract GraphBackend ABC
- [x] SQLiteGraphBackend implementation
- [x] Structural edge extraction
- [x] Graph as 4th RRF channel (`"L7": 0.6`)

### Phase 1.5: Grafeo Migration — IMPLEMENTATION COMPLETE (S123–S125)

Branch: `feature/grafeo-backend`

**Done:**
- [x] GrafeoGraphBackend stub, 13 ABC methods (commit `cb8fe5c`)
- [x] Phase 2 (S123): `__init__`, `init_schema` (no-op), `add_node` (direct API → upgraded to MERGE upsert in S125 review), `get_node` (Cypher), `node_count` (property) — commit `6444aa4`
- [x] Phase 2.5 Group A (S125): `add_edge`, `get_edges`, `get_neighbors`, `edge_count`, `edge_exists`, `close` — commit `e0a9a79`
- [x] Phase 2.5 Group B (S125): `clear_structural_edges`, `update_edge_timestamp_for_node` + Group A review-note comments — commit `93f015f`
- [x] Phase 2.5 Group C (S125): `bulk_upsert_entities_with_mentions` (three-phase UNWIND batch) — commit `cf444b5`
- [x] Full parity test suite: 72/72 green (36 tests × 2 backends)
- [x] `grafeo>=0.5.42` optional extra in pyproject.toml

**Deferred (not blocking merge):**
- [ ] Property index on `node_id` — Grafeo `create_property_index('Entity', 'node_id')` etc. Not blocking correctness; matters for performance at scale.
- [ ] `_add_edge_safe` race condition in `KnowledgeGraph` (high-level wrapper, not backend) — flagged by Ark S125; non-blocker for single-threaded agent. Tracked in backlog as GRAFEO-RACE-AWARENESS.

### Phase 2: Enrichment — PARTIAL
- [x] GLiNER entity extraction (operational since S112)
- [ ] Guided LLM relation extraction (end-to-end verification pending)
- [x] Bi-temporal recording on edges

### Phase 3: Federation
- Graph schema descriptors for P2P gossip
- Cross-node edge metadata shipping with consent gate
- Subgraph query protocol (Cypher query shipping)

### Phase 4: Advanced
- Query-time decay scoring
- GRAVITON 6-axis scoring
- Consequence walk for safety audit
- Perceptron predictor using graph features

## Risks

1. **Graph DB maturity (MEDIUM):** Grafeo v0.5.x, 4+ contributors, good code quality. Encryption NOT available in Python binding. Mitigation: ABC abstraction allows one-class backend swap.
2. **Cold start:** Mitigated by structural extraction (zero-cost).
3. **Hammer/nail bias:** BM25 remains primary. Graph = supplement.
4. **LLM hallucinated alternatives (S94):** SparrowDB/ocpg fabricated. Verify before considering.
5. **Schema evolution:** Start minimal, extend incrementally.
6. **Persistence cascade bugs (S111-S112):** Monitor KG edge delta per sleep cycle.
7. **Encryption gap (S123):** KG plaintext on disk. Mitigated by OS FDE.

## Open Questions

1. Graph vs `_meta.json` + `_index.md` coexistence? (Recommend: coexist, migrate when stable)
2. Quality bar for LLM-extracted edges?
3. Federation edge sharing: per-edge or per-type consent?
4. ~~Knowledge commit → graph edges?~~ **Answered S112:** indirect via file indexing.
5. Grafeo migration Phase 2.5 next. Benchmark pending post-Release/0.5.43.
6. **grafeo-memory reference architecture (S123):** Reconciliation patterns identified for potential borrowing. Detailed source-code research deferred.

## References

- Graphiti/Zep: arxiv:2501.13956, github.com/getzep/graphiti
- Mem0: arxiv:2504.19413, mem0.ai
- All-Mem: arxiv:2603.19595
- EMem: arxiv:2511.17208
- MemFactory: arxiv:2603.29493
- dotmd: github.com/inventivepotter/dotmd
- LadybugDB: github.com/LadybugDB/ladybug
- GRAVITON: internal agent sandbox spec (April 2026)
- Kuzu archived: theregister.com/2025/10/14/kuzudb_abandoned
- DPC Full Picture: ideas/dpc-full-picture/dpc-full-picture-s32.md
- VISION.md: project root
- Grafeo: github.com/GrafeoDB/grafeo, Apache-2.0
- grafeo-memory: github.com/GrafeoDB/grafeo-memory, Apache-2.0 (S123)
- segundo-cerebro: github.com/orobsonn/segundo-cerebro (`why` field on edges)
- S94 external validation: 2 independent LLMs + HN community + Karpathy gist 699 comments

### FusionBrain Lab references (S121)

FusionBrain Lab (AIRI/Innopolis), Andrey Kuznetsov as Head of Lab:

- KG Embeddings as Additional Modality: arxiv:2411.11531
- LLM-Microscope: arxiv:2502.15007
- Multi-Agent GraphRAG: arxiv:2511.08274 — text-to-Cypher for LPG, iterative refinement
- GigaEvo: arxiv:2511.17592 — MAP-Elites quality-diversity + LLM mutation
