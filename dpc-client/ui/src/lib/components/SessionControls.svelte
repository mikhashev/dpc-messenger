<!-- SessionControls.svelte - Extracted session management controls -->
<!-- Displays token counter, new session button, end session button, and markdown toggle -->

<script lang="ts">
  // Props (Svelte 5 runes mode)
  let {
    showForChatId,
    isAIChat,
    isPeerConnected,
    isTelegramChat = false,
    tokenUsed = 0,
    tokenLimit = 0,
    estimatedTokens = 0,
    showEstimation = false,
    historyTokens = 0,
    contextEstimated = 0,
    messageCount = 0,
    enableMarkdown = $bindable(true),
    onNewSession,
    onEndSession
  }: {
    showForChatId: string;
    isAIChat: boolean;
    isPeerConnected: boolean;
    isTelegramChat?: boolean;
    tokenUsed?: number;
    tokenLimit?: number;
    estimatedTokens?: number;
    showEstimation?: boolean;
    historyTokens?: number;
    contextEstimated?: number;
    messageCount?: number;
    enableMarkdown?: boolean;
    onNewSession: (chatId: string) => void;
    onEndSession: (chatId: string) => void;
  } = $props();

  // Computed properties
  // Use default limit of 16,384 tokens if not yet set (before first message)
  const DEFAULT_TOKEN_LIMIT = 16384;
  let effectiveLimit = $derived(tokenLimit > 0 ? tokenLimit : DEFAULT_TOKEN_LIMIT);

  let totalTokens = $derived(
    tokenUsed + (showEstimation ? estimatedTokens : 0)
  );

  let tokenUsagePercent = $derived(
    effectiveLimit > 0 ? (totalTokens / effectiveLimit) : 0
  );

  // Three-metric display (shown when context_estimated is available from backend)
  let showThreeMetrics = $derived(contextEstimated > 0);
  // historyTokens is a rough chars÷4 estimate; contextEstimated is the actual total from the LLM
  // API. Clamp so the estimate never exceeds the measured total (prevents negative staticMemory).
  let effectiveHistoryTokens = $derived(
    showThreeMetrics ? Math.min(historyTokens, contextEstimated) : historyTokens
  );
  let staticMemory = $derived(showThreeMetrics ? contextEstimated - effectiveHistoryTokens : 0);
  let dialogAvailable = $derived(showThreeMetrics ? effectiveLimit - staticMemory : effectiveLimit);
  let dialogPercent = $derived(dialogAvailable > 0 ? effectiveHistoryTokens / dialogAvailable : 0);
  let totalContextPercent = $derived(effectiveLimit > 0 ? contextEstimated / effectiveLimit : 0);

  let dialogWithInput = $derived(showThreeMetrics && showEstimation && estimatedTokens > 0
    ? effectiveHistoryTokens + estimatedTokens
    : effectiveHistoryTokens);
  let totalWithInput = $derived(showThreeMetrics && showEstimation && estimatedTokens > 0
    ? contextEstimated + estimatedTokens
    : contextEstimated);
  let dialogPercentWithInput = $derived(dialogAvailable > 0 ? dialogWithInput / dialogAvailable : 0);
  let totalContextPercentWithInput = $derived(effectiveLimit > 0 ? totalWithInput / effectiveLimit : 0);

  let showWarning = $derived(
    showThreeMetrics ? totalContextPercentWithInput >= 0.8 : tokenUsagePercent >= 0.8
  );

  // End session is disabled only for P2P chats when peer is offline
  // AI chats and Telegram chats can always end session (no peer voting required)
  let endSessionDisabled = $derived(
    !isPeerConnected && !isAIChat && !isTelegramChat
  );

  let endSessionTitle = $derived(
    endSessionDisabled
      ? "Peer must be online to extract knowledge (requires voting)"
      : "Extract knowledge from this conversation"
  );
</script>

