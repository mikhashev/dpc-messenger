<!-- src/lib/panels/ChatHistorySyncPanel.svelte -->
<!-- Syncs chat history from backend when switching to a peer/agent/group chat with no messages. -->
<!-- Handles page-refresh scenario: frontend loses chatHistories, backend keeps conversation_monitors. -->
<!-- Logic-only panel — no markup, no styles. -->

<script lang="ts">
  import type { Writable } from 'svelte/store';
  import { connectionStatus, sendCommand } from '$lib/coreService';
  import { mapBackendMessage } from '$lib/utils/messageMapper';
  import { mergeBackfillWithLive } from '$lib/utils/liveMessageIdentity';
  import { onMount, untrack } from 'svelte';

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

  // ---------------------------------------------------------------------------
  // Reactive: Sync chat history from backend when switching to peer chat with no messages (v0.11.2)
  // ---------------------------------------------------------------------------
  $effect(() => {
    if ($connectionStatus === 'connected' && canLoadHistory && activeChatId && activeChatId !== 'local_ai' && !activeChatId.startsWith('ai_')) {
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
            const result = await sendCommand('get_conversation_history', { conversation_id: reqChatId });
            console.log(`[ChatHistory] Backend response:`, result);
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
                // See HistorySyncPanel: an undated record borrows the time of
                // the one before it rather than the clock.
                let previousTimestamp: number | undefined;
                const loadedMessages = result.messages.map((msg: any, index: number) => {
                  if (index === 0) console.log(`[ChatHistory] DIAG first msg keys:`, Object.keys(msg), `tool_calls:`, msg.tool_calls?.length, `sender_type:`, msg.sender_type, `msg_index:`, msg.msg_index);
                  const fallbackSender = msg.sender_node_id || (msg.role === 'user' ? 'user' : reqChatId);
                  const fallbackName = msg.sender_name || (msg.role === 'user' ? 'You' : getPeerDisplayName(reqChatId));
                  const mapped = mapBackendMessage(msg, {
                    fallbackSender,
                    fallbackSenderName: fallbackName,
                    index,
                    totalCount: result.messages.length,
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
                const agentMsgs = loadedMessages.filter((m: any) => m.isAgent);
                const withTools = loadedMessages.filter((m: any) => m.tool_calls?.length > 0);
                console.log(`[ChatHistory] DIAG mapped: total=${loadedMessages.length}, agents=${agentMsgs.length}, withToolCalls=${withTools.length}, firstAgent:`, agentMsgs[0] ? {sender: agentMsgs[0].sender, isAgent: agentMsgs[0].isAgent, tool_calls_len: agentMsgs[0].tool_calls?.length, msg_index: agentMsgs[0].msg_index} : 'none');
                // Populate processedMessageIds so real-time events for these messages are deduped
                loadedMessages.forEach((m: any) => {
                  if (m.id && !m.id.startsWith('backend-')) processedMessageIds.add(m.id);
                });
                // Anything already on screen that the backend snapshot does not
                // carry is newer than the snapshot — a live message that landed
                // while this fetch was in flight, or before the chat was opened.
                // Replacing outright would drop it, which never happened before
                // only because a chat with messages was never backfilled at all.
                const merged = mergeBackfillWithLive(loadedMessages, map.get(reqChatId) || []);
                newMap.set(reqChatId, merged);
                const kept = merged.length - loadedMessages.length;
                console.log(`[ChatHistory] Updated chatHistories with ${loadedMessages.length} messages` +
                            (kept ? ` + ${kept} kept from the live stream` : ''));
                return newMap;
              });
              loadedHistory.add(reqChatId);

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
        // History was restored from localStorage but token counter needs refreshing.
        // This happens after a full app restart: localStorage provides the messages but
        // tokenUsageMap is empty, so the counter stays at 0 unless we fetch token data.
        console.log(`[ChatHistory] Agent history cached but no token data - fetching from backend for ${reqChatId.slice(0,20)}`);
        loadingHistory.add(reqChatId);
        (async () => {
          try {
            const result = await sendCommand('get_conversation_history', { conversation_id: reqChatId });
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
