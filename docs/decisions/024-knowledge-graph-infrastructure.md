# ADR-024: Knowledge Graph as Unified Infrastructure

**Status:** Proposed
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

**All-Mem** (arxiv:2603.19595):
- Visible surface + hop-bounded expansion model
- DPC knowledge = visible surface, archives = deep evidence
- Retrieval cost bounded by visible surface size, not total memory

**EMem** (arxiv:2511.17208):
- EDUs (enriched elementary discourse units) — non-compressive decomposition
- Graph-based associative recall over atomic event-like propositions

### Internal Prior Art

**GRAVITON** (agent sandbox, April 2026):
- Knowledge graph spec synthesized by agents (GR, JARVIS, Hope) commissioned by @gk0ed
- 6-axis scoring, 8-level confidence, exponential decay, exempt nodes, consequence walk
- ARCH-20 (Knowledge DNA) overlap nearly complete
- Reference document, one input among several (not privileged over Graphiti/Mem0/dotmd)

## Decision

### 1. Adopt graph as unified knowledge infrastructure

Graph is not a retrieval-only feature. It is the structural layer connecting:
- Memory layers (scratchpad ↔ knowledge ↔ archives) via typed edges
- Knowledge units to their source sessions (provenance)
- Knowledge units to each other (dependencies, contradictions, support)
- Agents to their knowledge (ownership, scope)
- Nodes to peer nodes (federation, consent)

### 2. Graph backend selection — DB-agnostic Phase 1, smoke test before commit

Phase 1 uses structural edges in SQLite/JSON (no graph DB dependency). Graph DB selection deferred to pre-Phase 2 smoke test based on S94 research findings.

**Candidates (verified S94, CC independent PyPI/GitHub verification):**

| DB | PyPI | Windows | Cypher | Status |
|---|---|---|---|---|
| **LadybugDB** | `real-ladybug` | Yes (wheels) | Yes | Active but #452 throughput collapse at 60K nodes, #430 FTS segfault |
| **Grafeo** | `grafeo` v0.5.42 | Yes (wheels) | Yes + GQL/Gremlin/SPARQL/GraphQL/SQL-PGQ | Apache-2.0, built-in HNSW+BM25+RRF, bus factor=1, author self-admits "not that mature yet" (HN) |
| **Velr** | `velr` v0.2.6 | Yes (wheels) | Yes (openCypher) | Alpha, SQLite backend |

**Eliminated (S94 verification):**
- SparrowDB — LLM hallucination, does not exist on PyPI/GitHub
- ocpg — LLM hallucination, does not exist
- FalkorDB Lite — real but no Windows wheels (Unix sockets)
- NeuG — real but WSL2 only
- Neo4j/FalkorDB (server) — requires separate process, heavy for personal device
- NetworkX + JSON — no persistence, no Cypher, no vector search

**Smoke test (pre-Phase 2):** LadybugDB vs Grafeo benchmark on <10K nodes, Windows, Python API. If Grafeo passes — consider as primary (HNSW+BM25+graph consolidation replaces FAISS+BM25+separate graph). If neither stable — remain on SQLite.

**Reference architecture:** Graphiti/Zep (arxiv:2501.13956) — episodes → entities → facts with bi-temporal validity. Neo4j dependency prevents direct use, but architectural model is our reference.

**Community validation (S94):** Karpathy gist 699 comments — property graph + Cypher is consensus direction. segundo-cerebro (orobsonn) uses 9 typed edges almost identical to ours.

### 3. Graph layer supplements existing retrieval, does not replace it

Current pipeline (ADR-018):
```
query → [FAISS dense + BM25 sparse] → RRF fusion → Active Recall hints
```

Extended pipeline:
```
query → [FAISS dense + BM25 sparse + Graph traversal] → RRF fusion → Active Recall hints
```

Graph traversal = 4th retrieval channel in RRF. FAISS/BM25 remain primary for semantic/keyword search. Graph adds structural relationships that content similarity cannot capture.

