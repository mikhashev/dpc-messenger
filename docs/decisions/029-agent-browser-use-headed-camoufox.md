---
adr: 029
title: "Replace T10 popup orchestration with headed Camoufox for agent browser interaction"
status: accepted
date: 2026-05-24
axis: knowledge
deciders: [Mike]
consulted: [Ark, CC]
informed: []
depends_on: [ADR-028]
related: []
supersedes: []
session: S146-S147
---

# ADR-029: Replace T10 popup orchestration with headed Camoufox for agent browser interaction

> **Amended 2026-09-11.** Two things this ADR specifies are gone. The `privacy_rules.json`
> domain whitelist that Key Property 3 and Task 2 make the authorisation **no longer exists**;
> and **headed mode is no longer exempt from the stored-login gate** — spending a saved login
> now requires a recorded approval whether the window is visible or not. Login also no longer
> happens in the browsing window: it has its own tool and its own clean window. The affected
> passages are marked in place and the reasoning is in *Amendment 2026-09-11* below. The
> mechanism that replaced the whitelist is described in
> [ADR-028's amendment of the same day](028-agent-web-auth-cookie-sharing.md#amendment-2026-09-11--the-whitelist-is-gone-a-recorded-approval-is-the-gate).
>
> **Amended again 2026-09-11 (second reading).** A third thing is gone: `storage_state` and the
> `browser_state.json` cache on disk, which Key Property 4 and the architecture diagram still
> describe. And one sentence of the amendment above has itself gone stale — the UI approval
> channel it calls unfinished is wired. Both are in
> *[Amendment 2026-09-11 (second reading)](#amendment-2026-09-11-second-reading--the-disk-cache-is-gone-and-the-approval-channel-is-wired)*.

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

Mike's directive (S146): the agent must be able to use a browser fully, the way a person does, and a person must be able to watch it doing so — full browser-use with human observability.

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

**Option B** — headed Camoufox as the sole agent browser pipeline, including login. Tauri WebView2 popup deprecated for auth-domain sites (S181 amendment: cross-browser session race with Yarche+ proved popup-based login creates unreliable sessions for Camoufox).

### Rationale

Separation by specialization. Tauri popup is good at human-driven login (cookie capture from password entry through WebView2's process-wide cookie jar). Camoufox is good at programmatic navigation (Playwright API). Trying to make one tool do both led to the T10 bug streak (4 open bugs across S142-S146). Replacing post-login flow with Camoufox unlocks cross-platform support, cookie persistence, and human observability simultaneously, with no new mechanism — the Tauri→Camoufox cookie handoff already exists in ADR-028 Phase 1.

Option A keeps the broken popup pipeline alive; Option C adds a second LLM in the loop with no clear win over Playwright primitives for the v1 scope (READ-only structured-HTML sites). B is the lowest-friction path that resolves all named drivers.

### Architecture

```
┌──────────────────────────────────────────────────────────┐
│                    DPC Agent (Python)                     │
│                                                          │
│  Agent wants to interact with a website:                 │
│  1. Check if storage_state / vault cookies exist         │
│     ├─ Yes → load into Camoufox context, navigate        │
│     └─ No  → open login URL in headed Camoufox,          │
│              human enters credentials in same window,     │
│              cookies saved to vault from same context     │
│  2. Agent calls browser_* tools (navigate, click, etc.)  │
│  3. Human observes in visible Firefox window              │
│  4. On completion → save storage_state + sync to Vault   │
│  5. Close browser                                        │
│                                                          │
│  Key: login + navigation in SAME browser context →       │
│  no cross-browser session transfer, no race conditions    │
│                                                          │
│  Interrupt: human presses Stop button → agent checks     │
│  inbox between browser steps and halts the flow           │
└──────────────────────────────────────────────────────────┘
```

> **Amended 2026-09-11 (second reading) — steps 1 and 4 no longer describe the code.**
> Nothing checks for or saves `storage_state`: the context is created bare, a session's identity
> is the vault jar for its own scope alone, and the close-time writeback goes to the vault and
> nowhere else. Login is also not "the same window" — it has its own tool and its own clean one
> (Key Property 1's marker above). Kept as drawn, because it is the architecture that was
> decided; what replaced it is in *Amendment 2026-09-11 (second reading)* §1 below.

### Key Properties

1. **Login in Camoufox** — Agent opens login URL in headed Camoufox; human enters credentials in the same Firefox window the agent will use for subsequent navigation. Cookies stay in the same browser context — no cross-browser session transfer, no single-session invalidation race. ~~Tauri WebView2 popup login path (ADR-028 T2) deprecated for auth-domain sites; retained only as legacy fallback.~~ *(The popup was not retained as a fallback: `c2cfab07` deleted `web_auth.rs` entirely, 2026-06-05.)*
   > **Amended 2026-09-11 — "the same window" is now deliberately a different one.** Login has its own tool, `open_login_window`, which opens a **clean** window: no `storage_state`, no vault cookies, one site reachable. The reason is in Amendment §2 below — a window that already carries the agent's logins is a window a login page can be made to hand them to.
2. **Headed Camoufox for agent actions** — All browsing, login, extraction, and interaction happens in a visible Firefox window via Playwright APIs. Human can observe everything the agent does.
3. ~~**Domain restriction** — Navigation is gated to authorized domains only (`privacy_rules.json` whitelist). Fail-closed: if no whitelist, agent cannot browse.~~
   > **Void since 2026-09-10.** There is no `privacy_rules.json` whitelist: `agent_profiles.<id>.web_auth.allowed_domains` is gone, and so is the firewall method that read it. Navigation is still gated, but by the session's own eTLD+1 scope and a per-site CDN manifest at node level, and the authorisation to spend a stored login is a recorded approval in the vault. See Amendment §1 and §3 below.
4. ~~**Storage state persistence** — Playwright `context.storage_state()` saved to disk as fast-restore cache. Vault stores encrypted backup. No auto-expiry (Q3 decision).~~
> **Void since 2026-09-11 (second reading).** There is no disk cache and no second store. `browser_state.json` is **neither read nor written**: `AuthBrowser._open` creates the context with no `storage_state` at all and takes the session's identity from `_inject_vault_cookies` — the vault jar for its own scope — while the close-time writeback, renamed `_persist_session_cookies`, syncs in-scope cookies to the vault and maintains no file. The vault is not a backup of a cache; it is the only store. An existing `browser_state.json` on disk is left where it lies and read by nothing. See Amendment §1 below.
5. **Audit trail** — All browser actions logged with action type, URL, selector, timestamp. Privacy: `fill` logs `text_length`, not content; `screenshot` logs `byte_size`, not pixels.
6. **Interrupt mechanism** — Stop button in chat panel (appears only when browser session live). Halts current tool call + prevents next steps; Firefox window stays alive for user inspection (Q1 decision, S147).
7. **Ref-based interaction (S154)** — agent operates over an accessibility-tree snapshot (`page.accessibility.snapshot()`) annotated with ref IDs (`@e1`, `@e2`, …). Tools like `browser_click` / `browser_fill` accept `ref` and the backend resolves it via Playwright's accessibility-aware locators (`get_by_role`). CSS selector remains as a fallback. Replaces raw HTML extraction as the primary interaction surface; HTML/markdown extraction stays available for read-only summarisation.
8. **Auto-snapshot after navigate (S154)** — `browser_navigate` returns the post-navigation a11y snapshot inline, eliminating the round-trip `navigate` → separate `browser_snapshot` call. Reduces tool-call count per page-transition by one.
9. **Snapshot summarisation (S154)** — when a raw snapshot exceeds a configurable size threshold, route the snapshot through the LLM Manager for task-aware summarisation (same infrastructure already used by the sleep-consolidation pipeline). Phase 1 may start with a heuristic viewport-based truncation; Phase 2 swaps in the LLM-based filter when needed.

### Retained from T10

- `browse_page(keep_open=false)` headless single-shot extraction (unchanged)
- Cookie sync to Vault (existing `encrypt_and_store` path)
- ~~Tauri WebView2 popup login as legacy fallback only (deprecated for auth-domain sites per S181 decision)~~ — **not retained: deleted in `c2cfab07` (2026-06-05), `web_auth.rs` and all three `web_auth_*` commands.** Login is now `open_login_window`, a clean headed Camoufox window (Amendment §2)

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
| 2 | Domain restriction | Playwright `context.route("**/*")` with eTLD+1 gate, ~~fail-closed empty whitelist~~ — **amended 2026-09-11**: the whitelist is gone; the gate's scope is the session's own eTLD+1 list, widened for GET/HEAD subresources by a node-level CDN manifest (Amendment §3) | Task 1 |
| 3 | Storage state | ~~Load/save `storage_state`~~, vault sync, no auto-expiry — **amended 2026-09-11 (second reading)**: the load and the save are both gone; the vault sync is the whole of it (Amendment §1) | Task 1 |
| 4 | Audit trail | Structured log of browser actions, privacy-preserving field filtering | Task 1 |
| 5 | Tool registry | Register 9 `browser_*` tools + firewall defaults; **accessibility-tree snapshot + ref-based interaction + auto-snapshot after navigate + LLM-Manager-routed summarization** for snapshots beyond a size threshold (S154 decision matrix) | Task 1, 2, 3 |
| 6 | Interrupt mechanism | Stop button + agent inbox check between browser steps | Task 1 |
| 7 | Camoufox login flow | Agent opens login URL in headed Camoufox; ~~human authenticates in same window~~ — **amended 2026-09-11: in a separate clean window, `open_login_window`** (Amendment §2); cookies saved from that context. Replaces Tauri popup for auth-domain sites. Eliminates cross-browser session race (S181 Yarche+ incident). | Task 1, 3 |

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
- ~~**`storage_state` plaintext on disk**: Acceptable for single-user desktop trust boundary, documented in Confirmation; Vault remains canonical encrypted backup~~ — **void 2026-09-11 (second reading)**: nothing writes it, so the trust boundary this accepted is not one the code asks for any more (Amendment §1)

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
- [ ] ~~Cookies and localStorage persist across browser restart (via `storage_state`)~~ — **replaced 2026-09-11 (second reading):** the writeback carries cookies, and only those in the session's own scope, into the vault; `localStorage` is not in what it saves. The check is now "a second session for the same scope arrives logged in, and `browser_state.json` is neither created nor read" (Amendment §1)
- [ ] Domain restriction enforced — agent cannot navigate outside auth domain (eTLD+1 + subdomains). **Amended 2026-09-11:** navigation, yes; a GET/HEAD *subresource* may also reach a host named in this site's CDN manifest, so the check is "no navigation outside the scope, and no subresource to a host the manifest does not name" (Amendment §3)
- [ ] **Added 2026-09-11:** `browse_page(use_auth=D)` is refused with `auth_denied:no_approved_login` when the vault holds no approval for `D` — **with `keep_open=true` as well as without it**
- [ ] All agent browser actions recorded in `web_audit.jsonl` (extends ADR-028 audit schema)
- [ ] Agent can trigger Camoufox login flow: opens login URL in headed window, human authenticates, cookies saved to vault from same context (Task 7)
- [ ] Stop button appears when browser session live; pressing it halts current tool call without closing Firefox window
- [ ] Cross-platform smoke test: Windows + at least one of Linux/macOS

## Open Questions

- **Q1 — Human intervention model:** ~~deferred to Phase 2~~ **RESOLVED S147 (Mike):** Phase 1, variant A — Stop button in chat panel (visible only when browser session live). Halts current tool call + prevents next steps; Firefox window stays alive for user inspection. No resume/abort/state-machine in v1. Decomposed as Task 6.
- **Q2 — Headless server deployment:** if DPC ever runs on a headless server, headless mode + Xvfb is the path. Not in v1 scope. — @Mike to confirm if/when server deployment is on the roadmap
- **Q3 — Cleanup cadence for `storage_state` files:** ~~@CC to decide during implementation~~ **RESOLVED S147 (CC):** No auto-expiry. Vault is canonical encrypted backup; `storage_state` is a fast-restore cache. Stale cache worst case = re-login popup, which is the same recovery path users already trigger when cookies legitimately expire server-side. TTL adds complexity without payoff. — **Moot 2026-09-11 (second reading):** there are no `storage_state` files to clean up (Amendment §1). Jar freshness in the vault is a separate question and is not answered here.
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
| Task 2 — the hybrid half removed again: no `storage_state`, vault only | Done (2026-09-11) | `974309a9` |
| Task 3 — domain restriction (eTLD+1) | Done | `83353ac` |
| Task 4 — audit trail extension | Done | `50e52dd` |
| Task 5 — agent tool registry rewire | Pending | — |
| Task 6 — interrupt mechanism (Stop button) | Pending | — |
| Task 7 — Camoufox login flow | ~~Pending~~ **Done** — checked 2026-09-11 (second reading): `open_login_window` is a function and a registered tool in `browser.py`, `default_enabled=False` | `974309a9` |
| Task 008 — remove Tauri login popup + per-request headless auth gate | Done (2026-06-05) | `c2cfab07` |
| Web-auth whitelist removal + approval-in-vault gate + PSL resolver + CDN manifest | In the working tree, **uncommitted** as of 2026-09-11 | — |

> **Amended 2026-09-11.** Task 008 exists only in `tasks/adr-029-headed-camoufox/008-…md`
> (gitignored) and was never listed here; its own header still reads `**Status:** PENDING`
> although `c2cfab07` landed it on 2026-06-05, and it records Task 7 as DONE while the row
> above still says Pending. The two rows added here are what was verified in this session;
> the Task 7 row is left as found because its state was not checked.
>
> **Amended 2026-09-11 (second reading).** Task 7 has now been checked, and it is done:
> `974309a9` carries `open_login_window` and its registration. The row above says so.

## Amendment 2026-09-11 — the whitelist is gone, and headed is no longer exempt

**Decided by Mike, 2026-09-11**, in the team chat, on a reviewer's point about ADR-028: a
decision record that still prescribes a deleted mechanism means "the next person who opens the
ADR reads a cancelled world". ADR-028 is the primary amendment and carries the full mechanism;
this one records the two places where *this* ADR's own text is now wrong. Board:
`THE-DECISION-RECORD-STILL-SPECIFIES-THE-WEB-AUTH-GATE-THAT-WAS-DELETED`.

Form follows ADR-032 (2026-09-07): the superseded text stays where it is with a dated marker,
because a decision that was reversed should show both states rather than only the last one.
Code below is cited **by symbol, not by line** — `browser.py` and `web_auth.py` were being
edited by a parallel task while this was written, and line numbers taken in the afternoon were
wrong by the evening.

### 1. Headed mode is not an authorisation

Task 008 (`c2cfab07`, 2026-06-05) set up two modes: headed needs no gate because "human sees the
browser", headless asks a person per request. The first half no longer holds. A stored login is
now spent only against a **recorded approval** in the vault — `web_auth.is_approved`, checked in
`browse_page` before anything opens, refused with audit status `auth_denied:no_approved_login` —
and that check does not look at `keep_open`. The per-request UI prompt of Task 008 is unchanged
and still applies to headless only; it is a *second* gate, not the first one.

The reason is written at the gate in `browser.py`: `keep_open=True` spends a stored login exactly
as headless does, and a window being visible does not make the account's owner the one who chose
to spend it. A human watching a browser window is evidence that something is happening, not
consent that it should.

### 2. Login moved out of the browsing window

Key Property 1 makes it a virtue that the human logs in *in the same window the agent will keep
using*. That was true against the cross-browser session race it was written for, and it is the
wrong trade now: a window that already carries every login the agent holds is a window a hostile
login page has something to take from. Login is now its own tool, `open_login_window`, opening a
window that starts with nothing in it — no `storage_state`, no vault cookies, one site reachable
(`AuthBrowser._start_clean`). It is registered `default_enabled=False`, per the S148 rule in
CLAUDE.md.

The cross-browser race that Key Property 1 was answering does not come back, because both windows
are Camoufox and the cookies pass through the vault, not through a second browser engine.

### 3. What the route gate checks now

Task 2's "fail-closed empty whitelist" referred to `privacy_rules.json`. With that whitelist gone,
the gate's scope is the session's own eTLD+1 list, and an empty list still fails closed — a
session with no scope is instead started clean, so it carries no identity to leak.

One widening was added deliberately: a **GET/HEAD subresource** (never a navigation) may reach a
host outside the scope when the initiating frame is inside the scope **and** the host is named in
that site's entry of `~/.dpc/web_cdn_manifest.json` — node level, outside every agent's sandbox.
Without it, real sites render blank, because a site's bundle lives on hosts it never names.
Refused hosts are folded into a per-agent refusal file read by the UI and by nothing on the
request path; **a refusal authorises nothing**, and promotion into the manifest is a
separate act. The same shape as the vault: what the agent can write is never what grants.

`resolve_etld1`, which both this gate and the vault key spend as a security boundary, is now a
real Public Suffix List resolver and returns `None` — never its input — for anything that is not
a registrable domain (board `RESOLVE-ETLD1-IS-THE-IDENTITY-FUNCTION-FOR-EVERY-REAL-DOMAIN`).

### 4. Open, and not closed by this amendment

- Approval is being moved to an explicit human answer over the UI approval channel, because a
  clean window turned out not to guarantee that a cookie came from a person: a site that hands
  any visitor anonymous cookies minted an approval on a logged-out page during the first live
  run, 2026-09-11 (*reported from that run, not reproduced here*). `web_auth.record_approval`
  exists and takes no cookie argument on purpose; at the last reading it had **no caller**. Not
  finished.
- `browser_state.json` in the agent sandbox is a second, plaintext store of logins that the
  approval gate does not read — board
  `A-HEADED-SESSION-OPENED-WITHOUT-USE-AUTH-INSTALLS-NO-ROUTE-GATE-AND-STILL-LOADS-EVERY-COOKIE`
  and `LIST-AUTH-DOMAINS-REPORTS-ON-A-STORE-THAT-NO-LONGER-RECEIVES-LOGINS`.
- Q4 (session adoption across DPC restart) and Q2 (headless server) are untouched by this
  amendment.

## Amendment 2026-09-11 (second reading) — the disk cache is gone, and the approval channel is wired

This repairs the amendment above rather than the body: two of its own sentences have since become
false, and one of them false in the direction that says less was built than was. The earlier
amendment stays exactly as written — an amendment that is edited in place stops being a record of
what was believed when. Both readings are the same day; this one was taken after `974309a9`.

The caution the first amendment gave itself applies here twice over: `browser.py` and
`web_auth.py` were **being edited by a parallel task while this was written**, so code is cited
**by symbol, not by line**, and §2 below separates what is settled from what was moving.

### 1. `browser_state.json` is not a store any more — it is not touched at all

Key Property 4, the architecture diagram's steps 1 and 4, Task 3, the Q3 resolution and the
`storage_state`-on-disk consequence all describe a fast-restore cache on disk. There is none.

- `AuthBrowser._open` creates its context with no `storage_state` argument, for any session.
- A session's identity is `_inject_vault_cookies`: the vault jar for the scope it was opened
  with, and nothing else. A clean-start session (`_start_clean`) gets none at all.
- The writeback is `_persist_session_cookies`, and its whole effect is
  `_sync_cookies_to_vault` — in-scope cookies into the encrypted vault. Its docstring states
  the rest: `browser_state.json` "is neither read nor written any more — the file on disk is
  left alone, but nothing here maintains it".
- `_save_storage_state`, which the ADR-028 amendment of the same morning still names, no longer
  exists.

So the second bullet of *Amendment 2026-09-11* §4 — "`browser_state.json` in the agent sandbox is
a second, plaintext store of logins that the approval gate does not read" — was true when it was
written and is not true now. The gate does not read that file, and neither does anything else. A
file left on disk from before the change is inert; deleting it is housekeeping, not a fix.

Why this mattered is worth keeping, because it is the reason the property was reversed rather
than merely dropped: loading that file made a `use_auth=S` browser arrive holding identities for
sites nobody had approved — the approval gate guarding one door while the identity came in
through another.

The board entries `A-HEADED-SESSION-OPENED-WITHOUT-USE-AUTH-INSTALLS-NO-ROUTE-GATE-AND-STILL-LOADS-EVERY-COOKIE`
and `LIST-AUTH-DOMAINS-REPORTS-ON-A-STORE-THAT-NO-LONGER-RECEIVES-LOGINS` are not closed by this
note: the loading half is what was checked here.

### 2. The UI approval channel is wired; "not finished" no longer holds

The first bullet of *Amendment 2026-09-11* §4 says approval "is being moved" to an explicit human
answer over the UI approval channel, that `web_auth.record_approval` "had **no caller**", and
"Not finished." The move has landed:

- `open_login_window` asks, through `_ask_human_to_approve(kind=APPROVAL_KIND_LOGIN, …)`, and
  writes nothing unless the answer is yes — the vault is left exactly as it was on every other
  outcome.
- The answer travels back over the existing channel: `web_auth_approve_headless` /
  `web_auth_reject_headless` in `service.py`, both on the `local_api.py` command allowlist, from
  `WebAuthApprovalDialog.svelte` in the client UI.
- The same helper serves the headless per-request gate, which is the older of the two callers.

**One distinction this amendment insists on, because the two are easy to read as one fact.** The
*channel* is finished. The *function named in the sentence above* is not what performs the write:
the approval and the cookies go down together through `commit_login_cookies` →
`save_cookies(approved_via=…)`, and at this reading `web_auth.record_approval` again had no
production caller — it had one at `974309a9`, and the parallel task's uncommitted edit replaced
that call with a read. Which function writes the approval was moving while this was written and
may have moved again; that a human answer, and only a human answer, is what grants is the part
that is settled.

### 3. What this reading did not check

- What the person sees when the wait runs out. The backend sends no event at that moment —
  `_ask_human_to_approve` pops the entry and proceeds as a refusal in silence, unlike the shell
  gate, which broadcasts `shell_approval_expired` — so the card retires itself from the
  `timeout_sec` the request carries (`dpc-client/ui/src/lib/services/webAuthApproval.ts`,
  2026-09-11). A backend emitter would be the better half of that pair and does not exist.
- Session reuse deciding reuse without comparing the domain argument — the other half of
  `A-HEADED-SESSION-OPENED-WITHOUT-USE-AUTH-INSTALLS-NO-ROUTE-GATE-AND-STILL-LOADS-EVERY-COOKIE`.
- Q4 (session adoption across DPC restart) and Q2 (headless server), untouched by either
  amendment.

## Authors

Workflow roles per Protocol 13:

- **Mike** — Decision (vision: the agent uses a browser fully, the way a person does, and a person can watch it doing so; option B; order B safety-first; Stop semantics)
- **Ark** — Analysis, initial draft (S146 [48], [50], [53], [56], [58]); architecture diagram + key properties summary (S147)
- **CC** — Review, technical critique, decomposition author (`tasks/adr-029-headed-camoufox/`), template-compliance restoration (S147)

## References

- [ADR-028](028-agent-web-auth-cookie-sharing.md) — Cookie Sharing foundation (this ADR depends on ADR-028 Phase 1 cookie handoff infrastructure)
- [TEMPLATE.md](TEMPLATE.md) — ADR template this document follows
- [`dpc-client/core/dpc_client_core/dpc_agent/tools/browser.py:353-466`](../../dpc-client/core/dpc_client_core/dpc_agent/tools/browser.py#L353) — current `AuthBrowser` implementation
- [`dpc-client/core/dpc_client_core/web_auth.py`](../../dpc-client/core/dpc_client_core/web_auth.py) — `resolve_etld1()` used by Task 2 (domain restriction); since 2026-09-11 a real Public Suffix List resolver, and the home of the vault approval (`record_approval`, `is_approved`) and the CDN manifest
- [ADR-028 — Amendment 2026-09-11](028-agent-web-auth-cookie-sharing.md#amendment-2026-09-11--the-whitelist-is-gone-a-recorded-approval-is-the-gate) — the mechanism that replaced the whitelist, in full
- Commit `c2cfab07` (2026-06-05) — ADR-029 Task 008: Tauri login popup deleted, per-request headless approval added. Task file: `tasks/adr-029-headed-camoufox/008-tauri-login-removal-and-headless-gate.md` (gitignored)
- backlog `THE-DECISION-RECORD-STILL-SPECIFIES-THE-WEB-AUTH-GATE-THAT-WAS-DELETED` — the entry this amendment closes
- backlog `THE-VAULT-STAMPS-A-FRESH-LOGIN-EVERY-TIME-THE-BROWSER-CLOSES` — why `authenticated_at` could not be the approval
- backlog `RESOLVE-ETLD1-IS-THE-IDENTITY-FUNCTION-FOR-EVERY-REAL-DOMAIN` — why the resolver had to become real before either gate could be trusted
- backlog `A-HEADED-SESSION-OPENED-WITHOUT-USE-AUTH-INSTALLS-NO-ROUTE-GATE-AND-STILL-LOADS-EVERY-COOKIE` — ~~open, and the reason the amendment claims nothing about `browser_state.json`~~ — **2026-09-11 (second reading):** the loading half was checked and is gone; the session-reuse half was not (Amendment §1)
- backlog `NOTHING-ASKS-WHICH-PREMISE-UNDER-A-DECISION-CARRIES-A-MEASUREMENT` — the unmeasured premise the whitelist deletion rested on
- ~~[`dpc-client/ui/src-tauri/src/web_auth.rs`](../../dpc-client/ui/src-tauri/src/web_auth.rs) — current Tauri popup handlers (Task 0 removes the post-login subset)~~ — **the file no longer exists**; deleted whole in `c2cfab07`, so this link is dead. Read it at `git show c2cfab07^:dpc-client/ui/src-tauri/src/web_auth.rs`
- Commit `fc927e1` (S146) — last T10 runtime fixes before deprecation (dict mutation race + UTF-8 panic)
- Commit `48ebb44` (S147) — initial ADR-029 commit (this file replaces it with template-compliant restoration)
- [Camoufox](https://github.com/daijro/camoufox) — anti-detect Playwright Firefox fork (already in use as `AuthBrowser`)
- backlog `AGENT-TOOL-FIREWALL-DEFAULT-DRIFT` — same firewall-sync lesson applies to new `browser_*` tools (Task 5)
- backlog `T10-PATH-A-CLOSE-EXTRACTION`, `T10-POPUP-EXTRACT-LINUX-MACOS` — both close as moot once Task 0 lands
- [Hermes browser patterns research note](../../ideas/dpc-research/hermes-browser-patterns.md) — accessibility-tree + auto-snapshot + summarisation + session-adoption analysis from upstream `NousResearch/hermes-agent`, source of the S154 decision matrix referenced in Task 5 and Q4
