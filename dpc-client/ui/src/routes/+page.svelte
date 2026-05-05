<!-- dpc-client/ui/src/routes/+page.svelte -->
<!-- FIXED VERSION - Proper URI detection for Direct TLS vs WebRTC -->

<script lang="ts">
  import { onMount } from "svelte";
  import { writable } from "svelte/store";
  import { connectionStatus, nodeStatus, sendCommand, resetReconnection, connectToCoreService, knowledgeCommitProposal, personalContext, tokenWarning, extractionFailure, availableProviders, peerProviders, unreadMessageCounts, resetUnreadCount, setActiveChat, newSessionProposal, proposeNewSession, voteNewSession, defaultProviders, providersList, groupChats, loadGroups, listAgents, agentsList, sleepStateChanged, sleepProgress } from "$lib/coreService";
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
  import ChatMessageList from "$lib/components/ChatMessageList.svelte";
  import SessionControls from "$lib/components/SessionControls.svelte";
  import TelegramStatus from "$lib/components/TelegramStatus.svelte";
  import Sidebar from "$lib/components/Sidebar.svelte";
  import NewGroupDialog from "$lib/components/NewGroupDialog.svelte";
  import GroupSettingsDialog from "$lib/components/GroupSettingsDialog.svelte";
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
  import SessionEventsPanel from "$lib/panels/SessionEventsPanel.svelte";
  import KnowledgeEventsPanel from "$lib/panels/KnowledgeEventsPanel.svelte";
  import PersistencePanel from "$lib/panels/PersistencePanel.svelte";
  import { estimateConversationUsage } from '$lib/tokenEstimator';
  import type { Message, Mention } from '$lib/types.js';

  // Tauri APIs - will be loaded in onMount if in Tauri environment
  let ask: any = null;
  let open: any = null;

  console.log("Full D-PC Messenger loading...");
  
  // --- STATE ---
  // Message and Mention types imported from $lib/types.ts (canonical definitions)
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

  // Current input text (bound to ChatPanel) — needed for real-time token estimation in SessionControls
  let currentInput = $state('');

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

  // Track which chat each AI command belongs to (commandId -> chatId).
  // Persisted to localStorage so the mapping survives page reloads / Vite HMR /
  // Tauri restarts that happen while a long-running agent query is in flight.
  // Without this, any reload between sendCommand and the response races the
  // routing logic and forces a fallback to activeChatId — see UI-1.
  const COMMAND_CHAT_MAP_KEY = 'dpc-cmd-chat-map';
  let commandToChatMap = new Map<string, string>(
    (() => {
      try {
        const raw = typeof localStorage !== 'undefined' ? localStorage.getItem(COMMAND_CHAT_MAP_KEY) : null;
        return raw ? JSON.parse(raw) as [string, string][] : [];
      } catch {
        return [];
      }
    })()
  );
  function persistCommandToChatMap() {
    try {
      if (typeof localStorage !== 'undefined') {
        localStorage.setItem(COMMAND_CHAT_MAP_KEY, JSON.stringify([...commandToChatMap.entries()]));
      }
    } catch { /* quota / privacy mode — ignore */ }
  }

  let processedMessageIds = new Set<string>();

  // Knowledge Architecture UI state
  let showContextViewer = $state(false);
  let showInstructionsEditor = $state(false);
  let showFirewallEditor = $state(false);
  let showProvidersEditor = $state(false);
  let showAgentBoard = $state(false);
  let showCommitDialog = $state(false);
  let isExtractingKnowledge = $state(false);
  let showNewSessionDialog = $state(false);  // v0.11.3: mutual session approval
  let showNewGroupDialog = $state(false);  // v0.19.0: group chat creation
  // showGroupInviteDialog + pendingGroupInvite moved to GroupPanel.svelte (Step 7)
  let showGroupSettingsDialog = $state(false);  // v0.19.0: group settings/members panel
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

  // Sleep state (ADR-014)
  let isSleeping = $state(false);

  function handleToggleSleep() {
    isSleeping = !isSleeping;
    sendCommand('toggle_sleep', { agent_id: activeChatId });
  }

  // Update sleep state from backend events
  $effect(() => {
    const state = $sleepStateChanged;
    if (state && state.agent_id === activeChatId) {
      isSleeping = state.status === 'sleeping';
      if (state.status === 'awake') {
        const cmd = sendCommand('get_conversation_history', { conversation_id: state.agent_id });
        if (cmd && typeof cmd === 'object' && 'then' in cmd) (cmd as Promise<any>).then((result: any) => {
          if (result?.status === 'success' && result.messages?.length > 0) {
            chatHistories.update(map => {
              const newMap = new Map(map);
              const agentName = $agentsList?.find((a: any) => a.agent_id === state.agent_id)?.name || state.agent_id;
              const msgs = result.messages.map((msg: any, index: number) => {
                const ts = msg.timestamp ? new Date(msg.timestamp).getTime() : Date.now() - (result.messages.length - index) * 1000;
                const stableId = msg.id || `${state.agent_id}-${ts}`;
                const isAgent = msg.role === 'assistant' || msg.sender_name === state.agent_id;
                return {
                  id: stableId,
                  sender: isAgent ? state.agent_id : 'user',
                  senderName: msg.sender_name || (isAgent ? agentName : 'You'),
                  text: msg.content,
                  timestamp: ts,
                  attachments: msg.attachments || [],
                  thinking: msg.thinking,
                  streamingRaw: msg.streaming_raw,
                };
              });
              newMap.set(state.agent_id, msgs);
              return newMap;
            });
          }
        });
      }
    }
  });

  // Whisper loading effects moved to VoicePanel.svelte (Step 6)
  // Agent progress and streaming effects moved to AgentPanel.svelte (Step 5)

  // AI chat + history localStorage persistence moved to PersistencePanel.svelte

  // Telegram message effects moved to TelegramPanel.svelte (Step 8)

  // Phase 7: Context hash tracking for "Updated" status indicators
  let currentContextHash = $state("");  // Current hash from backend (when context is saved)
  let lastSentContextHash = $state(new Map<string, string>());  // Per-conversation: last hash sent to AI
  let peerContextHashes = $state(new Map<string, string>());  // Per-peer: current hash from backend
  let lastSentPeerHashes = $state(new Map<string, Map<string, string>>());  // Per-conversation, per-peer: last hash sent

  // "Start new session?" confirmation state (#7 fix: confirm BEFORE reset)
  let showNewSessionConfirm = $state(false);
  let pendingNewSessionChatId = $state<string | null>(null);

  // Connection state (Phase 2: UX improvements)
  let isConnecting = $state(false);
  let connectionError = $state("");
  let showConnectionError = $state(false);

  // UI collapse states
  let modeSectionCollapsed = $state(true);  // Mode section collapsible (collapsed by default)
  let chatHeaderCollapsed = $state(false);  // Chat header collapsible

  // Chat history loading state (prevent infinite loop)
  let loadingHistory = new Set<string>();

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

  // Knowledge commit/context/token warning effects moved to KnowledgeEventsPanel.svelte

  // Model download effects moved to ModelDownloadPanel.svelte (Step 8)
  // Session proposal/result/reset effects moved to SessionEventsPanel.svelte


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

  // Reactive: Estimate token usage including current input (real-time feedback in SessionControls)
  let estimatedUsage = $derived(
    estimateConversationUsage(effectiveTokenUsage, currentInput)
  );

  // Reactive: Check if current peer/group is connected (for enabling/disabling send controls)
  let isPeerConnected = $derived(
    activeChatId.startsWith('ai_') || activeChatId === 'local_ai'
      ? true  // AI chats don't require peer connection
      : activeChatId.startsWith('group-')
        ? true  // Group chats always allow sending — agents respond locally, peers are optional
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
  function getPeersByStrategy(peerInfo: any[] | undefined) {
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

  // Scroll to bottom when switching chats (double-rAF ensures DOM is updated)
  $effect(() => {
    void activeChatId;
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (chatWindow) chatWindow.scrollTop = chatWindow.scrollHeight;
    }));
  });

  // Show connection error toast when backend becomes unreachable after exhausting reconnects
  $effect(() => {
    if ($connectionStatus === 'error') {
      connectionError = "Backend unreachable — check that the service is running";
      showConnectionError = true;
    }
  });

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
      const peerName = peer.name || peer.display_name;
      if (peerName) {
        const displayName = `${peerName} | ${peer.node_id}`;
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
    const { proposal_id, vote, comment, entries, summary } = event.detail;
    sendCommand("vote_knowledge_commit", {
      proposal_id,
      vote,
      comment,
      entries,
      summary
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

  async function handleEndSession(conversationId: string) {
    // No confirm dialog — user can Reject the proposal if extraction was accidental.
    isExtractingKnowledge = true;
    sendCommand("end_conversation_session", {
      conversation_id: conversationId
    });
  }

  function handleNewChat(chatId: string) {
    // #7 fix: Show confirmation dialog BEFORE sending reset to backend.
    // Previously, proposeNewSession() was called immediately, archiving history
    // before the user could confirm. Now we confirm first.
    pendingNewSessionChatId = chatId;
    showNewSessionConfirm = true;
  }

  function confirmNewSession() {
    if (pendingNewSessionChatId) {
      proposeNewSession(pendingNewSessionChatId);
    }
    showNewSessionConfirm = false;
    pendingNewSessionChatId = null;
  }

  function cancelNewSession() {
    showNewSessionConfirm = false;
    pendingNewSessionChatId = null;
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
      onGetAgentModelConfig={async (agentId) => await sendCommand('get_agent_model_config', { agent_id: agentId })}
      onSaveAgentModelConfig={async (agentId, config) => { await sendCommand('save_agent_model_config', { agent_id: agentId, ...config }); const r = await listAgents(); if (r?.status === 'success' && r.agents) agentsList.set(r.agents); if (config.provider_alias) aiChats.update(m => { const e = m.get(agentId); if (e) { e.llm_provider = config.provider_alias; } return new Map(m); }); }}
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
                {$agentsList.find((a: any) => a.agent_id === activeChatId)?.name || $aiChats.get(activeChatId)?.name || 'AI Assistant'}
              {:else}
                Chat with {getPeerDisplayName(activeChatId)}
              {/if}
            </h2>
          </button>

          <!-- Group topic display (v0.19.0) - inside title section for proper positioning -->
          {#if !chatHeaderCollapsed && isGroupChat && $groupChats.get(activeChatId)?.topic}
            <div class="group-topic">{$groupChats.get(activeChatId)?.topic}</div>
          {/if}

          <!-- Auto Transcribe toggle (P2P and Telegram chats, NOT AI or group chats — groups have it in Settings) -->
          {#if !$aiChats.has(activeChatId) && activeChatId !== 'local_ai' && !activeChatId.startsWith('group-')}
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
            messageCount={$chatHistories.get(activeChatId)?.length ?? 0}
            bind:enableMarkdown
            isExtracting={isExtractingKnowledge}
            {isSleeping}
            sleepCurrent={$sleepProgress?.agent_id === activeChatId ? $sleepProgress?.current ?? 0 : 0}
            sleepTotal={$sleepProgress?.agent_id === activeChatId ? $sleepProgress?.total ?? 0 : 0}
            sleepPhase={$sleepProgress?.agent_id === activeChatId ? $sleepProgress?.phase ?? '' : ''}
            onNewSession={handleNewChat}
            onEndSession={handleEndSession}
            onToggleSleep={handleToggleSleep}
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
        {persistCommandToChatMap}
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
        {isSleeping}
        bind:chatPanelHeight
        bind:showAgentBoard
        bind:currentInput
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
  {persistCommandToChatMap}
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
  nodeAgents={$agentsList.map((a: any) => ({ agent_id: a.agent_id, name: a.name, provider_alias: a.provider_alias }))}
  {autoTranscribeEnabled}
  {whisperModelLoading}
  on:addMember={handleGroupAddMember}
  on:removeMember={handleGroupRemoveMember}
  on:toggleAutoTranscribe={() => { autoTranscribeEnabled = !autoTranscribeEnabled; voicePanelComp?.saveAutoTranscribeSetting(); }}
  on:updateAgents={async (e) => {
    const result = await sendCommand('set_group_agents', { group_id: e.detail.group_id, agent_ids: e.detail.agent_ids });
    if (result?.status === 'success') {
      groupChats.update(map => {
        const newMap = new Map(map);
        const grp = newMap.get(e.detail.group_id);
        if (grp) {
          const agents = { ...(grp.agents || {}) };
          agents[$nodeStatus?.node_id || ''] = e.detail.agent_ids;
          newMap.set(e.detail.group_id, { ...grp, agents });
        }
        return newMap;
      });
    }
  }}
/>

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

<!-- Clear messages after knowledge extraction? -->
<!-- Start new session? Confirm BEFORE reset (#7 fix) -->
{#if showNewSessionConfirm}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="confirm-overlay" role="dialog" aria-modal="true" aria-label="New session?">
    <div class="confirm-dialog">
      <p>Start new session? Current history will be archived.</p>
      <div class="confirm-actions">
        <button class="btn-confirm-yes" onclick={confirmNewSession}>
          Yes, new session
        </button>
        <button class="btn-confirm-no" onclick={cancelNewSession}>
          Cancel
        </button>
      </div>
    </div>
  </div>
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

<!-- PersistencePanel: debounced localStorage persistence for aiChats + chatHistories -->
<PersistencePanel {aiChats} {chatHistories} />

<!-- KnowledgeEventsPanel: commit/token/extraction/context hash events -->
<KnowledgeEventsPanel
  onOpenCommitDialog={() => { showCommitDialog = true; isExtractingKnowledge = false; }}
  onUpdateTokenUsage={(convId, usage) => {
    tokenUsageMap = new Map(tokenUsageMap);
    tokenUsageMap.set(convId, usage);
  }}
  onShowTokenWarning={(message) => {
    showTokenWarning = true;
    tokenWarningMessage = message;
  }}
  onShowExtractionFailure={(message) => {
    showExtractionFailure = true;
    extractionFailureMessage = message;
    isExtractingKnowledge = false;
  }}
  onShowCommitResult={(message, type, result) => {
    commitResultMessage = message;
    commitResultType = type;
    currentVoteResult = result;
    // Open the full Voting Results dialog automatically. The toast still
    // appears as a secondary notification, but the dialog is the primary
    // surface — without auto-open, results were lost if the user didn't
    // click the toast in time. See backlog: knowledge_extraction_ux.
    showVoteResultDialog = true;
    showCommitResultToast = true;
  }}
  onCloseCommitDialog={() => {
    showCommitDialog = false;
    knowledgeCommitProposal.set(null);
  }}
  onUpdateContextHash={(hash) => { currentContextHash = hash; }}
  onUpdatePeerContextHash={(nodeId, hash) => {
    peerContextHashes = new Map(peerContextHashes);
    peerContextHashes.set(nodeId, hash);
  }}
/>

<!-- SessionEventsPanel: session proposal/result/reset effects -->
<SessionEventsPanel
  {chatHistories}
  {getPeerDisplayName}
  onOpenNewSessionDialog={() => { showNewSessionDialog = true; }}
  onConversationReset={(_convId, clear) => {
    // Auto-clear without confirm dialog.
    // User already confirmed via New Session dialog (Dialog 2).
    // Extraction no longer sends conversation_reset (commit c98debc).
    clear();
  }}
  onClearStateForConversation={(convId) => {
    tokenUsageMap = new Map(tokenUsageMap);
    tokenUsageMap.delete(convId);
    lastSentContextHash = new Map(lastSentContextHash);
    lastSentContextHash.delete(convId);
    lastSentPeerHashes = new Map(lastSentPeerHashes);
    lastSentPeerHashes.delete(convId);
  }}
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
  .confirm-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 9000;
  }

  .confirm-dialog {
    background: #1e1e2e;
    border: 1px solid #45475a;
    border-radius: 10px;
    padding: 1.5rem 2rem;
    box-shadow: 0 8px 32px rgba(0,0,0,0.4);
    text-align: center;
    min-width: 280px;
  }

  .confirm-dialog p {
    margin: 0 0 1.25rem;
    color: #cdd6f4;
    font-size: 1rem;
  }

  .confirm-actions {
    display: flex;
    gap: 0.75rem;
    justify-content: center;
  }

  .btn-confirm-yes {
    padding: 0.5rem 1.25rem;
    background: #f44336;
    color: white;
    border: none;
    border-radius: 6px;
    font-size: 0.9rem;
    cursor: pointer;
  }

  .btn-confirm-yes:hover { background: #d32f2f; }

  .btn-confirm-no {
    padding: 0.5rem 1.25rem;
    background: #4caf50;
    color: white;
    border: none;
    border-radius: 6px;
    font-size: 0.9rem;
    cursor: pointer;
  }

  .btn-confirm-no:hover { background: #388e3c; }

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

</style>