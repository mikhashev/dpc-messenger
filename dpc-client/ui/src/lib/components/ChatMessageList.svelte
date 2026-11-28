<!-- ChatMessageList.svelte - Extracted chat window component -->
<!-- Displays message history with auto-scroll, attachments, and markdown support -->

<script lang="ts">
  import MarkdownMessage from './MarkdownMessage.svelte';
  import ImageMessage from './ImageMessage.svelte';
  import VoicePlayer from './VoicePlayer.svelte';
  import ThinkingBlock from './ThinkingBlock.svelte';
  import { showsThinkingBlock } from '$lib/utils/thinkingVisibility';
  import AgentProgressCollapsible from './AgentProgressCollapsible.svelte';
  import { agentLiveTools, agentLiveAgentIds, agentStopNotes, blockedNodeIds, attachmentRequests, activeFileTransfers, sendCommand, rejectedRecords } from '$lib/coreService';
  import {
    isMissingHere, showsRequestButton, requestKey, requestStatusText, requestFromAnswer,
  } from '$lib/utils/attachmentRequest';
  import { foldBlockedRuns, isBlockedAuthor, blockedRunLabel } from '$lib/utils/blockedRuns';
  import { dedupeMessagesById, formatDedupeDrop } from '$lib/utils/messageMapper';
  import { verdictNote } from '$lib/utils/messageVerdict';
  import { rejectedReasonText, rejectedRecordTime, rejectedRowSlots } from '$lib/utils/rejectedRows';
  import type { Message, Mention, RejectedRecord } from '$lib/types.js';
  import { revealItemInDir } from '@tauri-apps/plugin-opener';
  import { revealInFolder } from '$lib/utils/revealInFolder';
  import { playsInline, knownDurationSeconds, isAttachmentPlaceholderText } from '$lib/utils/pendingAttachment';
  import {
    audioMetaParts, attachmentCaption, musicTrackIdOf, showsOpenInPlayer, renderableAttachments,
  } from '$lib/utils/musicAttachment';
  import { openTrackInPlayer } from '$lib/services/music';
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

  // "Request file": an attachment whose file is not on this node (core marks it
  // `available: false`) asks the peers online for it. A file that is here never
  // shows the button or the label (Mike's call, 2026-11-19).
  function requestOf(messageId: string | undefined, attachment: { filename?: string }) {
    if (!messageId) return undefined;
    return $attachmentRequests.get(requestKey(conversationId, messageId, attachment.filename ?? ''));
  }

  function requestPercent(messageId: string | undefined, attachment: { filename?: string }): number | undefined {
    const transferId = requestOf(messageId, attachment)?.transferId;
    const progress = transferId ? ($activeFileTransfers.get(transferId) as any)?.progress : undefined;
    return typeof progress === 'number' ? progress : undefined;
  }

  async function requestFile(messageId: string | undefined, attachment: { filename?: string }) {
    if (!messageId) return;
    const key = requestKey(conversationId, messageId, attachment.filename ?? '');
    const setState = (state: any) => attachmentRequests.update(map => new Map(map).set(key, state));
    setState({ state: 'requesting' });
    let answer: any;
    try {
      const sent = sendCommand('request_attachment', {
        conversation_id: conversationId, message_id: messageId, filename: attachment.filename,
      });
      answer = typeof sent === 'boolean' ? { status: 'error', message: 'Not connected to the backend' } : await sent;
    } catch (e) {
      answer = { status: 'error', message: e instanceof Error ? e.message : String(e) };
    }
    const next = requestFromAnswer(answer);
    // An event may already have moved the request on (receiving): keep that.
    const now = $attachmentRequests.get(key);
    if (next.state !== 'requesting' || !now || now.state === 'requesting') setState({ ...now, ...next });
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

  // Records this node refused for a wrong signature (DPC #111), shown as red
  // rows at their own place in time. They are not messages: displayMessages,
  // the paging anchors and the unread counts never see them, and the rows are
  // slotted in front of the message their time precedes (utils/rejectedRows.ts).
  let rejectedRows = $derived($rejectedRecords.get(conversationId) ?? []);
  let rejectedSlots = $derived(rejectedRowSlots(displayMessages, rejectedRows));
  // The refused text is folded away until the person opens it, keyed by the
  // row's own key as expandedRuns above is keyed by its run's.
  let expandedRejected = $state<Record<string, boolean>>({});

  // The roster's name for a node, or its full id when the roster does not know
  // it: a rejected row exists because identities did not add up, so an id the
  // roster cannot vouch for is never shortened and never replaced by a name
  // the refused record itself claims.
  function rejectedNodeName(id: string | null): string {
    return (id && peerDisplayNames.get(id)?.split(' | ')[0]) || id || 'unknown';
  }

  function isDpcNodeId(id: string | null): id is string {
    return !!id && id.startsWith('dpc-node-');
  }

  // "seen 1 time, seen 2 times": the count is printed right beside the word,
  // so the plural is chosen here rather than left as a bare " times" on the row.
  function timesWord(count: number): string {
    return count === 1 ? 'time' : 'times';
  }

  // The claimed author: the roster's name for the claimed node when it knows
  // it, else the claimed node's full id; the record's own `claimed_sender_name`
  // is believed only when the record carries no node id to check it against —
  // a look-alike id must not be laundered into a familiar name.
  function rejectedClaimedAuthor(row: RejectedRecord): string {
    if (row.claimed_sender_node_id) {
      return peerDisplayNames.get(row.claimed_sender_node_id)?.split(' | ')[0] || row.claimed_sender_node_id;
    }
    return row.claimed_sender_name || 'unknown';
  }

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
  // A Stop's status lines keep the live block on screen even before any tool ran.
  let liveStopNotes = $derived($agentStopNotes[conversationId] ?? []);

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
  let readerMovedAt = 0;      // performance.now() of the last wheel, pointer, touch or scroll key

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
    readerMovedAt = performance.now();
    const el = chatWindowElement;
    if (!el || e.deltaY >= 0) return;
    if (shouldLoadOlder({ pager, scrollTop: el.scrollTop, movedUp: true, userScrolled })) loadOlder();
  }

  function onReaderInput() {
    userScrolled = true;
    readerMovedAt = performance.now();
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
    if (e.key === 'PageUp' || e.key === 'Home' || e.key === 'ArrowUp') { userScrolled = true; readerMovedAt = performance.now(); }
  }

  // Follow a live turn for a reader at the bottom: streamed text, and every
  // tool-call snapshot and progress line, since a round of tools streams no text.
  // Instant, not the window's smooth scroll: a smooth jump fires scroll events
  // short of the bottom, which read as the reader leaving it and ended the follow.
  $effect(() => {
    void liveToolCalls; void agentProgressRound; void agentProgressTool; void agentProgressMessage; void liveStopNotes;
    const el = chatWindowElement;
    const live = agentStreamingText || liveToolCalls.length > 0 || agentProgressTool || agentProgressMessage || liveStopNotes.length > 0;
    // A reader's own scroll in progress (a smooth jump to round 1) is not overridden.
    if (!live || !el || !atBottom || performance.now() - readerMovedAt < 1000) return;
    const behavior = el.style.scrollBehavior;
    el.style.scrollBehavior = 'auto';
    el.scrollTop = el.scrollHeight;
    el.style.scrollBehavior = behavior;
    lastScrollTop = el.scrollTop;
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

<!-- The row under an attachment whose file is not on this node: its state, and
     "Request file" while no request is under way. Never drawn for a file that is here. -->
{#snippet missingFileRow(messageId: string | undefined, attachment: any)}
  {@const request = requestOf(messageId, attachment)}
  <div class="file-missing-row">
    <span class="file-missing-state" class:file-missing-failed={request && ['not_held', 'no_answer', 'no_peers', 'failed', 'error'].includes(request.state)}>
      {requestStatusText(request, requestPercent(messageId, attachment))}
    </span>
    {#if showsRequestButton(attachment, request) && messageId}
      <button
        type="button"
        class="file-reveal-btn"
        title="Ask the members online to send this file"
        onclick={() => requestFile(messageId, attachment)}
      >Request file</button>
    {/if}
  </div>
{/snippet}

<!-- A record this node refused for a wrong signature (DPC #111), at the record's
     own place in time. The refused text is untrusted — it may carry instructions
     for agents — so it is rendered as text only, never through the markdown
     renderer and never as HTML, and it stays folded until the person opens it.
     The row is not a message: no reply, quote, copy or "send to agent" action,
     nothing the UI sends back. -->
{#snippet rejectedRow(row: RejectedRecord)}
  {@const ms = rejectedRecordTime(row)}
  {@const expanded = !!expandedRejected[row.key]}
  <div class="rejected-row">
    <div class="message-header">
      <strong>{row.reason === 'conflict' ? 'conflict' : 'rejected'}</strong>
      <!-- The record's own time, when it parsed; a refused record may carry a
           garbage or absent time, and "Invalid Date" is not a fact about it. -->
      <span class="timestamp">{#if !Number.isNaN(ms)}{new Date(ms).toLocaleTimeString()}{/if}</span>
    </div>
    <div class="rejected-line">
      sent by <b>{rejectedNodeName(row.transport_node_id)}</b>{#if isDpcNodeId(row.transport_node_id)} <span class="node-id-badge" title={row.transport_node_id}>({truncateNodeId(row.transport_node_id)})</span>{/if}
      · claimed author <b>{rejectedClaimedAuthor(row)}</b>{#if isDpcNodeId(row.claimed_sender_node_id)} <span class="node-id-badge" title={row.claimed_sender_node_id}>({truncateNodeId(row.claimed_sender_node_id)})</span>{/if}{#if row.claimed_agent_owner}, agent of {rejectedNodeName(row.claimed_agent_owner)}{/if}{#if isDpcNodeId(row.claimed_agent_owner)} <span class="node-id-badge" title={row.claimed_agent_owner}>({truncateNodeId(row.claimed_agent_owner)})</span>{/if}
      {#if row.signer_node_id && row.signer_node_id !== row.claimed_sender_node_id}
        · signed by <b>{rejectedNodeName(row.signer_node_id)}</b>{#if isDpcNodeId(row.signer_node_id)} <span class="node-id-badge" title={row.signer_node_id}>({truncateNodeId(row.signer_node_id)})</span>{/if}
      {/if}
      · {rejectedReasonText(row.reason)}
      {#if row.live_count > 0}· live ×{row.live_count}{/if}
      {#if row.sync_count > 0}· sync ×{row.sync_count}{/if}
      {#if row.repair_count > 0}· repair ×{row.repair_count}{/if}
      · seen {row.repeat_count} {timesWord(row.repeat_count)}, first {new Date(row.first_seen).toLocaleTimeString()}
      · id {row.record_id ?? 'unknown'}
    </div>
    <!-- A record with no text to unfold offers no toggle at all; the facts
         above are the whole row then. -->
    {#if row.text}
      <button
        type="button"
        class="rejected-toggle"
        aria-expanded={expanded}
        onclick={() => { expandedRejected = { ...expandedRejected, [row.key]: !expanded }; }}
      >{expanded ? '▼' : '▶'} text</button>
      {#if expanded}
        <pre class="rejected-text">{row.text}</pre>
        {#if row.text_truncated}<span class="rejected-truncated">truncated</span>{/if}
      {/if}
    {/if}
  </div>
{/snippet}

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
      {@const verdict = verdictNote(msg.verification)}
      {#each rejectedSlots.get(i) ?? [] as row (row.key)}
        {@render rejectedRow(row)}
      {/each}
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
          <!-- The signature verdict's quiet note (DPC #111): "awaiting certificate"
               when the author's certificate has not arrived on this machine.
               That one kind is all the row draws — 'verified' and an absent
               verdict say nothing (absence on old records is not an alarm),
               'legacy', a record from before signatures existed and most of the
               owner's history, says nothing either: verdictNote keeps its words
               for the step that needs them, but on the row the note would only
               repeat. A verdict this build does not know, and this node's own
               word 'own', draw nothing too: a note is drawn only for a verdict
               whose meaning is known to be "not checked". The note is text, not
               a colour or an icon, so it reads as words. -->
          <span class="timestamp">{#if msg.msg_index}<span class="msg-index">#{msg.msg_index}</span> {/if}{new Date(msg.timestamp).toLocaleTimeString()}{#if verdict.kind === 'awaiting-certificate'} <span class="verdict-note">{verdict.text}</span>{/if}</span>
        </div>
        <!-- Whose reasoning is shown is a property of the surface, not of the
             sender: an agent chat renders rounds and tool calls of its own, and
             the local AI chat has nothing else — while marking its own answers
             `sender: 'ai'`, which is how it fell under a rule written for agents
             and could never draw this block at all. -->
        {#if msg.thinking && showsThinkingBlock(conversationId, isAiSender(msg.sender, msg))}
          <ThinkingBlock thinking={msg.thinking} tokenCount={msg.thinkingTokens} />
        {/if}

        <!-- Message text (hidden for voice attachments with transcription to avoid duplication, v0.15.1+,
             and when it is only the "Sent file: <name> (<MB> MB)" line the attachment card repeats) -->
        {#if msg.text && msg.text !== '[Image]' && !msg.attachments?.some(a => a.type === 'voice' && a.transcription)
             && !isAttachmentPlaceholderText(msg.text, msg.attachments)}
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
        {#if isAiSender(msg.sender, msg) && ((msg.tool_calls && msg.tool_calls.length > 0) || (msg.stop_notes && msg.stop_notes.length > 0)) && ownsToolCalls(msg)}
          <AgentProgressCollapsible
            toolCalls={msg.tool_calls ?? []}
            agentName={msg.senderName || ''}
            speed={msg.speed_summary || null}
            savedStopNotes={msg.stop_notes ?? []}
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

        <!-- Attachments (Phase 2.5: Images + Files + v0.13.0: Voice). An agent's answer may be
             only a file, with no text (chat_post_file), so the bubble is made of this list alone. -->
        {#if renderableAttachments(msg.attachments).length > 0}
          <div class="message-attachments">
            {#each renderableAttachments(msg.attachments) as attachment}
              {#if attachment.type === 'image' && isMissingHere(attachment)}
                <!-- An image whose file is not here: its thumbnail, if the record has one, and the request -->
                <ImageMessage attachment={{ ...attachment, file_path: undefined }} {conversationId} />
                {@render missingFileRow(msg.id, attachment)}
              {:else if attachment.type === 'image'}
                <!-- Image attachment (Phase 2.5: Screenshot + Vision) -->
                <ImageMessage {attachment} {conversationId} />
              {:else if attachment.type === 'voice' && attachment.voice_metadata}
                <!-- Voice attachment (v0.13.0: Voice Messages) -->
                <div class="voice-attachment">
                  {#if isMissingHere(attachment)}
                    <div class="voice-pending">
                      <span>Voice message ({attachment.voice_metadata.duration_seconds}s)</span>
                    </div>
                    {@render missingFileRow(msg.id, attachment)}
                  {:else if attachment.file_path}
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
                <!-- Regular file attachment; an audio/* file also plays here (ADR-051 D19) -->
                {@const missingHere = isMissingHere(attachment)}
                <div class="file-attachment" class:audio-file={!missingHere && playsInline(attachment)}>
                  <div class="file-details">
                    <div class="file-name">{attachment.filename}</div>
                    <div class="file-meta">
                      {attachment.size_mb ? `${attachment.size_mb} MB` : `${(attachment.size_bytes / (1024 * 1024)).toFixed(2)} MB`}
                      {#if attachment.mime_type}
                        • {attachment.mime_type}
                      {/if}
                      {#each audioMetaParts(attachment) as part}
                        • {part}
                      {/each}
                      {#if attachment.status && !missingHere}
                        • {attachment.status}
                      {/if}
                    </div>
                    {#if attachmentCaption(attachment)}
                      <div class="file-caption">{attachmentCaption(attachment)}</div>
                    {/if}
                    {#if missingHere}
                      {@render missingFileRow(msg.id, attachment)}
                    {:else if playsInline(attachment)}
                      <div class="file-audio-player">
                        {#key attachment.file_path}
                          <VoicePlayer
                            audioUrl={attachment.file_path ?? ''}
                            filePath={attachment.file_path}
                            duration={knownDurationSeconds(attachment)}
                            downloadName={attachment.filename}
                            showDownload={false}
                          />
                        {/key}
                      </div>
                    {/if}
                    {#if attachment.file_path && revealErrors[attachment.file_path]}
                      <div class="file-reveal-error">{revealErrors[attachment.file_path]}</div>
                    {/if}
                  </div>
                  {#if showsOpenInPlayer(attachment) || (attachment.file_path && !missingHere)}
                    <div class="file-actions">
                      {#if showsOpenInPlayer(attachment)}
                        {@const trackId = musicTrackIdOf(attachment) ?? ''}
                        <button
                          type="button"
                          class="file-reveal-btn file-open-player-btn"
                          title="Open this track in the Music Player"
                          onclick={() => openTrackInPlayer(trackId)}
                        >Open in player</button>
                      {/if}
                      {#if attachment.file_path && !missingHere}
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

  <!-- The last slot: rows after the newest message, and every row of a chat
       that holds no message at all — a refused record needs no message to
       precede, which is why this sits outside the condition above. -->
  {#each rejectedSlots.get(displayMessages.length) ?? [] as row (row.key)}
    {@render rejectedRow(row)}
  {/each}

  <!-- Unified agent collapsible: tool calls + streaming text in one block (S187).
       liveToolCalls.length keeps it visible when switching INTO a chat with an active
       run (snapshot present) before the next progress event arrives. -->
  {#if agentProgressTool || agentProgressMessage || filteredStreamingText || liveToolCalls.length > 0 || liveStopNotes.length > 0}
    <AgentProgressCollapsible
      toolCalls={liveToolCalls}
      agentName={agentProgressName}
      isLive={true}
      currentTool={agentProgressTool || ''}
      currentMessage={agentProgressMessage}
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

  /* A record this node refused for a wrong signature (DPC #111): the same reds
     an error bubble wears, and the same margin and padding as an ordinary
     reply — but it is not a message, only the report of one that was refused.
     Its content is untrusted, so nothing inside it is ever rendered as markup. */
  .rejected-row {
    margin: 0 0 1rem 0.5rem;
    padding: 0.75rem;
    border-radius: 12px;
    max-width: 80%;
    background: #fff5f5;
    border: 1px solid #fc8181;
    border-left: 4px solid #e53e3e;
    overflow-wrap: break-word;
    word-break: break-word;
  }

  .rejected-row .message-header strong {
    color: #c53030;
  }

  /* The facts of the refusal, as words — who delivered it, who it claims, why
     it was refused — in the header's size, not the bubble's. */
  .rejected-line {
    font-size: 0.85rem;
    color: #555;
  }

  /* The refused text, unfolded by the toggle below: plain text in the
     surrounding font. Not the browser's monospace default — a <pre> that
     suddenly looks like a code block is a look the record never earned. */
  .rejected-text {
    margin: 0.25rem 0 0;
    white-space: pre-wrap;
    word-break: break-word;
    font-family: inherit;
    font-size: inherit;
  }

  .rejected-truncated {
    font-size: 0.7rem;
    color: #999;
    margin-left: 0.5rem;
  }

  /* A quiet text button: opening the refused text is the reader's choice, and
     the only control the row offers. */
  .rejected-toggle {
    background: none;
    border: none;
    cursor: pointer;
    color: #c53030;
    padding: 0;
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

  /* Quiet on purpose: an open request or an unknown verdict is information,
     not an alarm — the message stays styled as an ordinary reply. */
  .verdict-note {
    font-size: 0.7rem;
    color: #999;
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

  /* The player sits under the name; "Show in folder" stays at the top right. */
  .file-attachment.audio-file {
    align-items: flex-start;
  }

  .file-audio-player {
    margin-top: 8px;
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

  /* The posted caption: a line under the file's name, in the words the agent chose. */
  .file-caption {
    color: #1a1a1a;
    font-size: 13px;
    margin-top: 4px;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  .file-actions {
    display: flex;
    flex-direction: column;
    align-items: stretch;
    gap: 6px;
    flex-shrink: 0;
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

  .file-missing-row {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 4px;
    font-size: 12px;
  }

  .file-missing-state {
    color: #666;
  }

  .file-missing-state.file-missing-failed {
    color: #b3261e;
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
