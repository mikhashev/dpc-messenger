// ADR-027 T2 — Tauri WebView2 cookie extraction
//
// SCAFFOLD ONLY (S137): Cookie / AuthStatus types defined, 3 Tauri commands
// registered, but actual WebView2 cookie extraction is NOT implemented yet.
// All commands return `Err("TODO: ...")` until the full implementation
// sub-task lands.
//
// Reason for scaffolding: full WebView2 / CoreWebView2.CookieManager
// integration is ~200-300 lines of WinRT bindings that needs deliberate
// implementation + testing. Scaffolding proves the module structure compiles
// + commands are registered, so the frontend (T8) can wire against real
// signatures without waiting for the WinRT work to finish.
//
// Done criteria for scaffold:
//   - Cookie struct serializes via serde
//   - AuthStatus struct serializes via serde
//   - 3 Tauri commands present with correct signatures
//   - `cargo check` passes
//
// Next sub-task (full T2 implementation):
//   - Add `webview2-com` Cargo dep (or use Tauri's WebviewWindow API if it
//     exposes cookies natively — Tauri 2 docs suggest checking
//     `WebviewWindow::cookies()` if available)
//   - Implement `open_login_window` to spawn a WebViewWindow popup, wait
//     for it to close, enumerate cookies via CoreWebView2.CookieManager
//   - Implement `get_auth_status` / `revoke_auth`
//   - Emit Tauri event `web_auth_login_complete` so frontend can forward
//     to Python via existing WebSocket (local_api.py command
//     `web_auth_login_complete`)
//
// See tasks/adr-027-agent-web-auth/002-tauri-webview2-cookies.md
// for the full task spec.

use serde::Serialize;

/// HTTP cookie format on the wire to Python.
/// Field names match snake_case per existing DPC conventions.
/// T4 AuthBrowser converts to Playwright camelCase (httpOnly, sameSite)
/// at the Python side (see spike/cookie_plant_test.py:normalize_cookie).
///
/// `expires` is Unix epoch seconds (NOT milliseconds), matching Playwright
/// and most cookie-jar conventions. None = session cookie (no expiry).
#[derive(Debug, Serialize)]
pub struct Cookie {
    pub name: String,
    pub value: String,
    pub domain: String,
    pub path: String,
    pub expires: Option<i64>,
    pub secure: bool,
    pub httponly: bool,
    pub samesite: Option<String>,
}

/// Status of cookie vault for a given domain.
/// Returned by get_auth_status (cheap check, no WebView interaction).
#[derive(Debug, Serialize)]
pub struct AuthStatus {
    pub has_cookies: bool,
    pub expires: Option<i64>,
}

const NOT_IMPLEMENTED_HINT: &str =
    "T2 WebView2 cookie extraction is scaffolded but not yet implemented. \
     Full implementation tracked in tasks/adr-027-agent-web-auth/002-tauri-webview2-cookies.md.";

/// Open a Tauri WebView popup at the given domain, wait for user to log in,
/// extract cookies via CoreWebView2.CookieManager, return them.
///
/// NOT YET IMPLEMENTED — returns Err with TODO until full T2 sub-task lands.
#[tauri::command]
pub async fn web_auth_open_login_window(domain: String) -> Result<Vec<Cookie>, String> {
    Err(format!(
        "open_login_window({}): {}",
        domain, NOT_IMPLEMENTED_HINT
    ))
}

/// Check whether cookies exist for the given domain in the vault.
/// Cheap — does NOT open a WebView window.
///
/// NOT YET IMPLEMENTED — returns Err for consistency with the other
/// stubs (per Ark review S137: Ok(has_cookies: false) would masquerade
/// non-impl as valid "no cookies" state).
#[tauri::command]
pub async fn web_auth_get_status(domain: String) -> Result<AuthStatus, String> {
    Err(format!(
        "get_auth_status({}): {}",
        domain, NOT_IMPLEMENTED_HINT
    ))
}

/// Revoke cookies for the given domain in WebView2 storage.
///
/// NOT YET IMPLEMENTED — returns Err with TODO until full T2 sub-task lands.
#[tauri::command]
pub async fn web_auth_revoke(domain: String) -> Result<(), String> {
    Err(format!(
        "revoke_auth({}): {}",
        domain, NOT_IMPLEMENTED_HINT
    ))
}
