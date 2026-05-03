<!-- ChatMessageList.svelte - Extracted chat window component -->
<!-- Displays message history with auto-scroll, attachments, and markdown support -->

<script lang="ts">
  import MarkdownMessage from './MarkdownMessage.svelte';
  import ImageMessage from './ImageMessage.svelte';
  import VoicePlayer from './VoicePlayer.svelte';
  import ThinkingBlock from './ThinkingBlock.svelte';
  import type { Message, Mention } from '$lib/types.js';

  // Props (Svelte 5 runes mode)
  let {
    messages,
    conversationId,
    enableMarkdown = $bindable(true),
    chatWindowElement = $bindable(),
    showTranscription = true,  // v0.13.2+: Control transcription display
    agentProgressMessage = null,  // v0.15.0+: Agent progress message
    agentProgressTool = null,  // v0.15.0+: Current tool being executed
    agentProgressRound = 0,  // v0.15.0+: Current round number
    agentStreamingText = "",  // v0.16.0+: Streaming text from agent
    peerDisplayNames = new Map<string, string>(),  // v0.19.1+: Map of node_id -> display name
    selfNodeId = "",  // v0.19.1+: Current user's node ID
    selfName = ""  // v0.19.1+: Current user's display name
  }: {
    messages: Message[];
    conversationId: string;
    enableMarkdown?: boolean;
    chatWindowElement?: HTMLElement;
    showTranscription?: boolean;
    agentProgressMessage?: string | null;
    agentProgressTool?: string | null;
    agentProgressRound?: number;
    agentStreamingText?: string;
    peerDisplayNames?: Map<string, string>;
    selfNodeId?: string;
    selfName?: string;
  } = $props();

  // A sender counts as "AI" if it's the canonical 'ai' string (direct DPC queries),
  // starts with 'agent_' (Telegram-bridged, history-loaded, or proactively-fetched agent messages),
  // or is 'cc' (Claude Code responses injected via @CC mentions).
  const isAiSender = (sender: string) => sender === 'ai' || sender === 'cc' || sender?.startsWith('agent_');

  // Debug: Log when progress props change
  $effect(() => {
    if (agentProgressTool || agentProgressMessage) {
      console.log(`[ChatMessageList] Progress props: tool=${agentProgressTool}, msg=${agentProgressMessage?.substring(0,50)}, round=${agentProgressRound}`);
    }
  });

  // Auto-scroll when streaming text updates
  $effect(() => {
    if (agentStreamingText && chatWindowElement) {
      chatWindowElement.scrollTop = chatWindowElement.scrollHeight;
    }
  });

  // Escape HTML to prevent XSS
  function escapeHtml(text: string): string {
    return text
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  // Turn [42] message refs into clickable anchors pointing to #msg-42.
  // Input MUST already be HTML-escaped — we don't re-escape the N digits.
  function linkifyMessageRefs(escapedHtml: string): string {
    return escapedHtml.replace(/\[(\d+)\]/g, '<a class="msg-ref" href="#msg-$1">[$1]</a>');
  }

  // Highlight @-mentions in message text
  function highlightMentions(text: string, mentions: Mention[] | undefined): string {
    if (!mentions || mentions.length === 0) {
      return linkifyMessageRefs(escapeHtml(text));
    }

    // Sort mentions by start position
    const sortedMentions = [...mentions].sort((a, b) => a.start - b.start);

    let result = '';
    let lastEnd = 0;

    for (const mention of sortedMentions) {
      // Add text before this mention
      result += linkifyMessageRefs(escapeHtml(text.slice(lastEnd, mention.start)));
      // Add highlighted mention
      result += `<span class="mention" data-node-id="${escapeHtml(mention.node_id)}">@${escapeHtml(mention.name)}</span>`;
      lastEnd = mention.end;
    }

    // Add remaining text after last mention
    result += linkifyMessageRefs(escapeHtml(text.slice(lastEnd)));

    return result;
  }
</script>

<div class="chat-window" bind:this={chatWindowElement}>
  {#if messages.length > 0}
    {#each messages as msg, i (msg.id)}
      <div id="msg-{i}" class="message" class:user={msg.sender === 'user'} class:system={msg.sender === 'system'} class:error={msg.isError}>
        <div class="message-header">
          <strong>
            {#if msg.sender === 'user'}
              {#if conversationId.startsWith('group-') && selfName}
                <!-- Group chat: Show own name instead of "You" -->
                {selfName} | {selfNodeId}
              {:else if conversationId.startsWith('agent_') || conversationId.startsWith('agent-')}
                <!-- Agent chat: always "You" for user messages, backend may set senderName -->
                You
              {:else}
                {msg.senderName || 'You'}
              {/if}
            {:else if msg.sender === 'ai'}
              {msg.senderName || (msg.model ? `AI (${msg.model})` : 'AI Assistant')}
            {:else}
              {#if conversationId.startsWith('group-')}
                <!-- Group chat: Agent messages show agent name; human messages use peerDisplayNames -->
                {#if msg.isAgent && msg.senderName}
                  {msg.senderName} (agent)
                {:else}
                  {peerDisplayNames.get(msg.sender)?.split(' | ')[0] || msg.senderName || msg.sender} | {msg.sender}
                {/if}
              {:else}
                {msg.senderName ? `${msg.senderName} | ${msg.sender}` : msg.sender}
              {/if}
            {/if}
          </strong>
          <span class="timestamp"><span class="msg-index">#{i}</span> {new Date(msg.timestamp).toLocaleTimeString()}</span>
        </div>
        <!-- Thinking block (v1.4+): Display AI reasoning before main response -->
        {#if isAiSender(msg.sender) && msg.thinking}
          <ThinkingBlock thinking={msg.thinking} tokenCount={msg.thinkingTokens} />
        {/if}

        <!-- Message text (hidden for voice attachments with transcription to avoid duplication, v0.15.1+) -->
        {#if msg.text && msg.text !== '[Image]' && !msg.attachments?.some(a => a.type === 'voice' && a.transcription)}
          {#if isAiSender(msg.sender) && enableMarkdown}
            <MarkdownMessage content={msg.text} />
          {:else if msg.mentions && msg.mentions.length > 0}
            <!-- Group chat message with @-mentions -->
            <p>{@html highlightMentions(msg.text, msg.mentions)}</p>
          {:else}
            <p>{@html linkifyMessageRefs(escapeHtml(msg.text))}</p>
          {/if}
        {/if}

        <!-- Raw streaming output (v0.16.0+): Collapsible section showing incremental text -->
        {#if isAiSender(msg.sender) && msg.streamingRaw && msg.streamingRaw.length > 50}
          <details class="streaming-raw-details">
            <summary class="streaming-raw-summary">
              <span class="streaming-raw-icon">📝</span>
              <span>Raw output ({msg.streamingRaw.length} chars)</span>
            </summary>
            <pre class="streaming-raw-content">{msg.streamingRaw}</pre>
          </details>
        {/if}

        <!-- Attachments (Phase 2.5: Images + Files + v0.13.0: Voice) -->
        {#if msg.attachments && msg.attachments.length > 0}
          <div class="message-attachments">
            {#each msg.attachments as attachment}
              {#if attachment.type === 'image'}
                <!-- Image attachment (Phase 2.5: Screenshot + Vision) -->
                <ImageMessage {attachment} {conversationId} />
              {:else if attachment.type === 'voice' && attachment.voice_metadata}
                <!-- Voice attachment (v0.13.0: Voice Messages) -->
                <div class="voice-attachment">
                  {#if attachment.file_path}
                    {#key attachment.transfer_id || attachment.file_path}
                      <VoicePlayer
                        audioUrl={attachment.file_path}
                        filePath={attachment.file_path}
                        duration={attachment.voice_metadata.duration_seconds}
                        timestamp={attachment.voice_metadata.recorded_at}
                        transcription={showTranscription ? attachment.transcription : undefined}
                        showTranscriberName={false}
                      />
                    {/key}
                  {:else}
                    <div class="voice-pending">
                      <span>Voice message ({attachment.voice_metadata.duration_seconds}s)</span>
                      {#if attachment.status}
                        • {attachment.status}
                      {/if}
                    </div>
                  {/if}
                </div>
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

  <!-- Agent progress indicator (v0.15.0+) -->
  {#if agentProgressTool || agentProgressMessage}
    <div class="agent-progress">
      <div class="agent-progress-spinner"></div>
      <div class="agent-progress-content">
        {#if agentProgressTool}
          <span class="agent-progress-tool">🔧 {agentProgressTool}</span>
        {/if}
        {#if agentProgressRound > 0}
          <span class="agent-progress-round">Round {agentProgressRound}</span>
        {/if}
        {#if agentProgressMessage}
          <span class="agent-progress-message">{agentProgressMessage}</span>
        {/if}
      </div>
    </div>
  {/if}

  <!-- Agent streaming text (v0.16.0+) - Shows AI response as it's generated -->
  {#if agentStreamingText}
    <div class="message ai-streaming">
      <div class="message-header">
        <strong>{conversationId?.startsWith('agent_') ? 'Agent' : 'AI Assistant'}</strong>
        <span class="streaming-indicator">✨ Generating...</span>
      </div>
      <!-- Always use plain text during streaming for performance - markdown renders on final message -->
      <div class="message-text streaming-content">
        <pre class="streaming-plain">{agentStreamingText}</pre>
      </div>
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
    scroll-behavior: smooth; /* Smooth scroll when [N] refs are clicked */
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
    margin-right: 0.5rem; /* Add right margin for spacing from edge */
  }

  .message:not(.user):not(.system) {
    background: white;
    border: 1px solid #eee;
    margin-left: 0.5rem; /* Add left margin for spacing from edge */
  }

  .message.system {
    background: #fff0f0;
    border: 1px solid #ffc0c0;
    font-style: italic;
    margin-left: auto;
    margin-right: auto;
  }

  .message.error {
    background: #fff5f5;
    border: 1px solid #fc8181;
    border-left: 4px solid #e53e3e;
    margin-left: 0.5rem;
  }

  .message.error :global(strong) {
    color: #c53030;
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

  .msg-index {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.7rem;
    color: #aaa;
    margin-right: 0.25rem;
    user-select: all;
  }

  :global(.msg-ref) {
    color: #0366d6;
    text-decoration: none;
    font-variant-numeric: tabular-nums;
  }

  :global(.msg-ref:hover) {
    text-decoration: underline;
  }

  /* Brief target highlight when user clicks a [N] ref */
  .message:target {
    background: rgba(3, 102, 214, 0.08);
    transition: background 1.5s ease-out;
  }

  .message p {
    margin: 0;
    white-space: pre-wrap;
    word-wrap: break-word;
    overflow-wrap: break-word; /* Break long words */
    word-break: break-word; /* Break long unbreakable strings */
  }

  :global(.mention) {
    background: rgba(137, 180, 250, 0.2);
    color: #1e88e5;
    padding: 0.1em 0.2em;
    border-radius: 4px;
    font-weight: 500;
    cursor: pointer;
  }

  :global(.mention:hover) {
    background: rgba(137, 180, 250, 0.35);
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

  .voice-attachment {
    margin-top: 8px;
  }

  .voice-pending {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 12px;
    background: rgba(200, 220, 255, 0.1);
    border: 1px solid rgba(100, 150, 255, 0.2);
    border-radius: 8px;
    color: #555;
    font-size: 13px;
  }

  /* Agent progress indicator (v0.15.0+) */
  .agent-progress {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 12px 16px;
    margin: 8px 0;
    background: linear-gradient(135deg, #e3f2fd 0%, #f3e5f5 100%);
    border: 1px solid #90caf9;
    border-radius: 12px;
    animation: pulse 2s ease-in-out infinite;
  }

  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.8; }
  }

  .agent-progress-spinner {
    width: 20px;
    height: 20px;
    border: 2px solid #90caf9;
    border-top-color: #1976d2;
    border-radius: 50%;
    animation: spin 1s linear infinite;
  }

  @keyframes spin {
    to { transform: rotate(360deg); }
  }

  .agent-progress-content {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    flex: 1;
  }

  .agent-progress-tool {
    font-weight: 600;
    color: #1565c0;
  }

  .agent-progress-round {
    font-size: 0.85em;
    color: #666;
    background: rgba(0,0,0,0.05);
    padding: 2px 8px;
    border-radius: 10px;
  }

  .agent-progress-message {
    color: #555;
    font-size: 0.9em;
    max-width: 300px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* Agent streaming text (v0.16.0+) */
  .ai-streaming {
    background: linear-gradient(135deg, #e8f5e9 0%, #f1f8e9 100%);
    border: 1px solid #a5d6a7;
    animation: fade-in 0.3s ease;
  }

  @keyframes fade-in {
    from { opacity: 0; transform: translateY(10px); }
    to { opacity: 1; transform: translateY(0); }
  }

  .streaming-indicator {
    color: #4caf50;
    font-size: 0.75rem;
    margin-left: 8px;
    animation: blink 1.5s ease-in-out infinite;
  }

  @keyframes blink {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
  }

  .streaming-content {
    min-height: 20px;
  }

  .streaming-plain {
    margin: 0;
    padding: 0;
    white-space: pre-wrap;
    word-wrap: break-word;
    font-family: inherit;
    font-size: inherit;
    background: transparent;
    color: inherit;
    line-height: 1.5;
  }

  /* Collapsible raw streaming output (v0.16.0+) */
  .streaming-raw-details {
    margin-top: 0.75rem;
    border: 1px solid #e0e0e0;
    border-radius: 6px;
    background: #fafafa;
  }

  .streaming-raw-summary {
    padding: 0.5rem 0.75rem;
    cursor: pointer;
    font-size: 0.85rem;
    color: #666;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    user-select: none;
  }

  .streaming-raw-summary:hover {
    background: #f0f0f0;
  }

  .streaming-raw-icon {
    font-size: 0.9rem;
  }

  .streaming-raw-content {
    margin: 0;
    padding: 0.75rem;
    border-top: 1px solid #e0e0e0;
    background: #f5f5f5;
    font-family: 'Courier New', Consolas, Monaco, monospace;
    font-size: 0.8rem;
    line-height: 1.4;
    white-space: pre-wrap;
    word-wrap: break-word;
    max-height: 300px;
    overflow-y: auto;
    color: #555;
  }
</style>
