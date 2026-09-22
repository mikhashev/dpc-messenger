---
adr: 043
title: "Book conversion shares one reader with read_document; the module moves into dpc-messenger once the user flow is known"
status: proposed
date: 2026-09-22
axis: knowledge, reach
deciders: [Mike]
consulted: [Ark, Johnny, Zcode, CC]
informed: []
depends_on: []
related: [ADR-009, ADR-010, ADR-018, ADR-019, ADR-024, ADR-042]
supersedes: []
session: "DPC Project, 2026-09-22 — Mike's goal: dpc-library is the whole D:\\Books library and a technology rehearsal; the tools are eventually to ship in dpc-messenger so any user can convert their own library locally"
---

# ADR-043 — One reader now, the module in dpc-messenger later

> **Proposed, not decided.** Recorded on Mike's instruction, 2026-09-22, carrying the
> four-way consensus of Ark, Johnny, Zcode and CC, and revised the same day after Mike
> set two further requirements (R1, R2). The direction is unchanged; the next step is not.

## Context and Problem Statement

**Why books at all.** Mike recorded the purpose on 2026-08-16 and it is three purposes:
search, a readable archive for agents *and* humans, and a corpus for tasks — three
consumers of **one** record. The agent needs a locator, the text and the right to believe
it (`verdict`, `method`); the human needs structure and a render on demand; a task needs
the text as data with document and page boundaries intact. What is being built is
therefore not a format converter but the intake of a **knowledge base** in VISION's sense:
"Knowledge that accumulates… attributed, versioned, searchable", held as a Knowledge
Commons "governed by attribution and transparent curation, not algorithmic
recommendation". C7 asks that every piece of knowledge carry who made it, when and why,
and the page record materialises C7 for a book — `method` says who produced the text, the
run when and with what, the refusal reason why a page is not `done`; a conversation yields
a knowledge commit and a book yields a page record, one anatomy. C7's other half, "You
curate. The agent assists.", divides the labour and settles the apparent clash between
manual curation and half a million pages: the pipeline **verdicts**, it does not curate;
curation stays human and happens at the moment of citation. (Ark's review, 2026-09-22.)

The sibling research repository `dpc-research/dpc-library` has measured a pilot
corpus page by page and stopped where content would be written. The two facts that
matter here, from its 2026-09-22 measurement report: roughly four pages in ten need
the visual path, and no book is readable for free in its entirety — a cheap first pass
leaves a tail in every one. Which books, which classes and which counts are the
report's business, not this record's.

The reading code already lives here. `read_document`
(`dpc-client/core/dpc_client_core/dpc_agent/tools/document.py`, 1 221 lines) opens PDF
through pypdfium2 and DjVu through DjVuLibre, counts image objects per page, borrows
dpc-library's own 300-character and three-image thresholds by name, routes a page with no
text layer to a vision provider under a hard page cap, and reports `thin_layer_pages` "so
a pipeline routes on it without reading prose".

What is missing is not a reader but everything around one call of it: a folder scan, a
route per page, a record per page, a resumable run, a summary. Writing *a second reader*
for that would put two instruments on the same page — and dpc-library has already paid
for two instruments once, when two definitions of "image" gave two different vision
shares for the same pages. Mike's goal makes location a product question too: any user should
eventually convert their own library locally, so the code has to ship in the client. But
**how it runs, on what, how it integrates and what the user does with it are unknown
today** — which is why the move is not the next step (R2).

## Where this sits in the whole flow

A conversion pipeline is four links of eight. The contour, from the shelf to the prompt
and back into shared knowledge (Ark, `dpc-library/docs/knowledge-flow.md`, 2026-09-22):

| # | Layer | What happens | Where it lives | Exists? |
|---|---|---|---|---|
| 0 | source / catalog | the source is never modified; a *catalog* holds a status per file, a *corpus* is what has been measured | dpc-library 0006 | no catalog |
| 1 | class | one class per unit, two axes plus `raster` | dpc-library 0008, the re-measurement | yes |
| 2 | route | cheap path or model path, by class | dpc-library 0002, 0010 §3 | yes, as a rule |
| 3 | read | one reader, two callers | **this ADR** | in progress |
| 4 | record | one line per unit: `verdict` / `method` / `label` / `tree` / `cost_s` | dpc-library 0010, `page-record.md` | contract yes, writer no |
| 5 | index | unit → FAISS + BM25 + graph → RRF | ADR-010, ADR-018/019, ADR-024 | infrastructure yes, unit no |
| 6 | retrieval | hints into the agent's prompt; navigation and render on demand for the human | ADR-010 (Active Recall), dpc-library 0013 | infrastructure yes |
| 7 | citation | the answer carries `work_id` + locator + `method` + `verdict` | — | no |
| 8 | return | what was read becomes a knowledge commit whose `claim` points back at the record | ADR-009, VISION | yes, not connected to 0–4 |

**This ADR covers layers 1–4, and layer 3 in particular.** Layers 5–8 are described in
`dpc-library/docs/knowledge-flow.md`, which is a contour and not a decision: it has no
measurement behind it and settles nothing. They will need an ADR of their own once the
unit of indexing is settled, and the sentence that joins the halves is Johnny's: **the
record is the input of the index (layer 5), not only the output of the pipeline** — the
indexing unit is the record unit, so `verdict` decides what is indexed at all (only
`done`), `method` gives the weight of trust, `label` gives the locator a citation carries,
`tree` gives navigation. ADR-010 today indexes whole files with model-sized chunking; a
book of several hundred pages is one file, which yields no locator and no right to believe. That mismatch,
not a missing button, is what Q0 is about.

## Decision Drivers

- **D1 — one reader.** Every refusal rate the research repository reports must be a
  number about the corpus, not about which of two readers ran.
- **D2 — the agent is not the only caller.** A run over thousands of pages cannot go through an
  agent loop: tool results truncate at 15 000 characters and `read_document` caps a call
  at 20 pages on purpose. The pipeline needs the functions, not the tool.
- **D3 — no client, no GUI, no service** in the import path, and **D4 — resumable**: a
  run is started from a shell, runs for hours, and must not restart at page one or
  silently reuse a record made by an older reader.
- **D5 — the record comes first.** `docs/page-record.md` in dpc-library (Ark,
  2026-09-22) is the contract: one line per unit, verdict computed from class + method +
  evidence, denominator from a second instrument. A facsimile page read by the cheap path
  yields its running head and nothing else, while the eye reads the whole document —
  which is why the verdict is computed, not claimed by the stage that did the work.

## Requirements

Set by Mike on 2026-09-22 (Mike's call, 2026-09-22), as conditions this ADR and its
successors can be checked against.

### R1 — cross-platform and multilingual

**(a) Windows, Linux, macOS.** Binary discovery already suits all three: `_djvulibre()`
in `document.py` takes `$DPC_DJVULIBRE_DIR`, then PATH via `shutil.which`, then the two
Windows install directories, and its failure message names `winget`, `apt` and `brew`.
The **known Windows defect must be fixed**: DjVuLibre 3.5.29 there opens no path with a
non-ASCII component — tested live 2026-09-22, a Cyrillic directory or filename gives a
`djvused` crash or `Failed to open: Invalid argument`, an ASCII copy in an ASCII directory
reads. Fix: stage an ASCII-path hard link (copy as fallback) before the binary runs,
**on Windows only** — Linux and macOS open such paths today, and neither their behaviour
nor their measurements may change.

**(b) macOS has zero observations.** No DjVuLibre call, no pypdfium2 page, no path case
has been run there; every macOS statement here is read from code and vendor docs. The
first macOS number is a measurement to take, not a box to tick.

**(c) Russian and English at minimum, the major languages ideally.** The corpus is
Russian and tonight's measurement is Russian-only, so nothing is shown for a second
language yet. So the engine measurement designed in dpc-library 0012 for decision 0007
**must carry a `language coverage` column**, and a cheap second-language check is
available at once, because the pilot corpus already holds English sources in both
EPUB and PDF (listed in the dpc-library catalog).
Surya/Marker and Tesseract language coverage is to be **read from their own
documentation and then measured**, never assumed.

**(d) File names and paths in any language.** The ASCII staging of (a) is a general rule
about paths the toolchain cannot open, not a Cyrillic special case; a Greek, Chinese or
accented-Latin path is the same defect, and the fixtures carry more than one script.

### R2 — dpc-library is where the ideas are tested first

`dpc-research/dpc-library` is the place to try this. Full integration of the solution and
its code into dpc-messenger comes later: how it runs, on what, how it integrates and what
the user flow is are all open, and building it into the client now would settle those
questions by accident.

## Decision

**Direction (unchanged):** one reader for the project, shared by the `read_document`
agent tool and by the conversion pipeline, whose eventual home is a headless, importable
module of `dpc-client/core/dpc_client_core/`, so the code a user runs is the code that
was measured.

**Next step (new):** the move is **deferred**. The pilot runs in dpc-library, whose
scripts **import the reader from dpc-messenger** rather than reimplementing it — Option
D. No second reader is created, and no interface is designed before the user flow is
known.

### The pilot, concretely

Cheap-pass and measurement scripts live in `dpc-research/dpc-library/tools/`, import the
dpc-messenger reader, and run from the client environment:

```bash
cd C:/Users/mikha/Documents/dpc-messenger/dpc-client/core
uv run python C:/Users/mikha/Documents/dpc-research/dpc-library/tools/<script>.py ...
```

Not a new arrangement: `tools/textlayer_remeasure.py` is invoked exactly this way today,
the command on record at the head of
`docs/measurements/textlayer_command_and_decision_2026-09-22.md`; dpc-library carries no
`pyproject.toml` and no `.venv`, so the client environment is already its runtime. The
pilot exercises the **contract and the routing**, not an interface — it is a rehearsal of
0010 on real data, not a library for use (dpc-library 0011, Ark's wording). Flow: **scan →
classify → route → extract through the shared reader → write `records.jsonl` per source →
summary.** A unit is skipped when a record for it exists with the same `source_sha256`
**and** the same reader version; a version bump re-reads, a changed source re-reads, an
interrupted night continues (D4).

### The eventual home

For the integration phase: `dpc-client/core/dpc_client_core/document_pipeline/` with
`reader.py`, `scan.py`, `classify.py`, `route.py`, `record.py`, `summary.py`,
`__main__.py`, plus one `ToolEntry` with `default_enabled=False` — the posture
`read_document` and `web_fetch` take, since converting a folder is a long, GPU-spending,
disk-writing act. **The package name is proposed, not settled, and is Mike's** —
`document_pipeline` for the module, *knowledge base* for the entity it fills, the
vocabulary argument in Q1 — and it is owed at the integration phase, not before.

### Rationale

The reader is here and is the only one, so D1 is met by importing it, today, at no cost.
The refactor that splits the reading core out of the tool is worth doing, but the pilot
is not blocked on it: D2 and D3 say the pipeline must not inherit the tool's caps, and a
script importing the reader's functions already avoids them. Deferring the move keeps the
expensive, hard-to-reverse choices — package name, entry point, record location, user
flow — for the moment there is evidence to make them with (R2). **Not in scope:**
indexing (layer 5 above; dpc-library Stage 7), Markdown rendering (layer 6; Stage 8), the
engine choice (dpc-library 0007, open — measurement designed in that repository's 0012).
The pilot writes records; what reads them is a later decision, owed under Q0.

## Considered Options

**A — a module in dpc-messenger** (the eventual target, not the next step) · **B — an
independent implementation in dpc-library** (rejected) · **C — a separate package or
repository** (rejected for now) · **D — pilot scripts in dpc-library that import the
dpc-messenger reader** (proposed for the pilot phase; Ark's proposal, 2026-09-22).

### Pros and Cons of the Options

**A.** Good: one reader, one set of thresholds, one DjVu defect to fix, and it ships — a
user gets the code that was measured. Neutral: a refactor of a 1 221-line tool module.
Bad **today**: it fixes entry point, package name and record location before the user flow
is known, which is what R2 calls unknown. Still where the direction leads.

**B.** Good: nothing here changes. Bad: **a second reader**, rejected on that alone — two
readers drifting by one threshold turn every refusal rate into a statement about the
instrument; and nothing ships to a user.

**C.** Good: a clean boundary if a second consumer appears. Bad today: the reader is
already here, so C is the cost of A plus a packaging boundary for no current gain.

**D.** Good: one reader (D1) with no refactor first, starting today on the runtime
`textlayer_remeasure.py` already uses, and committing nothing about the user flow (R2).
Neutral: the scripts are written to be moved into the module later — pure functions, no
hard-coded absolute paths. Bad: the import crosses two repositories with no CI over the
seam, so a reader change here can break a measurement there unnoticed; and until the
refactor the scripts import from a module shaped for a tool.

## Consequences

- **Positive:** `read_document` and the pilot cannot disagree about what a page gave up;
  the DjVu non-ASCII path defect (R1a) is fixed once, for both callers; dpc-library's
  measurements become runs of shipped code; and package name, entry point and where a
  user's records land are decided after the pilot, with the pilot as evidence.
- **Negative:** the cross-repository import has no CI over it, so a parity test belongs
  in the client suite before the corpus run; and `document.py` is still touched in the
  pilot phase, for R1a, before any new capability appears.
- **Neutral:** the research repository keeps the corpus, measurements, contracts and
  reports; code there stays measurement and pilot scripts.

## Confirmation

- [ ] A pilot script in `dpc-library/tools/` runs the cheap pass over the corpus under
      `uv run python` from `dpc-client/core`, with no CoreService, WebSocket or agent
      loop in the import path.
- [ ] Tool and pilot, given the same page, report the same class, character count and
      image count.
- [ ] A run interrupted mid-corpus and restarted re-reads no unit whose record carries
      the same source sha and reader version, and re-reads every unit whose does not.
- [ ] The summary prints refusal rate beside unit count and wall time, against a
      denominator the extractor did not produce.
- [ ] A DjVu under a Cyrillic path reads on Windows, the same fix leaves the Linux path
      unchanged (R1a, R1d), and macOS either has a first run recorded or this ADR still
      says it has none (R1b).
- [ ] The 0007 engine measurement carries a `language coverage` column, and the nine
      English sources have been run (R1c).

## Open Questions

- **Q0 — layers 5–8 are unwritten as decisions**, and that, not a missing button, is what
  makes the user flow unknown and **gates the integration phase**. The open thing is where
  a record lands in the index, what a citation carries and how an extract returns as
  knowledge — who converts what, when and from where follows from those. The contour
  exists (`dpc-library/docs/knowledge-flow.md`); no measurement stands behind it, so it is
  not yet a decision. Until it is one, Option A is not started, and Q0 will be reopened
  every session it is left as "the flow is unknown". — @Mike
- **Q1 — the names, two of them.** The **entity** is a *knowledge base*, not a library
  (Mike's call, 2026-09-22): "library" is taken twice over — the cryptography sense in
  this source tree, and the user's own book collection on disk — and VISION already says
  "shared knowledge bases". The **module** is proposed as `document_pipeline`, built from
  words the project owns: `pipeline` names a staged transform in two ADR-backed modules
  (`dpc_agent/indexing_pipeline.py`, ADR-010/018/024; `dpc_agent/sleep_pipeline.py`,
  ADR-014), and `document` is what `read_document` calls its input. `library` as a package
  name is what this replaces. A `docs/GLOSSARY.md` row is owed for *knowledge base*, which
  the file does not carry today; both names are still Mike's call. — @Mike
- **Q2 — where records land for a user**, as opposed to for a measurement. `~/.dpc/`
  holds per-peer conversation files today; a knowledge base built from a user's book
  collection is larger and is not conversation. — @Mike
- **Q3 — engine.** dpc-library 0007 is open and its measurement designed but not run, so
  the router ships with one route to the configured vision provider and a seam where an
  engine choice goes. That measurement now owes a language-coverage column (R1c). — @Ark

## Effort

**Every number below is an estimate, not a measurement** — nothing here has been built or
timed, and they are for sequencing, not for a schedule. The integration phase is gated on
Q0. Corpus sizes are the dpc-library catalog's business and are not restated here.

| Phase | Item | Engineer-hours (estimate) |
|---|---|---:|
| Pilot | Cheap-pass + measurement scripts in `dpc-library/tools/`, importing the reader | 4–6 |
| Pilot | ASCII-path staging fix in the reader, Windows-only, no-op elsewhere (R1a) | 3–5 |
| Pilot | Record writer + computed verdict + resume by sha and reader version | 8–12 |
| Pilot | Language-coverage column for the 0007 measurement + English check (R1c) | 4–6 |
| Pilot | Parity test: tool and pilot agree on class, chars and images for one fixture | 3–4 |
| Pilot | Run over the 52-source corpus, with the write-up | 6–10 |
| | **Pilot total** | **28–43** |
| Integration | Shared reader refactor out of `document.py`, tool behaviour unchanged | 10–16 |
| Integration | Pilot scripts become module stages (scan/classify/route/record/summary) | 6–10 |
| Integration | Headless CLI (`__main__.py`, arguments, summary output) | 3–5 |
| Integration | Tests (fixtures, parity, resume, non-ASCII paths on three platforms) | 10–14 |
| Integration | `ToolEntry` with `default_enabled=False` and firewall seeding (S148) | 2–3 |
| | **Integration total** | **31–48** |

## Authors

- **Mike** — Decision (pending), the two requirements of 2026-09-22, and the naming call
  (knowledge base, not library); **Ark** — the page-record contract, the corpus analysis,
  the Option D proposal, the eight-layer contour; **Johnny** — the record is the input of
  the index; **Ark, Johnny, Zcode, CC** — the 2026-09-22 consensus this record states;
  **CC** — draft and revisions.

## References

- `dpc-client/core/dpc_client_core/dpc_agent/tools/document.py` — `read_document`, the
  reader to be shared; `_djvulibre()` for the three-platform binary discovery
- `dpc_agent/tools/registry.py` — `ToolEntry`, `default_enabled` (S148);
  `dpc_agent/indexing_pipeline.py`, `dpc_agent/sleep_pipeline.py` — the existing sense of
  `pipeline` here
- `VISION.md` — C4, C6, C7; "Knowledge Commons"; "Knowledge that accumulates"; "You
  curate. The agent assists." — the why in Context and the acceptance of layers 7–8
- dpc-library: `docs/knowledge-flow.md` (the eight-layer contour, Ark, 2026-09-22 —
  a contour, not a decision), `docs/page-record.md` (the unit contract),
  `tools/textlayer_remeasure.py` (the run arrangement Option D reuses),
  `docs/measurements/textlayer_command_and_decision_2026-09-22.md` (every number quoted
  above, and the command at its head), `docs/decisions/0003`, `0007`, `0008`, `0010`–`0013`