{#if isAIChat}
  <div class="token-counter" class:token-counter--detailed={showThreeMetrics}>
    {#if showThreeMetrics}
      <div class="token-row">
        <span class="token-label">Dialog</span>
        <span class="token-value">{historyTokens.toLocaleString()}{#if showEstimation && estimatedTokens > 0} + ~{estimatedTokens.toLocaleString()}{/if} / {dialogAvailable.toLocaleString()}</span>
        <span class="token-percentage" class:warning={dialogPercentWithInput >= 0.8}>({Math.round(dialogPercentWithInput * 100)}%)</span>
      </div>
      <div class="token-row">
        <span class="token-label">Total</span>
        <span class="token-value">{contextEstimated.toLocaleString()}{#if showEstimation && estimatedTokens > 0} + ~{estimatedTokens.toLocaleString()}{/if} / {effectiveLimit.toLocaleString()}</span>
        <span class="token-percentage" class:warning={totalContextPercentWithInput >= 0.8}>({Math.round(totalContextPercentWithInput * 100)}%)</span>
      </div>
      <div class="token-row token-row--muted" title="System prompt + contexts + tool schemas">
        <span class="token-label">Static</span>
        <span class="token-value">≈{staticMemory.toLocaleString()}</span>
        <span class="token-percentage"></span>
      </div>
      <div class="token-row token-row--muted" title="Number of messages in current conversation">
        <span class="token-label">Messages</span>
        <span class="token-value">{messageCount}</span>
        <span class="token-percentage"></span>
      </div>
    {:else}
      <span
        class="token-value"
        title={showEstimation && estimatedTokens > 0
          ? "Estimate includes current input (4 chars ≈ 1 token, excludes contexts)"
          : ""}
      >
        {#if showEstimation && estimatedTokens > 0}
          {tokenUsed.toLocaleString()} + ~{estimatedTokens.toLocaleString()} / {effectiveLimit.toLocaleString()} tokens
        {:else}
          {tokenUsed.toLocaleString()} / {effectiveLimit.toLocaleString()} tokens
        {/if}
      </span>
      <span class="token-percentage" class:warning={showWarning}>
        ({Math.round(tokenUsagePercent * 100)}%)
      </span>
    {/if}
    {#if showWarning}
      <span class="warning-label">
        ⚠️ Approaching limit
      </span>
    {/if}
  </div>
{/if}

<div class="chat-actions">
  <button class="btn-new-chat" onclick={() => onNewSession(showForChatId)}>
    New Session
  </button>
  <button
    class="btn-end-session"
    onclick={() => onEndSession(showForChatId)}
    disabled={endSessionDisabled}
    title={endSessionTitle}
  >
    Extract Knowledge
  </button>
  {#if isAIChat}
    <button
      class="btn-markdown-toggle"
      class:active={enableMarkdown}
      onclick={() => enableMarkdown = !enableMarkdown}
      title={enableMarkdown ? 'Disable markdown rendering' : 'Enable markdown rendering'}
    >
      {enableMarkdown ? 'Markdown' : 'Text'}
    </button>
  {/if}
</div>

<style>
  .token-counter {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    font-size: 0.85rem;
    padding: 0.4rem 0.8rem;
    background: #f8f9fa;
    border-radius: 6px;
    border: 1px solid #e0e0e0;
  }

  .token-counter--detailed {
    flex-direction: column;
    align-items: stretch;
    gap: 0.2rem;
  }

  .token-row {
    display: flex;
    align-items: center;
    gap: 0.4rem;
  }

  .token-label {
    font-family: 'Courier New', monospace;
    color: #888;
    font-size: 0.75rem;
    font-weight: 500;
    min-width: 3.2rem;
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }

  .token-row--muted .token-value {
    color: #aaa;
  }

  .token-row--muted .token-label {
    color: #bbb;
  }

  .token-value {
    font-family: 'Courier New', monospace;
    color: #333;
    font-weight: 600;
  }

  .token-percentage {
    color: #4CAF50;
    font-weight: 500;
  }

  .token-percentage.warning {
    color: #ff9800;
    font-weight: 600;
  }

  .warning-label {
    color: #ff9800;
    font-weight: 600;
    font-size: 0.8rem;
    animation: pulse 2s ease-in-out infinite;
  }

  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.6; }
  }

  .chat-actions {
    display: flex;
    flex-wrap: wrap;  /* Wrap buttons naturally when they don't fit */
    gap: 0.75rem;
    align-items: center;
    justify-content: flex-end;  /* Keep buttons right-aligned */
    flex: 0 1 auto;  /* Don't grow, but allow shrinking */
    min-width: 0;  /* Allow shrinking below content size */
  }

  .btn-new-chat {
    padding: 0.6rem 1rem;
    background: #6c757d;
    color: white;
    border: none;
    border-radius: 6px;
    font-size: 0.9rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s;
    white-space: nowrap;
    box-shadow: 0 2px 8px rgba(108, 117, 125, 0.3);
  }

  .btn-new-chat:hover {
    background: #5a6268;
    transform: translateY(-2px);
    box-shadow: 0 4px 12px rgba(108, 117, 125, 0.4);
  }

  .btn-new-chat:active {
    transform: translateY(0);
    box-shadow: 0 1px 4px rgba(108, 117, 125, 0.2);
  }

  .btn-end-session {
    padding: 0.6rem 1rem;
    background: #28a745;
    color: white;
    border: none;
    border-radius: 6px;
    font-size: 0.9rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s;
    white-space: nowrap;
    box-shadow: 0 2px 8px rgba(40, 167, 69, 0.3);
  }

  .btn-end-session:hover {
    background: #20c997;
    transform: translateY(-2px);
    box-shadow: 0 4px 12px rgba(40, 167, 69, 0.4);
  }

  .btn-end-session:active {
    transform: translateY(0);
    box-shadow: 0 1px 4px rgba(40, 167, 69, 0.2);
  }

  .btn-end-session:disabled {
    background: #6c757d;
    cursor: not-allowed;
    opacity: 0.6;
    transform: none;
    box-shadow: 0 2px 4px rgba(108, 117, 125, 0.2);
  }

  .btn-markdown-toggle {
    padding: 0.6rem 1rem;
    background: #6c757d;
    color: white;
    border: none;
    border-radius: 6px;
    font-size: 0.9rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s;
    white-space: nowrap;
    opacity: 0.7;
    box-shadow: 0 2px 8px rgba(108, 117, 125, 0.3);
  }

  .btn-markdown-toggle.active {
    background: #17a2b8;
    opacity: 1;
    box-shadow: 0 2px 8px rgba(23, 162, 184, 0.3);
  }

  .btn-markdown-toggle:hover {
    background: #5a6268;
    transform: translateY(-2px);
    box-shadow: 0 4px 12px rgba(108, 117, 125, 0.4);
    opacity: 1;
  }

  .btn-markdown-toggle.active:hover {
    background: #138496;
    box-shadow: 0 4px 12px rgba(23, 162, 184, 0.4);
  }

  .btn-markdown-toggle:active {
    transform: translateY(0);
    box-shadow: 0 1px 4px rgba(108, 117, 125, 0.2);
  }
</style>
