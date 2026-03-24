<!-- TelegramStatus.svelte - Telegram bot integration status indicator -->
<!-- Shows Telegram connection status and allows linking chats (v0.14.0+) -->

<script lang="ts">
  import { telegramEnabled, telegramConnected, telegramLinkedChats } from '$lib/coreService';
  import { linkTelegramChat } from '$lib/coreService';

  // Props
  let {
    conversationId
  }: {
    conversationId: string;
  } = $props();

  let linkingChat = $state(false);
  let showLinkDialog = $state(false);
  let chatIdInput = $state('');
  let linkError = $state('');
  let linkSuccess = $state(false);

  // Get linked chat ID for this conversation
  let linkedChatId = $derived($telegramLinkedChats.get(conversationId));

  function handleLinkClick() {
    showLinkDialog = true;
    chatIdInput = '';
    linkError = '';
    linkSuccess = false;
  }

  async function handleLinkChat() {
    if (!chatIdInput.trim()) {
      linkError = 'Please enter a Chat ID';
      return;
    }

    linkingChat = true;
    linkError = '';

    try {
      const result = await linkTelegramChat(conversationId, chatIdInput.trim());

      if (result.status === 'success') {
        linkSuccess = true;
        // Update local state
        telegramLinkedChats.set(new Map($telegramLinkedChats).set(conversationId, chatIdInput.trim()));

        // Close dialog after 1.5 seconds
        setTimeout(() => {
          showLinkDialog = false;
          linkSuccess = false;
        }, 1500);
      } else {
        linkError = result.message || 'Failed to link chat';
      }
    } catch (error: unknown) {
      linkError = error instanceof Error ? error.message : 'Failed to link chat';
    } finally {
      linkingChat = false;
    }
  }

  function handleOverlayKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape' && !linkingChat) {
      showLinkDialog = false;
    }
  }
</script>

