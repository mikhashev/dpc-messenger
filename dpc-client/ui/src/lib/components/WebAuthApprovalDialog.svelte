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
    import { pendingWebAuthApprovals } from "$lib/services/webAuthApproval";
    import { sendCommand } from "$lib/coreService";

    const MAX_VISIBLE_CARDS = 3;

    async function approve(requestId: string) {
        pendingWebAuthApprovals.update(list => list.filter(r => r.request_id !== requestId));
        await sendCommand("web_auth_approve_headless", { request_id: requestId });
    }

    async function reject(requestId: string) {
        pendingWebAuthApprovals.update(list => list.filter(r => r.request_id !== requestId));
        await sendCommand("web_auth_reject_headless", { request_id: requestId });
    }

    $: visibleApprovals = $pendingWebAuthApprovals.slice(0, MAX_VISIBLE_CARDS);
    $: hiddenCount = Math.max(0, $pendingWebAuthApprovals.length - MAX_VISIBLE_CARDS);
</script>

{#if visibleApprovals.length > 0}
    <div class="webauth-approval-overlay">
        {#if hiddenCount > 0}
            <div class="hidden-count">+{hiddenCount} more pending...</div>
        {/if}
        {#each visibleApprovals as request (request.request_id)}
            <div class="webauth-approval-card">
                <div class="approval-header">
                    <span class="approval-icon">🔑</span>
                    <span class="approval-title">Headless Login Access</span>
                    <span class="approval-agent">{request.agent_id}</span>
                </div>
                <div class="approval-domain">
                    <code>{request.domain}</code>
                </div>
                <div class="approval-url" title={request.url}>{request.url}</div>
                <div class="approval-reason">
                    Uses your saved cookies in a browser you will not see.
                    Expires in 2 minutes.
                </div>
                <div class="approval-actions">
                    <button class="btn-approve" on:click={() => approve(request.request_id)}>
                        ✓ Allow once
                    </button>
                    <button class="btn-reject" on:click={() => reject(request.request_id)}>
                        ✕ Deny
                    </button>
                </div>
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

    .approval-agent {
        margin-left: auto;
        font-size: 0.85em;
        opacity: 0.7;
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

    .approval-actions {
        display: flex;
        gap: 8px;
    }

    .btn-approve, .btn-reject {
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

    .btn-approve:hover { opacity: 0.9; }
    .btn-reject:hover { opacity: 0.9; }
</style>
