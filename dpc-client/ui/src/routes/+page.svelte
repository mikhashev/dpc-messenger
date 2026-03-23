<!-- dpc-client/ui/src/routes/+page.svelte -->
<!-- FIXED VERSION - Proper URI detection for Direct TLS vs WebRTC -->

<script lang="ts">
  import { onMount, onDestroy, untrack } from "svelte";
  import { writable } from "svelte/store";
  import { connectionStatus, nodeStatus, coreMessages, p2pMessages, sendCommand, resetReconnection, connectToCoreService, knowledgeCommitProposal, knowledgeCommitResult, personalContext, tokenWarning, extractionFailure, availableProviders, peerProviders, contextUpdated, peerContextUpdated, firewallRulesUpdated, unreadMessageCounts, resetUnreadCount, setActiveChat, fileTransferOffer, fileTransferProgress, fileTransferComplete, fileTransferCancelled, activeFileTransfers, sendFile, acceptFileTransfer, cancelFileTransfer, sendVoiceMessage, filePreparationStarted, filePreparationProgress, filePreparationCompleted, historyRestored, newSessionProposal, newSessionResult, proposeNewSession, voteNewSession, conversationReset, aiResponseWithImage, defaultProviders, providersList, voiceTranscriptionComplete, voiceTranscriptionReceived, setConversationTranscription, getConversationTranscription, whisperModelLoadingStarted, whisperModelLoaded, whisperModelLoadingFailed, preloadWhisperModel, whisperModelDownloadRequired, whisperModelDownloadStarted, whisperModelDownloadCompleted, whisperModelDownloadFailed, telegramEnabled, telegramConnected, telegramMessageReceived, telegramVoiceReceived, telegramImageReceived, telegramFileReceived, telegramLinkedChats, telegramMessages, sendToTelegram, agentProgress, agentProgressClear, agentTextChunk, agentTelegramLinked, agentTelegramUnlinked, agentHistoryUpdated, groupChats, groupTextReceived, groupFileReceived, groupInviteReceived, groupUpdated, groupMemberLeft, groupDeleted, groupHistorySynced, createGroupChat, sendGroupMessage, sendGroupImage, sendGroupVoiceMessage, sendGroupFile, addGroupMember, removeGroupMember, leaveGroup, deleteGroup, loadGroups, createAgent, listAgents, listAgentProfiles, deleteAgent, agentCreated, agentsList, integrityWarnings } from "$lib/coreService";
  import KnowledgeCommitDialog from "$lib/components/KnowledgeCommitDialog.svelte";
  import NewSessionDialog from "$lib/components/NewSessionDialog.svelte";
  import VoteResultDialog from "$lib/components/VoteResultDialog.svelte";
  import ModelDownloadDialog from "$lib/components/ModelDownloadDialog.svelte";
  import ContextViewer from "$lib/components/ContextViewer.svelte";
  import AgentTaskBoard from "$lib/components/AgentTaskBoard.svelte";
  import InstructionsEditor from "$lib/components/InstructionsEditor.svelte";
  import FirewallEditor from "$lib/components/FirewallEditor.svelte";
  import ProvidersEditor from "$lib/components/ProvidersEditor.svelte";
  import ProviderSelector from "$lib/components/ProviderSelector.svelte";
  import Toast from "$lib/components/Toast.svelte";
  import MarkdownMessage from "$lib/components/MarkdownMessage.svelte";
  import ImageMessage from "$lib/components/ImageMessage.svelte";
  import ChatPanel from "$lib/components/ChatPanel.svelte";
  import SessionControls from "$lib/components/SessionControls.svelte";
  import TelegramStatus from "$lib/components/TelegramStatus.svelte";
  import FileTransferUI from "$lib/components/FileTransferUI.svelte";
  import Sidebar from "$lib/components/Sidebar.svelte";
  import NewGroupDialog from "$lib/components/NewGroupDialog.svelte";
  import GroupInviteDialog from "$lib/components/GroupInviteDialog.svelte";
  import GroupSettingsDialog from "$lib/components/GroupSettingsDialog.svelte";
  import TokenWarningBanner from "$lib/components/TokenWarningBanner.svelte";
  import IntegrityWarningBanner from "$lib/components/IntegrityWarningBanner.svelte";
  import VoiceRecorder from "$lib/components/VoiceRecorder.svelte";
  import VoicePlayer from "$lib/components/VoicePlayer.svelte";
  import MentionAutocomplete from "$lib/components/MentionAutocomplete.svelte";
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
  let currentInput = $state("");
  let chatLoadingStates = $state(new Map<string, boolean>());  // Per-chat loading state
  let chatWindow = $state<HTMLElement>();  // Bound to ChatPanel's chatWindowElement

  // Mention autocomplete state (group chats only)
  let mentionAutocompleteVisible = $state(false);
  let mentionQuery = $state("");
  let mentionStartPosition = $state(0);
  let mentionDropdownPosition = $state({ top: 0, left: 0 });
  let mentionSelectedIndex = $state(0);

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
  let selectedPeerContexts = $state(new Set<string>());  // Set of peer node_ids to fetch context from

  // Store draft input text per chat (preserves text when switching chats)
  let chatDraftInputs = $state(new Map<string, string>());

  // Voice message state (v0.13.0 - Voice Messages)
  let voicePreview = $state<{ blob: Blob; duration: number } | null>(null);

  // Auto-transcribe toggle state (v0.13.2+ Auto-Transcription)
  let autoTranscribeEnabled = $state(true);  // Default ON

  // Whisper model loading state (v0.13.3+ Model Pre-loading)
  let whisperModelLoading = $state(false);
  let whisperModelLoadError = $state<string | null>(null);

  // DPC Agent progress state (v0.15.0+ - real-time agent progress)
  let agentProgressMessage = $state<string | null>(null);
  let agentProgressTool = $state<string | null>(null);
  let agentProgressRound = $state<number>(0);
  let agentStreamingText = $state<string>("");  // Accumulated streaming text from agent
  let lastActiveChatId: string | null = null;  // Non-reactive tracker for chat switches

  // Throttled streaming: accumulate chunks in non-reactive buffer, flush to state periodically
  let streamingBuffer = "";  // Non-reactive buffer for chunks
  let streamingFlushTimeout: ReturnType<typeof setTimeout> | null = null;

  // Helper to clear streaming state (buffer + state)
  function clearAgentStreaming() {
    if (streamingFlushTimeout) {
      clearTimeout(streamingFlushTimeout);
      streamingFlushTimeout = null;
    }
    streamingBuffer = "";
    agentStreamingText = "";
  }

  // Dual provider selection (Phase 1: separate text and vision providers)
  // Managed by ProviderSelector component (extracted)
  let selectedTextProvider = $state("");  // Provider for text-only queries
  let selectedVisionProvider = $state("");  // Provider for image queries
  let selectedVoiceProvider = $state("");  // v0.13.0+: Provider for voice transcription

  // Helper function to parse provider selection (Phase 2.3)
  // Used by parent for routing remote inference requests
  function parseProviderSelection(uniqueId: string): { source: 'local' | 'remote', alias: string, nodeId?: string } {
    if (!uniqueId) return { source: 'local', alias: '' };

    if (uniqueId.startsWith('remote:')) {
      const parts = uniqueId.split(':');
      return {
        source: 'remote',
        nodeId: parts[1],  // Extract node_id
        alias: parts.slice(2).join(':')  // Rejoin alias (in case it contains ':')
      };
    }

    return { source: 'local', alias: uniqueId.replace('local:', '') };
  }

  // Resizable chat panel state
  let chatPanelHeight = $state((() => {
    // Load saved height from localStorage, default to calc(100vh - 120px)
    const saved = localStorage.getItem('chatPanelHeight');
    return saved ? parseInt(saved, 10) : 600;
  })());
  let isResizing = $state(false);
  let resizeStartY: number = 0;
  let resizeStartHeight: number = 0;

  // Store provider selection per chat (chatId -> provider alias)
  const chatProviders = writable<Map<string, string>>(new Map());

  // Store AI chat metadata (chatId -> {name: string, provider: string, instruction_set_name?: string})
  const aiChats = writable<Map<string, {name: string, provider: string, instruction_set_name?: string, profile_name?: string, llm_provider?: string}>>(
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
  let showGroupInviteDialog = $state(false);  // v0.19.0: accept/decline group invite
  let pendingGroupInvite = $state<any>(null);  // v0.19.0: pending group invite data
  let showGroupSettingsDialog = $state(false);  // v0.19.0: group settings/members panel
  // Initialize from localStorage (browser-safe)
  let autoKnowledgeDetection = $state(
    typeof window !== 'undefined' && localStorage.getItem('autoKnowledgeDetection') === 'true'
  );

  // Token tracking state (Phase 2)
  let tokenUsageMap = $state(new Map<string, {used: number, limit: number}>());
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
  let showModelDownloadDialog = $state(false);
  let modelDownloadInfo = $state<any>(null);
  let isDownloadingModel = $state(false);
  let showModelDownloadToast = $state(false);
  let modelDownloadToastMessage = $state("");
  let modelDownloadToastType = $state<"info" | "error" | "warning">("info");

  // Agent operation toast state (v0.19.0+)
  let showAgentToast = $state(false);
  let agentToastMessage = $state("");
  let agentToastType = $state<"info" | "error" | "warning">("info");

  // Add AI Chat dialog state
  let showAddAIChatDialog = $state(false);
  let selectedProviderForNewChat = $state("");
  let selectedInstructionSetForNewChat = $state("general");
  let selectedProfileForNewAgent = $state("default");  // Agent permission profile
  let newAgentName = $state("");  // Agent name input
  let selectedAgentLLMProvider = $state("");  // LLM provider for agent

  // Agent profiles state (v0.19.0+ - per-agent isolation)
  let availableAgentProfiles = $state<string[]>(["default"]);

  // Map: AI chat ID -> backend agent_id (for agent chats)
  let agentChatToAgentId = $state<Map<string, string>>(new Map());

  // Instruction Sets state
  type InstructionSets = {
    schema_version: string;
    default: string;
    sets: Record<string, {name: string, description: string}>;
  };
  let availableInstructionSets = $state<InstructionSets | null>(null);

  // Selected instruction set for active chat (derived from aiChats metadata)
  let selectedInstructionSet = $derived($aiChats.get(activeChatId)?.instruction_set_name || 'general');

  // Personal context inclusion toggle
  let includePersonalContext = $state(false);

  // AI Scope selection (for filtering what local AI can access)
  let selectedAIScope = $state(""); // Empty = no filtering (full context)
  let availableAIScopes = $state<string[]>([]); // List of scope names from privacy rules
  let aiScopesLoaded = $state(false); // Guard flag to prevent infinite loop

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

  // Whisper model loading event handlers (v0.13.3+ Model Pre-loading)
  $effect(() => {
    if ($whisperModelLoadingStarted) {
      console.log(`[Whisper] Model loading started: ${$whisperModelLoadingStarted.provider}`);
      whisperModelLoading = true;
      whisperModelLoadError = null;
    }
  });

  $effect(() => {
    if ($whisperModelLoaded) {
      console.log(`[Whisper] Model loaded successfully: ${$whisperModelLoaded.provider}`);
      whisperModelLoading = false;
      whisperModelLoadError = null;
    }
  });

  $effect(() => {
    if ($whisperModelLoadingFailed) {
      console.error(`[Whisper] Model loading failed: ${$whisperModelLoadingFailed.error}`);
      whisperModelLoading = false;
      whisperModelLoadError = $whisperModelLoadingFailed.error;
    }
  });

  // DPC Agent progress (v0.15.0+): Show real-time agent progress in chat
  $effect(() => {
    if ($agentProgress) {
      const { conversation_id, message, round, tool_name, ts } = $agentProgress;
      console.log(`[AgentProgress] Conv: ${conversation_id}, Tool: ${tool_name}, Round: ${round}`);
      console.log(`[AgentProgress] activeChatId: ${activeChatId}, match: ${activeChatId === conversation_id}`);

      // Only show progress for the active AI chat
      if (activeChatId === conversation_id) {
        console.log(`[AgentProgress] Setting progress: tool=${tool_name}, round=${round}`);
        agentProgressMessage = message || null;
        agentProgressTool = tool_name || null;
        agentProgressRound = round || 0;

        // Append tool call activity to streaming buffer so it appears in Raw output
        if (tool_name) {
          streamingBuffer += `\n⚙ ${tool_name}…\n`;
        } else if (message && (message.startsWith('✓') || message.startsWith('❌'))) {
          streamingBuffer += `${message}\n`;
        }
      } else {
        console.log(`[AgentProgress] SKIPPED - conversation_id mismatch`);
      }
    }
  });

  // Clear agent progress when switching chats (only when activeChatId actually changes)
  $effect(() => {
    // This effect tracks activeChatId - only runs when it changes
    if (activeChatId !== lastActiveChatId) {
      // Clear progress and streaming state when switching chats
      agentProgressMessage = null;
      agentProgressTool = null;
      agentProgressRound = 0;
      clearAgentStreaming();
      lastActiveChatId = activeChatId;
      console.log(`[ChatSwitch] Cleared progress for chat switch to: ${activeChatId}`);
    }
  });

  $effect(() => {
    if ($agentProgressClear) {
      const { conversation_id } = $agentProgressClear;
      // Clear progress when task completes/fails
      if (activeChatId === conversation_id) {
        agentProgressMessage = null;
        agentProgressTool = null;
        agentProgressRound = 0;
        // NOTE: Do NOT call clearAgentStreaming() here — the streaming buffer
        // needs to survive until the final response handler captures it as
        // capturedStreamingText. Clearing here would wipe the Raw output before
        // the final answer renders it. clearAgentStreaming() is called by the
        // chat-switch effect above when the user changes conversations.
      }
    }
  });

  // DPC Agent streaming text (v0.16.0+ - real-time text streaming)
  // Throttled approach: accumulate chunks in buffer, flush to state every 100ms
  $effect(() => {
    if ($agentTextChunk) {
      const { conversation_id, chunk } = $agentTextChunk;
      // Only accumulate chunks for the active AI chat
      if (activeChatId === conversation_id) {
        // Add to non-reactive buffer
        streamingBuffer += chunk;

        // Throttle state updates - flush buffer to state every 100ms
        if (!streamingFlushTimeout) {
          streamingFlushTimeout = setTimeout(() => {
            agentStreamingText += streamingBuffer;
            streamingBuffer = "";
            streamingFlushTimeout = null;
          }, 100);
        }
      }
    }
  });

  // Clear streaming state when switching away from AI chats
  // This prevents stale "Generating..." indicators when returning to an AI chat
  $effect(() => {
    // Track the current chat ID reactively
    const currentChatId = activeChatId;

    // Clear streaming state when chat changes
    return () => {
      // This cleanup runs when activeChatId changes
      clearAgentStreaming();
      agentProgressMessage = null;
      agentProgressTool = null;
      agentProgressRound = 0;
    };
  });

  // Reactive: Handle agent Telegram linked event (v0.15.0+)
  $effect(() => {
    if ($agentTelegramLinked) {
      const { agent_id, chat_id } = $agentTelegramLinked;
      console.log(`[AgentTelegram] Agent ${agent_id} linked to Telegram chat ${chat_id}`);

      // Show success toast
      agentToastMessage = `Agent linked to Telegram successfully`;
      agentToastType = 'info';
      showAgentToast = true;
      setTimeout(() => { showAgentToast = false; }, 3000);

      // Refresh agent list to update Telegram status
      (async () => {
        try {
          const agentsResult = await listAgents();
          if (agentsResult?.status === 'success' && agentsResult.agents) {
            agentsList.set(agentsResult.agents);
          }
        } catch (error) {
          console.error('Failed to refresh agents list:', error);
        }
      })();
    }
  });

  // Reactive: Handle agent Telegram unlinked event (v0.15.0+)
  $effect(() => {
    if ($agentTelegramUnlinked) {
      const { agent_id } = $agentTelegramUnlinked;
      console.log(`[AgentTelegram] Agent ${agent_id} unlinked from Telegram`);

      // Show info toast
      agentToastMessage = `Agent unlinked from Telegram`;
      agentToastType = 'info';
      showAgentToast = true;
      setTimeout(() => { showAgentToast = false; }, 3000);

      // Refresh agent list to update Telegram status
      (async () => {
        try {
          const agentsResult = await listAgents();
          if (agentsResult?.status === 'success' && agentsResult.agents) {
            agentsList.set(agentsResult.agents);
          }
        } catch (error) {
          console.error('Failed to refresh agents list:', error);
        }
      })();
    }
  });

  // Reactive: Handle Telegram→agent messages in unified_conversation mode
  // Silently refreshes the agent chat history when Telegram bridge processes a message
  $effect(() => {
    if ($agentHistoryUpdated) {
      const { conversation_id, messages, tokens_used, token_limit, thinking } = $agentHistoryUpdated;
      console.log(`[AgentTelegramMsg] Refreshing chat history for ${conversation_id} (${messages?.length} messages)`);

      // Capture accumulated streaming text NOW (before untrack/clearAgentStreaming).
      // agentStreamingText holds tool-call traces + LLM chunks accumulated during this
      // task. Because _execute_task now uses reply_conversation_id (e.g. "agent_001"),
      // streaming events arrive with the correct conversation_id and populate this buffer.
      let capturedAgentStreaming = "";
      if (activeChatId === conversation_id) {
        // Flush any pending buffer content that hasn't been moved to agentStreamingText yet
        if (streamingBuffer) {
          agentStreamingText += streamingBuffer;
          streamingBuffer = "";
          if (streamingFlushTimeout) { clearTimeout(streamingFlushTimeout); streamingFlushTimeout = null; }
        }
        capturedAgentStreaming = agentStreamingText;
        if (capturedAgentStreaming) clearAgentStreaming();
      }

      // All side-effects are wrapped in untrack() to ensure this effect ONLY re-runs when
      // $agentHistoryUpdated changes (i.e., a new Telegram message arrives).
      // Without untrack():
      //   - chatHistories.update() makes Svelte track chatHistories as a dependency
      //     (store access inside $effect creates a subscription), causing this effect
      //     to re-run every time chatHistories changes → [ChatHistory] effect loops
      //   - activeChatId read tracks it as a dependency, causing re-run on chat switch
      //     while $agentHistoryUpdated still holds the old payload → same loop
      untrack(() => {
        // Update token usage map so the token counter reflects the agent's LLM usage
        if (tokens_used !== undefined && token_limit !== undefined && token_limit > 0) {
          tokenUsageMap = new Map(tokenUsageMap);
          tokenUsageMap.set(conversation_id, { used: tokens_used, limit: token_limit });
        }

        chatHistories.update(map => {
          const newMap = new Map(map);
          const mappedMessages = (messages || []).map((msg: any, index: number) => {
            const isUser = msg.role === 'user';
            const mapped: any = {
              id: `tg-${index}-${Date.now()}`,
              sender: isUser ? 'user' : conversation_id,
              senderName: isUser ? (msg.sender_name || 'User') : getPeerDisplayName(conversation_id),
              text: msg.content,
              timestamp: msg.timestamp ? new Date(msg.timestamp).getTime() : Date.now(),
              attachments: msg.attachments || []
            };
            return mapped;
          });
          // Attach streamingRaw and thinking to the last assistant message.
          // Prefer capturedAgentStreaming (tool calls + LLM chunks) over plain text.
          const lastAssistantIdx = [...mappedMessages].reverse().findIndex(m => m.sender !== 'user');
          if (lastAssistantIdx !== -1) {
            const idx = mappedMessages.length - 1 - lastAssistantIdx;
            mappedMessages[idx] = {
              ...mappedMessages[idx],
              streamingRaw: capturedAgentStreaming || mappedMessages[idx].text || undefined,
              thinking: thinking || undefined,
            };
          }
          newMap.set(conversation_id, mappedMessages);
          return newMap;
        });

        // Scroll to bottom if this is the active chat
        if (activeChatId === conversation_id) {
          setTimeout(() => {
            if (chatWindow) chatWindow.scrollTop = chatWindow.scrollHeight;
          }, 50);
        }
      });
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
        // Only save histories for AI chats and agent chats (excluding Telegram)
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

  // Telegram bot integration (v0.14.0+): Handle incoming Telegram messages
  $effect(() => {
    if ($telegramMessageReceived) {
      const { conversation_id, telegram_chat_id, sender_name, text, timestamp } = $telegramMessageReceived;
      console.log(`[Telegram] Adding message to chat ${conversation_id}: ${text}`);

      // Auto-create conversation in aiChats if it doesn't exist
      if (!$aiChats.has(conversation_id)) {
        aiChats.update(chats => {
          const newMap = new Map(chats);
          newMap.set(conversation_id, {
            name: `📱 Telegram (${sender_name})`,
            provider: 'telegram',  // Unique provider for visual distinction
            instruction_set_name: 'general'
          });
          return newMap;
        });
        console.log(`[Telegram] Auto-created chat ${conversation_id} in sidebar`);

        // Persist Telegram chats to localStorage for page refresh recovery
        try {
          const telegramChats = Object.fromEntries(
            Array.from($aiChats.entries())
              .filter(([_, info]) => info.provider === 'telegram')
              .map(([id, info]) => [id, info])
          );
          localStorage.setItem('dpc-telegram-chats', JSON.stringify(telegramChats));
          console.log('[Telegram] Persisted Telegram chats to localStorage:', Object.keys(telegramChats));
        } catch (error) {
          console.error('[Telegram] Failed to persist chats:', error);
        }
      }

      // Add message to chatHistories
      chatHistories.update(map => {
        const newMap = new Map(map);
        const currentMessages = newMap.get(conversation_id) || [];
        newMap.set(conversation_id, [
          ...currentMessages,
          {
            id: `telegram-${Date.now()}`,
            sender: `telegram-${telegram_chat_id}`,
            senderName: sender_name,
            text: text,
            timestamp: new Date(timestamp).getTime()
          }
        ]);
        return newMap;
      });

      // Send notification if app is in background (v0.15.0)
      (async () => {
        const messagePreview = text.length > 50 ? text.slice(0, 50) + '...' : text;
        const notified = await showNotificationIfBackground({
          title: sender_name,
          body: messagePreview
        });
        console.log(`[Notifications] Telegram message notification: ${notified ? 'system' : 'skip'}`);
      })();

      // Clear the received event after processing
      telegramMessageReceived.set(null);
    }
  });

  // Handle Telegram voice messages
  $effect(() => {
    if ($telegramVoiceReceived) {
      const { conversation_id, telegram_chat_id, sender_name, filename, file_path, duration_seconds, transcription } = $telegramVoiceReceived;
      console.log(`[Telegram] Adding voice message to chat ${conversation_id}: ${filename}`);

      // Auto-create conversation in aiChats if it doesn't exist
      if (!$aiChats.has(conversation_id)) {
        aiChats.update(chats => {
          const newMap = new Map(chats);
          newMap.set(conversation_id, {
            name: `📱 Telegram (${sender_name})`,
            provider: 'telegram',  // Unique provider for visual distinction
            instruction_set_name: 'general'
          });
          return newMap;
        });
        console.log(`[Telegram] Auto-created chat ${conversation_id} in sidebar`);

        // Persist Telegram chats to localStorage for page refresh recovery
        try {
          const telegramChats = Object.fromEntries(
            Array.from($aiChats.entries())
              .filter(([_, info]) => info.provider === 'telegram')
              .map(([id, info]) => [id, info])
          );
          localStorage.setItem('dpc-telegram-chats', JSON.stringify(telegramChats));
          console.log('[Telegram] Persisted Telegram chats to localStorage (from voice):', Object.keys(telegramChats));
        } catch (error) {
          console.error('[Telegram] Failed to persist chats:', error);
        }
      }

      // Add voice message to chatHistories
      chatHistories.update(map => {
        const newMap = new Map(map);
        const currentMessages = newMap.get(conversation_id) || [];
        newMap.set(conversation_id, [
          ...currentMessages,
          {
            id: `telegram-voice-${Date.now()}`,
            sender: `telegram-${telegram_chat_id}`,
            senderName: sender_name,
            text: transcription ? `Voice message: ${transcription}` : 'Voice message',  // Transcription in text for knowledge extraction
            timestamp: Date.now(),
            attachments: [{
              type: 'voice',
              filename: filename,
              file_path: file_path,  // Use actual file path from backend
              size_bytes: 0,
              mime_type: 'audio/ogg',
              voice_metadata: {
                duration_seconds: duration_seconds,
                sample_rate: 48000,
                channels: 1,
                codec: 'opus',
                recorded_at: new Date().toISOString()
              },
              transcription: transcription ? {
                text: transcription,
                provider: 'unknown'
              } : undefined
            }]
          }
        ]);
        return newMap;
      });

      // Send notification if app is in background (v0.15.0)
      (async () => {
        const notified = await showNotificationIfBackground({
          title: sender_name,
          body: `🎤 Voice message (${duration_seconds}s)`
        });
        console.log(`[Notifications] Telegram voice notification: ${notified ? 'system' : 'skip'}`);
      })();

      // Clear the received event after processing
      telegramVoiceReceived.set(null);
    }
  });

  // Handle Telegram image messages
  $effect(() => {
    const imageEvent = $telegramImageReceived;
    if (imageEvent) {
      const { conversation_id, telegram_chat_id, sender_name, filename, file_path, caption, size_bytes } = imageEvent;
      console.log(`[Telegram] Adding image to chat ${conversation_id}: ${filename}`);

      // Auto-create conversation in aiChats if it doesn't exist
      if (!$aiChats.has(conversation_id)) {
        aiChats.update(chats => {
          const newMap = new Map(chats);
          newMap.set(conversation_id, {
            name: `📱 Telegram (${sender_name})`,
            provider: 'telegram',
            instruction_set_name: 'general'
          });
          return newMap;
        });
        console.log(`[Telegram] Auto-created chat ${conversation_id} in sidebar (from image)`);

        // Persist Telegram chats to localStorage
        try {
          const telegramChats = Object.fromEntries(
            Array.from($aiChats.entries())
              .filter(([_, info]) => info.provider === 'telegram')
              .map(([id, info]) => [id, info])
          );
          localStorage.setItem('dpc-telegram-chats', JSON.stringify(telegramChats));
        } catch (error) {
          console.error('[Telegram] Failed to persist chats:', error);
        }
      }

      // Add image to chatHistories
      chatHistories.update(map => {
        const newMap = new Map(map);
        const currentMessages = newMap.get(conversation_id) || [];
        newMap.set(conversation_id, [
          ...currentMessages,
          {
            id: `telegram-image-${Date.now()}`,
            sender: `telegram-${telegram_chat_id}`,
            senderName: sender_name,
            text: caption || "Image",
            timestamp: Date.now(),
            attachments: [{
              type: 'image',
              filename: filename,
              file_path: file_path,
              size_bytes: size_bytes || 0
            }]
          }
        ]);
        return newMap;
      });

      // Send notification if app is in background (v0.15.0)
      (async () => {
        const notified = await showNotificationIfBackground({
          title: sender_name,
          body: `📷 Photo${caption ? ': ' + caption.slice(0, 30) : ''}`
        });
        console.log(`[Notifications] Telegram image notification: ${notified ? 'system' : 'skip'}`);
      })();

      // Clear the received event after processing
      telegramImageReceived.set(null);
    }
  });

  // Handle Telegram file/document messages
  $effect(() => {
    const fileEvent = $telegramFileReceived;
    if (fileEvent) {
      const { conversation_id, telegram_chat_id, sender_name, filename, file_path, caption, size_bytes, mime_type } = fileEvent;
      console.log(`[Telegram] Adding file to chat ${conversation_id}: ${filename}`);

      // Auto-create conversation in aiChats if it doesn't exist
      if (!$aiChats.has(conversation_id)) {
        aiChats.update(chats => {
          const newMap = new Map(chats);
          newMap.set(conversation_id, {
            name: `📱 Telegram (${sender_name})`,
            provider: 'telegram',
            instruction_set_name: 'general'
          });
          return newMap;
        });
        console.log(`[Telegram] Auto-created chat ${conversation_id} in sidebar (from file)`);

        // Persist Telegram chats to localStorage
        try {
          const telegramChats = Object.fromEntries(
            Array.from($aiChats.entries())
              .filter(([_, info]) => info.provider === 'telegram')
              .map(([id, info]) => [id, info])
          );
          localStorage.setItem('dpc-telegram-chats', JSON.stringify(telegramChats));
        } catch (error) {
          console.error('[Telegram] Failed to persist chats:', error);
        }
      }

      // Determine if it's an image or other file
      const isImage = mime_type?.startsWith('image/');

      // Add file to chatHistories
      chatHistories.update(map => {
        const newMap = new Map(map);
        const currentMessages = newMap.get(conversation_id) || [];
        newMap.set(conversation_id, [
          ...currentMessages,
          {
            id: `telegram-file-${Date.now()}`,
            sender: `telegram-${telegram_chat_id}`,
            senderName: sender_name,
            text: caption || filename,
            timestamp: Date.now(),
            attachments: [{
              type: isImage ? 'image' : 'file',
              filename: filename,
              file_path: file_path,
              size_bytes: size_bytes || 0,
              mime_type: mime_type
            }]
          }
        ]);
        return newMap;
      });

      // Send notification if app is in background (v0.15.0)
      (async () => {
        const notified = await showNotificationIfBackground({
          title: sender_name,
          body: `📎 File: ${filename}`
        });
        console.log(`[Notifications] Telegram file notification: ${notified ? 'system' : 'skip'}`);
      })();

      // Clear the received event after processing
      telegramFileReceived.set(null);
    }
  });

  // Phase 7: Context hash tracking for "Updated" status indicators
  let currentContextHash = $state("");  // Current hash from backend (when context is saved)
  let lastSentContextHash = $state(new Map<string, string>());  // Per-conversation: last hash sent to AI
  let peerContextHashes = $state(new Map<string, string>());  // Per-peer: current hash from backend
  let lastSentPeerHashes = $state(new Map<string, Map<string, string>>());  // Per-conversation, per-peer: last hash sent

  // File transfer UI state (Week 1)
  let showFileOfferDialog = $state(false);
  let currentFileOffer = $state<any>(null);
  let fileOfferToastMessage = $state("");
  let showFileOfferToast = $state(false);

  // Connection state (Phase 2: UX improvements)
  let isConnecting = $state(false);
  let connectionError = $state("");
  let showConnectionError = $state(false);

  // Send file confirmation dialog
  let showSendFileDialog = $state(false);
  let pendingFileSend = $state<{
    filePath: string;
    fileName: string;
    recipientId: string;
    recipientName: string;
    imageData?: { dataUrl: string; filename: string; sizeBytes: number };  // For screenshots
    caption?: string;  // Optional caption for images
  } | null>(null);
  let isSendingFile = $state(false);  // Prevent double-click bug

  // Image paste state (Phase 2.4: Screenshot + Vision - improved UX)
  let pendingImage = $state<{ dataUrl: string; filename: string; sizeBytes: number } | null>(null);

  // UI collapse states
  let contextPanelCollapsed = $state(false);  // Context toggle panel collapsible
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
          for (const [id, info] of Object.entries(aiChatsData)) {
            // Only restore if not already in aiChats (excluding telegram chats)
            if (!newMap.has(id) && !id.startsWith('telegram-')) {
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
            if (chatInfo.provider && !id.startsWith('telegram-')) {
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
        // Without this, history only loads when the user clicks on the agent chat.
        for (const agent of agentsResult.agents) {
          const conv_id = agent.agent_id;
          try {
            const histResult = await sendCommand('get_conversation_history', { conversation_id: conv_id });
            if (histResult?.status === 'success' && histResult.messages?.length > 0) {
              chatHistories.update(map => {
                const newMap = new Map(map);
                const msgs = histResult.messages.map((msg: any, index: number) => ({
                  id: `agent-init-${index}-${Date.now()}`,
                  sender: msg.role === 'user' ? 'user' : conv_id,
                  senderName: msg.role === 'user' ? (msg.sender_name || 'You') : (agent.name || conv_id),
                  text: msg.content,
                  timestamp: msg.timestamp ? new Date(msg.timestamp).getTime() : Date.now() - (histResult.messages.length - index) * 1000,
                  attachments: msg.attachments || []
                }));
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

  // Reactive: Open model download dialog when model not cached (v0.13.5)
  $effect(() => {
    if ($whisperModelDownloadRequired) {
      console.log('[ModelDownload] Model download required:', $whisperModelDownloadRequired);
      modelDownloadInfo = $whisperModelDownloadRequired;
      showModelDownloadDialog = true;
      isDownloadingModel = false;
    }
  });

  // Reactive: Update download status when download starts (v0.13.5)
  $effect(() => {
    if ($whisperModelDownloadStarted) {
      console.log('[ModelDownload] Download started:', $whisperModelDownloadStarted);
      isDownloadingModel = true;
    }
  });

  // Reactive: Close dialog and show success when download completes (v0.13.5)
  $effect(() => {
    if ($whisperModelDownloadCompleted) {
      console.log('[ModelDownload] Download completed:', $whisperModelDownloadCompleted);
      isDownloadingModel = false;
      showModelDownloadDialog = false;

      // Show success toast
      modelDownloadToastMessage = '✅ Model download successful! Voice transcription is now available.';
      modelDownloadToastType = 'info';
      showModelDownloadToast = true;

      // Clear the event
      whisperModelDownloadCompleted.set(null);
    }
  });

  // Reactive: Show error and reset when download fails (v0.13.5)
  $effect(() => {
    if ($whisperModelDownloadFailed) {
      console.error('[ModelDownload] Download failed:', $whisperModelDownloadFailed);
      isDownloadingModel = false;

      // Show error toast
      modelDownloadToastMessage = `❌ Model download failed: ${$whisperModelDownloadFailed.error}`;
      modelDownloadToastType = 'error';
      showModelDownloadToast = true;

      // Clear the event
      whisperModelDownloadFailed.set(null);
    }
  });

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
      const {conversation_id, tokens_used, token_limit, usage_percent} = $tokenWarning;

      // Guard: Only update if values actually changed (prevent infinite loop)
      const existing = tokenUsageMap.get(conversation_id);
      if (existing && existing.used === tokens_used && existing.limit === token_limit) {
        return; // Values unchanged, skip update
      }

      // Update token usage map
      tokenUsageMap = new Map(tokenUsageMap);
      tokenUsageMap.set(conversation_id, {used: tokens_used, limit: token_limit});

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
    limit: currentTokenUsage.limit > 0 ? currentTokenUsage.limit : DEFAULT_TOKEN_LIMIT
  });

  // Reactive: Estimate token usage including current input (real-time feedback)
  let estimatedUsage = $derived(
    estimateConversationUsage(effectiveTokenUsage, currentInput)
  );

  // Reactive: Determine warning level based on estimated usage
  let tokenWarningLevel = $derived(
    !$aiChats.has(activeChatId)
      ? 'none'
      : estimatedUsage.percentage >= 1.0
        ? 'critical'
        : estimatedUsage.percentage >= 0.9
          ? 'warning'
          : 'none'
  );

  let showTokenBanner = $derived(
    tokenWarningLevel === 'critical' || tokenWarningLevel === 'warning'
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

  // Reactive: Sync chat history from backend when switching to peer chat with no messages (v0.11.2)
  // Handles page refresh scenario: frontend loses chatHistories, backend keeps conversation_monitors
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

                  return {
                    id: `backend-${index}-${Date.now()}`,
                    sender: sender,
                    senderName: senderName,
                    text: msg.content,
                    timestamp: timestamp,
                    attachments: msg.attachments || []
                  };
                });
                newMap.set(activeChatId, loadedMessages);
                console.log(`[ChatHistory] Updated chatHistories with ${loadedMessages.length} messages`);
                return newMap;
              });

              // Update token counter with restored history token counts
              if (result.tokens_used !== undefined && result.token_limit !== undefined && result.token_limit > 0) {
                tokenUsageMap = new Map(tokenUsageMap);
                tokenUsageMap.set(activeChatId, { used: result.tokens_used, limit: result.token_limit });
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
      } else {
        console.log(`[ChatHistory] Skipping load - already have ${currentHistory.length} messages`);
      }
    }
  });

  // Clear input state when switching chats (prevent cross-chat pollution)
  // Track previous chat to detect actual chat switches
  let previousChatId: string = '';

  $effect(() => {
    // Track activeChatId dependency
    const currentChat = activeChatId;

    // Skip first run (just initialize)
    if (previousChatId === '') {
      previousChatId = currentChat;
      return;
    }

    // When actually switching to a different chat
    if (currentChat !== previousChatId) {
      // 1. Save draft for previous chat
      chatDraftInputs = new Map(chatDraftInputs).set(previousChatId, currentInput);

      // 2. Restore draft for new chat (if exists)
      const draft = chatDraftInputs.get(currentChat);
      currentInput = draft !== undefined ? draft : "";

      // 3. Clear pending image and voice preview
      if (pendingImage !== null) {
        pendingImage = null;
      }
      if (voicePreview !== null) {
        voicePreview = null;
      }

      // 4. Update tracking
      previousChatId = currentChat;
    }
  });

  // Phase 7: Reactive: Check if context window is full (100% or more) - uses estimated total
  // Only applies to actual AI chats (not Telegram, which are in aiChats for sidebar)
  let isContextWindowFull = $derived(isActuallyAIChat && estimatedUsage.percentage >= 1.0);

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

  // File transfer event handlers (Week 1)
  $effect(() => {
    if ($fileTransferOffer) {
      const { node_id, filename, size_bytes, transfer_id, sender_name, group_id } = $fileTransferOffer;

      // Skip acceptance dialog for group files (auto-accepted by backend v0.19.1+)
      if (group_id) {
        console.log(`Group file offer received (auto-accepted): ${filename} from ${sender_name || node_id.slice(0, 15)}...`);
        // Show toast notification instead of dialog
        fileOfferToastMessage = `Receiving file: ${filename} from ${sender_name || 'group member'}`;
        showFileOfferToast = true;
        setTimeout(() => showFileOfferToast = false, 3000);
        return;
      }

      currentFileOffer = $fileTransferOffer;
      showFileOfferDialog = true;
      console.log(`File offer received: ${filename} (${(size_bytes / 1024).toFixed(1)} KB) from ${node_id.slice(0, 15)}...`);

      // Send notification for file offer
      (async () => {
        const notified = await showNotificationIfBackground({
          title: `File from ${sender_name || node_id.slice(0, 16)}`,
          body: `${filename} (${(size_bytes / 1048576).toFixed(2)} MB)`
        });
        console.log(`[Notifications] File offer notification: ${notified ? 'system' : 'skip'}`);
      })();
    }
  });

  $effect(() => {
    if ($fileTransferComplete) {
      const { filename, node_id, direction } = $fileTransferComplete;
      fileOfferToastMessage = direction === "download"
        ? `✓ File downloaded: ${filename}`
        : `✓ File sent: ${filename}`;
      showFileOfferToast = true;
      setTimeout(() => showFileOfferToast = false, 5000);

      // Send notification for file transfer complete
      (async () => {
        const notified = await showNotificationIfBackground({
          title: 'File Transfer Complete',
          body: `${filename} (${direction})`
        });
        console.log(`[Notifications] File complete notification: ${notified ? 'system' : 'skip'}`);
      })();
    }
  });

  $effect(() => {
    if ($fileTransferCancelled) {
      const { filename, reason } = $fileTransferCancelled;
      fileOfferToastMessage = `✗ Transfer cancelled: ${filename} (${reason})`;
      showFileOfferToast = true;
      setTimeout(() => showFileOfferToast = false, 5000);

      // Send notification for file transfer cancelled
      (async () => {
        const notified = await showNotificationIfBackground({
          title: 'Transfer Cancelled',
          body: `${filename} (${reason})`
        });
        console.log(`[Notifications] File cancelled notification: ${notified ? 'system' : 'skip'}`);
      })();
    }
  });

  // Reactive: Handle voice transcription complete (v0.13.2+)
  $effect(() => {
    if ($voiceTranscriptionComplete) {
      const { transfer_id, node_id, text, transcriber_node_id, provider, confidence, language, timestamp, remote_provider_node_id } = $voiceTranscriptionComplete;
      console.log(`[VoiceTranscription] Received transcription for ${transfer_id}: "${text}"`);

      // Find the message with this transfer_id and add transcription to attachment
      // CRITICAL: Must create NEW objects at every level to trigger Svelte reactivity
      chatHistories.update(histories => {
        const updatedHistories = new Map();

        for (const [chatId, messages] of histories) {
          // Create NEW messages array for this chat
          const updatedMessages = messages.map(message => {
            // Check if this message has the voice attachment we're looking for
            if (message.attachments) {
              const hasTargetVoice = message.attachments.some(
                att => att.type === 'voice' && att.transfer_id === transfer_id
              );

              if (hasTargetVoice) {
                // Create NEW message with NEW attachments array
                return {
                  ...message,
                  attachments: message.attachments.map(attachment => {
                    if (attachment.type === 'voice' && attachment.transfer_id === transfer_id) {
                      // Create NEW attachment with transcription
                      console.log(`[VoiceTranscription] Adding transcription to message in chat ${chatId}`);
                      return {
                        ...attachment,
                        transcription: {
                          text,
                          provider,
                          transcriber_node_id,
                          confidence,
                          language,
                          timestamp,
                          remote_provider_node_id
                        }
                      };
                    }
                    return attachment;
                  })
                };
              }
            }
            return message;
          });

          updatedHistories.set(chatId, updatedMessages);
        }

        return updatedHistories;
      });
    }
  });

  // Reactive: Handle voice transcription received from peer (v0.13.2+)
  $effect(() => {
    if ($voiceTranscriptionReceived) {
      const { transfer_id, node_id, text, transcriber_node_id, provider, confidence, language, timestamp } = $voiceTranscriptionReceived;
      console.log(`[VoiceTranscription] Received transcription from peer for ${transfer_id}: "${text}"`);

      // Find the message with this transfer_id and add transcription to attachment
      // CRITICAL: Must create NEW objects at every level to trigger Svelte reactivity
      chatHistories.update(histories => {
        const updatedHistories = new Map();

        for (const [chatId, messages] of histories) {
          // Create NEW messages array for this chat
          const updatedMessages = messages.map(message => {
            // Check if this message has the voice attachment we're looking for
            if (message.attachments) {
              const hasTargetVoice = message.attachments.some(
                att => att.type === 'voice' && att.transfer_id === transfer_id
              );

              if (hasTargetVoice) {
                // Create NEW message with NEW attachments array
                return {
                  ...message,
                  attachments: message.attachments.map(attachment => {
                    if (attachment.type === 'voice' && attachment.transfer_id === transfer_id) {
                      // Create NEW attachment with transcription from peer
                      console.log(`[VoiceTranscription] Adding peer transcription to message in chat ${chatId}`);
                      return {
                        ...attachment,
                        transcription: {
                          text,
                          provider,
                          transcriber_node_id,
                          confidence,
                          language,
                          timestamp
                        }
                      };
                    }
                    return attachment;
                  })
                };
              }
            }
            return message;
          });

          updatedHistories.set(chatId, updatedMessages);
        }

        return updatedHistories;
      });
    }
  });

  // Reactive: Handle chat history restored (v0.11.2)
  $effect(() => {
    if ($historyRestored) {
      console.log(`Restoring ${$historyRestored.message_count} messages to chat with ${$historyRestored.conversation_id}`);

      // Update chatHistories store - convert backend format to UI format
      chatHistories.update(map => {
        const newMap = new Map(map);
        const restoredMessages = $historyRestored.messages.map((msg: any, index: number) => ({
          id: `restored-${index}-${Date.now()}`,
          sender: msg.role === 'user' ? 'user' : $historyRestored.conversation_id,
          senderName: msg.role === 'user' ? 'You' : getPeerDisplayName($historyRestored.conversation_id),
          text: msg.content,
          timestamp: Date.now() - ($historyRestored.messages.length - index) * 1000,  // Stagger timestamps
          attachments: msg.attachments || []
        }));
        newMap.set($historyRestored.conversation_id, restoredMessages);
        return newMap;
      });

      // Scroll to bottom after restoring history
      setTimeout(() => {
        if (chatWindow) {
          chatWindow.scrollTop = chatWindow.scrollHeight;
        }
      }, 100);

      // Show success toast
      fileOfferToastMessage = `✓ Chat history restored: ${$historyRestored.message_count} messages`;
      showFileOfferToast = true;
      setTimeout(() => showFileOfferToast = false, 3000);
    }
  });

  // Reactive: Handle group history synced (v0.20.0) - reload when P2P sync completes
  $effect(() => {
    if ($groupHistorySynced && $groupHistorySynced.group_id) {
      const syncedGroupId = $groupHistorySynced.group_id;
      const messageCount = $groupHistorySynced.message_count || 0;
      console.log(`[GroupHistorySync] Group ${syncedGroupId} synced with ${messageCount} messages`);

      // Only reload if this is the active chat
      if (activeChatId === syncedGroupId && messageCount > 0) {
        console.log(`[GroupHistorySync] Reloading history for active group ${syncedGroupId}`);

        // Load history from backend (async IIFE to allow await in reactive statement)
        (async () => {
          try {
            const response = await sendCommand('get_conversation_history', { conversation_id: syncedGroupId });
            if (response.status === 'success' && response.messages?.length > 0) {
              console.log(`[GroupHistorySync] Loaded ${response.messages.length} messages from backend`);

              // Update chatHistories store
              chatHistories.update(map => {
                const newMap = new Map(map);
                const syncedMessages = response.messages.map((msg: any, index: number) => ({
                  id: msg.id || `synced-${index}-${Date.now()}`,
                  sender: msg.node_id || (msg.role === 'user' ? 'user' : syncedGroupId),
                  senderName: msg.display_name || (msg.role === 'user' ? 'You' : getPeerDisplayName(msg.node_id || syncedGroupId)),
                  text: msg.content || msg.text,
                  timestamp: new Date(msg.timestamp).getTime() || Date.now() - (response.messages.length - index) * 1000,
                  attachments: msg.attachments || []
                }));
                newMap.set(syncedGroupId, syncedMessages);
                return newMap;
              });

              // Show success toast
              fileOfferToastMessage = `✓ Group history synced: ${response.messages.length} messages`;
              showFileOfferToast = true;
              setTimeout(() => showFileOfferToast = false, 3000);

              // Scroll to bottom
              setTimeout(() => {
                if (chatWindow) {
                  chatWindow.scrollTop = chatWindow.scrollHeight;
                }
              }, 100);
            }
          } catch (err: any) {
            console.error('[GroupHistorySync] Error loading synced history:', err);
          }
        })();
      }
    }
  });

  // Phase 7: Reactive: Check if local context has changed (not yet sent to AI)
  let localContextUpdated = $derived(currentContextHash && lastSentContextHash.get(activeChatId) !== currentContextHash);

  // Phase 7: Reactive: Check which peer contexts have changed (not yet sent to AI)
  let peerContextsUpdated = $derived(new Set(
    Array.from(peerContextHashes.keys()).filter(nodeId => {
      const conversationPeerHashes = lastSentPeerHashes.get(activeChatId);
      if (!conversationPeerHashes) return true;  // Never sent
      return conversationPeerHashes.get(nodeId) !== peerContextHashes.get(nodeId);
    })
  ));

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

  // Reactive: Reset selected peer contexts if peers disconnect
  $effect(() => {
    if (selectedPeerContexts.size > 0 && $nodeStatus?.p2p_peers) {
      const connectedPeers = new Set($nodeStatus.p2p_peers);
      let needsUpdate = false;
      for (const peerId of selectedPeerContexts) {
        if (!connectedPeers.has(peerId)) {
          selectedPeerContexts.delete(peerId);
          needsUpdate = true;
          console.log(`Peer ${peerId} disconnected, removing from selected contexts`);
        }
      }
      if (needsUpdate) {
        // Trigger reactivity by creating new Set instance (required for Svelte 5 $state)
        selectedPeerContexts = new Set(selectedPeerContexts);
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

  function isNearBottom(element: HTMLElement | undefined, threshold: number = 150): boolean {
    if (!element) return true;
    const { scrollTop, scrollHeight, clientHeight } = element;
    return scrollHeight - scrollTop - clientHeight < threshold;
  }
  
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

  // --- MENTION AUTOCOMPLETE (Group Chats) ---
  // Get mentionable members for the current group chat (excludes self)
  function getMentionableMembers(): Array<{ node_id: string; name: string }> {
    if (!activeChatId.startsWith('group-')) return [];

    const group = $groupChats.get(activeChatId);
    if (!group?.members) return [];

    const selfId = $nodeStatus?.node_id || '';

    // Filter out self and map to display names (full name, no truncation)
    return group.members
      .filter((nodeId: string) => nodeId !== selfId)
      .map((nodeId: string) => ({
        node_id: nodeId,
        name: peerDisplayNames.get(nodeId)?.split(' | ')[0] || nodeId
      }));
  }

  // Filter mentionable members by query
  let filteredMentionMembers = $derived.by(() => {
    const members = getMentionableMembers();
    if (!mentionQuery) return members;

    const lowerQuery = mentionQuery.toLowerCase();
    return members.filter(
      m => m.name.toLowerCase().includes(lowerQuery) || m.node_id.toLowerCase().includes(lowerQuery)
    );
  });

  // Handle input changes to detect @ mentions
  function handleMentionInput(event: Event) {
    const textarea = event.target as HTMLTextAreaElement;
    const value = textarea.value;
    const cursorPos = textarea.selectionStart;

    // Only enable in group chats
    if (!activeChatId.startsWith('group-')) {
      mentionAutocompleteVisible = false;
      return;
    }

    // Find @ before cursor
    const lastAtIndex = value.lastIndexOf('@', cursorPos - 1);
    if (lastAtIndex !== -1) {
      const textAfterAt = value.slice(lastAtIndex + 1, cursorPos);
      // Check for space (ends mention) or newline
      if (!textAfterAt.includes(' ') && !textAfterAt.includes('\n')) {
        mentionQuery = textAfterAt;
        mentionStartPosition = lastAtIndex;
        mentionSelectedIndex = 0;

        // Calculate dropdown position (approximate)
        const textareaRect = textarea.getBoundingClientRect();
        mentionDropdownPosition = {
          top: textareaRect.bottom + 4,
          left: textareaRect.left + (lastAtIndex * 8) // Approximate char width
        };

        mentionAutocompleteVisible = true;
        return;
      }
    }

    mentionAutocompleteVisible = false;
  }

  // Handle mention selection
  function handleMentionSelect(member: { node_id: string; name: string }) {
    // Replace @query with @Name | node_id (full format)
    const before = currentInput.slice(0, mentionStartPosition);
    const after = currentInput.slice(mentionStartPosition + mentionQuery.length + 1);
    currentInput = `${before}@${member.name} | ${member.node_id} ${after}`;

    mentionAutocompleteVisible = false;
    mentionSelectedIndex = 0;
  }

  // Handle mention navigation (from keyboard events)
  function handleMentionNavigate(direction: 'up' | 'down') {
    const maxIndex = filteredMentionMembers.length - 1;
    if (direction === 'down') {
      mentionSelectedIndex = Math.min(mentionSelectedIndex + 1, maxIndex);
    } else {
      mentionSelectedIndex = Math.max(mentionSelectedIndex - 1, 0);
    }
  }

  // Close mention autocomplete
  function closeMentionAutocomplete() {
    mentionAutocompleteVisible = false;
    mentionSelectedIndex = 0;
  }

  // --- PEER CONTEXT SELECTION ---
  function togglePeerContext(peerId: string) {
    if (selectedPeerContexts.has(peerId)) {
      selectedPeerContexts.delete(peerId);
    } else {
      selectedPeerContexts.add(peerId);
    }
    // Trigger reactivity by creating new Set instance (required for Svelte 5 $state)
    selectedPeerContexts = new Set(selectedPeerContexts);
  }

  // Update instruction set for active chat
  function updateInstructionSet(newInstructionSet: string) {
    const chatMetadata = $aiChats.get(activeChatId);
    if (chatMetadata) {
      chatMetadata.instruction_set_name = newInstructionSet;
      // Trigger reactivity by creating new Map instance
      aiChats.update(map => new Map(map));
    }
  }

  // --- CHAT PANEL RESIZE HANDLERS ---
  function startResize(e: MouseEvent) {
    isResizing = true;
    resizeStartY = e.clientY;
    resizeStartHeight = chatPanelHeight;

    // Prevent text selection during resize
    e.preventDefault();

    // Add body class to prevent text selection
    document.body.classList.add('resizing');

    // Add global listeners for mouse move and up
    document.addEventListener('mousemove', handleResize);
    document.addEventListener('mouseup', stopResize);
  }

  function handleResize(e: MouseEvent) {
    if (!isResizing) return;

    // Calculate new height (add because we want to grow downward)
    const deltaY = e.clientY - resizeStartY;
    const newHeight = resizeStartHeight + deltaY;

    // Enforce minimum constraint only (no maximum)
    chatPanelHeight = Math.max(300, newHeight);
  }

  function stopResize() {
    isResizing = false;

    // Remove body class
    document.body.classList.remove('resizing');

    // Remove global listeners
    document.removeEventListener('mousemove', handleResize);
    document.removeEventListener('mouseup', stopResize);

    // Save the new height to localStorage
    localStorage.setItem('chatPanelHeight', chatPanelHeight.toString());
  }

  // --- CHAT FUNCTIONS ---
  async function handleSendMessage() {
    // Handle image + text
    if (pendingImage) {
      const text = currentInput.trim();
      const imageData = pendingImage;

      // Clear input, draft, and pending image
      currentInput = "";
      chatDraftInputs = new Map(chatDraftInputs).set(activeChatId, "");
      pendingImage = null;

      // Check if this is an AI chat, Telegram chat, or P2P chat (Phase 2.3: Fix P2P screenshot sharing)
      // Note: Telegram chats are in $aiChats but should be handled separately (not as AI chats)
      if ($aiChats.has(activeChatId) && !activeChatId.startsWith('telegram-')) {
        // AI chat: Add to conversation history with attachment
        chatHistories.update(h => {
          const newMap = new Map(h);
          const hist = newMap.get(activeChatId) || [];
          newMap.set(activeChatId, [...hist, {
            id: crypto.randomUUID(),
            sender: 'user',
            text: text || '[Image]',
            timestamp: Date.now(),
            attachments: [{
              type: 'image',
              filename: imageData.filename,
              thumbnail: imageData.dataUrl,
              size_bytes: imageData.sizeBytes
            }]
          }]);
          return newMap;
        });

        // AI chat: Send for vision analysis
        try {
          setChatLoading(activeChatId, true);

          // Parse vision provider to extract compute_host for remote vision
          const visionProvider = parseProviderSelection(selectedVisionProvider);

          const payload: any = {
            conversation_id: activeChatId,
            image_base64: imageData.dataUrl,
            filename: imageData.filename,
            caption: text,
            provider_alias: visionProvider.alias,
            chat_provider: $chatProviders.get(activeChatId) || null
          };

          // Add compute_host if using remote vision provider
          if (visionProvider.source === 'remote' && visionProvider.nodeId) {
            payload.compute_host = visionProvider.nodeId;
          }

          await sendCommand('send_image', payload);
          autoScroll();
          // Note: Don't clear loading here!
          // The ai_response_with_image event handler will clear it when the vision response arrives
        } catch (error) {
          console.error('Error sending image:', error);

          // Parse error into user-friendly message
          let userMessage = 'Failed to analyze image';
          let errorDetails = '';

          const errorStr = String(error);

          if (errorStr.includes('Failed to connect to Ollama')) {
            userMessage = 'Ollama Connection Failed';
            errorDetails = 'Ollama is not running. Please start Ollama and try again.\n\nDownload from: https://ollama.com/download';
          } else if (errorStr.includes('memory layout cannot be allocated') || errorStr.includes('out of memory') || errorStr.includes('OOM') || errorStr.includes('VRAM')) {
            userMessage = 'Out of Memory';
            errorDetails = 'Not enough GPU memory for vision analysis. Try closing other GPU-intensive apps or using a smaller model.';
          } else if (errorStr.includes('Ollama vision API failed')) {
            // Generic Ollama vision failure (not connection or memory specific)
            userMessage = 'Ollama Vision Failed';
            errorDetails = 'The vision model encountered an error. Check Ollama logs for details.';
          } else if (errorStr.includes('connection refused') || errorStr.includes('Connection refused')) {
            userMessage = 'Connection Refused';
            errorDetails = 'The AI service is not accepting connections. Please check if it\'s running.';
          } else if (errorStr.includes('timeout') || errorStr.includes('Timeout')) {
            userMessage = 'Request Timeout';
            errorDetails = 'The AI service took too long to respond. Try again or use a smaller model.';
          } else {
            // Extract meaningful part of generic errors
            const match = errorStr.match(/RuntimeError:\s*(.+)/);
            if (match) {
              errorDetails = match[1];
            } else {
              errorDetails = errorStr.slice(0, 200); // Truncate long errors
            }
          }

          // Add error message to chat history (like AI response but with error styling)
          chatHistories.update(h => {
            const newMap = new Map(h);
            const hist = newMap.get(activeChatId) || [];
            newMap.set(activeChatId, [
              ...hist,
              {
                id: crypto.randomUUID(),
                sender: 'ai',
                text: `⚠️ **${userMessage}**\n\n${errorDetails}`,
                timestamp: Date.now(),
                isError: true
              }
            ]);
            return newMap;
          });

          autoScroll();
          setChatLoading(activeChatId, false);
        }
      } else if (activeChatId.startsWith('telegram-')) {
        // Telegram chat: Save image to temp file and send via Telegram
        try {
          // Convert data URL to blob and save to temp file
          const response = await fetch(imageData.dataUrl);
          const blob = await response.blob();
          const arrayBuffer = await blob.arrayBuffer();
          const uint8Array = new Uint8Array(arrayBuffer);

          // Check if Tauri environment (same detection as line 424)
          const isTauriEnv = typeof window !== 'undefined' && (
            (window as any).isTauri === true ||
            !!(window as any).__TAURI__
          );

          if (isTauriEnv) {
            const { writeFile, BaseDirectory, mkdir } = await import('@tauri-apps/plugin-fs');
            const { invoke } = await import('@tauri-apps/api/core');

            const timestamp = Date.now();
            const filename = imageData.filename || `screenshot_${timestamp}.png`;
            const relativePath = `.dpc/temp/${filename}`;

            // Ensure temp directory exists
            await mkdir('.dpc/temp', { baseDir: BaseDirectory.Home, recursive: true });

            // Write file to home directory
            await writeFile(relativePath, uint8Array, { baseDir: BaseDirectory.Home });

            // Get home directory path and construct full path for backend
            const homeDir = await invoke<string>('get_home_directory');
            const fullPath = `${homeDir}/${relativePath}`;

            // Send to Telegram with the full file path
            await sendToTelegram(activeChatId, text || '', [], undefined, undefined, undefined, fullPath);

            // Add to local history
            chatHistories.update(h => {
              const newMap = new Map(h);
              const hist = newMap.get(activeChatId) || [];
              newMap.set(activeChatId, [...hist, {
                id: crypto.randomUUID(),
                sender: 'user',
                text: text || '',
                timestamp: Date.now(),
                attachments: [{
                  type: 'image',
                  filename: filename,
                  file_path: fullPath,
                  size_bytes: uint8Array.length
                }]
              }]);
              return newMap;
            });

            console.log('[Telegram] Screenshot sent to Telegram');
          } else {
            throw new Error('Telegram file transfer requires Tauri desktop app');
          }
        } catch (error) {
          console.error('Error sending screenshot to Telegram:', error);
          fileOfferToastMessage = `Failed to send screenshot to Telegram: ${error}`;
          showFileOfferToast = true;
          setTimeout(() => showFileOfferToast = false, 5000);
        }
      } else if (activeChatId.startsWith('group-')) {
        // Group chat: Send screenshot via group fan-out (v0.19.0)
        // Use custom Svelte dialog for confirmation (fixes Tauri auto-accept bug)
        const group = $groupChats.get(activeChatId);
        const groupName = group?.name || 'group';
        pendingFileSend = {
          filePath: '',
          fileName: imageData.filename,
          recipientId: activeChatId,
          recipientName: `group "${groupName}"`,
          imageData: imageData,
          caption: text
        };
        showSendFileDialog = true;
        return;  // Exit early - actual send happens in handleConfirmSendFile
      } else {
        // P2P chat: Send screenshot via file transfer
        try {
          // Check image size and warn if large
          const imageSizeBytes = imageData.dataUrl.length * 0.75; // Approximate base64 overhead
          const imageSizeMB = imageSizeBytes / (1024 * 1024);

          if (imageSizeMB > 25) {
            const confirm = window.confirm(
              `This screenshot is ${imageSizeMB.toFixed(1)} MB. Large images may take time to upload. Continue?`
            );
            if (!confirm) {
              pendingImage = null;
              return;
            }
          }

          // Send screenshot to peer via backend
          await sendCommand("send_p2p_image", {
            node_id: activeChatId,
            image_base64: imageData.dataUrl,
            filename: imageData.filename,
            text: text  // Include user caption with screenshot
          });

          // Success - backend will broadcast new_p2p_message when transfer completes
          console.log("Screenshot transfer initiated successfully");
        } catch (error) {
          console.error('Error sending screenshot:', error);
          // Extract error message from Error object
          const errorMsg = error instanceof Error ? error.message : String(error);
          fileOfferToastMessage = `Failed to send screenshot: ${errorMsg}`;
          showFileOfferToast = true;
          setTimeout(() => showFileOfferToast = false, 5000);
        }
        // Note: currentInput and pendingImage already cleared at top of function
      }
      return;
    }

    // Handle text-only message
    if (!currentInput.trim()) return;

    const text = currentInput.trim();
    currentInput = "";

    // Clear draft for this chat after sending
    chatDraftInputs = new Map(chatDraftInputs).set(activeChatId, "");

    // Clear streaming text when sending a new message
    clearAgentStreaming();

    chatHistories.update(h => {
      const newMap = new Map(h);
      const hist = newMap.get(activeChatId) || [];
      newMap.set(activeChatId, [...hist, { id: crypto.randomUUID(), sender: 'user', text, timestamp: Date.now() }]);
      return newMap;
    });

    // Check if this is an AI chat (local_ai or ai_chat_*), but NOT a Telegram chat
    // Telegram chats are in $aiChats for sidebar display but should NOT trigger AI queries
    if ($aiChats.has(activeChatId) && !activeChatId.startsWith('telegram-')) {
      setChatLoading(activeChatId, true);
      const commandId = crypto.randomUUID();

      // Track which chat this command belongs to
      commandToChatMap.set(commandId, activeChatId);

      chatHistories.update(h => {
        const newMap = new Map(h);
        const hist = newMap.get(activeChatId) || [];
        newMap.set(activeChatId, [...hist, {
          id: crypto.randomUUID(),
          sender: 'ai',
          text: 'Thinking...',
          timestamp: Date.now(),
          commandId: commandId
        }]);
        return newMap;
      });

      // Get chat metadata for instruction set
      const chatMetadata = $aiChats.get(activeChatId);

      // Prepare AI query payload with optional compute host and provider/model
      const payload: any = {
        prompt: text,
        include_context: includePersonalContext,  // Add context inclusion flag
        conversation_id: activeChatId,  // Phase 7: Pass conversation ID for history tracking
        ai_scope: selectedAIScope || null,  // AI Scope for filtering (null = no filtering)
        instruction_set_name: chatMetadata?.instruction_set_name || 'general'  // Instruction set for this conversation
      };

      // Add peer contexts if any are selected
      if (selectedPeerContexts.size > 0) {
        payload.context_ids = Array.from(selectedPeerContexts);
      }

      // Determine provider: use chat-specific provider if set, otherwise use dropdown selection
      const chatSpecificProvider = $chatProviders.get(activeChatId);

      if (chatSpecificProvider) {
        // Use chat-specific provider (e.g., dpc_agent for agent chats)
        payload.provider = chatSpecificProvider;

        // For DPC Agent, pass the underlying LLM provider (Phase 3: per-agent provider selection)
        if (chatSpecificProvider === 'dpc_agent' && chatMetadata?.llm_provider) {
          payload.agent_llm_provider = chatMetadata.llm_provider;
        }
      } else {
        // Fall back to dropdown selection (supports remote inference)
        const textProvider = parseProviderSelection(selectedTextProvider);

        if (textProvider.source === 'remote' && textProvider.nodeId) {
          // Remote inference - send compute_host and provider alias
          payload.compute_host = textProvider.nodeId;
          payload.provider = textProvider.alias;
        } else {
          // Local inference - pass provider alias
          if (textProvider.alias) {
            payload.provider = textProvider.alias;
          }
        }
      }

      const success = sendCommand("execute_ai_query", payload, commandId);
      if (!success) {
        setChatLoading(activeChatId, false);
        chatHistories.update(h => {
          const newMap = new Map(h);
          const hist = newMap.get(activeChatId) || [];
          newMap.set(activeChatId, hist.map(m =>
            m.commandId === commandId ? { ...m, sender: 'system', text: 'Error: Not connected' } : m
          ));
          return newMap;
        });
        // Clean up the command mapping
        commandToChatMap.delete(commandId);
      }
    } else if (activeChatId.startsWith('telegram-')) {
      // Telegram chat: Send message to Telegram
      try {
        const linkedChatId = $telegramLinkedChats.get(activeChatId);
        if (linkedChatId) {
          await sendToTelegram(activeChatId, text);
          console.log(`[Telegram] Sent message to Telegram chat ${linkedChatId}`);
        } else {
          console.warn(`[Telegram] No linked chat ID for ${activeChatId}, message not sent to Telegram`);
        }
      } catch (error) {
        console.error('[Telegram] Failed to send message:', error);
      }
    } else if (activeChatId.startsWith('group-')) {
      // Group chat: Send via group fan-out (v0.19.0)
      sendGroupMessage(activeChatId, text);
    } else {
      // P2P chat: Send via P2P
      sendCommand("send_p2p_message", { target_node_id: activeChatId, text });
    }
    
    autoScroll();
  }
  
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
        // Tries: DHT lookup → Peer cache → Hub WebRTC
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

  // Load AI scopes from firewall rules (privacy_rules.json)
  // IMPORTANT PATTERN: If you add UI components that display data from privacy_rules.json
  // (e.g., node_groups dropdown, compute settings toggle, device_sharing rules), follow this pattern:
  // 1. Create a load function (like loadAIScopes below)
  // 2. Add a guard flag (like aiScopesLoaded) to prevent infinite reactive loops
  // 3. Add reactive statements to reload on connection and firewall rules updates
  // 4. Subscribe to $firewallRulesUpdated store to trigger reload when user saves rules
  // This ensures UI stays in sync with privacy_rules.json without requiring page refresh.
  async function loadAIScopes() {
    try {
      const result = await sendCommand("get_firewall_rules", {});
      if (result.status === "success" && result.rules?.ai_scopes) {
        // Extract scope names (excluding _comment fields)
        availableAIScopes = Object.keys(result.rules.ai_scopes).filter(key => !key.startsWith('_'));
      } else {
        availableAIScopes = [];
      }
    } catch (error) {
      console.error("Error loading AI scopes:", error);
      availableAIScopes = [];
    } finally {
      // Mark as loaded regardless of success/failure to prevent infinite loop
      aiScopesLoaded = true;
    }
  }

  // Load AI scopes when WebSocket connects (only once per connection)
  $effect(() => {
    if ($connectionStatus === "connected" && !aiScopesLoaded) {
      loadAIScopes();
    }
  });

  // Reset AI scopes loaded flag on disconnection (to reload on reconnect)
  $effect(() => {
    if ($connectionStatus === "disconnected" || $connectionStatus === "error") {
      aiScopesLoaded = false;
    }
  });

  // Reload AI scopes when firewall rules are updated (via FirewallEditor)
  // IMPORTANT: This reactive statement ensures UI updates immediately after saving firewall rules.
  // If you add more UI components that read from privacy_rules.json, add similar reactive
  // statements here to reload their data when $firewallRulesUpdated changes.
  // Example: if ($firewallRulesUpdated && $connectionStatus === "connected") { loadNodeGroups(); }
  $effect(() => {
    if ($firewallRulesUpdated && $connectionStatus === "connected") {
      aiScopesLoaded = false;
      loadAIScopes();
    }
  });

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

  // Group chat handlers (v0.19.0)
  async function handleCreateGroup(event: CustomEvent) {
    const { name, topic, member_node_ids } = event.detail;
    try {
      const result = await createGroupChat(name, topic, member_node_ids);
      if (result && result.status === "success" && result.group) {
        const groupId = result.group.group_id;

        // Update groupChats store so group appears in sidebar
        groupChats.update(map => {
          const newMap = new Map(map);
          newMap.set(groupId, result.group);
          return newMap;
        });

        // Ensure chatHistories entry exists
        chatHistories.update(h => {
          if (!h.has(groupId)) {
            const newMap = new Map(h);
            newMap.set(groupId, []);
            return newMap;
          }
          return h;
        });
        // Switch to the new group chat
        activeChatId = groupId;
      }
      showNewGroupDialog = false;
    } catch (e) {
      console.error("Failed to create group:", e);
    }
  }

  async function handleLeaveGroup(groupId: string) {
    try {
      await leaveGroup(groupId);
      if (activeChatId === groupId) {
        activeChatId = 'local_ai';
      }
      // Remove from groupChats store so it disappears from sidebar
      groupChats.update(map => {
        const newMap = new Map(map);
        newMap.delete(groupId);
        return newMap;
      });
      chatHistories.update(h => {
        const newMap = new Map(h);
        newMap.delete(groupId);
        return newMap;
      });
    } catch (e) {
      console.error("Failed to leave group:", e);
    }
  }

  async function handleDeleteGroup(groupId: string) {
    // Show confirmation dialog before deletion
    let shouldDelete = false;
    if (ask) {
      shouldDelete = await ask(
        "Delete this group chat? This will permanently remove all messages and data for all members.",
        { title: "Confirm Group Deletion", kind: "warning" }
      );
    } else {
      shouldDelete = confirm("Delete this group chat? This will permanently remove all messages and data for all members.");
    }

    if (!shouldDelete) {
      return;
    }

    try {
      await deleteGroup(groupId);
      if (activeChatId === groupId) {
        activeChatId = 'local_ai';
      }
      // Remove from groupChats store so it disappears from sidebar
      groupChats.update(map => {
        const newMap = new Map(map);
        newMap.delete(groupId);
        return newMap;
      });
      chatHistories.update(h => {
        const newMap = new Map(h);
        newMap.delete(groupId);
        return newMap;
      });
    } catch (e) {
      console.error("Failed to delete group:", e);
    }
  }

  // Model download dialog handlers (v0.13.5)
  async function handleModelDownload(event: CustomEvent) {
    const { provider_alias } = event.detail;
    console.log('[ModelDownload] Starting download for provider:', provider_alias);

    try {
      const result = await sendCommand('download_whisper_model', {
        provider_alias
      });

      if (result.status === 'success') {
        console.log('[ModelDownload] Download initiated successfully');
      } else {
        console.error('[ModelDownload] Download failed:', result.error);
        modelDownloadToastMessage = `❌ Download failed: ${result.error}`;
        modelDownloadToastType = 'error';
        showModelDownloadToast = true;
        isDownloadingModel = false;
      }
    } catch (error) {
      console.error('[ModelDownload] Error initiating download:', error);
      modelDownloadToastMessage = `❌ Error: ${error}`;
      modelDownloadToastType = 'error';
      showModelDownloadToast = true;
      isDownloadingModel = false;
    }
  }

  function handleModelDownloadCancel() {
    console.log('[ModelDownload] User cancelled download');
    showModelDownloadDialog = false;
    whisperModelDownloadRequired.set(null);
  }

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

  async function handleAddAIChat() {
    if (!$availableProviders || !$availableProviders.providers || $availableProviders.providers.length === 0) {
      alert("No AI providers available. Please configure providers in ~/.dpc/providers.toml");
      return;
    }

    // Load agent profiles from backend (v0.19.0+)
    try {
      const profilesResult = await listAgentProfiles();
      if (profilesResult?.status === 'success' && profilesResult.profiles) {
        availableAgentProfiles = profilesResult.profiles;
      }
    } catch (e) {
      console.warn('Failed to load agent profiles:', e);
      availableAgentProfiles = ["default"];
    }

    // Set default selections and show dialog
    selectedProviderForNewChat = $availableProviders.default_provider;
    selectedInstructionSetForNewChat = availableInstructionSets?.default || "general";
    selectedProfileForNewAgent = "default";
    showAddAIChatDialog = true;
  }

  async function confirmAddAIChat() {
    if (!selectedProviderForNewChat) return;

    // Find the selected provider
    const provider = $availableProviders.providers.find((p: any) => p.alias === selectedProviderForNewChat);
    if (!provider) {
      alert(`Provider '${selectedProviderForNewChat}' not found.`);
      return;
    }

    // Create new AI chat ID
    const chatId = `ai_chat_${crypto.randomUUID().slice(0, 8)}`;

    // Determine chat name
    let chatName: string;
    if (selectedProviderForNewChat === 'dpc_agent') {
      // Use agent name if provided, otherwise use default
      chatName = newAgentName.trim() || `Agent (${selectedProfileForNewAgent})`;
    } else {
      chatName = `${provider.alias} (${provider.model})`;
    }

    // If creating a DPC Agent, also create backend agent storage (v0.19.0+)
    if (selectedProviderForNewChat === 'dpc_agent') {
      try {
        const result = await createAgent(
          chatName,
          selectedAgentLLMProvider || $availableProviders?.default_provider || 'dpc_agent',
          selectedProfileForNewAgent,
          'general'  // Default instruction set for agents
        );
        if (result?.status === 'success') {
          console.log('[DPC Agent] Created agent storage:', result.agent_id);
          // Store agent_id in chat metadata for later reference
          agentChatToAgentId.set(chatId, result.agent_id);

          // Show success toast
          agentToastMessage = `Agent "${chatName}" created successfully`;
          agentToastType = 'info';
          showAgentToast = true;
          setTimeout(() => { showAgentToast = false; }, 3000);
        } else {
          console.warn('[DPC Agent] Failed to create agent storage:', result?.message);
          agentToastMessage = `Warning: Agent chat created but storage failed: ${result?.message}`;
          agentToastType = 'warning';
          showAgentToast = true;
          setTimeout(() => { showAgentToast = false; }, 5000);
        }
      } catch (e) {
        console.warn('[DPC Agent] Error creating agent storage:', e);
        // Continue anyway - non-blocking
      }
    }

    // Add to aiChats
    aiChats.update(chats => {
      const newMap = new Map(chats);
      newMap.set(chatId, {
        name: chatName,
        provider: selectedProviderForNewChat,
        instruction_set_name: selectedProviderForNewChat === 'dpc_agent' ? 'general' : selectedInstructionSetForNewChat,
        profile_name: selectedProviderForNewChat === 'dpc_agent' ? selectedProfileForNewAgent : undefined,
        llm_provider: selectedProviderForNewChat === 'dpc_agent' ? selectedAgentLLMProvider : undefined
      });
      return newMap;
    });

    // Set provider for this chat
    chatProviders.update(map => {
      const newMap = new Map(map);
      newMap.set(chatId, selectedProviderForNewChat);
      return newMap;
    });

    // Initialize chat history
    chatHistories.update(h => {
      const newMap = new Map(h);
      newMap.set(chatId, []);
      return newMap;
    });

    // Switch to the new chat
    activeChatId = chatId;

    // Close dialog
    showAddAIChatDialog = false;
  }

  function cancelAddAIChat() {
    showAddAIChatDialog = false;
    selectedProviderForNewChat = "";
    selectedProfileForNewAgent = "default";
    newAgentName = "";
    selectedAgentLLMProvider = "";
  }

  async function handleAddAgentChat() {
    // Check if dpc_agent provider exists
    const agentProvider = $availableProviders?.providers?.find((p: any) => p.alias === 'dpc_agent');
    if (!agentProvider) {
      alert("DPC Agent provider not configured. Add 'dpc_agent' to ~/.dpc/providers.json");
      return;
    }

    // Load agent profiles from backend (v0.19.0+)
    try {
      const profilesResult = await listAgentProfiles();
      if (profilesResult?.status === 'success' && profilesResult.profiles) {
        availableAgentProfiles = profilesResult.profiles;
      }
    } catch (e) {
      console.warn('Failed to load agent profiles:', e);
      availableAgentProfiles = ["default"];
    }

    // Show dialog with dpc_agent pre-selected
    selectedProviderForNewChat = 'dpc_agent';
    selectedInstructionSetForNewChat = availableInstructionSets?.default || "general";
    selectedProfileForNewAgent = "default";
    showAddAIChatDialog = true;
  }

  async function handleDeleteAIChat(chatId: string) {
    console.log('Delete AI chat button clicked for:', chatId);

    if (chatId === 'local_ai') {
      if (ask) {
        await ask("Cannot delete the default Local AI chat.", { title: "D-PC Messenger", kind: "info" });
      } else {
        alert("Cannot delete the default Local AI chat.");
      }
      return;
    }

    // Use Tauri's ask dialog (works on all platforms including macOS)
    // Show Telegram-specific message for Telegram chats
    let shouldDelete = false;
    if (ask) {
      if (chatId.startsWith('telegram-')) {
        shouldDelete = await ask(
          "Delete this Telegram chat? This will remove the chat history and unlink the Telegram conversation. You can still receive new messages from this contact.",
          { title: "Confirm Telegram Chat Deletion", kind: "warning" }
        );
      } else {
        shouldDelete = await ask(
          "Delete this AI chat? This will permanently remove the chat history.",
          { title: "Confirm Deletion", kind: "warning" }
        );
      }
    } else {
      if (chatId.startsWith('telegram-')) {
        shouldDelete = confirm("Delete this Telegram chat? This will remove the chat history and unlink the Telegram conversation. You can still receive new messages from this contact.");
      } else {
        shouldDelete = confirm("Delete this AI chat? This will permanently remove the chat history.");
      }
    }
    console.log('User confirmed deletion:', shouldDelete);

    if (!shouldDelete) {
      return;
    }

    // If this is a Telegram chat, tell backend to remove the conversation link
    // This prevents the chat from reappearing on restart
    if (chatId.startsWith('telegram-')) {
      try {
        const result = await sendCommand('delete_telegram_conversation_link', {
          conversation_id: chatId
        });
        if (result.status === 'error') {
          console.error('Failed to delete Telegram conversation link:', result.message);
        } else {
          console.log('Telegram conversation link deleted from backend');
        }
      } catch (error) {
        console.error('Error deleting Telegram conversation link:', error);
      }
    }

    // Remove from aiChats
    aiChats.update(chats => {
      const newMap = new Map(chats);
      newMap.delete(chatId);
      return newMap;
    });

    // Remove from chatProviders
    chatProviders.update(map => {
      const newMap = new Map(map);
      newMap.delete(chatId);
      return newMap;
    });

    // Remove chat history
    chatHistories.update(h => {
      const newMap = new Map(h);
      newMap.delete(chatId);
      return newMap;
    });

    // For Telegram chats, also clean up Telegram-specific storage
    if (chatId.startsWith('telegram-')) {
      // Update dpc-telegram-chats localStorage
      try {
        const savedTelegramChats = localStorage.getItem('dpc-telegram-chats');
        if (savedTelegramChats) {
          const telegramChats = JSON.parse(savedTelegramChats);
          delete telegramChats[chatId];
          localStorage.setItem('dpc-telegram-chats', JSON.stringify(telegramChats));
          console.log('[Telegram] Removed chat from dpc-telegram-chats localStorage');
        }
      } catch (error) {
        console.error('[Telegram] Failed to update dpc-telegram-chats:', error);
      }

      // Update telegramLinkedChats store
      telegramLinkedChats.update(links => {
        const newMap = new Map(links);
        newMap.delete(chatId);
        return newMap;
      });

      // Update telegramMessages store (clear any cached messages)
      telegramMessages.update(msgs => {
        const newMap = new Map(msgs);
        newMap.delete(chatId);
        return newMap;
      });
    }

    // Switch to default chat
    if (activeChatId === chatId) {
      activeChatId = 'local_ai';
    }

    console.log('AI chat deleted successfully');
  }

  // Agent handlers (Phase 4)
  function handleSelectAgent(agentId: string) {
    console.log('Selected agent:', agentId);

    // Find the agent in the agents list
    const agent = $agentsList.find(a => a.agent_id === agentId);
    if (!agent) {
      console.error('Agent not found:', agentId);
      return;
    }

    // Check if there's already a chat associated with this agent
    // by looking through agentChatToAgentId map
    let existingChatId: string | null = null;
    for (const [chatId, mappedAgentId] of agentChatToAgentId) {
      if (mappedAgentId === agentId) {
        existingChatId = chatId;
        break;
      }
    }

    if (existingChatId && $aiChats.has(existingChatId)) {
      // Switch to existing chat for this agent
      activeChatId = existingChatId;
      resetUnreadCount(existingChatId);
      console.log('Switched to existing agent chat:', existingChatId);
      return;
    }

    // Also check if there's a chat with the agent ID as key (legacy format)
    if ($aiChats.has(agentId)) {
      activeChatId = agentId;
      resetUnreadCount(agentId);
      console.log('Switched to existing agent chat (legacy):', agentId);
      return;
    }

    // Create a new chat for this agent using agentId directly as chatId
    aiChats.update(chats => {
      const newMap = new Map(chats);
      newMap.set(agentId, {
        name: agent.name,
        provider: 'dpc_agent',
        profile_name: agent.profile_name,
        llm_provider: agent.provider_alias,
      });
      return newMap;
    });

    // Set the chat provider
    chatProviders.update(map => {
      const newMap = new Map(map);
      newMap.set(agentId, 'dpc_agent');
      return newMap;
    });

    // Store the mapping
    agentChatToAgentId.set(agentId, agentId);

    // Switch to the new chat
    activeChatId = agentId;
    console.log('Created new agent chat:', agentId);
  }

  async function handleDeleteAgent(agentId: string) {
    console.log('Delete agent:', agentId);

    let shouldDelete = false;
    if (ask) {
      shouldDelete = await ask(
        "Delete this agent? This will permanently remove the agent's memory, knowledge, and all associated data.",
        { title: "Confirm Agent Deletion", kind: "warning" }
      );
    } else {
      shouldDelete = confirm("Delete this agent? This will permanently remove the agent's memory, knowledge, and all associated data.");
    }

    if (!shouldDelete) {
      return;
    }

    try {
      // Delete from backend
      const result = await deleteAgent(agentId);
      if (result.status === 'error') {
        console.error('Failed to delete agent:', result.message);
        agentToastMessage = `Failed to delete agent: ${result.message}`;
        agentToastType = 'error';
        showAgentToast = true;
        setTimeout(() => { showAgentToast = false; }, 5000);
        return;
      }

      // Remove agent chat if exists
      const chatId = `agent_${agentId}`;
      if ($aiChats.has(chatId)) {
        aiChats.update(chats => {
          const newMap = new Map(chats);
          newMap.delete(chatId);
          return newMap;
        });

        chatProviders.update(map => {
          const newMap = new Map(map);
          newMap.delete(chatId);
          return newMap;
        });

        chatHistories.update(h => {
          const newMap = new Map(h);
          newMap.delete(chatId);
          return newMap;
        });

        if (activeChatId === chatId) {
          activeChatId = 'local_ai';
        }
      }

      // Show success toast
      agentToastMessage = 'Agent deleted successfully';
      agentToastType = 'info';
      showAgentToast = true;
      setTimeout(() => { showAgentToast = false; }, 3000);

      console.log('Agent deleted successfully');
    } catch (error) {
      console.error('Error deleting agent:', error);
      agentToastMessage = `Error deleting agent: ${error}`;
      agentToastType = 'error';
      showAgentToast = true;
      setTimeout(() => { showAgentToast = false; }, 5000);
    }
  }

  async function handleLinkAgentTelegram(agentId: string, config: {
    bot_token: string;
    chat_ids: string[];
    event_filter?: string[];
    max_events_per_minute?: number;
    cooldown_seconds?: number;
    transcription_enabled?: boolean;
    unified_conversation?: boolean;
  }) {
    console.log('Link agent to Telegram:', agentId, 'config:', { ...config, bot_token: '***' });

    try {
      const result = await sendCommand('link_agent_telegram', {
        agent_id: agentId,
        bot_token: config.bot_token,
        chat_ids: config.chat_ids,
        event_filter: config.event_filter,
        max_events_per_minute: config.max_events_per_minute || 20,
        cooldown_seconds: config.cooldown_seconds || 3.0,
        transcription_enabled: config.transcription_enabled !== false,
        unified_conversation: config.unified_conversation === true,
      });

      if (result.status === 'error') {
        console.error('Failed to link agent to Telegram:', result.message);
        agentToastMessage = `Failed to link agent: ${result.message}`;
        agentToastType = 'error';
        showAgentToast = true;
        setTimeout(() => { showAgentToast = false; }, 5000);
        throw new Error(result.message);
      }

      // Show success toast
      agentToastMessage = 'Agent Telegram configuration updated successfully';
      agentToastType = 'info';
      showAgentToast = true;
      setTimeout(() => { showAgentToast = false; }, 3000);

      // Refresh agent list to update Telegram status
      try {
        const agentsResult = await listAgents();
        if (agentsResult?.status === 'success' && agentsResult.agents) {
          agentsList.set(agentsResult.agents);
        }
      } catch (error) {
        console.error('Failed to refresh agents list:', error);
      }

      console.log('Agent Telegram configuration updated successfully');
    } catch (error) {
      console.error('Error linking agent to Telegram:', error);
      agentToastMessage = `Error linking agent: ${error}`;
      agentToastType = 'error';
      showAgentToast = true;
      setTimeout(() => { showAgentToast = false; }, 5000);
      throw error;
    }
  }

  async function handleUnlinkAgentTelegram(agentId: string) {
    console.log('Unlink agent from Telegram:', agentId);

    try {
      const result = await sendCommand('unlink_agent_telegram', { agent_id: agentId });

      if (result.status === 'error') {
        console.error('Failed to unlink agent from Telegram:', result.message);
        agentToastMessage = `Failed to unlink agent: ${result.message}`;
        agentToastType = 'error';
        showAgentToast = true;
        setTimeout(() => { showAgentToast = false; }, 5000);
        return;
      }

      // Show success toast
      agentToastMessage = 'Agent unlinked from Telegram successfully';
      agentToastType = 'info';
      showAgentToast = true;
      setTimeout(() => { showAgentToast = false; }, 3000);

      // Refresh agent list to update Telegram status
      try {
        const agentsResult = await listAgents();
        if (agentsResult?.status === 'success' && agentsResult.agents) {
          agentsList.set(agentsResult.agents);
        }
      } catch (error) {
        console.error('Failed to refresh agents list:', error);
      }

      console.log('Agent unlinked from Telegram successfully');
    } catch (error) {
      console.error('Error unlinking agent from Telegram:', error);
      agentToastMessage = `Error unlinking agent: ${error}`;
      agentToastType = 'error';
      showAgentToast = true;
      setTimeout(() => { showAgentToast = false; }, 5000);
    }
  }

  // File transfer handlers (Week 1)
  async function handleSendFile() {
    // Only allow file transfer to P2P chats and Telegram chats (not local_ai or ai_xxx chats)
    if (activeChatId === 'local_ai' || activeChatId.startsWith('ai_')) {
      if (ask) {
        await ask("File transfer is only available in P2P and Telegram chats.", { title: "D-PC Messenger", kind: "info" });
      } else {
        alert("File transfer is only available in P2P and Telegram chats.");
      }
      return;
    }

    // Check if running in Tauri
    if (!open) {
      alert("File transfer requires the Tauri desktop app. Please use the desktop version.");
      return;
    }

    try {
      const selected = await open({
        multiple: false,
        directory: false
      });

      if (!selected) {
        console.log('File selection cancelled');
        return;
      }

      const filePath = selected as string;
      console.log(`Selected file: ${filePath}`);

      // Get file name from path
      const fileName = filePath.split(/[\\/]/).pop() || filePath;

      // Check if this is a Telegram chat
      if (activeChatId.startsWith('telegram-')) {
        // Send directly to Telegram (no confirmation dialog needed)
        console.log(`[Telegram] Sending file to Telegram: ${fileName}`);
        await sendToTelegram(activeChatId, '', [], undefined, undefined, undefined, filePath);

        // Add to local chat history (with minimal attachment data for UI display)
        chatHistories.update(h => {
          const newMap = new Map(h);
          const hist = newMap.get(activeChatId) || [];
          newMap.set(activeChatId, [...hist, {
            id: crypto.randomUUID(),
            sender: 'user',
            text: '',
            timestamp: Date.now(),
            attachments: [{
              type: 'file',
              filename: fileName,
              file_path: filePath,
              size_bytes: 0  // Placeholder - actual size handled by backend
            }]
          }]);
          return newMap;
        });
      } else if (activeChatId.startsWith('group-')) {
        // Group chat: Send file via group fan-out (v0.19.0)
        // Use custom Svelte dialog instead of native confirm() (fixes Tauri auto-accept bug)
        const group = $groupChats.get(activeChatId);
        const groupName = group?.name || 'group';
        pendingFileSend = {
          filePath,
          fileName,
          recipientId: activeChatId,
          recipientName: `group "${groupName}"`
        };
        showSendFileDialog = true;
      } else {
        // P2P file transfer with confirmation dialog (existing behavior)
        // Get recipient name from peer info
        const peer = $nodeStatus.peer_info.find((p: any) => p.node_id === activeChatId);
        const recipientName = peer?.name || activeChatId.slice(0, 20) + '...';

        // Store pending file send and show confirmation dialog
        pendingFileSend = {
          filePath,
          fileName,
          recipientId: activeChatId,
          recipientName
        };
        showSendFileDialog = true;
      }
    } catch (error) {
      console.error('Error sending file:', error);
      fileOfferToastMessage = `Failed to send file: ${error}`;
      showFileOfferToast = true;
      setTimeout(() => showFileOfferToast = false, 5000);
    }
  }

  async function handleAcceptFile() {
    if (!currentFileOffer) return;

    try {
      const filename = currentFileOffer.filename;
      await acceptFileTransfer(currentFileOffer.transfer_id);
      showFileOfferDialog = false;
      currentFileOffer = null;
      console.log(`Accepted file transfer: ${filename}`);
    } catch (error) {
      console.error('Error accepting file:', error);
      fileOfferToastMessage = `Failed to accept file: ${error}`;
      showFileOfferToast = true;
      setTimeout(() => showFileOfferToast = false, 5000);
    }
  }

  async function handleRejectFile() {
    if (!currentFileOffer) return;

    try {
      const filename = currentFileOffer.filename;
      await cancelFileTransfer(currentFileOffer.transfer_id, "user_rejected");
      showFileOfferDialog = false;
      currentFileOffer = null;
      console.log(`Rejected file transfer: ${filename}`);
    } catch (error) {
      console.error('Error rejecting file:', error);
    }
  }

  // Image paste handlers (Phase 2.4: Screenshot + Vision - improved UX)
  async function handlePaste(event: ClipboardEvent) {
    console.log('[Paste] Paste event triggered');

    // First try the standard ClipboardEvent API
    const items = event.clipboardData?.items;
    if (items && items.length > 0) {
      console.log(`[Paste] Found ${items.length} clipboard items via standard API`);
      for (let i = 0; i < items.length; i++) {
        const item = items[i];
        console.log(`[Paste] Item ${i}: type="${item.type}", kind="${item.kind}"`);

        if (item.type.startsWith('image/')) {
          console.log('[Paste] Image item detected, processing...');
          event.preventDefault();
          const blob = item.getAsFile();

          // Check if blob is valid
          if (!blob) {
            console.log('[Paste] Failed to get file blob from clipboard item');
            continue;
          }

          console.log(`[Paste] Got blob: size=${blob.size} bytes, type=${blob.type}`);

          // Validate size (5MB limit)
          if (blob.size > 5 * 1024 * 1024) {
            fileOfferToastMessage = `Image too large (${(blob.size / (1024 * 1024)).toFixed(2)}MB). Max: 5MB`;
            showFileOfferToast = true;
            setTimeout(() => showFileOfferToast = false, 5000);
            return;
          }

          // Convert to data URL and show as preview chip
          const reader = new FileReader();
          reader.onload = (e) => {
            console.log('[Paste] Image converted to data URL, setting pendingImage');
            pendingImage = {
              dataUrl: e.target?.result as string,
              filename: `screenshot_${Date.now()}.png`,
              sizeBytes: blob.size
            };
          };
          reader.onerror = (e) => {
            console.error('[Paste] FileReader error:', e);
          };
          reader.readAsDataURL(blob);
          return;
        }
      }
    }

    // Fallback: Try navigator.clipboard.read() for Linux compatibility
    try {
      console.log('[Paste] Standard API failed, trying navigator.clipboard.read()...');
      const clipboardItems = await navigator.clipboard.read();

      for (const clipboardItem of clipboardItems) {
        console.log('[Paste] ClipboardItem types:', clipboardItem.types);

        for (const type of clipboardItem.types) {
          if (type.startsWith('image/')) {
            console.log(`[Paste] Found image type: ${type}`);
            event.preventDefault();
            const blob = await clipboardItem.getType(type);

            console.log(`[Paste] Got blob from navigator.clipboard: size=${blob.size} bytes, type=${blob.type}`);

            // Validate size (5MB limit)
            if (blob.size > 5 * 1024 * 1024) {
              fileOfferToastMessage = `Image too large (${(blob.size / (1024 * 1024)).toFixed(2)}MB). Max: 5MB`;
              showFileOfferToast = true;
              setTimeout(() => showFileOfferToast = false, 5000);
              return;
            }

            // Convert to data URL and show as preview chip
            const reader = new FileReader();
            reader.onload = (e) => {
              console.log('[Paste] Image converted to data URL (fallback), setting pendingImage');
              pendingImage = {
                dataUrl: e.target?.result as string,
                filename: `screenshot_${Date.now()}.png`,
                sizeBytes: blob.size
              };
            };
            reader.onerror = (e) => {
              console.error('[Paste] FileReader error (fallback):', e);
            };
            reader.readAsDataURL(blob);
            return;
          }
        }
      }

      console.log('[Paste] No image found in clipboard via fallback API');
    } catch (err) {
      console.error('[Paste] navigator.clipboard.read() failed:', err);
      // Might fail due to permissions or not being supported
    }

    console.log('[Paste] Paste handling complete');
  }

  function clearPendingImage() {
    pendingImage = null;
  }

  async function handleConfirmSendFile() {
    if (!pendingFileSend || isSendingFile) return;  // Guard against double-click

    isSendingFile = true;  // Set flag immediately to block subsequent clicks

    try {
      // Check if this is image data (screenshot) or file path
      if (pendingFileSend.imageData) {
        // Screenshot/image paste
        console.log(`Sending screenshot: ${pendingFileSend.fileName} to ${pendingFileSend.recipientId}`);
        if (pendingFileSend.recipientId.startsWith('group-')) {
          await sendGroupImage(pendingFileSend.recipientId, pendingFileSend.imageData.dataUrl, pendingFileSend.imageData.filename, pendingFileSend.caption || '');
          console.log(`[Group] Screenshot transfer initiated: ${pendingFileSend.fileName}`);
        } else {
          // P2P screenshot
          await sendCommand("send_p2p_image", {
            node_id: pendingFileSend.recipientId,
            image_base64: pendingFileSend.imageData.dataUrl,
            filename: pendingFileSend.imageData.filename,
            text: pendingFileSend.caption || ''
          });
          console.log(`Screenshot transfer initiated: ${pendingFileSend.fileName}`);
        }
      } else {
        // Regular file
        console.log(`Sending file: ${pendingFileSend.filePath} to ${pendingFileSend.recipientId}`);
        if (pendingFileSend.recipientId.startsWith('group-')) {
          await sendGroupFile(pendingFileSend.recipientId, pendingFileSend.filePath);
          console.log(`[Group] File transfer initiated: ${pendingFileSend.fileName}`);
        } else {
          await sendFile(pendingFileSend.recipientId, pendingFileSend.filePath);
        }
      }

      showSendFileDialog = false;
      pendingFileSend = null;

      fileOfferToastMessage = `Sending...`;
      showFileOfferToast = true;
      setTimeout(() => showFileOfferToast = false, 3000);
    } catch (error) {
      console.error('Error sending file:', error);
      fileOfferToastMessage = `Failed to send: ${error}`;
      showFileOfferToast = true;
      setTimeout(() => showFileOfferToast = false, 5000);
    } finally {
      isSendingFile = false;  // Reset flag after completion
      // Clear file preparation state
      filePreparationStarted.set(null);
      filePreparationProgress.set(null);
      filePreparationCompleted.set(null);
    }
  }

  function handleCancelSendFile() {
    showSendFileDialog = false;
    pendingFileSend = null;
    // Clear file preparation state
    filePreparationStarted.set(null);
    filePreparationProgress.set(null);
    filePreparationCompleted.set(null);
    console.log('File send cancelled by user');
  }

  async function handleCancelTransfer(transferId: string, filename: string) {
    try {
      console.log(`Cancelling file transfer: ${transferId}`);
      await cancelFileTransfer(transferId, 'user_cancelled');

      // Show toast notification
      fileOfferToastMessage = `Cancelled: ${filename}`;
      showFileOfferToast = true;
      setTimeout(() => showFileOfferToast = false, 3000);
    } catch (error) {
      console.error('Error cancelling file transfer:', error);
      fileOfferToastMessage = `Failed to cancel: ${error}`;
      showFileOfferToast = true;
      setTimeout(() => showFileOfferToast = false, 5000);
    }
  }

  // Voice message handlers (v0.13.0 - Voice Messages)
  async function handleRecordingComplete(blob: Blob, duration: number) {
    voicePreview = { blob, duration };
  }

  async function handleSendVoiceMessage() {
    if (!voicePreview) return;

    try {
      // Store blob and duration locally to avoid null check issues after await
      const blob = voicePreview.blob;
      const duration = voicePreview.duration;

      // Check if this is a Telegram chat
      if (activeChatId.startsWith('telegram-')) {
        // Convert blob to base64
        const arrayBuffer = await blob.arrayBuffer();
        const uint8Array = new Uint8Array(arrayBuffer);

        // Convert to base64 in chunks to avoid "Maximum call stack size exceeded"
        let binaryString = '';
        const chunkSize = 8192;
        for (let i = 0; i < uint8Array.length; i += chunkSize) {
          const chunk = uint8Array.subarray(i, i + chunkSize);
          binaryString += String.fromCharCode(...chunk);
        }
        const base64Audio = btoa(binaryString);

        // Send to Telegram
        await sendToTelegram(
          activeChatId,
          '', // Empty text for voice-only message
          [],
          base64Audio,
          duration,
          blob.type || 'audio/webm'
        );

        // Add message to local history
        chatHistories.update(h => {
          const newMap = new Map(h);
          const hist = newMap.get(activeChatId) || [];
          newMap.set(activeChatId, [...hist, {
            id: crypto.randomUUID(),
            sender: 'user',
            text: 'Voice message',
            timestamp: Date.now(),
            attachments: [{
              type: 'voice',
              filename: `voice_${Date.now()}.${(blob.type || 'audio/webm').split('/')[1]}`,
              file_path: '', // Will be filled by backend response
              size_bytes: blob.size,
              mime_type: blob.type || 'audio/webm',
              voice_metadata: {
                duration_seconds: duration,
                sample_rate: 48000,
                channels: 1,
                codec: 'opus',
                recorded_at: new Date().toISOString()
              }
            }]
          }]);
          return newMap;
        });

        console.log(`[Telegram] Sent voice message to Telegram`);
      } else if (activeChatId.startsWith('group-')) {
        // Group chat: Convert blob to base64 and send via group voice fan-out (v0.19.0)
        const arrayBuffer = await blob.arrayBuffer();
        const uint8Array = new Uint8Array(arrayBuffer);
        let binaryString = '';
        const chunkSize = 8192;
        for (let i = 0; i < uint8Array.length; i += chunkSize) {
          const chunk = uint8Array.subarray(i, i + chunkSize);
          binaryString += String.fromCharCode(...chunk);
        }
        const base64Audio = btoa(binaryString);
        await sendGroupVoiceMessage(activeChatId, base64Audio, duration, blob.type || 'audio/webm');
      } else if (activeChatId !== 'local_ai' && !activeChatId.startsWith('ai_')) {
        // For P2P chats, send via file transfer
        await sendVoiceMessage(activeChatId, blob, duration);
      }
      // For AI chats, voice messages must be transcribed first (use handleTranscribeVoiceMessage instead)

      // Clear preview
      voicePreview = null;
    } catch (error) {
      console.error('Error sending voice message:', error);
      fileOfferToastMessage = `Failed to send voice: ${error}`;
      showFileOfferToast = true;
      setTimeout(() => showFileOfferToast = false, 5000);
    }
  }

  async function handleTranscribeVoiceMessage() {
    if (!voicePreview) return;

    try {
      // Convert blob to base64
      const arrayBuffer = await voicePreview.blob.arrayBuffer();
      const uint8Array = new Uint8Array(arrayBuffer);

      // Convert to base64 in chunks to avoid "Maximum call stack size exceeded"
      let binaryString = '';
      const chunkSize = 8192;
      for (let i = 0; i < uint8Array.length; i += chunkSize) {
        const chunk = uint8Array.subarray(i, i + chunkSize);
        binaryString += String.fromCharCode(...chunk);
      }
      const base64Audio = btoa(binaryString);

      // Show loading state
      fileOfferToastMessage = 'Transcribing voice message...';
      showFileOfferToast = true;

      // Get selected voice provider (v0.15.1+: Pass full provider ID for remote support)
      const selectedProviderId = selectedVoiceProvider || selectedTextProvider;

      // Call backend for transcription
      const response = await sendCommand('transcribe_audio', {
        audio_base64: base64Audio,
        mime_type: voicePreview.blob.type || 'audio/webm',
        provider_alias: selectedProviderId  // v0.15.1+: Pass full ID (supports "remote:" and "local:" prefixes)
      });

      if (response.error) {
        throw new Error(response.error);
      }

      // Insert transcribed text into textarea
      const transcription = response.text || '';
      if (transcription) {
        currentInput = currentInput + (currentInput ? ' ' : '') + transcription;
      }

      // Clear preview and hide toast
      voicePreview = null;
      showFileOfferToast = false;

      // Focus textarea
      const textarea = document.getElementById('message-input') as HTMLTextAreaElement;
      if (textarea) {
        textarea.focus();
        textarea.setSelectionRange(currentInput.length, currentInput.length);
      }
    } catch (error) {
      console.error('Error transcribing voice message:', error);
      fileOfferToastMessage = `Transcription failed: ${error}`;
      showFileOfferToast = true;
      setTimeout(() => showFileOfferToast = false, 5000);
    }
  }

  function handleCancelVoicePreview() {
    voicePreview = null;
  }

  // Auto-transcribe setting management (v0.13.2+ Auto-Transcription)
  async function saveAutoTranscribeSetting() {
    // Only save for P2P chats (not AI chats or local_ai)
    if ($aiChats.has(activeChatId) || activeChatId === 'local_ai') {
      return;
    }

    try {
      const result = await setConversationTranscription(activeChatId, autoTranscribeEnabled);
      console.log(`[AutoTranscribe] Saved setting for ${activeChatId}: ${autoTranscribeEnabled}`, result);

      // Model will load lazily on first voice message recording
      // No need to preload here - this speeds up app startup significantly
    } catch (error) {
      console.error(`[AutoTranscribe] Failed to save setting for ${activeChatId}:`, error);
    }
  }

  async function loadAutoTranscribeSetting(chatId: string) {
    // Only load for P2P chats (not AI chats or local_ai)
    if ($aiChats.has(chatId) || chatId === 'local_ai') {
      autoTranscribeEnabled = true;  // Not applicable for AI chats
      return;
    }

    try {
      const result = await getConversationTranscription(chatId);
      if (result.status === 'success') {
        autoTranscribeEnabled = result.enabled;
        console.log(`[AutoTranscribe] Loaded setting for ${chatId}: ${autoTranscribeEnabled}`);
      } else {
        // Default to true on error
        autoTranscribeEnabled = true;
        console.warn(`[AutoTranscribe] Failed to load setting for ${chatId}, defaulting to true`);
      }
    } catch (error) {
      console.error(`[AutoTranscribe] Error loading setting for ${chatId}:`, error);
      autoTranscribeEnabled = true;  // Default to true on error
    }
  }

  // Load auto-transcribe setting when chat changes
  $effect(() => {
    if (activeChatId && $connectionStatus === 'connected') {
      loadAutoTranscribeSetting(activeChatId);
    }
  });

  // --- HANDLE INCOMING MESSAGES ---
  $effect(() => {
    if ($coreMessages?.id) {
      const message = $coreMessages;
      const messageId = message.id;  // Unique ID for deduplication

    if (message.command === "execute_ai_query") {
      // Guard: Skip if already processed (prevents reactive loops in Svelte 5)
      if (processedMessageIds.has(messageId)) {
        console.log(`[execute_ai_query] Skipping already processed message: ${messageId}`);
        return;
      }
      processedMessageIds.add(messageId);

      // Flush any pending streaming buffer before capturing (handles batch-mode single-chunk delivery
      // where the chunk and final response arrive within milliseconds — before the 100ms throttle fires)
      if (streamingBuffer) {
        if (streamingFlushTimeout) {
          clearTimeout(streamingFlushTimeout);
          streamingFlushTimeout = null;
        }
        agentStreamingText += streamingBuffer;
        streamingBuffer = "";
      }
      // Capture streaming text before clearing (for collapsible raw output)
      const capturedStreamingText = agentStreamingText;
      clearAgentStreaming();  // Clear streaming text when final response arrives

      const newText = message.status === "OK"
        ? message.payload.content
        : `Error: ${message.payload?.message || 'Unknown error'}`;
      const newSender = message.status === "OK" ? 'ai' : 'system';
      const modelName = message.status === "OK" ? message.payload.model : undefined;
      // v1.4+: Extract thinking fields for reasoning models
      const thinkingContent = message.status === "OK" ? message.payload.thinking : undefined;
      const thinkingTokenCount = message.status === "OK" ? message.payload.thinking_tokens : undefined;

      // Show toast notification for errors (helps remote users see host failures)
      if (message.status !== "OK") {
        console.error(`[TokenCounter] AI query failed: ${message.payload?.message}`);
        fileOfferToastMessage = `⚠️ AI Query Failed: ${message.payload?.message || 'Unknown error'}`;
        showFileOfferToast = true;
        setTimeout(() => showFileOfferToast = false, 7000);  // 7s for errors (longer than success)
      }

      const responseCommandId = message.id;

      // Find which chat this command belongs to
      let chatId = commandToChatMap.get(responseCommandId);

      // Debug: Log if chatId not found (helps diagnose race conditions)
      if (!chatId) {
        console.warn(`[execute_ai_query] No chatId found for commandId=${responseCommandId}, using activeChatId=${activeChatId} as fallback`);
        // Fallback: use active chat if command mapping not found
        // This handles edge cases where the map was cleared or response arrived late
        chatId = activeChatId;
      }

      // Clear loading state for the specific chat that received the response
      if (chatId) {
        setChatLoading(chatId, false);
        console.log(`[TokenCounter] Loading cleared for chatId=${chatId}`);
      }

      if (chatId) {
        // Debug: Log what we're searching for
        console.log(`[execute_ai_query] Looking for commandId=${responseCommandId} in chatId=${chatId}`);

        chatHistories.update(h => {
          const newMap = new Map(h);
          const hist = newMap.get(chatId) || [];

          // Debug: Log all commandIds in history
          const commandIds = hist.filter(m => m.commandId).map(m => m.commandId);
          console.log(`[execute_ai_query] History commandIds:`, commandIds);

          // Check if we found a match
          const found = hist.some(m => m.commandId === responseCommandId);
          console.log(`[execute_ai_query] Found matching message: ${found}`);

          newMap.set(chatId, hist.map(m =>
            m.commandId === responseCommandId ? {
              ...m,
              sender: newSender,
              text: newText,
              model: modelName,
              thinking: thinkingContent,  // v1.4+: Thinking/reasoning content
              thinkingTokens: thinkingTokenCount,  // v1.4+: Thinking token count
              streamingRaw: capturedStreamingText || undefined,  // v0.16.0+: Raw streaming text
              commandId: undefined
            } : m
          ));
          return newMap;
        });

        // Update token usage map with data from response (Phase 2)
        if (message.status === "OK" && message.payload.tokens_used && message.payload.token_limit) {
          tokenUsageMap = new Map(tokenUsageMap);
          tokenUsageMap.set(chatId, {
            used: message.payload.tokens_used,
            limit: message.payload.token_limit
          });
        }

        // Phase 7: Mark context as sent (clears "Updated" status)
        if (message.status === "OK") {
          // Update local context hash if it exists
          if (currentContextHash) {
            lastSentContextHash = new Map(lastSentContextHash);
            lastSentContextHash.set(chatId, currentContextHash);
            console.log(`[Context Sent] Marked context as sent for ${chatId}`);
          }

          // Also mark peer contexts as sent if they were included (independent of local context)
          if (selectedPeerContexts.size > 0) {
            const chatPeerHashes = new Map();
            for (const peerId of selectedPeerContexts) {
              const peerHash = peerContextHashes.get(peerId);
              if (peerHash) {
                chatPeerHashes.set(peerId, peerHash);
              }
            }
            lastSentPeerHashes = new Map(lastSentPeerHashes);
            lastSentPeerHashes.set(chatId, chatPeerHashes);
            console.log(`[Peer Contexts Sent] Marked ${chatPeerHashes.size} peer contexts as sent for ${chatId}`);
          }
        }

        // Clean up the command mapping
        commandToChatMap.delete(responseCommandId);
      }

      // Cleanup old processed IDs to prevent memory leak
      if (processedMessageIds.size > 100) {
        const firstId = processedMessageIds.values().next().value;
        if (firstId) {
          processedMessageIds.delete(firstId);
        }
      }

      autoScroll();
    }
    }
  });

  // Handle AI vision responses (Phase 2)
  $effect(() => {
    if ($aiResponseWithImage) {
      const response = $aiResponseWithImage;
      setChatLoading(response.conversation_id, false);

      chatHistories.update(h => {
        const newMap = new Map(h);
        const hist = newMap.get(response.conversation_id) || [];

        // Add assistant response (just text, image is already in user's message)
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

      // Clear the store after processing
      aiResponseWithImage.set(null);
    }
  });

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

        // Include attachments if present (file transfers)
        if (msg.attachments && msg.attachments.length > 0) {
          messageData.attachments = msg.attachments;
        }

        newMap.set(chatId, [...hist, messageData]);
        return newMap;
      });

      if (wasNearBottom || activeChatId === msg.sender_node_id || msg.sender_node_id === "user") {
        autoScroll();
      }

      // Send notification if app is in background (showNotificationIfBackground handles the check)
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

      if (processedMessageIds.size > 100) {
        const firstId = processedMessageIds.values().next().value;
        if (firstId) {
          processedMessageIds.delete(firstId);
        }
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

        if (processedMessageIds.size > 100) {
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

        if (processedMessageIds.size > 100) {
          const firstId = processedMessageIds.values().next().value;
          if (firstId) processedMessageIds.delete(firstId);
        }
      }
    }
  });

  // Group invite accept/decline dialog (v0.19.0)
  $effect(() => {
    if ($groupInviteReceived) {
      pendingGroupInvite = $groupInviteReceived;
      showGroupInviteDialog = true;
    }
  });

  function handleGroupInviteAccept(event: CustomEvent<{ group_id: string }>) {
    const groupId = event.detail.group_id;
    // Group is already stored by backend — just init chat history and show in sidebar
    chatHistories.update(h => {
      if (!h.has(groupId)) {
        const newMap = new Map(h);
        newMap.set(groupId, []);
        return newMap;
      }
      return h;
    });
    const name = pendingGroupInvite?.name || 'group';
    fileOfferToastMessage = `Joined group "${name}"`;
    showFileOfferToast = true;
    setTimeout(() => showFileOfferToast = false, 3000);
    activeChatId = groupId;
    pendingGroupInvite = null;
  }

  async function handleGroupInviteDecline(event: CustomEvent<{ group_id: string }>) {
    const groupId = event.detail.group_id;
    // Leave the group (backend already stored it, so we need to remove)
    await leaveGroup(groupId);
    pendingGroupInvite = null;
  }

  // Group settings: add/remove member (v0.19.0)
  async function handleGroupAddMember(event: CustomEvent<{ group_id: string; node_id: string }>) {
    const { group_id, node_id } = event.detail;
    try {
      await addGroupMember(group_id, node_id);
    } catch (e) {
      console.error("Failed to add group member:", e);
    }
  }

  async function handleGroupRemoveMember(event: CustomEvent<{ group_id: string; node_id: string }>) {
    const { group_id, node_id } = event.detail;
    try {
      await removeGroupMember(group_id, node_id);
    } catch (e) {
      console.error("Failed to remove group member:", e);
    }
  }

  // Group deletion notification (v0.19.0)
  $effect(() => {
    if ($groupDeleted) {
      const deleted = $groupDeleted;
      fileOfferToastMessage = `Group "${deleted.group_name || deleted.group_id}" was deleted`;
      showFileOfferToast = true;
      setTimeout(() => showFileOfferToast = false, 5000);

      // Switch away if viewing deleted group
      if (activeChatId === deleted.group_id) {
        activeChatId = 'local_ai';
      }
      // Clean up chat history
      chatHistories.update(h => {
        const newMap = new Map(h);
        newMap.delete(deleted.group_id);
        return newMap;
      });
    }
  });

  // Group member left notification (v0.19.0)
  $effect(() => {
    if ($groupMemberLeft) {
      const left = $groupMemberLeft;
      const memberName = left.member_name || left.node_id?.slice(0, 16) || 'A member';
      const group = $groupChats.get(left.group_id);
      if (group) {
        fileOfferToastMessage = `${memberName} left "${group.name}"`;
        showFileOfferToast = true;
        setTimeout(() => showFileOfferToast = false, 4000);
      }
    }
  });

  let activeMessages = $derived($chatHistories.get(activeChatId) || []);

  // Auto-scroll when activeMessages change (for all chat types)
  $effect(() => {
    // Track activeMessages length to detect new messages
    activeMessages.length;
    // Scroll to bottom when messages are added for the active chat
    autoScroll();
  });
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
                onchange={saveAutoTranscribeSetting}
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
            bind:enableMarkdown
            onNewSession={handleNewChat}
            onEndSession={handleEndSession}
          />
        {/if}
      </div>

      <ChatPanel
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

      <div class="chat-input">
        {#if $aiChats.has(activeChatId) && !activeChatId.startsWith('telegram-')}
          <!-- Personal Context Toggle (hidden for Telegram chats - not applicable) -->
          <div class="context-toggle">
            <button
              type="button"
              class="context-toggle-header"
              onclick={() => contextPanelCollapsed = !contextPanelCollapsed}
              aria-expanded={!contextPanelCollapsed}
            >
              <span class="context-toggle-title">
                {contextPanelCollapsed ? '▶' : '▼'} Context Settings
              </span>
            </button>

            {#if !contextPanelCollapsed}
            <label class="context-checkbox">
              <input
                id="include-personal-context"
                name="include-personal-context"
                type="checkbox"
                bind:checked={includePersonalContext}
              />
              <span>
                Include Personal Context (profile, instructions, device info)
                {#if localContextUpdated}
                  <span class="status-badge updated">Updated</span>
                {/if}
              </span>
            </label>
            {#if !includePersonalContext}
              <span class="context-hint">⚠️ AI won't know your preferences or device specs</span>
            {/if}

            <!-- AI Scope Selector (only show when context is enabled) -->
            {#if includePersonalContext && availableAIScopes.length > 0}
              <div class="ai-scope-selector">
                <label for="ai-scope-select">
                  AI Context Mode:
                </label>
                <select id="ai-scope-select" bind:value={selectedAIScope}>
                  <option value="">Full Access (no filtering)</option>
                  {#each availableAIScopes as scopeName}
                    <option value={scopeName}>{scopeName}</option>
                  {/each}
                </select>
                <span class="context-hint">
                  {#if selectedAIScope}
                    🔒 AI can only access: {selectedAIScope} scope
                  {:else}
                    🔓 AI has full context access
                  {/if}
                </span>
              </div>
            {/if}

            <!-- Instruction Set Selector (only show when context is enabled) -->
            {#if includePersonalContext && (activeChatId === 'local_ai' || activeChatId.startsWith('ai_'))}
              <div class="ai-scope-selector">
                <label for="instruction-set-select">
                  AI Instruction Set:
                </label>
                <select
                  id="instruction-set-select"
                  value={selectedInstructionSet}
                  onchange={(e) => updateInstructionSet((e.target as HTMLSelectElement).value)}
                >
                  <option value="none">None (No Instructions)</option>
                  {#if availableInstructionSets}
                    {#each Object.entries(availableInstructionSets.sets) as [key, set]}
                      <option value={key}>
                        {set.name} {availableInstructionSets.default === key ? '⭐' : ''}
                      </option>
                    {/each}
                  {:else}
                    <option value="general">General Purpose</option>
                  {/if}
                </select>
                <span class="context-hint">
                  Controls AI behavior and responses
                </span>
              </div>
            {/if}

            <!-- Peer Context Selector (show for all AI chats) -->
            {#if $nodeStatus?.peer_info && $nodeStatus.peer_info.length > 0}
            <div class="peer-context-selector">
              <div class="peer-context-header">
                <span class="peer-context-label">Include Peer Context:</span>
                <span class="peer-context-hint">
                  ({selectedPeerContexts.size} selected)
                </span>
              </div>
              <div class="peer-context-checkboxes">
                {#each $nodeStatus.peer_info as peer}
                  {@const displayName = peer.name
                    ? `${peer.name} | ${peer.node_id.slice(0, 15)}...`
                    : `${peer.node_id.slice(0, 20)}...`}
                  {@const isPeerContextUpdated = peerContextsUpdated.has(peer.node_id)}
                  <label class="peer-context-checkbox">
                    <input
                      id={`peer-context-${peer.node_id}`}
                      name={`peer-context-${peer.node_id}`}
                      type="checkbox"
                      checked={selectedPeerContexts.has(peer.node_id)}
                      onchange={() => togglePeerContext(peer.node_id)}
                    />
                    <span>
                      {displayName}
                      {#if isPeerContextUpdated}
                        <span class="status-badge updated">Updated</span>
                      {/if}
                    </span>
                  </label>
                {/each}
              </div>
            </div>
            {/if}
            {/if}
          </div>
        {/if}

        <!-- FileTransferUI component: Image/voice preview appears here, modals/panels are positioned -->
        <FileTransferUI
          pendingImage={pendingImage}
          onClearPendingImage={clearPendingImage}
          voicePreview={voicePreview}
          onClearVoicePreview={handleCancelVoicePreview}
          onSendVoiceMessage={handleSendVoiceMessage}
          onTranscribeVoiceMessage={handleTranscribeVoiceMessage}
          isLocalAIChat={activeChatId === 'local_ai' || activeChatId.startsWith('ai_')}
          showFileOfferDialog={showFileOfferDialog}
          currentFileOffer={currentFileOffer}
          onAcceptFile={handleAcceptFile}
          onRejectFile={handleRejectFile}
          showSendFileDialog={showSendFileDialog}
          pendingFileSend={pendingFileSend}
          isSendingFile={isSendingFile}
          filePreparationStarted={$filePreparationStarted}
          filePreparationProgress={$filePreparationProgress}
          filePreparationCompleted={$filePreparationCompleted}
          onConfirmSendFile={handleConfirmSendFile}
          onCancelSendFile={handleCancelSendFile}
          activeFileTransfers={$activeFileTransfers}
          onCancelTransfer={handleCancelTransfer}
          showFileOfferToast={showFileOfferToast}
          fileOfferToastMessage={fileOfferToastMessage}
          onDismissToast={() => showFileOfferToast = false}
        />

        <div class="input-row">
          <!-- Knowledge Integrity Warning Banner (shown on startup if tampered/corrupted commits detected) -->
          {#if $integrityWarnings && $integrityWarnings.count > 0 && !$integrityWarnings.dismissed}
            <IntegrityWarningBanner
              count={$integrityWarnings.count}
              warnings={$integrityWarnings.warnings}
              onDismiss={() => integrityWarnings.update(w => w ? { ...w, dismissed: true } : w)}
            />
          {/if}

          <!-- Token Warning Banner (90% and 100% warnings) -->
          {#if showTokenBanner}
            <TokenWarningBanner
              severity={tokenWarningLevel === 'critical' ? 'critical' : 'warning'}
              percentage={estimatedUsage.percentage}
              onEndSession={() => handleEndSession(activeChatId)}
              dismissible={tokenWarningLevel !== 'critical'}
            />
          {/if}

          <textarea
            id="message-input"
            name="message-input"
            bind:value={currentInput}
            placeholder={
              isContextWindowFull ? 'Context window full - Delete text or end session to continue' :
              ($connectionStatus === 'connected' ? (pendingImage ? 'Add a caption (optional)...' : 'Type a message or paste an image... (Enter to send, Shift+Enter for new line)') : 'Connect to Core Service first...')
            }
            disabled={$connectionStatus !== 'connected' || isLoading}
            oninput={handleMentionInput}
            onkeydown={(e) => {
              // If mention autocomplete is open, let the component handle all navigation
              if (mentionAutocompleteVisible && (e.key === 'ArrowUp' || e.key === 'ArrowDown' || e.key === 'Tab' || e.key === 'Enter' || e.key === 'Escape')) {
                if (e.key === 'Enter' || e.key === 'Tab' || e.key === 'Escape') {
                  e.preventDefault(); // Prevent sending message when autocomplete is open
                }
                return; // Let MentionAutocomplete handle it
              }
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                // Send if: peer connected, OR local AI chat, OR Telegram chat, OR AI_xxx chat, OR agent chat (including 'default') AND (has text OR has pending image)
                if ((isPeerConnected || isTelegramChat || activeChatId === 'local_ai' || activeChatId.startsWith('ai_') || activeChatId.startsWith('agent_') || activeChatId === 'default') && (currentInput.trim() || pendingImage)) {
                  handleSendMessage();
                }
              }
            }}
            onpaste={handlePaste}
          ></textarea>

          <!-- Voice Recorder (v0.13.0) -->
          <VoiceRecorder
            disabled={$connectionStatus !== 'connected' || isLoading || (autoTranscribeEnabled && whisperModelLoading)}
            maxDuration={300}
            onRecordingComplete={handleRecordingComplete}
          />

          <button
            class="file-button"
            onclick={handleSendFile}
            disabled={$connectionStatus !== 'connected' || isLoading || activeChatId === 'local_ai' || activeChatId.startsWith('ai_') || activeChatId.startsWith('agent_') || activeChatId === 'default' || (!isPeerConnected && !isTelegramChat)}
            title={isPeerConnected || isTelegramChat || activeChatId.startsWith('agent_') || activeChatId === 'default' ? "Send file" : "Peer disconnected"}
          >
            📎
          </button>
          <button
            onclick={handleSendMessage}
            disabled={$connectionStatus !== 'connected' || isLoading || (!currentInput.trim() && !pendingImage) || isContextWindowFull || (!isPeerConnected && !isTelegramChat && activeChatId !== 'local_ai' && !activeChatId.startsWith('ai_') && !activeChatId.startsWith('agent_') && activeChatId !== 'default')}
            title={!isPeerConnected && !isTelegramChat && activeChatId !== 'local_ai' && !activeChatId.startsWith('ai_') && !activeChatId.startsWith('agent_') && activeChatId !== 'default' ? "Peer disconnected" : ""}
          >
            {#if isLoading}Sending...{:else}Send{/if}
          </button>
        </div>
      </div>

      <!-- Resize Handle -->
      <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
      <div
        class="resize-handle"
        class:resizing={isResizing}
        onmousedown={startResize}
        role="separator"
        aria-orientation="horizontal"
        aria-label="Resize chat panel"
      >
        <div class="resize-handle-line"></div>
      </div>
    </div>
  </div>
</main>

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

<!-- Group Invite Accept/Decline Dialog (v0.19.0) -->
<GroupInviteDialog
  bind:open={showGroupInviteDialog}
  invite={pendingGroupInvite}
  on:accept={handleGroupInviteAccept}
  on:decline={handleGroupInviteDecline}
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

<!-- Model Download Dialog (v0.13.5) -->
<ModelDownloadDialog
  bind:open={showModelDownloadDialog}
  modelName={modelDownloadInfo?.model_name || ''}
  downloadSizeGb={modelDownloadInfo?.download_size_gb || 3.0}
  cachePath={modelDownloadInfo?.cache_path || ''}
  providerAlias={modelDownloadInfo?.provider_alias || ''}
  downloading={isDownloadingModel}
  on:download={handleModelDownload}
  on:cancel={handleModelDownloadCancel}
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
            fileOfferToastMessage = granted
              ? 'Notifications enabled'
              : 'Notifications disabled - you can enable them later in settings';
            showFileOfferToast = true;
            setTimeout(() => showFileOfferToast = false, 3000);
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

<AgentTaskBoard
  bind:open={showAgentBoard}
  agentId={activeChatId && agentChatToAgentId.has(activeChatId)
    ? (agentChatToAgentId.get(activeChatId) ?? 'agent_001')
    : 'agent_001'}
  onSendToAgent={(msg) => {
    if (activeChatId && agentChatToAgentId.has(activeChatId)) {
      currentInput = msg;
      handleSendMessage();
    }
  }}
  on:close={() => showAgentBoard = false}
/>

<!-- Mention Autocomplete (Group Chats) -->
<MentionAutocomplete
  visible={mentionAutocompleteVisible}
  query={mentionQuery}
  members={getMentionableMembers()}
  position={mentionDropdownPosition}
  selectedIndex={mentionSelectedIndex}
  on:select={(e) => handleMentionSelect(e.detail)}
  on:navigate={(e) => handleMentionNavigate(e.detail.direction)}
  on:close={closeMentionAutocomplete}
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

<!-- Model Download Toast (v0.13.5) -->
{#if showModelDownloadToast}
  <Toast
    message={modelDownloadToastMessage}
    type={modelDownloadToastType}
    duration={modelDownloadToastType === 'error' ? 10000 : 5000}
    dismissible={true}
    onDismiss={() => {
      showModelDownloadToast = false;
      modelDownloadToastMessage = "";
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

<!-- Add AI Chat Dialog -->
{#if showAddAIChatDialog}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div
    class="modal-overlay"
    role="presentation"
    onclick={cancelAddAIChat}
    onkeydown={(e) => e.key === 'Escape' && cancelAddAIChat()}
  >
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div
      class="modal-content"
      role="dialog"
      aria-labelledby="modal-title"
      aria-modal="true"
      tabindex="-1"
      onclick={(e) => e.stopPropagation()}
    >
      <h2 id="modal-title">Add New AI Chat</h2>
      <p>Select an AI provider for the new chat:</p>

      <div class="dialog-provider-selector">
        <label for="new-chat-provider">Chat Type:</label>
        <select id="new-chat-provider" bind:value={selectedProviderForNewChat}>
          {#each $availableProviders.providers as provider}
            <option value={provider.alias}>
              {#if provider.alias === 'dpc_agent'}
                🤖 DPC Agent (Autonomous AI with tools)
              {:else}
                {provider.alias} - {provider.model}
              {/if}
            </option>
          {/each}
        </select>
        <p class="dialog-hint" style="font-size: 0.85em; color: #888; margin-top: 4px;">
          {#if selectedProviderForNewChat === 'dpc_agent'}
            Agents are autonomous AI assistants with tool access (file system, web search, etc.)
          {:else}
            Standard AI chat using the selected provider
          {/if}
        </p>
      </div>

      {#if selectedProviderForNewChat === 'dpc_agent'}
        <!-- Agent-specific fields -->
        <div class="dialog-provider-selector">
          <label for="new-agent-name">Agent Name:</label>
          <input type="text" id="new-agent-name" bind:value={newAgentName} placeholder="e.g., Coding Assistant, Research Bot..." style="width: 100%; padding: 8px; border-radius: 4px; border: 1px solid #ccc;" />
        </div>

        <div class="dialog-provider-selector">
          <label for="new-chat-llm-provider">AI Model (LLM):</label>
          <select id="new-chat-llm-provider" bind:value={selectedAgentLLMProvider}>
            {#each $availableProviders.providers as provider}
              {#if provider.alias !== 'dpc_agent'}
                <option value={provider.alias}>
                  {provider.alias} - {provider.model}
                </option>
              {/if}
            {/each}
          </select>
          <p class="dialog-hint" style="font-size: 0.85em; color: #888; margin-top: 4px;">
            The underlying AI model this agent will use for reasoning.
          </p>
        </div>

        <div class="dialog-provider-selector">
          <label for="new-chat-profile">Permission Profile:</label>
          <select id="new-chat-profile" bind:value={selectedProfileForNewAgent}>
            {#each availableAgentProfiles as profile}
              <option value={profile}>
                {profile}
              </option>
            {/each}
          </select>
          <p class="dialog-hint" style="font-size: 0.85em; color: #888; margin-top: 4px;">
            Controls what tools and data this agent can access. Configure in Firewall → Agent Profiles.
          </p>
        </div>
      {:else}
        <!-- Non-agent fields: Instruction Set -->
        <div class="dialog-provider-selector">
          <label for="new-chat-instruction-set">Instruction Set:</label>
          <select id="new-chat-instruction-set" bind:value={selectedInstructionSetForNewChat}>
            <option value="none">None (No Instructions)</option>
            {#if availableInstructionSets}
              {#each Object.entries(availableInstructionSets.sets) as [key, set]}
                <option value={key}>
                  {set.name} {availableInstructionSets.default === key ? '⭐' : ''}
                </option>
              {/each}
            {:else}
              <option value="general">General Purpose</option>
            {/if}
          </select>
        </div>
      {/if}

      <div class="dialog-actions">
        <button class="btn-cancel" onclick={cancelAddAIChat}>Cancel</button>
        <button class="btn-confirm" onclick={confirmAddAIChat}>Create Chat</button>
      </div>
    </div>
  </div>
{/if}

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

  /* Resize Handle */
  .resize-handle {
    height: 8px;
    background: linear-gradient(to bottom, #f9f9f9 0%, #e0e0e0 50%, #f9f9f9 100%);
    cursor: ns-resize;
    display: flex;
    align-items: center;
    justify-content: center;
    position: relative;
    transition: background 0.2s;
    user-select: none;
  }

  .resize-handle:hover {
    background: linear-gradient(to bottom, #e0e0e0 0%, #007bff 50%, #e0e0e0 100%);
  }

  .resize-handle:active,
  .resize-handle.resizing {
    background: linear-gradient(to bottom, #d0d0d0 0%, #0056b3 50%, #d0d0d0 100%);
  }

  .resize-handle-line {
    width: 60px;
    height: 3px;
    background: #999;
    border-radius: 2px;
    pointer-events: none;
  }

  .resize-handle:hover .resize-handle-line {
    background: #007bff;
  }

  .resize-handle:active .resize-handle-line {
    background: #0056b3;
  }

  .chat-input {
    padding: 1rem;
    border-top: 1px solid #eee;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    overflow-x: hidden; /* Prevent horizontal overflow */
    max-width: 100%; /* Constrain to parent width */
  }

  /* Personal Context Toggle Styles */
  .context-toggle {
    padding: 0.75rem;
    background: linear-gradient(135deg, #fff9f3 0%, #fff4e6 100%);
    border-radius: 8px;
    border: 1px solid #ffd4a3;
    margin-bottom: 0.5rem;
    position: relative;
  }

  .context-toggle-header {
    width: 100%;
    text-align: left;
    cursor: pointer;
    user-select: none;
    padding: 0.5rem;
    margin: -0.75rem -0.75rem 0.75rem -0.75rem;
    background: rgba(255, 212, 163, 0.3);
    border: none;
    border-radius: 8px 8px 0 0;
    border-bottom: 1px solid #ffd4a3;
    transition: background 0.2s ease;
  }

  .context-toggle-header:hover {
    background: rgba(255, 212, 163, 0.5);
  }

  .context-toggle-title {
    font-weight: 600;
    color: #c45500;
    font-size: 0.95rem;
  }

  .context-checkbox {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    cursor: pointer;
    user-select: none;
    font-size: 0.9rem;
    font-weight: 500;
    color: #374151;
  }

  .context-checkbox input[type="checkbox"] {
    width: 18px;
    height: 18px;
    cursor: pointer;
    accent-color: #f59e0b;
  }

  .context-hint {
    display: block;
    margin-top: 0.5rem;
    font-size: 0.75rem;
    color: #d97706;
    font-weight: 500;
    padding: 0.3rem 0.6rem;
    background: white;
    border-radius: 6px;
    border-left: 3px solid #f59e0b;
  }

  .ai-scope-selector {
    margin-top: 0.75rem;
    padding-top: 0.75rem;
    border-top: 1px solid #ffd4a3;
  }

  .ai-scope-selector label {
    display: block;
    font-size: 0.85rem;
    font-weight: 600;
    color: #374151;
    margin-bottom: 0.4rem;
  }

  .ai-scope-selector select {
    width: 100%;
    padding: 0.5rem;
    border: 1px solid #d1d5db;
    border-radius: 6px;
    font-size: 0.9rem;
    background: white;
    cursor: pointer;
    transition: border-color 0.2s;
  }

  .ai-scope-selector select:hover {
    border-color: #f59e0b;
  }

  .ai-scope-selector select:focus {
    outline: none;
    border-color: #f59e0b;
    box-shadow: 0 0 0 3px rgba(245, 158, 11, 0.1);
  }

  /* Phase 7: Status Badge Styles (for context update indicators) */
  .status-badge {
    display: inline-block;
    margin-left: 0.5rem;
    padding: 0.15rem 0.5rem;
    font-size: 0.7rem;
    font-weight: 600;
    border-radius: 12px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .status-badge.updated {
    background: #10b981;
    color: white;
    box-shadow: 0 2px 4px rgba(16, 185, 129, 0.3);
    animation: pulse-badge 2s ease-in-out infinite;
  }

  @keyframes pulse-badge {
    0%, 100% {
      opacity: 1;
      transform: scale(1);
    }
    50% {
      opacity: 0.8;
      transform: scale(0.98);
    }
  }

  /* Peer Context Selector Styles */
  .peer-context-selector {
    padding: 0.75rem;
    background: linear-gradient(135deg, #f8f3ff 0%, #f0f4ff 100%);
    border-radius: 8px;
    border: 1px solid #d4c5f9;
    margin-bottom: 0.5rem;
  }

  .peer-context-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 0.5rem;
  }

  .peer-context-label {
    font-size: 0.9rem;
    font-weight: 600;
    color: #5b21b6;
    margin: 0;
  }

  .peer-context-hint {
    font-size: 0.75rem;
    color: #8b5cf6;
    font-weight: 500;
    padding: 0.2rem 0.5rem;
    background: white;
    border-radius: 12px;
  }

  .peer-context-checkboxes {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
  }

  .peer-context-checkbox {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.4rem 0.7rem;
    background: white;
    border: 2px solid #e0e0e0;
    border-radius: 6px;
    cursor: pointer;
    transition: all 0.2s;
    font-size: 0.85rem;
    user-select: none;
  }

  .peer-context-checkbox:hover {
    border-color: #8b5cf6;
    background: #faf5ff;
  }

  .peer-context-checkbox input[type="checkbox"] {
    width: 16px;
    height: 16px;
    cursor: pointer;
    margin: 0;
    padding: 0;
  }

  .peer-context-checkbox input[type="checkbox"]:checked {
    accent-color: #8b5cf6;
  }

  .peer-context-checkbox span {
    color: #374151;
    font-weight: 500;
  }

  .input-row {
    display: flex;
    gap: 0.5rem;
    overflow-x: hidden; /* Prevent horizontal overflow */
    max-width: 100%; /* Constrain to parent width */
  }

  .input-row textarea {
    flex: 1;
    min-height: 120px;
    max-height: 240px;
    resize: vertical;
    overflow-wrap: break-word; /* Break long words */
    word-break: break-word; /* Break long unbreakable strings */
    overflow-x: auto; /* Allow horizontal scroll in textarea if needed */
    padding: 0.75rem; /* Add padding inside textarea */
    border: 1px solid #ddd;
    border-radius: 8px;
    font-family: inherit;
    font-size: 1rem;
  }

  .input-row button {
    min-width: 100px;
    height: 45px; /* Match file button height */
    padding: 0 16px; /* Horizontal padding only, height is fixed */
    align-self: flex-end;
    background: #5a67d8;
    color: white;
    border: none;
    border-radius: 4px;
    cursor: pointer;
    font-size: 1rem;
    font-weight: 500;
    transition: all 0.2s;
    box-shadow: 0 2px 8px rgba(90, 103, 216, 0.3);
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .input-row button:hover:not(:disabled) {
    background: #4c51bf;
    transform: translateY(-2px);
    box-shadow: 0 4px 12px rgba(90, 103, 216, 0.4);
  }

  .input-row button:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }
  
  /* Modal Dialog Styles */
  .modal-overlay {
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    background: rgba(0, 0, 0, 0.5);
    display: flex;
    justify-content: center;
    align-items: center;
    z-index: 1000;
  }

  .modal-content {
    background: white;
    padding: 2rem;
    border-radius: 12px;
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.2);
    max-width: 500px;
    width: 90%;
  }

  .modal-content h2 {
    margin: 0 0 0.5rem 0;
    color: #333;
    font-size: 1.5rem;
  }

  .modal-content p {
    margin: 0 0 1.5rem 0;
    color: #666;
    font-size: 0.95rem;
  }

  .dialog-provider-selector {
    margin-bottom: 1.5rem;
  }

  .dialog-provider-selector label {
    display: block;
    margin-bottom: 0.5rem;
    font-weight: 600;
    color: #333;
  }

  .dialog-provider-selector select {
    width: 100%;
    padding: 0.75rem;
    border: 1px solid #ddd;
    border-radius: 6px;
    font-size: 1rem;
    background: white;
    cursor: pointer;
  }

  .dialog-provider-selector select:hover {
    border-color: #4CAF50;
  }

  .dialog-provider-selector select:focus {
    outline: none;
    border-color: #4CAF50;
    box-shadow: 0 0 0 3px rgba(76, 175, 80, 0.1);
  }

  .dialog-actions {
    display: flex;
    gap: 1rem;
    justify-content: flex-end;
  }

  .btn-cancel,
  .btn-confirm {
    padding: 0.75rem 1.5rem;
    border: none;
    border-radius: 6px;
    font-size: 1rem;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.2s;
  }

  .btn-cancel {
    background: #f0f0f0;
    color: #666;
  }

  .btn-cancel:hover {
    background: #e0e0e0;
  }

  .btn-confirm {
    background: #4CAF50;
    color: white;
    box-shadow: 0 2px 4px rgba(76, 175, 80, 0.3);
  }

  .btn-confirm:hover {
    background: #45a049;
    box-shadow: 0 4px 8px rgba(76, 175, 80, 0.4);
    transform: translateY(-1px);
  }

  .btn-confirm:active {
    transform: translateY(0);
    box-shadow: 0 1px 3px rgba(76, 175, 80, 0.2);
  }

  /* Prevent text selection during resize */
  :global(body.resizing) {
    cursor: ns-resize !important;
    user-select: none !important;
  }

  /* File Transfer UI Styles (Week 1) */
  .file-button {
    min-width: 45px;
    width: 45px; /* Fixed width for icon button */
    height: 45px; /* Square button */
    padding: 8px;
    font-size: 20px;
    cursor: pointer;
    background: #5a67d8;
    color: white;
    border: none;
    border-radius: 4px;
    transition: all 0.2s;
    box-shadow: 0 2px 8px rgba(90, 103, 216, 0.3);
    align-self: flex-end;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .file-button:hover:not(:disabled) {
    background: #4c51bf;
    transform: translateY(-2px);
    box-shadow: 0 4px 12px rgba(90, 103, 216, 0.4);
  }

  .file-button:disabled {
    opacity: 0.4;
    cursor: not-allowed;
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