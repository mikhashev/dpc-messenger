<!-- SessionControls.svelte - Extracted session management controls -->
<!-- Displays token counter, new session button, end session button, and markdown toggle -->

<script lang="ts">
  // Props (Svelte 5 runes mode)
  let {
    showForChatId,
    isAIChat,
    isPeerConnected,
    tokenUsed = 0,
    tokenLimit = 0,
    enableMarkdown = $bindable(true),
    onNewSession,
    onEndSession
  }: {
    showForChatId: string;
    isAIChat: boolean;
    isPeerConnected: boolean;
    tokenUsed?: number;
    tokenLimit?: number;
    enableMarkdown?: boolean;
    onNewSession: (chatId: string) => void;
    onEndSession: (chatId: string) => void;
  } = $props();

  // Computed properties
  let tokenUsagePercent = $derived(
    tokenLimit > 0 ? (tokenUsed / tokenLimit) : 0
  );

  let showWarning = $derived(
    tokenUsagePercent >= 0.8
  );

  let endSessionDisabled = $derived(
    !isPeerConnected && !isAIChat  // Disabled for P2P if peer offline
  );

  let endSessionTitle = $derived(
    endSessionDisabled
      ? "Peer must be online to save knowledge (requires voting)"
      : "Extract and save knowledge from this conversation"
  );
</script>

{#if isAIChat && tokenLimit > 0}
  <div class="token-counter">
    <span class="token-value">
      {tokenUsed.toLocaleString()} / {tokenLimit.toLocaleString()} tokens
    </span>
    <span class="token-percentage" class:warning={showWarning}>
      ({Math.round(tokenUsagePercent * 100)}%)
    </span>
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
    End Session & Save Knowledge
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