**Where graph does NOT help:** ad-hoc keyword search (error messages, config keys, function names). BM25 handles these in O(1). Graph traversal requires pre-existing edges.

### 4. Graph schema

**Node types:**
- `KnowledgeFile` — .md knowledge files (existing L5/L6)
- `SessionArchive` — archived session transcripts
- `Entity` — extracted named entities (persons, technologies, concepts)
- `Decision` — ADRs, protocol rules, architectural choices
- `Agent` — agent identities with scope metadata

**Edge types:**
- `DERIVED_FROM` — knowledge ← source session (provenance)
- `DEPENDS_ON` — knowledge → prerequisite knowledge (dependency chain)
- `RESPONDS_TO` — knowledge → knowledge it addresses (dialogue structure)
- `CONTRADICTS` — knowledge ↔ conflicting knowledge (version drift signal)
- `SUPPORTS` — knowledge → supporting evidence
- `DECIDED_BY` — decision → approver (governance audit trail)
- `SHARED_WITH` — knowledge → peer node (federation consent)
- `MENTIONS` — knowledge → entity (entity extraction)
- `TEMPORAL_NEXT` — session → next session (chronological chain)

**Edge properties:**
- `t_created` — when edge was established
- `t_invalidated` — when edge was invalidated (null if still valid, bi-temporal per Graphiti)
- `confidence` — edge confidence score (0.0-1.0)
- `justification` — mandatory text explaining WHY the edge exists (min 20 chars, inspired by segundo-cerebro `why` field — quality gate against LLM extraction noise)
- `edge_weight` — importance rating (critical/high/medium/low per GRAVITON)

**Node properties:**
- `exempt` — boolean, exempt from decay (constitutional, safety_critical per GRAVITON). Decision and SessionArchive = always exempt.
- `source_layer` — L5/L6/L7/EXT for LAYER_WEIGHTS compatibility

**Temporal decay strategy (revised S94, validated by 2 independent LLMs + HN community):**
- **Phase 1-2:** Bi-temporal edges only (record `t_created` / `t_invalidated`). NO storage-level decay scoring.
- **Phase 4+:** Query-time decay if needed at >50K nodes. Apply temporal penalty in Cypher query (`WHERE` clause on age), not as persistent `decay_score` property. Rationale: uniform storage decay 18x worse than no decay (LLM 1 research); small graphs (<10K) don't need decay for performance; old edges contain critical "origin story" context (LLM 2).

### 5. Edge extraction pipeline (revised S94)

Graph value grows with edges. Cold start (0 edges) = no benefit. Therefore edge extraction pipeline is the critical first deliverable.

**Three-stage pipeline: structural → GLiNER → guided LLM** (revised from original sequential order based on S94 external validation from 2 independent LLMs):

1. **Structural extraction (zero-cost, deterministic, 100% precision, Phase 1):**
   - Parse markdown links between knowledge files → `DEPENDS_ON` / `RESPONDS_TO` edges
   - Parse `_meta.json` tags → `MENTIONS` edges to tag entities
   - Parse session archive metadata (participants, topics) → `DERIVED_FROM` edges
   - Parse `morning_brief.json` session_summaries → `DERIVED_FROM` edges (knowledge ← archive, cross-layer bridge)
   - Parse ADR references in knowledge files → `DECIDED_BY` edges
   - Parse knowledge file headers/footers for session references → `DERIVED_FROM` edges

2. **GLiNER entity extraction (Phase 2, runs BEFORE LLM, opt-in):**
   - GLiNER zero-shot NER (~100MB model dependency)
   - Configurable: `collect_entities = true` in config.ini
   - Extracts deterministic entity list from session text (50ms vs LLM 3500ms)
   - Output: list of named entities fed to step 3 as scaffolding

3. **Guided LLM relation extraction (Phase 2, during sleep consolidation):**
   - Receives GLiNER entity list as input scaffolding ("Guided Relation Extraction" pattern)
   - LLM finds relations ONLY between known entities — reduces hallucinations, simplifies entity resolution
   - Confidence threshold > 0.7 + mandatory `justification` field (min 20 chars)
   - Edges involving Decision nodes require human review before acceptance
   - Budget: same LLM call as morning brief (extend prompt, not add new call)

