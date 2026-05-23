# ADR-028: Agent Web Authentication via Cookie Sharing

**Status:** Accepted
**Date:** 2026-05-22
**Authors:** Ark, CC (review)
**Scope:** dpc-client (Rust + Python + UI)

## Context

Agents need READ-only access to authenticated web services on behalf of the user (e.g., Ozon order history, Yandex account data). Currently, `browser.py` tools (`browse_page`, `fetch_json`) make anonymous HTTP requests with no authentication capability.

Three approaches were considered in backlog AGENT-WEB-AUTH:
- **(A) OAuth2** — correct but many target services (Ozon, Wildberries) lack public OAuth endpoints
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

```
Agent: "I need access to ozon.ru. Open login window?"
  ↓ user confirms
Tauri command: open_login_window("ozon.ru")
  → Tauri WebView popup (cross-platform — WebView2/WKWebView/WebKitGTK abstracted by Tauri runtime)
  → user logs in manually
  → popup closes → cookies extracted via Tauri native cookies API
  ↓
Cookies stored in encrypted vault per-agent per-domain
  ↓
Agent notified: "authenticated for ozon.ru"
  ↓
Agent: browse_page(url="https://ozon.ru/my/orders", use_auth="ozon.ru")
  → Camoufox launched with planted cookies in profile
  → page content returned to agent
```

### 2. Cookie Storage (Credential Vault)

**Location:** `~/.dpc/agents/{agent_id}/web_credentials.json`

**Format:**
```json
{
  "domains": {
    "ozon.ru": {
      "cookies": [
        {"name": "session_id", "value": "...", "domain": ".ozon.ru", "path": "/", "expires": 1748000000, "secure": true, "httponly": true}
      ],
      "authenticated_at": "2026-05-22T17:30:00Z",
      "last_used_at": "2026-05-22T17:35:00Z"
    }
  }
}
```

**Encryption:** Windows DPAPI via `keyring` crate (Rust-side) / `keyring` library (Python-side). Key derived from OS user account, no additional password prompt. Trade-off: same-user attacker can decrypt. Acceptable for Alpha. Master password option deferred to Phase 2.

**Per-agent isolation:** Each agent has separate vault. Even if two agents authenticate on same domain, cookie jars are independent.

### 3. Cookie Extraction (Tauri Rust)

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

- `login.ozon.ru` → eTLD+1 = `ozon.ru`
- `www.ozon.ru` → eTLD+1 = `ozon.ru`
- `api.ozon.ru` → eTLD+1 = `ozon.ru`

For MVP: hardcoded eTLD+1 map for supported domains. For Phase 2: Mozilla Public Suffix List integration.

All cookies for eTLD+1 and its subdomains are included in authenticated requests.

### 5. Camoufox as Primary Transport for Auth

**Critical design decision:** When `use_auth=domain` is specified, requests ALWAYS go through Camoufox, never through plain `requests.get`.

**Rationale (code-verified):** Current Camoufox fallback in `browser.py` triggers on `len(text) < 200` heuristic. Auth challenge responses from Ozon/Yandex often exceed 200 chars ("please log in" pages), so the heuristic will NOT trigger Camoufox. Even with correct cookies, plain HTTP will fail due to:
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
      "allowed_domains": ["ozon.ru", "yarcheplus.ru"],
      "permissions": "read_only"
    }
  }
}
```

**Enforcement:** `use_auth=domain` parameter in `browse_page`/`fetch_json` is validated against `web_auth.allowed_domains` for the calling agent. If domain not in whitelist → tool call rejected with "domain not authorized".

**`permissions: "read_only"`** is the only value for MVP. WRITE permission is explicitly out of scope. Agent tools cannot perform POST/PUT/DELETE with auth cookies.

### 7. Audit Trail

**New file:** `~/.dpc/agents/{agent_id}/web_audit.jsonl`

**Format:**
```jsonl
{"timestamp": "2026-05-22T17:42:00Z", "agent_id": "agent_001", "domain": "ozon.ru", "url": "https://ozon.ru/my/orders", "method": "GET", "status": 200, "bytes": 45230}
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
- Tool call returns error: "Cookies for ozon.ru expired. Ask user to re-login."
- Agent notifies user: "My access to Ozon has expired. Open login window again?"
- No auto-popup — user must explicitly trigger re-auth (transparent, no surprise)

