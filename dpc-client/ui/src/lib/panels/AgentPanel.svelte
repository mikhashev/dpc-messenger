<!-- src/lib/panels/AgentPanel.svelte -->
<!-- Agent progress/streaming logic panel (Phase 3 Step 5) -->
<!-- Logic-only panel — no markup, no styles. -->
<!-- Owns: agentProgressMessage, agentProgressTool, agentProgressRound, agentStreamingText -->
<!-- Manages: $agentProgress, $agentProgressClear, $agentTextChunk, $agentHistoryUpdated effects -->

<script lang="ts">
  import type { Writable } from 'svelte/store';
  import { untrack } from 'svelte';
  import {
    agentProgress,
    agentProgressClear,
    agentTextChunk,
    agentHistoryUpdated,
    agentTelegramLinked,
    agentTelegramUnlinked,
    agentsList,
    listAgents,
    sendCommand,
    connectionStatus,
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
    onUpdateTokenUsage: (conversationId: string, usage: { used: number; limit: number }) => void;
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
                  const stableId = `${conv_id}-${msg.timestamp ? ts : index}`;
                  const local = localById.get(stableId);
                  return {
                    id: stableId,
                    sender: msg.role === 'user' ? 'user' : conv_id,
                    senderName: msg.role === 'user' ? (msg.sender_name || 'You') : (agent.name || conv_id),
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
        // NOTE: Do NOT clear streamingBuffer here — it needs to survive until the
        // final response handler captures it as capturedStreamingText via flushAndCapture().
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
      const { conversation_id, messages, tokens_used, token_limit, thinking } = $agentHistoryUpdated;

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

        // Notify parent to update token usage map
        if (tokens_used !== undefined && token_limit !== undefined && token_limit > 0) {
          onUpdateTokenUsage(conversation_id, { used: tokens_used, limit: token_limit });
        }

        chatHistories.update(map => {
          const newMap = new Map(map);
          const existing = map.get(conversation_id) || [];
          // Preserve any pending DPC execute_ai_query placeholders
          const pendingMsgs = existing.filter((m: any) => m.commandId);

          const mappedMessages = (messages || []).map((msg: any, index: number) => {
            const isUser = msg.role === 'user';
            const stableId = `${conversation_id}-${msg.timestamp ? new Date(msg.timestamp).getTime() : index}`;
            return {
              id: stableId,
              sender: isUser ? 'user' : conversation_id,
              senderName: isUser ? (msg.sender_name || 'User') : getPeerDisplayName(conversation_id),
              text: msg.content,
              timestamp: msg.timestamp ? new Date(msg.timestamp).getTime() : Date.now(),
              attachments: msg.attachments || [],
            } as Message;
          });

          // Attach streamingRaw and thinking to the last assistant message
          const lastAssistantIdx = [...mappedMessages].reverse().findIndex(m => m.sender !== 'user');
          if (lastAssistantIdx !== -1) {
            const idx = mappedMessages.length - 1 - lastAssistantIdx;
            mappedMessages[idx] = {
              ...mappedMessages[idx],
              streamingRaw: capturedAgentStreaming || mappedMessages[idx].text || undefined,
              thinking: thinking || undefined,
            };
          }

          newMap.set(conversation_id, [...mappedMessages, ...pendingMsgs]);
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
