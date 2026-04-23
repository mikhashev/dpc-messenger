# ADR-016: Agent Web Pipeline — Multi-Engine Search & Extraction

**Status:** Draft
**Date:** 2026-04-23
**Authors:** Ark (analysis + draft), CC (code audit + ddgs research), Mike (direction)
**Session:** S67
**Related:** [TOOL-1 backlog item](../../backlog.md), [dpc-full-picture §7.4](../../ideas/dpc-full-picture/dpc-full-picture-s32.md)
**Replaces:** browser.py search_web, browse_page, extract_links

---

## Context

### Current State

Agent web tools are implemented in `browser.py` (~458 lines):

| Tool | Implementation | Problem |
|---|---|---|
| `search_web` | DDG HTML scrape + regex | Single engine, breaks on layout changes, no snippets |
| `browse_page` | HTTP GET + HTMLParser | Flat text, loses structure (headings, tables, lists) |
| `extract_links` | HTTP GET + regex | Superset of browse_page functionality |
| `fetch_json` | HTTP GET + JSON parse | Works fine |
| `check_url` | HTTP GET + timing | Works fine |

**Critical pain point:** search_web returns only title + URL (no snippets). Agent must browse_page every result to assess relevance. This burns context window tokens on every web research task.

**Secondary pain point:** browse_page strips all HTML structure. Tables, headings, lists, code blocks — all become flat text. Agent cannot navigate content structure.

### Research (S42-S46, 12 OSS repos)

12 open-source agent repositories analyzed for web tool patterns. Key findings:
- Tavily API dominates (4/7 repos with search)
- trafilatura is the standard for HTML→markdown extraction
- SearXNG used by 0 repos (Docker-only, Windows incompatible)
- YaCy: P2P search, Java runtime, Windows git clone broken
- Pattern: thin Python libraries preferred over middleware services

### Requirements (Mike, S67)

- No Docker (native stack decision)
- Reliable across VPN/blockages (multi-engine fallback)
- Adaptable and independent (no single external dependency)
- Privacy-first (no mandatory cloud API keys)

---

## Decision

### Stage 1: Search — `ddgs` package (pip install ddgs)

**ddgs** (formerly duckduckgo-search, v9.14.1, MIT license) — multi-engine metasearch via HTML scraping with automatic fallback.

**Capabilities:**
- 8+ backends: bing, brave, ddg, google, mojeek, yandex, yahoo, wikipedia
- `backend="auto"` — automatic fallback across engines
- `text()` returns title + href + body (snippet) — eliminates token burn
- Regional targeting (Yandex for RU, Google for EN)
- Zero native dependencies, Windows compatible, pip install

**Trade-off acknowledged:** ddgs scrapes HTML under the hood (not proper API). Same fragility as current DDG, but multiplied resilience: 8 backends compensate for individual engine breakage. Active maintainer (v9.14.1 released 2026-04-20).

**Provider pattern:** `SearchProvider` ABC with `DDGProvider` implementation. Future: `BraveProvider`, `TavilyProvider` as opt-in backends via config. Analogous to LLM provider pattern in `providers/`.

### Stage 2: Extraction — `trafilatura` (pip install trafilatura)

**trafilatura** — HTML→structured markdown conversion. Preserves headings, lists, tables, links, code blocks.

**Why trafilatura over ddgs.extract:**
- Deep content extraction with structure preservation
- Battle-tested in 12/12 analyzed repos
- Size presets (s/m/l/f) inspired by Searcharvester approach
- ddgs.extract for quick reads, trafilatura for deep extraction — both available

### Stage 3: Browser (JS rendering) — DEFERRED

SPA/Cloudflare sites remain uncovered. Will be addressed separately when need is demonstrated. Not blocking for TOOL-1.

### What changes

| Current | Replacement | Status |
|---|---|---|
| `search_web` (DDG regex) | `ddgs.text()` multi-engine | Replace |
| `browse_page` (HTMLParser) | `trafilatura` → markdown | Replace |
| `extract_links` (regex) | Superset of Stage 2 output | Remove (merged into browse_page) |
| `fetch_json` | Unchanged | Keep |
| `check_url` | Unchanged | Keep |

### Response schema

Tavily-compatible response format across all search backends. De-facto standard for agent systems. Allows future drop-in backends without agent prompt changes.

