<script>
    /**
     * ShellApprovalDialog — ADR-030 v2 approval UI.
     *
     * Shows pending shell command approval requests as floating cards.
     * User can Approve, Approve+Whitelist, or Reject each command.
     */
    import { pendingShellApprovals } from "$lib/services/shellApproval";
    import { sendCommand } from "$lib/coreService";

    async function approve(requestId, addToWhitelist = false) {
        await sendCommand("shell_approve_command", {
            request_id: requestId,
            add_to_whitelist: addToWhitelist,
        });
    }

    async function reject(requestId) {
        await sendCommand("shell_reject_command", {
            request_id: requestId,
        });
    }
</script>

{#if $pendingShellApprovals.length > 0}
    <div class="shell-approval-overlay">
        {#each $pendingShellApprovals as request (request.request_id)}
            <div class="shell-approval-card">
                <div class="approval-header">
                    <span class="approval-icon">⚡</span>
                    <span class="approval-title">Shell Command Approval</span>
                    <span class="approval-agent">{request.agent_name}</span>
                </div>
                <div class="approval-command">
                    <code>{request.command}</code>
                </div>
                <div class="approval-reason">
                    {request.reason}
                </div>
                <div class="approval-actions">
                    <button
                        class="btn-approve"
                        on:click={() => approve(request.request_id)}
                    >
                        ✓ Approve
                    </button>
                    <button
                        class="btn-whitelist"
                        on:click={() => approve(request.request_id, true)}
                    >
                        ✓ Approve + Whitelist
                    </button>
                    <button
                        class="btn-reject"
                        on:click={() => reject(request.request_id)}
                    >
                        ✕ Reject
                    </button>
                </div>
            </div>
        {/each}
    </div>
{/if}

<style>
    .shell-approval-overlay {
        position: fixed;
        bottom: 80px;
        right: 20px;
        z-index: 1000;
        display: flex;
        flex-direction: column;
        gap: 8px;
        max-width: 420px;
    }

    .shell-approval-card {
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

    .approval-command {
        background: var(--bg-tertiary, #11111b);
        padding: 8px;
        border-radius: 4px;
        margin-bottom: 8px;
        overflow-x: auto;
    }

    .approval-command code {
        font-family: monospace;
        font-size: 0.9em;
        color: var(--text-primary, #cdd6f4);
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

    .btn-approve, .btn-whitelist, .btn-reject {
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

    .btn-whitelist {
        background: var(--bg-info, #17a2b8);
        color: white;
    }

    .btn-reject {
        background: var(--bg-danger, #dc3545);
        color: white;
    }

    .btn-approve:hover { opacity: 0.9; }
    .btn-whitelist:hover { opacity: 0.9; }
    .btn-reject:hover { opacity: 0.9; }
</style>
