<!-- src/lib/panels/AgentPanel.svelte -->
<!-- Agent progress/streaming logic panel (Phase 3 Step 5) -->
<!-- Logic-only panel — no markup, no styles. -->
<!-- Owns: agentProgressMessage, agentProgressTool, agentProgressRound, agentStreamingText -->
<!-- Manages: $agentProgress, $agentProgressClear, $agentTextChunk, $agentHistoryUpdated effects -->

<script lang="ts">
  import { type Writable, get } from 'svelte/store';
  import { untrack } from 'svelte';
  import {
    agentProgress,
    agentProgressClear,
    agentTextChunk,
    agentChatMessage,
    agentHistoryUpdated,
    agentTelegramLinked,
    agentTelegramUnlinked,
    agentsList,
    listAgents,
    sendCommand,
    connectionStatus,
    nodeStatus,
  } from '$lib/coreService';
  import type { Message } from '$lib/types.js';

  // ---------------------------------------------------------------------------
  // Props
  // ---------------------------------------------------------------------------
  let {
    activeChatId,
    agentChatToAgentId,
    chatHistories,
    getPeerDisplayName,
    chatWindow,
    onUpdateTokenUsage,
    onAgentToast,
    onRefreshAgents,
    agentProgressMessage = $bindable<string | null>(null),
    agentProgressTool = $bindable<string | null>(null),
    agentProgressRound = $bindable<number>(0),
    agentStreamingText = $bindable<string>(''),
  }: {
    activeChatId: string;
    agentChatToAgentId: Map<string, string>;
    chatHistories: Writable<Map<string, Message[]>>;
    getPeerDisplayName: (id: string) => string;
    chatWindow: HTMLElement | null;
    onUpdateTokenUsage: (conversationId: string, usage: { used: number; limit: number; historyTokens?: number; contextEstimated?: number }) => void;
    onAgentToast: (message: string, type: 'info' | 'warning' | 'error') => void;
    onRefreshAgents: () => void;
    agentProgressMessage?: string | null;
    agentProgressTool?: string | null;
    agentProgressRound?: number;
    agentStreamingText?: string;
  } = $props();

  // ---------------------------------------------------------------------------
  // Internal state (non-reactive — not exposed)
  // ---------------------------------------------------------------------------
  let lastActiveChatId: string | null = null;
  let streamingBuffer = '';
  let streamingFlushTimeout: ReturnType<typeof setTimeout> | null = null;

  // ---------------------------------------------------------------------------
  // Internal helpers
  // ---------------------------------------------------------------------------

  /**
   * Map backend message fields to frontend sender/senderName.
   * Single source of truth — used by all 3 message-mapping paths
   * (initial load, agentChatMessage, agentHistoryUpdated).
   */
  function mapMessageSender(msg: any, conversationId: string, agentName: string): { sender: string; senderName: string } {
    if (msg.role === 'assistant')
      return { sender: conversationId, senderName: agentName };
    const selfNodeId = $nodeStatus?.node_id || '';
    // Local user: no sender_node_id, or sender_node_id matches own node
    if (!msg.sender_node_id || msg.sender_node_id === selfNodeId)
      return { sender: 'user', senderName: msg.sender_name || 'You' };
    // External participant: CC, future agents, etc.
    return { sender: msg.sender_node_id, senderName: msg.sender_name || msg.sender_node_id };
  }

  function getAgentName(conversationId: string): string {
    return $agentsList?.find((a: any) => a.agent_id === conversationId)?.name || getPeerDisplayName(conversationId);
  }

  /** Clear all streaming state (buffer + state). Called by chat-switch and response handler. */
  function clearAgentStreaming() {
    if (streamingFlushTimeout) {
      clearTimeout(streamingFlushTimeout);
      streamingFlushTimeout = null;
    }
    streamingBuffer = '';
    agentStreamingText = '';
  }

  /**
   * Flush the pending streaming buffer to agentStreamingText, capture the value,
   * then clear all streaming state. Called by +page.svelte's AI response handler
   * via bind:this so it can attach the accumulated raw output to the final message.
   */
  export function flushAndCapture(): string {
    if (streamingBuffer) {
      if (streamingFlushTimeout) {
        clearTimeout(streamingFlushTimeout);
        streamingFlushTimeout = null;
      }
      agentStreamingText += streamingBuffer;
      streamingBuffer = '';
    }
    const captured = agentStreamingText;
    clearAgentStreaming();
    return captured;
  }

  /**
   * Whether conversation_id matches the active chat.
   * Agent chats use an id like "agent_001"; backend sends conversation_id "agent_001".
   */
  function isActiveChatConv(conversation_id: string): boolean {
    return activeChatId === conversation_id ||
           agentChatToAgentId.get(activeChatId) === conversation_id;
  }

  // ---------------------------------------------------------------------------
  // Load agents from backend once connected (v0.19.0+)
  // Uses $effect on connectionStatus instead of onMount to ensure WebSocket is ready.
  // ---------------------------------------------------------------------------

  let agentsLoaded = false;

  $effect(() => {
    if ($connectionStatus !== 'connected' || agentsLoaded) return;
    agentsLoaded = true;

    (async () => {
    try {
      const agentsResult = await listAgents();
      if (agentsResult?.status === 'success' && agentsResult.agents) {
        agentsList.set(agentsResult.agents);
        console.log(`[Agents] Loaded ${agentsResult.agents.length} agents from backend`);

        // Proactively fetch each agent's conversation history from the backend.
        // The backend is authoritative (includes Telegram messages received while app was closed).
        // We merge with any localStorage snapshot so that UI metadata (thinking blocks,
        // raw tool-call output) is preserved for messages that already exist locally.
        // New messages that arrived while the app was closed are added without metadata.
        for (const agent of agentsResult.agents) {
          const conv_id = agent.agent_id;
          try {
            const histResult = await sendCommand('get_conversation_history', { conversation_id: conv_id });
            if (histResult?.status === 'success' && histResult.messages?.length > 0) {
              chatHistories.update(map => {
                const newMap = new Map(map);

                // Build a lookup of existing localStorage messages by stable ID so we can
                // carry over thinking blocks and streamingRaw onto matching backend messages.
                const localHistory: any[] = newMap.get(conv_id) || [];
                const localById = new Map(localHistory.map((m: any) => [m.id, m]));

                const msgs = histResult.messages.map((msg: any, index: number) => {
                  const ts = msg.timestamp ? new Date(msg.timestamp).getTime() : Date.now() - (histResult.messages.length - index) * 1000;
                  // Prefer backend UUID (msg.id) for uniqueness; fall back to timestamp-based ID
                  const stableId = msg.id || `${conv_id}-${msg.timestamp ? ts : index}`;
                  const local = localById.get(stableId);
                  const { sender, senderName } = mapMessageSender(msg, conv_id, agent.name || conv_id);
                  return {
                    id: stableId,
                    sender,
                    senderName,
                    text: msg.content,
                    timestamp: ts,
                    attachments: msg.attachments || [],
                    // Restore rich metadata: prefer persisted history.json fields, fall back to localStorage
                    thinking: msg.thinking || local?.thinking,
                    streamingRaw: msg.streaming_raw || local?.streamingRaw,
                  };
                });
                newMap.set(conv_id, msgs);
                return newMap;
              });
              console.log(`[Agents] Restored ${histResult.message_count} messages for ${conv_id}`);
            }
          } catch (err) {
            console.warn(`[Agents] Failed to load history for ${conv_id}:`, err);
          }
        }
      }
    } catch (error) {
      console.error('[Agents] Failed to load agents:', error);
    }
    })();
  });

  // ---------------------------------------------------------------------------
  // Effects
  // ---------------------------------------------------------------------------

  // DPC Agent progress: Show real-time tool/round progress
  $effect(() => {
    if ($agentProgress) {
      const { conversation_id, message, round, tool_name } = $agentProgress;

      if (isActiveChatConv(conversation_id)) {
        agentProgressMessage = message || null;
        agentProgressTool = tool_name || null;
        agentProgressRound = round || 0;

        // Append tool call activity to streaming buffer (appears in Raw output)
        if (tool_name) {
          streamingBuffer += `\n\u2699 ${tool_name}\u2026\n`;
        } else if (message && (message.startsWith('\u2713') || message.startsWith('\u274c'))) {
          streamingBuffer += `${message}\n`;
        }
      }
    }
  });

  // Clear agent progress when switching chats
  $effect(() => {
    if (activeChatId !== lastActiveChatId) {
      agentProgressMessage = null;
      agentProgressTool = null;
      agentProgressRound = 0;
      clearAgentStreaming();
      lastActiveChatId = activeChatId;
    }
  });

  // Clear progress when agent task completes/fails
  $effect(() => {
    if ($agentProgressClear) {
      const { conversation_id } = $agentProgressClear;
      if (isActiveChatConv(conversation_id)) {
        agentProgressMessage = null;
        agentProgressTool = null;
        agentProgressRound = 0;
        // For chain-triggered responses (CC→@Ark), there's no execute_ai_query
        // response to capture streaming text, so clear it here to prevent
        // the streaming indicator from staying stuck.
        const hist = get(chatHistories).get(conversation_id) || [];
        const hasPendingCommand = hist.some((m: any) => m.commandId);
        if (!hasPendingCommand) {
          clearAgentStreaming();
        }
      }
    }
  });

  // DPC Agent streaming text — throttled chunk accumulation
  $effect(() => {
    if ($agentTextChunk) {
      const { conversation_id, chunk } = $agentTextChunk;
      if (isActiveChatConv(conversation_id)) {
        streamingBuffer += chunk;

        // Throttle state updates — flush buffer to state every 100ms
        if (!streamingFlushTimeout) {
          streamingFlushTimeout = setTimeout(() => {
            agentStreamingText += streamingBuffer;
            streamingBuffer = '';
            streamingFlushTimeout = null;
          }, 100);
        }
      }
    }
  });

  // Cleanup effect: clear streaming state when chat changes
  $effect(() => {
    void activeChatId; // track reactively — suppress unused-var lint
    return () => {
      clearAgentStreaming();
      agentProgressMessage = null;
      agentProgressTool = null;
      agentProgressRound = 0;
    };
  });

  // Handle Telegram→agent history updates (v0.15.0+)
  // CRITICAL: All side-effects wrapped in untrack() — suppresses Svelte's infinite loop
  // detection. Without untrack(), chatHistories.update() subscribes chatHistories as a
  // dependency, causing re-runs on every history change (infinite loop).
  $effect(() => {
    if ($agentHistoryUpdated) {
      const { conversation_id, messages, tokens_used, token_limit, thinking, context_estimated } = $agentHistoryUpdated;

      untrack(() => {
        // Flush pending buffer and capture accumulated streaming text before overwriting history
        let capturedAgentStreaming = '';
        if (isActiveChatConv(conversation_id)) {
          if (streamingBuffer) {
            agentStreamingText += streamingBuffer;
            streamingBuffer = '';
            if (streamingFlushTimeout) { clearTimeout(streamingFlushTimeout); streamingFlushTimeout = null; }
          }
          capturedAgentStreaming = agentStreamingText;
          if (capturedAgentStreaming) clearAgentStreaming();
        }

        // Notify parent to update token usage map (include contextEstimated for Total counter)
        if (tokens_used !== undefined && token_limit !== undefined && token_limit > 0) {
          onUpdateTokenUsage(conversation_id, {
            used: tokens_used,
            limit: token_limit,
            historyTokens: tokens_used,
            contextEstimated: context_estimated || 0,
          });
        }

        chatHistories.update(map => {
          const newMap = new Map(map);
          const existing = map.get(conversation_id) || [];

          // Defence-in-depth: never overwrite UI history with a shorter backend payload (B1 guard).
          const nonPendingExisting = existing.filter((m: any) => !m.commandId);
          if ((messages || []).length < nonPendingExisting.length) {
            return map; // skip — backend has fewer messages than UI
          }

          // Preserve any pending DPC execute_ai_query placeholders
          const pendingMsgs = existing.filter((m: any) => m.commandId);

          // B2 Fix 1: Use backend msg.id for stable IDs (prevents dedup collisions on same-timestamp msgs)
          const mappedMessages = (messages || []).map((msg: any, index: number) => {
            const { sender, senderName } = mapMessageSender(msg, conversation_id, getAgentName(conversation_id));
            const stableId = msg.id || `${conversation_id}-${msg.timestamp ? new Date(msg.timestamp).getTime() : index}`;
            return {
              id: stableId,
              sender,
              senderName,
              text: msg.content,
              timestamp: msg.timestamp ? new Date(msg.timestamp).getTime() : Date.now(),
              attachments: msg.attachments || [],
            } as Message;
          });

          // Attach streamingRaw and thinking to the last assistant message
          const lastAssistantIdx = [...mappedMessages].reverse().findIndex(m => m.sender === conversation_id);
          if (lastAssistantIdx !== -1) {
            const idx = mappedMessages.length - 1 - lastAssistantIdx;
            mappedMessages[idx] = {
              ...mappedMessages[idx],
              streamingRaw: capturedAgentStreaming || mappedMessages[idx].text || undefined,
              thinking: thinking || undefined,
            };
          }

          // B2 Fix 2: Merge backend messages with pending placeholders, sort by timestamp
          // Backend messages are authoritative for content; pending placeholders kept until resolved
          const backendIds = new Set(mappedMessages.map((m: any) => m.id));
          const keptPending = pendingMsgs.filter((m: any) => !backendIds.has(m.id));
          const merged = [...mappedMessages, ...keptPending];
          merged.sort((a: any, b: any) => (a.timestamp || 0) - (b.timestamp || 0));

          newMap.set(conversation_id, merged);
          return newMap;
        });

        // Scroll to bottom if this is the active chat (two rAF calls for layout accuracy)
        if (isActiveChatConv(conversation_id)) {
          requestAnimationFrame(() => requestAnimationFrame(() => {
            if (chatWindow) chatWindow.scrollTop = chatWindow.scrollHeight;
          }));
        }
      });
    }
  });

  // Handle agent chat message (CC responses via send_cc_agent_response,
  // or Ark chain-triggered responses via _invoke_ark_in_agent_chat)
  $effect(() => {
    if ($agentChatMessage) {
      const { conversation_id, message_id, content, sender_name, sender_node_id, timestamp, role, thinking, streaming_raw,
              context_estimated, history_tokens, tokens_limit } = $agentChatMessage;

      untrack(() => {
        chatHistories.update(map => {
          const newMap = new Map(map);
          const existing = newMap.get(conversation_id) || [];
          const { sender, senderName } = mapMessageSender({ role, sender_node_id, sender_name }, conversation_id, getAgentName(conversation_id));
          const stableId = message_id || `${sender}-${timestamp ? new Date(timestamp).getTime() : Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
          const newMsg: Message = {
            id: stableId,
            sender,
            senderName,
            text: content,
            timestamp: timestamp ? new Date(timestamp).getTime() : Date.now(),
            attachments: [],
            thinking: thinking || undefined,
            streamingRaw: streaming_raw || undefined,
          };
          newMap.set(conversation_id, [...existing, newMsg]);
          return newMap;
        });

        // Update token usage from CC message (#4: context_estimated stale after CC messages)
        if (context_estimated && tokens_limit) {
          onUpdateTokenUsage(conversation_id, {
            used: history_tokens || 0,
            limit: tokens_limit,
            historyTokens: history_tokens || 0,
            contextEstimated: context_estimated,
          });
        }

        // Scroll to bottom if active
        if (isActiveChatConv(conversation_id)) {
          requestAnimationFrame(() => requestAnimationFrame(() => {
            if (chatWindow) chatWindow.scrollTop = chatWindow.scrollHeight;
          }));
        }
      });
    }
  });

  // Handle agent Telegram linked event
  $effect(() => {
    if ($agentTelegramLinked) {
      const { agent_id, chat_id } = $agentTelegramLinked;
      console.log(`[AgentTelegram] Agent ${agent_id} linked to Telegram chat ${chat_id}`);
      onAgentToast('Agent linked to Telegram successfully', 'info');
      onRefreshAgents();
    }
  });

  // Handle agent Telegram unlinked event
  $effect(() => {
    if ($agentTelegramUnlinked) {
      const { agent_id } = $agentTelegramUnlinked;
      console.log(`[AgentTelegram] Agent ${agent_id} unlinked from Telegram`);
      onAgentToast('Agent unlinked from Telegram', 'info');
      onRefreshAgents();
    }
  });
</script>

<!-- No markup — logic-only panel -->
