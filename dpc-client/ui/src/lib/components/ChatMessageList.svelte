<!-- ChatMessageList.svelte - Extracted chat window component -->
<!-- Displays message history with auto-scroll, attachments, and markdown support -->

<script lang="ts">
  import MarkdownMessage from './MarkdownMessage.svelte';
  import ImageMessage from './ImageMessage.svelte';
  import VoicePlayer from './VoicePlayer.svelte';
  import ThinkingBlock from './ThinkingBlock.svelte';
  import { showsThinkingBlock } from '$lib/utils/thinkingVisibility';
  import AgentProgressCollapsible from './AgentProgressCollapsible.svelte';
  import { agentLiveTools, agentLiveAgentIds, blockedNodeIds } from '$lib/coreService';
  import { foldBlockedRuns, isBlockedAuthor, blockedRunLabel } from '$lib/utils/blockedRuns';
  import { dedupeMessagesById, formatDedupeDrop } from '$lib/utils/messageMapper';
  import type { Message, Mention } from '$lib/types.js';
  import { revealItemInDir } from '@tauri-apps/plugin-opener';
  import { revealInFolder } from '$lib/utils/revealInFolder';
  import { tick } from 'svelte';
  import {
    historyPagers, shouldLoadOlder, pickAnchorIndex, scrollTopKeepingAnchor,
    readerIsAtBottom, startLineText,
  } from '$lib/utils/historyPaging';

  // "Show in folder" failures, by the path that failed (file gone, no file manager).
  let revealErrors = $state<Record<string, string>>({});

  async function showInFolder(path: string) {
    const failure = await revealInFolder(path, revealItemInDir);
    const { [path]: _cleared, ...rest } = revealErrors;
    revealErrors = failure ? { ...rest, [path]: failure } : rest;
  }

  // Whose agent ran these calls. A tool call carries its input, its full
  // output and the round's reasoning — the owner's data (ADR-042). Records
  // that arrived before the calls stopped travelling are still on disk here;
  // this is what keeps them folded away rather than one click from a reader.
  function ownsToolCalls(msg: any): boolean {
    const owner = msg?.agentOwner;
    if (!owner) return true;            // our own local record, or one predating the field
    return !selfNodeId || owner === selfNodeId;
  }

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
    agentProgressName = "",  // S185: Agent display name for live progress
    agentProgressAgentId = "",
    agentProgressSpeed = null,  // S190: Agent ID for Stop button routing
    agentStreamingText = "",  // v0.16.0+: Streaming text from agent
    peerDisplayNames = new Map<string, string>(),  // v0.19.1+: Map of node_id -> display name
    selfNodeId = "",  // v0.19.1+: Current user's node ID
    selfName = "",  // v0.19.1+: Current user's display name
    // Loads the page before the oldest message (ChatHistorySyncPanel.loadOlderPage).
    onLoadOlder = undefined,
  }: {
    messages: Message[];
    conversationId: string;
    enableMarkdown?: boolean;
    chatWindowElement?: HTMLElement;
    showTranscription?: boolean;
    agentProgressMessage?: string | null;
    agentProgressTool?: string | null;
    agentProgressRound?: number;
    agentProgressName?: string;
    agentProgressAgentId?: string;
    agentProgressSpeed?: Record<string, unknown> | null;
    agentStreamingText?: string;
    peerDisplayNames?: Map<string, string>;
    selfNodeId?: string;
    selfName?: string;
    onLoadOlder?: (chatId: string, beforeInsert: () => void) => Promise<number>;
  } = $props();

  // Last wall before the keyed {#each} below: the seven batch guards upstream
  // (HistorySyncPanel, ChatHistorySyncPanel, +page.svelte, AgentPanel) catch a
  // repeated id in a history batch, but a single append can also repeat one —
  // Telegram ids keyed on Date.now(), an agent tick, a bubble re-keyed to a
  // resp.message_id. Whatever reaches `messages`, this
  // is the list actually rendered, so every index/order-dependent read below
  // uses it too, not the raw prop.
  let deduped = $derived(dedupeMessagesById(messages));
  let displayMessages = $derived(deduped.kept);

  // A blocked node's group messages stay in the history; here a run of them folds into
  // one row. `runAt` maps a message's position in `displayMessages` to its run: the
  // run's key (the id of its first message), its length, and whether this is the first.
  let blockedSet = $derived(new Set($blockedNodeIds));
  let expandedRuns = $state<Record<string, boolean>>({});
  let runAt = $derived.by(() => {
    const at = new Map<number, { key: string; count: number; first: boolean }>();
    if (!conversationId.startsWith('group-') || blockedSet.size === 0) return at;
    const rows = foldBlockedRuns(displayMessages, (m) =>
      isBlockedAuthor(blockedSet, { sender: m.sender, agentOwner: m.agentOwner }));
    for (const row of rows) {
      if (row.kind !== 'run') continue;
      const key = row.messages[0].id;
      row.messages.forEach((_, k) => {
        at.set(row.startIndex + k, { key, count: row.messages.length, first: k === 0 });
      });
    }
    return at;
  });

  let lastWarnedDropped = 0;
  $effect(() => {
    const { droppedCount, conflictCount } = deduped;
    if (droppedCount > 0 && droppedCount !== lastWarnedDropped) {
      console.warn(`[ChatMessageList] Dropped ${formatDedupeDrop(droppedCount, conflictCount)} in messages for ${conversationId}`);
    }
    lastWarnedDropped = droppedCount;
  });

  // A sender counts as "AI" if it's the canonical 'ai' string (direct DPC queries),
  // starts with 'agent_' (Telegram-bridged, history-loaded, or proactively-fetched agent messages),
  // or is 'cc' (Claude Code responses injected via @CC mentions).
  const isAiSender = (sender: string, msg?: any) => sender === 'ai' || sender === 'cc' || sender?.startsWith('agent_') || msg?.isAgent;

  const truncateNodeId = (id: string) => {
    const prefix = 'dpc-node-';
    if (id.startsWith(prefix)) return prefix + id.slice(prefix.length, prefix.length + 8);
    return id;
  };

  // Live tool calls: authoritative snapshot from the backend (agent_progress.tool_calls),
  // keyed per conversation. Renders the full run directly — no lossy accumulation, no
  // dropped results, survives chat switches. The currently-executing tool shows via the
  // live spinner (currentTool); completed tools come from the snapshot with their results.
  let liveToolCalls = $derived($agentLiveTools[conversationId] ?? []);

  // Who runs here, as far as the client knows. Never the first agent in the
  // list: in a group that named an idle agent and Stop missed. Empty lets
  // CoreService.interrupt_agent find the running agent itself.
  let stopAgentId = $derived(
    agentProgressAgentId
      || $agentLiveAgentIds[conversationId]
      || (conversationId.startsWith('agent_') ? conversationId : '')
  );

  // Filter tool_call code blocks from streaming when collapsible is active
  let filteredStreamingText = $derived.by(() => {
    if (!agentStreamingText || liveToolCalls.length === 0) return agentStreamingText || '';
    return agentStreamingText
      .replace(/```tool_call\n[\s\S]*?```\n?/g, '')
      .replace(/[⊙✓✗] \w[\w_]*(?:\.\.\.|: [\s\S]*?\n)/g, '')
      .trim();
  });

  // ---------------------------------------------------------------------------
  // Older pages on scroll-up (historyPaging.ts). Plain lets, not state: the
  // scroll handlers write them and nothing renders from them.
  // ---------------------------------------------------------------------------
  let pager = $derived($historyPagers.get(conversationId));
  let startLine = $derived(startLineText(pager, displayMessages.length > 0));
  let lastScrollTop = 0;
  let userScrolled = false;   // the reader has touched this chat's scroll since it opened
  let atBottom = true;        // as of the last scroll: live content may follow the reader
  let loadingOlder = false;

  $effect(() => {
    void conversationId;
    lastScrollTop = 0;
    userScrolled = false;
    atBottom = true;
  });

  type Anchor = { node: Element; offset: number };

  function topVisibleMessage(el: HTMLElement): Anchor | null {
    const nodes = Array.from(el.querySelectorAll(':scope > .message'));
    const top = el.getBoundingClientRect().top;
    const i = pickAnchorIndex(nodes.map((n) => n.getBoundingClientRect()), top);
    return i < 0 ? null : { node: nodes[i], offset: nodes[i].getBoundingClientRect().top - top };
  }

  // Put the anchor back where it was. By hand rather than CSS overflow-anchor,
  // which WebKit (WKWebView, WebKitGTK) cannot be relied on for; measured, so a
  // webview that did anchor by itself gets a correction of zero.
  function keepAnchor(el: HTMLElement, anchor: Anchor) {
    if (!anchor.node.isConnected) return;
    const after = anchor.node.getBoundingClientRect().top - el.getBoundingClientRect().top;
    if (after === anchor.offset) return;
    // `scroll-behavior: smooth` on the window would animate this jump.
    const behavior = el.style.scrollBehavior;
    el.style.scrollBehavior = 'auto';
    el.scrollTop = scrollTopKeepingAnchor(el.scrollTop, anchor.offset, after);
    el.style.scrollBehavior = behavior;
    lastScrollTop = el.scrollTop;
  }

  async function loadOlder() {
    const el = chatWindowElement;
    if (loadingOlder || !onLoadOlder || !el) return;
    loadingOlder = true;
    const chatId = conversationId;
    const noted: { anchor: Anchor | null } = { anchor: null };
    try {
      const added = await onLoadOlder(chatId, () => { noted.anchor = topVisibleMessage(el); });
      const a = noted.anchor;
      if (added > 0 && a && chatId === conversationId) {
        await tick();
        keepAnchor(el, a);
        // Once more after layout settles (fonts, markdown blocks).
        requestAnimationFrame(() => keepAnchor(el, a));
      }
    } finally {
      loadingOlder = false;
    }
  }

  function onScroll() {
    const el = chatWindowElement;
    if (!el) return;
    const movedUp = el.scrollTop < lastScrollTop;
    lastScrollTop = el.scrollTop;
    atBottom = readerIsAtBottom(el);
    if (shouldLoadOlder({ pager, scrollTop: el.scrollTop, movedUp, userScrolled })) loadOlder();
  }

  // A wheel up at the very top moves nothing, so no scroll event says so.
  function onWheel(e: WheelEvent) {
    userScrolled = true;
    const el = chatWindowElement;
    if (!el || e.deltaY >= 0) return;
    if (shouldLoadOlder({ pager, scrollTop: el.scrollTop, movedUp: true, userScrolled })) loadOlder();
  }

  function onReaderInput() {
    userScrolled = true;
  }

  // A drag on the scrollbar or a touch also counts. Listened to directly: the
  // window is not a control, and a handler attribute would make it one to a11y.
  // The wheel too, and passive: Svelte's onwheel is not, which made Chromium
  // hold every wheel scroll for a handler that never cancels it.
  $effect(() => {
    const el = chatWindowElement;
    if (!el) return;
    el.addEventListener('pointerdown', onReaderInput, { passive: true });
    el.addEventListener('touchstart', onReaderInput, { passive: true });
    el.addEventListener('wheel', onWheel, { passive: true });
    return () => {
      el.removeEventListener('pointerdown', onReaderInput);
      el.removeEventListener('touchstart', onReaderInput);
      el.removeEventListener('wheel', onWheel);
    };
  });

  // Keys that scroll the focused window up; the window itself takes no keys.
  function onWindowKey(e: KeyboardEvent) {
    if (e.key === 'PageUp' || e.key === 'Home' || e.key === 'ArrowUp') userScrolled = true;
  }

  // Auto-scroll when streaming text updates — only for a reader at the bottom.
  $effect(() => {
    if (agentStreamingText && chatWindowElement && atBottom) {
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

<svelte:window onkeydown={onWindowKey} />

<div
  class="chat-window"
  bind:this={chatWindowElement}
  onscroll={onScroll}
>
  {#if startLine}
    <div class="history-boundary">{startLine}</div>
  {/if}
  {#if displayMessages.length > 0}
    {#each displayMessages as msg, i (msg.id)}
      {@const run = runAt.get(i)}
      {#if run?.first}
        <button
          type="button"
          class="blocked-run"
          aria-expanded={!!expandedRuns[run.key]}
          onclick={() => { expandedRuns = { ...expandedRuns, [run.key]: !expandedRuns[run.key] }; }}
        >{expandedRuns[run.key] ? '▼' : '▶'} {blockedRunLabel(run.count)}</button>
      {/if}
      {#if !run || expandedRuns[run.key]}
      <div id="msg-{i}" class="message" class:user={msg.sender === 'user'} class:system={msg.sender === 'system'} class:error={msg.isError || msg.turnEnd === 'failed'} class:muted={!!msg.turnEnd && msg.turnEnd !== 'failed'}>
        <div class="message-header">
          <strong>
            {#if msg.sender === 'user'}
              {#if conversationId.startsWith('group-') && selfName}
                {selfName}
              {:else if conversationId.startsWith('agent_') || conversationId.startsWith('agent-')}
                You
              {:else}
                {msg.senderName || 'You'}
              {/if}
            {:else if msg.sender === 'ai'}
              {msg.senderName || (msg.model ? `AI (${msg.model})` : 'AI Assistant')}
            {:else}
              {#if conversationId.startsWith('group-')}
                {#if msg.isAgent && msg.senderName}
                  {msg.senderName} <span class="agent-badge" title={msg.agentOwner || ''}>(agent{#if msg.agentOwner}, {msg.agentOwner.startsWith('dpc-node-') ? truncateNodeId(msg.agentOwner) : msg.agentOwner}{/if})</span>
                {:else}
                  {peerDisplayNames.get(msg.sender)?.split(' | ')[0] || msg.senderName || msg.sender}
                  {#if msg.sender && msg.sender.startsWith('dpc-node-') && msg.sender !== 'user'}
                    <span class="node-id-badge" title={msg.sender}>({truncateNodeId(msg.sender)})</span>
                  {/if}
                {/if}
              {:else}
                {msg.senderName || msg.sender}
              {/if}
            {/if}
          </strong>
          <span class="timestamp">{#if msg.msg_index}<span class="msg-index">#{msg.msg_index}</span> {/if}{new Date(msg.timestamp).toLocaleTimeString()}</span>
        </div>
        <!-- Whose reasoning is shown is a property of the surface, not of the
             sender: an agent chat renders rounds and tool calls of its own, and
             the local AI chat has nothing else — while marking its own answers
             `sender: 'ai'`, which is how it fell under a rule written for agents
             and could never draw this block at all. -->
        {#if msg.thinking && showsThinkingBlock(conversationId, isAiSender(msg.sender, msg))}
          <ThinkingBlock thinking={msg.thinking} tokenCount={msg.thinkingTokens} />
        {/if}

        <!-- Message text (hidden for voice attachments with transcription to avoid duplication, v0.15.1+) -->
        {#if msg.text && msg.text !== '[Image]' && !msg.attachments?.some(a => a.type === 'voice' && a.transcription)}
          {#if enableMarkdown}
            <MarkdownMessage content={msg.text} />
          {:else if msg.mentions && msg.mentions.length > 0}
            <!-- Group chat message with @-mentions -->
            <p>{@html highlightMentions(msg.text, msg.mentions)}</p>
          {:else}
            <p>{@html linkifyMessageRefs(escapeHtml(msg.text))}</p>
          {/if}
        {/if}

        <!-- Tool calls collapsible (ADR-030 v3 / UI-AGENT-ACTIONS-COLLAPSIBLE) -->
        {#if isAiSender(msg.sender, msg) && msg.tool_calls && msg.tool_calls.length > 0 && ownsToolCalls(msg)}
          <AgentProgressCollapsible
            toolCalls={msg.tool_calls}
            agentName={msg.senderName || ''}
            speed={(msg as any).speed_summary || null}
          />
        {/if}

        <!-- Raw streaming output hidden for all agent messages -->
        {#if !isAiSender(msg.sender, msg) && msg.streamingRaw && msg.streamingRaw.length > 50}
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
                    {#if attachment.file_path && revealErrors[attachment.file_path]}
                      <div class="file-reveal-error">{revealErrors[attachment.file_path]}</div>
                    {/if}
                  </div>
                  {#if attachment.file_path}
                    {@const savedPath = attachment.file_path}
                    <button
                      type="button"
                      class="file-reveal-btn"
                      title={savedPath}
                      onclick={() => showInFolder(savedPath)}
                    >Show in folder</button>
                  {/if}
                </div>
              {/if}
            {/each}
          </div>
        {/if}

        <!-- Optimistic VL indicator: shown while the backend runs the vision pass
             for a pasted group image opted into "Describe for agents (VL)". -->
        {#if msg.describing}
          <div class="vl-describing">
            <span class="vl-spinner"></span>
            <span>Describing… (VL)</span>
          </div>
        {/if}
      </div>
      {/if}
    {/each}
  {:else}
    <div class="empty-chat">
      <p>No messages yet. Start the conversation!</p>
    </div>
  {/if}

  <!-- Unified agent collapsible: tool calls + streaming text in one block (S187).
       liveToolCalls.length keeps it visible when switching INTO a chat with an active
       run (snapshot present) before the next progress event arrives. -->
  {#if agentProgressTool || agentProgressMessage || filteredStreamingText || liveToolCalls.length > 0}
    <AgentProgressCollapsible
      toolCalls={liveToolCalls}
      agentName={agentProgressName}
      isLive={true}
      currentTool={agentProgressTool || ''}
      currentRound={agentProgressRound}
      streamingText={filteredStreamingText}
      conversationId={conversationId}
      agentId={stopAgentId}
      speed={agentProgressSpeed}
    />
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

  .history-boundary {
    text-align: center;
    color: #888;
    font-size: 0.8rem;
    margin: 0 0 1rem;
    padding: 0.4rem 0;
    border-bottom: 1px dashed #ddd;
  }

  .blocked-run {
    display: block;
    margin: 0 0 1rem;
    padding: 0.4rem 0.75rem;
    border: 1px dashed #bbb;
    border-radius: 12px;
    background: transparent;
    color: #777;
    font-size: 0.85rem;
    cursor: pointer;
    text-align: left;
  }

  .blocked-run:hover {
    background: #f0f0f0;
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

  /* The second selector is as specific as the reply rule above
     (`.message:not(.user):not(.system)`), which an error bubble would
     otherwise lose to and be drawn white like an answer. */
  .message.error,
  .message.error:not(.user):not(.system) {
    background: #fff5f5;
    border: 1px solid #fc8181;
    border-left: 4px solid #e53e3e;
    margin-left: 0.5rem;
  }

  .message.error :global(strong) {
    color: #c53030;
  }

  /* An agent turn that ended without an answer (stopped, killed, refused,
     ended by a guard): kept in the history, drawn as a note rather than a reply.
     As specific as the reply rule above, which would otherwise win. */
  .message.muted:not(.user):not(.system) {
    color: #777;
    background: #f4f4f4;
    border-style: dashed;
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

  .file-reveal-btn {
    flex-shrink: 0;
    padding: 4px 10px;
    font-size: 12px;
    color: #1a1a1a;
    background: rgba(255, 255, 255, 0.5);
    border: 1px solid rgba(0, 0, 0, 0.2);
    border-radius: 6px;
    cursor: pointer;
  }

  .file-reveal-btn:hover {
    background: rgba(255, 255, 255, 0.8);
  }

  .file-reveal-error {
    color: #b3261e;
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

  .vl-describing {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    margin-top: 8px;
    padding: 4px 10px;
    background: rgba(120, 90, 220, 0.1);
    border: 1px solid rgba(120, 90, 220, 0.25);
    border-radius: 8px;
    color: #6b4fd0;
    font-size: 12px;
  }

  .vl-spinner {
    width: 12px;
    height: 12px;
    border: 2px solid rgba(120, 90, 220, 0.3);
    border-top-color: #6b4fd0;
    border-radius: 50%;
    animation: vl-spin 0.7s linear infinite;
  }

  @keyframes vl-spin {
    to { transform: rotate(360deg); }
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
  .agent-badge {
    font-size: 0.75em;
    color: #888;
    font-weight: normal;
  }
  .node-id-badge {
    font-size: 0.7em;
    color: #999;
    font-weight: normal;
    margin-left: 4px;
  }
</style>