---

## Scope

### Dependencies to add
- `ddgs` (MIT, ~zero native deps)
- `trafilatura` (Apache 2.0)

### Files to modify
- `dpc_agent/browser.py` — rewrite search + extract, keep fetch_json/check_url
- `dpc_agent/settings.py` — search provider config section
- `pyproject.toml` / `requirements.txt` — add dependencies

### Files to create
- Provider ABC for search (inside browser.py or separate module — CC decides)

### Documentation updates
- CLAUDE.md — updated file manifest
- backlog.md — TOOL-1 status update
- ROADMAP.md — TOOL-1 progress

---

## Alternatives Considered

### 1. SearXNG subprocess
Full metasearch engine, 100+ engines, privacy-first. Rejected: officially Docker-only, git clone broken on Windows (NTFS `:` in filenames), subprocess lifecycle management overhead, 0/12 repos use it, fighting upstream.

### 2. Tavily / Brave cloud API
Proven in agent systems (4/7 repos), ranking tuned for LLM, snippets from box. Rejected as default: privacy contradiction with DPC mission, cloud lock-in, API key requirement. Viable as future opt-in provider.

### 3. YaCy (P2P own index)
Philosophically aligned with DPC P2P vision. Rejected for TOOL-1: Java runtime (~500MB), Windows git clone broken, overkill for internet search. **Preserved for future Context B** (P2P Knowledge Discovery between DPC nodes, ROADMAP Phase 3+).

### 4. Searcharvester (full adopt)
Self-hosted Tavily replacement with SearXNG + trafilatura + deep research agents. Rejected: Docker-only, includes Hermes agent framework we don't need. Used as reference for schema and extraction patterns.

### 5. Searcharvester (partial adopt)
Take FastAPI adapter + trafilatura, drop Hermes. Rejected: still requires SearXNG (Docker), adding complexity for what ddgs provides natively.

### 6. Hybrid (SearXNG default + opt-in cloud)
Best architecture but inherits all SearXNG problems. Deferred: ddgs already provides multi-engine with auto-fallback. Hybrid pattern can emerge later if ddgs proves insufficient.

---

## Two Search Contexts (Important Distinction)

TOOL-1 addresses **Context A: Internet Search** — agent finding information in the public web.

**Context B: P2P Knowledge Discovery** — finding relevant knowledge across DPC nodes — is a different architectural layer (ROADMAP Phase 3+). YaCy, concept graphs, and distributed indexing belong there, not in TOOL-1.

| | Context A (TOOL-1) | Context B (Future) |
|---|---|---|
| Network | Public internet | DPC mesh |
| Content | Web pages | Knowledge DNA units |
| Trust | Zero | Graduated (Dunbar) |
| Privacy | Hide from services | E2E encrypted |
| Solution | ddgs + trafilatura | YaCy / concept graphs / ??? |
| Timing | Now | Phase 3+ |

---

## Consequences

### Positive
- **Token savings.** Snippets eliminate browse-every-result pattern.
- **Reliability.** 8 engines with auto-fallback vs single DDG regex.
- **Structure preservation.** Markdown output with headings, tables, lists.
- **Privacy.** No mandatory cloud API keys, no data sent to third parties by default.
- **Simplicity.** Two pip installs, zero Docker, zero subprocess management.
- **Extensibility.** Provider pattern allows adding backends without architecture changes.

### Negative
- **HTML scrape fragility.** ddgs scrapes like current DDG, just with more fallbacks. Individual engine breakages still happen — compensated but not eliminated.
- **No JS rendering.** SPA/Cloudflare sites still inaccessible until Stage 3 implemented.
- **ddgs single dependency.** If ddgs maintainer abandons project, we maintain a fork or find alternative. Mitigated: MIT license, can vendor if needed.
- **Rate limits.** DDG rate-limits after ~50 requests. Mitigated: auto-fallback to other backends.

---

## References

- browser.py audit (CC, S67)
- 12 OSS repos research (Ark, S42-S46)
- Searcharvester analysis (CC, S67)
- ddgs PyPI: https://pypi.org/project/ddgs/
- trafilatura: https://github.com/adbar/trafilatura
- dpc-full-picture §7.4 (search/retrieval inadequacy at scale)
- Tavily API schema: https://docs.tavily.com/
