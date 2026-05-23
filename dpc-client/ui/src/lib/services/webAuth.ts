// src/lib/services/webAuth.ts
// ADR-028 web-auth UI stores.
//
// T9 popup-fallback (anti-bot challenge / JS-rendered content):
//   - backend broadcasts `web_auth_popup_request` over the local WS when
//     an agent's authenticated browse hits a challenge page; payload
//     {request_id, agent_id, domain, url, reason}
//   - the UI surfaces a popup-request prompt; user clicks "Open" → Tauri
//     opens a WebView, user solves challenge / waits for JS render, closes
//   - Rust popup emits Tauri event `web_auth_popup_extracted` with
//     {request_id, content_html, current_url} (or {request_id, error})
//   - coreService.ts forwards that as WS command `web_auth_popup_complete`
//     to the Python Step-3 handler which resolves the agent's pending
//     Future and the agent receives the markdown
//
// This store holds the CURRENT outstanding popup-request, if any. Phase 1
// supports one popup at a time (sequential per Q3 decision). When the
// request is satisfied (success, error, timeout, cancel) the store is
// cleared to null.

import { writable } from 'svelte/store';

export interface WebAuthPopupRequest {
    request_id: string;
    agent_id: string;
    domain: string;
    url: string;
    reason: string;
    // T10 Q4 (S143): true when the agent is opening a multi-page
    // session via browse_page(keep_open=true). Drives the popup
    // window title ("Agent active — close to abort") and signals
    // to the user that closing aborts the agent workflow.
    keep_open?: boolean;
    // S144 T10 fix: post-page-load settle delay before the frontend
    // auto-triggers `web_auth_popup_extract_now` on the keep_open=true
    // path (replaces the broken "wait for user to close popup" Path A
    // semantics on T10 sessions). Ignored when keep_open is false.
    wait_seconds?: number;
}

export const webAuthPopupRequest = writable<WebAuthPopupRequest | null>(null);
