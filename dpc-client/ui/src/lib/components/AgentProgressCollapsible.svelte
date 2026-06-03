<script lang="ts">
    /**
     * AgentProgressCollapsible — collapsible tool call group for agent messages.
     * Renders from message.tool_calls (persisted) or live progress events.
     * Unified for 1:1 and group chats. Drift-style categories + human-readable labels.
     */
    import { getToolLabel, getToolCategory, getCategoryLabel, getToolArgPreview, getActionLabel, type ToolCategory } from '$lib/utils/toolDisplay';

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
        streamingText = '',
    }: {
        toolCalls: ToolCall[];
        agentName?: string;
        isLive?: boolean;
        currentTool?: string;
        currentRound?: number;
        streamingText?: string;
    } = $props();

    let expanded = $state(isLive);
    let expandedTools = $state<Set<number>>(new Set());

    function toggleTool(index: number) {
        const next = new Set(expandedTools);
        if (next.has(index)) next.delete(index);
        else next.add(index);
        expandedTools = next;
    }

    function truncateOutput(output: string, maxLen: number = 500): string {
        if (!output || output.length <= maxLen) return output || '';
        return output.slice(0, maxLen) + '...';
    }

    interface GroupedCategory {
        category: ToolCategory | 'other';
        label: string;
        tools: ToolCall[];
    }

    function groupByCategory(calls: ToolCall[]): GroupedCategory[] {
        const map = new Map<string, ToolCall[]>();
        for (const tc of calls) {
            const cat = getToolCategory(tc.tool);
            if (!map.has(cat)) map.set(cat, []);
            map.get(cat)!.push(tc);
        }
        return Array.from(map.entries()).map(([cat, tools]) => ({
            category: cat as ToolCategory | 'other',
            label: getCategoryLabel(cat as ToolCategory | 'other'),
            tools,
        }));
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
            <span class="action-count">
                {getActionLabel(toolCalls.length)}{isLive ? '...' : ''}
            </span>
            <span class="expand-icon">{expanded ? '▾' : '▸'}</span>
        </button>

        {#if expanded}
            <div class="tool-calls-list">
                {#each groupByCategory(toolCalls) as group}
                    <div class="category-label">{group.label}</div>
                    {#each group.tools as tc, i}
                        {@const globalIdx = toolCalls.indexOf(tc)}
                        <div class="tool-call-wrapper">
                            <button
                                class="tool-call-item"
                                class:error={tc.is_error}
                                class:has-output={!!tc.output}
                                onclick={() => tc.output && toggleTool(globalIdx)}
                            >
                                <span class="tool-status">{tc.is_error ? '✗' : '✓'}</span>
                                <span class="tool-name">{getToolLabel(tc.tool)}</span>
                                {#if getToolArgPreview(tc.tool, tc.input)}
                                    <span class="tool-arg"> — {getToolArgPreview(tc.tool, tc.input)}</span>
                                {/if}
                                {#if tc.output}
                                    <span class="tool-expand-icon">{expandedTools.has(globalIdx) ? '▾' : '▸'}</span>
                                {/if}
                            </button>
                            {#if expandedTools.has(globalIdx) && tc.output}
                                <pre class="tool-output">{truncateOutput(tc.output)}</pre>
                            {/if}
                        </div>
                    {/each}
                {/each}
                {#if isLive && currentTool}
                    <div class="tool-call-item current">
                        <span class="spinner-small"></span>
                        <span class="tool-name">{getToolLabel(currentTool)}</span>
                    </div>
                {/if}
            </div>
        {/if}
        {#if streamingText}
            <div class="streaming-inside-collapsible">
                <pre class="streaming-text">{streamingText}</pre>
            </div>
        {/if}
    </div>
{/if}

<style>
    .tool-calls-collapsible {
        margin: 6px 0;
        border-left: 3px solid #60a5fa;
        padding: 8px 10px;
        background: #1e293b;
        border-radius: 0 6px 6px 0;
    }

    .tool-calls-collapsible.live {
        border-left-color: #34d399;
    }

    .tool-calls-header {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        background: none;
        border: none;
        cursor: pointer;
        padding: 4px 6px;
        font-size: 0.9em;
        color: #f8fafc;
        border-radius: 3px;
    }

    .tool-calls-header:hover {
        background: rgba(255, 255, 255, 0.08);
    }

    .agent-label {
        font-weight: 700;
        color: #ffffff;
    }

    .action-count {
        color: #e2e8f0;
        font-weight: 500;
    }

    .expand-icon {
        font-size: 0.8em;
        color: #94a3b8;
    }

    .spinner {
        display: inline-block;
        width: 12px;
        height: 12px;
        border: 2px solid #60a5fa;
        border-top-color: transparent;
        border-radius: 50%;
        animation: spin 0.8s linear infinite;
    }

    .spinner-small {
        display: inline-block;
        width: 10px;
        height: 10px;
        border: 2px solid #60a5fa;
        border-top-color: transparent;
        border-radius: 50%;
        animation: spin 0.8s linear infinite;
    }

    @keyframes spin {
        to { transform: rotate(360deg); }
    }

    .tool-calls-list {
        margin: 4px 0 2px 0;
        padding: 0 0 0 4px;
        font-size: 0.85em;
    }

    .category-label {
        font-size: 0.75em;
        font-weight: 700;
        color: #94a3b8;
        letter-spacing: 0.08em;
        margin-top: 6px;
        margin-bottom: 2px;
        padding-left: 20px;
        text-transform: uppercase;
    }

    .category-label:first-child {
        margin-top: 2px;
    }

    .tool-call-item {
        padding: 2px 0;
        color: #e2e8f0;
        display: flex;
        align-items: center;
        gap: 6px;
        padding-left: 8px;
    }

    .tool-call-item.error {
        color: #f87171;
    }

    .tool-call-item.current {
        color: #60a5fa;
    }

    .tool-status {
        color: #4ade80;
        font-size: 0.85em;
        width: 12px;
        text-align: center;
        flex-shrink: 0;
    }

    .tool-call-item.error .tool-status {
        color: #f87171;
    }

    .tool-name {
        font-weight: 600;
        color: #ffffff;
    }

    .tool-arg {
        color: #cbd5e1;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        max-width: 400px;
    }

    .tool-call-wrapper {
        margin-bottom: 1px;
    }

    button.tool-call-item {
        width: 100%;
        text-align: left;
        background: none;
        border: none;
        cursor: default;
        font-family: inherit;
    }

    button.tool-call-item.has-output {
        cursor: pointer;
    }

    button.tool-call-item.has-output:hover {
        background: rgba(255, 255, 255, 0.04);
        border-radius: 3px;
    }

    .tool-expand-icon {
        margin-left: auto;
        font-size: 0.7em;
        color: #64748b;
    }

    .tool-output {
        margin: 2px 0 4px 20px;
        padding: 6px 8px;
        background: #0f172a;
        border-radius: 4px;
        font-size: 0.8em;
        color: #94a3b8;
        white-space: pre-wrap;
        word-wrap: break-word;
        max-height: 200px;
        overflow-y: auto;
        border-left: 2px solid #334155;
    }

    .streaming-inside-collapsible {
        margin-top: 8px;
        padding-top: 6px;
        border-top: 1px solid #334155;
    }

    .streaming-text {
        margin: 0;
        font-family: inherit;
        font-size: 0.9em;
        color: #e2e8f0;
        white-space: pre-wrap;
        word-wrap: break-word;
        line-height: 1.5;
    }
</style>