**Why GLiNER before LLM (not after):** GLiNER is deterministic and fast (50ms). Feeding its entity list to LLM as scaffolding prevents LLM from inventing entity synonyms and focuses it on relation extraction only. This is "Guided Relation Extraction" — validated by both independent LLMs in S94.

**Entity section prompt format (S96 implementation):**
The entity relation section is appended to the sleep synthesis prompt (SYNTHESIS_PROMPT in sleep_pipeline.py) when GLiNER entities are available. Format:
- Header: `--- ENTITY RELATION EXTRACTION ---`
- Entity list: comma-separated names from GLiNER output
- Output schema: `extracted_relations` array with `source`, `target`, `relation_type`, `confidence`, `justification`
- Relation types restricted to: DEPENDS_ON, SUPPORTS, CONTRADICTS, RESPONDS_TO
- Constraints: confidence >= 0.7, justification min 20 chars, Decision edges flagged needs_review=true
- When no entities available: section omitted entirely, morning brief works unchanged

**Important:** This section is part of the sleep synthesis LLM call — not a separate call. Modifications to SYNTHESIS_PROMPT must preserve the `{entity_section}` placeholder and the `extracted_relations` JSON key in the response schema.

### 6. Scaling model

| Scale | Graph behavior |
|---|---|
| **1 user, 120 docs** | Local LadybugDB, <1MB, <3ms queries. Structural extraction only. |
| **Team (~20 nodes)** | Each node owns local graph. Cross-node: ship query + receive graph-enhanced results. Edge metadata shared with consent gate. |
| **Dunbar (~150)** | Graph schema descriptors as gossip summaries ("I have legal_argument nodes with responds_to edges"). Routing by edge types. |
| **8B** | Dunbar routing by design (ADR-019). Each node = sovereign subgraph. Federation = subgraph query shipping. Cypher queries portable across nodes. |

