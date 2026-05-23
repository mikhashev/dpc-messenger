<script lang="ts">
    /**
     * WebAuthPopupRequest — ADR-028 T9 UI surface.
     *
     * Subscribes to the `webAuthPopupRequest` store; when a backend
     * agent emits a popup-request, this component renders a modal
     * prompt with an "Open {domain}" button. Clicking it invokes the
     * Tauri `web_auth_open_popup_for_content` command, which spawns
     * a WebView popup at the requested URL using the shared WebView2
     * cookie jar (Q1 (b) — relies on prior T8 login session). The
     * user solves the challenge / waits for JS render / navigates as
     * needed, then closes the popup; the Rust side emits
     * `web_auth_popup_extracted` which coreService.ts forwards via
     * WebSocket to the Step-3 Python handler.
     *
     * The store is cleared as soon as the popup is launched OR when
     * the user cancels — there is one outstanding request at a time
     * (Q3 sequential decision). If the user cancels, we forward an
     * explicit `error: "user_cancelled"` so the agent's Future
     * resolves with AuthRequiredError instead of timing out at 5min.
     */
    import { webAuthPopupRequest, sendCommand } from '$lib/coreService';

    let opening = $state(false);
    let openError = $state<string | null>(null);

    async function handleOpen() {
        const req = $webAuthPopupRequest;
        if (!req) return;
        opening = true;
        openError = null;
        try {
            const { invoke } = await import('@tauri-apps/api/core');
            await invoke('web_auth_open_popup_for_content', {
                url: req.url,
                requestId: req.request_id,
            });
            // Tauri popup is now visible to the user. The store stays set
            // until the extracted-event listener in coreService.ts clears it
            // (success path) — keeping the "Открыто, закройте окно" overlay.
        } catch (e: any) {
            openError = e?.message ?? String(e);
            opening = false;
        }
    }

    function handleCancel() {
        const req = $webAuthPopupRequest;
        if (!req) return;
        // Tell the backend handler to resolve the pending Future with an
        // explicit cancellation error rather than waiting out the 5-min
        // timeout — the agent gets a clean AuthRequiredError immediately.
        sendCommand('web_auth_popup_complete', {
            request_id: req.request_id,
            error: 'user_cancelled',
        });
        webAuthPopupRequest.set(null);
        opening = false;
        openError = null;
    }
</script>

{#if $webAuthPopupRequest}
    <div class="popup-backdrop" role="dialog" aria-modal="true" aria-labelledby="webauth-popup-title">
        <div class="popup-card">
            <h3 id="webauth-popup-title">🌐 Agent web verification</h3>
            <p class="popup-detail">
                Agent <strong>{$webAuthPopupRequest.agent_id}</strong> needs your help opening
                <strong>{$webAuthPopupRequest.domain}</strong> in a browser window
                to {$webAuthPopupRequest.reason === 'anti_bot_challenge'
                    ? 'complete an anti-bot challenge'
                    : 'render JS-loaded content'}.
            </p>
            <p class="popup-url">URL: <code>{$webAuthPopupRequest.url}</code></p>

            {#if openError}
                <p class="popup-error">Failed to open: {openError}</p>
            {/if}

            {#if !opening}
                <div class="popup-actions">
                    <button class="popup-btn-primary" onclick={handleOpen}>
                        Open {$webAuthPopupRequest.domain}
                    </button>
                    <button class="popup-btn-secondary" onclick={handleCancel}>
                        Cancel
                    </button>
                </div>
            {:else}
                <p class="popup-status">
                    Browser window opened — close it when you are done.
                </p>
                <div class="popup-actions">
                    <button class="popup-btn-secondary" onclick={handleCancel}>
                        Cancel request
                    </button>
                </div>
            {/if}
        </div>
    </div>
{/if}

<style>
    .popup-backdrop {
        position: fixed;
        inset: 0;
        background: rgba(0, 0, 0, 0.4);
        display: flex;
        align-items: center;
        justify-content: center;
        z-index: 1000;
    }

    .popup-card {
        background: var(--bg-color, #fff);
        color: var(--text-color, #000);
        border-radius: 8px;
        padding: 24px;
        max-width: 540px;
        width: calc(100% - 48px);
        box-shadow: 0 10px 40px rgba(0, 0, 0, 0.3);
    }

    .popup-card h3 {
        margin: 0 0 12px 0;
        font-size: 1.1em;
    }

    .popup-detail {
        margin: 0 0 12px 0;
        line-height: 1.5;
    }

    .popup-url {
        margin: 0 0 16px 0;
        font-size: 0.9em;
        opacity: 0.8;
        word-break: break-all;
    }

    .popup-url code {
        background: var(--code-bg, rgba(0, 0, 0, 0.05));
        padding: 2px 6px;
        border-radius: 3px;
    }

    .popup-error {
        color: #c53030;
        background: rgba(197, 48, 48, 0.1);
        padding: 8px 12px;
        border-radius: 4px;
        margin: 0 0 12px 0;
    }

    .popup-status {
        color: #2c5282;
        background: rgba(44, 82, 130, 0.1);
        padding: 8px 12px;
        border-radius: 4px;
        margin: 0 0 12px 0;
    }

    .popup-actions {
        display: flex;
        gap: 8px;
        justify-content: flex-end;
    }

    .popup-btn-primary {
        background: #3182ce;
        color: white;
        border: none;
        padding: 8px 16px;
        border-radius: 4px;
        cursor: pointer;
        font-size: 0.95em;
    }

    .popup-btn-primary:hover {
        background: #2c5282;
    }

    .popup-btn-secondary {
        background: transparent;
        color: var(--text-color, #000);
        border: 1px solid var(--border-color, #ccc);
        padding: 8px 16px;
        border-radius: 4px;
        cursor: pointer;
        font-size: 0.95em;
    }

    .popup-btn-secondary:hover {
        background: rgba(0, 0, 0, 0.05);
    }
</style>
