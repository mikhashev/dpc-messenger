<!-- src/lib/panels/MessageRouterPanel.svelte -->
<!-- Incoming message routing panel (Phase 3 Step 8) -->
<!-- Logic-only panel — no markup, no styles. -->
<!-- Manages: $p2pMessages, $groupTextReceived, $groupFileReceived, $aiResponseWithImage,         -->
<!--           $coreMessages (execute_ai_query response) routing effects                          -->

<script lang="ts">
  import type { Writable } from 'svelte/store';
  import {
    p2pMessages,
    groupTextReceived,
    groupFileReceived,
    aiResponseWithImage,
    coreMessages,
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
    commandToChatMap,
    persistCommandToChatMap,
    currentContextHash,
    aiChats,
    onSetChatLoading,
    onUpdateTokenUsage,
    onMarkContextSent,
    onAgentToast,
    getStreamingText,
  }: {
    activeChatId: string;
    chatHistories: Writable<Map<string, any[]>>;
    chatWindow: HTMLElement | null;
    processedMessageIds: Set<string>;
    commandToChatMap: Map<string, string>;
    persistCommandToChatMap?: () => void;
    currentContextHash: string;
    aiChats: Writable<Map<string, any>>;
    onSetChatLoading: (chatId: string, loading: boolean) => void;
    onUpdateTokenUsage: (chatId: string, usage: { used: number; limit: number; historyTokens?: number; contextEstimated?: number }) => void;
    onMarkContextSent: (chatId: string, hash: string) => void;
    onAgentToast: (message: string, type: 'info' | 'warning' | 'error') => void;
    getStreamingText: () => string;
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

  // AI query response handler ($coreMessages execute_ai_query)
  $effect(() => {
    if ($coreMessages?.id) {
      const message = $coreMessages;
      const messageId = message.id;

      if (message.command === 'execute_ai_query') {
        // Guard: Skip if already processed (prevents reactive loops in Svelte 5)
        if (processedMessageIds.has(messageId)) {
          console.log(`[execute_ai_query] Skipping already processed message: ${messageId}`);
          return;
        }
        processedMessageIds.add(messageId);

        // Flush pending buffer and capture streaming text (AgentPanel owns buffer + state)
        const capturedStreamingText = getStreamingText();

        // CC pending: @CC mention was broadcast to MCP bridge, response comes later
        // via agent_chat_message — just remove the "Thinking..." placeholder
        if (message.status === 'OK' && message.payload.provider === 'cc_pending') {
          const responseCommandId = message.id;
          const chatId = commandToChatMap.get(responseCommandId) || activeChatId;
          if (chatId) {
            onSetChatLoading(chatId, false);
            // Remove the "Thinking..." placeholder entirely
            chatHistories.update(h => {
              const newMap = new Map(h);
              const hist = newMap.get(chatId) || [];
              newMap.set(chatId, hist.filter((m: any) => m.commandId !== responseCommandId));
              return newMap;
            });
            commandToChatMap.delete(responseCommandId);
            persistCommandToChatMap?.();
          }
          return;
        }

        const newText = message.status === 'OK'
          ? message.payload.content
          : `Error: ${message.payload?.message || 'Unknown error'}`;
        // CC auto-responses use provider="cc" — render as 'cc' sender (not 'ai')
        const newSender = message.status === 'OK'
          ? (message.payload.provider === 'cc' ? 'cc' : 'ai')
          : 'system';
        const modelName = message.status === 'OK' ? message.payload.model : undefined;
        // v1.4+: Extract thinking fields for reasoning models
        const thinkingContent = message.status === 'OK' ? message.payload.thinking : undefined;
        const thinkingTokenCount = message.status === 'OK' ? message.payload.thinking_tokens : undefined;

        // Show toast notification for errors (helps remote users see host failures)
        if (message.status !== 'OK') {
          console.error(`[TokenCounter] AI query failed: ${message.payload?.message}`);
          onAgentToast(`⚠️ AI Query Failed: ${message.payload?.message || 'Unknown error'}`, 'error');
        }

        const responseCommandId = message.id;

        // Find which chat this command belongs to
        let chatId = commandToChatMap.get(responseCommandId);

        // Debug: Log if chatId not found (helps diagnose race conditions)
        if (!chatId) {
          console.warn(`[execute_ai_query] No chatId found for commandId=${responseCommandId}, using activeChatId=${activeChatId} as fallback`);
          chatId = activeChatId;
        }

        // Clear loading state for the specific chat that received the response
        if (chatId) {
          onSetChatLoading(chatId, false);
          console.log(`[TokenCounter] Loading cleared for chatId=${chatId}`);
        }

        if (chatId) {
          console.log(`[execute_ai_query] Looking for commandId=${responseCommandId} in chatId=${chatId}`);

          chatHistories.update(h => {
            const newMap = new Map(h);
            const hist = newMap.get(chatId) || [];

            const commandIds = hist.filter((m: any) => m.commandId).map((m: any) => m.commandId);
            console.log(`[execute_ai_query] History commandIds:`, commandIds);

            const found = hist.some((m: any) => m.commandId === responseCommandId);
            console.log(`[execute_ai_query] Found matching message: ${found}`);

            // For CC auto-responses, use 'CC'; for agent chats, use the agent's display name
            const agentSenderName = newSender === 'cc'
              ? 'CC'
              : (chatId?.startsWith('agent_') ? ($aiChats.get(chatId)?.name || undefined) : undefined);

            newMap.set(chatId, hist.map((m: any) =>
              m.commandId === responseCommandId ? {
                ...m,
                sender: newSender,
                senderName: agentSenderName,
                text: newText,
                model: modelName,
                thinking: thinkingContent,
                thinkingTokens: thinkingTokenCount,
                streamingRaw: capturedStreamingText || undefined,
                commandId: undefined
              } : m
            ));
            return newMap;
          });

          // Update token usage map with data from response (Phase 2)
          if (message.status === 'OK' && message.payload.tokens_used !== undefined && message.payload.token_limit) {
            onUpdateTokenUsage(chatId, {
              used: message.payload.tokens_used,
              limit: message.payload.token_limit,
              historyTokens: message.payload.history_tokens ?? 0,
              contextEstimated: message.payload.context_estimated ?? 0,
            });
          }

          // Phase 7: Mark context as sent (clears "Updated" status)
          if (message.status === 'OK' && currentContextHash) {
            onMarkContextSent(chatId, currentContextHash);
            console.log(`[Context Sent] Marked context as sent for ${chatId}`);
          }

          // Clean up the command mapping
          commandToChatMap.delete(responseCommandId);
          persistCommandToChatMap?.();
        }

        // Cleanup old processed IDs to prevent memory leak
        if (processedMessageIds.size > 500) {
          const firstId = processedMessageIds.values().next().value;
          if (firstId) processedMessageIds.delete(firstId);
        }

        autoScroll();
      }
    }
  });
</script>

<!-- No markup — logic-only panel -->