{#if $telegramEnabled && $telegramConnected}
  <div class="telegram-status">
    <span class="badge telegram-badge">📱 Telegram</span>
    {#if linkedChatId}
      <span class="linked">✅ Linked to {linkedChatId}</span>
      <button
        class="btn-link"
        onclick={handleLinkClick}
        title="Link different chat"
        type="button"
      >
        Change
      </button>
    {:else}
      <button
        class="btn-link"
        onclick={handleLinkClick}
        title="Link Telegram chat"
        type="button"
      >
        Link Chat
      </button>
    {/if}
  </div>

  {#if showLinkDialog}
    <div
      class="telegram-link-dialog-overlay"
      onclick={() => !linkingChat && (showLinkDialog = false)}
      onkeydown={handleOverlayKeydown}
      role="presentation"
    >
      <div
        class="telegram-link-dialog"
        onclick={(e) => e.stopPropagation()}
        onkeydown={handleOverlayKeydown}
        role="dialog"
        aria-modal="true"
        aria-labelledby="dialog-title"
        tabindex="-1"
      >
        <h3 id="dialog-title">Link Telegram Chat</h3>
        <p class="dialog-help">
          Get your Chat ID from <a href="https://t.me/userinfobot" target="_blank" rel="noopener noreferrer">@userinfobot</a>
          on Telegram.
        </p>

        {#if linkSuccess}
          <div class="success-message">
            ✅ Chat linked successfully!
          </div>
        {:else}
          <div class="form-group">
            <label for="chat-id-input">Chat ID:</label>
            <input
              id="chat-id-input"
              type="text"
              bind:value={chatIdInput}
              placeholder="123456789"
              disabled={linkingChat}
              onkeypress={(e) => e.key === 'Enter' && handleLinkChat()}
            />
          </div>

          {#if linkError}
            <div class="error-message">
              {linkError}
            </div>
          {/if}

          <div class="dialog-actions">
            <button
              class="btn-cancel"
              onclick={() => showLinkDialog = false}
              disabled={linkingChat}
            >
              Cancel
            </button>
            <button
              class="btn-link-confirm"
              onclick={handleLinkChat}
              disabled={linkingChat || !chatIdInput.trim()}
            >
              {linkingChat ? 'Linking...' : 'Link Chat'}
            </button>
          </div>
        {/if}
      </div>
    </div>
  {/if}
{/if}

<style>
  .telegram-status {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.25rem 0.5rem;
    background: #0088cc;
    color: white;
    border-radius: 4px;
    font-size: 0.85rem;
  }

  .telegram-badge {
    font-weight: bold;
  }

  .linked {
    opacity: 0.9;
  }

  .btn-link {
    padding: 0.2rem 0.5rem;
    background: rgba(255, 255, 255, 0.2);
    color: white;
    border: 1px solid rgba(255, 255, 255, 0.3);
    border-radius: 3px;
    font-size: 0.75rem;
    cursor: pointer;
    transition: all 0.2s;
  }

  .btn-link:hover {
    background: rgba(255, 255, 255, 0.3);
  }

  .btn-link:active {
    transform: scale(0.95);
  }

  /* Dialog styles */
  .telegram-link-dialog-overlay {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: rgba(0, 0, 0, 0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
    animation: fadeIn 0.2s ease-out;
  }

  @keyframes fadeIn {
    from {
      opacity: 0;
    }
    to {
      opacity: 1;
    }
  }

  .telegram-link-dialog {
    background: white;
    border-radius: 8px;
    padding: 1.5rem;
    min-width: 400px;
    max-width: 90vw;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
    animation: slideUp 0.3s ease-out;
  }

  @keyframes slideUp {
    from {
      opacity: 0;
      transform: translateY(20px);
    }
    to {
      opacity: 1;
      transform: translateY(0);
    }
  }

  .telegram-link-dialog h3 {
    margin: 0 0 0.75rem 0;
    color: #333;
  }

  .dialog-help {
    margin: 0 0 1rem 0;
    color: #666;
    font-size: 0.9rem;
  }

  .dialog-help a {
    color: #0088cc;
    text-decoration: none;
  }

  .dialog-help a:hover {
    text-decoration: underline;
  }

  .form-group {
    margin-bottom: 1rem;
  }

  .form-group label {
    display: block;
    margin-bottom: 0.4rem;
    font-size: 0.9rem;
    font-weight: 500;
    color: #555;
  }

  .form-group input {
    width: 100%;
    padding: 0.6rem;
    border: 1px solid #ddd;
    border-radius: 4px;
    font-size: 1rem;
    box-sizing: border-box;
  }

  .form-group input:focus {
    outline: none;
    border-color: #0088cc;
    box-shadow: 0 0 0 3px rgba(0, 136, 204, 0.1);
  }

  .error-message {
    padding: 0.6rem;
    background: #f8d7da;
    color: #721c24;
    border: 1px solid #f5c6cb;
    border-radius: 4px;
    font-size: 0.9rem;
    margin-bottom: 1rem;
  }

  .success-message {
    padding: 0.6rem;
    background: #d4edda;
    color: #155724;
    border: 1px solid #c3e6cb;
    border-radius: 4px;
    font-size: 0.9rem;
    text-align: center;
    margin-bottom: 1rem;
  }

  .dialog-actions {
    display: flex;
    justify-content: flex-end;
    gap: 0.75rem;
  }

  .btn-cancel {
    padding: 0.5rem 1rem;
    background: #6c757d;
    color: white;
    border: none;
    border-radius: 4px;
    font-size: 0.9rem;
    cursor: pointer;
    transition: background 0.2s;
  }

  .btn-cancel:hover {
    background: #5a6268;
  }

  .btn-link-confirm {
    padding: 0.5rem 1rem;
    background: #0088cc;
    color: white;
    border: none;
    border-radius: 4px;
    font-size: 0.9rem;
    cursor: pointer;
    transition: background 0.2s;
  }

  .btn-link-confirm:hover:not(:disabled) {
    background: #006699;
  }

  .btn-link-confirm:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  /* Responsive */
  @media (max-width: 480px) {
    .telegram-link-dialog {
      min-width: calc(100vw - 2rem);
    }
  }
</style>
