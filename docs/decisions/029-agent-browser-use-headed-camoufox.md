---
adr: 029
title: "Replace T10 popup orchestration with headed Camoufox for agent browser interaction"
status: proposed
date: 2026-05-24
deciders: [Mike]
consulted: [Ark, CC]
informed: []
depends_on: [ADR-028]
related: []
supersedes: []
session: S146
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
- UI staleness — Re-login status badge doesn't reactively update (open, separate from T10)

Meanwhile, Camoufox (anti-detect Playwright fork) is already integrated as `AuthBrowser` ([`browser.py:353-466`](dpc-client/core/dpc_client_core/dpc_agent/tools/browser.py#L353)) for headless authenticated single-shot reads. Playwright provides stable, cross-platform APIs for scroll, click, type, screenshot, navigation, and multi-tab handling.

## Decision

We will replace the T10 popup-based agent interaction pipeline with **headed Camoufox** (visible Firefox window), keeping the Tauri WebView2 popup only for the initial login/cookie capture step.

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
│  Interrupt: human sends "stop" → agent checks inbox      │
│  between browser steps and halts the flow                 │
└──────────────────────────────────────────────────────────┘
```

### Key Properties

1. **Login popup retained** — Tauri WebView2 popup for initial cookie capture only (ADR-028 Phase 1 path). User types credentials, agent gets cookies. No change to existing flow.
2. **Headed Camoufox for agent actions** — All post-login browsing, extraction, and interaction happens in a visible Firefox window via Playwright APIs. Human can observe everything the agent does.
3. **Domain restriction** — Navigation is gated to authorized domains only (`privacy_rules.json` whitelist). Fail-closed: if no whitelist, agent cannot browse.
4. **Storage state persistence** — Playwright `context.storage_state()` saved to disk as fast-restore cache. Vault stores encrypted backup. No auto-expiry (Q3 decision).
5. **Audit trail** — All browser actions logged with action type, URL, selector, timestamp. Privacy: `type` logs `text_length`, not content; `screenshot` logs `byte_size`, not pixels.
6. **Interrupt mechanism** — Agent checks for incoming "stop" messages between browser steps. Stops current flow, reports last completed action. Human can then give new instructions.

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

| Order | Task | Description | Depends on |
|-------|------|-------------|------------|
| 0 | Remove popup code | Delete `popup_*` tools, Rust handlers, frontend `keep_open=true` paths | — |
| 1 | Extend AuthBrowser | Add headed mode, navigation, click, type, scroll, screenshot, extract, close methods + `browser.headed` config | Task 0 |
| 2 | Domain restriction | Playwright `context.route("**/*")` with eTLD+1 gate, fail-closed empty whitelist | Task 1 |
| 3 | Storage state | Load/save `storage_state`, vault sync, no auto-expiry | Task 1 |
| 4 | Audit trail | Structured log of browser actions, privacy-preserving field filtering | Task 1 |
| 5 | Tool registry | Register 9 `browser_*` tools + firewall defaults in one commit | Task 1, 2, 3 |
| 6 | Interrupt mechanism | Agent checks inbox between browser steps, stops on "stop" | Task 1 |

## Consequences

### Positive
- **Stability**: Playwright APIs replace fragile Tauri popup orchestration
- **Cross-platform**: Camoufox works on Linux/macOS (no WebView2 dependency for agent actions)
- **Observability**: Human sees exactly what agent is doing in real-time
- **Anti-detect**: Camoufox fingerprint masking for authenticated sites with bot protection
- **Control**: Interrupt mechanism lets human stop agent mid-flow

### Negative
- **Resource usage**: Headed Firefox window uses more memory than headless
- **Latency**: Each browser action is a tool call round-trip (vs pipelined popup commands)
- **Scope**: Headless server deployment requires Xvfb (Q2 — not in v1)

### Risks
- **Camoufox maintenance**: Fork of Firefox, updates may lag. Mitigation: pin version, test before upgrade.
- **Headed window visibility**: On shared screens, browser content visible to bystanders. Mitigation: audit trail does not log private content; screenshots saved to agent sandbox only.

## Open Questions

- **Q1 — Human intervention model:** ~~for v1, headed Firefox window is observation-only; agent runs, human watches. Pause / take-over controls (human can pause agent mid-flow, manually intervene, then resume) deferred to Phase 2 ADR if needed. — @Mike to confirm~~ **RESOLVED (S147): Phase 1, variant A — interrupt message. Agent checks for incoming "stop" between browser steps and halts. Added as Task 6 to decomposition.**
- **Q2 — Headless server deployment:** if DPC ever runs on a headless server, headless mode + Xvfb is the path. Not in v1 scope. — @Mike
- **Q3 — Cleanup cadence for `storage_state` files:** if a domain hasn't been used in N days, should the cached `storage_state` expire? Vault holds the encrypted canonical anyway. — ~~@CC to decide during implementation~~ **RESOLVED (S147): No auto-expiry. Vault = canonical encrypted backup, storage_state = fast-restore cache. Stale cache worst case = re-login popup.**

## Implementation Status

| Task | Status | Commit |
|------|--------|--------|
| ADR-029 draft + review | In Progress (S146) | — |
| Task decomposition | Done (S147) | tasks/adr-029-headed-camoufox/ (7+1 files) |
