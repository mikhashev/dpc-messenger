<!-- src/lib/panels/ChatHistorySyncPanel.svelte -->
<!-- Syncs chat history from backend when switching to a peer/agent/group chat with no messages. -->
<!-- Handles page-refresh scenario: frontend loses chatHistories, backend keeps conversation_monitors. -->
<!-- Logic-only panel — no markup, no styles. -->

<script lang="ts">
  import type { Writable } from 'svelte/store';
  import { connectionStatus, sendCommand } from '$lib/coreService';
  import { untrack } from 'svelte';

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
  }: {
    activeChatId: string;
    chatHistories: Writable<Map<string, any[]>>;
    loadingHistory: Set<string>;
    processedMessageIds: Set<string>;
    chatWindow: HTMLElement | undefined;
    getPeerDisplayName: (id: string) => string;
    onUpdateTokenUsage: (chatId: string, usage: { used: number; limit: number; historyTokens?: number; contextEstimated?: number }) => void;
    hasTokenUsage: (chatId: string) => boolean;
  } = $props();

  // ---------------------------------------------------------------------------
  // Reactive: Sync chat history from backend when switching to peer chat with no messages (v0.11.2)
  // ---------------------------------------------------------------------------
  $effect(() => {
    if ($connectionStatus === 'connected' && activeChatId && activeChatId !== 'local_ai' && !activeChatId.startsWith('ai_')) {
      // Check if this peer chat has no messages in frontend
      const currentHistory = $chatHistories.get(activeChatId);
      console.log(`[ChatHistory] Reactive triggered: chatId=${activeChatId.slice(0,20)}, historyLen=${currentHistory?.length || 0}, loading=${loadingHistory.has(activeChatId)}`);

      // Guard: Skip if already loading or already have messages
      if (loadingHistory.has(activeChatId)) {
        console.log(`[ChatHistory] Skipping - already loading history for ${activeChatId.slice(0,20)}`);
      } else if (currentHistory === undefined) {
        console.log(`[ChatHistory] Loading history from backend for ${activeChatId.slice(0,20)}...`);

        // Mark as loading to prevent re-triggers
        loadingHistory.add(activeChatId);

        // Load from backend (async IIFE to allow await in reactive statement)
        (async () => {
          try {
            const result = await sendCommand('get_conversation_history', { conversation_id: activeChatId });
            console.log(`[ChatHistory] Backend response:`, result);
            if (result.status === 'success' && result.messages && result.messages.length > 0) {
              console.log(`[ChatHistory] Loaded ${result.message_count} messages from backend`);

              // Convert backend format to frontend format (v0.15.3: use backend metadata)
              chatHistories.update(map => {
                const newMap = new Map(map);
                const loadedMessages = result.messages.map((msg: any, index: number) => {
                  // Use backend's timestamp if available (ISO format), otherwise generate fake timestamp
                  let timestamp;
                  if (msg.timestamp) {
                    // Parse ISO timestamp to Date (milliseconds)
                    timestamp = new Date(msg.timestamp).getTime();
                  } else {
                    // Fallback to fake timestamp (sequential from now)
                    timestamp = Date.now() - (result.messages.length - index) * 1000;
                  }

                  // Use backend's sender info if available, otherwise fallback to role-based logic
                  let sender;
                  let senderName;
                  if (msg.sender_node_id) {
                    sender = msg.sender_node_id;
                    senderName = msg.sender_name || (msg.role === 'user' ? 'You' : getPeerDisplayName(activeChatId));
                  } else {
                    // Fallback for messages without sender info (old format)
                    sender = msg.role === 'user' ? 'user' : activeChatId;
                    senderName = msg.role === 'user' ? 'You' : getPeerDisplayName(activeChatId);
                  }

                  const stableId = msg.message_id || `backend-${index}-${Date.now()}`;
                  return {
                    id: stableId,
                    sender: sender,
                    senderName: senderName,
                    text: msg.content,
                    timestamp: timestamp,
                    attachments: msg.attachments || []
                  };
                });
                // Populate processedMessageIds so real-time events for these messages are deduped
                loadedMessages.forEach((m: any) => {
                  if (m.id && !m.id.startsWith('backend-')) processedMessageIds.add(m.id);
                });
                newMap.set(activeChatId, loadedMessages);
                console.log(`[ChatHistory] Updated chatHistories with ${loadedMessages.length} messages`);
                return newMap;
              });

              // Update token counter with restored history token counts
              if (result.tokens_used !== undefined && result.token_limit !== undefined && result.token_limit > 0) {
                onUpdateTokenUsage(activeChatId, {
                  used: result.tokens_used,
                  limit: result.token_limit,
                  historyTokens: result.history_tokens ?? 0,
                  contextEstimated: result.context_estimated ?? 0,
                });
              }

              // Remove from loading AFTER chatHistories update completes
              loadingHistory.delete(activeChatId);

              // Scroll to bottom
              setTimeout(() => {
                if (chatWindow) {
                  chatWindow.scrollTop = chatWindow.scrollHeight;
                }
              }, 100);
            } else {
              console.log(`[ChatHistory] No messages: status=${result.status}, count=${result.messages?.length || 0}`);

              // Initialize with empty array to mark as "loaded but empty"
              // This prevents infinite re-loading when chatHistories updates trigger reactive statement
              chatHistories.update(map => {
                const newMap = new Map(map);
                newMap.set(activeChatId, []);
                return newMap;
              });

              // Remove from loading AFTER chatHistories update completes
              loadingHistory.delete(activeChatId);
            }
          } catch (e) {
            console.error(`[ChatHistory] Error loading history:`, e);
            // On error, remove from loading to allow retry
            loadingHistory.delete(activeChatId);
          }
        })();
      } else if (activeChatId.startsWith('agent_') && !loadingHistory.has(activeChatId) && untrack(() => !hasTokenUsage(activeChatId))) {
        // History was restored from localStorage but token counter needs refreshing.
        // This happens after a full app restart: localStorage provides the messages but
        // tokenUsageMap is empty, so the counter stays at 0 unless we fetch token data.
        console.log(`[ChatHistory] Agent history cached but no token data - fetching from backend for ${activeChatId.slice(0,20)}`);
        loadingHistory.add(activeChatId);
        (async () => {
          try {
            const result = await sendCommand('get_conversation_history', { conversation_id: activeChatId });
            if (result.tokens_used !== undefined && result.token_limit !== undefined && result.token_limit > 0) {
              onUpdateTokenUsage(activeChatId, {
                used: result.tokens_used,
                limit: result.token_limit,
                historyTokens: result.history_tokens ?? 0,
                contextEstimated: result.context_estimated ?? 0,
              });
              console.log(`[ChatHistory] Token counter refreshed for ${activeChatId.slice(0,20)}: ${result.tokens_used}/${result.token_limit}`);
            }
          } catch (e) {
            console.error(`[ChatHistory] Error fetching token usage for ${activeChatId.slice(0,20)}:`, e);
          } finally {
            loadingHistory.delete(activeChatId);
          }
        })();
      } else {
        console.log(`[ChatHistory] Skipping load - already have ${currentHistory.length} messages`);
      }
    }
  });
</script>

<!-- No markup — logic-only panel -->
