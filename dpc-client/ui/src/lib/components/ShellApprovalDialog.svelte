<script lang="ts">
    /**
     * ShellApprovalDialog — ADR-030 v2 approval UI.
     *
     * Shows pending shell command approval requests as floating cards.
     * User can Approve, Approve+Whitelist, or Reject each command.
     * Also shows execution results briefly after approval/rejection.
     */
    import { pendingShellApprovals, shellExecutionResults } from "$lib/services/shellApproval";
    import { sendCommand } from "$lib/coreService";

    const MAX_VISIBLE_CARDS = 3;
    const RESULT_DISPLAY_MS = 8000;

    async function approve(requestId: string, addToWhitelist = false) {
        await sendCommand("shell_approve_command", {
            request_id: requestId,
            add_to_whitelist: addToWhitelist,
        });
    }

    async function reject(requestId: string) {
        await sendCommand("shell_reject_command", {
            request_id: requestId,
        });
    }

    function dismissResult(requestId: string) {
        shellExecutionResults.update(list =>
            list.filter(r => r.request_id !== requestId)
        );
    }

    $: visibleApprovals = $pendingShellApprovals.slice(0, MAX_VISIBLE_CARDS);
    $: hiddenCount = Math.max(0, $pendingShellApprovals.length - MAX_VISIBLE_CARDS);

    $: {
        for (const result of $shellExecutionResults) {
            const rid = result.request_id;
            setTimeout(() => dismissResult(rid), RESULT_DISPLAY_MS);
        }
    }
</script>

{#if $shellExecutionResults.length > 0}
    <div class="shell-result-overlay">
        {#each $shellExecutionResults as result (result.request_id)}
            <div class="shell-result-card" class:rejected={result.rejected}>
                <div class="result-header">
                    <span class="result-icon">{result.rejected ? '❌' : '✓'}</span>
                    <span class="result-title">{result.rejected ? 'Rejected' : 'Executed'}</span>
                    <span class="result-agent">{result.agent_name}</span>
                    <button class="btn-dismiss" on:click={() => dismissResult(result.request_id)}>×</button>
                </div>
                <div class="result-command"><code>{result.command}</code></div>
                {#if !result.rejected && result.output}
                    <div class="result-output"><pre>{result.output.slice(0, 500)}</pre></div>
                {/if}
            </div>
        {/each}
    </div>
{/if}

{#if visibleApprovals.length > 0}
    <div class="shell-approval-overlay">
        {#if hiddenCount > 0}
            <div class="hidden-count">+{hiddenCount} more pending...</div>
        {/if}
        {#each visibleApprovals as request (request.request_id)}
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
    .shell-result-overlay {
        position: fixed;
        top: 80px;
        right: 20px;
        z-index: 1001;
        display: flex;
        flex-direction: column;
        gap: 8px;
        max-width: 420px;
    }

    .shell-result-card {
        background: var(--bg-secondary, #1e1e2e);
        border: 1px solid var(--border-success, #28a745);
        border-radius: 8px;
        padding: 10px;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
    }

    .shell-result-card.rejected {
        border-color: var(--border-danger, #dc3545);
    }

    .result-header {
        display: flex;
        align-items: center;
        gap: 8px;
        margin-bottom: 6px;
    }

    .result-icon { font-size: 1.1em; }
    .result-title { font-weight: 600; font-size: 0.9em; }
    .result-agent { margin-left: auto; font-size: 0.8em; opacity: 0.7; }

    .btn-dismiss {
        background: none;
        border: none;
        color: var(--text-primary, #cdd6f4);
        cursor: pointer;
        font-size: 1.1em;
        opacity: 0.5;
        padding: 0 4px;
    }
    .btn-dismiss:hover { opacity: 1; }

    .result-command {
        background: var(--bg-tertiary, #11111b);
        padding: 4px 8px;
        border-radius: 4px;
        margin-bottom: 4px;
    }
    .result-command code { font-size: 0.85em; }

    .result-output {
        background: var(--bg-tertiary, #11111b);
        padding: 6px 8px;
        border-radius: 4px;
        max-height: 150px;
        overflow-y: auto;
    }
    .result-output pre {
        margin: 0;
        font-size: 0.8em;
        white-space: pre-wrap;
        word-break: break-all;
    }

    .hidden-count {
        text-align: center;
        font-size: 0.85em;
        opacity: 0.6;
        padding: 4px;
    }

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
