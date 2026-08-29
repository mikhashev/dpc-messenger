<script lang="ts">
    /**
     * AgentProgressCollapsible — collapsible tool call group for agent messages.
     * Renders from message.tool_calls (persisted) or live progress events.
     * Unified for 1:1 and group chats. Drift-style categories + human-readable labels.
     */
    import { untrack } from 'svelte';
    import { getToolLabel, getToolArgPreview } from '$lib/utils/toolDisplay';
    import { occupancyFromSpeed, occupancyLabel, occupancyTitle } from '$lib/utils/contextOccupancy';
    import { clampDetail, formatToolInput } from '$lib/utils/toolDetail';
    import { sendCommand } from '$lib/coreService';

    interface ToolCall {
        tool: string;
        input: string;
        output: string;
        is_error: boolean;
        duration_ms: number;
        round: number;
        round_text?: string;
    }

    let {
        toolCalls = [],
        agentName = '',
        isLive = false,
        currentTool = '',
        currentRound = 0,
        streamingText = '',
        conversationId = '',
        agentId = '',
        speed = null,
    }: {
        toolCalls: ToolCall[];
        agentName?: string;
        isLive?: boolean;
        currentTool?: string;
        currentRound?: number;
        streamingText?: string;
        conversationId?: string;
        agentId?: string;
        speed?: Record<string, any> | null;
    } = $props();

    // Open while the run is live, and the reader owns it from then on — the run
    // ending must not fold a block someone is reading. untrack states that the
    // prop is read once, which is what the warning asks for.
    let expanded = $state(untrack(() => isLive));
    let expandedTools = $state<Set<number>>(new Set());
    // Distinct round count for the header summary ("N rounds · M actions").
    let roundCount = $derived(new Set(toolCalls.map((tc) => tc.round)).size);

    function toggleTool(index: number) {
        const next = new Set(expandedTools);
        if (next.has(index)) next.delete(index);
        else next.add(index);
        expandedTools = next;
    }

    // Per-round nested collapsible — rounds expanded by default (transparency),
    // click a round header to collapse it.
    let collapsedRounds = $state<Set<number>>(new Set());

    function toggleRound(round: number) {
        const next = new Set(collapsedRounds);
        if (next.has(round)) next.delete(round);
        else next.add(round);
        collapsedRounds = next;
    }

    // Stop used to be fire-and-forget: the envelope said OK while the payload
    // could say error/no_active_loop, and a miss looked identical to a hit.
    // The handler answers {status: stopped | no_active_loop | error}.
    let stopState = $state<'idle' | 'stopping' | 'miss' | 'error'>('idle');
    let stopNote = $state('');
    // A new round invalidates a stale 'miss' from the previous one: the loop
    // is demonstrably running again, so the button returns to a live Stop.
    let lastRoundSeen: number | undefined;
    $effect(() => {
        const r = currentRound;
        if (lastRoundSeen === undefined) {
            lastRoundSeen = r;
            return;
        }
        if (r !== lastRoundSeen) {
            lastRoundSeen = r;
            if (stopState === 'miss' || stopState === 'error') {
                stopState = 'idle';
                stopNote = '';
            }
        }
    });
    async function requestStop(e: Event) {
        e.stopPropagation();
        if (stopState === 'stopping') return;
        stopState = 'stopping';
        try {
            const res: any = await sendCommand('interrupt_agent', {
                agent_id: agentId, conversation_id: conversationId,
            });
            const st = res?.status || res?.payload?.status;
            if (st === 'stopped') {
                stopNote = 'stopping…';
            } else if (st === 'no_active_loop') {
                stopState = 'miss';
                stopNote = 'no active loop';
            } else {
                stopState = 'error';
                stopNote = res?.message || 'error';
            }
        } catch (err: any) {
            stopState = 'error';
            stopNote = err?.message || 'send failed';
        }
    }

    // The occupancy half of the strip rides the same payload as the speed
    // (loop.py: round_progress_payload) but reads differently — the denominator
    // is the raw window and the reserve is a colour, not a subtraction. That
    // reasoning lives in contextOccupancy.ts, where it can be tested.
    let ctx = $derived(occupancyFromSpeed(speed));
    let ctxTitle = $derived(
        ctx ? occupancyTitle(ctx, { round: speed?.round, median: !!speed?.median }) : '',
    );

    // One-line result preview shown in the collapsed tool row; the expanded
    // block below carries the arguments and the output.
    function outputPreview(output: string, maxLen: number = 100): string {
        if (!output) return '';
        const oneLine = output.replace(/\s+/g, ' ').trim();
        return oneLine.length > maxLen ? oneLine.slice(0, maxLen) + '...' : oneLine;
    }

    interface GroupedRound {
        round: number;
        text: string;
        tools: ToolCall[];
    }

    // Group by LLM round — mirrors loop.py execution: each round is one LLM call
    // that may issue several tool calls. round + round_text are stamped on each
    // accumulated tool call; round_text is the agent's reasoning for that round.
    function groupByRound(calls: ToolCall[]): GroupedRound[] {
        const map = new Map<number, ToolCall[]>();
        for (const tc of calls) {
            const r = tc.round ?? 0;
            if (!map.has(r)) map.set(r, []);
            map.get(r)!.push(tc);
        }
        return Array.from(map.entries())
            .sort((a, b) => a[0] - b[0])
            .map(([round, tools]) => ({ round, text: tools[0]?.round_text || '', tools }));
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
                {roundCount} {roundCount === 1 ? 'round' : 'rounds'} · {toolCalls.length} {toolCalls.length === 1 ? 'action' : 'actions'}{isLive ? '...' : ''}
            </span>
            {#if speed && (isLive || speed.median)}
                <span class="speed-counter" title={speed.elapsed_s ? `model ${speed.model || speed.alias || ''}, round ${speed.round ?? '?'}, ${speed.prompt_tokens ?? 0} in / ${speed.completion_tokens ?? 0} out, ${speed.elapsed_s}s` : ''}>
                    {#if speed.prefill_tok_s}
                        <span class="speed-part">{speed.median ? 'median ' : ''}prefill: {speed.prefill_tok_s} tok/s | decode: {speed.decode_tok_s} tok/s</span>
                    {:else if speed.total_tok_s}
                        <span class="speed-part">{speed.median ? 'median ' : ''}{speed.total_tok_s} tok/s</span>
                    {/if}
                    {#if ctx}
                        <span
                            class="speed-part context"
                            class:warn={ctx.warn}
                            class:blocked={ctx.blocked}
                            title={ctxTitle}
                        >{occupancyLabel(ctx)}</span>
                    {/if}
                    {#if speed.model || speed.alias}
                        <span class="speed-model">{speed.model || speed.alias}</span>
                    {/if}
                </span>
            {/if}
            <span class="expand-icon">{expanded ? '▾' : '▸'}</span>
        </button>
        <!-- Expanded, the sticky footer Stop owns the control; the header one
             serves the collapsed state, where the block is short. -->
        {#if isLive && conversationId && !expanded}
            <button
                class="stop-btn"
                title={stopNote || 'Stop agent'}
                disabled={stopState === 'stopping'}
                onclick={requestStop}
            >{stopState === 'stopping' ? 'Stopping…' : stopState === 'miss' ? 'No active loop' : 'Stop'}</button>
        {/if}

        {#if expanded}
            <div class="tool-calls-list">
                {#each groupByRound(toolCalls) as group}
                    {@const roundCollapsed = collapsedRounds.has(group.round)}
                    <div class="round-group">
                        <button
                            class="round-header"
                            onclick={() => toggleRound(group.round)}
                            aria-expanded={!roundCollapsed}
                        >
                            <span class="round-expand-icon">{roundCollapsed ? '▸' : '▾'}</span>
                            <span class="round-label">Round {group.round}</span>
                            <span class="round-count">{group.tools.length} {group.tools.length === 1 ? 'action' : 'actions'}</span>
                        </button>
                        {#if !roundCollapsed}
                            {#if group.text}
                                <div class="round-text">
                                    <span class="round-text-label">Thinking</span>
                                    <span class="round-text-body">{group.text}</span>
                                </div>
                            {/if}
                            {#each group.tools as tc, i}
                                {@const globalIdx = toolCalls.indexOf(tc)}
                                {@const hasDetail = !!(tc.input || tc.output)}
                                <div class="tool-call-wrapper">
                                    <button
                                        class="tool-call-item"
                                        class:error={tc.is_error}
                                        class:has-output={hasDetail}
                                        onclick={() => hasDetail && toggleTool(globalIdx)}
                                    >
                                        <span class="tool-status">{tc.is_error ? '✗' : '✓'}</span>
                                        <span class="tool-name">{getToolLabel(tc.tool)}</span>
                                        {#if getToolArgPreview(tc.tool, tc.input)}
                                            <span class="tool-arg"> — {getToolArgPreview(tc.tool, tc.input)}</span>
                                        {/if}
                                        {#if hasDetail}
                                            <span class="tool-expand-icon">{expandedTools.has(globalIdx) ? '▾' : '▸'}</span>
                                        {/if}
                                    </button>
                                    {#if tc.output && !expandedTools.has(globalIdx)}
                                        <div class="tool-output-preview">{outputPreview(tc.output)}</div>
                                    {/if}
                                    {#if expandedTools.has(globalIdx)}
                                        {#if tc.input}
                                            <pre class="tool-input">{clampDetail(formatToolInput(tc.input))}</pre>
                                        {/if}
                                        {#if tc.output}
                                            <pre class="tool-output">{clampDetail(tc.output)}</pre>
                                        {/if}
                                    {/if}
                                </div>
                            {/each}
                        {/if}
                    </div>
                {/each}
                {#if isLive && currentTool}
                    <div class="tool-call-item current">
                        <span class="spinner-small"></span>
                        <span class="tool-name">{getToolLabel(currentTool)}</span>
                    </div>
                {/if}
                {#if isLive && conversationId}
                    <!-- The run grows downward and autoscroll keeps the bottom in
                         view, so the one control that ends the run must live at the
                         bottom too — sticky, so it survives the user scrolling up
                         mid-run. The top Stop stays for the collapsed state. -->
                    <div class="stop-footer">
                        <button
                            class="stop-btn"
                            title={stopNote || 'Stop agent'}
                            disabled={stopState === 'stopping'}
                            onclick={requestStop}
                        >{stopState === 'stopping' ? 'Stopping…' : stopState === 'miss' ? 'No active loop' : 'Stop'}</button>
                        {#if stopState === 'error'}
                            <span class="stop-note error">{stopNote}</span>
                        {/if}
                    </div>
                {/if}
            </div>
        {/if}
        <!-- During live with per-round structure, narration lives inside each round —
             suppress the bottom block to avoid duplicating it (and the old raw leak).
             The final answer appears as msg.text when the message finalizes. -->
        {#if streamingText && !(isLive && toolCalls.length > 0)}
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

    .stop-footer {
        position: sticky;
        bottom: 0;
        display: flex;
        align-items: center;
        gap: 10px;
        justify-content: center;
        padding: 10px 0 8px;
        background: linear-gradient(to top, var(--bg-primary, #111) 75%, transparent);
    }
    .stop-footer .stop-btn {
        font-size: 1em;
        padding: 8px 28px;
        min-width: 120px;
    }
    .stop-note.error {
        color: #f87171;
        font-size: 0.85em;
    }
    .speed-counter {
        display: inline-flex;
        gap: 8px;
        align-items: baseline;
        margin-left: 8px;
        font-size: 0.85em;
        opacity: 0.85;
    }
    .speed-part {
        font-variant-numeric: tabular-nums;
        color: var(--text-secondary, #9ca3af);
        white-space: nowrap;
    }
    /* Amber at the backend's own >80% warning line, red once the round guard
       would refuse the next call — the colour is the reserve made visible. */
    .speed-part.context.warn {
        color: var(--warning, #f59e0b);
    }
    .speed-part.context.blocked {
        color: var(--danger, #ef4444);
        font-weight: 600;
    }
    .speed-model {
        font-size: 0.9em;
        color: var(--text-tertiary, #6b7280);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        max-width: 160px;
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

    .stop-btn {
        background: #dc2626;
        color: white;
        border: none;
        border-radius: 4px;
        padding: 2px 8px;
        font-size: 0.75rem;
        cursor: pointer;
        margin-left: 8px;
    }
    .stop-btn:hover { background: #b91c1c; }

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

    .round-group {
        margin-top: 4px;
    }

    .round-header {
        display: flex;
        align-items: center;
        gap: 6px;
        width: 100%;
        text-align: left;
        background: none;
        border: none;
        cursor: pointer;
        padding: 3px 4px;
        border-radius: 3px;
    }

    .round-header:hover {
        background: rgba(255, 255, 255, 0.06);
    }

    .round-expand-icon {
        font-size: 0.7em;
        color: #64748b;
        width: 10px;
        flex-shrink: 0;
    }

    .round-label {
        font-size: 0.75em;
        font-weight: 700;
        color: #cbd5e1;
        letter-spacing: 0.08em;
    }

    .round-count {
        font-size: 0.7em;
        color: #94a3b8;
    }

    .round-text {
        display: flex;
        flex-direction: column;
        gap: 2px;
        margin: 3px 0 6px 20px;
        padding-left: 8px;
        border-left: 2px solid #475569;
    }

    .round-text-label {
        font-size: 0.7em;
        font-weight: 700;
        color: #94a3b8;
        letter-spacing: 0.06em;
        text-transform: uppercase;
    }

    .round-text-body {
        font-size: 0.85em;
        color: #e2e8f0;
        white-space: pre-wrap;
        word-wrap: break-word;
        line-height: 1.45;
    }

    .tool-output-preview {
        margin: 0 0 3px 28px;
        font-size: 0.78em;
        color: #64748b;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        max-width: 600px;
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

    /* The arguments, above the output and marked apart from it: the same block
       now answers both "what did it run" and "what came back". */
    .tool-input {
        margin: 2px 0 2px 20px;
        padding: 6px 8px;
        background: #0f172a;
        border-radius: 4px;
        font-size: 0.8em;
        color: #cbd5e1;
        white-space: pre-wrap;
        word-wrap: break-word;
        max-height: 200px;
        overflow-y: auto;
        border-left: 2px solid #475569;
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
