<!-- dpc-client/ui/src/routes/+page.svelte -->
<!-- FIXED VERSION - Proper URI detection for Direct TLS vs WebRTC -->

<script lang="ts">
  import { onMount, onDestroy, untrack } from "svelte";
  import { writable } from "svelte/store";
  import { connectionStatus, nodeStatus, coreMessages, p2pMessages, sendCommand, resetReconnection, connectToCoreService, knowledgeCommitProposal, knowledgeCommitResult, personalContext, tokenWarning, extractionFailure, availableProviders, peerProviders, contextUpdated, peerContextUpdated, firewallRulesUpdated, unreadMessageCounts, resetUnreadCount, setActiveChat, fileTransferOffer, fileTransferProgress, fileTransferComplete, fileTransferCancelled, activeFileTransfers, sendFile, acceptFileTransfer, cancelFileTransfer, sendVoiceMessage, filePreparationStarted, filePreparationProgress, filePreparationCompleted, historyRestored, newSessionProposal, newSessionResult, proposeNewSession, voteNewSession, conversationReset, aiResponseWithImage, defaultProviders, providersList, voiceTranscriptionComplete, voiceTranscriptionReceived, setConversationTranscription, getConversationTranscription, whisperModelLoadingStarted, whisperModelLoaded, whisperModelLoadingFailed, preloadWhisperModel, whisperModelDownloadRequired, whisperModelDownloadStarted, whisperModelDownloadCompleted, whisperModelDownloadFailed, telegramEnabled, telegramConnected, telegramMessageReceived, telegramVoiceReceived, telegramImageReceived, telegramFileReceived, telegramLinkedChats, telegramMessages, sendToTelegram, agentProgress, agentProgressClear, agentTextChunk, agentTelegramLinked, agentTelegramUnlinked, agentHistoryUpdated, groupChats, groupTextReceived, groupFileReceived, groupInviteReceived, groupUpdated, groupMemberLeft, groupDeleted, groupHistorySynced, createGroupChat, sendGroupMessage, sendGroupImage, sendGroupVoiceMessage, sendGroupFile, addGroupMember, removeGroupMember, leaveGroup, deleteGroup, loadGroups, createAgent, listAgents, listAgentProfiles, deleteAgent, agentCreated, agentsList, integrityWarnings } from "$lib/coreService";
  import KnowledgeCommitDialog from "$lib/components/KnowledgeCommitDialog.svelte";
  import NewSessionDialog from "$lib/components/NewSessionDialog.svelte";
  import VoteResultDialog from "$lib/components/VoteResultDialog.svelte";
  import ModelDownloadPanel from "$lib/panels/ModelDownloadPanel.svelte";
  import ContextViewer from "$lib/components/ContextViewer.svelte";
  import InstructionsEditor from "$lib/components/InstructionsEditor.svelte";
  import FirewallEditor from "$lib/components/FirewallEditor.svelte";
  import ProvidersEditor from "$lib/components/ProvidersEditor.svelte";
  import ProviderSelector from "$lib/components/ProviderSelector.svelte";
  import Toast from "$lib/components/Toast.svelte";
  import MarkdownMessage from "$lib/components/MarkdownMessage.svelte";
  import ImageMessage from "$lib/components/ImageMessage.svelte";
  import ChatMessageList from "$lib/components/ChatMessageList.svelte";
  import SessionControls from "$lib/components/SessionControls.svelte";
  import TelegramStatus from "$lib/components/TelegramStatus.svelte";
  import Sidebar from "$lib/components/Sidebar.svelte";
  import NewGroupDialog from "$lib/components/NewGroupDialog.svelte";
  import GroupSettingsDialog from "$lib/components/GroupSettingsDialog.svelte";
  import VoicePlayer from "$lib/components/VoicePlayer.svelte";
  import ChatPanel from "$lib/panels/ChatPanel.svelte";
  import AgentPanel from "$lib/panels/AgentPanel.svelte";
  import VoicePanel from "$lib/panels/VoicePanel.svelte";
  import GroupPanel from "$lib/panels/GroupPanel.svelte";
  import TelegramPanel from "$lib/panels/TelegramPanel.svelte";
  import MessageRouterPanel from "$lib/panels/MessageRouterPanel.svelte";
  import HistorySyncPanel from "$lib/panels/HistorySyncPanel.svelte";
  import ChatHistorySyncPanel from "$lib/panels/ChatHistorySyncPanel.svelte";
  import AddAIChatPanel from "$lib/panels/AddAIChatPanel.svelte";
  import AgentManagementPanel from "$lib/panels/AgentManagementPanel.svelte";
  import GroupManagementPanel from "$lib/panels/GroupManagementPanel.svelte";
  import { showNotificationIfBackground, requestNotificationPermission } from '$lib/notificationService';
  import { estimateConversationUsage } from '$lib/tokenEstimator';

  // Tauri APIs - will be loaded in onMount if in Tauri environment
  let ask: any = null;
  let open: any = null;

  console.log("Full D-PC Messenger loading...");
  
  // --- STATE ---
  type Mention = {
    node_id: string;
    name: string;
    start: number;
    end: number;
  };

  type Message = {
    id: string;
    sender: string;
    senderName?: string;  // Display name for the sender (peer name or model name)
    text: string;
    timestamp: number;
    commandId?: string;
    model?: string;  // AI model name (for AI responses)
    streamingRaw?: string;  // v0.16.0+: Raw streaming text (shown in collapsible)
    mentions?: Mention[];  // @-mentions in group chat messages
    attachments?: Array<{  // File attachments (Week 1) + Images (Phase 2.4) + Voice (v0.13.0)
      type: 'file' | 'image' | 'voice';
      filename: string;
      file_path?: string;  // Full-size image file path (for P2P file transfers)
      size_bytes: number;
      size_mb?: number;
      hash?: string;
      mime_type?: string;
      transfer_id?: string;
      status?: string;
      // Image-specific fields (Phase 2.4):
      dimensions?: { width: number; height: number };
      thumbnail?: string;  // Base64 data URL
      vision_analyzed?: boolean;  // AI chat only: was vision API used?
      vision_result?: string;  // AI chat only: vision analysis text
      // Voice-specific fields (v0.13.0):
      voice_metadata?: {
        duration_seconds: number;
        sample_rate: number;
        channels: number;
        codec: string;
        recorded_at: string;
      };
      // Voice transcription (v0.13.2+):
      transcription?: {
        text: string;
        provider: string;
        transcriber_node_id?: string;
        confidence?: number;
        language?: string;
        timestamp?: string;
        remote_provider_node_id?: string;
      };
    }>;
    isError?: boolean;  // Error message styling (v0.19.2+)
  };
  const chatHistories = writable<Map<string, Message[]>>(new Map([
    ['local_ai', []]
  ]));
  
  let activeChatId = $state('local_ai');
  const activeMessages = $derived($chatHistories.get(activeChatId) ?? []);
  let chatLoadingStates = $state(new Map<string, boolean>());  // Per-chat loading state
  let chatWindow = $state<HTMLElement>();  // Bound to ChatPanel's chatWindowElement
  let chatPanelRef = $state<any>(null);      // Ref to ChatPanel for mention input delegation
  let groupPanelRef = $state<any>(null);     // Ref to GroupPanel for mention autocomplete
  let addAIChatPanelRef = $state<any>(null);         // Ref to AddAIChatPanel for open/openForAgent/handleDelete
  let agentManagementPanelRef = $state<any>(null);   // Ref to AgentManagementPanel for agent handlers
  let groupManagementPanelRef = $state<any>(null);   // Ref to GroupManagementPanel for group handlers

  // Helper to check if a specific chat is loading
  function isChatLoading(chatId: string): boolean {
    return chatLoadingStates.get(chatId) || false;
  }

  // Helper to set loading state for a specific chat
  function setChatLoading(chatId: string, loading: boolean): void {
    chatLoadingStates = new Map(chatLoadingStates).set(chatId, loading);
  }

  // Computed property - checks if current chat is loading
  let isLoading = $derived(isChatLoading(activeChatId));
  let peerInput = $state("");  // RENAMED from peerUri for clarity
  let selectedComputeHost = $state("local");  // "local" or node_id for remote inference
  let selectedRemoteModel = $state("");  // Selected model when using remote compute host
  // Voice state — owned by VoicePanel (Step 6), bound here for chat header template
  let autoTranscribeEnabled = $state(true);
  let whisperModelLoading = $state(false);
  let whisperModelLoadError = $state<string | null>(null);
  let voicePanelComp: VoicePanel | null = $state(null);

  // DPC Agent progress state — owned by AgentPanel (Step 5), bound here for ChatMessageList
  let agentProgressMessage = $state<string | null>(null);
  let agentProgressTool = $state<string | null>(null);
  let agentProgressRound = $state<number>(0);
  let agentStreamingText = $state<string>('');
  // Component ref — used to call flushAndCapture() in the AI response handler
  let agentPanelComp: AgentPanel | null = $state(null);

  // Helper: clear streaming state — delegates to AgentPanel (kept for compatibility)
  function clearAgentStreaming() {
    agentPanelComp?.flushAndCapture(); // discard captured value
  }

  // Dual provider selection (Phase 1: separate text and vision providers)
  // Managed by ProviderSelector component (extracted)
  let selectedTextProvider = $state("");  // Provider for text-only queries
  let selectedVisionProvider = $state("");  // Provider for image queries
  let selectedVoiceProvider = $state("");  // v0.13.0+: Provider for voice transcription

  // Resizable chat panel state (ChatPanel manages resize; +page.svelte owns height for outer div style)
  let chatPanelHeight = $state((() => {
    const saved = localStorage.getItem('chatPanelHeight');
    return saved ? parseInt(saved, 10) : 600;
  })());

  // Store provider selection per chat (chatId -> provider alias)
  const chatProviders = writable<Map<string, string>>(new Map());

  // Store AI chat metadata (chatId -> {name: string, provider: string, instruction_set_name?: string})
  const aiChats = writable<Map<string, {name: string, provider: string, instruction_set_name?: string, profile_name?: string, llm_provider?: string, compute_host?: string}>>(
    new Map([['local_ai', {name: 'Local AI Chat', provider: '', instruction_set_name: 'general'}]])
  );

  // Track which chat each AI command belongs to (commandId -> chatId)
  let commandToChatMap = new Map<string, string>();

  let processedMessageIds = new Set<string>();

  // Knowledge Architecture UI state
  let showContextViewer = $state(false);
  let showInstructionsEditor = $state(false);
  let showFirewallEditor = $state(false);
  let showProvidersEditor = $state(false);
  let showAgentBoard = $state(false);
  let showCommitDialog = $state(false);
  let showNewSessionDialog = $state(false);  // v0.11.3: mutual session approval
  let showNewGroupDialog = $state(false);  // v0.19.0: group chat creation
  // showGroupInviteDialog + pendingGroupInvite moved to GroupPanel.svelte (Step 7)
  let showGroupSettingsDialog = $state(false);  // v0.19.0: group settings/members panel
  // Initialize from localStorage (browser-safe)
  let autoKnowledgeDetection = $state(
    typeof window !== 'undefined' && localStorage.getItem('autoKnowledgeDetection') === 'true'
  );

  // Token tracking state (Phase 2)
  let tokenUsageMap = $state(new Map<string, {used: number, limit: number, historyTokens?: number, contextEstimated?: number}>());
  let showTokenWarning = $state(false);
  let tokenWarningMessage = $state("");

  // Knowledge extraction failure state (Phase 4)
  let showExtractionFailure = $state(false);
  let extractionFailureMessage = $state("");

  // Knowledge commit result notification state
  let showCommitResultToast = $state(false);
  let commitResultMessage = $state("");
  let commitResultType = $state<"info" | "error" | "warning">("info");
  let showVoteResultDialog = $state(false);
  let currentVoteResult = $state<any>(null);

  // Model download dialog state (v0.13.5)
  // Model download state moved to ModelDownloadPanel.svelte (Step 8)

  // Agent operation toast state (v0.19.0+)
  let showAgentToast = $state(false);
  let agentToastMessage = $state("");
  let agentToastType = $state<"info" | "error" | "warning">("info");

  // Add AI Chat dialog state moved to AddAIChatPanel.svelte

  // Map: AI chat ID -> backend agent_id (for agent chats)
  let agentChatToAgentId = $state<Map<string, string>>(new Map());

  // Instruction Sets state
  type InstructionSets = {
    schema_version: string;
    default: string;
    sets: Record<string, {name: string, description: string}>;
  };
  let availableInstructionSets = $state<InstructionSets | null>(null);


  // Markdown rendering toggle (with localStorage persistence)
  let enableMarkdown = $state((() => {
    const saved = localStorage.getItem('enableMarkdown');
    return saved !== null ? saved === 'true' : true; // Default: enabled
  })());

  // Save markdown preference to localStorage when changed
  $effect(() => {
    localStorage.setItem('enableMarkdown', enableMarkdown.toString());
  });

  // Save auto-knowledge detection preference to localStorage when changed
  $effect(() => {
    if (typeof window !== 'undefined') {
      localStorage.setItem('autoKnowledgeDetection', autoKnowledgeDetection.toString());
    }
  });

  // Whisper loading effects moved to VoicePanel.svelte (Step 6)

  // Agent progress and streaming effects moved to AgentPanel.svelte (Step 5)

  // Keep: Restore compute host from agent metadata on chat switch
  (() => {
    const chatMeta = $aiChats.get(activeChatId);
    if (chatMeta?.compute_host) {
      selectedComputeHost = chatMeta.compute_host;
    } else if (activeChatId.startsWith('agent_') && selectedComputeHost !== 'local') {
      selectedComputeHost = 'local';
    }
  });

  // Persist AI chats (including Agent chats) to localStorage for page refresh recovery
  // Debounced to avoid excessive writes
  let aiChatsSaveTimeout: ReturnType<typeof setTimeout> | null = null;
  $effect(() => {
    // Track aiChats reactively
    $aiChats;

    // Clear any pending save
    if (aiChatsSaveTimeout) {
      clearTimeout(aiChatsSaveTimeout);
    }

    // Debounce save by 500ms
    aiChatsSaveTimeout = setTimeout(() => {
      try {
        // Only save AI chats and agent chats (excluding Telegram and local_ai)
        const aiChatsToSave = Object.fromEntries(
          Array.from($aiChats.entries())
            .filter(([id, info]) =>
              (id.startsWith('ai_') || id.startsWith('agent_') || id === 'default') &&
              !id.startsWith('telegram-') &&
              info.provider !== 'telegram'
            )
        );
        localStorage.setItem('dpc-ai-chats', JSON.stringify(aiChatsToSave));
        console.log(`[AI Chats] Persisted ${Object.keys(aiChatsToSave).length} AI chats to localStorage`);
      } catch (error) {
        console.error('[AI Chats] Failed to persist chats:', error);
      }
    }, 500);
  });

  // Persist AI chat histories to localStorage for page refresh recovery
  // Debounced to avoid excessive writes
  let chatHistoriesSaveTimeout: ReturnType<typeof setTimeout> | null = null;
  $effect(() => {
    // Track chatHistories reactively
    $chatHistories;

    // Clear any pending save
    if (chatHistoriesSaveTimeout) {
      clearTimeout(chatHistoriesSaveTimeout);
    }

    // Debounce save by 500ms
    chatHistoriesSaveTimeout = setTimeout(() => {
      try {
        // Save histories for AI chats and agent chats (excluding Telegram).
        // Agent chats are also included so that thinking blocks and raw tool-call
        // output survive UI/app restarts. On startup, backend messages are merged
        // onto the localStorage snapshot so new Telegram messages received while
        // the app was closed are picked up without losing the UI metadata.
        const historiesToSave = Object.fromEntries(
          Array.from($chatHistories.entries())
            .filter(([id, _]) => (id.startsWith('ai_') || id.startsWith('agent_')) && !id.startsWith('telegram-'))
        );
        localStorage.setItem('dpc-ai-chat-histories', JSON.stringify(historiesToSave));
      } catch (error) {
        console.error('[AI Chats] Failed to persist chat histories:', error);
      }
    }, 500);
  });

  // Telegram message effects moved to TelegramPanel.svelte (Step 8)

  // Phase 7: Context hash tracking for "Updated" status indicators
  let currentContextHash = $state("");  // Current hash from backend (when context is saved)
  let lastSentContextHash = $state(new Map<string, string>());  // Per-conversation: last hash sent to AI
  let peerContextHashes = $state(new Map<string, string>());  // Per-peer: current hash from backend
  let lastSentPeerHashes = $state(new Map<string, Map<string, string>>());  // Per-conversation, per-peer: last hash sent

  // Connection state (Phase 2: UX improvements)
  let isConnecting = $state(false);
  let connectionError = $state("");
  let showConnectionError = $state(false);

  // UI collapse states
  let modeSectionCollapsed = $state(true);  // Mode section collapsible (collapsed by default)
  let chatHeaderCollapsed = $state(false);  // Chat header collapsible

  // Notification state
  let windowFocused = $state(true);
  let showNotificationPermissionDialog = $state(false);

  // Chat history loading state (prevent infinite loop)
  let loadingHistory = new Set<string>();

  // Window focus tracking cleanup
  let unlistenFocus: (() => void) | null = null;

  // Initialize window focus tracking and notification permission (runs once on mount)
  onMount(async () => {
    // Detect if running in Tauri (official method for Tauri 2.x)
    const isTauri = typeof window !== 'undefined' && (
      (window as any).isTauri === true ||  // Tauri 2.x official detection
      !!(window as any).__TAURI__           // Fallback for older versions
    );
    console.log(`[Environment] Detected: ${isTauri ? 'Tauri' : 'Browser'} mode`);

    // Load Tauri dialog APIs if in Tauri environment
    if (isTauri) {
      try {
        const dialog = await import('@tauri-apps/plugin-dialog');
        ask = dialog.ask;
        open = dialog.open;
        console.log('[Tauri] Dialog API loaded');
      } catch (err) {
        console.error('[Tauri] Failed to load dialog API:', err);
      }
    }

    if (typeof window !== 'undefined') {
      try {
        // Load window tracking API if in Tauri
        const { getCurrentWindow } = await import('@tauri-apps/api/window');
        const appWindow = getCurrentWindow();

        // Listen to focus changes (store unlisten function for cleanup)
        unlistenFocus = await appWindow.onFocusChanged(({ payload: focused }) => {
          windowFocused = focused;
          console.log(`[Notifications] Window focus changed: ${focused}`);
        });

        // Check initial focus state
        windowFocused = await appWindow.isFocused();
      } catch (error) {
        console.log('[Notifications] Window tracking not available (running in browser)');
        // In browser, assume window is always focused
        windowFocused = true;
      }
    }

    // Load instruction sets for conversation creation dialog
    try {
      const result = await sendCommand('get_instructions', {});
      if (result && result.status === 'success') {
        availableInstructionSets = result.instruction_sets;
      }
    } catch (error) {
      console.error('Failed to load instruction sets:', error);
    }

    // Restore Telegram chats from localStorage (for page refresh recovery)
    try {
      const savedTelegramChats = localStorage.getItem('dpc-telegram-chats');
      if (savedTelegramChats) {
        const telegramChats = JSON.parse(savedTelegramChats);
        let restoredCount = 0;

        aiChats.update(chats => {
          const newMap = new Map(chats);
          for (const [id, info] of Object.entries(telegramChats)) {
            // Only restore if not already in aiChats
            if (!newMap.has(id)) {
              newMap.set(id, info as { name: string; provider: string; instruction_set_name?: string });
              restoredCount++;
            }
          }
          return newMap;
        });

        // Also populate telegramLinkedChats by extracting chat_id from conversation_id
        // Format: telegram-{chat_id} -> {chat_id}
        const restoredLinks: Record<string, string> = {};
        for (const conversationId of Object.keys(telegramChats)) {
          if (conversationId.startsWith('telegram-')) {
            const chatId = conversationId.replace('telegram-', '');
            restoredLinks[conversationId] = chatId;
          }
        }

        if (Object.keys(restoredLinks).length > 0) {
          import('$lib/coreService.js').then(({ telegramLinkedChats }) => {
            telegramLinkedChats.set(new Map(Object.entries(restoredLinks)));
            console.log(`[Telegram] Restored ${Object.keys(restoredLinks).length} conversation links from localStorage`);
          });
        }

        if (restoredCount > 0) {
          console.log(`[Telegram] Restored ${restoredCount} Telegram chats from localStorage`);
        }
      }
    } catch (error) {
      console.error('[Telegram] Failed to restore chats from localStorage:', error);
    }

    // Restore AI chats (including Agent chats) from localStorage (for page refresh recovery)
    try {
      const savedAIChats = localStorage.getItem('dpc-ai-chats');
      if (savedAIChats) {
        const aiChatsData = JSON.parse(savedAIChats);
        let restoredCount = 0;

        aiChats.update(chats => {
          const newMap = new Map(chats);
          // Remove any stale entries where a P2P node_id was mistakenly stored as an AI/Telegram chat
          for (const id of newMap.keys()) {
            if (id.startsWith('dpc-node-')) {
              newMap.delete(id);
              console.warn(`[AI Chats] Removed stale P2P node entry from aiChats: ${id}`);
            }
          }
          for (const [id, info] of Object.entries(aiChatsData)) {
            // Only restore if not already in aiChats (excluding telegram chats and P2P node IDs)
            if (!newMap.has(id) && !id.startsWith('telegram-') && !id.startsWith('dpc-node-')) {
              newMap.set(id, info as { name: string; provider: string; instruction_set_name?: string });
              restoredCount++;
            }
          }
          return newMap;
        });

        if (restoredCount > 0) {
          console.log(`[AI Chats] Restored ${restoredCount} AI chats from localStorage`);
        }

        // Populate chatProviders from restored aiChats (fixes Agent chat provider reset on refresh)
        chatProviders.update(map => {
          const newMap = new Map(map);
          for (const [id, info] of Object.entries(aiChatsData)) {
            const chatInfo = info as { name: string; provider: string; instruction_set_name?: string };
            if (chatInfo.provider && !id.startsWith('telegram-') && !id.startsWith('dpc-node-')) {
              newMap.set(id, chatInfo.provider);
            }
          }
          return newMap;
        });
      }
    } catch (error) {
      console.error('[AI Chats] Failed to restore chats from localStorage:', error);
    }

    // Restore AI chat histories from localStorage (for page refresh recovery)
    try {
      const savedHistories = localStorage.getItem('dpc-ai-chat-histories');
      if (savedHistories) {
        const historiesData = JSON.parse(savedHistories);
        let restoredCount = 0;

        chatHistories.update(h => {
          const newMap = new Map(h);
          for (const [id, messages] of Object.entries(historiesData)) {
            // Only restore if not already in chatHistories (excluding telegram chats)
            if (!newMap.has(id) && !id.startsWith('telegram-')) {
              newMap.set(id, messages as Message[]);
              restoredCount++;
            }
          }
          return newMap;
        });

        if (restoredCount > 0) {
          console.log(`[AI Chats] Restored ${restoredCount} chat histories from localStorage`);
        }
      }
    } catch (error) {
      console.error('[AI Chats] Failed to restore chat histories from localStorage:', error);
    }

    // Load group chats from backend (v0.19.0)
    try {
      await loadGroups();
      console.log('[Groups] Loaded group chats from backend');
    } catch (error) {
      console.error('[Groups] Failed to load group chats:', error);
    }

    // Load agents from backend (v0.19.0+)
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
  });

  // Cleanup focus listener on component destroy
  onDestroy(() => {
    if (unlistenFocus) {
      unlistenFocus();
      unlistenFocus = null;
    }
  });

  // Reactive: Update active chat in coreService to prevent unread badges on open chats
  $effect(() => {
    setActiveChat(activeChatId);
  });

  // Reactive: Sync provider dropdown with chat-specific provider when switching chats
  $effect(() => {
    const chatProvider = $chatProviders.get(activeChatId);
    if (chatProvider && chatProvider !== 'local_ai') {
      // Update dropdown to show chat-specific provider (e.g., dpc_agent)
      selectedTextProvider = `local:${chatProvider}`;
    } else {
      // Reset to default provider for chats without specific provider (local_ai, AI chats, etc.)
      if ($availableProviders?.default_provider) {
        selectedTextProvider = `local:${$availableProviders.default_provider}`;
      }
    }
  });

  // Reactive: Open commit dialog when proposal received
  $effect(() => {
    if ($knowledgeCommitProposal) {
      showCommitDialog = true;

      // Send notification for knowledge commit proposal
      (async () => {
        const notified = await showNotificationIfBackground({
          title: 'Vote Requested',
          body: $knowledgeCommitProposal.proposal?.topic || 'Knowledge commit proposal'
        });
        console.log(`[Notifications] Knowledge proposal notification: ${notified ? 'system' : 'skip'}`);
      })();
    }
  });

  // Reactive: Open new session dialog when proposal received (v0.11.3)
  $effect(() => {
    if ($newSessionProposal) {
      showNewSessionDialog = true;

      // Send notification for new session proposal
      (async () => {
        const initiatorName = getPeerDisplayName($newSessionProposal.initiator_node_id);
        const notified = await showNotificationIfBackground({
          title: 'New Session Requested',
          body: `${initiatorName} wants to start a new session`
        });
        console.log(`[Notifications] New session proposal notification: ${notified ? 'system' : 'skip'}`);
      })();
    }
  });

  // Reactive: Clear frontend state when new session approved (v0.11.3)
  $effect(() => {
    if ($newSessionResult && $newSessionResult.result === "approved") {
      // v0.20.0 FIX: Prioritize conversation_id over sender_node_id
      // For group chats, conversation_id is the group_id (correct)
      // sender_node_id is only used as fallback for legacy peer chats
      const conversationId = $newSessionResult.conversation_id || $newSessionResult.sender_node_id;

      // Send notification for new session result
      (async () => {
        const notified = await showNotificationIfBackground({
          title: `Session ${$newSessionResult.result}`,
          body: `New session ${$newSessionResult.result}`
        });
        console.log(`[Notifications] New session result notification: ${notified ? 'system' : 'skip'}`);
      })();

      console.log('[NewSession] Clearing chat for:', conversationId);
      console.log('[NewSession] sender_node_id:', $newSessionResult.sender_node_id);
      console.log('[NewSession] conversation_id:', $newSessionResult.conversation_id);
      console.log('[NewSession] Current chatHistories keys:', Array.from($chatHistories.keys()));

      // Clear message history for this chat
      chatHistories.update(h => {
        const newMap = new Map(h);
        newMap.set(conversationId, []);
        return newMap;
      });

      // Clear token usage
      tokenUsageMap = new Map(tokenUsageMap);
      tokenUsageMap.delete(conversationId);

      // Clear context tracking (will show "Updated" badge again on next query)
      lastSentContextHash = new Map(lastSentContextHash);
      lastSentContextHash.delete(conversationId);
      lastSentPeerHashes = new Map(lastSentPeerHashes);
      lastSentPeerHashes.delete(conversationId);

      // Clear the result to prevent re-triggering this reactive statement
      newSessionResult.set(null);
    }
  });

  // Model download effects moved to ModelDownloadPanel.svelte (Step 8)

  // Reactive: Clear chat window on conversation reset (v0.11.3 - for AI chats and P2P resets)
  $effect(() => {
    if ($conversationReset) {
      const conversationId = $conversationReset.conversation_id;
      console.log('[ConversationReset] Clearing chat for:', conversationId);

      // Clear message history for this chat
      chatHistories.update(h => {
        const newMap = new Map(h);
        newMap.set(conversationId, []);
        return newMap;
      });

      // Clear token usage
      tokenUsageMap = new Map(tokenUsageMap);
      tokenUsageMap.delete(conversationId);

      // Clear context tracking
      lastSentContextHash = new Map(lastSentContextHash);
      lastSentContextHash.delete(conversationId);
      lastSentPeerHashes = new Map(lastSentPeerHashes);
      lastSentPeerHashes.delete(conversationId);

      // Clear the event to prevent re-triggering
      conversationReset.set(null);
    }
  });

  // Reactive: Handle token warnings (Phase 2)
  $effect(() => {
    if ($tokenWarning) {
      const {conversation_id, tokens_used, token_limit, usage_percent,
             history_tokens, context_estimated} = $tokenWarning;

      // Guard: Only update if values actually changed (prevent infinite loop)
      const existing = tokenUsageMap.get(conversation_id);
      if (existing && existing.used === tokens_used && existing.limit === token_limit) {
        return; // Values unchanged, skip update
      }

      // Update token usage map
      tokenUsageMap = new Map(tokenUsageMap);
      tokenUsageMap.set(conversation_id, {
        used: tokens_used,
        limit: token_limit,
        historyTokens: history_tokens ?? 0,
        contextEstimated: context_estimated ?? 0,
      });

      // Show warning toast
      showTokenWarning = true;
      tokenWarningMessage = `Context window ${Math.round(usage_percent * 100)}% full. Consider ending session to save knowledge.`;
    }
  });

  // Reactive: Get current chat's token usage
  const DEFAULT_TOKEN_LIMIT = 16384; // Default limit for new AI chats (before first message)
  let currentTokenUsage = $derived(tokenUsageMap.get(activeChatId) || {used: 0, limit: 0});

  // Use effective limit (default if not yet set by backend)
  let effectiveTokenUsage = $derived({
    used: currentTokenUsage.used,
    limit: currentTokenUsage.limit > 0 ? currentTokenUsage.limit : DEFAULT_TOKEN_LIMIT,
    historyTokens: currentTokenUsage.historyTokens ?? 0,
    contextEstimated: currentTokenUsage.contextEstimated ?? 0,
  });

  // Reactive: Estimate token usage (currentInput owned by ChatPanel; use '' here for SessionControls)
  let estimatedUsage = $derived(
    estimateConversationUsage(effectiveTokenUsage, '')
  );

  // Reactive: Check if current peer/group is connected (for enabling/disabling send controls)
  let isPeerConnected = $derived(
    activeChatId.startsWith('ai_') || activeChatId === 'local_ai'
      ? true  // AI chats don't require peer connection
      : activeChatId.startsWith('group-')
        ? (() => {
            // Group chat: at least one other member online
            const group = $groupChats.get(activeChatId);
            if (!group) return false;
            const selfId = $nodeStatus?.node_id || '';
            return group.members?.some((m: string) =>
              m !== selfId && ($nodeStatus?.peer_info?.some((p: any) => p.node_id === m) ?? false)
            ) ?? false;
          })()
        : ($nodeStatus?.peer_info?.some((p: any) => p.node_id === activeChatId) ?? false)
  );

  // Reactive: Check if current chat is a Telegram chat (for UI adjustments)
  let isTelegramChat = $derived(activeChatId.startsWith('telegram-'));

  // Reactive: Check if current chat is a P2P chat (not local AI, not AI chat, not Telegram, not group)
  // P2P chats are identified by node IDs like 'dpc-node-xxx'
  let isP2PChat = $derived(
    activeChatId !== 'local_ai' &&
    !activeChatId.startsWith('ai_') &&
    !activeChatId.startsWith('agent_') &&
    activeChatId !== 'default' &&
    !activeChatId.startsWith('telegram-') &&
    !activeChatId.startsWith('group-')
  );

  // Reactive: Check if current chat is an AI chat (excluding Telegram which are stored in aiChats for sidebar)
  // Telegram chats are in $aiChats for sidebar display but are NOT AI chats
  let isActuallyAIChat = $derived($aiChats.has(activeChatId) && !activeChatId.startsWith('telegram-'));

  // Reactive: Check if current chat is a group chat (v0.19.0)
  let isGroupChat = $derived(activeChatId.startsWith('group-'));

  // Chat history sync on chat switch moved to ChatHistorySyncPanel.svelte


  // Reactive: Handle knowledge extraction failures (Phase 4)
  $effect(() => {
    if ($extractionFailure) {
      const {conversation_id, reason} = $extractionFailure;
      showExtractionFailure = true;
      extractionFailureMessage = `Knowledge extraction failed for ${conversation_id}: ${reason}`;
    }
  });

  // Reactive: Handle knowledge commit voting results
  $effect(() => {
    if ($knowledgeCommitResult) {
      const { status, topic, vote_tally } = $knowledgeCommitResult;

      // Store full result for detailed view
      currentVoteResult = $knowledgeCommitResult;

      if (status === "approved") {
        commitResultMessage = `✅ Knowledge commit approved: ${topic} (${vote_tally.approve}/${vote_tally.total} votes) - Click for details`;
        commitResultType = "info";
      } else if (status === "rejected") {
        commitResultMessage = `❌ Knowledge commit rejected: ${topic} (${vote_tally.reject} reject, ${vote_tally.request_changes} change requests) - Click for details`;
        commitResultType = "error";
      } else if (status === "revision_needed") {
        commitResultMessage = `📝 Changes requested for: ${topic} (${vote_tally.request_changes}/${vote_tally.total} requested changes) - Click for details`;
        commitResultType = "warning";
      } else if (status === "timeout") {
        commitResultMessage = `⏱️ Voting timeout for: ${topic} (${vote_tally.total} votes received) - Click for details`;
        commitResultType = "warning";
      }

      showCommitResultToast = true;
      closeCommitDialog();

      // Send notification for knowledge commit result
      (async () => {
        const notified = await showNotificationIfBackground({
          title: 'Vote Complete',
          body: `${topic} - ${status}`
        });
        console.log(`[Notifications] Knowledge commit result notification: ${notified ? 'system' : 'skip'}`);
      })();

      // Clear the result from store after showing
      knowledgeCommitResult.set(null);
    }
  });

  // Phase 7: Handle personal context updates (for "Updated" status indicator)
  $effect(() => {
    if ($contextUpdated) {
      const { context_hash } = $contextUpdated;
      if (context_hash) {
        // Guard: Only update if hash actually changed (prevent infinite loop)
        if (currentContextHash !== context_hash) {
          currentContextHash = context_hash;
          console.log(`[Context Updated] New hash: ${context_hash.slice(0, 8)}...`);
        }
      }
    }
  });

  // Phase 7: Handle peer context updates (for "Updated" status indicators)
  $effect(() => {
    if ($peerContextUpdated) {
      const { node_id, context_hash } = $peerContextUpdated;
      if (node_id && context_hash) {
        // Guard: Only update if hash actually changed (prevent infinite loop)
        const currentHash = peerContextHashes.get(node_id);
        if (currentHash !== context_hash) {
          peerContextHashes = new Map(peerContextHashes);
          peerContextHashes.set(node_id, context_hash);
          console.log(`[Peer Context Updated] ${node_id.slice(0, 15)}... - hash: ${context_hash.slice(0, 8)}...`);
        }
      }
    }
  });


  // Voice transcription effects moved to VoicePanel.svelte (Step 6)
  // historyRestored + groupHistorySynced effects moved to HistorySyncPanel.svelte (Step 8)

  // Reactive: Reset compute host if selected peer disconnects
  $effect(() => {
    if (selectedComputeHost !== "local" && $nodeStatus?.p2p_peers) {
      const isStillConnected = $nodeStatus.p2p_peers.includes(selectedComputeHost);
      if (!isStillConnected) {
        console.log(`Compute host ${selectedComputeHost} disconnected, resetting to local`);
        selectedComputeHost = "local";
        selectedRemoteModel = "";
      }
    }
  });


  // Helper: Group peers by connection strategy
  function getPeersByStrategy(peerInfo: any[]) {
    const strategyMap: Record<string, any[]> = {};

    if (!peerInfo) return strategyMap;

    for (const peer of peerInfo) {
      if (peer.strategy_used) {
        if (!strategyMap[peer.strategy_used]) {
          strategyMap[peer.strategy_used] = [];
        }
        strategyMap[peer.strategy_used].push(peer);
      }
    }

    return strategyMap;
  }

  // Helper: Format peer for tooltip display
  function formatPeerForTooltip(peer: any): string {
    let displayName = peer.name || 'Unknown';

    // Trim display name to 20 characters
    if (displayName.length > 20) {
      displayName = displayName.substring(0, 17) + '...';
    }

    // Extract node ID suffix (remove "dpc-node-" prefix)
    const nodeIdSuffix = peer.node_id.replace(/^dpc-node-/, '');

    return `${displayName} (${nodeIdSuffix})`;
  }

  // Reactive statement to compute peer counts
  let peersByStrategy = $derived(getPeersByStrategy($nodeStatus?.peer_info));

  function autoScroll() {
    setTimeout(() => {
      if (chatWindow) {
        chatWindow.scrollTop = chatWindow.scrollHeight;
      }
    }, 100);
  }
  
  // Reactive derived value that maps peer IDs to display names
  // This ensures Svelte tracks changes to peer_info properly
  let peerDisplayNames = $derived.by(() => {
    const names = new Map<string, string>();

    // Add current user's own name first (from personal context)
    const selfId = $nodeStatus?.node_id;
    const selfName = $personalContext?.profile?.name;
    if (selfId && selfName) {
      const displayName = `${selfName} | ${selfId}`;
      console.log(`[PeerNames] SELF ${selfId} -> ${displayName}`);
      names.set(selfId, displayName);
    }

    // Add connected peers
    if (!$nodeStatus || !$nodeStatus.peer_info) {
      console.log('[PeerNames] No peer_info, returning self only');
      return names;
    }
    console.log('[PeerNames] Building display names map from peer_info:', $nodeStatus.peer_info);
    for (const peer of $nodeStatus.peer_info) {
      if (peer.name) {
        const displayName = `${peer.name} | ${peer.node_id}`;
        console.log(`[PeerNames] ${peer.node_id} -> ${displayName}`);
        names.set(peer.node_id, displayName);
      } else {
        const displayName = peer.node_id;
        console.log(`[PeerNames] ${peer.node_id} -> ${displayName} (no name)`);
        names.set(peer.node_id, displayName);
      }
    }
    console.log('[PeerNames] Final map size:', names.size);
    return names;
  });

  function getPeerDisplayName(peerId: string): string {
    // Use the reactive map, with fallback for peers not in peer_info yet
    return peerDisplayNames.get(peerId) || peerId;
  }

  // handleSendMessage moved to ChatPanel.svelte (Step 4)

  // --- PEER CONNECTION FUNCTIONS ---
  // Connection strategy: Direct TLS (if dpc:// URI) or DHT-first (if node_id)
  async function handleConnectPeer() {
    if (!peerInput.trim()) return;

    const input = peerInput.trim();
    console.log("Connecting to peer:", input);

    // Show connecting state
    isConnecting = true;
    connectionError = "";
    showConnectionError = false;

    try {
      let result;
      // Detect if input is a dpc:// URI (Direct TLS) or just a node_id (DHT-first)
      if (input.startsWith('dpc://')) {
        // Direct TLS connection (manual IP/port)
        console.log("Using Direct TLS connection");
        result = await sendCommand("connect_to_peer", { uri: input });
      } else {
        // DHT-first connection (automatic discovery)
        // Tries: DHT → Peer cache → IPv4/IPv6 direct → Hub WebRTC → Relay → Gossip
        console.log("Using DHT-first discovery strategy");
        result = await sendCommand("connect_via_dht", { node_id: input });
      }

      // Check result
      if (result && result.status === 'error') {
        connectionError = result.message || "Connection failed";
        showConnectionError = true;
      } else {
        // Success - clear input
        peerInput = "";
      }
    } catch (error: any) {
      console.error('Connection error:', error);
      connectionError = error.message || "Connection failed - check backend logs";
      showConnectionError = true;
    } finally {
      isConnecting = false;
    }
  }
  
  function handleDisconnectPeer(nodeId: string) {
    sendCommand("disconnect_from_peer", { node_id: nodeId });
    if (activeChatId === nodeId) {
      activeChatId = 'local_ai';
    }
  }
  
  function handleReconnect() {
    resetReconnection();
    connectToCoreService();
  }

  // --- Knowledge Architecture Handlers ---

  function loadPersonalContext() {
    sendCommand("get_personal_context");
    showContextViewer = true;
  }

  function openAgentBoard() {
    showAgentBoard = true;
  }

  function openInstructionsEditor() {
    showInstructionsEditor = true;
  }

  function openFirewallEditor() {
    showFirewallEditor = true;
  }

  // loadAIScopes and its 3 effects moved to ChatPanel.svelte (Step 4)

  function openProvidersEditor() {
    showProvidersEditor = true;
  }

  function handleCommitVote(event: CustomEvent) {
    const { proposal_id, vote, comment } = event.detail;
    sendCommand("vote_knowledge_commit", {
      proposal_id,
      vote,
      comment
    });
    showCommitDialog = false;
  }

  function closeCommitDialog() {
    showCommitDialog = false;
    knowledgeCommitProposal.set(null);
  }

  function handleSessionVote(event: CustomEvent) {
    const { proposal_id, vote } = event.detail;
    voteNewSession(proposal_id, vote);
    showNewSessionDialog = false;
  }

  function closeNewSessionDialog() {
    showNewSessionDialog = false;
    newSessionProposal.set(null);
  }

  // handleCreateGroup, handleLeaveGroup, handleDeleteGroup moved to GroupManagementPanel.svelte
  async function handleCreateGroup(event: CustomEvent) { groupManagementPanelRef?.handleCreateGroup(event); }
  async function handleLeaveGroup(groupId: string) { groupManagementPanelRef?.handleLeaveGroup(groupId, activeChatId); }
  async function handleDeleteGroup(groupId: string) { groupManagementPanelRef?.handleDeleteGroup(groupId, activeChatId, ask); }

    // Model download dialog handlers (v0.13.5)
  // handleModelDownload + handleModelDownloadCancel moved to ModelDownloadPanel.svelte (Step 8)

  function handleEndSession(conversationId: string) {
    if (confirm("End this conversation session and extract knowledge?")) {
      sendCommand("end_conversation_session", {
        conversation_id: conversationId
      });
    }
  }

  async function toggleAutoKnowledgeDetection() {
    // bind:checked already updates the variable, now sync to backend and wait for confirmation
    const previousState = !autoKnowledgeDetection; // Store previous state in case we need to revert

    try {
      const result = await sendCommand("toggle_auto_knowledge_detection", {
        enabled: autoKnowledgeDetection
      });

      // Check if backend confirmed the change
      if (result.status === "success") {
        console.log(`✓ Auto-detection ${result.enabled ? 'enabled' : 'disabled'}`);
      } else {
        // Backend failed, revert checkbox
        console.error("Failed to toggle auto-detection:", result.message);
        autoKnowledgeDetection = previousState;
      }
    } catch (error) {
      // Network/timeout error, revert checkbox
      console.error("Error toggling auto-detection:", error);
      autoKnowledgeDetection = previousState;
    }
  }

  function handleNewChat(chatId: string) {
    // v0.11.3: Send proposal to peer for mutual approval
    // Frontend state will be cleared only after approval
    proposeNewSession(chatId);
  }

  // handleAddAIChat, confirmAddAIChat, cancelAddAIChat, handleAddAgentChat, handleDeleteAIChat
  // moved to AddAIChatPanel.svelte — delegate via addAIChatPanelRef
  async function handleAddAIChat() { addAIChatPanelRef?.open(); }
  async function handleAddAgentChat() { addAIChatPanelRef?.openForAgent(); }
  async function handleDeleteAIChat(chatId: string) {
    const deleted = await addAIChatPanelRef?.handleDeleteAIChat(chatId, ask);
    if (deleted && activeChatId === deleted) activeChatId = 'local_ai';
  }

  // handleSelectAgent, handleDeleteAgent, handleLinkAgentTelegram, handleUnlinkAgentTelegram
  // moved to AgentManagementPanel.svelte — delegate via agentManagementPanelRef
  function handleSelectAgent(agentId: string) {
    agentManagementPanelRef?.handleSelectAgent(agentId, agentChatToAgentId, activeChatId);
  }
  async function handleDeleteAgent(agentId: string) {
    agentManagementPanelRef?.handleDeleteAgent(agentId, ask, activeChatId);
  }
  async function handleLinkAgentTelegram(agentId: string, config: any) {
    return agentManagementPanelRef?.handleLinkAgentTelegram(agentId, config);
  }
  async function handleUnlinkAgentTelegram(agentId: string) {
    agentManagementPanelRef?.handleUnlinkAgentTelegram(agentId);
  }


  // execute_ai_query response moved to MessageRouterPanel.svelte (Step 8)

  // P2P, group text/file, and AI vision effects moved to MessageRouterPanel.svelte (Step 8)

  // Group invite/deletion/memberLeft effects moved to GroupPanel.svelte (Step 7)

  // handleGroupAddMember, handleGroupRemoveMember moved to GroupManagementPanel.svelte
  async function handleGroupAddMember(event: CustomEvent<{ group_id: string; node_id: string }>) { groupManagementPanelRef?.handleGroupAddMember(event); }
  async function handleGroupRemoveMember(event: CustomEvent<{ group_id: string; node_id: string }>) { groupManagementPanelRef?.handleGroupRemoveMember(event); }

