<!-- dpc-client/ui/src/routes/+page.svelte -->
<!-- FIXED VERSION - Proper URI detection for Direct TLS vs WebRTC -->

<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { writable } from "svelte/store";
  import { connectionStatus, nodeStatus, coreMessages, p2pMessages, sendCommand, resetReconnection, connectToCoreService, knowledgeCommitProposal, knowledgeCommitResult, personalContext, tokenWarning, extractionFailure, availableProviders, peerProviders, contextUpdated, peerContextUpdated, firewallRulesUpdated, unreadMessageCounts, resetUnreadCount, setActiveChat, fileTransferOffer, fileTransferProgress, fileTransferComplete, fileTransferCancelled, activeFileTransfers, sendFile, acceptFileTransfer, cancelFileTransfer, sendVoiceMessage, filePreparationStarted, filePreparationProgress, filePreparationCompleted, historyRestored, newSessionProposal, newSessionResult, proposeNewSession, voteNewSession, conversationReset, aiResponseWithImage, defaultProviders, providersList } from "$lib/coreService";
  import KnowledgeCommitDialog from "$lib/components/KnowledgeCommitDialog.svelte";
  import NewSessionDialog from "$lib/components/NewSessionDialog.svelte";
  import VoteResultDialog from "$lib/components/VoteResultDialog.svelte";
  import ContextViewer from "$lib/components/ContextViewer.svelte";
  import InstructionsEditor from "$lib/components/InstructionsEditor.svelte";
  import FirewallEditor from "$lib/components/FirewallEditor.svelte";
  import ProvidersEditor from "$lib/components/ProvidersEditor.svelte";
  import ProviderSelector from "$lib/components/ProviderSelector.svelte";
  import Toast from "$lib/components/Toast.svelte";
  import MarkdownMessage from "$lib/components/MarkdownMessage.svelte";
  import ImageMessage from "$lib/components/ImageMessage.svelte";
  import ChatPanel from "$lib/components/ChatPanel.svelte";
  import SessionControls from "$lib/components/SessionControls.svelte";
  import FileTransferUI from "$lib/components/FileTransferUI.svelte";
  import Sidebar from "$lib/components/Sidebar.svelte";
  import TokenWarningBanner from "$lib/components/TokenWarningBanner.svelte";
  import VoiceRecorder from "$lib/components/VoiceRecorder.svelte";
  import VoicePlayer from "$lib/components/VoicePlayer.svelte";
  import { ask, open } from '@tauri-apps/plugin-dialog';
  import { getCurrentWindow } from '@tauri-apps/api/window';
  import { showNotificationIfBackground, requestNotificationPermission } from '$lib/notificationService';
  import { estimateConversationUsage } from '$lib/tokenEstimator';

  console.log("Full D-PC Messenger loading...");
  
  // --- STATE ---
  type Message = {
    id: string;
    sender: string;
    senderName?: string;  // Display name for the sender (peer name or model name)
    text: string;
    timestamp: number;
    commandId?: string;
    model?: string;  // AI model name (for AI responses)
    attachments?: Array<{  // File attachments (Week 1) + Images (Phase 2.4)
      type: 'file' | 'image';
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
    }>;
  };
  const chatHistories = writable<Map<string, Message[]>>(new Map([
    ['local_ai', []]
  ]));
  
  let activeChatId = $state('local_ai');
  let currentInput = $state("");
  let isLoading = $state(false);
  let chatWindow = $state<HTMLElement>();  // Bound to ChatPanel's chatWindowElement
  let peerInput = $state("");  // RENAMED from peerUri for clarity
  let selectedComputeHost = $state("local");  // "local" or node_id for remote inference
  let selectedRemoteModel = $state("");  // Selected model when using remote compute host
  let selectedPeerContexts = $state(new Set<string>());  // Set of peer node_ids to fetch context from

  // Voice message state (v0.13.0 - Voice Messages)
  let voicePreview = $state<{ blob: Blob; duration: number } | null>(null);

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
  const aiChats = writable<Map<string, {name: string, provider: string, instruction_set_name?: string}>>(
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
  let showCommitDialog = $state(false);
  let showNewSessionDialog = $state(false);  // v0.11.3: mutual session approval
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

  // Add AI Chat dialog state
  let showAddAIChatDialog = $state(false);
  let selectedProviderForNewChat = $state("");
  let selectedInstructionSetForNewChat = $state("general");

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
  let pendingFileSend = $state<{ filePath: string, fileName: string, recipientId: string, recipientName: string } | null>(null);
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
    if (typeof window !== 'undefined') {
      try {
        const appWindow = getCurrentWindow();

        // Listen to focus changes (store unlisten function for cleanup)
        unlistenFocus = await appWindow.onFocusChanged(({ payload: focused }) => {
          windowFocused = focused;
          console.log(`[Notifications] Window focus changed: ${focused}`);
        });

        // Check initial focus state
        windowFocused = await appWindow.isFocused();
      } catch (error) {
        console.error('[Notifications] Failed to set up window tracking:', error);
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
      // Use sender_node_id if present (received from peer), else conversation_id (initiator)
      const conversationId = $newSessionResult.sender_node_id || $newSessionResult.conversation_id;

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

  // Reactive: Check if current peer is connected (for enabling/disabling send controls)
  let isPeerConnected = $derived(!activeChatId.startsWith('ai_') && activeChatId !== 'local_ai'
    ? ($nodeStatus?.peer_info?.some((p: any) => p.node_id === activeChatId) ?? false)
    : true); // AI chats don't require peer connection

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

              // Convert backend format to frontend format
              chatHistories.update(map => {
                const newMap = new Map(map);
                const loadedMessages = result.messages.map((msg: any, index: number) => ({
                  id: `backend-${index}-${Date.now()}`,
                  sender: msg.role === 'user' ? 'user' : activeChatId,
                  senderName: msg.role === 'user' ? 'You' : getPeerDisplayName(activeChatId),
                  text: msg.content,
                  timestamp: Date.now() - (result.messages.length - index) * 1000,
                  attachments: msg.attachments || []
                }));
                newMap.set(activeChatId, loadedMessages);
                console.log(`[ChatHistory] Updated chatHistories with ${loadedMessages.length} messages`);
                return newMap;
              });

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

    // Clear pending image and voice preview when actually switching to a different chat
    if (currentChat !== previousChatId) {
      if (pendingImage !== null) {
        pendingImage = null;
      }
      if (voicePreview !== null) {
        voicePreview = null;
      }
      previousChatId = currentChat;
    }
  });

  // Phase 7: Reactive: Check if context window is full (100% or more) - uses estimated total
  let isContextWindowFull = $derived($aiChats.has(activeChatId) && estimatedUsage.percentage >= 1.0);

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
      const { node_id, filename, size_bytes, transfer_id, sender_name } = $fileTransferOffer;
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
    if (!$nodeStatus || !$nodeStatus.peer_info) {
      console.log('[PeerNames] No nodeStatus or peer_info');
      return new Map();
    }
    console.log('[PeerNames] Building display names map from peer_info:', $nodeStatus.peer_info);
    const names = new Map<string, string>();
    for (const peer of $nodeStatus.peer_info) {
      if (peer.name) {
        const displayName = `${peer.name} | ${peer.node_id.slice(0, 20)}...`;
        console.log(`[PeerNames] ${peer.node_id.slice(0, 16)}... -> ${displayName}`);
        names.set(peer.node_id, displayName);
      } else {
        const displayName = `${peer.node_id.slice(0, 20)}...`;
        console.log(`[PeerNames] ${peer.node_id.slice(0, 16)}... -> ${displayName} (no name)`);
        names.set(peer.node_id, displayName);
      }
    }
    console.log('[PeerNames] Final map size:', names.size);
    return names;
  });

  function getPeerDisplayName(peerId: string): string {
    // Use the reactive map, with fallback for peers not in peer_info yet
    return peerDisplayNames.get(peerId) || `${peerId.slice(0, 20)}...`;
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

      // Clear input and pending image
      currentInput = "";
      pendingImage = null;

      // Check if this is an AI chat or P2P chat (Phase 2.3: Fix P2P screenshot sharing)
      if ($aiChats.has(activeChatId)) {
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
          isLoading = true;

          // Parse vision provider to extract compute_host for remote vision
          const visionProvider = parseProviderSelection(selectedVisionProvider);

          const payload: any = {
            conversation_id: activeChatId,
            image_base64: imageData.dataUrl,
            filename: imageData.filename,
            caption: text,
            provider_alias: visionProvider.alias
          };

          // Add compute_host if using remote vision provider
          if (visionProvider.source === 'remote' && visionProvider.nodeId) {
            payload.compute_host = visionProvider.nodeId;
          }

          await sendCommand('send_image', payload);
          autoScroll();
          // Note: Don't set isLoading = false here!
          // The ai_response_with_image event handler will clear it when the vision response arrives
        } catch (error) {
          console.error('Error sending image:', error);
          fileOfferToastMessage = `Failed to send image: ${error}`;
          showFileOfferToast = true;
          setTimeout(() => showFileOfferToast = false, 5000);
          isLoading = false;  // Only clear loading on error
        }
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

    chatHistories.update(h => {
      const newMap = new Map(h);
      const hist = newMap.get(activeChatId) || [];
      newMap.set(activeChatId, [...hist, { id: crypto.randomUUID(), sender: 'user', text, timestamp: Date.now() }]);
      return newMap;
    });

    // Check if this is an AI chat (local_ai or ai_chat_*)
    if ($aiChats.has(activeChatId)) {
      isLoading = true;
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

      // Phase 2.3: Parse text provider to support remote text inference
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

      const success = sendCommand("execute_ai_query", payload, commandId);
      if (!success) {
        isLoading = false;
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
    } else {
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

  function handleAddAIChat() {
    if (!$availableProviders || !$availableProviders.providers || $availableProviders.providers.length === 0) {
      alert("No AI providers available. Please configure providers in ~/.dpc/providers.toml");
      return;
    }

    // Set default selections and show dialog
    selectedProviderForNewChat = $availableProviders.default_provider;
    selectedInstructionSetForNewChat = availableInstructionSets?.default || "general";
    showAddAIChatDialog = true;
  }

  function confirmAddAIChat() {
    if (!selectedProviderForNewChat) return;

    // Find the selected provider
    const provider = $availableProviders.providers.find((p: any) => p.alias === selectedProviderForNewChat);
    if (!provider) {
      alert(`Provider '${selectedProviderForNewChat}' not found.`);
      return;
    }

    // Create new AI chat ID
    const chatId = `ai_chat_${crypto.randomUUID().slice(0, 8)}`;
    const chatName = `${provider.alias} (${provider.model})`;

    // Add to aiChats
    aiChats.update(chats => {
      const newMap = new Map(chats);
      newMap.set(chatId, {
        name: chatName,
        provider: selectedProviderForNewChat,
        instruction_set_name: selectedInstructionSetForNewChat
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
  }

  async function handleDeleteAIChat(chatId: string) {
    console.log('Delete AI chat button clicked for:', chatId);

    if (chatId === 'local_ai') {
      await ask("Cannot delete the default Local AI chat.", { title: "D-PC Messenger", kind: "info" });
      return;
    }

    // Use Tauri's ask dialog (works on all platforms including macOS)
    const shouldDelete = await ask(
      "Delete this AI chat? This will permanently remove the chat history.",
      { title: "Confirm Deletion", kind: "warning" }
    );
    console.log('User confirmed deletion:', shouldDelete);

    if (!shouldDelete) {
      return;
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

    // Switch to default chat
    if (activeChatId === chatId) {
      activeChatId = 'local_ai';
    }

    console.log('AI chat deleted successfully');
  }

  // File transfer handlers (Week 1)
  async function handleSendFile() {
    // Only allow file transfer to P2P chats (not local_ai or ai_xxx chats)
    if (activeChatId === 'local_ai' || activeChatId.startsWith('ai_')) {
      await ask("File transfer is only available in P2P chats.", { title: "D-PC Messenger", kind: "info" });
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
  function handlePaste(event: ClipboardEvent) {
    const items = event.clipboardData?.items;
    if (!items) return;

    for (const item of items) {
      if (item.type.startsWith('image/')) {
        event.preventDefault();
        const blob = item.getAsFile();

        // Check if blob is valid
        if (!blob) continue;

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
          pendingImage = {
            dataUrl: e.target?.result as string,
            filename: `screenshot_${Date.now()}.png`,
            sizeBytes: blob.size
          };
        };
        reader.readAsDataURL(blob);
        break;
      }
    }
  }

  function clearPendingImage() {
    pendingImage = null;
  }

  async function handleConfirmSendFile() {
    if (!pendingFileSend || isSendingFile) return;  // Guard against double-click

    isSendingFile = true;  // Set flag immediately to block subsequent clicks

    try {
      console.log(`Sending file: ${pendingFileSend.filePath} to ${pendingFileSend.recipientId}`);
      await sendFile(pendingFileSend.recipientId, pendingFileSend.filePath);

      showSendFileDialog = false;
      pendingFileSend = null;

      fileOfferToastMessage = `Sending file...`;
      showFileOfferToast = true;
      setTimeout(() => showFileOfferToast = false, 3000);
    } catch (error) {
      console.error('Error sending file:', error);
      fileOfferToastMessage = `Failed to send file: ${error}`;
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
      // For P2P chats, send via file transfer
      if (activeChatId !== 'local_ai' && !activeChatId.startsWith('ai_')) {
        await sendVoiceMessage(activeChatId, voicePreview.blob, voicePreview.duration);
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

      // Parse selected voice provider (v0.13.0+: Use selected provider instead of auto-detect)
      const parsedProvider = parseProviderSelection(selectedVoiceProvider || selectedTextProvider);

      // Call backend for transcription
      const response = await sendCommand('transcribe_audio', {
        audio_base64: base64Audio,
        mime_type: voicePreview.blob.type || 'audio/webm',
        provider_alias: parsedProvider.alias  // v0.13.0+: Pass selected provider
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

  // --- HANDLE INCOMING MESSAGES ---
  $effect(() => {
    if ($coreMessages?.id) {
      const message = $coreMessages;

    if (message.command === "execute_ai_query") {
      console.log(`[TokenCounter] execute_ai_query response: status=${message.status}, isLoading before clear=${isLoading}`);
      isLoading = false;
      console.log(`[TokenCounter] isLoading cleared: ${isLoading}`);

      const newText = message.status === "OK"
        ? message.payload.content
        : `Error: ${message.payload?.message || 'Unknown error'}`;
      const newSender = message.status === "OK" ? 'ai' : 'system';
      const modelName = message.status === "OK" ? message.payload.model : undefined;

      // Show toast notification for errors (helps remote users see host failures)
      if (message.status !== "OK") {
        console.error(`[TokenCounter] AI query failed: ${message.payload?.message}`);
        fileOfferToastMessage = `⚠️ AI Query Failed: ${message.payload?.message || 'Unknown error'}`;
        showFileOfferToast = true;
        setTimeout(() => showFileOfferToast = false, 7000);  // 7s for errors (longer than success)
      }

      const responseCommandId = message.id;

      // Find which chat this command belongs to
      const chatId = commandToChatMap.get(responseCommandId);
      if (chatId) {
        chatHistories.update(h => {
          const newMap = new Map(h);
          const hist = newMap.get(chatId) || [];
          newMap.set(chatId, hist.map(m =>
            m.commandId === responseCommandId ? { ...m, sender: newSender, text: newText, model: modelName, commandId: undefined } : m
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

      autoScroll();
    }
    }
  });

  // Handle AI vision responses (Phase 2)
  $effect(() => {
    if ($aiResponseWithImage) {
      const response = $aiResponseWithImage;
      isLoading = false;

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

  let activeMessages = $derived($chatHistories.get(activeChatId) || []);
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
      onToggleAutoKnowledgeDetection={toggleAutoKnowledgeDetection}
      onConnectPeer={handleConnectPeer}
      onResetUnreadCount={resetUnreadCount}
      onGetPeerDisplayName={getPeerDisplayName}
      onAddAIChat={handleAddAIChat}
      onDeleteAIChat={handleDeleteAIChat}
      onDisconnectPeer={handleDisconnectPeer}
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
              {#if $aiChats.has(activeChatId)}
                {$aiChats.get(activeChatId)?.name || 'AI Assistant'}
              {:else}
                Chat with {getPeerDisplayName(activeChatId)}
              {/if}
            </h2>
          </button>
        </div>

        {#if !chatHeaderCollapsed}
          <ProviderSelector
            bind:selectedComputeHost
            bind:selectedTextProvider
            bind:selectedVisionProvider
            bind:selectedVoiceProvider
            showForChatId={activeChatId}
            isAIChat={$aiChats.has(activeChatId)}
            providersList={$providersList}
            peerProviders={$peerProviders}
            nodeStatus={$nodeStatus}
            defaultProviders={$defaultProviders}
          />

        <SessionControls
          showForChatId={activeChatId}
          isAIChat={$aiChats.has(activeChatId)}
          isPeerConnected={isPeerConnected}
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
      />

      <div class="chat-input">
        {#if $aiChats.has(activeChatId)}
          <!-- Personal Context Toggle -->
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
            onkeydown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                // Only send if peer is connected (or local AI chat) AND (has text OR has pending image)
                if ((isPeerConnected || activeChatId === 'local_ai' || activeChatId.startsWith('ai_')) && (currentInput.trim() || pendingImage)) {
                  handleSendMessage();
                }
              }
            }}
            onpaste={handlePaste}
          ></textarea>

          <!-- Voice Recorder (v0.13.0) -->
          <VoiceRecorder
            disabled={$connectionStatus !== 'connected' || isLoading}
            maxDuration={300}
            onRecordingComplete={handleRecordingComplete}
          />

          <button
            class="file-button"
            onclick={handleSendFile}
            disabled={$connectionStatus !== 'connected' || isLoading || activeChatId === 'local_ai' || activeChatId.startsWith('ai_') || !isPeerConnected}
            title={isPeerConnected ? "Send file (P2P chat only)" : "Peer disconnected"}
          >
            📎
          </button>
          <button
            onclick={handleSendMessage}
            disabled={$connectionStatus !== 'connected' || isLoading || (!currentInput.trim() && !pendingImage) || isContextWindowFull || !isPeerConnected}
            title={!isPeerConnected && activeChatId !== 'local_ai' && !activeChatId.startsWith('ai_') ? "Peer disconnected" : ""}
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
        <label for="new-chat-provider">Provider:</label>
        <select id="new-chat-provider" bind:value={selectedProviderForNewChat}>
          {#each $availableProviders.providers as provider}
            <option value={provider.alias}>
              {provider.alias} - {provider.model}
            </option>
          {/each}
        </select>
      </div>

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

  .collapse-indicator {
    font-size: 0.8em;
    color: #666;
    transition: transform 0.2s ease;
    display: inline-block;
    min-width: 1em;
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