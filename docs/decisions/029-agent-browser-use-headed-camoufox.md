---
adr: 029
title: "Replace T10 popup orchestration with headed Camoufox for agent browser interaction"
status: accepted
date: 2026-05-24
deciders: [Mike]
consulted: [Ark, CC]
informed: []
depends_on: [ADR-028]
related: []
supersedes: []
session: S146-S147
---

# ADR-029: Replace T10 popup orchestration with headed Camoufox for agent browser interaction

## Context and Problem Statement

ADR-028 implemented web authentication via Tauri WebView2 popup windows. Phase 1 (cookie capture + headless single-shot read) works. Phase 2 (`keep_open=true` multi-page agent interaction via `popup_scroll`, `popup_extract_now`, `popup_navigate`, `popup_close`) proved fragile across S142-S146 testing sessions:

- Dict mutation race in Q7-concurrency check (`browser.py:626`, fixed S146)
- UTF-8 byte-slice panic in Rust debug log (`web_auth.rs:649`, fixed S146)
- Extraction-after-scroll consistently times out (open)
- Cookie loss after Tauri rebuild + restart (open)
- WebSocket frame corruption with `p_scroll<HEX>...` non-JSON fragments during scroll flow (open)
- Windows-only (WebView2 dependency)
- UI staleness — Re-login status badge does not reactively update (open, separate from T10)