</script>
<main class="container">
  <div class="grid">
    <!-- Sidebar -->
    <Sidebar
      connectionStatus={$connectionStatus}
      nodeStatus={$nodeStatus}
      aiChats={$aiChats}
      unreadMessageCounts={$unreadMessageCounts}
      bind:activeChatId
      peerDisplayNames={peerDisplayNames}
      bind:autoKnowledgeDetection
      bind:peerInput
      isConnecting={isConnecting}
      peersByStrategy={peersByStrategy}
      formatPeerForTooltip={formatPeerForTooltip}
      onReconnect={handleReconnect}
      onLoginToHub={(provider) => sendCommand('login_to_hub', {provider})}
      onViewPersonalContext={loadPersonalContext}
      onOpenInstructionsEditor={openInstructionsEditor}
      onOpenFirewallEditor={openFirewallEditor}
      onOpenProvidersEditor={openProvidersEditor}
      onOpenAgentBoard={openAgentBoard}
      onToggleAutoKnowledgeDetection={toggleAutoKnowledgeDetection}
      onConnectPeer={handleConnectPeer}
      onResetUnreadCount={resetUnreadCount}
      onGetPeerDisplayName={getPeerDisplayName}
      onAddAIChat={handleAddAIChat}
      onAddAgentChat={handleAddAgentChat}
      onDeleteAIChat={handleDeleteAIChat}
      onDisconnectPeer={handleDisconnectPeer}
      groupChats={$groupChats}
      onCreateGroup={() => showNewGroupDialog = true}
      onLeaveGroup={handleLeaveGroup}
      onDeleteGroup={handleDeleteGroup}
      selfNodeId={$nodeStatus?.node_id || ""}
      agents={$agentsList}
      onSelectAgent={handleSelectAgent}
      onDeleteAgent={handleDeleteAgent}
      onLinkAgentTelegram={handleLinkAgentTelegram}
      onUnlinkAgentTelegram={handleUnlinkAgentTelegram}
    />


    <!-- Chat Panel -->
    <div class="chat-panel" style="height: {chatPanelHeight}px;">
      <div class="chat-header">
        <div class="chat-title-section">
          <button
            type="button"
            class="chat-header-toggle"
            onclick={() => chatHeaderCollapsed = !chatHeaderCollapsed}
            aria-expanded={!chatHeaderCollapsed}
          >
            <h2>
              <span class="collapse-indicator">{chatHeaderCollapsed ? '▶' : '▼'}</span>
              {#if isGroupChat}
                {$groupChats.get(activeChatId)?.name || 'Group Chat'}
                <span class="group-header-meta">
                  ({$groupChats.get(activeChatId)?.members?.length || 0} members)
                  <!-- svelte-ignore a11y_click_events_have_key_events -->
                  <span
                    class="group-settings-btn"
                    role="button"
                    tabindex="0"
                    title="Group settings"
                    onclick={(e: MouseEvent) => { e.stopPropagation(); showGroupSettingsDialog = true; }}
                  >
                    settings
                  </span>
                </span>
              {:else if isActuallyAIChat}
                {$aiChats.get(activeChatId)?.name || 'AI Assistant'}
              {:else}
                Chat with {getPeerDisplayName(activeChatId)}
              {/if}
            </h2>
          </button>

          <!-- Group topic display (v0.19.0) - inside title section for proper positioning -->
          {#if !chatHeaderCollapsed && isGroupChat && $groupChats.get(activeChatId)?.topic}
            <div class="group-topic">{$groupChats.get(activeChatId)?.topic}</div>
          {/if}

          <!-- Auto Transcribe toggle (P2P and Telegram chats, NOT AI chats) -->
          {#if !$aiChats.has(activeChatId) && activeChatId !== 'local_ai'}
            <label class="auto-transcribe-toggle" title="Automatically transcribe received voice messages">
              <input
                type="checkbox"
                bind:checked={autoTranscribeEnabled}
                onchange={() => voicePanelComp?.saveAutoTranscribeSetting()}
                disabled={whisperModelLoading}
              />
              <span>Auto Transcribe</span>
              {#if whisperModelLoading}
                <span class="whisper-loading-indicator" title="Loading Whisper model...">⏳</span>
              {/if}
              {#if whisperModelLoadError}
                <span class="whisper-error-indicator" title={whisperModelLoadError}>⚠️</span>
              {/if}
            </label>
          {/if}

          <!-- Telegram bot integration status (v0.14.0+) - P2P chats only -->
          {#if isP2PChat}
            <TelegramStatus conversationId={activeChatId} />
          {/if}
        </div>

        {#if !chatHeaderCollapsed}
          <!-- ProviderSelector: AI chats only (not Telegram) -->
          {#if isActuallyAIChat}
          <ProviderSelector
            bind:selectedComputeHost
            bind:selectedTextProvider
            bind:selectedVisionProvider
            bind:selectedVoiceProvider
            showForChatId={activeChatId}
            isAIChat={isActuallyAIChat}
            providersList={$providersList}
            peerProviders={$peerProviders}
            nodeStatus={$nodeStatus}
            defaultProviders={$defaultProviders}
            agentLlmProvider={$aiChats.get(activeChatId)?.llm_provider || ""}
          />
          {/if}

          <!-- SessionControls: AI chats, P2P chats, and Telegram chats -->
          <SessionControls
            showForChatId={activeChatId}
            isAIChat={isActuallyAIChat}
            isPeerConnected={isPeerConnected}
            isTelegramChat={isTelegramChat}
            tokenUsed={effectiveTokenUsage.used}
            tokenLimit={effectiveTokenUsage.limit}
            estimatedTokens={estimatedUsage.estimated}
            showEstimation={estimatedUsage.isEstimated}
            historyTokens={effectiveTokenUsage.historyTokens ?? 0}
            contextEstimated={effectiveTokenUsage.contextEstimated ?? 0}
            bind:enableMarkdown
            onNewSession={handleNewChat}
            onEndSession={handleEndSession}
          />
        {/if}
      </div>

      <ChatMessageList
        messages={activeMessages}
        conversationId={activeChatId}
        bind:enableMarkdown
        bind:chatWindowElement={chatWindow}
        showTranscription={autoTranscribeEnabled}
        agentProgressMessage={agentProgressMessage}
        agentProgressTool={agentProgressTool}
        agentProgressRound={agentProgressRound}
        agentStreamingText={agentStreamingText}
        peerDisplayNames={peerDisplayNames}
        selfNodeId={$nodeStatus?.node_id || ''}
        selfName={$personalContext?.profile?.name || ''}
      />

      <ChatPanel
        bind:this={chatPanelRef}
        {activeChatId}
        {chatHistories}
        {commandToChatMap}
        {agentChatToAgentId}
        {aiChats}
        {chatProviders}
        {selectedTextProvider}
        {selectedVisionProvider}
        {selectedVoiceProvider}
        {clearAgentStreaming}
        {autoScroll}
        {setChatLoading}
        {isLoading}
        {tokenUsageMap}
        {availableInstructionSets}
        {currentContextHash}
        {lastSentContextHash}
        {peerContextHashes}
        {lastSentPeerHashes}
        {peerDisplayNames}
        {autoTranscribeEnabled}
        {whisperModelLoading}
        {groupPanelRef}
        bind:chatPanelHeight
        bind:showAgentBoard
      />
    </div>
  </div>
</main>

<!-- AgentPanel: logic-only, manages agent progress/streaming/history effects (Step 5) -->
<AgentPanel
  bind:this={agentPanelComp}
  {activeChatId}
  {agentChatToAgentId}
  {chatHistories}
  {getPeerDisplayName}
  chatWindow={chatWindow ?? null}
  onUpdateTokenUsage={(convId, usage) => {
    tokenUsageMap = new Map(tokenUsageMap);
    tokenUsageMap.set(convId, usage);
  }}
  onAgentToast={(message, type) => {
    agentToastMessage = message;
    agentToastType = type;
    showAgentToast = true;
    setTimeout(() => { showAgentToast = false; }, 3000);
  }}
  onRefreshAgents={async () => {
    try {
      const result = await listAgents();
      if (result?.status === 'success' && result.agents) {
        agentsList.set(result.agents);
      }
    } catch (error) {
      console.error('Failed to refresh agents list:', error);
    }
  }}
  bind:agentProgressMessage
  bind:agentProgressTool
  bind:agentProgressRound
  bind:agentStreamingText
/>

<!-- VoicePanel: logic-only, manages whisper loading + transcription effects (Step 6) -->
<VoicePanel
  bind:this={voicePanelComp}
  {activeChatId}
  {aiChats}
  {chatHistories}
  bind:autoTranscribeEnabled
  bind:whisperModelLoading
  bind:whisperModelLoadError
/>

<!-- TelegramPanel: logic-only, handles incoming Telegram message events (Step 8) -->
<TelegramPanel {aiChats} {chatHistories} />

<!-- MessageRouterPanel: logic-only, routes P2P/group/vision/AI-query messages to chatHistories (Step 8) -->
<MessageRouterPanel
  {activeChatId}
  {chatHistories}
  chatWindow={chatWindow ?? null}
  {processedMessageIds}
  {commandToChatMap}
  {currentContextHash}
  {aiChats}
  onSetChatLoading={setChatLoading}
  onUpdateTokenUsage={(chatId, usage) => {
    tokenUsageMap = new Map(tokenUsageMap);
    tokenUsageMap.set(chatId, usage);
  }}
  onMarkContextSent={(chatId, hash) => {
    lastSentContextHash = new Map(lastSentContextHash);
    lastSentContextHash.set(chatId, hash);
  }}
  onAgentToast={(message, type) => {
    agentToastMessage = message;
    agentToastType = type;
    showAgentToast = true;
    setTimeout(() => { showAgentToast = false; }, 7000);
  }}
  getStreamingText={() => agentPanelComp?.flushAndCapture() ?? ''}
/>

<!-- ModelDownloadPanel: model download dialog + effects (Step 8) -->
<ModelDownloadPanel />

<!-- ChatHistorySyncPanel: loads history from backend when switching to peer/agent/group chat (Step 8) -->
<ChatHistorySyncPanel
  {activeChatId}
  {chatHistories}
  {loadingHistory}
  {processedMessageIds}
  chatWindow={chatWindow}
  {getPeerDisplayName}
  onUpdateTokenUsage={(chatId, usage) => {
    tokenUsageMap = new Map(tokenUsageMap);
    tokenUsageMap.set(chatId, usage);
  }}
  hasTokenUsage={(chatId) => tokenUsageMap.has(chatId)}
/>

<!-- HistorySyncPanel: handles $historyRestored and $groupHistorySynced effects (Step 8) -->
<HistorySyncPanel
  {activeChatId}
  {chatHistories}
  chatWindow={chatWindow ?? null}
  {processedMessageIds}
  {getPeerDisplayName}
  onAgentToast={(message, type) => {
    agentToastMessage = message;
    agentToastType = type;
    showAgentToast = true;
    setTimeout(() => { showAgentToast = false; }, 3000);
  }}
/>

<!-- Knowledge Architecture UI Components -->
<KnowledgeCommitDialog
  bind:open={showCommitDialog}
  proposal={$knowledgeCommitProposal}
  on:vote={handleCommitVote}
  on:close={closeCommitDialog}
/>

<NewSessionDialog
  bind:open={showNewSessionDialog}
  proposal={$newSessionProposal}
  on:vote={handleSessionVote}
  on:close={closeNewSessionDialog}
/>

<!-- New Group Dialog (v0.19.0) -->
<NewGroupDialog
  bind:open={showNewGroupDialog}
  connectedPeers={($nodeStatus?.peer_info || []).map((p: any) => ({ node_id: p.node_id, name: p.name || p.node_id }))}
  on:create={handleCreateGroup}
  on:cancel={() => showNewGroupDialog = false}
/>

<!-- GroupPanel: group invite/deletion/memberLeft effects + mention autocomplete (Steps 7 & 8) -->
<GroupPanel
  bind:this={groupPanelRef}
  {activeChatId}
  {chatHistories}
  {peerDisplayNames}
  getCurrentInput={() => chatPanelRef?.getInputValue() ?? ''}
  onSetCurrentInput={(val) => chatPanelRef?.setInputValue(val)}
  onSetActiveChatId={(id) => { activeChatId = id; }}
  onAgentToast={(message, type) => {
    agentToastMessage = message;
    agentToastType = type;
    showAgentToast = true;
    setTimeout(() => { showAgentToast = false; }, 4000);
  }}
/>

<!-- Group Settings Dialog (v0.19.0) -->
<GroupSettingsDialog
  bind:open={showGroupSettingsDialog}
  group={isGroupChat ? $groupChats.get(activeChatId) ?? null : null}
  selfNodeId={$nodeStatus?.node_id || ''}
  connectedPeers={($nodeStatus?.peer_info || []).map((p: any) => ({ node_id: p.node_id, name: p.name || p.node_id }))}
  peerDisplayNames={peerDisplayNames}
  on:addMember={handleGroupAddMember}
  on:removeMember={handleGroupRemoveMember}
/>


<!-- Notification Permission Dialog -->
{#if showNotificationPermissionDialog}
  <div
    class="dialog-overlay"
    role="button"
    tabindex="0"
    onclick={() => showNotificationPermissionDialog = false}
    onkeydown={(e) => {
      if (e.key === 'Escape' || e.key === 'Enter') {
        showNotificationPermissionDialog = false;
      }
    }}
  >
    <div
      class="notification-dialog-box"
      role="dialog"
      aria-labelledby="notification-dialog-title"
      tabindex="-1"
      onclick={(e) => e.stopPropagation()}
      onkeydown={(e) => e.stopPropagation()}
    >
      <h2 id="notification-dialog-title">Enable Desktop Notifications</h2>
      <p>Get notified when:</p>
      <ul>
        <li>You receive new messages</li>
        <li>Someone sends you a file</li>
        <li>You're asked to vote on knowledge</li>
        <li>Session requests are made</li>
      </ul>
      <div class="dialog-actions">
        <button
          class="btn-primary"
          onclick={async () => {
            const granted = await requestNotificationPermission();
            showNotificationPermissionDialog = false;

            // Show result toast
            agentToastMessage = granted
              ? 'Notifications enabled'
              : 'Notifications disabled - you can enable them later in settings';
            agentToastType = 'info';
            showAgentToast = true;
            setTimeout(() => showAgentToast = false, 3000);
          }}
        >
          Enable Notifications
        </button>
        <button
          class="btn-secondary"
          onclick={() => {
            showNotificationPermissionDialog = false;
            localStorage.setItem('notificationPreference', 'disabled');
            localStorage.setItem('permissionRequestedAt', new Date().toISOString());
          }}
        >
          Maybe Later
        </button>
      </div>
    </div>
  </div>
{/if}

<ContextViewer
  bind:open={showContextViewer}
  context={$personalContext}
  on:close={() => showContextViewer = false}
/>


<InstructionsEditor
  bind:open={showInstructionsEditor}
  on:close={() => showInstructionsEditor = false}
/>

<FirewallEditor
  bind:open={showFirewallEditor}
  on:close={() => showFirewallEditor = false}
/>

<ProvidersEditor
  bind:open={showProvidersEditor}
  on:close={() => showProvidersEditor = false}
/>

<!-- Token Warning Toast (Phase 2) -->
{#if showTokenWarning}
  <Toast
    message={tokenWarningMessage}
    type="warning"
    duration={10000}
    dismissible={true}
    onDismiss={() => {
      showTokenWarning = false;
      tokenWarning.set(null);
    }}
  />
{/if}

<!-- Knowledge Extraction Failure Toast (Phase 4) -->
{#if showExtractionFailure}
  <Toast
    message={extractionFailureMessage}
    type="error"
    duration={8000}
    dismissible={true}
    onDismiss={() => {
      showExtractionFailure = false;
      extractionFailure.set(null);
    }}
  />
{/if}

<!-- Knowledge Commit Result Toast -->
{#if showCommitResultToast}
  <Toast
    message={commitResultMessage}
    type={commitResultType}
    duration={6000}
    dismissible={true}
    onDismiss={() => {
      showCommitResultToast = false;
    }}
    onClick={() => {
      showVoteResultDialog = true;
      showCommitResultToast = false;
    }}
  />
{/if}

<!-- Connection Error Toast -->
{#if showConnectionError}
  <Toast
    message={connectionError}
    type="error"
    duration={8000}
    dismissible={true}
    onDismiss={() => {
      showConnectionError = false;
      connectionError = "";
    }}
  />
{/if}


<!-- Agent Operation Toast (v0.19.0+) -->
{#if showAgentToast}
  <Toast
    message={agentToastMessage}
    type={agentToastType}
    duration={agentToastType === 'error' ? 5000 : 3000}
  />
{/if}

<!-- Vote Result Details Dialog -->
<VoteResultDialog
  result={currentVoteResult}
  open={showVoteResultDialog}
  on:close={() => {
    showVoteResultDialog = false;
  }}
/>

<!-- AddAIChatPanel: Add AI Chat dialog + handlers (Step 8) -->
<AddAIChatPanel
  bind:this={addAIChatPanelRef}
  {aiChats}
  {chatHistories}
  {chatProviders}
  {availableInstructionSets}
  onSetActiveChatId={(id) => { activeChatId = id; }}
  onSetAgentChatToAgentId={(chatId, agentId) => { agentChatToAgentId.set(chatId, agentId); agentChatToAgentId = new Map(agentChatToAgentId); }}
  onAgentToast={(message, type) => {
    agentToastMessage = message;
    agentToastType = type;
    showAgentToast = true;
    setTimeout(() => { showAgentToast = false; }, 3000);
  }}
/>

<!-- GroupManagementPanel: group create/leave/delete/member handlers (Step 8) -->
<GroupManagementPanel
  bind:this={groupManagementPanelRef}
  {chatHistories}
  onSetActiveChatId={(id) => { activeChatId = id; }}
  onCloseNewGroupDialog={() => { showNewGroupDialog = false; }}
/>

<!-- AgentManagementPanel: agent select/delete/Telegram-link handlers (Step 8) -->
<AgentManagementPanel
  bind:this={agentManagementPanelRef}
  {aiChats}
  {chatHistories}
  {chatProviders}
  onSetActiveChatId={(id) => { activeChatId = id; }}
  onSetSelectedComputeHost={(host) => { selectedComputeHost = host; }}
  onSetAgentChatToAgentId={(chatId, agentId) => { agentChatToAgentId.set(chatId, agentId); agentChatToAgentId = new Map(agentChatToAgentId); }}
  onAgentToast={(message, type) => {
    agentToastMessage = message;
    agentToastType = type;
    showAgentToast = true;
    setTimeout(() => { showAgentToast = false; }, type === 'error' ? 5000 : 3000);
  }}
/>

<style>
  .container {
    padding: 1.5rem;
    width: auto;
    margin: 0 auto;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    overflow-x: hidden; /* Prevent horizontal overflow */
    max-width: 100vw; /* Constrain to viewport width */
    box-sizing: border-box;
  }

  .grid {
    display: grid;
    grid-template-columns: 380px 1fr;
    gap: 1.5rem;
    overflow-x: hidden; /* Prevent horizontal overflow */
    max-width: 100%; /* Constrain to parent width */
  }
  
  @media (max-width: 968px) {
    .grid { grid-template-columns: 1fr; }
  }

  h2 {
    margin: 0 0 0.75rem 0;
    font-size: 1.1rem;
    border-bottom: 1px solid #eee;
    padding-bottom: 0.5rem;
  }

  
  .chat-panel {
    background: white;
    border: 1px solid #e0e0e0;
    border-radius: 8px;
    display: flex;
    flex-direction: column;
    /* Height is set via inline style */
    min-height: 300px;
    overflow-x: hidden; /* Prevent horizontal overflow */
    max-width: 100%; /* Constrain to parent width */
  }
  
  .chat-header {
    display: flex;
    flex-wrap: wrap;  /* Wrap left/right sections when they don't fit */
    justify-content: space-between;
    align-items: center;
    gap: 0.75rem;  /* Add spacing between wrapped items */
    padding: 1rem;
    border-bottom: 1px solid #eee;
  }

  .chat-title-section {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    flex: 1 1 auto;  /* Allow to shrink/grow, wrap when doesn't fit */
    min-width: 0;  /* Allow shrinking below content size */
  }

  .chat-header h2 {
    margin: 0;
    border: none;
    padding: 0;
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .chat-header-toggle {
    width: 100%;
    text-align: left;
    cursor: pointer;
    background: none;
    border: none;
    padding: 0;
    font: inherit;
    color: inherit;
    transition: opacity 0.2s ease;
  }

  .chat-header-toggle:hover {
    opacity: 0.7;
  }

  .group-header-meta {
    font-size: 0.7em;
    color: #888;
    font-weight: 400;
    margin-left: 0.3em;
  }

  .group-settings-btn {
    cursor: pointer;
    color: #89b4fa;
    margin-left: 0.4em;
    text-decoration: underline;
    font-size: 0.95em;
  }

  .group-settings-btn:hover {
    color: #74c7ec;
  }

  .group-topic {
    font-size: 0.85rem;
    color: #aaa;
    padding: 0.2rem 0.8rem;
    font-style: italic;
  }

  .collapse-indicator {
    font-size: 0.8em;
    color: #666;
    transition: transform 0.2s ease;
    display: inline-block;
    min-width: 1em;
  }

  /* Auto Transcribe Toggle (v0.13.2+) */
  .auto-transcribe-toggle {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-size: 14px;
    color: #555;
    cursor: pointer;
    padding: 0.5rem 0.75rem;
    margin-left: auto;
    background: #f5f5f5;
    border-radius: 6px;
    transition: all 0.2s ease;
    user-select: none;
  }

  .auto-transcribe-toggle:hover {
    background: #e8e8e8;
  }

  .auto-transcribe-toggle input[type="checkbox"] {
    cursor: pointer;
    width: 16px;
    height: 16px;
    margin: 0;
  }

  .auto-transcribe-toggle span {
    font-weight: 500;
    white-space: nowrap;
  }

  /* Whisper model loading indicator (v0.13.3+) */
  .whisper-loading-indicator {
    font-size: 14px;
    animation: pulse 1.5s ease-in-out infinite;
  }

  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.5; }
  }

  .whisper-error-indicator {
    font-size: 14px;
    color: #ff9800;
  }

  /* Notification Permission Dialog */
  .dialog-overlay {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: rgba(0, 0, 0, 0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 10001;
  }

  .notification-dialog-box {
    background: white;
    border-radius: 8px;
    padding: 2rem;
    max-width: 400px;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
  }

  .notification-dialog-box h2 {
    margin-top: 0;
    margin-bottom: 1rem;
    color: #333;
  }

  .notification-dialog-box p {
    margin-bottom: 0.5rem;
    color: #666;
  }

  .notification-dialog-box ul {
    margin: 1rem 0;
    padding-left: 1.5rem;
  }

  .notification-dialog-box li {
    margin: 0.5rem 0;
    color: #666;
  }

  .dialog-actions {
    display: flex;
    gap: 1rem;
    margin-top: 1.5rem;
    justify-content: flex-end;
  }

  .btn-primary {
    background: #2196F3;
    color: white;
    border: none;
    padding: 0.75rem 1.5rem;
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.95rem;
    font-weight: 500;
    transition: background 0.2s ease;
  }

  .btn-primary:hover {
    background: #1976D2;
  }

  .btn-secondary {
    background: #f5f5f5;
    color: #666;
    border: 1px solid #ddd;
    padding: 0.75rem 1.5rem;
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.95rem;
    transition: background 0.2s ease;
  }

  .btn-secondary:hover {
    background: #e0e0e0;
  }
</style>