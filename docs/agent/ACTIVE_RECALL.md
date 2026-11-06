# Active Recall — how it works, precisely enough to rebuild it

Active Recall is the part of a DPC agent's prompt assembly that, on every turn, searches the
agent's knowledge and puts up to three short pointers to relevant documents into the prompt,
without the model having to call a tool. The decision that introduced it is
[ADR-010, Component 2](../decisions/010-agent-memory-architecture.md#component-2-embeddings-active-recall--cross-layer-retrieval);
the decay step comes from [ADR-013 (S4)](../decisions/013-agent-selection-layer.md), the
whole-document index from [ADR-018](../decisions/018-retrieval-infrastructure-upgrade.md), the
graph channel from [ADR-024](../decisions/024-knowledge-graph-infrastructure.md).

This document describes the mechanism **as the code runs it on 2026-11-06**, not as the ADRs
planned it; where the two differ it says so (§13). Every constant is listed. The body needs no
access to the repository; file and line references are only in the appendix.

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
the agent's `read_file` calls, feeds the next turn's decay.

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
| **read** | A finished `read_file` call by the agent, logged by the tool loop. |

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
its sequence limit is set to `min(max_tokens, the model's current limit)` (bge-m3 ships 8192),
and never raised; a `max_tokens` of 0 or less leaves the model's own limit. `max_tokens` is 4096
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
under the other's; and because the limit is never raised past the value already applied, a
larger `max_tokens` stated after the model has loaded is recorded but has no effect on the model.

### 4.3 When it runs

* **Startup sync** (agent start, or lazily before the first prompt): collect all three layers,
  compute `doc_hash` for each, embed only documents whose hash changed or is new, delete rows
  for keys that disappeared, write `file_hashes` last as the commit point. A full rebuild is
  forced when the embedding model, the key-format marker or the backend pair changed, or when
  the stored map lists documents while the stored index failed to load or holds no rows at all.
  A partial mismatch (the map lists more documents than the index holds, but the index is not
  empty) does not force a rebuild. Embedding runs in batches of
  16 (configurable).
* **Live replace**: when the agent writes a file under `knowledge/`, that one key is removed and
  re-added in both channels; when a knowledge commit is approved, the L6 file is replaced the
  same way.
* EXT changes are picked up only by the next startup sync.

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
non-empty. Any exception skips recall for that turn: the user sees an ordinary turn with no
recall block, and the log records it — at INFO when the embedding model is not cached locally,
at WARNING with the traceback for anything else.

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
joined by spaces and passed through `bm25s.tokenize` (lowercase, regex `\b\w\w+\b`, English
stop words). The corpus side keeps punctuation glued to words (`recall,`), the query side does
not (`recall`); a query word therefore matches only corpus occurrences that stood between
whitespace. Reproduce this if you want identical rankings; fix it if you want better ones.

### 6.4 Graph channel

`G = graph_expand(seeds = distinct keys of V and T)`, §5. Skipped when there are no seeds or no
graph.

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
  that is a finished call (successful or failed; "attempt" rows are excluded). The `args.path`
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

After decay, drop candidates that have **nothing to offer**: a candidate stays if it has an
address (§10); otherwise it stays only if it has a preview `text` and is not an L6 document
with the shared-knowledge gate closed. Then take the **first 3**.

## 10. Rendering and placement

**Address**: an L5 key is its own address. Any other document's address is its `source_path`,
but only while the corresponding gate is open now (shared-knowledge access for L6, extended-path
read access for EXT and for any non-L5 graph hit); otherwise there is no address.

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

## 11. Logging and the feedback loop

Whenever at least one candidate is injected (modes `full` and `hints`), one line is appended
to `state/knowledge_access.jsonl`:

```json
{"ts": "<UTC ISO>", "task_id": "<turn id>", "mode": "full",
 "files": ["<key1>", "<key2>", "<key3>"],
 "addresses": ["<printed address or null>", "..."], "useful": null}
```

`addresses` is empty in `hints` mode. The printed addresses are also kept in memory under the
turn's task id (in DPC the conversation id, so each turn replaces the previous one; up to 32
ids are kept); when the agent then calls `read_file` under that id, the tool log row gets
`"via_hint": true|false` (omitted if the turn was offered nothing). `via_hint` is recorded for measurement; decay does
not use it.

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
not a follow-rate.

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

1. **Content-free messages produce noise.** A message like the one in §12 has no topic; Q1
   matches the sender's and agent's names and Q2 matches whatever the last ten messages were
   about. Nothing detects that the query carries no information.
2. **Decay reinforces itself.** An injection earns credit, and credit raises the multiplier,
   so a document shown often keeps being shown while nobody opens it; with zero reads in the
   set (§12) the ranking is decided by past injections alone. The cap (0.3) and saturation (20)
   are documented placeholders.
