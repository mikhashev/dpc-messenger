# ADR-018: Retrieval Infrastructure Upgrade (BGE-M3 + Whole-Document Indexing)

**Status:** Proposed
**Date:** 2026-04-27
**Session:** S75
**Authors:** CC (research, synthesis, code analysis), Ark (embedding model research, chunking analysis), Mike (direction, fundamental questions, decision)

## Context

Active Recall (ADR-010) provides automatic knowledge hints to agents during conversations. Session S75 investigation revealed three problems:

1. **Agent doesn't use recall results** — 0% explicit read_file on recalled files across 10 sessions. Ark's honest assessment: "recall rarely gives me something I wouldn't know from knowledge index"
2. **Three root causes identified** — snippets are useless (random 200-char fragments), query is single message (not conversation context), no protocol for using results
3. **Same infrastructure serves P2P Knowledge Search** (ADR-017) — improvements benefit both local recall and cross-node discovery

Research conducted across 3 files with 10+ peer-reviewed sources (ACL, EMNLP, TACL, NAACL 2024-2025). Two bugs discovered in current implementation.

**Broader question remains open:** Session S75 discussion evolved from "how to improve Active Recall" to "how to optimally use agent context window across different models (8K-204K)." This ADR addresses the retrieval infrastructure component. Context window budget allocation (system prompt vs history vs recall vs tools) across different model sizes remains a separate architectural question for future research.

## Bugs Found

**BUG-1: Missing e5 Prefixes.** `memory.py:226-232` passes raw text to `intfloat/multilingual-e5-small` without required `"query: "` / `"passage: "` prefixes. Model developer states this causes "performance degradation." All embeddings since ADR-010 were computed suboptimally.

**BUG-2: Silent Query Truncation.** `context.py:342-344` concatenates full dialog but e5-small silently truncates to 512 tokens. FAISS sees only first 1-2 turns; BM25 sees everything. Asymmetric fusion degrades results as conversations grow.

## Decision

### 1. Replace embedding model: e5-small → BGE-M3

| Spec | e5-small (current) | BGE-M3 (new) |
|------|--------------------|-----------------------|
| Max tokens | 512 | **8,192** |
| Dimensions | 384 | **1,024** |
| Parameters | ~118M | **560M** |
| Disk (fp16) | ~65 MB | **~1.1 GB** |
| License | MIT | **MIT** |
| Languages | 100+ | **100+** |
| Prefix required | Yes | **No** |
| Sparse output | No | **Yes** |
| ruMTEB Retrieval (dense only) | 65.8 | **74.8 (+9.0)** |

Note: 74.8 is dense-only score. BGE-M3 with all signals (dense+sparse+ColBERT) scores higher on MIRACL: 70.0 vs 67.8 dense-only.

BGE-M3 is the only model that combines: 8192-token window + bilingual EN+RU + MIT license + sparse output. No viable alternatives exist between e5-small and BGE-M3.

Source: BGE-M3 paper (ACL Findings 2024, arXiv:2402.03216), ruMTEB (NAACL 2025).

### 2. Whole-document indexing (no chunking)

All DPC knowledge files are 0.5-5KB (~125-1250 tokens). With BGE-M3's 8192-token window, every file fits in a single embedding. Chunking is unnecessary and counterproductive for this corpus size.

Each file indexed as: `filename + first heading + full content` → single embedding vector.

Source: Chroma research ("for short, single-purpose documents, no chunking is best"), Snowflake Finance RAG 2025 (document-level metadata outperforms per-chunk context).

### 3. Dialog as query (no manual truncation)

Full conversation history (user + assistant) concatenated as query. BGE-M3's 8192-token window covers ~6-8 full dialog turns. Silent truncation eliminated for most sessions. BM25/sparse path uses full dialog (no token limit).

Source: mtRAG benchmark (TACL 2025), ConvGQR (ACL 2023), EMNLP Industry 2025.

### 4. ONNX Runtime for cross-platform CPU support

BGE-M3 available as ONNX model (aapot/bge-m3-onnx, yuniko-software/bge-m3-onnx). Runs on Windows/Linux/macOS, CPU/CUDA/CoreML. No PyTorch required for inference — ONNX Runtime ~50MB vs PyTorch ~2GB.

