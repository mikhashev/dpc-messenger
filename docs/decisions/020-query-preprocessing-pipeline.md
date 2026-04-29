# ADR-020: Query Preprocessing Pipeline

**Status:** Proposed
**Date:** 2026-04-28
**Session:** S78
**Authors:** Ark (architecture, three-layer design), CC (code analysis, BM25 bug discovery, stop words implementation), Mike (direction, query quality insight, domain-specific stop words idea)

## Context

S78 showed: query dilution affects ALL retrieval channels (dense FAISS, BGE-M3 sparse, BM25) simultaneously. A single rare term "EEG" was not found by any of 3 channels until stop words were filtered from the query. Hardcoded frozenset for 2 languages does not scale to 100+ languages claimed by the project.

Current state (S78 bootstrap):
- `_STOP_WORDS` frozenset in `bm25_index.py` — ~130 Russian + English words
- Applied to BM25 tokenizer only (both build and search paths)
- Dense and sparse channels have no query preprocessing

## Decision

Three-layer query preprocessing pipeline, applied upstream before all retrieval channels:

### Layer 1: Standard stop words by language
Ready-made stop word lists (stop-words-iso covers 57 languages, NLTK 20+, spaCy 60+). Language detection on query (fasttext, lingua — microsecond latency) selects the appropriate list. Zero config for new users.

### Layer 2: Corpus-adaptive (statistical)
At index build/rebuild time: tokenize entire corpus, compute document frequency (DF) per token. Tokens with DF > threshold (80%?) become automatic stop words for this specific corpus. Self-calibrating — medical corpus gets different stop words than programming corpus. Language-independent. Stored in index metadata.

### Layer 3: User-specific (personalization)
Analysis of user messages in conversation history. High-frequency but semantically empty tokens specific to each user's communication patterns. Source: session archives. Mike's insight (S78 [75]): "можно дополнять словарь словами на основе архива сессий — это специфика тех кто общается."

### Pipeline

```
raw query → language detection → Layer 1 (standard stop words)
                               → Layer 2 (corpus stop words from index metadata)
                               → Layer 3 (user stop words from history)
                               → clean query → FAISS dense
                                             → BGE-M3 sparse
                                             → BM25
```

Filtering is upstream — before all three channels, not channel-specific. Index rebuild uses the same pipeline for consistency.

## Consequences

**Positive:**
- Open-source users: zero config, auto-works on first install
- Multilingual: language detection + per-language stop lists (100+ languages feasible)
- Corpus-adaptive: different domains get different stop words automatically
- Personalized: each user's filler words filtered
- Query quality improvement across all three retrieval channels simultaneously

**Negative / Trade-offs:**
- Language detection adds ~1ms per query (negligible)
- Layer 2 computation at rebuild time (~seconds for 684 docs)
- Layer 3 requires conversation history analysis (deferred, not blocking)
- New dependency for language detection library (fasttext or lingua)

## Research (S78)

**We are NOT reinventing the wheel** — stop word removal for retrieval exists since 1950s (Hans Peter Luhn). Our three-layer approach maps to established practices:

**Layer 1:** stopwords-iso (GitHub) covers 57 languages. NLTK 20+, spaCy 60+. Standard, solved problem.

**Layer 2:** scikit-learn `TfidfVectorizer(max_df=0.8)` does exactly this — corpus-specific stop words by document frequency threshold. Fox (1989) — first empirical stop list from Brown Corpus. Our approach = what sklearn does out of the box.

**Layer 3:** User-personal stop words from conversation history — this is DPC-specific contribution. Not standard in retrieval systems. Personalization layer.

**Industry context:**
- General trend: from large stop lists (200-300) to small (7-12) to none — BUT this applies to neural ranking (BERT). For BM25 keyword channel, stop words remain critical (BGE-M3 paper confirms BM25 as competitive baseline).
- "From BM25 to Corrective RAG" (arXiv 2604.01733, Apr 2026): BM25 outperforms dense retrieval on domain-specific queries. Hybrid retrieval + reranking achieves best results.
- ReFormeR (arXiv 2604.01417, Apr 2026): pattern-guided query reformulation — structured approach to query preprocessing beyond stop words.
- Pipeline best practice: Query interpretation → Candidate generation → Reranking → Context assembly. Our current pipeline covers candidate generation; query interpretation and reranking are future directions.

**Future directions (not in scope of this ADR):**
- HyDE (Hypothetical Document Embeddings) — LLM generates hypothetical answer, embeds that instead of query
- Multi-query expansion — split query into sub-queries
- Neural reranking — cross-encoder on RRF output
- Domain-specific stop words from session archives (Mike's idea, S78 [75])

## Current State

Hardcoded `_STOP_WORDS` frozenset in `bm25_index.py` (commit `b020c99`) serves as bootstrap until this ADR is implemented. BM25-only, Russian + English only.
