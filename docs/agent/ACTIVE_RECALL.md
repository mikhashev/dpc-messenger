# Active Recall — how it works, precisely enough to rebuild it

Active Recall is the part of a DPC agent's prompt assembly that, on every turn, searches the
agent's knowledge and puts up to three short pointers to relevant documents into the prompt,
without the model having to call a tool. The decision that introduced it is
[ADR-010, Component 2](../decisions/010-agent-memory-architecture.md#component-2-embeddings-active-recall--cross-layer-retrieval);
the decay step comes from [ADR-013 (S4)](../decisions/013-agent-selection-layer.md), the
whole-document index from [ADR-018](../decisions/018-retrieval-infrastructure-upgrade.md), the
graph channel from [ADR-024](../decisions/024-knowledge-graph-infrastructure.md).

This document describes the mechanism **as the code runs it at commit `14aaf88b` on branch
`dev` (2026-11-06)**, not as the ADRs planned it; where the two differ it says so (§13). The
file exists only on `dev`, not on `main`. Every constant is listed. The body needs no access to
the repository; file and line references are only in the appendix.

A rebuilder should read §14 before copying anything. It keeps three things apart: what was
built at that commit (§14.1), the known defects of that build, which a rebuild should not carry
over (§14.2), and the contracts a correct rebuild holds (§14.3). Sections 3 to 12 describe the
build as it is, defects included, and mark each defect where the mechanism meets it.

---

## 1. Summary in one paragraph

Each document is indexed once, whole, into two indexes: a dense vector (one embedding per
document) and a BM25 keyword row. On a turn, two queries are formed — the current message and
the last ten messages of history — and three channels are searched: vector (top 10 per query),
keyword (top 10), and a one-hop expansion over a knowledge graph seeded by the files the first
two channels found. The three ranked lists are merged by weighted Reciprocal Rank Fusion, one
entry per document. The fused scores are multiplied by an access-history factor ("decay"),
candidates with nothing to show are dropped, and the top three are rendered as a short block
appended to the current user message. Every injection is logged, and that log, together with
the agent's `read_file` calls, feeds the next turn's decay. Every embedding, of a document or of
a query, passes through one disk cache per node keyed by the text alone (§4.4).

## 2. Words used here

| Word | Meaning in this document |
|---|---|
| **layer** | Where a document comes from. Indexed layers: **L5** — the agent's own knowledge directory (agent-written); **L6** — the shared human-committed knowledge directory of the node; **EXT** — external directories the owner marked as indexed for this agent. |
| **L7** | In the code, the label of the **graph channel's** results (ADR-024 calls the graph "L7, the 4th RRF channel"). Note that ADR-010's layer map uses L7 for the human; the two uses share a number, not a meaning. |
| **L1, L2** | Layers that have a fusion weight (§7) but no indexer that produces them today. |
| **index key** | The identity of one document across the index, fusion, decay, the graph and the logs (§3.2). |
| **channel** | One retrieval method: vector, text (BM25), graph. |
| **candidate** | One fused entry: one document with one score. |
| **hint** | One rendered line about one candidate. |
| **address** | The string printed in a hint that the agent can pass to `read_file` to open the document. |
| **injection** | The act of putting hints into a prompt; logged per turn. |
| **read** | A finished `read_file` call by the agent, logged by the tool loop, whether it succeeded or failed. |
| **decay** | The access-history multiplier of §8. The name is the code's and ADR-013's; it has no term that grows with age (§8, "What decay is not"). |

## 3. Data model

### 3.1 One index entry per document

There is no chunking: one row per document in each channel. Each row carries this metadata:

```json
{
  "source_file":  "<index key>",
  "heading":      "<first markdown heading of the body, or empty>",
  "source_layer": "L5 | L6 | EXT",
  "source_path":  "<absolute path of the file at indexing time>",
  "char_count":   12345,
  "text":         "<preview: first 500 characters of the body>",
  "doc_hash":     "<first 16 hex chars of sha256(document text)>"
}
```

"Body" is the file text with a leading front-matter block removed: if the text starts with
`---`, everything up to and including the next line that reads `---` once trailing whitespace
is removed is dropped (knowledge
commits open with such an envelope of ids and signatures). If no closing line exists, nothing
is removed.

### 3.2 Index key scheme

| Layer | Key | Notes |
|---|---|---|
| L5 | `knowledge/<path relative to the agent's knowledge dir>` | Also a working `read_file` argument, because the knowledge dir sits directly in the agent's sandbox. |
| L6 | `L6/<path relative to the shared knowledge dir>` | |
| EXT | `EXT/<root tail>/<path relative to that root>` | Forward slashes always. |

The **root tail** of an EXT root is the shortest trailing run of its path segments that no
other configured root shares at the same depth (usually one segment, e.g.
`EXT/dpc-messenger/README.md`). Segments have `:` and slashes stripped; an empty segment becomes
`root`. A root configured as a file uses its parent directory. If two roots still end up with
the same tail, each gets `~<first 6 hex of sha256(normalised root path)>` appended. A file under
several roots is keyed by the longest (deepest) containing root. Path comparison uses the
platform's case rules (case-insensitive on Windows).

The key format has a version marker (`layer_addressed_v6`); a stored index whose marker
differs is rebuilt from scratch.

### 3.3 Stores

Per agent: a vector index (FAISS `IndexFlatIP`, exact inner product) with a JSON side file
holding the row metadata list and a `file_hashes` map `{index key -> doc_hash}`; a BM25 index
with its own row list, the full text of every row, and the corpus stop-word list (§6.3); a
graph database (§5); and two logs — the injection log `state/knowledge_access.jsonl` and the
tool log `logs/tools.jsonl` (rotated at 5 MB into `.1`).

The vector side file, `index_meta.json`, is shared by two writers: the vector index writes its
`header` and its row list (`chunks`) there, and the startup sync writes `file_hashes` and the
header fields `model_name`, `key_format` and `backend` into the same file. The header also
carries `max_chars: 1500` and `overlap_chars: 200` on every save: they are defaults of the
header structure left from a chunked design, and nothing reads them. There is no chunking
(§3.1); the two numbers describe nothing.

Per node, shared by all agents: the embedding cache, `~/.dpc/cache/embeddings` (§4.4).

An optional alternative backend (Grafeo: HNSW cosine vectors and Grafeo's own BM25 with k1=1.2)
can replace either channel per agent via config keys `retrieval_vector`, `retrieval_text`
(`native` | `grafeo`) and `retrieval_fusion` (`custom` | `grafeo`; `grafeo` currently behaves
identically to `custom`). Everything below describes the default native backend.

## 4. Indexing

### 4.1 What is indexed

| Layer | Directory | Files taken | Gate |
|---|---|---|---|
| L5 | the agent's `knowledge/` | every regular file **at the top level only** (not recursive), except `_meta.json`, `_index.md` and binary extensions | indexing enabled for the agent |
| L6 | the node's shared `knowledge/` | top-level `*.md` | the agent is allowed the `knowledge` context by the firewall |
| EXT | each path in the agent's `sandbox_extensions.indexed_paths` that is also one of its granted extended paths | `*.md` **recursively**, skipping any path that has a component in the excluded-directory list (`node_modules`, `.git`, `__pycache__`, `.venv`, `venv`, `target`, `dist`, `build`, `.tox`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `.next`, `.nuxt`, `.svelte-kit`, `coverage`, `htmlcov`, `.eggs`, `*.egg-info`, `bower_components`, `.gradle`, `.idea`, `.vs`, `bin`, `obj`; overridable per profile) | owner opt-in per path |

`.gitignore` is not consulted: gitignored Markdown under an indexed root is indexed. A file is
collected once even if several roots reach it, and a file already taken by L5 or L6 is not
collected again as EXT.

Binary extensions skipped: images, `.pdf`, archives, audio/video, fonts, compiled objects,
`.faiss`, `.npy`, `.npz`, `.pt`, `.onnx`. Text is read as UTF-8 with replacement and **cut at
100,000 characters**; for `.json` files the text is every string value longer than 2
characters, joined by newlines.

### 4.2 The document text

```
doc_text = key + " " + heading + " " + body      # heading omitted if empty
```

The same `doc_text` goes to both channels. The embedding model is `BAAI/bge-m3` (1024
dimensions) via sentence-transformers with `normalize_embeddings=True`. When the model loads,
its sequence limit is set to `min(max_tokens, the model's current limit)` (bge-m3 ships 8192);
a `max_tokens` of 0 or less leaves the model's own limit. "Current" is the value on the loaded
model at that moment, which after a first lowering is no longer 8192 (see the known defect
below). `max_tokens` is 4096
by default, so the embedding sees only the **first 4096 tokens** of a long document. BM25 keeps
the full `doc_text` (up to the 100,000-character cap).

The default 4096 is written in two places: the per-agent memory config field and the embedding
provider's own constructor. The provider is one object per model per process, shared by indexing
and by every agent's queries: whoever creates it first sets its device, and a later caller that
states `max_tokens` (the startup sync does, from the agent's memory config; the agent constructor
and the per-turn recall fallback do not) moves the shared window. Both defaults are 4096 today
and no agent config on this node sets `max_tokens` (checked 2026-11-06), so none of this is
visible. It becomes visible once the window is not the same everywhere: two agents with different
`max_tokens` share one window, so an agent whose index was built under one window can be queried
under the other's.

**Known defect: within one process the window can be lowered but never raised again.** Each
application computes `min(max_tokens, current)` against the model's *current* sequence limit,
not against the limit it shipped with. Once a caller has lowered 8192 to, say, 2048, a later
caller stating 4096 sets the shared provider's `max_tokens` to 4096 and re-applies, but
`min(4096, 2048)` is 2048. Until the process restarts, the provider's `max_tokens` says 4096 while
documents and queries are cut at 2048; no log line reports the window actually in force, and an
index rebuilt in that process is built under a smaller window than its config states. A rebuild
should keep the model's native limit from load time and apply `min(max_tokens, native)` each
time, so the window follows the stated value in both directions, capped at the native limit
(never above it: positions the model was not trained on give worse embeddings, not longer ones).

### 4.3 When it runs

* **Startup sync** (agent start, or lazily before the first prompt): collect all three layers,
  compute `doc_hash` for each, embed only documents whose hash changed or is new, delete rows
  for keys that disappeared, write `file_hashes` last as the commit point. That is the commit
  point of the staleness map only, not a snapshot a reader can rely on (§4.5). A full rebuild is
  forced when the embedding model, the key-format marker or the backend pair changed, or when
  the stored map lists documents while the stored index failed to load or holds no rows at all.
  A partial mismatch (the map lists more documents than the index holds, but the index is not
  empty) does not force a rebuild. Embedding runs in batches of
  16 (configurable). The startup sync runs on the agent's index writer thread, not on the event
  loop; startup does not wait for it. The window (`max_tokens`) is not part of the rebuild
  decision: changing it rebuilds nothing, and documents whose hash did not move keep their old
  vectors. A full rebuild embeds every document through the embedding cache (§4.4).
* **Live replace**: when the agent writes a file under `knowledge/`, that one key is removed and
  re-added in both channels; when a knowledge commit is approved, the L6 file is replaced the
  same way.
* EXT changes are picked up only by the next startup sync.

### 4.4 The embedding cache

Every embedding call goes through one disk cache before it reaches the model. The cache is one
object per process, in `~/.dpc/cache/embeddings` (under `DPC_HOME` when that is set), shared by
every agent and every embedding model of the node.

* **Key**: the first 16 hex characters of `sha256(text)`, one file `<key>.bin` per text. The
  text is the only input to the key: not the model, not the window, not the dimension.
* **Value**: the vector as raw float16, no header. Written to a temporary file and moved into
  place.
* **Get**: if the file exists, it is read as float16 and returned. Its length is not checked
  against the model's dimension.
* **Eviction**: none. Nothing deletes an entry.

`embed` (one text) and `embed_batch` (a list) both look in the cache first and return a hit
without encoding. A miss is encoded, written to the cache and returned. `embed` loads the model
before it looks, so even a hit needs the model loaded. The per-turn queries Q1 and Q2 (§6.1) go
through `embed` too: every distinct query text of every turn is written to the cache and kept.

**Known defect: a cached vector is reused under any model and any window.** The rebuild
decision of §4.3 compares the model name, the key-format marker and the backend pair; the full
rebuild then calls `embed_batch` and gets the cached vectors back for every document text that
was embedded before.

* A change of model with the same dimension rebuilds the index under the new model's name with
  the old model's vectors for every text already cached, and the new model's vectors for the
  rest. The two spaces are mixed silently; nothing fails. With a different dimension the
  rebuild fails instead.
* A change of window never takes effect for a cached text, whether the index is rebuilt or not.
* The cache stores float16 while the index stores float32 (every caller converts). A miss
  returns the encoder's output as computed; a hit returns it rounded to float16. Unless the
  encoder already produced float16 values, the same text gets slightly different numbers
  depending on whether it was cached.

An observation by the team on this node, 2026-11-07: the cache held 28,129 files, 3 of them of
another dimension, written into the same directory by tests that use fake encoders. It was not
re-measured for this document.

A rebuild should key a cached vector by the text **and** the embedding identity (model,
revision, effective window, normalisation), check a hit's length, store it at the precision the
index uses, and record the same identity in the index header so that any change of it forces a
rebuild (§14.3).

### 4.5 Readers and writers of the stored index

Writers of one agent's index are serialised in the process by one writer thread per index
directory (the startup sync, the live replace, the purge). Readers are not: the per-turn recall
(§6) builds a fresh backend and loads both channels from disk on every turn, with no lock
against the writer.

* **Live replace** removes the key's row and appends the new one at the end: the row count is
  unchanged and the order is new.
* **Vector save** writes `vectors.faiss` (to a temporary file, then moved), then
  `index_meta.json` with the row list. **Vector load** reads `index_meta.json` first, then the
  vectors, and checks only that the two counts are equal.
* **BM25 save** builds the index directory beside the live one and swaps it in, then writes
  `bm25_texts.json`, then `bm25_chunks.json` (the row list), then the corpus stop words. **BM25
  load** reads the directory, then the row list, and checks only that the counts are equal. The
  digest that `bm25_texts.json` carries ties the full texts to the row list when a mutation
  reads them; the per-turn load does not read the texts, so it does not tie the searched index
  to its row list.

**Known defect: a reader can pair one generation's index with another generation's rows.**
Each file is replaced atomically, but the pair is not. A turn that loads between the two writes
of a save gets new vectors with old rows (or the reverse); the counts agree, the load succeeds,
and a hit on row *i* is printed under the name and address of whichever document row *i* held in
the other generation. A process that stops between the two writes leaves that pair on disk. The
startup repair compares rows by key, not by which vector belongs to which key, so it does not
notice; the mismatch stays until a full rebuild. A rebuild should publish the index, its rows
and its tokeniser state as one generation with one atomic step, and have a reader take one
generation whole or refuse (§14.3).

## 5. The knowledge graph

Node types: `KnowledgeFile`, `SessionArchive`, `Entity`, `Decision`, `Agent`. A
`KnowledgeFile` node's id is `kf:<index key>`; it stores the key as `path`, the absolute
`source_path`, and a label `stem.replace("_", " ").title()`. Nodes are created after each
startup sync for every top-level `*.md` (not starting with `_`) in L5 and, when the gate allows,
L6. EXT documents get no nodes.

Edges carry a `source` marker, one of three:

| Source | Created by | Edges |
|---|---|---|
| `structural` | deterministic parse of L5 files after each startup sync (cleared and re-derived each time) | `KnowledgeFile -DEPENDS_ON-> KnowledgeFile` for a markdown link `[..](x.md)` or a bare `name.md` mention whose stem is another L5 file; `KnowledgeFile -DECIDED_BY-> Decision` for `ADR-NNN`; `KnowledgeFile -MENTIONS-> Entity` for each tag in `_meta.json`; `SessionArchive -DERIVED_FROM-> Agent` for each participant of an archived session |
| `gliner_ner` | the sleep pipeline: GLiNER (`urchade/gliner_multi-v2.1`, threshold 0.5, first 5000 chars, labels person, organization, technology, concept, location) over session summaries | `SessionArchive -MENTIONS-> Entity` |
| `llm_relation` | the sleep pipeline's synthesis LLM, relations with confidence >= 0.7 and a justification of >= 20 chars | `Entity -<type>-> Entity` |

**Graph expansion** (the graph channel). Seeds are the distinct index keys returned by the
vector and text channels. For each seed whose `kf:` node exists, take its neighbours at
distance <= 1 (edges followed in both directions, any type; invalidated edges are not
filtered). Keep a neighbour only if it is a `KnowledgeFile`, not already emitted, and its key is
not itself a seed. Emit `({source_file: key, source_path, source_layer: "L7", heading: label},
1.0)`. The score is unused downstream (fusion uses rank only); the emission order follows set
iteration and is effectively arbitrary.

Consequence of the edge shapes: at one hop, only `structural` `DEPENDS_ON` edges connect two
`KnowledgeFile` nodes, so the graph channel can only surface **L5 files linked by name from or
to an L5 seed**. `gliner_ner` and `llm_relation` edges exist in the store but cannot produce a
graph hit at this depth.

## 6. Per-turn retrieval

### 6.1 Gate and queries

The block runs on every turn where the current message text or the conversation history is
non-empty. Any exception outside the graph step skips recall for that turn: the user sees an
ordinary turn with no recall block, and the log records it — at INFO when the embedding model is
not cached locally, at WARNING with the traceback for anything else.

The graph step (§6.4) has its own handler. An exception there — while opening the graph store or
during the expansion — is caught at DEBUG with the fixed text `Active Recall Graph L7:
unavailable (cold start or no graph DB)` and no traceback, and the turn continues with the vector
and text channels only (`G` empty). A failure to open the store is swallowed one level lower,
with no log line at all: the opener returns "no graph", is not cached, and is retried on the next
turn. At the default INFO level, therefore, a broken graph looks exactly like an absent one.
**Known defect** (§14.2): a rebuild should keep absence (no graph store yet) silent but log a
failure to open or expand the graph at WARNING with the traceback, as it does for the rest of
recall, while still continuing the turn without the graph channel.

* **Q1** — the current message text exactly as the agent receives it. DPC's dispatchers prefix
  it with the sender, e.g. `[Mike]: <text>` or `[Group chat — Mike says]: <text>`.
* **Q2** — the last 10 entries of the conversation history (the current message excluded),
  keeping only `user` and `assistant` entries with non-empty content (others in those 10 slots
  are dropped, not replaced), their raw content joined with single spaces.

Neither query is preprocessed before embedding.

### 6.2 Vector channel

Skipped if the vector index is absent or was built with another model. Otherwise:
`V1 = search(embed(Q1), 10)` if Q1 is non-empty, `V2 = search(embed(Q2), 10)` if Q2 is
non-empty. `search` L2-normalises the query, takes the top 10 rows by inner product (cosine),
and drops rows whose key already appeared earlier **in that one result list**. The channel's
list is the concatenation `V = V1 + V2` — up to 20 entries, Q2's entries at positions 10..19,
and a document may appear once from each query.

### 6.3 Text channel

Query: `Q1` if non-empty, else `Q2`. Library: `bm25s` with defaults (method `lucene`, k1 = 1.5,
b = 0.75). Top 10, entries with score <= 0 dropped, per-key dedupe.

Corpus tokenisation (at index time):

1. Script detection on the first 500 chars: if more than 15 % are CJK, Arabic or Thai, use
   character bigrams of the lowercased text (pairs not starting with whitespace).
2. Otherwise split on whitespace, lowercase, drop tokens of length <= 1 and tokens in the
   stop-word set: stopwords-iso `ru` + `en`, plus **corpus stop words** — tokens present in more
   than 80 % of documents (computed only when the corpus has >= 5 documents).

The corpus layer exists but in practice rarely fires: on 2026-11-06 the index of agent Johnny
held 1327 documents and exactly one corpus stop word (`##`). The fixed ru+en list does the
filtering.

Query tokenisation: the same function with the same stop words, then the surviving tokens are
joined by spaces and passed a second time through `bm25s.tokenize` with the library's defaults:
lowercase, token regex `(?u)\b\w\w+\b`, and the library's English stop-word list (NLTK's). The
corpus never passes through that second step. What differs, on the query side only:

* punctuation is stripped: the corpus token `recall,` stays `recall,`, the query token becomes
  `recall`;
* a token with punctuation inside is split: `cross-layer` becomes `cross` and `layer`,
  `memory.py` becomes `memory` and `py`;
* a run of one word character left by that split is dropped (`c++` yields nothing), while the
  corpus keeps such tokens whole;
* NLTK's English stop words are removed as well, after stopwords-iso's ru and en lists were
  removed by the first step.

A query word therefore matches only corpus occurrences that stood between whitespace with
nothing attached, and a hyphenated or dotted corpus token (`cross-layer`, `memory.py`) can never
be matched by any query. **Known defect** (§14.2): a rebuild uses one tokeniser for corpus and
query.

### 6.4 Graph channel

`G = graph_expand(seeds = distinct keys of V and T)`, §5. Skipped when there are no seeds or no
graph, and dropped (with the turn continuing) when the step raises (§6.1).

### 6.5 Where it runs, and what bounds it

The whole of §6 to §10 runs synchronously inside the agent's asynchronous turn handler: prompt
assembly is a plain function call on the event loop, with no executor and no `await` around it.
In that call, in this order: both indexes are loaded from disk, Q1 and Q2 are embedded, BM25 is
searched, the graph is expanded, the lists are fused, and only then is the mode of §9 computed
and decay read from the two logs (§8). A `skip` turn has done all the searching before it
decides to inject nothing.

Embedding waits on two locks shared with other work. Encoding holds a semaphore of one slot that
belongs to the embedding class, so it is shared by every model and every agent in the process;
the startup sync (§4.3) holds the same semaphore for each batch of cache misses (16 documents by
default, each up to the window). And a call that finds the model not yet loaded loads it right
there, or waits on the provider's load lock while another thread loads it. While a turn waits,
the event loop it runs on serves nothing else.

Failure scope: one `try` covers loading, the vector channel, the text channel, the graph step
and fusion (the graph step has its own inner handler, §6.1). An exception in the vector channel
— including the embedding model not being cached locally — skips the text channel too, though
the BM25 index needs no model.

There is no deadline and no cancellation point: a stop request reaches the model loop that
follows, not this stage. **Known defect** (§14.2): a rebuild checks the settings and the mode
before any search, runs retrieval off the event loop under a deadline, continues the turn
without recall when the deadline passes (without logging a late result as an injection), and
lets one channel fail without taking the others with it.

## 7. Fusion

Weighted Reciprocal Rank Fusion with k = 60, accumulated per index key:

```
for list in (V, T, G):                     # default layer: "L5" for V and T, "L7" for G
    for rank, (meta, _) in enumerate(list):           # rank starts at 0
        w = LAYER_WEIGHTS.get(meta.source_layer, 1.0)
        score[key] += w / (60 + rank + 1)
        keep meta of the occurrence with the highest w
candidates = sort keys by score, descending (stable)
```

| Layer label | Weight |
|---|---|
| L1 | 1.3 |
| L6 | 1.0 (1.5 in the code from its first version, 2026-04-18, until commit ed589b36 on 2026-08-12) |
| L5 | 1.0 |
| EXT | 0.9 |
| L2 | 0.8 |
| L7 (graph channel) | 0.6 |
| anything else | 1.0 |

Ranks inside V run 0..19, so a document found by Q2 is scored as if ranked 11th..20th. A
document found by both queries and the text channel collects three terms.

## 8. Decay (access-history multiplier)

Skipped entirely when the mode is `skip` (§9). Otherwise, for each candidate:

**Evidence.**
* *Injections* — each line of the injection log (§11) counts +1 for each key in its `files`.
* *Reads* — each line of `logs/tools.jsonl` and its rotated copies with `tool == "read_file"`
  that is a finished call: the test is only that the row's `phase` is not `attempt`. The row's
  `is_error` is never consulted, so a read that failed — access denied, file not found — counts
  as a full read (1.0), the same as one that returned the document. The `args.path`
  is counted under the absolute path (normalised) if absolute, else under the relative key
  (separators normalised to `/`, leading `./` stripped).
* *Window* — injection lines older than the oldest surviving read are ignored, so both sides
  cover the same period. With no reads at all, every injection counts.

**Access value** of a document (looked up both by its key and by its `source_path`):

```
access = reads + (min(1, injections / 20) * 0.3   if injections > 0 else 0)
```

**Multiplier**, normalised within this candidate set:

```
max_access = max(access over all candidates)
if no evidence at all, or max_access <= 0, or logs unreadable: no decay
multiplier = max(0.1, access / max_access)          if access > 0
           = 1.0   if access == 0 and 0 <= (now - the file's mtime) < 7 days
                   (no source_path, a failed stat, or an mtime in the future: not in grace)
           = 0.1   otherwise
score = fused_score * multiplier;  re-sort descending (stable)
```

| Constant | Value | Status in code |
|---|---|---|
| floor | 0.1 | ADR-013 |
| grace period | 7 days by file mtime | ADR-013 said 5 sessions; days because no session counter exists at this layer |
| injection credit cap | 0.3 | placeholder, "chosen by argument, not from data" |
| injection saturation | 20 | placeholder, same note |

A single read (1.0) always outranks any number of injections (at most 0.3) *in the access
value*; on the final order a candidate still wins if its fused score is more than ~3.3 times
better.

Grace applies only at `access == 0`, and an injection makes access non-zero. One counted
injection gives access 0.015 (1/20 x 0.3), so a document modified two days ago that has been
shown once leaves grace: its multiplier becomes `max(0.1, 0.015 / max_access)`, which is the 0.1
floor whenever the set's maximum is above 0.15. Grace protects a new document until its first
injection, not for seven days.

**What decay is not.** The name is the code's and ADR-013's and this document keeps it, but the
multiplier has no term that grows with age: a document is not discounted for being old, only for
being shown and read less than the other candidates in the same set. Age enters in two places
only. One is the grace test on the file's mtime above. The other is the window of surviving
logs: `tools.jsonl` rotates at 5 MB into a single `.1` copy, the older copy being deleted, and
the window opens at the oldest read still on disk. Every tool call writes to that log, not only
reads, so how far back the evidence reaches is set by the volume of tool calls, not by a time
span. Decay is therefore not a guarantee of forgetting, nor evidence that a document is current.

## 9. Selection and mode

Context usage ratio = (prompt tokens measured after the last response in this chat) /
(context window); if no prompt has been measured yet in this chat (for example the first turn
in a chat after a restart), history tokens / window, and 0 if neither figure is known. History
alone is a lower bound on the prompt, so on such a turn the mode can be fuller than the real
usage would allow.

| Ratio | Mode | Effect |
|---|---|---|
| < 0.5 | `full` | hints with address and excerpt |
| 0.5 <= r < 0.7 | `hints` | names only, no addresses |
| >= 0.7 | `skip` | nothing injected, nothing logged |

The mode depends on this ratio alone. Nothing about the query or the candidates is consulted:
no relevance threshold, no test of whether the message carries a topic. A message like "ok,
continue" gets up to three hints whenever any candidate survives the filter below (§13.1).

After decay, drop candidates that have **nothing to offer**: a candidate stays if it has an
address (§10); otherwise it stays only if it has a preview `text` and is not an L6 document
with the shared-knowledge gate closed. Then take the **first 3**.

**Settings that do not reach this path.** The per-turn path reads no memory setting at all. It
runs whenever the current message or the history is non-empty and an index loads from disk.

* `memory.enabled = false` stops the startup sync (§4.3) but not per-turn recall: an index
  already on disk keeps being searched and injected.
* `memory.active_recall` is read only for one log line at startup ("Memory system initialized
  (model=..., active_recall=...)").
* `memory.max_recall_results` is read nowhere; the slot count is the renderer's default of 3.
* The agent permissions panel of the UI shows an "Active Recall" checkbox ("Inject relevant
  memory hints into agent context automatically") bound to `memory.active_recall`; switching it
  off changes nothing.

**Known defect** (§14.2): a rebuild reads these settings on the per-turn path, before any
search, so that off means off and the slot count is the configured one.

## 10. Rendering and placement

**Address**: an L5 key is its own address. Any other document's address is its `source_path`,
but only while the corresponding gate is open now (shared-knowledge access for L6, extended-path
read access for EXT and for any non-L5 graph hit); otherwise there is no address.

The two gates are asked at the start of each turn, but each is one switch for the whole layer.
For L6 that is the whole permission, and a closed gate drops the candidate at selection (§9).
For EXT the recall path receives only the agent's global `extended_read_enabled` flag, never the
grant of the candidate's own path. With that flag on, an EXT document keeps its heading, its
excerpt and its address after the owner removes its directory from the agent's granted paths,
until the index drops the row at the next start of the agent manager or of the service: the
startup sync is the only thing that re-collects EXT, and the hook the UI calls after a firewall
save (`sync_firewall_settings` in the agent manager) does nothing. `read_file` asks the current
grants, so it refuses the very address the hint printed. **Known defect** (§14.2): a rebuild
checks each candidate's own path against the agent's current grants before selection and drops
a revoked one whole — heading, excerpt and address, not the address alone.

Separately, and on purpose: with the EXT flag off, an EXT candidate keeps its slot and its
excerpt and prints the dead end below instead of an address. The tests pin this as policy — the
flag governs reaching the file, not seeing that it exists — and it is not a defect.

**Full mode** block (exact text, one hint per candidate):

```
Active Recall
--- ACTIVE RECALL ---
If a recalled file looks relevant to the current discussion, call read_file() to get full content.
[<layer>] <index key>: <heading, or the key if no heading> — call read_file("<address>") for details
    <excerpt>
--- END RECALL ---

```

* With no address the action reads `not readable from here — extended path read access is off`
  (or `— shared knowledge access is off` for L6).
* `<excerpt>` = first 200 characters of the stored 500-character preview, newlines replaced by
  spaces, stripped. A row with no preview (every graph hit — its meta has no `text`) prints
  `(chunk 0)` instead.
* `<layer>` is the meta's `source_layer` (default `L5`); for a document found by several
  channels it is the label of the highest-weight occurrence.

**Retrieved text in the block.** Only the excerpt has a length cap (200 characters). The heading
is the whole first heading line of the document, with no cap of its own (only the
100,000-character cut on the text bounds it). The key, the label and the address are
interpolated as they are, inside the block and, for the address, inside the quotes of
`read_file("…")`; nothing escapes a quote or a delimiter, so a heading that reads
`</turn_context>` reaches the prompt as written. Nothing downstream caps the block either: the
soft cap that trims the per-turn tail when the prompt is too large drops only the recent-activity
sections, never the recall block. All of it then stands inside the `<turn_context>` tail that a
cached system section tells the model is written by the runtime (see Placement). The risk comes
from the missing caps, not from sizes seen so far: on 2026-11-07 the longest heading in agent
Johnny's index was 206 characters (a team observation). **Known defect** (§14.2): a rebuild caps
each retrieved field and the block as a whole, keeps retrieved text from closing or imitating the
runtime's delimiters, and frames it as quoted content rather than runtime instruction.

**Hints mode** block:

```
Active Recall
--- RECALL HINTS: [<layer>] <key1>, [<layer>] <key2>, [<layer>] <key3> ---
```

**Placement.** The block is not part of the system prompt. It is the first part of a per-turn
tail appended to the **current user message, after all history**:

```
<current message>

<turn_context>
<recall block>

<runtime section>

<sandbox paths> ...
</turn_context>
```

Parts are joined with blank lines. A cached system section tells the model that this tail is
written by the runtime, not the user. Earlier turns are replayed with the tail they carried, so
old recall blocks stay in the history byte for byte (this keeps the engine's prefix cache valid).

The replay asks no gate. An L6 excerpt injected before the shared-knowledge gate was closed is
sent again, unchanged, on every later turn whose history still holds that message, although a
new turn would no longer select the document (§9); the same holds for an EXT excerpt after its
path was revoked. The hints carry no `doc_hash` and no date, so neither an old replayed hint
nor a fresh one says which version of the document it quotes; an EXT preview stays the version
of the last startup sync (§4.3) while `read_file` returns the file as it is now. **Known
defect** (§14.2) for the replay after revocation: a rebuild decides explicitly what a replayed
tail does once the gate behind it is closed, rather than re-sending it by default. It cannot make
a model that already received the excerpt forget it.

## 11. Logging and the feedback loop

Whenever at least one candidate is injected (modes `full` and `hints`), one line is appended
to `state/knowledge_access.jsonl`:

```json
{"ts": "<UTC ISO>", "task_id": "<turn id>", "mode": "full",
 "files": ["<key1>", "<key2>", "<key3>"],
 "addresses": ["<printed address or null>", "..."], "useful": null}
```

`addresses` is empty in `hints` mode. The printed addresses are also kept in memory under the
turn's task id (in DPC the conversation id; up to 32 ids are kept); when the agent then calls
`read_file` under that id, the tool log row gets `"via_hint": true|false`, or no field when
nothing is stored under the id. `via_hint` is recorded for measurement; decay does not use it.

**Known defect: a turn that printed no address does not replace the previous offer.** The
stored set is overwritten only by a turn that printed at least one non-null address. A turn in
`hints` mode, a `full` turn whose every slot had no address, a turn with no candidate left, a
`skip` turn and a turn where recall failed all leave the earlier turn's set in place under the
same conversation id. A read in such a turn is then labelled `via_hint` against an older turn's
offer: `true` if it opens a document printed there, `false` instead of absent otherwise. Because
the id is the conversation, not the turn, this reaches back across any number of turns until the
next turn that prints an address.

The same store also has no agent in its key. It is one map for the whole process, keyed by the
task id alone, and the task id is the conversation id; in a group that is the group id, the same
for every agent of the node that speaks there. So the defect crosses agents: an agent's turn in a
group that prints no address inherits the offer another agent printed in that group, and its
reads are labelled against that agent's hints. Turns of different agents can also overlap. A
local mention in a group runs under the group's room gate, which lets one agent at a time into
that room; a mention that arrives from a peer node wakes the node's default agent through the
group handler, which does not take the room gate. While that agent waits for its model, another
agent's turn in the same group can overwrite its offer, and its reads are then judged against
the other agent's hints.

A rebuild should record every turn's offer, empty included, keyed by the agent and an id unique
to the turn, so that a read in a turn that printed nothing carries no `via_hint` and no agent's
read is judged against another agent's offer.

The loop closes on the next turn: injections and reads both feed §8. At each agent start,
injection lines older than the read window are moved to `knowledge_access.archive.jsonl`
(nothing is deleted).

The INFO log line per turn reports what actually went in, with post-decay scores:
`Active Recall injected 3 of 29 candidates (mode=full): key(0.018), ...`.

**One measurement (agent Johnny, 2026-11-06).** Population: his injection log (98 lines,
2026-10-23 to 2026-11-06; 80 `full`, 18 `hints`) and the finished `read_file` rows of his tool
log and its rotated copy (812 rows, 2026-10-22 to 2026-11-06). The injections named 64 distinct
keys. Matched the way §8 matches (by key or by `source_path`), 4 of those 64 had any read, 8
reads in total. All 812 read rows carry `via_hint`: 3 are `true` (two reads of one document in
one group chat, one read of another), 809 `false`. These are one agent's counts over two weeks,
not a follow-rate. The labels were written by the mechanism with the defect above, so a row may
have been judged against an earlier turn's offer; the counts were not corrected for that.

## 12. Worked example (agent Johnny, 2026-11-06)

The owner wrote in a group, addressing the agent: "@Johnny, you take a look too, think it
over" (translated). The previous ten messages were about an audio-editor idea, a GitHub link and
a song file. The prompt-size line logged for the turn read 37,419 / 250,000 tokens (15 %). That
line is an estimate of the assembled prompt, raised to the last measured prompt when that is
larger; it is not the ratio recall read, which is not logged. Recall's ratio is the last
measured prompt or the history alone (§9), neither of which exceeds the estimate, so it was at
most about 15 %: mode `full`.

**Channels** (from the debug log of that turn).
* V1 (Q1): ten documents, nearly all named after Johnny (his retrospectives, his identity file,
  an audit prompt with his name) — the only content word in Q1 is the agent's name. Position 0
  was `knowledge/retro-2026-09-08-observability-day-johnny.md`, position 8
  `EXT/dpc-messenger/ideas/dpc-research/grable_catalog.md`.
* V2 (Q2): ten EXT documents (positions 10..19), including two generated evaluation dumps.
* T: ten documents; position 1 `grable_catalog.md`, position 2 `EXT/dpc-messenger/backlog.md`,
  position 9 `retro-2026-09-08-...`.
* Seeds: 30 entries, 2 keys repeated, so 28 distinct. Graph: the knowledge graph held 1039
  nodes and 1625 edges (`gliner_ner` 1029, `llm_relation` 485, `structural` 111); expansion
  returned one file, `knowledge/retro-2026-09-10-gateway-tariff-johnny.md`. Candidates after
  fusion: 28 + 1 = 29.

**Fusion and decay** for the three injected documents and the pre-decay leader:

| Document | Layer | Fused score | Access (reads + credit) | Multiplier | Final |
|---|---|---|---|---|---|
| `retro-2026-09-08-observability-day-johnny.md` | L5 | 1.0 x (1/61 + 1/70) = 0.03068 | 0 + 3/20 x 0.3 = 0.045 | 0.045/0.3 = 0.15 | 0.0046 |
| `grable_catalog.md` | EXT | 0.9 x (1/69 + 1/62) = 0.02756 | 0 + 13/20 x 0.3 = 0.195 | 0.65 | **0.0179** |
| `backlog.md` | EXT | 0.9 x 1/63 = 0.01429 | 0 + 0.3 (>= 20 injections) | 1.0 | **0.0143** |
| `retro-2026-09-10-gateway-tariff-johnny.md` | L7 | 0.6 x 1/61 = 0.00984 | 0 + 12/20 x 0.3 = 0.18 | 0.6 | **0.0059** |

The access figures are back-solved from the logged scores (0.018, 0.014, 0.006) and agree with
the injection log: before this turn it held, for the four rows in table order
(`retro-2026-09-08`, `grable_catalog.md`, `backlog.md`, `retro-2026-09-10`), 3, 13, 62 and 12
injections, and zero reads for any of them. These counts were measured on 2026-11-06 and are a
snapshot, not constants: every turn that injects appends one line and adds one to each key it
names. After this turn and the next two the same four stood at 3, 15, 65 and 13; when the log
reached 98 lines later that day, `backlog.md` stood at 66. Because `backlog.md` received a multiplier of 1.0 while
its access was the 0.3 cap, no candidate of the 29 had a single counted read: the whole
reordering was done by injection counts. The fused leader fell to fourth; a graph hit, at the
lowest weight and with no excerpt, took the third slot. Its hint printed `(chunk 0)` as the
excerpt.

## 13. Known limitations and divergences from the ADRs

Limitations of the design as built: here the code does what it was written to do, and whether
that is the right thing is open. Known code defects — the code doing something other than what it states, or
breaking a contract the rest of the system relies on — are listed in §14.2.

1. **Content-free messages produce noise, and nothing abstains.** A message like the one in §12
   has no topic; Q1 matches the sender's and agent's names and Q2 matches whatever the last ten
   messages were about. Nothing detects that the query carries no information, and the mode
   depends only on how full the context is (§9): a message like "ok, continue" gets up to three
   hints whenever any candidate exists.
2. **Decay reinforces itself.** An injection earns credit, and credit raises the multiplier,
   so a document shown often keeps being shown while nobody opens it; with zero reads in the
   set (§12) the ranking is decided by past injections alone. The cap (0.3) and saturation (20)
   are documented placeholders. A failed read counts as a full read, one injection ends a new
   document's grace, and the evidence reaches back as far as the tool log's volume allows, not
   a time span (§8).
3. **One vector per whole document.** Each document gets one vector, built from its first 4096
   tokens; a large catalog or backlog is represented by its opening and competes with short
   notes on equal terms, and its hint quotes only its first 200 characters. BM25 is not the
   problem here: `bm25s` normalises for document length (b = 0.75). The limit is the dense
   window plus the 200-character excerpt.
4. **Graph hits carry no excerpt** (`(chunk 0)`), and at one hop only L5 name links can
   produce them (§5); the NER and LLM edge classes do not reach the channel.
5. **Everything `.md` under an indexed root is indexed**, gitignored included, so generated
   dumps and working notes compete with documentation.
6. **ADR-020 query preprocessing is only partly live.** Its decision is a pipeline applied
   upstream of all channels (language detection, standard stop words, corpus stop words,
   user stop words). In the live path only the BM25 channel filters stop words (fixed ru+en
   plus corpus stops, plus a second English list on the query side, §6.3); the vector channel
   embeds Q1 and Q2 raw; there is no language detection and no user layer.
7. **Q2 truncation keeps the oldest text.** The embedding keeps the first 4096 tokens of its
   input and drops everything after them. Q2 joins the ten messages oldest first, so on a long
   history the tokens that survive are those of the oldest messages, and the newest messages —
   the ones closest to the current turn — are the ones cut.
8. **No end-to-end measurement.** The retrieval evaluation in `eval/retrieval/`
   (`run_retrieval_eval.py`) asks whether a card is found by its own title, with
   one query through the text channel, the vector channel and their fusion. It does not run Q2,
   the graph channel, decay, the filter or the renderer, nor does it ask whether the agent
   opened or used a hint. Its published fused recall@1 of 0.968 (agent_001, 2026-08-24, 189
   queries) is not reliable for today's keys: its matcher falls back to comparing bare
   filenames when the full key does not match, and with layer-qualified keys that fallback does
   almost all the matching, so a namesake in another layer or root counts as a hit. The one
   measurement of use in this document is §11's, which states its own limits.
9. **Divergences from the ADRs:** BGE-M3 sparse output (ADR-018) is not used — the text channel
   is BM25 only; ADR-018 planned the whole history as the context query, the code uses the last
   10 messages; grace is 7 days, not 5 sessions (ADR-013); ADR-010 fixes no weights, only an
   order, L6 > L1 > L5 > L2, and the weight table no longer follows it (L1 1.3 is above L6 1.0
   since the code's own 1.5 for L6 was removed on 2026-08-12); L1 and L2 have weights but no
   producer. Of the per-agent memory config fields, `active_recall` is read only for a log line
   at startup and `max_recall_results` is read nowhere; neither reaches the live path, which
   runs whenever an index exists, with the slot count fixed at 3 (§9; the defect is in §14.2).

## 14. Rebuilding it

Three blocks, kept apart on purpose. §14.1 is the build at commit `14aaf88b` as it is, for
someone who wants to reproduce its rankings exactly. §14.2 lists what that build gets wrong;
reproducing those items reproduces the defects. §14.3 is the short list of contracts a correct
rebuild holds, whatever else it changes.

### 14.1 As built at `14aaf88b`

Checklist:

- [ ] Key scheme of §3.2 and one row per document with the metadata of §3.1.
- [ ] Front-matter strip, heading extraction (`^#+ (.+)$`, first match), `doc_text` of §4.2,
      100,000-char cap, 500-char preview.
- [ ] A multilingual embedding model with normalised output (bge-m3, 1024-d, 4096-token cut) and
      an exact cosine index.
- [ ] BM25 (`bm25s` defaults: method `lucene`, k1 1.5, b 0.75). The two tokenisers of §6.3 are
      the build's; a rebuild uses one tokeniser for corpus and query (§14.2, §14.3).
- [ ] Optional graph with `DEPENDS_ON` edges between knowledge files that link each other.
- [ ] Per turn: Q1, Q2, three channels, RRF with the weight table, decay, mode, filter, top 3,
      render, append to the current user message, log.
- [ ] Two logs: injections (written by recall) and reads (written by your file-read tool).

Minimal pseudocode of the build's ranking (it leaves out the embedding cache of §4.4, the
loading of §4.5 and the blocking of §6.5, which §14.2 says not to copy):

```python
K, TOP_V, TOP_T, SLOTS = 60, 10, 10, 3
W = {"L1": 1.3, "L6": 1.0, "L5": 1.0, "EXT": 0.9, "L2": 0.8, "L7": 0.6}

def recall(task_text, history, usage_ratio, now):
    q1 = task_text
    q2 = " ".join(m.content for m in history[-10:]
                  if m.role in ("user", "assistant") and m.content)
    if not (q1 or q2):
        return None
    V = (vec.search(embed(q1), TOP_V) if q1 else []) + \
        (vec.search(embed(q2), TOP_V) if q2 else [])      # each list deduped by key
    T = bm25.search(q1 or q2, TOP_T)                       # score > 0, deduped by key
    seeds = {m.key for m, _ in V + T}
    G = [n for n in graph.neighbours_1hop(seeds)
         if n.type == "KnowledgeFile" and n.key not in seeds]   # layer "L7"

    score, meta = {}, {}
    for lst, default in ((V, "L5"), (T, "L5"), (G, "L7")):
        for rank, (m, _) in enumerate(lst):
            w = W.get(m.layer or default, 1.0)
            score[m.key] = score.get(m.key, 0) + w / (K + rank + 1)
            if m.key not in meta or w > W.get(meta[m.key].layer, 1.0):
                meta[m.key] = m
    cands = sorted(score, key=lambda k: -score[k])

    mode = "skip" if usage_ratio >= 0.7 else "hints" if usage_ratio >= 0.5 else "full"
    if mode == "skip":
        return None

    try:
        ev = access_evidence()                  # both logs, one window (section 8)
    except LogUnreadable:
        ev = None                               # evidence unreadable: no decay
    if ev:                                      # no evidence at all: no decay
        acc = {k: ev.reads(k, meta[k].path) +
                  (min(1, ev.inj(k) / 20) * 0.3 if ev.inj(k) else 0) for k in cands}
        top = max(acc.values(), default=0)
        if top > 0:
            for k in cands:
                if acc[k]:
                    mult = max(0.1, acc[k] / top)
                else:
                    mult = 1.0 if in_grace(meta[k].path, now) else 0.1
                score[k] *= mult
            cands.sort(key=lambda k: -score[k])

    cands = [k for k in cands if address(meta[k]) or
             (meta[k].preview and not shared_and_gate_closed(meta[k]))]
    chosen = cands[:SLOTS]
    block = render(chosen, mode)                # formats of section 10; "" if chosen is empty
    if chosen:                                  # nothing selected: nothing logged
        log_injection(chosen, mode, addresses(chosen) if mode == "full" else [])
    return block                                # appended to the current user message

def in_grace(path, now):
    if not path:
        return False                            # no path: treated as old
    try:
        age = now - mtime(path)
    except OSError:
        return False                            # cannot stat: treated as old
    return 0 <= age < 7 * 86400                 # an mtime in the future is not in grace
```

### 14.2 Do not carry over

Known code defects of the build in §14.1. Each is described where the mechanism meets it, and
that description says what a rebuild does instead. Describing them here fixes none of them.

1. **A cached vector is reused under any model and any window** (§4.4). The cache key is the
   text alone; a rebuild after a model change takes old vectors back, a window change never
   reaches a cached text, and a hit is rounded to float16 where a miss is not.
2. **The embedding window can be lowered but not raised in one process** (§4.2). The limit is
   taken as `min(max_tokens, current)` against the already-lowered value, so the provider's
   `max_tokens` can say more than the window in use, unlogged.
3. **A reader can pair one generation's index with another generation's rows** (§4.5). Vectors
   and rows, and the BM25 directory and its rows, are published one after the other and loaded
   with a count check only; a hit can be printed under another document's name and address, and
   a crash between the writes persists until a full rebuild.
4. **The query and the corpus are tokenised differently** (§6.3). The query passes a second
   tokeniser that strips punctuation, splits hyphenated and dotted words and drops one-character
   runs; the corpus does not.
5. **Recall blocks the event loop and has no deadline** (§6.5). It runs synchronously, waits on
   a semaphore shared with startup indexing, decides `skip` only after searching, cannot be
   cancelled, and lets a vector-channel failure take the text channel with it.
6. **The settings do not reach the per-turn path** (§9). `memory.enabled = false`,
   `memory.active_recall`, `memory.max_recall_results` and the UI's "Active Recall" checkbox
   change nothing about per-turn recall.
7. **Authorization for EXT is per layer, not per path** (§10). A path removed from the agent's
   grants keeps its heading, excerpt and address in recall until the next manager or service
   start, while `read_file` already refuses it.
8. **Replayed tails ignore revocation** (§10, Placement). An L6 or EXT excerpt sent before its
   gate closed is re-sent byte for byte on later turns.
9. **Retrieved fields are uncapped and unescaped** (§10). The heading, key and address have no
   cap and nothing escapes quotes or `</turn_context>`; nothing caps the block downstream.
10. **A turn that printed no address keeps the previous offer, and the offer has no agent**
    (§11). Reads are labelled `via_hint` against an older turn's offer under the same
    conversation id, or against another agent's offer in the same group.
11. **A graph failure is logged as absence** (§6.1). Opening or expanding the graph can fail with
    no log line (open) or a DEBUG line with no traceback (expand).

### 14.3 Invariants of a correct rebuild

Contracts, not an implementation. A rebuild that holds all of them may differ from §14.1 in
anything else.

- [ ] **Embedding identity.** Every stored or cached vector belongs to one embedding identity —
      model, revision, effective window (`min(stated window, the model's native limit)`, applied
      the same in both directions), normalisation. Documents and queries of one index are
      embedded under the same identity; a cached vector is reused only under that identity and
      after a length check; the index records the identity, and any change of it forces a
      rebuild.
- [ ] **Index generation consistency.** The vectors, the rows and the BM25 state a search uses
      come from one generation. A writer publishes a generation in one atomic step; a reader
      takes one generation whole, or refuses; startup verifies that rows and vectors belong
      together, not only that their counts agree.
- [ ] **Per-path authorization at render time.** Before selection, each candidate's own path is
      checked against the agent's current grants for its layer, the way `read_file` would check
      it. A revoked document contributes nothing — no heading, no excerpt, no address — and what
      replayed tails do after revocation is a stated decision, not a side effect.
- [ ] **Bounded, non-blocking recall.** Retrieval runs off the event loop under a deadline and
      can be cancelled; the settings and the mode are checked before any search; one channel's
      failure does not take the others with it; a result that arrives after the deadline is not
      injected and not logged as an injection.
- [ ] **One tokeniser for corpus and query**, versioned with the BM25 index, so that changing it
      rebuilds the index.
- [ ] **Working settings.** On/off and the slot count are read on the per-turn path; memory
      switched off means no recall; a setting the UI offers changes what it says it changes.
- [ ] **Per-turn, per-agent offer identity.** Every turn's offer is recorded, empty included,
      keyed by the agent and an id unique to the turn; a read is judged only against its own
      turn's offer.
- [ ] **Capped retrieved fields.** The heading, key, address and excerpt each have a cap, the
      block as a whole has one, retrieved text cannot close or imitate the runtime's
      delimiters, and it is framed as quoted content, not as runtime instruction.

## Appendix — where it lives in DPC

Paths are relative to `dpc-client/core/dpc_client_core/` unless they start at the repository
root (marked *repo:*); line numbers as of commit `14aaf88b` on `dev` (2026-11-06).

| What | Where |
|---|---|
| Per-turn orchestration (gate, Q1/Q2, three channels, fusion call, placement) | `dpc_agent/context.py:726-829`, tail placement `:852-853`, `:990` |
| Channel sizes, hop limit, task text | `dpc_agent/context.py:38-40`, `task_query` `:57` |
| Context usage ratio | `dpc_agent/context.py:143`; figures from `managers/agent_manager.py:1896-1901` |
| Thresholds, decay constants | `dpc_agent/active_recall.py:27-59` |
| Address, render, hints mode, mode choice | `dpc_agent/active_recall.py:68`, `:130`, `:186`, `:174` |
| Filter, selection, logging | `dpc_agent/active_recall.py:222`, `:251`, `:572` |
| Access counts, grace, multiplier, decay | `dpc_agent/active_recall.py:287-349`, `:398`, `:472`, `:490`, `:504` |
| `via_hint`, log compaction | `dpc_agent/active_recall.py:557`, `:604`; offer kept only when an address was printed `:544-553`; `dpc_agent/loop.py:1500-1504` |
| RRF and layer weights | `dpc_agent/hybrid_search.py:13-80` |
| Vector index | `dpc_agent/faiss_index.py:70` (search) |
| BM25 index and tokeniser | `dpc_agent/bm25_index.py:53-73`, `:107`, `:178`, `:231` |
| Backend selection | `dpc_agent/retrieval/factory.py:42`, `:92`; `retrieval/native.py:168`; `retrieval/grafeo.py:178`, `:409`, `:565` |
| Graph expansion, node ids, structural edges, node import | `dpc_agent/knowledge_graph.py:1494`, `:71`, `:1528`, `:1455` |
| GLiNER and LLM relation edges | `dpc_agent/knowledge_graph.py:113-116`; `dpc_agent/sleep_pipeline.py:1104`, `:1255-1280` |
| Key scheme | `dpc_agent/index_keys.py` (whole file; `KEY_FORMAT` `:56`) |
| Document fields, preview, hash, live replace | `dpc_agent/indexing_pipeline.py:47-118`, `:170` |
| Text extraction | `dpc_agent/text_extract.py:23` |
| EXT collection | `dpc_agent/extended_paths_index.py:23-32`, `:158` |
| Startup sync | `managers/agent_manager.py:313` (`_init_memory_indexes`), `:367-680` |
| Live reindex triggers | `dpc_agent/tools/core.py:362-391` (L5 write); `knowledge_service.py:1621-1627` (L6 commit) |
| Embedding model and limits | `dpc_agent/memory.py:361-364`, `:673-707`; `dpc_agent/memory_config.py` |
| `max_tokens` defaults, shared provider | `dpc_agent/memory_config.py:30`; `dpc_agent/memory.py:556` (constructor default), `:407-441` (per-process singleton, later `max_tokens` applied), `:686-696` (`min(limit, current)`, the never-raised window), `:668-671` (applied at load); callers `dpc_agent/agent.py:210-218` (no `max_tokens`), `managers/agent_manager.py:387-399` (`mem_cfg.max_tokens`, re-applied to an existing provider), `dpc_agent/context.py:757` (per-turn fallback, no `max_tokens`) |
| Rebuild when the map outlives the index | `dpc_agent/indexing_pipeline.py:466`; caller `managers/agent_manager.py:516-518` |
| Recall failure logging, usage-ratio fallback | `dpc_agent/context.py:824-829`; `:143-154` |
| Graph-step failure handling | `dpc_agent/context.py:800-801` (expansion, DEBUG, no traceback); `:45-54` (opener, no log, not cached) |
| Prompt-size estimate log line | `dpc_agent/agent.py:491-515` |
| Index side file shared by two writers, unused header fields | `dpc_agent/faiss_index.py:34-41` (`max_chars`, `overlap_chars`), `:157-165`; `managers/agent_manager.py:641-647` (`model_name`, `key_format`, `backend`, `file_hashes`) |
| Embedding cache | `dpc_agent/memory.py:457-495` (key, float16 value, get without length check, put), `:497-507` (one per process); hits before encoding `:698-707` (`embed`, loads the model first), `:733-755` (`embed_batch`) |
| Rebuild decision without the window; vectors stored as float32 | `dpc_agent/indexing_pipeline.py:385-444`; `:193` (live replace); `managers/agent_manager.py:575-600` (startup batches through `embed_batch`, `:591`); `dpc_agent/context.py:763`, `:766` (queries) |
| Index writers, readers, publish order, count-only load | `dpc_agent/index_writer.py:1-24`; live replace `dpc_agent/indexing_pipeline.py:170-199`; `dpc_agent/faiss_index.py:149-166` (save), `:168-196` (load, count check `:185-192`); `dpc_agent/bm25_index.py:281-336` (save), `:338-375` (load, count check `:361-368`); repair by key `dpc_agent/indexing_pipeline.py:249-290` |
| Second query tokeniser | `dpc_agent/bm25_index.py:235-237`; library defaults in `bm25s/tokenization.py` (`tokenize`: `token_pattern=r"(?u)\b\w\w+\b"`, `stopwords="english"`, NLTK's list) |
| Synchronous assembly, shared semaphore, one `try`, no deadline | `dpc_agent/agent.py:327`, `:448-468` (plain call inside `async process`); `dpc_agent/memory.py:553` (class-level `threading.Semaphore(1)`), `:563`, `:622-627` (load lock), `:704`, `:747`; `managers/agent_manager.py:705-706` (startup sync on the index writer); `dpc_agent/context.py:743-829` (one `try`), `:803-805` (mode after fusion, via `dpc_agent/active_recall.py:261`) |
| Settings read nowhere on the per-turn path | `dpc_agent/memory_config.py:15-18`; `managers/agent_manager.py:322` (`enabled`, startup only), `:718` (`active_recall`, log line only); `dpc_agent/context.py:735-812` (no setting read; `max_results` not passed); *repo:* `dpc-client/ui/src/lib/components/AgentPermissionsPanel.svelte:691-710` (checkbox) |
| Per-layer EXT gate, revocation not seen until restart | `dpc_agent/agent.py:436-446`; `dpc_agent/active_recall.py:68-98`, `:222-248`; `managers/agent_manager.py:723-725` (`sync_firewall_settings` is `pass`); `read_file` path check `dpc_agent/tools/registry.py:406-449` |
| EXT excerpt with the flag off, as policy | *repo:* `dpc-client/core/tests/test_recall_shared_layer_gate.py:57-63`; `dpc-client/core/tests/test_recall_address_survives_the_store.py:241-255` |
| Replay of sent tails without a gate | `dpc_agent/context.py:889-914`; `dpc_agent/agent.py:423-424`, `:473-482` |
| Retrieved fields and framing | heading `dpc_agent/indexing_pipeline.py:66-69`; render `dpc_agent/active_recall.py:147-171`, `:186-193`, excerpt `:666-671`; soft cap drops only recent sections `dpc_agent/context.py:1016-1057`; runtime framing note `:980-987` |
| Failed reads counted, rotation | `dpc_agent/tool_ledger.py:35-40` (`is_outcome`); `dpc_agent/active_recall.py:433-454` (`is_error` not consulted); `dpc_agent/utils.py:890-908` (5 MB, one `.1`); `dpc_agent/loop.py:1484-1505` (every tool writes `tools.jsonl`) |
| Offer store keyed by conversation, no agent; group paths | `dpc_agent/active_recall.py:540-569`; task id `dpc_agent/agent.py:385-386`, `:589`; room gate `service.py:6975-7035`; peer path without it `message_handlers/group_handler.py:541-560`; per-agent gate `managers/agent_manager.py:1060-1071` |
| Retrieval evaluation | *repo:* `eval/retrieval/run_retrieval_eval.py:75-90` (`_same_card`, basename fallback), `:115-138` (one query, text/vector/fuse); *repo:* `eval/retrieval/results/agent_001-2026-08-24.json` |
| L6 weight history | `git log -S '"L6": 1.5'`: added in 4e062760 (2026-04-18), removed in ed589b36 (2026-08-12) |
