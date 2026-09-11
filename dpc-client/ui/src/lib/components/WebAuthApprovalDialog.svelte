<script lang="ts">
    /**
     * WebAuthApprovalDialog — ADR-029 Task 008 approval UI.
     *
     * Shows pending headless web-auth requests as floating cards. The
     * decision being asked for is narrow and worth stating plainly: an
     * agent wants to use your stored cookies for a domain in a browser you
     * cannot see. The headed path (keep_open) needs no approval precisely
     * because the window is in front of you.
     *
     * Mirrors ShellApprovalDialog, the other gate of this shape.
     */
    import {
        pendingWebAuthApprovals,
        closeWebAuthApproval,
        dismissWebAuthApproval,
    } from "$lib/services/webAuthApproval";
    import { sendCommand } from "$lib/coreService";

    const MAX_VISIBLE_CARDS = 3;

    /** Requests whose answer is in flight — the card cannot be clicked twice,
     *  and does not disappear before the backend has taken the answer. */
    let answering: string[] = [];

    async function answer(requestId: string, command: string) {
        if (answering.includes(requestId)) return;
        answering = [...answering, requestId];
        try {
            const result = await sendCommand(command, { request_id: requestId });
            if (result === false) {
                closeWebAuthApproval(
                    requestId,
                    "The backend is not connected, so this answer never " +
                        "reached it. Nothing was shared.",
                );
            } else if (result?.status === "error") {
                // The wait was already over when the click landed: the backend
                // has forgotten the id and has told the agent no. Removing the
                // card here would read as «allowed».
                closeWebAuthApproval(
                    requestId,
                    "This request had already run out when the click " +
                        "arrived, so the backend refused it on your behalf. " +
                        "Nothing was shared.",
                );
            } else {
                dismissWebAuthApproval(requestId);
            }
        } catch (e) {
            closeWebAuthApproval(
                requestId,
                `This answer did not reach the backend (${e}). Nothing was shared.`,
            );
        } finally {
            answering = answering.filter((id) => id !== requestId);
        }
    }

    const approve = (requestId: string) =>
        answer(requestId, "web_auth_approve_headless");
    const reject = (requestId: string) =>
        answer(requestId, "web_auth_reject_headless");

    $: visibleApprovals = $pendingWebAuthApprovals.slice(0, MAX_VISIBLE_CARDS);
    $: hiddenCount = Math.max(0, $pendingWebAuthApprovals.length - MAX_VISIBLE_CARDS);
</script>