3. **Whole-document vectors on large files.** One vector per document, built from the first
   4096 tokens; a large catalog or backlog is represented by its opening and competes with short
   notes on equal terms. BM25 sees the whole text, which favours long documents that mention
   many words.
4. **Graph hits carry no excerpt** (`(chunk 0)`), and at one hop only L5 name links can
   produce them (§5); the NER and LLM edge classes do not reach the channel.
5. **Everything `.md` under an indexed root is indexed**, gitignored included, so generated
   dumps and working notes compete with documentation.
6. **ADR-020 query preprocessing is only partly live.** Its decision is a pipeline applied
   upstream of all channels (language detection, standard stop words, corpus stop words,
   user stop words). In the live path only the BM25 channel filters stop words (fixed ru+en
   plus corpus stops); the vector channel embeds Q1 and Q2 raw; there is no language detection
   and no user layer.
7. **Q2 truncation keeps the oldest text.** The embedding keeps the first 4096 tokens of its
   input and drops everything after them. Q2 joins the ten messages oldest first, so on a long
   history the tokens that survive are those of the oldest messages, and the newest messages —
   the ones closest to the current turn — are the ones cut.
8. **BM25 token mismatch** between corpus and query (§6.3).
9. **Divergences from the ADRs:** BGE-M3 sparse output (ADR-018) is not used — the text channel
   is BM25 only; ADR-018 planned the whole history as the context query, the code uses the last
   10 messages; grace is 7 days, not 5 sessions (ADR-013); ADR-010 fixes no weights, only an
   order, L6 > L1 > L5 > L2, and the weight table no longer follows it (L1 1.3 is above L6 1.0
   since the code's own 1.5 for L6 was removed on 2026-08-12); L1 and L2 have weights but no
   producer. The per-agent memory config fields `active_recall`
   and `max_recall_results` are read but not consulted by the live path: recall runs whenever an
   index exists, and the slot count is fixed at 3.

## 14. Reproduce it

Checklist:

- [ ] Key scheme of §3.2 and one row per document with the metadata of §3.1.
- [ ] Front-matter strip, heading extraction (`^#+ (.+)$`, first match), `doc_text` of §4.2,
      100,000-char cap, 500-char preview.
- [ ] A multilingual embedding model with normalised output (bge-m3, 1024-d, 4096-token cut) and
      an exact cosine index.
- [ ] BM25 (k1 1.5, b 0.75) with the tokeniser and stop words of §6.3.
- [ ] Optional graph with `DEPENDS_ON` edges between knowledge files that link each other.
- [ ] Per turn: Q1, Q2, three channels, RRF with the weight table, decay, mode, filter, top 3,
      render, append to the current user message, log.
- [ ] Two logs: injections (written by recall) and reads (written by your file-read tool).

Minimal pseudocode:

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

## Appendix — where it lives in DPC

Paths are relative to `dpc-client/core/dpc_client_core/`; line numbers as of 2026-11-06.

| What | Where |
|---|---|
| Per-turn orchestration (gate, Q1/Q2, three channels, fusion call, placement) | `dpc_agent/context.py:726-829`, tail placement `:852-853`, `:990` |
| Channel sizes, hop limit, task text | `dpc_agent/context.py:38-40`, `task_query` `:57` |
| Context usage ratio | `dpc_agent/context.py:143`; figures from `managers/agent_manager.py:1896-1901` |
| Thresholds, decay constants | `dpc_agent/active_recall.py:27-59` |
| Address, render, hints mode, mode choice | `dpc_agent/active_recall.py:68`, `:130`, `:186`, `:174` |
| Filter, selection, logging | `dpc_agent/active_recall.py:222`, `:251`, `:572` |
| Access counts, grace, multiplier, decay | `dpc_agent/active_recall.py:287-349`, `:398`, `:472`, `:490`, `:504` |
| `via_hint`, log compaction | `dpc_agent/active_recall.py:557`, `:604`; `dpc_agent/loop.py:1500-1504` |
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
| `max_tokens` defaults, shared provider | `dpc_agent/memory_config.py:30`; `dpc_agent/memory.py:556` (constructor default), `:407-440` (per-process singleton, later `max_tokens` applied), `:696` (`min(limit, current)`); callers `dpc_agent/agent.py:210-218` (no `max_tokens`), `managers/agent_manager.py:387-399` (`mem_cfg.max_tokens`, re-applied to an existing provider), `dpc_agent/context.py:757` (per-turn fallback, no `max_tokens`) |
| Rebuild when the map outlives the index | `dpc_agent/indexing_pipeline.py:466`; caller `managers/agent_manager.py:516-518` |
| Recall failure logging, usage-ratio fallback | `dpc_agent/context.py:824-829`; `:143-154` |
| Prompt-size estimate log line | `dpc_agent/agent.py:491-515` |
| L6 weight history | `git log -S '"L6": 1.5'`: added in 4e062760 (2026-04-18), removed in ed589b36 (2026-08-12) |
