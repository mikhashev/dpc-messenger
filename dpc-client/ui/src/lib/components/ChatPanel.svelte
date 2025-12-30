<!-- ChatPanel.svelte - Extracted chat window component -->
<!-- Displays message history with auto-scroll, attachments, and markdown support -->

<script lang="ts">
  import MarkdownMessage from './MarkdownMessage.svelte';
  import ImageMessage from './ImageMessage.svelte';

  // Message type definition
  type Message = {
    id: string;
    sender: string;
    senderName?: string;  // Display name for the sender (peer name or model name)
    text: string;
    timestamp: number;
    commandId?: string;
    model?: string;  // AI model name (for AI responses)
    attachments?: Array<{  // File attachments (Week 1) + Images (Phase 2.4)
      type: 'file' | 'image';
      filename: string;
      file_path?: string;  // Full-size image file path (for P2P file transfers)
      size_bytes: number;
      size_mb?: number;
      hash?: string;
      mime_type?: string;
      transfer_id?: string;
      status?: string;
      // Image-specific fields (Phase 2.4):
      dimensions?: { width: number; height: number };
      thumbnail?: string;  // Base64 data URL
      vision_analyzed?: boolean;  // AI chat only: was vision API used?
      vision_result?: string;  // AI chat only: vision analysis text
    }>;
  };

  // Props (Svelte 5 runes mode)
  let {
    messages,
    conversationId,
    enableMarkdown = $bindable(true),
    chatWindowElement = $bindable()
  }: {
    messages: Message[];
    conversationId: string;
    enableMarkdown?: boolean;
    chatWindowElement?: HTMLElement;
  } = $props();
</script>

<div class="chat-window" bind:this={chatWindowElement}>
  {#if messages.length > 0}
    {#each messages as msg (msg.id)}
      <div class="message" class:user={msg.sender === 'user'} class:system={msg.sender === 'system'}>
        <div class="message-header">
          <strong>
            {#if msg.sender === 'user'}
              You
            {:else if msg.sender === 'ai'}
              {msg.model ? `AI (${msg.model})` : 'AI Assistant'}
            {:else}
              {msg.senderName ? `${msg.senderName} | ${msg.sender.slice(0, 20)}...` : msg.sender}
            {/if}
          </strong>
          <span class="timestamp">{new Date(msg.timestamp).toLocaleTimeString()}</span>
        </div>
        <!-- Message text (shown always, serves as caption for images) -->
        {#if msg.text && msg.text !== '[Image]'}
          {#if msg.sender === 'ai' && enableMarkdown}
            <MarkdownMessage content={msg.text} />
          {:else}
            <p>{msg.text}</p>
          {/if}
        {/if}

        <!-- Attachments (Phase 2.5: Images + Files) -->
        {#if msg.attachments && msg.attachments.length > 0}
          <div class="message-attachments">
            {#each msg.attachments as attachment}
              {#if attachment.type === 'image'}
                <!-- Image attachment (Phase 2.5: Screenshot + Vision) -->
                <ImageMessage {attachment} {conversationId} />
              {:else}
                <!-- Regular file attachment -->
                <div class="file-attachment">
                  <div class="file-details">
                    <div class="file-name">{attachment.filename}</div>
                    <div class="file-meta">
                      {attachment.size_mb ? `${attachment.size_mb} MB` : `${(attachment.size_bytes / (1024 * 1024)).toFixed(2)} MB`}
                      {#if attachment.mime_type}
                        • {attachment.mime_type}
                      {/if}
                      {#if attachment.status}
                        • {attachment.status}
                      {/if}
                    </div>
                  </div>
                </div>
              {/if}
            {/each}
          </div>
        {/if}
      </div>
    {/each}
  {:else}
    <div class="empty-chat">
      <p>No messages yet. Start the conversation!</p>
    </div>
  {/if}
</div>

<style>
  .chat-window {
    flex: 1;
    padding: 1rem;
    overflow-y: auto;
    overflow-x: hidden; /* Prevent horizontal overflow */
    background: #f9f9f9;
    max-width: 100%; /* Constrain to parent width */
  }

  .empty-chat {
    display: flex;
    align-items: center;
    justify-content: center;
    height: 100%;
    color: #999;
    font-style: italic;
  }

  .message {
    margin-bottom: 1rem;
    padding: 0.75rem;
    border-radius: 12px;
    max-width: 80%;
    animation: slideIn 0.2s ease-out;
    overflow-wrap: break-word; /* Break long words */
    word-break: break-word; /* Break long unbreakable strings */
  }

  @keyframes slideIn {
    from { opacity: 0; transform: translateY(10px); }
    to { opacity: 1; transform: translateY(0); }
  }

  .message.user {
    background: #dcf8c6;
    margin-left: auto;
  }

  .message:not(.user):not(.system) {
    background: white;
    border: 1px solid #eee;
  }

  .message.system {
    background: #fff0f0;
    border: 1px solid #ffc0c0;
    font-style: italic;
    margin-left: auto;
    margin-right: auto;
  }

  .message-header {
    display: flex;
    justify-content: space-between;
    margin-bottom: 0.5rem;
    font-size: 0.85rem;
  }

  .message-header strong {
    color: #555;
  }

  .timestamp {
    color: #999;
    font-size: 0.75rem;
  }

  .message p {
    margin: 0;
    white-space: pre-wrap;
    word-wrap: break-word;
    overflow-wrap: break-word; /* Break long words */
    word-break: break-word; /* Break long unbreakable strings */
  }

  .message-attachments {
    margin-top: 8px;
  }

  .file-attachment {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 12px;
    background: rgba(255, 255, 255, 0.05);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 8px;
    margin-top: 8px;
    transition: background 0.2s ease;
  }

  .file-attachment:hover {
    background: rgba(255, 255, 255, 0.08);
  }

  .file-details {
    flex: 1;
    min-width: 0;
  }

  .file-name {
    color: #1a1a1a;
    font-weight: 600;
    font-size: 14px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .file-meta {
    color: #4a4a4a;
    font-size: 12px;
    margin-top: 4px;
  }
</style>