{#if visibleApprovals.length > 0}
    <div class="webauth-approval-overlay">
        {#if hiddenCount > 0}
            <div class="hidden-count">+{hiddenCount} more pending...</div>
        {/if}
        {#each visibleApprovals as request (request.request_id)}
            <div class="webauth-approval-card" class:is-closed={request.closed}>
                <div class="approval-header">
                    <span class="approval-icon">{request.closed ? "⌛" : "🔑"}</span>
                    <span class="approval-title">
                        {#if request.closed}
                            {request.kind === "login_window"
                                ? "Login confirmation expired"
                                : "Headless access request expired"}
                        {:else}
                            {request.kind === "login_window"
                                ? "Confirm Login"
                                : "Headless Login Access"}
                        {/if}
                    </span>
                </div>
                <!-- At full contrast and on its own line: approving a headless
                     login without knowing who asked and from where is approving
                     it blind. -->
                <div class="approval-origin-line">
                    <span class="origin-agent">{request.agent_name || request.agent_id}</span>
                    {#if request.conversation_title || request.conversation_id}
                        <span class="origin-sep">in</span>
                        <span class="origin-chat"
                            >{request.conversation_title || request.conversation_id}</span
                        >
                    {/if}
                </div>
                <div class="approval-domain">
                    <code>{request.domain}</code>
                </div>
                <div class="approval-url" title={request.url}>{request.url}</div>
                <div class="approval-reason">
                    {request.question ||
                        "Uses your saved cookies in a browser you will not see."}
                </div>
                <!-- Context, deliberately not a verdict: a site hands guest
                     cookies to any anonymous visitor, so what appeared is
                     something to look at, never a reason to click yes. -->
                {#if request.evidence?.new_cookie_names?.length}
                    <div class="approval-evidence">
                        New since the page first loaded:
                        <code>{request.evidence.new_cookie_names.join(", ")}</code>
                    </div>
                {/if}
                <!-- Once the backend has stopped waiting, the only honest
                     control left is one that closes the notice. An Allow
                     button here resolves to nothing and reads as a grant. -->
                {#if request.closed}
                    <div class="approval-closed">{request.closed}</div>
                    <div class="approval-actions">
                        <button
                            class="btn-dismiss"
                            on:click={() => dismissWebAuthApproval(request.request_id)}
                        >
                            Dismiss
                        </button>
                    </div>
                {:else}
                    <div class="approval-actions">
                        <button
                            class="btn-approve"
                            disabled={answering.includes(request.request_id)}
                            on:click={() => approve(request.request_id)}
                        >
                            {answering.includes(request.request_id)
                                ? "Sending..."
                                : "✓ Allow once"}
                        </button>
                        <button
                            class="btn-reject"
                            disabled={answering.includes(request.request_id)}
                            on:click={() => reject(request.request_id)}
                        >
                            ✕ Deny
                        </button>
                    </div>
                {/if}
            </div>
        {/each}
    </div>
{/if}

<style>
    .webauth-approval-overlay {
        position: fixed;
        bottom: 80px;
        right: 20px;
        z-index: 1000;
        display: flex;
        flex-direction: column;
        gap: 8px;
        max-width: 420px;
    }

    .hidden-count {
        text-align: center;
        font-size: 0.85em;
        opacity: 0.6;
        padding: 4px;
    }

    .webauth-approval-card {
        background: var(--bg-secondary, #1e1e2e);
        border: 1px solid var(--border-warning, #f9a825);
        border-radius: 8px;
        padding: 12px;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
    }

    .approval-header {
        display: flex;
        align-items: center;
        gap: 8px;
        margin-bottom: 8px;
    }

    .approval-icon {
        font-size: 1.2em;
    }

    .approval-title {
        font-weight: 600;
        color: var(--text-warning, #f9a825);
    }

    .approval-origin-line {
        display: flex;
        align-items: baseline;
        gap: 6px;
        flex-wrap: wrap;
        margin-bottom: 8px;
        font-size: 0.9em;
    }

    .origin-agent {
        font-weight: 600;
        color: var(--text-primary, #e6e6e6);
    }

    .origin-sep {
        opacity: 0.6;
    }

    .origin-chat {
        font-weight: 600;
        color: var(--text-info, #4fc3f7);
    }

    .approval-domain {
        background: var(--bg-tertiary, #11111b);
        padding: 8px;
        border-radius: 4px;
        margin-bottom: 6px;
        overflow-x: auto;
    }

    .approval-domain code {
        font-family: monospace;
        font-size: 0.9em;
        color: var(--text-primary, #cdd6f4);
    }

    .approval-url {
        font-size: 0.8em;
        opacity: 0.6;
        margin-bottom: 8px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }

    .approval-reason {
        font-size: 0.85em;
        opacity: 0.7;
        margin-bottom: 12px;
    }

    .approval-evidence {
        font-size: 0.8em;
        opacity: 0.7;
        margin-bottom: 12px;
        word-break: break-word;
    }

    .approval-actions {
        display: flex;
        gap: 8px;
    }

    .btn-approve, .btn-reject, .btn-dismiss {
        padding: 6px 12px;
        border: none;
        border-radius: 4px;
        cursor: pointer;
        font-size: 0.85em;
        font-weight: 500;
    }

    .btn-approve {
        background: var(--bg-success, #28a745);
        color: white;
    }

    .btn-reject {
        background: var(--bg-danger, #dc3545);
        color: white;
    }

    .btn-dismiss {
        background: var(--bg-tertiary, #11111b);
        color: var(--text-primary, #cdd6f4);
        border: 1px solid var(--border-color, #45475a);
    }

    .btn-approve:hover:not(:disabled),
    .btn-reject:hover:not(:disabled),
    .btn-dismiss:hover { opacity: 0.9; }

    .btn-approve:disabled,
    .btn-reject:disabled {
        opacity: 0.5;
        cursor: default;
    }

    /* An expired card is a notice, not a question: the warning border it
       wore while it wanted an answer would keep asking for one. */
    .webauth-approval-card.is-closed {
        border-color: var(--border-color, #45475a);
    }

    .webauth-approval-card.is-closed .approval-title {
        color: var(--text-secondary, #a6adc8);
    }

    .approval-closed {
        font-size: 0.85em;
        color: var(--text-secondary, #a6adc8);
        margin-bottom: 12px;
    }
</style>
