# ADR-010: Agent Memory Architecture

**Status:** Accepted
**Date:** 2026-04-17 (initial), 2026-04-17 (S49 revision)
**Authors:** CC (draft), Ark (review), Mike (direction)
**Session:** S48 (initial draft), S49 (embedding research + implementation decisions)

---

## Context

Agent memory in DPC operates across 7 layers (L1 Strategy → L7 Human) that currently work in isolation. The agent's knowledge store (L5) has no connection to conversations (L4), no awareness of project context (L2), and no mechanism to recall relevant knowledge without explicit tool calls. Both agents (Ark and CC) demonstrate memory failures every session: stale recall, fabrication from memory, knowledge-exists-but-unused pattern.

Mike's "Active Recall" challenge from the Karpathy LLM-Wiki gist (April 2026): "How does the agent know to look for something it forgot it has?"

Research inputs: mem0 v3 analysis (15 findings), 560 gist comments (6 community approaches), Chado agent architecture (production-validated), P0 hooks infrastructure (ADR-007, landed S47).

## Decision

Implement a 4-component memory architecture that bridges 5 of 7 memory layers, using embeddings for retrieval and a dedicated memory provider for episodic extraction with human approval gate.

### Component 1: _meta.json — Access Registry

**Bridges:** L5 ↔ L4 ↔ L6

Per-file metadata sidecar in agent's knowledge/ directory:
- `last_accessed`, `access_count`, `last_verified` — access tracking
- `tags` — topic tags for fallback matching
- `summary` — short description for hint injection
- `source_layer` — tracks origin (L5 agent-written, L6 Mike-committed, L2 project)
- `project` — project scope (null = cross-project, string = project-specific)
- `embedding` — cached embedding vector
- `stale` — boolean, set when no access for 30+ days

Updated on every `knowledge_read()` call.

**Scope:** ~60 lines in memory.py

### Component 2: Embeddings Active Recall — Cross-Layer Retrieval

**Bridges:** L1 + L2-docs + L5 + L6 → conversations

**Search architecture:** Hybrid BM25 + vector search (not pure vector). BM25 catches exact matches (identifiers like ADR-010, function names); vector catches semantic similarity. Combined via Reciprocal Rank Fusion (RRF, k=60).

**Embedding model:** Configurable, default = `intfloat/multilingual-e5-small` (118M params, 384d, MIT, ~100 languages). Power-user alternative: `deepvk/USER2-small` (384d, index-compatible swap). Network-recommended for cross-lingual: `BAAI/bge-m3` (MIRACL 70.0). Model selection based on two research rounds (S49): embedding-model-comparison.md + embedding-research-v2-vision-scale.md.

**BM25 library:** bm25s (pure Python, scipy sparse, actively maintained). Character bigram fallback for non-whitespace scripts (CJK, Arabic, Thai).

**Vector index:** FAISS from start (not numpy). `IndexFlatIP` at small scale (brute force, same as numpy), upgrades to HNSW at scale without API change. Tested to 1M+ files. FAISS-cpu = MIT license, ~15MB dep.

**First-use UX:** Explicit download notification — "DPC needs embedding model (470MB) for intelligent memory recall. Without it, Active Recall is unavailable. Download now?" Buttons: Download / Later / Never. Same pattern as Whisper model download.

**GPU support:** GPU-first via sentence-transformers (auto-detects CUDA/MPS/ROCm), CPU fallback automatic. Cross-platform: Windows/Linux/macOS.

Embedding scope (configurable, multi-layer):
- L5: agent knowledge files — all text files (not just md; includes json values, py docstrings, config comments). Binary excluded.
- L1: strategy doc headers (ROADMAP sections, P13 sections, VISION sections)
- L6: Mike's signed knowledge titles (via firewall read-only)
- L2-docs: Extended Paths from firewall UI — all docs-like files in configured paths
- L2 code: NOT embedded — accessible via search_files tool + BM25 keyword index

