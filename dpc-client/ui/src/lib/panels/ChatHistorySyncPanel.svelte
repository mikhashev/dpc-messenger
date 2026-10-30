<!-- src/lib/panels/ChatHistorySyncPanel.svelte -->
<!-- Syncs chat history from backend when switching to a peer/agent/group/AI chat with no messages. -->
<!-- Handles page-refresh scenario: frontend loses chatHistories, backend keeps conversation_monitors. -->
<!-- Logic-only panel — no markup, no styles. -->

<script lang="ts">
  import type { Writable } from 'svelte/store';
  import { connectionStatus, sendCommand } from '$lib/coreService';
  import { mapBackendMessage, dedupeMessagesById, formatDedupeDrop } from '$lib/utils/messageMapper';
  import { mergeBackfillWithLive, unconfirmedUserBubbles } from '$lib/utils/liveMessageIdentity';
  import { isAIChatId, legacyImportPending } from '$lib/utils/aiChatStore';
  import { HISTORY_PAGE_LIMIT, historyPagers, setPager, pageFacts, oldestRecordId, prependOlderPage, stopPagerUntilReopen, forgetPagerOnReopen, placeholdersOnly } from '$lib/utils/historyPaging';
  import { onMount, untrack } from 'svelte';
  import { get } from 'svelte/store';
  import type { Message } from '$lib/types.js';

  // ---------------------------------------------------------------------------
  // Props
  // ---------------------------------------------------------------------------
  let {
    activeChatId,
    chatHistories,
    loadingHistory,
    processedMessageIds,
    chatWindow,
    getPeerDisplayName,
    onUpdateTokenUsage,
    hasTokenUsage,
    selfNodeId = "",
  }: {
    activeChatId: string;
    chatHistories: Writable<Map<string, any[]>>;
    loadingHistory: Set<string>;
    processedMessageIds: Set<string>;
    chatWindow: HTMLElement | undefined;
    getPeerDisplayName: (id: string) => string;
    onUpdateTokenUsage: (chatId: string, usage: { used: number; limit: number; historyTokens?: number; tokensAfterLastResponse?: number; tokensAfterLastResponseAt?: string | null; contextAgent?: string; contextAgents?: Array<{name: string, tokens: number, limit: number, percent: number}> | null }) => void;
    hasTokenUsage: (chatId: string) => boolean;
    selfNodeId?: string;
  } = $props();

  // ---------------------------------------------------------------------------
  // Gate history load on selfNodeId arrival (race fix).
  // Without this, get_conversation_history can fire before nodeStatus arrives,
  // and own messages get tagged with the literal node_id instead of 'user',
  // losing the green "me" styling until manual reload.
  // Fallback: after 3s, load anyway with best-effort (selfNodeId may stay '').
  // ---------------------------------------------------------------------------
  let nodeStatusFallbackElapsed = $state(false);
  onMount(() => {
    const t = setTimeout(() => { nodeStatusFallbackElapsed = true; }, 3000);
    return () => clearTimeout(t);
  });
  let canLoadHistory = $derived(!!selfNodeId || nodeStatusFallbackElapsed);

  // ---------------------------------------------------------------------------
  // "Have we fetched this history yet" is its own fact, and it used to be read
  // off the message map: a chat with an entry counted as loaded. One live
  // message arriving over the websocket before the chat was opened created that
  // entry, and the backfill then never ran — measured in ui.log on 2026-08-23,
  // where group-abd874a671e3 sat at «historyLen=1 → Skipping load» call after
  // call while sixty-odd messages waited on the backend. The empty-result
  // branch wrote `[]` for the same purpose, which poisoned the chat the same
  // way. The flag belongs here, not in the content.
  // ---------------------------------------------------------------------------
  const loadedHistory = new Set<string>();
  // Chats whose older-page request failed (the chat was reset or archived under
  // the reader): stopped until next opened, then reloaded from the newest page.
  // Without it a stopped pager stayed stopped until the app restarted.
  const reloadOnOpen = new Set<string>();
  // The chat the effect last saw as open, so a reopen is told from a re-run.
  let lastOpenedChatId: string | null = null;

  // Backend records to chat messages, the same way for the first page and
  // every older one.
  function mapLoadedPage(records: any[], reqChatId: string): Message[] {
    // See HistorySyncPanel: an undated record borrows the time of
    // the one before it rather than the clock.
    let previousTimestamp: number | undefined;
    const loadedMessages: Message[] = records.map((msg: any, index: number) => {
      if (index === 0) console.log(`[ChatHistory] DIAG first msg keys:`, Object.keys(msg), `tool_calls:`, msg.tool_calls?.length, `sender_type:`, msg.sender_type, `msg_index:`, msg.msg_index);
      // In an AI chat the other side is the model: the bubble the live
      // answer gets, named by the record's "AI (model)" when it has one.
      const aiAnswer = isAIChatId(reqChatId) && msg.role === 'assistant';
      const fallbackSender = aiAnswer ? 'ai' : (msg.sender_node_id || (msg.role === 'user' ? 'user' : reqChatId));
      const fallbackName = aiAnswer ? (msg.sender_name || '') : (msg.sender_name || (msg.role === 'user' ? 'You' : getPeerDisplayName(reqChatId)));
      const mapped = mapBackendMessage(msg, {
        fallbackSender,
        fallbackSenderName: fallbackName,
        index,
        totalCount: records.length,
        previousTimestamp,
      });
      previousTimestamp = mapped.timestamp;
      mapped.id = msg.message_id || `backend-${index}-${Date.now()}`;
      const isLocalHuman = msg.sender_type === 'human' && (!msg.sender_node_id || msg.sender_node_id === selfNodeId);
      if (isLocalHuman) {
        mapped.sender = 'user';
        mapped.senderName = 'You';
      }
      return mapped;
    });
    // The backend batch is not trusted to be clean: a repeated id
    // reaches the keyed {#each ... (msg.id)} block and Svelte 5
    // throws each_key_duplicate in production, with no boundary
    // around this panel to catch it. mergeBackfillWithLive
    // only checks membership against `loaded`'s ids — it does not
    // collapse a duplicate already inside `loaded` itself.
    const { kept, droppedCount, conflictCount } = dedupeMessagesById(loadedMessages);
    if (droppedCount > 0) {
      console.warn(`[ChatHistory] Dropped ${formatDedupeDrop(droppedCount, conflictCount)} in loaded history for ${reqChatId}`);
    }
    return kept;
  }

  /**
   * The page before the oldest message on screen, put in front of it. The chat
   * window calls this when the reader scrolls to the top; `beforeInsert` runs
   * just before the messages land so the window can note what to keep in place.
   * Resolves to how many messages were added. One request per chat at a time
   * (the pager's lock); an error stops the pager for that chat.
   */
  export async function loadOlderPage(chatId: string, beforeInsert?: () => void): Promise<number> {
    const pager = get(historyPagers).get(chatId);
    if (!pager || !pager.hasOlder || pager.stopped || pager.loading) return 0;
    const cursor = oldestRecordId(get(chatHistories).get(chatId) || [], unconfirmedUserBubbles);
    if (!cursor) {
      setPager(chatId, { stopped: true });
      return 0;
    }
    setPager(chatId, { loading: true });
    try {
      const result = await sendCommand('get_conversation_history', {
        conversation_id: chatId, before_message_id: cursor, limit: HISTORY_PAGE_LIMIT,
      });
      if (result?.status !== 'success' || (result.conversation_id && result.conversation_id !== chatId)) {
        console.warn(`[ChatHistory] Older page for ${chatId.slice(0,20)} refused, pager stopped until the chat is reopened: ${result?.message ?? result?.status}`);
        stopPagerUntilReopen(chatId, reloadOnOpen);
        return 0;
      }
      const older = mapLoadedPage(result.messages || [], chatId);
      let added = 0;
      if (older.length > 0) {
        beforeInsert?.();
        chatHistories.update(map => {
          const merged = prependOlderPage(map.get(chatId) || [], older);
          added = merged.added;
          if (!added) return map;
          const newMap = new Map(map);
          newMap.set(chatId, merged.merged);
          return newMap;
        });
        older.forEach((m: any) => {
          if (m.id && !m.id.startsWith('backend-')) processedMessageIds.add(m.id);
        });
      }
      const facts = pageFacts(result);
      // Nothing new and yet more to come would repeat the same cursor for ever.
      const stuck = facts.hasOlder && added === 0;
      setPager(chatId, { ...facts, loading: false, stopped: stuck });
      console.log(`[ChatHistory] Older page for ${chatId.slice(0,20)}: +${added}, has_older=${facts.hasOlder}`);
      return added;
    } catch (e) {
      console.error(`[ChatHistory] Older page for ${chatId.slice(0,20)} failed, pager stopped until the chat is reopened:`, e);
      stopPagerUntilReopen(chatId, reloadOnOpen);
      return 0;
    }
  }

  // ---------------------------------------------------------------------------
  // Reactive: Sync chat history from backend when switching to peer chat with no messages (v0.11.2)
  // AI chats included since 2026-10-19: their history.json in ~/.dpc is the
  // copy they are restored from, no longer the UI's localStorage.
  // ---------------------------------------------------------------------------
  $effect(() => {
    // A chat becoming the open one, told apart from the effect re-running for
    // the chat already open. Read before the guards below, so a switch made
    // while disconnected still counts.
    const openedChatId = activeChatId || null;
    if (openedChatId !== lastOpenedChatId) {
      lastOpenedChatId = openedChatId;
      if (openedChatId && forgetPagerOnReopen(openedChatId, loadedHistory, reloadOnOpen)) {
        console.log(`[ChatHistory] ${openedChatId.slice(0,20)} reopened after a failed page request - reloading its newest page`);
        untrack(() => chatHistories.update(map => {
          const current = map.get(openedChatId);
          if (!current) return map;
          const newMap = new Map(map);
          newMap.set(openedChatId, placeholdersOnly(current));
          return newMap;
        }));
      }
    }
    // An AI chat waits for the one-time import from the old UI store: fetched
    // before it, the chat would be marked loaded with an empty history.
    const importPending = $legacyImportPending;
    if (importPending && activeChatId?.startsWith('ai_')) return;
    if ($connectionStatus === 'connected' && canLoadHistory && activeChatId) {
      // activeChatId is reactive and can change while the async fetch awaits, so
      // capture it once and route the response strictly by this id (never the live chat).
      const reqChatId = activeChatId;
      const currentHistory = $chatHistories.get(reqChatId);
      console.log(`[ChatHistory] Reactive triggered: chatId=${reqChatId.slice(0,20)}, historyLen=${currentHistory?.length || 0}, loading=${loadingHistory.has(reqChatId)}`);

      // Guard: skip if already loading, or if this history has been fetched
      // once. Not "the map has an entry" — see loadedHistory above.
      if (loadingHistory.has(reqChatId)) {
        console.log(`[ChatHistory] Skipping - already loading history for ${reqChatId.slice(0,20)}`);
      } else if (!loadedHistory.has(reqChatId)) {
        console.log(`[ChatHistory] Loading history from backend for ${reqChatId.slice(0,20)}...`);

        // Mark as loading to prevent re-triggers
        loadingHistory.add(reqChatId);

        // Load from backend (async IIFE to allow await in reactive statement)
        (async () => {
          try {
            const result = await sendCommand('get_conversation_history', { conversation_id: reqChatId, limit: HISTORY_PAGE_LIMIT });
            // Counts, not the object: the whole history went into ui.log here, and
            // through the log relay back to the backend (see logger.ts).
            console.log(`[ChatHistory] Backend response: status=${result?.status}, messages=${result?.messages?.length ?? 0}`);
            if (result.conversation_id && result.conversation_id !== reqChatId) {
              console.warn(`[ChatHistory] Discarding mismatched history: requested ${reqChatId.slice(0,20)}, got ${String(result.conversation_id).slice(0,20)}`);
              loadingHistory.delete(reqChatId);
              return;
            }
            if (result.status === 'success' && result.messages && result.messages.length > 0) {
              console.log(`[ChatHistory] Loaded ${result.message_count} messages from backend`);

              // Convert backend format to frontend format (v0.15.3: use backend metadata)
              chatHistories.update(map => {
                const newMap = new Map(map);
                const dedupedLoadedMessages = mapLoadedPage(result.messages, reqChatId);
                const agentMsgs = dedupedLoadedMessages.filter((m: any) => m.isAgent);
                const withTools = dedupedLoadedMessages.filter((m: any) => m.tool_calls?.length > 0);
                console.log(`[ChatHistory] DIAG mapped: total=${dedupedLoadedMessages.length}, agents=${agentMsgs.length}, withToolCalls=${withTools.length}, firstAgent:`, agentMsgs[0] ? {sender: agentMsgs[0].sender, isAgent: agentMsgs[0].isAgent, tool_calls_len: agentMsgs[0].tool_calls?.length, msg_index: agentMsgs[0].msg_index} : 'none');
                // Populate processedMessageIds so real-time events for these messages are deduped
                dedupedLoadedMessages.forEach((m: any) => {
                  if (m.id && !m.id.startsWith('backend-')) processedMessageIds.add(m.id);
                });
                // Anything already on screen that the backend snapshot does not
                // carry is newer than the snapshot — a live message that landed
                // while this fetch was in flight, or before the chat was opened.
                // Replacing outright would drop it, which never happened before
                // only because a chat with messages was never backfilled at all.
                const merged = mergeBackfillWithLive(dedupedLoadedMessages, map.get(reqChatId) || []);
                newMap.set(reqChatId, merged);
                const kept = merged.length - dedupedLoadedMessages.length;
                console.log(`[ChatHistory] Updated chatHistories with ${dedupedLoadedMessages.length} messages` +
                            (kept ? ` + ${kept} kept from the live stream` : ''));
                return newMap;
              });
              loadedHistory.add(reqChatId);
              setPager(reqChatId, { ...pageFacts(result), stopped: false, loading: false });

              // Update token counter with restored history token counts
              if (result.tokens_used !== undefined && result.token_limit !== undefined && result.token_limit > 0) {
                onUpdateTokenUsage(reqChatId, {
                  used: result.tokens_used,
                  limit: result.token_limit,
                  historyTokens: result.history_tokens ?? 0,
                  tokensAfterLastResponse: result.tokens_after_last_response ?? 0,
                  tokensAfterLastResponseAt: result.tokens_after_last_response_at ?? null,
                  contextAgent: result.context_agent ?? '',
                  contextAgents: result.context_agents ?? null,
                });
              }

              // Remove from loading AFTER chatHistories update completes
              loadingHistory.delete(reqChatId);

              // Scroll to bottom
              setTimeout(() => {
                if (chatWindow) {
                  chatWindow.scrollTop = chatWindow.scrollHeight;
                }
              }, 100);
            } else {
              console.log(`[ChatHistory] No messages: status=${result.status}, count=${result.messages?.length || 0}`);

              // "Loaded and empty" is recorded in loadedHistory, not by writing
              // an empty array over whatever is on screen: that write used to be
              // the flag, and it erased live messages as a side effect. The map
              // is only seeded when the chat has no entry at all, so the list
              // renders as empty rather than undefined.
              chatHistories.update(map => {
                if (map.has(reqChatId)) return map;
                const newMap = new Map(map);
                newMap.set(reqChatId, []);
                return newMap;
              });
              loadedHistory.add(reqChatId);
              // An empty live history right after a reset still has a boundary to show.
              if (result.status === 'success') {
                setPager(reqChatId, { ...pageFacts(result), stopped: false, loading: false });
              }

              // Remove from loading AFTER chatHistories update completes
              loadingHistory.delete(reqChatId);
            }
          } catch (e) {
            console.error(`[ChatHistory] Error loading history:`, e);
            // On error, remove from loading to allow retry
            loadingHistory.delete(reqChatId);
          }
        })();
      } else if (reqChatId.startsWith('agent_') && !loadingHistory.has(reqChatId) && untrack(() => !hasTokenUsage(reqChatId))) {
        // History is on screen (AgentPanel loads every agent's at connect) but the
        // token counter needs refreshing: tokenUsageMap is empty after a restart,
        // so the counter stays at 0 unless we fetch token data.
        console.log(`[ChatHistory] Agent history cached but no token data - fetching from backend for ${reqChatId.slice(0,20)}`);
        loadingHistory.add(reqChatId);
        (async () => {
          try {
            // Only the token fields are read, and they describe the whole chat.
            const result = await sendCommand('get_conversation_history', { conversation_id: reqChatId, limit: 1 });
            if (result.tokens_used !== undefined && result.token_limit !== undefined && result.token_limit > 0) {
              onUpdateTokenUsage(reqChatId, {
                used: result.tokens_used,
                limit: result.token_limit,
                historyTokens: result.history_tokens ?? 0,
                tokensAfterLastResponse: result.tokens_after_last_response ?? 0,
                tokensAfterLastResponseAt: result.tokens_after_last_response_at ?? null,
              });
              console.log(`[ChatHistory] Token counter refreshed for ${reqChatId.slice(0,20)}: ${result.tokens_used}/${result.token_limit}`);
            }
          } catch (e) {
            console.error(`[ChatHistory] Error fetching token usage for ${reqChatId.slice(0,20)}:`, e);
          } finally {
            loadingHistory.delete(reqChatId);
          }
        })();
      } else {
        console.log(`[ChatHistory] Skipping load - already fetched once, have ${currentHistory?.length ?? 0} messages`);
      }
    }
  });
</script>

<!-- No markup — logic-only panel -->
