<!-- src/lib/panels/HistorySyncPanel.svelte -->
<!-- Chat history restoration effects panel (Phase 3 Step 8) -->
<!-- Logic-only panel — no markup, no styles. -->
<!-- Manages: $historyRestored, $groupHistorySynced effects -->

<script lang="ts">
  import type { Writable } from 'svelte/store';
  import { get } from 'svelte/store';
  import { mapBackendMessage } from '$lib/utils/messageMapper';
  import {
    historyRestored,
    groupHistorySynced,
    sendCommand,
  } from '$lib/coreService';

  // ---------------------------------------------------------------------------
  // Props
  // ---------------------------------------------------------------------------
  let {
    activeChatId,
    chatHistories,
    chatWindow,
    processedMessageIds,
    getPeerDisplayName,
    onAgentToast,
    selfNodeId = "",
  }: {
    activeChatId: string;
    chatHistories: Writable<Map<string, any[]>>;
    chatWindow: HTMLElement | null;
    processedMessageIds: Set<string>;
    getPeerDisplayName: (id: string) => string;
    onAgentToast: (message: string, type: 'info' | 'warning' | 'error') => void;
    selfNodeId?: string;
  } = $props();

  // ---------------------------------------------------------------------------
  // Effects
  // ---------------------------------------------------------------------------

  // Handle chat history restored from backend (v0.11.2)
  $effect(() => {
    if ($historyRestored) {
      console.log(`Restoring ${$historyRestored.message_count} messages to chat with ${$historyRestored.conversation_id}`);

      chatHistories.update(map => {
        const newMap = new Map(map);
        const restoredMessages = $historyRestored.messages.map((msg: any, index: number) => {
          const isSelf = msg.sender_node_id ? msg.sender_node_id === selfNodeId : msg.role === 'user';
          return {
            id: `restored-${index}-${Date.now()}`,
            sender: isSelf ? 'user' : ($historyRestored.conversation_id),
            senderName: isSelf ? (msg.sender_name || 'You') : (msg.sender_name || getPeerDisplayName($historyRestored.conversation_id)),
            text: msg.content,
            timestamp: Date.now() - ($historyRestored.messages.length - index) * 1000,
            attachments: msg.attachments || []
          };
        });
        newMap.set($historyRestored.conversation_id, restoredMessages);
        return newMap;
      });

      setTimeout(() => {
        if (chatWindow) chatWindow.scrollTop = chatWindow.scrollHeight;
      }, 100);

      onAgentToast(`✓ Chat history restored: ${$historyRestored.message_count} messages`, 'info');
    }
  });

  // Handle group history synced via P2P (v0.20.0)
  $effect(() => {
    if ($groupHistorySynced && $groupHistorySynced.group_id) {
      const syncedGroupId = $groupHistorySynced.group_id;
      const messageCount = $groupHistorySynced.message_count || 0;
      console.log(`[GroupHistorySync] Group ${syncedGroupId} synced with ${messageCount} messages`);

      // The event says this group's stored history changed; the backend holds the
      // truth, so reload it whether or not the group is on screen. Gating on the
      // active chat left a merged message invisible until a restart, and
      // message_count is the *peer's* count, so comparing it with ours skipped
      // reloads whenever the peer held fewer messages than we did.
      const isActive = activeChatId === syncedGroupId;

      console.log(`[GroupHistorySync] Reloading history for group ${syncedGroupId}`);

      (async () => {
        try {
          const response = await sendCommand('get_conversation_history', { conversation_id: syncedGroupId });
          if (response.status === 'success' && response.messages?.length > 0) {
            console.log(`[GroupHistorySync] Loaded ${response.messages.length} messages from backend`);

            chatHistories.update(map => {
              const newMap = new Map(map);
              // Carried forward so a record stored without a timestamp keeps
              // its place instead of being dated from the clock and sorting to
              // the end — which it did again on every reload.
              let previousTimestamp: number | undefined;
              const syncedMessages = response.messages.map((msg: any, index: number) => {
                const isAgent = msg.sender_type === 'agent' || msg.is_agent || false;
                const isLocalHuman = !isAgent && (!msg.sender_node_id || msg.sender_node_id === selfNodeId);
                const mapped = mapBackendMessage(msg, {
                  fallbackSender: isLocalHuman ? 'user' : (msg.sender_node_id || msg.node_id || syncedGroupId),
                  fallbackSenderName: isLocalHuman ? 'You' : (msg.sender_name || getPeerDisplayName(msg.sender_node_id || syncedGroupId)),
                  index,
                  totalCount: response.messages.length,
                  previousTimestamp,
                });
                previousTimestamp = mapped.timestamp;
                mapped.id = msg.message_id || msg.id || `synced-${index}-${Date.now()}`;
                return mapped;
              });

              syncedMessages.forEach((m: any) => {
                if (m.id && !m.id.startsWith('synced-')) processedMessageIds.add(m.id);
              });

              const backendIds = new Set(syncedMessages.map((m: any) => m.id).filter(Boolean));
              const existingMsgs = map.get(syncedGroupId) || [];
              const frontendOnly = existingMsgs.filter((m: any) => m.id && !backendIds.has(m.id));
              const merged = [...syncedMessages, ...frontendOnly].sort((a: any, b: any) => a.timestamp - b.timestamp);
              newMap.set(syncedGroupId, merged);
              return newMap;
            });

            if (isActive) {
              onAgentToast(`✓ Group history synced: ${response.messages.length} messages`, 'info');

              setTimeout(() => {
                if (chatWindow) chatWindow.scrollTop = chatWindow.scrollHeight;
              }, 100);
            }
          }
        } catch (err: any) {
          console.error('[GroupHistorySync] Error loading synced history:', err);
        }
      })();
    }
  });
</script>

<!-- No markup — logic-only panel -->