## Implementation Phases

### Phase 1 (MVP)
1. Rust: `open_login_window` Tauri command (cross-platform via Tauri native cookies API, since 2.9.5)
2. Rust: DPAPI-encrypted credential vault read/write
3. Python: `web_auth.py` module — vault access, cookie resolution, eTLD+1 matching
4. Python: `browser.py` extension — `use_auth` parameter, Camoufox-with-cookies routing
5. Python: firewall validation — `web_auth.allowed_domains` check
6. Python: `web_audit.jsonl` append on every auth request
7. UI: "Login" button in chat (agent-initiated, user-approved)
8. `privacy_rules.json`: `web_auth` block for agent_001

### Phase 2 (Future)
- Master password encryption option
- Audit trail UI panel
- OAuth2 driver for services that support it
- Public Suffix List for eTLD+1 resolution
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
| eTLD+1 edge cases (co.uk, etc.) | Low | Hardcoded MVP domains; PSL integration Phase 2 |
| Ozon/Yandex DOM changes break content extraction | Medium | Agent-side resilience; user can re-trigger extraction |

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

## Review

- **CC (S137):** Verify-pass against code: all 6 review points hold; 2 additional findings (firewall schema gap, Camoufox-as-primary routing) incorporated. 9 clarifying questions on T1-T8 task decomposition resolved.
- **Ark (S137):** Proposal author. Expanded proposal per review findings; answered all clarifying questions; agreed on design details (DPAPI via Python `keyring`, parallel AuthBrowser path, drop `fetch_json+use_auth` from MVP, separate `is_auth_domain_allowed()` firewall function, audit hook inside `web_auth.py`, separate `tools/web_auth_tools.py` module, inline ChatPanel login button).
- **Mike (S137):** Approved scope (variant B / READ-only / Tauri WebView popup). MVP domain list: `ozon.ru` (marketplace orders) + `yarcheplus.ru` (grocery orders); extensible via config without code changes. Design details accepted after team Discussion.

### Resolved Discussion Items

| # | Question | Resolution |
|---|----------|------------|
| 1 | MVP domains | `ozon.ru`, `yarcheplus.ru` — config-driven, extensible |
| 2 | Camoufox cookie planting mechanism | Spike (T1) gate before T4 implementation |
| 3 | Agent domain discovery | New tool `list_auth_domains()` returns `[{domain, has_cookies, expires}]` |
| 4 | WRITE prevention enforcement | Restricted `AuthBrowser` wrapper exposes only `goto(url)` + `get_page_content()` |
| 5 | Audit file write protection | DPC core process (`web_auth.py`) writes; agent has no write access |
| 6 | T1 spike done-criteria | Camoufox + planted cookies for `login.ozon.ru` → `ozon.ru/my/orders` renders as logged-in |
| 7 | T2 IPC mechanism | Existing WebSocket `local_api.py`, new command `web_auth_login_complete` |
| 8 | DPAPI library | Python `keyring` package (cross-platform abstraction; DPAPI on Windows) |
| 9 | AuthBrowser integration | Parallel to existing `_browse_with_camoufox`, triggered only when `use_auth` param present |
| 10 | `fetch_json+use_auth` in MVP | Out of scope — only `browse_page` supports `use_auth` |
| 11 | Firewall validation surface | New `is_auth_domain_allowed(agent_id, domain)`, separate from `is_tool_allowed()` |
| 12 | Audit hook location | Inside `web_auth.py` after successful auth-request |
| 13 | `list_auth_domains` module placement | `tools/web_auth_tools.py` (separate from `browser.py`) |
| 14 | UI panel placement | Inline in `ChatPanel.svelte` (agent-initiated flow, not settings) |