Meanwhile, Camoufox (anti-detect Playwright fork) is already integrated as `AuthBrowser` ([`browser.py:353-466`](../../dpc-client/core/dpc_client_core/dpc_agent/tools/browser.py#L353)) for headless authenticated single-shot reads. Playwright provides stable, cross-platform APIs for scroll, click, type, screenshot, navigation, and multi-tab handling.

Mike's directive (S146): *"агент мог пользоваться браузером полноценно как человек, а человек мог это видеть"* — full browser-use with human observability.

## Decision Drivers

- **Observability** — Human must see agent browser actions in real time (headed mode)
- **Reliability** — Replace fragile 3-layer Tauri popup orchestration with battle-tested Playwright API
- **Cross-platform** — Camoufox/Playwright works on Windows, Linux, macOS; current T10 is Windows-only (WebView2)
- **Cookie persistence** — Playwright browser contexts can persist state across restart via `storage_state`
- **Specialization** — Tauri popup specializes in login (human interaction), Camoufox specializes in navigation (agent interaction); one tool per purpose
- **Maintenance cost** — Each new T10 interaction = new Rust handler + Python tool + Tauri event routing (O(N)); Playwright = one method call per interaction (O(1))

## Considered Options

- **Option A — Extension only:** add `scroll()`, `screenshot()`, headed toggle to `AuthBrowser`; keep Tauri popup for `keep_open=true` flows. Two parallel pipelines coexist.
- **Option B — Redirect post-login interaction to headed Camoufox:** Tauri popup for login only; all post-login navigation/scroll/extract through Camoufox in headed mode.
- **Option C — AI-driven `browser-use` library (Playwright + LLM):** high-level abstraction where a second LLM decides what to click/type from page content.

### Pros and Cons of the Options

#### Option A — Extension only

- Good: Minimal change, incremental, low risk
- Bad: Two pipelines coexist — agent must choose which → confusing
- Bad: Existing T10 bugs (dict mutation cleanup, p_scroll WS framing) remain in the retained popup pipeline
- Bad: Does not address structural problem (3-layer WebView2 FFI complexity)

#### Option B — Redirect post-login to headed Camoufox

- Good: One post-login pipeline; cross-platform; cookie persistence via Playwright; full human observability via headed Firefox window
- Good: 4 open T10 bugs become irrelevant (popup pipeline deprecated)
- Good: Cookie handoff (Tauri vault → Camoufox context) already implemented in ADR-028 Phase 1 — reuse, not new mechanism
- Bad: Separate Firefox process (~100-200 MB RAM)
- Bad: T10 popup code becomes dead code, needs migration plan (handled via Task 0)
- Bad: Headed mode requires display (server use needs Xvfb)
- Neutral: Larger agent capability surface — sandboxing via Task 3 (domain restriction) compensates

#### Option C — AI-driven `browser-use` library

- Good: Highest-level abstraction; second AI "sees" page and decides actions
- Bad: Every action = additional LLM call = tokens + latency; overkill for simple tasks
- Bad: Two LLMs in loop (DPC agent + browser-use decision LLM) — debugging, prompt-engineering surface doubles
- Bad: Privacy concern — page content sent to additional LLM for decision

## Decision

**Option B** — headed Camoufox as the sole post-login agent browser pipeline. Tauri WebView2 popup retained for login only.

### Rationale

Separation by specialization. Tauri popup is good at human-driven login (cookie capture from password entry through WebView2's process-wide cookie jar). Camoufox is good at programmatic navigation (Playwright API). Trying to make one tool do both led to the T10 bug streak (4 open bugs across S142-S146). Replacing post-login flow with Camoufox unlocks cross-platform support, cookie persistence, and human observability simultaneously, with no new mechanism — the Tauri→Camoufox cookie handoff already exists in ADR-028 Phase 1.

Option A keeps the broken popup pipeline alive; Option C adds a second LLM in the loop with no clear win over Playwright primitives for the v1 scope (READ-only structured-HTML sites). B is the lowest-friction path that resolves all named drivers.

### Architecture

```
┌──────────────────────────────────────────────────────────┐
│                    DPC Agent (Python)                     │
│                                                          │
│  Agent wants to interact with a website:                 │
│  1. Check if storage_state exists for domain             │
│     ├─ Yes → load storage_state, skip login popup        │
│     └─ No  → Tauri popup for human login (ADR-028)       │
│  2. Launch headed Camoufox with auth context              │
│  3. Agent calls browser_* tools (navigate, click, etc.)  │
│  4. Human observes in visible Firefox window              │
│  5. On completion → save storage_state + sync to Vault   │
│  6. Close browser                                        │
│                                                          │
│  Interrupt: human presses Стоп button → agent checks     │
│  inbox between browser steps and halts the flow           │
└──────────────────────────────────────────────────────────┘
```

### Key Properties

1. **Login popup retained** — Tauri WebView2 popup for initial cookie capture only (ADR-028 Phase 1 path). User types credentials, agent gets cookies. No change to existing flow.
2. **Headed Camoufox for agent actions** — All post-login browsing, extraction, and interaction happens in a visible Firefox window via Playwright APIs. Human can observe everything the agent does.
3. **Domain restriction** — Navigation is gated to authorized domains only (`privacy_rules.json` whitelist). Fail-closed: if no whitelist, agent cannot browse.
4. **Storage state persistence** — Playwright `context.storage_state()` saved to disk as fast-restore cache. Vault stores encrypted backup. No auto-expiry (Q3 decision).
5. **Audit trail** — All browser actions logged with action type, URL, selector, timestamp. Privacy: `fill` logs `text_length`, not content; `screenshot` logs `byte_size`, not pixels.
6. **Interrupt mechanism** — Stop button in chat panel (appears only when browser session live). Halts current tool call + prevents next steps; Firefox window stays alive for user inspection (Q1 decision, S147).
7. **Ref-based interaction (S154)** — agent operates over an accessibility-tree snapshot (`page.accessibility.snapshot()`) annotated with ref IDs (`@e1`, `@e2`, …). Tools like `browser_click` / `browser_fill` accept `ref` and the backend resolves it via Playwright's accessibility-aware locators (`get_by_role`). CSS selector remains as a fallback. Replaces raw HTML extraction as the primary interaction surface; HTML/markdown extraction stays available for read-only summarisation.
8. **Auto-snapshot after navigate (S154)** — `browser_navigate` returns the post-navigation a11y snapshot inline, eliminating the round-trip `navigate` → separate `browser_snapshot` call. Reduces tool-call count per page-transition by one.
9. **Snapshot summarisation (S154)** — when a raw snapshot exceeds a configurable size threshold, route the snapshot through the LLM Manager for task-aware summarisation (same infrastructure already used by the sleep-consolidation pipeline). Phase 1 may start with a heuristic viewport-based truncation; Phase 2 swaps in the LLM-based filter when needed.

### Retained from T10

- Tauri WebView2 popup for login (user-facing cookie capture)
- `browse_page(keep_open=false)` headless single-shot extraction (unchanged)
- Cookie sync to Vault (existing `encrypt_and_store` path)

### Removed from T10

- `popup_scroll`, `popup_extract_now`, `popup_navigate`, `popup_close` tools
- `keep_open=true` popup lifecycle management
- Rust-side popup session tracking (`_pending_popup_requests`, popup event listeners)
- Frontend `keep_open` mode paths

### Task Breakdown

Decomposition under [`tasks/adr-029-headed-camoufox/`](../../tasks/adr-029-headed-camoufox/) (7 task files + overview, gitignored). Execution order locked S147 (Mike: order B / safety-first).

| Execution Order | Task | Description | Depends on |
|-----------------|------|-------------|------------|
| 0 | Remove popup code | Delete `popup_*` tools, Rust handlers, frontend `keep_open=true` paths | — |
| 1 | Extend AuthBrowser | Add headed mode, navigation, click, type, scroll, screenshot, extract, close methods + `browser.headed` config | Task 0 |
| 2 | Domain restriction | Playwright `context.route("**/*")` with eTLD+1 gate, fail-closed empty whitelist | Task 1 |
| 3 | Storage state | Load/save `storage_state`, vault sync, no auto-expiry | Task 1 |
| 4 | Audit trail | Structured log of browser actions, privacy-preserving field filtering | Task 1 |
| 5 | Tool registry | Register 9 `browser_*` tools + firewall defaults; **accessibility-tree snapshot + ref-based interaction + auto-snapshot after navigate + LLM-Manager-routed summarization** for snapshots beyond a size threshold (S154 decision matrix) | Task 1, 2, 3 |
| 6 | Interrupt mechanism | Stop button + agent inbox check between browser steps | Task 1 |

## Consequences

### Positive
- **Stability**: Playwright APIs replace fragile Tauri popup orchestration
- **Cross-platform**: Camoufox works on Linux/macOS (no WebView2 dependency for agent actions)
- **Observability**: Human sees exactly what agent is doing in real-time
- **Anti-detect**: Camoufox fingerprint masking for authenticated sites with bot protection
- **Control**: Stop button lets human halt agent mid-flow
- **Bug closure**: 4 open T10 bugs become moot once Task 0 lands

### Negative
- **Resource usage**: Headed Firefox window uses more memory than headless (~100-200 MB per session)
- **Latency**: Each browser action is a tool call round-trip (vs pipelined popup commands)
- **Server deployment**: Headless server use requires Xvfb (Q2 — not in v1)
- **`storage_state` plaintext on disk**: Acceptable for single-user desktop trust boundary, documented in Confirmation; Vault remains canonical encrypted backup

### Neutral
- `browse_page(keep_open=true)` semantics change: spawns visible Firefox window instead of Tauri popup
- Agent tool registry changes: `popup_*` tools replaced by `browser_*` tools (Task 5)

### Risks
- **Camoufox maintenance**: Fork of Firefox, updates may lag. Mitigation: pin version, test before upgrade.
- **Headed window visibility**: On shared screens, browser content visible to bystanders. Mitigation: audit trail does not log private content; screenshots saved to agent sandbox only.

## Confirmation

How to verify the decision was implemented correctly:

- [ ] Agent can open authenticated page, scroll, click, extract — all via headed Camoufox
- [ ] Human can observe Firefox window in real time during agent action
- [ ] Cookies and localStorage persist across browser restart (via `storage_state`)
- [ ] Domain restriction enforced — agent cannot navigate outside auth domain (eTLD+1 + subdomains)
- [ ] All agent browser actions recorded in `web_audit.jsonl` (extends ADR-028 audit schema)
- [ ] Tauri popup still functions for login flow (ADR-028 T2 unaffected)
- [ ] Stop button appears when browser session live; pressing it halts current tool call without closing Firefox window
- [ ] Cross-platform smoke test: Windows + at least one of Linux/macOS

## Open Questions

- **Q1 — Human intervention model:** ~~deferred to Phase 2~~ **RESOLVED S147 (Mike):** Phase 1, variant A — Stop button in chat panel (visible only when browser session live). Halts current tool call + prevents next steps; Firefox window stays alive for user inspection. No resume/abort/state-machine in v1. Decomposed as Task 6.
- **Q2 — Headless server deployment:** if DPC ever runs on a headless server, headless mode + Xvfb is the path. Not in v1 scope. — @Mike to confirm if/when server deployment is on the roadmap
- **Q3 — Cleanup cadence for `storage_state` files:** ~~@CC to decide during implementation~~ **RESOLVED S147 (CC):** No auto-expiry. Vault is canonical encrypted backup; `storage_state` is a fast-restore cache. Stale cache worst case = re-login popup, which is the same recovery path users already trigger when cookies legitimately expire server-side. TTL adds complexity without payoff.
- **Q4 — Session adoption across DPC restart:** **DEFERRED post-Task 6 (S154, Mike [#28] + Ark [#37]).** Hermes implements session adoption via Camoufox-as-separate-daemon, which survives Hermes Python restart. Our `AuthBrowser` owns the Camoufox subprocess directly, so a DPC restart kills the browser. Three architectural options exist (detached subprocess, persistent CDP context, Node.js sidecar) and the choice needs its own discussion. v1 ships without session adoption — each `browse_page(keep_open=True)` is a fresh session. See `ideas/dpc-research/hermes-browser-patterns.md` §Pattern 2 for the option matrix.

## Implementation Status

| ADR Task | Status | Commit |
|----------|--------|--------|
| ADR-029 draft (first cut) | Done (S147) | `48ebb44` |
| ADR-029 restore to TEMPLATE.md compliance | Done (S147) | `0078b12` |
| Task decomposition | Done (S147) | `tasks/adr-029-headed-camoufox/` (8 files: overview + 7 tasks, gitignored) |
| Task 0 — remove T10 popup code | Done | `d5b171b` |
| Task 1 — extend AuthBrowser | Done | `b36af15` + review fix `a1597aa` |
| Task 2 — storage_state + vault hybrid | Done | `967164f` |
| Task 3 — domain restriction (eTLD+1) | Done | `83353ac` |
| Task 4 — audit trail extension | Done | `50e52dd` |
| Task 5 — agent tool registry rewire | Pending | — |
| Task 6 — interrupt mechanism (Stop button) | Pending | — |

## Authors

Workflow roles per Protocol 13:

- **Mike** — Decision (vision: *"агент мог пользоваться браузером полноценно как человек, а человек мог это видеть"*; option B; order B safety-first; Stop semantics)
- **Ark** — Analysis, initial draft (S146 [48], [50], [53], [56], [58]); architecture diagram + key properties summary (S147)
- **CC** — Review, technical critique, decomposition author (`tasks/adr-029-headed-camoufox/`), template-compliance restoration (S147)

## References

- [ADR-028](028-agent-web-auth-cookie-sharing.md) — Cookie Sharing foundation (this ADR depends on ADR-028 Phase 1 cookie handoff infrastructure)
- [TEMPLATE.md](TEMPLATE.md) — ADR template this document follows
- [`dpc-client/core/dpc_client_core/dpc_agent/tools/browser.py:353-466`](../../dpc-client/core/dpc_client_core/dpc_agent/tools/browser.py#L353) — current `AuthBrowser` implementation
- [`dpc-client/core/dpc_client_core/web_auth.py`](../../dpc-client/core/dpc_client_core/web_auth.py) — `resolve_etld1()` used by Task 2 (domain restriction)
- [`dpc-client/ui/src-tauri/src/web_auth.rs`](../../dpc-client/ui/src-tauri/src/web_auth.rs) — current Tauri popup handlers (Task 0 removes the post-login subset)
- Commit `fc927e1` (S146) — last T10 runtime fixes before deprecation (dict mutation race + UTF-8 panic)
- Commit `48ebb44` (S147) — initial ADR-029 commit (this file replaces it with template-compliant restoration)
- [Camoufox](https://github.com/daijro/camoufox) — anti-detect Playwright Firefox fork (already in use as `AuthBrowser`)
- backlog `AGENT-TOOL-FIREWALL-DEFAULT-DRIFT` — same firewall-sync lesson applies to new `browser_*` tools (Task 5)
- backlog `T10-PATH-A-CLOSE-EXTRACTION`, `T10-POPUP-EXTRACT-LINUX-MACOS` — both close as moot once Task 0 lands
- [Hermes browser patterns research note](../../ideas/dpc-research/hermes-browser-patterns.md) — accessibility-tree + auto-snapshot + summarisation + session-adoption analysis from upstream `NousResearch/hermes-agent`, source of the S154 decision matrix referenced in Task 5 and Q4
