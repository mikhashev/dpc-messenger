<script lang="ts">
    /**
     * AgentProgressCollapsible — collapsible tool call group for agent messages.
     * Renders from message.tool_calls (persisted) or live progress events.
     * Unified for 1:1 and group chats. No emojis (Mike requirement).
     */

    interface ToolCall {
        tool: string;
        input: string;
        output: string;
        is_error: boolean;
        duration_ms: number;
        round: number;
    }

    let {
        toolCalls = [],
        agentName = '',
        isLive = false,
        currentTool = '',
        currentRound = 0,
    }: {
        toolCalls: ToolCall[];
        agentName?: string;
        isLive?: boolean;
        currentTool?: string;
        currentRound?: number;
    } = $props();

    let expanded = $state(false);

    function formatToolLabel(tool: string): string {
        return tool.replace(/_/g, ' ');
    }

    function formatInput(input: string): string {
        if (!input) return '';
        const short = input.length > 120 ? input.slice(0, 120) + '...' : input;
        return short;
    }
</script>

{#if toolCalls.length > 0 || isLive}
    <div class="tool-calls-collapsible" class:live={isLive}>
        <button
            class="tool-calls-header"
            onclick={() => expanded = !expanded}
            aria-expanded={expanded}
        >
            {#if isLive}
                <span class="spinner"></span>
            {/if}
            {#if agentName}
                <span class="agent-label">{agentName}</span>
            {/if}
            {#if currentRound > 0}
                <span class="round-label">Round {currentRound}</span>
            {/if}
            <span class="action-count">
                {toolCalls.length} {toolCalls.length === 1 ? 'action' : 'actions'}{isLive ? '...' : ''}
            </span>
            <span class="expand-icon">{expanded ? '▾' : '▸'}</span>
        </button>

        {#if expanded}
            <ul class="tool-calls-list">
                {#each toolCalls as tc}
                    <li class="tool-call-item" class:error={tc.is_error}>
                        <span class="tool-name">{formatToolLabel(tc.tool)}</span>
                        {#if tc.input}
                            <span class="tool-input"> -- {formatInput(tc.input)}</span>
                        {/if}
                        {#if tc.duration_ms > 0}
                            <span class="tool-duration">{tc.duration_ms}ms</span>
                        {/if}
                    </li>
                {/each}
                {#if isLive && currentTool}
                    <li class="tool-call-item current">
                        <span class="spinner-small"></span>
                        <span class="tool-name">{formatToolLabel(currentTool)}</span>
                    </li>
                {/if}
            </ul>
        {/if}
    </div>
{/if}

<style>
    .tool-calls-collapsible {
        margin: 4px 0;
        border-left: 2px solid var(--border-muted, #374151);
        padding-left: 8px;
    }

    .tool-calls-collapsible.live {
        border-left-color: var(--border-info, #3b82f6);
    }

    .tool-calls-header {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        background: none;
        border: none;
        cursor: pointer;
        padding: 2px 4px;
        font-size: 0.82em;
        color: var(--text-secondary, #9ca3af);
        border-radius: 3px;
    }

    .tool-calls-header:hover {
        background: var(--bg-hover, rgba(255,255,255,0.05));
    }

    .agent-label {
        font-weight: 600;
        color: var(--text-primary, #e5e7eb);
    }

    .round-label {
        opacity: 0.7;
    }

    .action-count {
        opacity: 0.8;
    }

    .expand-icon {
        font-size: 0.75em;
        opacity: 0.5;
    }

    .spinner {
        display: inline-block;
        width: 10px;
        height: 10px;
        border: 2px solid var(--border-info, #3b82f6);
        border-top-color: transparent;
        border-radius: 50%;
        animation: spin 0.8s linear infinite;
    }

    .spinner-small {
        display: inline-block;
        width: 8px;
        height: 8px;
        border: 1.5px solid var(--border-info, #3b82f6);
        border-top-color: transparent;
        border-radius: 50%;
        animation: spin 0.8s linear infinite;
    }

    @keyframes spin {
        to { transform: rotate(360deg); }
    }

    .tool-calls-list {
        list-style: none;
        margin: 2px 0 0 0;
        padding: 0 0 0 12px;
        font-size: 0.78em;
    }

    .tool-call-item {
        padding: 1px 0;
        color: var(--text-secondary, #9ca3af);
        display: flex;
        align-items: center;
        gap: 4px;
    }

    .tool-call-item.error {
        color: var(--text-danger, #ef4444);
    }

    .tool-call-item.current {
        color: var(--text-info, #3b82f6);
    }

    .tool-name {
        font-weight: 500;
        color: var(--text-primary, #d1d5db);
    }

    .tool-input {
        opacity: 0.7;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        max-width: 400px;
    }

    .tool-duration {
        margin-left: auto;
        opacity: 0.5;
        font-size: 0.9em;
    }
</style>