**Chunking:** Model-dependent, configurable per model. e5-small (512 max tokens) requires chunking for files >~1.5 pages; bge-m3 (8192 tokens) covers most files without chunking. Chunk size, overlap, and strategy stored per-model in index header. Auto-detect script via Unicode ranges for BM25 tokenizer: whitespace tokenization for Latin/Cyrillic scripts, character bigram fallback for non-whitespace scripts (CJK, Arabic, Thai).

**Known model candidates:** multilingual-e5-small (default, 100 langs), deepvk/USER2-small (RU+EN power-user), bge-m3 (network-recommended for Phase 2 cross-lingual), gte-multilingual-base (alternative multilingual).

**Incremental indexing:** Embed on write (not rebuild on startup). Three trigger events: knowledge_write(), Mike-approved knowledge commit, Extended Paths file change (mtime check at startup). First-use or corrupted cache triggers full rebuild. Edge cases: model swap detection (model_name in cache header), deferred download catch-up, concurrent write debounce (100ms).

On each user message: embed query → FAISS similarity + BM25 keyword → RRF merge → top-3 matches across all layers → inject hints in Block2 with source layer label.

Priority: L6 > L1 > L5 > L2-docs. Rationale: Mike's signed knowledge (L6) represents human-verified facts; strategy docs (L1) are shared-approved; agent knowledge (L5) is agent-written; project docs (L2) are contextual.

Budget-aware: context >50% → hints only, >70% → skip injection entirely.

Project scope: L2 embedding scope derived from Extended Paths configured in firewall UI (`list_extended_sandbox_paths()`). Multiple projects supported simultaneously — embeddings index the union of all extended paths containing project markers (CLAUDE.md / pyproject.toml / package.json). Dynamic: paths added/removed in UI trigger re-indexing. L5 embeddings persist across project changes.

**Scope:** ~130 lines in context.py + memory.py

### Component 3: Memory Provider — Conditional Episodic Extraction

**Bridges:** L4 conversations ↔ L5 knowledge ↔ L7 Mike

