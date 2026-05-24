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
    // Cookie lookup target: MAIN window, not the popup.
    //
    // First pass (S140) extracted cookies from the popup window itself via
    // `get_webview_window(&label)`. Mike S141 empirical test showed the
    // popup window handle is already gone by the time the spawned thread
    // runs — `get_webview_window` returns None and the thread early-returns
    // silently without emitting cookies. The ui.log line
    // `[web_auth] Tauri event listener installed` appears but no
    // `[web_auth] received login_complete` ever follows.
    //
    // Switching to the MAIN window's cookie store works because on
    // Windows WebView2 the cookie jar is process-wide — the same
    // pattern is already used by `web_auth_get_status` and
    // `web_auth_revoke` below. The popup's URL/origin determines which
    // cookies are returned, so we still get only the login site's jar.
    let app_for_event = app.clone();
    let domain_for_event = domain.clone();
    let url_for_event = url.clone();
    window.on_window_event(move |event| {
        if let WindowEvent::CloseRequested { .. } = event {
            let app_thread = app_for_event.clone();
            let domain_thread = domain_for_event.clone();
            let url_thread = url_for_event.clone();
            std::thread::spawn(move || {
                let Some(main_win) = app_thread.get_webview_window("main") else {
                    return;
                };
                let Ok(cookies) = main_win.cookies_for_url(url_thread) else {
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

/// ADR-028 T9 — popup fallback for anti-bot challenge sites + JS-rendered
/// content (YarchePlus orders class).
///
/// Opens a Tauri WebView popup at `url` using the shared WebView2 cookie
/// jar (same one populated by `web_auth_open_login_window` at T8 login).
/// When the user closes the popup, a JS function injected at create-time
/// captures `document.documentElement.outerHTML` + `window.location.href`
/// and emits the Tauri event `web_auth_popup_extracted` with payload
/// `{request_id, content_html, current_url}` — or `{request_id, error}`
/// on failure. The frontend listener forwards via WebSocket
/// `web_auth_popup_complete` to the Python Step 3 handler.
///
/// ## Two-step IPC pattern (T9 spec constraint)
///
/// `webview.eval()` in Tauri 2 returns `Result<()>`, NOT the JS value
/// (intentional design — eval is fire-and-forget). To get HTML back to
/// Rust/Python we cannot use `eval(...).then(html => ...)` — instead:
///   1. inject a JS function `__dpc_t9_emit_html__()` at popup create
///   2. on CloseRequested, spawn a thread that calls `eval("__dpc_t9_emit_html__()")`
///   3. the JS function emits a Tauri event with the captured payload
///   4. frontend's existing Tauri event listener catches it
///
/// The spawn-thread on close matches the working T2 pattern (avoids the
/// Windows sync-handler deadlock flagged in Tauri docs). There is a
/// race: window may already be torn down by the time the thread runs
/// eval. In that case the no-window branch emits an `error` payload so
/// the Step 3 handler surfaces `AuthRequiredError` cleanly rather than
/// timing out at the 5-min wait.
#[tauri::command]
pub async fn web_auth_open_popup_for_content(
    app: AppHandle,
    url: String,
    request_id: String,
    domain: String,
    keep_open: bool,
) -> Result<(), String> {
    let parsed = Url::parse(&url)
        .map_err(|e| format!("invalid url '{}': {}", url, e))?;
    let label = format!("web_auth_popup_{}", request_id);

    // Injected at create-time so it survives intra-popup navigation
    // (Tauri's `initialization_script` runs before every page script,
    // including after the user clicks links inside the popup). The
    // request_id is closed over so the same popup can never resolve
    // a different request even if the JS were called externally.
    let init_script = format!(
        r#"
        window.__dpc_t9_request_id__ = "{request_id}";
        window.__dpc_t9_emit_html__ = function() {{
            try {{
                window.__TAURI__.event.emit("web_auth_popup_extracted", {{
                    request_id: window.__dpc_t9_request_id__,
                    content_html: document.documentElement.outerHTML,
                    current_url: window.location.href
                }});
            }} catch (e) {{
                window.__TAURI__.event.emit("web_auth_popup_extracted", {{
                    request_id: window.__dpc_t9_request_id__,
                    error: String(e)
                }});
            }}
        }};
        "#,
        request_id = request_id
    );

    // T10 Q4: when the agent is driving a multi-page session, the
    // window title advertises that closing aborts the workflow so the
    // user understands the popup is not orphaned.
    let title = if keep_open {
        format!("Agent active — close to abort — {}", url)
    } else {
        format!("DPC — {}", url)
    };

    let window = WebviewWindowBuilder::new(
        &app,
        &label,
        WebviewUrl::External(parsed.clone()),
    )
    .title(title)
    .inner_size(1000.0, 800.0)
    .initialization_script(&init_script)
    .build()
    .map_err(|e| e.to_string())?;

    let app_for_event = app.clone();
    let label_for_event = label.clone();
    let request_id_for_event = request_id.clone();
    let domain_for_event = domain.clone();
    let url_for_cookies = parsed;
    window.on_window_event(move |event| {
        if let WindowEvent::CloseRequested { .. } = event {
            // Notify the frontend that the popup is closing BEFORE we
            // try to eval the JS emitter. This decouples "user closed
            // the window" from "JS ran successfully and emitted" — the
            // frontend can start a watchdog timer here, and the
            // listener for `web_auth_popup_extracted` cancels it when
            // the success/error event actually arrives. Without this,
            // a torn-down WebView (Path A — eval Ok but JS context
            // killed before emit) leaves the frontend modal stuck.
            let _ = app_for_event.emit(
                "web_auth_popup_closing",
                serde_json::json!({ "request_id": &request_id_for_event }),
            );

            // Bug 4 — vault re-sync. The WebView2 cookie jar may have
            // refreshed cookies via ordinary page navigation between
            // popup open and close (Set-Cookie headers from the site,
            // session renewals, etc.). The Python vault is a snapshot
            // taken at T8 login time and never re-syncs — so without
            // this hook, the agent's auth status report stays stale
            // even when the site is in fact still authenticated.
            // Re-using the existing `web_auth_login_complete` event:
            // the frontend pendingMap (registered by WebAuthPopupRequest
            // before invoking this command) will pair the cookies with
            // the originating agent and forward to the Python vault.
            let cookies_app = app_for_event.clone();
            let cookies_domain = domain_for_event.clone();
            let cookies_url = url_for_cookies.clone();
            std::thread::spawn(move || {
                let Some(main_win) = cookies_app.get_webview_window("main") else {
                    return;
                };
                let Ok(cookies) = main_win.cookies_for_url(cookies_url) else {
                    return;
                };
                let converted: Vec<Cookie> = cookies.iter().map(convert_cookie).collect();
                let payload = serde_json::json!({
                    "domain": cookies_domain,
                    "cookies": converted,
                });
                let _ = cookies_app.emit("web_auth_login_complete", payload);
            });

            let app_thread = app_for_event.clone();
            let label_thread = label_for_event.clone();
            let req_id_thread = request_id_for_event.clone();
            std::thread::spawn(move || {
                if let Some(popup) = app_thread.get_webview_window(&label_thread) {
                    // Fire-and-forget — JS event leaves the popup
                    // process and is delivered to the frontend listener
                    // before the window's resources are released.
                    if let Err(e) = popup.eval("window.__dpc_t9_emit_html__()") {
                        // Path C — eval failed at the Rust↔WebView2
                        // boundary (WebView already in tear-down).
                        // Surface so the frontend can clean up without
                        // waiting for the watchdog timeout.
                        let _ = app_thread.emit(
                            "web_auth_popup_extracted",
                            serde_json::json!({
                                "request_id": req_id_thread,
                                "error": format!("eval failed during popup close: {}", e),
                            }),
                        );
                    }
                } else {
                    let _ = app_thread.emit(
                        "web_auth_popup_extracted",
                        serde_json::json!({
                            "request_id": req_id_thread,
                            "error": "popup window closed before extraction",
                        }),
                    );
                }
            });
        }
    });

    Ok(())
}

// ─────────────────────────────────────────────────────────────
// T10 — Agent-orchestrated multi-page popup browsing
// ─────────────────────────────────────────────────────────────
//
// The three commands below extend the T9 popup machinery so an agent
// can drive a single popup across multiple page loads — extract HTML
// at any point while the popup is alive (bypassing the Path A race
// that affects extract-on-close), navigate within the same eTLD+1,
// and close programmatically when done.
//
// Window lookup convention: all three commands key off the popup
// label `web_auth_popup_{request_id}` set up by
// `web_auth_open_popup_for_content`. The Python side owns the
// session state (lifetime cap, concurrency, eTLD+1 enforcement) and
// these Rust commands stay mechanical.
//
// See `tasks/adr-028-agent-web-auth/010-agent-popup-orchestration.md`
// — decisions locked S143.

fn popup_label(request_id: &str) -> String {
    format!("web_auth_popup_{}", request_id)
}

/// T10: explicit HTML extraction trigger.
///
/// Reads the popup's `document.documentElement.outerHTML` and emits
/// `web_auth_popup_extracted` from the main app handle — the existing
/// `coreService.ts` listener forwards the payload to the Python
/// step-3 handler exactly as in the on-close flow.
///
/// S145 (ADR-028): the previous implementation called the injected
/// `__dpc_t9_emit_html__()` JS function and relied on its
/// `window.__TAURI__.event.emit(...)` to bubble the result back. That
/// silently failed on external URLs (yarcheplus.ru, ozon.ru) because
/// Tauri 2 does not inject `window.__TAURI__` into webviews loading
/// non-app origins — the JS function's outer `try` AND the catch-block
/// fallback both die on `Cannot read 'event' of undefined`. Tauri's
/// own `WebviewWindow::eval()` is fire-and-forget (returns
/// `Result<(), Error>`, never a JS value), so we cannot route the
/// extraction through the high-level Tauri API either.
///
/// Fix: on Windows, use `with_webview` to drop down to the native
/// WebView2 `ICoreWebView2::ExecuteScript` API, which DOES return the
/// JS expression's value as a JSON-encoded string via callback. We
/// then emit `web_auth_popup_extracted` from the main `AppHandle`
/// (where `__TAURI__` is fine — it's the app's own webview, not the
/// popup), preserving the existing event contract.
///
/// Linux/macOS: not yet implemented (backlog item — needs WebKitGTK
/// `webkit_web_view_run_javascript` and WKWebView
/// `evaluateJavaScript:completionHandler:` respectively). Returns
/// `Err` with a clear message so the caller can surface it.
#[tauri::command]
pub async fn web_auth_popup_extract_now(
    app: AppHandle,
    request_id: String,
) -> Result<(), String> {
    let label = popup_label(&request_id);
    let popup = app
        .get_webview_window(&label)
        .ok_or_else(|| format!("popup window not found: request_id={}", request_id))?;

    #[cfg(target_os = "windows")]
    {
        extract_html_via_webview2(&app, &popup, request_id)
    }

    #[cfg(not(target_os = "windows"))]
    {
        let _ = (&popup, &app); // suppress unused warnings on non-Windows
        Err(format!(
            "web_auth_popup_extract_now: HTML extraction not yet implemented \
             on this platform (request_id={}). See ADR-028 T10 backlog.",
            request_id
        ))
    }
}

/// Windows-only: extract popup HTML via native WebView2 `ExecuteScript`
/// and emit `web_auth_popup_extracted` on the main app handle.
///
/// `ExecuteScript` returns the JS expression value JSON-encoded — so a
/// JS string `"foo"` comes back as the 5-byte Rust string `"\"foo\""`.
/// We wrap the extraction in `JSON.stringify({html, url})` so the
/// callback receives one well-formed JSON-encoded object string.
///
/// `with_webview` returns synchronously; the ExecuteScript callback
/// fires later on the main thread. The Tauri command returns `Ok(())`
/// immediately — the frontend's `await invoke()` resolves before the
/// extraction finishes, but it doesn't matter because the frontend
/// only cares about the `web_auth_popup_extracted` event that fires
/// when the callback emits it.
#[cfg(target_os = "windows")]
fn extract_html_via_webview2(
    app: &AppHandle,
    popup: &tauri::WebviewWindow,
    request_id: String,
) -> Result<(), String> {
    use webview2_com::ExecuteScriptCompletedHandler;
    use windows::core::HSTRING;

    let app_for_callback = app.clone();
    let request_id_for_callback = request_id.clone();

    popup
        .with_webview(move |webview| {
            // SAFETY: webview2-com COM pointers are valid for the lifetime
            // of the popup window; the closure only runs while the popup
            // is alive (Tauri schedules it on the popup's UI thread).
            let core_webview2 = unsafe { webview.controller().CoreWebView2() };
            let core_webview2 = match core_webview2 {
                Ok(cv) => cv,
                Err(e) => {
                    let _ = app_for_callback.emit(
                        "web_auth_popup_extracted",
                        serde_json::json!({
                            "request_id": &request_id_for_callback,
                            "error": format!("CoreWebView2() failed: {}", e),
                        }),
                    );
                    return;
                }
            };

            // IIFE try/catch so the expression always evaluates to a STRING
            // even if the inner JS throws (CSP block, undefined access on a
            // partially-loaded page, etc.). Without this wrap WebView2
            // catches the exception and returns the literal value `null`
            // with HRESULT=S_OK, which our outer Rust parse can't tell
            // apart from a legitimate JS null result — diagnostic info lost.
            // S145 follow-up: first runtime test on YarchePlus returned
            // exactly `null`; without the catch we couldn't see WHY.
            let js = "(function(){try{return JSON.stringify({\
                content_html: document.documentElement.outerHTML, \
                current_url: window.location.href\
            });}catch(e){return JSON.stringify({\
                error: 'js_exception', \
                name: String(e && e.name), \
                message: String(e && e.message), \
                stack: String(e && e.stack)\
            });}})()";

            let app_for_handler = app_for_callback.clone();
            let request_id_for_handler = request_id_for_callback.clone();
            let handler = ExecuteScriptCompletedHandler::create(Box::new(
                move |hr, json_encoded| {
                    // S145 diagnostic: log raw ExecuteScript output so any
                    // subsequent unexpected `null` / unwrap-shape result
                    // can be debugged from the Tauri dev console without
                    // a second round of patch+rebuild. Truncated to keep
                    // the log line readable when HTML is the payload.
                    let raw_preview: String = if json_encoded.len() > 300 {
                        format!("{}…({}b total)", &json_encoded[..300], json_encoded.len())
                    } else {
                        json_encoded.clone()
                    };
                    eprintln!(
                        "[web_auth] ExecuteScript callback for {}: hr={:?} raw={}",
                        request_id_for_handler, hr, raw_preview
                    );
                    let payload = if hr.is_ok() && !json_encoded.is_empty() {
                        // WebView2 hands us the JS expression value
                        // JSON-encoded — so for `JSON.stringify({...})`
                        // we receive a JSON-encoded string whose inner
                        // value is the JSON of our object. Unwrap once.
                        match serde_json::from_str::<String>(&json_encoded) {
                            Ok(inner_json) => match serde_json::from_str::<
                                serde_json::Value,
                            >(
                                &inner_json
                            ) {
                                Ok(obj) => {
                                    let mut out = serde_json::json!({
                                        "request_id": &request_id_for_handler,
                                    });
                                    if let serde_json::Value::Object(map) = obj {
                                        for (k, v) in map {
                                            out[k] = v;
                                        }
                                    }
                                    out
                                }
                                Err(e) => serde_json::json!({
                                    "request_id": &request_id_for_handler,
                                    "error": format!(
                                        "inner JSON parse failed: {}",
                                        e
                                    ),
                                }),
                            },
                            Err(e) => serde_json::json!({
                                "request_id": &request_id_for_handler,
                                "error": format!(
                                    "outer JSON parse failed: {} (raw={})",
                                    e,
                                    if json_encoded.len() > 200 {
                                        &json_encoded[..200]
                                    } else {
                                        &json_encoded
                                    }
                                ),
                            }),
                        }
                    } else {
                        serde_json::json!({
                            "request_id": &request_id_for_handler,
                            "error": format!(
                                "ExecuteScript failed: hr={:?}, empty={}",
                                hr,
                                json_encoded.is_empty()
                            ),
                        })
                    };
                    let _ = app_for_handler.emit("web_auth_popup_extracted", payload);
                    Ok(())
                },
            ));

            unsafe {
                if let Err(e) = core_webview2.ExecuteScript(&HSTRING::from(js), &handler)
                {
                    let _ = app_for_callback.emit(
                        "web_auth_popup_extracted",
                        serde_json::json!({
                            "request_id": &request_id_for_callback,
                            "error": format!("ExecuteScript call failed: {}", e),
                        }),
                    );
                }
            }
        })
        .map_err(|e| format!("with_webview failed: {}", e))?;

    Ok(())
}

/// T10: navigate the popup to a new URL within the same session.
/// Same-eTLD+1 enforcement is the Python side's responsibility (see
/// the matching tool in `browser.py`) — it MUST reject cross-origin
/// urls before reaching this command. JS-side `window.location.href`
/// assignment keeps the same WebView instance (same cookie jar, same
/// init_script — `__dpc_t9_emit_html__` survives navigation).
#[tauri::command]
pub async fn web_auth_popup_navigate(
    app: AppHandle,
    request_id: String,
    url: String,
) -> Result<(), String> {
    // Validate URL syntax — defence in depth alongside the Python
    // eTLD+1 check. Rejects javascript:, data:, malformed values.
    let parsed = Url::parse(&url)
        .map_err(|e| format!("invalid url '{}': {}", url, e))?;
    if !matches!(parsed.scheme(), "http" | "https") {
        return Err(format!(
            "invalid url scheme '{}' (only http/https allowed)",
            parsed.scheme()
        ));
    }
    let label = popup_label(&request_id);
    let popup = app
        .get_webview_window(&label)
        .ok_or_else(|| format!("popup window not found: request_id={}", request_id))?;
    // Use the parsed URL's serialised form to ensure JS-safe quoting;
    // Url::as_str() returns the canonical form which is safe inside
    // single quotes (no backslashes or quote characters by spec).
    let safe_url = parsed.as_str();
    popup
        .eval(&format!("window.location.href = '{}'", safe_url))
        .map_err(|e| format!("eval failed: {}", e))
}

/// T10: close the popup window programmatically.
/// Triggers the existing CloseRequested handler — `web_auth_popup_closing`
/// fires, the vault re-sync extracts cookies via the main window jar,
/// and the watchdog timer arms in case Path A applies. Same lifecycle
/// as a user clicking the X button.
#[tauri::command]
pub async fn web_auth_popup_close(
    app: AppHandle,
    request_id: String,
) -> Result<(), String> {
    let label = popup_label(&request_id);
    let popup = app
        .get_webview_window(&label)
        .ok_or_else(|| format!("popup window not found: request_id={}", request_id))?;
    popup.close().map_err(|e| format!("close failed: {}", e))
}

/// T10 Step 5 (S143): scroll the popup window programmatically.
/// Needed for sites with infinite-scroll pagination (YarchePlus orders
/// list — only the first ~4 entries fit the initial viewport, older
/// orders are JS-loaded on scroll-to-bottom). The agent calls this
/// before popup_extract_now to ensure the DOM contains the data it
/// expects to read.
///
/// `direction`:
///   - `"down"` / `"up"` — relative scroll by `distance_px` pixels
///   - `"top"` / `"bottom"` — absolute scroll to start/end of document
/// `distance_px` is ignored for absolute directions.
#[tauri::command]
pub async fn web_auth_popup_scroll(
    app: AppHandle,
    request_id: String,
    direction: String,
    distance_px: i64,
) -> Result<(), String> {
    let label = popup_label(&request_id);
    let popup = app
        .get_webview_window(&label)
        .ok_or_else(|| format!("popup window not found: request_id={}", request_id))?;
    let js = match direction.as_str() {
        "bottom" => "window.scrollTo(0, document.documentElement.scrollHeight)".to_string(),
        "top" => "window.scrollTo(0, 0)".to_string(),
        "down" => format!("window.scrollBy(0, {})", distance_px),
        "up" => format!("window.scrollBy(0, -{})", distance_px),
        _ => {
            return Err(format!(
                "invalid scroll direction '{}' (allowed: down, up, top, bottom)",
                direction
            ))
        }
    };
    popup
        .eval(&js)
        .map_err(|e| format!("eval failed: {}", e))
}