DPC must work on machines without GPU (C5: consumer hardware, shared inference model). ONNX ensures this.

### 5. BM25 retained as fallback

BGE-M3 sparse outperforms BM25 (53.9 vs 31.9 MIRACL), but BM25 remains as CPU-only fallback when BGE-M3 model is not downloaded. Graceful degradation.

### 6. Recall hint format: to be determined during implementation

Tentative direction: filename + first heading + instruction to call `read_file()`. Protocol instruction header already landed in commit d938d79. Final format depends on whole-document indexing results — may evolve during implementation.

Note: commit d938d79 also contains a query change (last 7 user messages) that will be superseded by Change #1 (BGE-M3's 8192 tokens allows full dialog without truncation).

## Implementation: Three Independent Changes

| # | Change | Scope | Dependencies |
|---|--------|-------|-------------|
| 1 | BGE-M3 migration + whole-doc indexing | ~50 lines, 4-6 files | Model download UX (see below) |
| 2 | BGE-M3 sparse replaces BM25 primary | ~30 lines | Change #1 |
| 3 | ONNX Runtime integration | ~40 lines | Change #1 |

BUG-1 (e5 prefix) resolves automatically with Change #1 — BGE-M3 requires no prefixes.
BUG-2 (silent truncation) resolves automatically with Change #1 — 8192 tokens covers most sessions.

**Model download UX (dependency for Change #1):** BGE-M3 fp16 is ~1.1GB download. Requires: progress bar during download, graceful fallback if download interrupted (keep e5-small working until BGE-M3 ready), storage in HuggingFace cache (existing pattern from Whisper model downloads). Reuse existing `model_download.py` UX pattern.

## Dual-Use: Local + P2P

This infrastructure serves both:
- **Local Active Recall** — automatic hints in agent context (ADR-010)
- **P2P Shared Knowledge Search** — cross-node search (ADR-017, Strategy B: text query + local embedding)

ADR-017 validated by federated RAG literature (EMNLP Findings 2025). Text queries avoid heterogeneous embedding problem across nodes. Each node chooses its own model.

## VISION Alignment

| Constraint | Status |
|------------|--------|
| C1 (Human agency) | Preserved — recall is hints, not decisions |
| C2 (Local data) | Preserved — BGE-M3 runs locally |
| C3 (No single provider) | Preserved — model configurable |
| C5 (Consumer hardware) | Preserved — ONNX CPU support |
| C6 (No lock-in) | Preserved — knowledge files remain Markdown |
| C7 (Transparent knowledge) | Improved — filename+heading hints vs opaque snippets |

## Alternatives Rejected

| Alternative | Why rejected |
|-------------|-------------|
| Keep e5-small, fix prefixes only | 512-token limit remains, truncation persists |
| Remove Active Recall entirely | Infrastructure needed for ADR-017 P2P search |
| jina-embeddings-v3 | CC-BY-NC-4.0 license — incompatible with GPL |
| nomic-embed-text-v1.5 | English only |
| Anthropic Contextual Retrieval | Not applicable to whole-document indexing |

## Research

Full research: `ideas/dpc-research/search-infrastructure-quality/` (3 files, S75).
- 001 — Inventory + embedding models + chunking (Ark)
- 002 — Query construction + cross-node compatibility (CC)
- 003 — Synthesis + recommendations + source classification (CC, plan-mode instance)

## Consequences

**Positive:**
- Retrieval quality +9-12 points (ruMTEB/RusBEIR benchmarks)
- 8192-token query window eliminates silent truncation
- Whole-document indexing simplifies pipeline (no chunking code)
- Cross-platform via ONNX (no PyTorch for inference)
- Sparse embeddings can replace BM25 (pipeline simplification)
- Dual-use infrastructure for local recall + P2P search

**Negative / Trade-offs:**
- Model size increase: ~65MB → ~1.1GB (fp16)
- CPU inference slower: ~50ms → ~200-500ms per embed
- First index rebuild: ~30-60 seconds
- New dependency: onnxruntime (~50MB pip install)