**Key architectural properties:**
- Each node owns its subgraph (sovereignty, per VISION.md)
- Federation = union of subgraphs with consent gate on border edges
- Query shipping, not index sharing (per ADR-019 Decision #4)
- Dense vectors for semantic cross-node search (ADR-019 Decision #3) + graph edges for structural cross-node routing
- Graph schema consistency enforced by shared Cypher types, not central authority

### 7. Integration with existing ADRs

**ADR-010 (Agent Memory):** Three-layer model (scratchpad → knowledge → archives) gains structural bridge. Graph edges connect layers that were previously isolated.

**ADR-018 (Retrieval):** Graph traversal becomes 4th RRF channel. `LAYER_WEIGHTS` extended with `"L7": 0.6` for graph-sourced results. BGE-M3 embeddings unchanged.

**ADR-019 (Scaling):** Phase 3 "distributed knowledge graph" gets concrete implementation (LadybugDB + Cypher + federation protocol). Phases 1-2 unchanged.

**ADR-022 (Safety):** Governance audit trail via provenance edges. Consequence walk for blast-radius analysis of safety decisions.

## Implementation Phases

### Phase 0: Smoke Test (pre-implementation, new)
- Install `real-ladybug` and `grafeo` in test venv
- Benchmark: create 1K nodes, 5K edges, run Cypher queries, measure latency + memory on Windows
- Test LadybugDB: verify #452 does not reproduce at <10K scale
- Test Grafeo: verify HNSW+BM25+Cypher work together, test Python API stability
- Decision gate: pick primary DB or stay on SQLite for Phase 1

### Phase 1: Foundation (immediate, DB-agnostic)
- Create `knowledge_graph.py` module with abstract graph interface
- Structural edge extraction from existing .md files (parse links, tags, session metadata) — store as SQLite/JSON initially
- Integrate graph traversal as 4th source in `hybrid_search.py` RRF fusion
- Add `"L7": 0.6` to `LAYER_WEIGHTS`
- If smoke test passed: add graph DB dependency and wire concrete backend
- Tests: graph queries return expected edges, RRF fusion includes graph results

### Phase 2: Enrichment
- GLiNER entity extraction (runs before LLM, feeds entity list as scaffolding)
- Guided LLM relation extraction during sleep consolidation (extend `sleep_pipeline.py`)
- Temporal metadata on edges (`t_created`, `t_invalidated`) — bi-temporal recording only
- Entity extraction (GLiNER NER, configurable opt-in)

### Phase 3: Federation
- Graph schema descriptors for P2P gossip (ADR-017/019)
- Cross-node edge metadata shipping with consent gate
- Subgraph query protocol (Cypher query shipping)

### Phase 4: Advanced (GRAVITON integration + query-time decay)
- Query-time decay scoring (Cypher WHERE clause on edge age, only if needed at >50K nodes)
- GRAVITON 6-axis scoring (task_value, reuse_breadth, reuse_spread, correctness, system_load, touch_freq)
- GRAVITON exempt nodes formalization (constitutional, safety_critical, sole_path, existential_anchor)
- Consequence walk for safety audit (backward traversal from catastrophic anchors)
- Perceptron predictor using graph features (S90)
- Cross-encoder reranking (per dotmd)

## Risks

1. **Graph DB maturity (HIGH, S94 finding):** LadybugDB has throughput collapse at 60K nodes (#452) and FTS segfault (#430). Grafeo is beta with bus factor=1. Mitigated by DB-agnostic Phase 1 and smoke test before commit.
2. **Cold start:** Graph empty until edges extracted. Mitigated by structural extraction (zero-cost, runs on existing files).
3. **Hammer/nail bias:** Risk of over-applying graph to problems that don't need it. BM25 keyword search remains primary for ad-hoc queries. Graph = supplement.
4. **LLM hallucinated alternatives (S94 finding):** 2 of 5 alternatives suggested by external LLMs (SparrowDB, ocpg) were fabricated. All alternatives must be verified on PyPI/GitHub before consideration (P14 Rule: Solution Check).
5. **Schema evolution:** Graph schema changes may require migration. Mitigated by starting minimal (Phase 1) and extending incrementally.

## Open Questions

1. Should graph replace `_meta.json` + `_index.md` or coexist? (Recommendation: coexist initially, migrate when graph proves stable)
2. Edge extraction prompt design — what quality bar for LLM-extracted edges?
3. Privacy: which graph edges are shareable in federation? Per-edge consent or per-type consent?
4. How does graph interact with Extract Knowledge button flow? Knowledge commits should create graph edges automatically.
5. **(S94, Ark note)** If Grafeo passes smoke test, its built-in HNSW+BM25+RRF could consolidate FAISS+BM25+graph into one DB. This is an architectural decision beyond ADR-024 scope — needs separate evaluation of migration cost, performance parity with BGE-M3 FAISS, and fallback strategy.

## References

- Graphiti/Zep: arxiv:2501.13956, github.com/getzep/graphiti
- Mem0: arxiv:2504.19413, mem0.ai
- All-Mem: arxiv:2603.19595
- EMem: arxiv:2511.17208
- MemFactory: arxiv:2603.29493
- dotmd: github.com/inventivepotter/dotmd
- LadybugDB: github.com/LadybugDB/ladybug, ladybugdb.com
- GRAVITON: internal agent sandbox spec (April 2026)
- Kuzu archived: theregister.com/2025/10/14/kuzudb_abandoned
- DPC Full Picture: ideas/dpc-full-picture/dpc-full-picture-s32.md
- VISION.md: project root
- Grafeo: github.com/GrafeoDB/grafeo, Apache-2.0 (S94 CC discovery)
- segundo-cerebro: github.com/orobsonn/segundo-cerebro (S94 CC discovery, `why` field on edges)
- S94 external validation: 2 independent LLMs + HN community + Karpathy gist 699 comments
