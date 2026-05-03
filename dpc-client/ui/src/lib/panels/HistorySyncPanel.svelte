<!-- src/lib/panels/HistorySyncPanel.svelte -->
<!-- Chat history restoration effects panel (Phase 3 Step 8) -->
<!-- Logic-only panel — no markup, no styles. -->
<!-- Manages: $historyRestored, $groupHistorySynced effects -->

<script lang="ts">
  import type { Writable } from 'svelte/store';
  import { get } from 'svelte/store';
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
  }: {
    activeChatId: string;
    chatHistories: Writable<Map<string, any[]>>;
    chatWindow: HTMLElement | null;
    processedMessageIds: Set<string>;
    getPeerDisplayName: (id: string) => string;
    onAgentToast: (message: string, type: 'info' | 'warning' | 'error') => void;
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
        const restoredMessages = $historyRestored.messages.map((msg: any, index: number) => ({
          id: `restored-${index}-${Date.now()}`,
          sender: msg.role === 'user' ? 'user' : $historyRestored.conversation_id,
          senderName: msg.role === 'user' ? 'You' : getPeerDisplayName($historyRestored.conversation_id),
          text: msg.content,
          timestamp: Date.now() - ($historyRestored.messages.length - index) * 1000,
          attachments: msg.attachments || []
        }));
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

      // Only reload if this is the active chat AND backend has more messages than we do
      const existingCount = get(chatHistories).get(syncedGroupId)?.length || 0;

      if (activeChatId === syncedGroupId && messageCount > existingCount) {
        console.log(`[GroupHistorySync] Reloading history for active group ${syncedGroupId}`);

        (async () => {
          try {
            const response = await sendCommand('get_conversation_history', { conversation_id: syncedGroupId });
            if (response.status === 'success' && response.messages?.length > 0) {
              console.log(`[GroupHistorySync] Loaded ${response.messages.length} messages from backend`);

              chatHistories.update(map => {
                const newMap = new Map(map);
                const syncedMessages = response.messages.map((msg: any, index: number) => {
                  const stableId = msg.message_id || msg.id || `synced-${index}-${Date.now()}`;
                  const senderName = msg.sender_name || '';
                  const isAgent = senderName !== 'User' && senderName !== '' && senderName !== 'You';
                  const isUser = !isAgent;
                  return {
                    id: stableId,
                    sender: isUser ? 'user' : (msg.sender_node_id || msg.node_id || syncedGroupId),
                    senderName: isUser ? 'You' : senderName,
                    text: msg.content || msg.text,
                    timestamp: new Date(msg.timestamp).getTime() || Date.now() - (response.messages.length - index) * 1000,
                    attachments: msg.attachments || [],
                    isAgent: isAgent,
                  };
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

              onAgentToast(`✓ Group history synced: ${response.messages.length} messages`, 'info');

              setTimeout(() => {
                if (chatWindow) chatWindow.scrollTop = chatWindow.scrollHeight;
              }, 100);
            }
          } catch (err: any) {
            console.error('[GroupHistorySync] Error loading synced history:', err);
          }
        })();
      }
    }
  });
</script>

<!-- No markup — logic-only panel -->