Dedicated `memory_provider` config slot (separate from agent's main provider), reusing existing `background_provider` infrastructure from llm_adapter.py. Per-agent UI configuration (same pattern as evolution/consciousness toggles). Cheap model (local Ollama or API Haiku).

Triggers conditionally (not every response):
- Response contains tool call (action taken)
- Response longer than threshold (substantive content)
- Mike's decision verb detected
- Explicit knowledge claim in response

Extracts: `{ts, topic, decision, rationale, participants, source_session, status, supersedes, parent_id}` → `decision_proposals.jsonl` (DRAFT status). Reserved fields `status` (default "active"), `supersedes` (default null), `parent_id` (default null) for forward-compatibility with ARCH-20 Knowledge DNA schema — zero migration when that ADR lands.

**C3 compliance (Human Cognitive Bottleneck):** Journal entries are proposals, not committed knowledge. Mike reviews via Session Close extension in DPC desktop (reuses KnowledgeCommitDialog pattern from Extract Knowledge — same modal, summary/entries/edit/approve/reject). Approved entries graduate to `decision_journal.jsonl` (permanent episodic memory). Rejected entries feed back into extraction prompt improvement (mem0 Finding 12 pattern).

**C4 compliance (Language):** Embedding model is configurable per user's language. Default = multilingual (e5-small, 100 languages). Users with primarily single-language content can switch to language-optimized model (e.g., deepvk/USER2-small for RU+EN). BM25 layer works on any language without model dependency.

**C8 graceful degradation (Agent Substrate):** If memory provider unavailable, Components 1 and 2 continue working. Active Recall degrades but doesn't break.

**Scope:** ~100 lines in llm_adapter.py + memory.py

### Component 4: Smart _index.md — Tiered Visibility

**Bridges:** L5 internal

Generated from _meta.json with sections:
- Active (accessed this session) — title + summary
- Recent (last 7 days) — title + summary
- Reference (older) — title + summary
- Stale (30+ days) — title only + "(stale, last: N days)" marker

Mark, don't remove — all files remain visible. Stale files show title without summary to save tokens while preserving awareness.

**Scope:** ~50 lines in context.py

## Constraints Compliance

| Constraint | How addressed |
|---|---|
| C3 Human Cognitive Bottleneck | Episodic extraction writes drafts; human approves at session close |
| C7 Lifecycle Asymmetry | Access timestamps enable decay; stale detection prevents unbounded growth |
| C8 Agent Substrate | Embeddings stored locally (survive model swap); LLM extraction optional with graceful fallback |
| Memory Asymmetry | Active Recall solves "agent doesn't know what it forgot"; episodic extraction captures decisions |

## Rollout

Each phase delivers value independently. Any phase can be skipped without breaking others.

| Phase | Component | Lines | Layers | Depends on |
|---|---|---|---|---|
| 1 | _meta.json | ~60 | L5↔L4↔L6 | nothing |
| 2 | Smart index | ~50 | L5 | Phase 1 |
| 3 | Embeddings | ~130 | L1+L2-docs+L5+L6→conv | Phase 1, torch (installed) |
| 4 | Memory provider | ~100 | L4↔L5↔L7 | Phase 1, Ollama/API |
| **Total** | | **~340** | **5 of 7 layers** | |

Files modified: memory.py, context.py, llm_adapter.py

## Layer Connection Map

```
                    L7 Mike
                   ↗ C3 gate ↘
    proposals ← approve    query →
         ↑                      ↓
    L5 decision_proposals.jsonl
         ↑                      ↓
    L4 conversations ←── hints ←── embeddings
         ↑     ↓                   ↑
    extraction  Active Recall      │
         ↑     ↓                   │
    L5 decision_journal.jsonl      │
         ↑                         │
    weekly consolidation           │
         ↓                         │
    L5 knowledge ←── _meta.json ──┘
         ↑                         │
    L1 strategy docs ──embeddings──┘
    L6 Mike's knowledge ──embeddings─┘
    L2 project docs ── embeddings ─┘
    L2 project code ── tools only
    L3 CC memory ── not connected (different runtime)
```

## Alternatives Considered

1. **TF-IDF only** — keyword-based matching without embeddings. Rejected: ~75% semantic coverage insufficient; "extraction prompt" would not match "knowledge commit quality".

2. **Embeddings-only without episodic extraction** — Active Recall hints but no decision journal. Rejected: solves retrieval but not the conversations→knowledge gap.

3. **Full auto-extraction without C3 gate** — Chado pattern applied directly (per-response extraction auto-committed). Rejected: violates C3 (Human Cognitive Bottleneck) — "knowledge automatically extracted by an LLM does not create learning."

4. **mem0 as library** — adopt mem0 as DPC's memory layer. Rejected: PostHog telemetry by default (C6 sovereignty conflict), single-owner scoping (C8 substrate), no multi-party consensus.

## Dependency on ADR-007

Component 3 (Memory Provider) triggers after agent responses via the hook infrastructure from ADR-007. The relevant lifecycle point is post-response (after LLM returns but before next user message). ADR-007 Phase 0 hooks are landed (S47); Component 3 is a consumer of this infrastructure.

## What This Does Not Address

- L3 CC memory (different runtime, not our code)
- L7 succession (who curates knowledge after author dies)
- Knowledge graph navigation (ARCH-23, future)
- Full semantic search across L2 code (tools-only, BM25 keyword search available)
- Network-level embedding protocol (Phase 2 Team Track — share original text, not embedding vectors; each node embeds locally with its own model; bge-m3 as network-recommended model for cross-lingual retrieval)

## References

- mem0 v3 research: `ideas/cc-research-plan-2026-04-17-mem0.md` (15 findings)
- Karpathy gist: 560 comments, 6 community approaches analyzed
- Chado agent: production-validated episodic memory + Auto-RAG (1.5x boost)
- ADR-007: hooks infrastructure (P0, landed S47) — enables Component 3
- Seahorse plan: `ideas/cc-mike-research/enumerated-strolling-seahorse.md` (P4 Memory Upgrade)
- Foundation constraints: `ideas/karpathy-gist/mike-gist-posts/foundation-constraints-review-draft.md`
- Embedding research v1: `ideas/cc-mike-research/embedding-model-comparison.md` (10 models, MTEB-rus focused)
- Embedding research v2: `ideas/cc-mike-research/embedding-research-v2-vision-scale.md` (VISION-scale, hybrid BM25+vector, 7000 languages)
- MTEB leaderboard data: `ideas/tmpp2rbf790.csv` (405 models export)

## Resolved Questions (S49)

**1. Weekly consolidation:** Two-tier approach. Tier 1 (auto): _meta.json stats refresh + _index.md reshuffling (Active→Recent→Stale) — background task, no user involvement. Tier 2 (manual): "Consolidate Memory" button in UI — agent proposes merges/archives, user approves via C3 gate pattern. Content decisions are always human-gated.

**2. ARCH-20 interaction:** Forward-compatible schema, not forward-implemented. Reserved fields (status, supersedes, parent_id) added to decision entry schema now. Palinode operations (KEEP/UPDATE/MERGE/SUPERSEDE/ARCHIVE) deferred to ARCH-20 scope. Zero migration when ARCH-20 lands.

**3. Embedding model:** Default = multilingual-e5-small (118M, 384d, MIT, 100 languages). Configurable — users can switch to language-specific (deepvk/USER2-small for RU+EN) or heavier multilingual (bge-m3). Based on two research rounds: MTEB leaderboard analysis + VISION-scale constraints review.

**4. L2-docs scope:** Extended Paths from firewall UI. All text files indexed (not just md). BM25 on raw text from any file; vector on extracted natural-language content. Binary excluded.

**5. Extraction triggers:** trigger=ALL on start (any of 4 triggers fires extraction). Tuning by user feedback.

**6. C3 gate UI:** Session Close extension, reusing KnowledgeCommitDialog pattern from Extract Knowledge.

**7. Search backend:** FAISS from start (IndexFlatIP at small scale, HNSW at 100K+). Not numpy — same API at any scale, no code rewrite needed.

**8. Index persistence:** Incremental (embed on write, load from disk on startup). Cache header stores model_name + dimensions + chunk_config for swap detection.

**9. Model swap:** Background incremental rebuild with resume. Batch processing, pausable. Explicit user prompt on model change with estimated time.

**10. Scale target:** 1M+ files (Mike's 32TB drives). FAISS + BM25 handle this. Full rebuild at 1M = ~5.5h (background, resumable).

**11. First-use UX:** Explicit download notification with Download/Later/Never buttons. Same pattern as Whisper model download.

**12. Memory provider:** Dedicated config slot (separate from agent's main LLM). Per-agent UI setting.

**13. BM25 tokenization:** Character bigram fallback for non-whitespace scripts (CJK, Arabic, Thai). Pluggable stemmers for top languages.

## Scale Analysis

| Files | Chunks (5x avg) | Vector Index | BM25 Index | FAISS Search | Full Rebuild |
|-------|-----------------|-------------|-----------|-------------|-------------|
| 1K | 5K | 7 MB | 4 MB | <1 ms | ~20 sec |
| 10K | 50K | 73 MB | 40 MB | ~7 ms | ~3 min |
| 100K | 500K | 732 MB | 400 MB | ~5 ms (HNSW) | ~30 min |
| 1M | 5M | 7.5 GB | 2-5 GB | ~5 ms (HNSW) | ~5.5 hours |

Index Manager designed as separate component (not inline) to support background rebuild with progress, resume, and queue at scale.

## Implementation Notes (remaining)

- Extraction trigger threshold tuning — runtime, based on user feedback
- Similarity score cutoff for hint injection — empirical testing needed
- ONNX INT8 export for CPU-only fallback (<1% quality loss, 2-3x speedup)
- File type extractors: per-format (stdlib + tree-sitter for TS/JS)
