// ADR-028 T2 — Tauri WebView cookie extraction (cross-platform)
//
// Uses Tauri 2.x native `WebviewWindow::cookies_for_url()` and friends —
// no per-OS WinRT / WKWebView / WebKitGTK bindings. API available since
// Tauri 2.9.5 (our current Cargo.lock pin).
//
// Implementation pattern: close-event + spawn-thread.
//   - open_login_window spawns a Tauri WebView popup
//   - on WindowEvent::CloseRequested, a separate thread is spawned that
//     calls cookies_for_url(); spawning avoids the Windows sync-handler
//     deadlock flagged in Tauri docs.
//   - extracted cookies converted to our snake_case Cookie struct and
//     emitted via Tauri event `web_auth_login_complete` with payload
//     {domain, cookies}; frontend forwards via existing WebSocket to
//     Python vault (T3).
//
// See ADR-028 + tasks/adr-028-agent-web-auth/002-tauri-webview2-cookies.md.

use serde::Serialize;
use tauri::{
    AppHandle, Emitter, Manager, Url, WebviewUrl, WebviewWindowBuilder, WindowEvent,
};

/// HTTP cookie format on the wire to Python.
/// Field names match snake_case per existing DPC conventions.
/// T4 AuthBrowser converts to Playwright camelCase (httpOnly, sameSite)
/// at the Python side (see spike/cookie_plant_test.py:normalize_cookie).
///
/// `expires` is Unix epoch seconds (NOT milliseconds), matching Playwright
/// and most cookie-jar conventions. None = session cookie (no expiry).
#[derive(Debug, Serialize, Clone)]
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

/// Status of cookies in the Tauri runtime store for a domain.
/// Returned by get_status (cheap check, no WebView popup).
///
/// Note: this is Tauri runtime store state, NOT the encrypted
/// Python-side vault (T3). For vault state, ask Python via WebSocket.
#[derive(Debug, Serialize)]
pub struct AuthStatus {
    pub has_cookies: bool,
    pub expires: Option<i64>,
}

fn convert_cookie(c: &tauri::webview::Cookie<'_>) -> Cookie {
    Cookie {
        name: c.name().to_string(),
        value: c.value().to_string(),
        domain: c.domain().unwrap_or("").to_string(),
        path: c.path().unwrap_or("/").to_string(),
        expires: c.expires_datetime().map(|dt| dt.unix_timestamp()),
        secure: c.secure().unwrap_or(false),
        httponly: c.http_only().unwrap_or(false),
        samesite: c.same_site().map(|s| s.to_string()),
    }
}

fn label_for(domain: &str) -> String {
    format!("web_auth_{}", domain.replace('.', "_"))
}

fn parse_domain_url(domain: &str) -> Result<Url, String> {
    Url::parse(&format!("https://{}", domain))
        .map_err(|e| format!("invalid domain '{}': {}", domain, e))
}

/// Open a Tauri WebView popup at the given domain for the user to log in.
/// When the user closes the popup, cookies for the domain are read from
/// the Tauri runtime cookie store and emitted as the
/// `web_auth_login_complete` Tauri event with payload {domain, cookies}.
///
/// Returns Ok(()) immediately after the popup is built — the actual
/// cookie delivery is asynchronous via the Tauri event.
#[tauri::command]
pub async fn web_auth_open_login_window(
    app: AppHandle,
    domain: String,
) -> Result<(), String> {
    let url = parse_domain_url(&domain)?;
    let label = label_for(&domain);

    let window = WebviewWindowBuilder::new(
        &app,
        &label,
        WebviewUrl::External(url.clone()),
    )
    .title(format!("Login — {}", domain))
    .inner_size(900.0, 700.0)
    .build()
    .map_err(|e| e.to_string())?;

    // close-event + spawn-thread pattern: spawning a thread before calling
    // cookies_for_url() avoids the Windows sync-handler deadlock flagged
    // in Tauri docs (WebView2 limitation).
    //
    // Race-condition note (Ark review S140): the spawned thread reaches
    // for `get_webview_window(&label)` AFTER CloseRequested has fired,
    // so there is a narrow window where the WebviewWindow may already
    // be destroyed before the thread runs. Tauri's contract is that
    // CloseRequested fires BEFORE destruction (the close is cancelable
    // from the handler), so in practice the window handle is still
    // valid when the thread starts; but the `get_webview_window` lookup
    // returns Option<_> and we early-return None — if a future Tauri
    // release tightens timing such that destroy runs concurrently with
    // our thread spawn, this code degrades to "popup closes silently
    // without emitting cookies" rather than crashing. The user sees no
    // login_complete event and can retry. Phase 2 follow-up: capture
    // cookies synchronously in the handler via a oneshot channel into
    // the spawned thread, eliminating the lookup race.
    let app_for_event = app.clone();
    let domain_for_event = domain.clone();
    let label_for_event = label.clone();
    let url_for_event = url.clone();
    window.on_window_event(move |event| {
        if let WindowEvent::CloseRequested { .. } = event {
            let app_thread = app_for_event.clone();
            let domain_thread = domain_for_event.clone();
            let label_thread = label_for_event.clone();
            let url_thread = url_for_event.clone();
            std::thread::spawn(move || {
                let Some(win) = app_thread.get_webview_window(&label_thread) else {
                    return;
                };
                let Ok(cookies) = win.cookies_for_url(url_thread) else {
                    return;
                };
                let converted: Vec<Cookie> = cookies.iter().map(convert_cookie).collect();
                let payload = serde_json::json!({
                    "domain": domain_thread,
                    "cookies": converted,
                });
                let _ = app_thread.emit("web_auth_login_complete", payload);
            });
        }
    });

    Ok(())
}

/// Check whether cookies exist for the given domain in the Tauri runtime
/// cookie store. Cheap — does NOT open a WebView popup.
#[tauri::command]
pub async fn web_auth_get_status(
    app: AppHandle,
    domain: String,
) -> Result<AuthStatus, String> {
    let url = parse_domain_url(&domain)?;
    let main = app
        .get_webview_window("main")
        .ok_or_else(|| "main window not found".to_string())?;
    let cookies = main
        .cookies_for_url(url)
        .map_err(|e| format!("cookies_for_url failed: {}", e))?;
    let earliest_expiry = cookies
        .iter()
        .filter_map(|c| c.expires_datetime().map(|dt| dt.unix_timestamp()))
        .min();
    Ok(AuthStatus {
        has_cookies: !cookies.is_empty(),
        expires: earliest_expiry,
    })
}

/// Revoke (delete) all cookies for the given domain from the Tauri
/// runtime cookie store. Vault deletion is a separate Python-side
/// concern (T3).
#[tauri::command]
pub async fn web_auth_revoke(
    app: AppHandle,
    domain: String,
) -> Result<(), String> {
    let url = parse_domain_url(&domain)?;
    let main = app
        .get_webview_window("main")
        .ok_or_else(|| "main window not found".to_string())?;
    let cookies = main
        .cookies_for_url(url)
        .map_err(|e| format!("cookies_for_url failed: {}", e))?;
    for cookie in cookies {
        main.delete_cookie(cookie)
            .map_err(|e| format!("delete_cookie failed: {}", e))?;
    }
    Ok(())
}
