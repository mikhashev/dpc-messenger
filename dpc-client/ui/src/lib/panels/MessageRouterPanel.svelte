<!-- src/lib/panels/MessageRouterPanel.svelte -->
<!-- Incoming message routing panel (Phase 3 Step 8) -->
<!-- Logic-only panel — no markup, no styles. -->
<!-- Manages: $p2pMessages, $groupTextReceived, $groupFileReceived, $aiResponseWithImage routing effects -->

<script lang="ts">
  import type { Writable } from 'svelte/store';
  import {
    p2pMessages,
    groupTextReceived,
    groupFileReceived,
    aiResponseWithImage,
  } from '$lib/coreService';
  import { showNotificationIfBackground } from '$lib/notificationService';

  // ---------------------------------------------------------------------------
  // Props
  // ---------------------------------------------------------------------------
  let {
    activeChatId,
    chatHistories,
    chatWindow,
    processedMessageIds,
    onSetChatLoading,
  }: {
    activeChatId: string;
    chatHistories: Writable<Map<string, any[]>>;
    chatWindow: HTMLElement | null;
    processedMessageIds: Set<string>;
    onSetChatLoading: (chatId: string, loading: boolean) => void;
  } = $props();

  // ---------------------------------------------------------------------------
  // Internal helpers
  // ---------------------------------------------------------------------------

  function isNearBottom(element: HTMLElement | null, threshold: number = 150): boolean {
    if (!element) return true;
    return element.scrollHeight - element.scrollTop - element.clientHeight < threshold;
  }

  function autoScroll() {
    requestAnimationFrame(() => {
      if (chatWindow) {
        chatWindow.scrollTop = chatWindow.scrollHeight;
      }
    });
  }

  // ---------------------------------------------------------------------------
  // Effects
  // ---------------------------------------------------------------------------

  // P2P text/file message routing
  $effect(() => {
    if ($p2pMessages) {
      const msg = $p2pMessages;
      const messageId = msg.message_id || `${msg.sender_node_id}-${msg.text}`;

      if (!processedMessageIds.has(messageId)) {
        processedMessageIds.add(messageId);

        const wasNearBottom = isNearBottom(chatWindow);

        chatHistories.update(h => {
          const newMap = new Map(h);
          // For user's own messages (file sends), store in activeChatId
          // For peer messages, store in sender's node_id
          const chatId = msg.sender_node_id === "user" ? activeChatId : msg.sender_node_id;
          const hist = newMap.get(chatId) || [];

          const messageData: any = {
            id: crypto.randomUUID(),
            sender: msg.sender_node_id,
            senderName: msg.sender_name,
            text: msg.text,
            timestamp: Date.now()
          };
          if (msg.attachments && msg.attachments.length > 0) {
            messageData.attachments = msg.attachments;
          }
          newMap.set(chatId, [...hist, messageData]);
          return newMap;
        });

        if (wasNearBottom || activeChatId === msg.sender_node_id || msg.sender_node_id === "user") {
          autoScroll();
        }

        // Send notification if app is in background
        if (msg.sender_node_id !== "user") {
          (async () => {
            const messagePreview = msg.text.length > 50 ? msg.text.slice(0, 50) + '...' : msg.text;
            const notified = await showNotificationIfBackground({
              title: msg.sender_name || msg.sender_node_id.slice(0, 16),
              body: messagePreview
            });
            console.log(`[Notifications] P2P message notification: ${notified ? 'system' : 'skip'}`);
          })();
        }

        if (processedMessageIds.size > 500) {
          const firstId = processedMessageIds.values().next().value;
          if (firstId) processedMessageIds.delete(firstId);
        }
      }
    }
  });

  // Group text message routing (v0.19.0)
  $effect(() => {
    if ($groupTextReceived) {
      const msg = $groupTextReceived;
      const messageId = msg.message_id || `${msg.group_id}-${msg.sender_node_id}-${Date.now()}`;

      if (!processedMessageIds.has(messageId)) {
        processedMessageIds.add(messageId);

        const wasNearBottom = isNearBottom(chatWindow);

        chatHistories.update(h => {
          const newMap = new Map(h);
          const hist = newMap.get(msg.group_id) || [];
          newMap.set(msg.group_id, [...hist, {
            id: crypto.randomUUID(),
            sender: msg.sender_node_id,
            senderName: msg.sender_name,
            text: msg.text,
            timestamp: Date.now(),
            mentions: msg.mentions || []
          }]);
          return newMap;
        });

        if (wasNearBottom || activeChatId === msg.group_id) {
          autoScroll();
        }

        if (processedMessageIds.size > 500) {
          const firstId = processedMessageIds.values().next().value;
          if (firstId) processedMessageIds.delete(firstId);
        }
      }
    }
  });

  // Group file/image/voice message routing (v0.19.0)
  $effect(() => {
    if ($groupFileReceived) {
      const msg = $groupFileReceived;
      const messageId = msg.message_id || `group-file-${msg.group_id}-${Date.now()}`;

      if (!processedMessageIds.has(messageId)) {
        processedMessageIds.add(messageId);

        const wasNearBottom = isNearBottom(chatWindow);

        chatHistories.update(h => {
          const newMap = new Map(h);
          const hist = newMap.get(msg.group_id) || [];
          const messageData: any = {
            id: crypto.randomUUID(),
            sender: msg.sender_node_id,
            senderName: msg.sender_name,
            text: msg.text || "",
            timestamp: Date.now()
          };
          if (msg.attachments && msg.attachments.length > 0) {
            messageData.attachments = msg.attachments;
          }
          newMap.set(msg.group_id, [...hist, messageData]);
          return newMap;
        });

        if (wasNearBottom || activeChatId === msg.group_id) {
          autoScroll();
        }

        if (processedMessageIds.size > 500) {
          const firstId = processedMessageIds.values().next().value;
          if (firstId) processedMessageIds.delete(firstId);
        }
      }
    }
  });

  // AI vision response routing (Phase 2)
  $effect(() => {
    if ($aiResponseWithImage) {
      const response = $aiResponseWithImage;
      onSetChatLoading(response.conversation_id, false);

      chatHistories.update(h => {
        const newMap = new Map(h);
        const hist = newMap.get(response.conversation_id) || [];
        newMap.set(response.conversation_id, [
          ...hist,
          {
            id: crypto.randomUUID(),
            sender: 'ai',
            text: response.response,
            timestamp: Date.now(),
            model: response.model
          }
        ]);
        return newMap;
      });

      autoScroll();
      aiResponseWithImage.set(null);
    }
  });
</script>

<!-- No markup — logic-only panel -->
