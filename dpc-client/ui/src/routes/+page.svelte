<!-- dpc-client/ui/src/routes/+page.svelte -->
<!-- FIXED VERSION - Proper URI detection for Direct TLS vs WebRTC -->

<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { writable } from "svelte/store";
  import { connectionStatus, nodeStatus, coreMessages, p2pMessages, sendCommand, resetReconnection, connectToCoreService, knowledgeCommitProposal, knowledgeCommitResult, personalContext, tokenWarning, extractionFailure, availableProviders, peerProviders, contextUpdated, peerContextUpdated, firewallRulesUpdated, unreadMessageCounts, resetUnreadCount, setActiveChat, fileTransferOffer, fileTransferProgress, fileTransferComplete, fileTransferCancelled, activeFileTransfers, sendFile, acceptFileTransfer, cancelFileTransfer, filePreparationStarted, filePreparationProgress, filePreparationCompleted, historyRestored, newSessionProposal, newSessionResult, proposeNewSession, voteNewSession } from "$lib/coreService";
  import KnowledgeCommitDialog from "$lib/components/KnowledgeCommitDialog.svelte";
  import NewSessionDialog from "$lib/components/NewSessionDialog.svelte";
  import VoteResultDialog from "$lib/components/VoteResultDialog.svelte";
  import ContextViewer from "$lib/components/ContextViewer.svelte";
  import InstructionsEditor from "$lib/components/InstructionsEditor.svelte";
  import FirewallEditor from "$lib/components/FirewallEditor.svelte";
  import ProvidersEditor from "$lib/components/ProvidersEditor.svelte";
  import Toast from "$lib/components/Toast.svelte";
  import MarkdownMessage from "$lib/components/MarkdownMessage.svelte";
  import { ask, open } from '@tauri-apps/plugin-dialog';
  import { getCurrentWindow } from '@tauri-apps/api/window';
  import { showNotificationIfBackground, requestNotificationPermission } from '$lib/notificationService';

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
    attachments?: Array<{  // File attachments (Week 1)
      type: string;
      filename: string;
      size_bytes: number;
      size_mb?: number;
      hash?: string;
      mime_type?: string;
      transfer_id?: string;
      status?: string;
    }>;
  };
  const chatHistories = writable<Map<string, Message[]>>(new Map([
    ['local_ai', []]
  ]));
  
  let activeChatId: string = 'local_ai';
  let currentInput: string = "";
  let isLoading: boolean = false;
  let chatWindow: HTMLElement;
  let peerInput: string = "";  // RENAMED from peerUri for clarity
  let selectedComputeHost: string = "local";  // "local" or node_id for remote inference
  let selectedRemoteModel: string = "";  // Selected model when using remote compute host
  let selectedPeerContexts: Set<string> = new Set();  // Set of peer node_ids to fetch context from

  // Resizable chat panel state
  let chatPanelHeight: number = (() => {
    // Load saved height from localStorage, default to calc(100vh - 120px)
    const saved = localStorage.getItem('chatPanelHeight');
    return saved ? parseInt(saved, 10) : 600;
  })();
  let isResizing: boolean = false;
  let resizeStartY: number = 0;
  let resizeStartHeight: number = 0;

  // Store provider selection per chat (chatId -> provider alias)
  const chatProviders = writable<Map<string, string>>(new Map());

  // Store AI chat metadata (chatId -> {name: string, provider: string})
  const aiChats = writable<Map<string, {name: string, provider: string}>>(
    new Map([['local_ai', {name: 'Local AI Chat', provider: ''}]])
  );

  // Track which chat each AI command belongs to (commandId -> chatId)
  let commandToChatMap = new Map<string, string>();

  let processedMessageIds = new Set<string>();

  // Knowledge Architecture UI state
  let showContextViewer: boolean = false;
  let showInstructionsEditor: boolean = false;
  let showFirewallEditor: boolean = false;
  let showProvidersEditor: boolean = false;
  let showCommitDialog: boolean = false;
  let showNewSessionDialog: boolean = false;  // v0.11.3: mutual session approval
  let autoKnowledgeDetection: boolean = false;  // Default: disabled

  // Token tracking state (Phase 2)
  let tokenUsageMap: Map<string, {used: number, limit: number}> = new Map();
  let showTokenWarning: boolean = false;
  let tokenWarningMessage: string = "";

  // Knowledge extraction failure state (Phase 4)
  let showExtractionFailure: boolean = false;
  let extractionFailureMessage: string = "";

  // Knowledge commit result notification state
  let showCommitResultToast: boolean = false;
  let commitResultMessage: string = "";
  let commitResultType: "info" | "error" | "warning" = "info";
  let showVoteResultDialog: boolean = false;
  let currentVoteResult: any = null;

  // Add AI Chat dialog state
  let showAddAIChatDialog: boolean = false;
  let selectedProviderForNewChat: string = "";

  // Personal context inclusion toggle
  let includePersonalContext: boolean = true;

  // AI Scope selection (for filtering what local AI can access)
  let selectedAIScope: string = ""; // Empty = no filtering (full context)
  let availableAIScopes: string[] = []; // List of scope names from privacy rules
  let aiScopesLoaded: boolean = false; // Guard flag to prevent infinite loop

  // Markdown rendering toggle (with localStorage persistence)
  let enableMarkdown: boolean = (() => {
    const saved = localStorage.getItem('enableMarkdown');
    return saved !== null ? saved === 'true' : true; // Default: enabled
  })();

  // Save markdown preference to localStorage when changed
  $: {
    localStorage.setItem('enableMarkdown', enableMarkdown.toString());
  }

  // Phase 7: Context hash tracking for "Updated" status indicators
  let currentContextHash: string = "";  // Current hash from backend (when context is saved)
  let lastSentContextHash: Map<string, string> = new Map();  // Per-conversation: last hash sent to AI
  let peerContextHashes: Map<string, string> = new Map();  // Per-peer: current hash from backend
  let lastSentPeerHashes: Map<string, Map<string, string>> = new Map();  // Per-conversation, per-peer: last hash sent

  // File transfer UI state (Week 1)
  let showFileOfferDialog: boolean = false;
  let currentFileOffer: any = null;
  let fileOfferToastMessage: string = "";
  let showFileOfferToast: boolean = false;

  // Send file confirmation dialog
  let showSendFileDialog: boolean = false;
  let pendingFileSend: { filePath: string, fileName: string, recipientId: string, recipientName: string } | null = null;
  let isSendingFile: boolean = false;  // Prevent double-click bug

  // UI collapse states
  let contextPanelCollapsed: boolean = false;  // Context toggle panel collapsible

  // Notification state
  let windowFocused: boolean = true;
  let showNotificationPermissionDialog: boolean = false;

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

        // Request notification permission on first launch
        const permissionRequested = localStorage.getItem('permissionRequestedAt');
        if (!permissionRequested) {
          // First launch - show friendly dialog after 2s
          setTimeout(() => {
            showNotificationPermissionDialog = true;
          }, 2000);
        }
      } catch (error) {
        console.error('[Notifications] Failed to set up window tracking:', error);
      }
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
  $: setActiveChat(activeChatId);

  // Reactive: Open commit dialog when proposal received
  $: if ($knowledgeCommitProposal) {
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

  // Reactive: Open new session dialog when proposal received (v0.11.3)
  $: if ($newSessionProposal) {
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

  // Reactive: Clear frontend state when new session approved (v0.11.3)
  $: if ($newSessionResult && $newSessionResult.result === "approved") {
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

  // Reactive: Handle token warnings (Phase 2)
  $: if ($tokenWarning) {
    const {conversation_id, tokens_used, token_limit, usage_percent} = $tokenWarning;
    // Update token usage map
    tokenUsageMap = new Map(tokenUsageMap);
    tokenUsageMap.set(conversation_id, {used: tokens_used, limit: token_limit});
    // Show warning toast
    showTokenWarning = true;
    tokenWarningMessage = `Context window ${Math.round(usage_percent * 100)}% full. Consider ending session to save knowledge.`;
  }

  // Reactive: Get current chat's token usage
  $: currentTokenUsage = tokenUsageMap.get(activeChatId) || {used: 0, limit: 0};

  // Reactive: Check if current peer is connected (for enabling/disabling send controls)
  $: isPeerConnected = !activeChatId.startsWith('ai_') && activeChatId !== 'local_ai'
    ? ($nodeStatus?.peer_info?.some((p: any) => p.node_id === activeChatId) ?? false)
    : true; // AI chats don't require peer connection

  // Reactive: Sync chat history from backend when switching to peer chat with no messages (v0.11.2)
  // Handles page refresh scenario: frontend loses chatHistories, backend keeps conversation_monitors
  $: if ($connectionStatus === 'connected' && activeChatId && activeChatId !== 'local_ai' && !activeChatId.startsWith('ai_')) {
    // Check if this peer chat has no messages in frontend
    const currentHistory = $chatHistories.get(activeChatId);
    console.log(`[ChatHistory] Reactive triggered: chatId=${activeChatId.slice(0,20)}, historyLen=${currentHistory?.length || 0}, loading=${loadingHistory.has(activeChatId)}`);

    // Guard: Skip if already loading or already have messages
    if (loadingHistory.has(activeChatId)) {
      console.log(`[ChatHistory] Skipping - already loading history for ${activeChatId.slice(0,20)}`);
    } else if (!currentHistory || currentHistory.length === 0) {
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

            // Scroll to bottom
            setTimeout(() => {
              if (chatWindow) {
                chatWindow.scrollTop = chatWindow.scrollHeight;
              }
            }, 100);
          } else {
            console.log(`[ChatHistory] No messages: status=${result.status}, count=${result.messages?.length || 0}`);
          }
        } catch (e) {
          console.error(`[ChatHistory] Error loading history:`, e);
        } finally {
          // Always remove from loading set when done
          loadingHistory.delete(activeChatId);
        }
      })();
    } else {
      console.log(`[ChatHistory] Skipping load - already have ${currentHistory.length} messages`);
    }
  }

  // Phase 7: Reactive: Check if context window is full (100% or more)
  $: isContextWindowFull = currentTokenUsage.limit > 0 && (currentTokenUsage.used / currentTokenUsage.limit) >= 1.0;

  // Reactive: Handle knowledge extraction failures (Phase 4)
  $: if ($extractionFailure) {
    const {conversation_id, reason} = $extractionFailure;
    showExtractionFailure = true;
    extractionFailureMessage = `Knowledge extraction failed for ${conversation_id}: ${reason}`;
  }

  // Reactive: Handle knowledge commit voting results
  $: if ($knowledgeCommitResult) {
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

  // Phase 7: Handle personal context updates (for "Updated" status indicator)
  $: if ($contextUpdated) {
    const { context_hash } = $contextUpdated;
    if (context_hash) {
      currentContextHash = context_hash;
      console.log(`[Context Updated] New hash: ${context_hash.slice(0, 8)}...`);
    }
  }

  // Phase 7: Handle peer context updates (for "Updated" status indicators)
  $: if ($peerContextUpdated) {
    const { node_id, context_hash } = $peerContextUpdated;
    if (node_id && context_hash) {
      peerContextHashes = new Map(peerContextHashes);
      peerContextHashes.set(node_id, context_hash);
      console.log(`[Peer Context Updated] ${node_id.slice(0, 15)}... - hash: ${context_hash.slice(0, 8)}...`);
    }
  }

  // File transfer event handlers (Week 1)
  $: if ($fileTransferOffer) {
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

  $: if ($fileTransferComplete) {
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

  $: if ($fileTransferCancelled) {
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

  // Reactive: Handle chat history restored (v0.11.2)
  $: if ($historyRestored) {
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

  // Phase 7: Reactive: Check if local context has changed (not yet sent to AI)
  $: localContextUpdated = currentContextHash && lastSentContextHash.get(activeChatId) !== currentContextHash;

  // Phase 7: Reactive: Check which peer contexts have changed (not yet sent to AI)
  $: peerContextsUpdated = new Set(
    Array.from(peerContextHashes.keys()).filter(nodeId => {
      const conversationPeerHashes = lastSentPeerHashes.get(activeChatId);
      if (!conversationPeerHashes) return true;  // Never sent
      return conversationPeerHashes.get(nodeId) !== peerContextHashes.get(nodeId);
    })
  );

  // Reactive: Reset compute host if selected peer disconnects
  $: if (selectedComputeHost !== "local" && $nodeStatus?.p2p_peers) {
    const isStillConnected = $nodeStatus.p2p_peers.includes(selectedComputeHost);
    if (!isStillConnected) {
      console.log(`Compute host ${selectedComputeHost} disconnected, resetting to local`);
      selectedComputeHost = "local";
      selectedRemoteModel = "";
    }
  }

  // Reactive: Reset selected peer contexts if peers disconnect
  $: if (selectedPeerContexts.size > 0 && $nodeStatus?.p2p_peers) {
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
      selectedPeerContexts = selectedPeerContexts;
    }
  }

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
  $: peersByStrategy = getPeersByStrategy($nodeStatus?.peer_info);

  function isNearBottom(element: HTMLElement, threshold: number = 150): boolean {
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
  $: peerDisplayNames = (() => {
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
  })();

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
    // Trigger reactivity
    selectedPeerContexts = selectedPeerContexts;
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
  function handleSendMessage() {
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

      // Prepare AI query payload with optional compute host and provider/model
      const payload: any = {
        prompt: text,
        include_context: includePersonalContext,  // Add context inclusion flag
        conversation_id: activeChatId,  // Phase 7: Pass conversation ID for history tracking
        ai_scope: selectedAIScope || null  // AI Scope for filtering (null = no filtering)
      };

      // Add peer contexts if any are selected
      if (selectedPeerContexts.size > 0) {
        payload.context_ids = Array.from(selectedPeerContexts);
      }

      if (selectedComputeHost !== "local") {
        // Remote inference - send compute_host and model
        payload.compute_host = selectedComputeHost;
        if (selectedRemoteModel) {
          payload.model = selectedRemoteModel;
        }
      } else {
        // Local inference - send provider if one is selected
        const selectedProvider = $chatProviders.get(activeChatId);
        if (selectedProvider) {
          payload.provider = selectedProvider;
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
  function handleConnectPeer() {
    if (!peerInput.trim()) return;

    const input = peerInput.trim();
    console.log("Connecting to peer:", input);

    // Detect if input is a dpc:// URI (Direct TLS) or just a node_id (DHT-first)
    if (input.startsWith('dpc://')) {
      // Direct TLS connection (manual IP/port)
      console.log("Using Direct TLS connection");
      sendCommand("connect_to_peer", { uri: input });
    } else {
      // DHT-first connection (automatic discovery)
      // Tries: DHT lookup → Peer cache → Hub WebRTC
      console.log("Using DHT-first discovery strategy");
      sendCommand("connect_via_dht", { node_id: input });
    }

    peerInput = "";
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
  $: if ($connectionStatus === "connected" && !aiScopesLoaded) {
    loadAIScopes();
  }

  // Reset AI scopes loaded flag on disconnection (to reload on reconnect)
  $: if ($connectionStatus === "disconnected" || $connectionStatus === "error") {
    aiScopesLoaded = false;
  }

  // Reload AI scopes when firewall rules are updated (via FirewallEditor)
  // IMPORTANT: This reactive statement ensures UI updates immediately after saving firewall rules.
  // If you add more UI components that read from privacy_rules.json, add similar reactive
  // statements here to reload their data when $firewallRulesUpdated changes.
  // Example: if ($firewallRulesUpdated && $connectionStatus === "connected") { loadNodeGroups(); }
  $: if ($firewallRulesUpdated && $connectionStatus === "connected") {
    aiScopesLoaded = false;
    loadAIScopes();
  }

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

    // Set default selection and show dialog
    selectedProviderForNewChat = $availableProviders.default_provider;
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
      newMap.set(chatId, { name: chatName, provider: selectedProviderForNewChat });
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

  // --- HANDLE INCOMING MESSAGES ---
  $: if ($coreMessages?.id) {
    const message = $coreMessages;

    if (message.command === "execute_ai_query") {
      isLoading = false;
      const newText = message.status === "OK"
        ? message.payload.content
        : `Error: ${message.payload?.message || 'Unknown error'}`;
      const newSender = message.status === "OK" ? 'ai' : 'system';
      const modelName = message.status === "OK" ? message.payload.model : undefined;

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
  
  $: if ($p2pMessages) {
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

      // Send notification if message is from a different chat and not from user
      if (msg.sender_node_id !== "user" && msg.sender_node_id !== activeChatId) {
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
  
  $: activeMessages = $chatHistories.get(activeChatId) || [];
</script>

<main class="container">
  <!-- Status Bar -->
  <div class="status-bar">
    {#if $connectionStatus === 'connected'}
      <span class="status-connected">Backend status: connected</span>
    {:else if $connectionStatus === 'connecting'}
      <span class="status-connecting">Backend status: connecting...</span>
    {:else if $connectionStatus === 'error'}
      <span class="status-error">Backend status: error</span>
      <button class="btn-small" on:click={handleReconnect}>Retry</button>
    {:else}
      <span class="status-disconnected">Backend status: disconnected</span>
      <button class="btn-small" on:click={handleReconnect}>Connect</button>
    {/if}
  </div>

  <div class="grid">
    <!-- Sidebar -->
    <div class="sidebar">
      {#if $connectionStatus === 'connected' && $nodeStatus}
        <!-- Node Info -->
        <div class="node-info">
          <p><strong>Your Node ID:</strong></p>
          <div class="node-id-container">
            <code class="node-id">{$nodeStatus.node_id}</code>
            <button
              class="copy-btn"
              on:click={() => {
                navigator.clipboard.writeText($nodeStatus.node_id);
                alert('Node ID copied!');
              }}
              title="Copy Node ID"
            >
              📋
            </button>
          </div>

          <!-- Direct TLS Connection URIs (Local Network) -->
          {#if $nodeStatus.dpc_uris && $nodeStatus.dpc_uris.length > 0}
            <div class="dpc-uris-section">
              <details class="uri-details">
                <summary class="uri-summary">
                  <span class="uri-icon">🔗</span>
                  <span class="uri-title">Local Network ({$nodeStatus.dpc_uris.length})</span>
                </summary>
                <div class="uri-help-text">
                  Share with peers on your local network
                </div>
                {#each $nodeStatus.dpc_uris as { ip, uri }}
                  <div class="uri-card">
                    <div class="uri-card-header">
                      <span class="ip-badge">{ip}</span>
                      <button
                        class="copy-btn-icon"
                        on:click={() => {
                          navigator.clipboard.writeText(uri);
                          alert('✓ URI copied!');
                        }}
                        title="Copy URI"
                      >
                        📋
                      </button>
                    </div>
                    <details class="uri-full-details">
                      <summary class="show-uri-btn">Full URI ▼</summary>
                      <code class="uri-full-text">{uri}</code>
                    </details>
                  </div>
                {/each}
              </details>
            </div>
          {/if}

          <!-- External URIs (From STUN Servers) -->
          {#if $nodeStatus.external_uris && $nodeStatus.external_uris.length > 0}
            <div class="dpc-uris-section">
              <details class="uri-details">
                <summary class="uri-summary">
                  <span class="uri-icon">🌐</span>
                  <span class="uri-title">External (Internet) ({$nodeStatus.external_uris.length})</span>
                </summary>
                <div class="uri-help-text">
                  Your public IP address(es) discovered via STUN servers
                </div>
                {#each $nodeStatus.external_uris as { ip, uri }}
                  <div class="uri-card">
                    <div class="uri-card-header">
                      <span class="ip-badge external">{ip}</span>
                      <button
                        class="copy-btn-icon"
                        on:click={() => {
                          navigator.clipboard.writeText(uri);
                          alert('✓ External URI copied!');
                        }}
                        title="Copy External URI"
                      >
                        📋
                      </button>
                    </div>
                    <details class="uri-full-details">
                      <summary class="show-uri-btn">Full URI ▼</summary>
                      <code class="uri-full-text">{uri}</code>
                    </details>
                  </div>
                {/each}
              </details>
            </div>
          {/if}

          <!-- Connection Status (NEW) -->
          {#if $nodeStatus.operation_mode}
            <div class="connection-mode">
              <p><strong>Mode:</strong></p>
              <div class="mode-badge" class:fully-online={$nodeStatus.operation_mode === 'fully_online'}
                   class:hub-offline={$nodeStatus.operation_mode === 'hub_offline'}
                   class:fully-offline={$nodeStatus.operation_mode === 'fully_offline'}>
                {#if $nodeStatus.operation_mode === 'fully_online'}
                  🟢 Online
                {:else if $nodeStatus.operation_mode === 'hub_offline'}
                  🟡 Hub Offline
                {:else}
                  🔴 Offline
                {/if}
              </div>
              <p class="mode-description">{$nodeStatus.connection_status || 'All features available'}</p>

              {#if $nodeStatus.available_features}
                <details class="features-details">
                  <summary>Available Features</summary>
                  <ul class="features-list">
                    {#each Object.entries($nodeStatus.available_features) as [feature, available]}
                      {@const peerCount = peersByStrategy[feature]?.length || 0}
                      {@const tooltip = peersByStrategy[feature]
                        ? peersByStrategy[feature].map(formatPeerForTooltip).join(', ')
                        : ''}
                      <li
                        class:feature-available={available}
                        class:feature-unavailable={!available}
                        title={peerCount > 0 ? tooltip : ''}
                      >
                        {available ? '✓' : '✗'} {feature.replace(/_/g, ' ')}
                        {#if peerCount > 0}
                          <span class="peer-count">({peerCount})</span>
                        {/if}
                      </li>
                    {/each}
                  </ul>
                  {#if $nodeStatus.cached_peers_count > 0}
                    <p class="cached-info">💾 {$nodeStatus.cached_peers_count} cached peer(s)</p>
                  {/if}
                </details>
              {/if}
            </div>
          {/if}

          <!-- Hub Login -->
          {#if $nodeStatus.hub_status !== 'Connected'}
            <div class="hub-login-section">
              <p class="info-text">Connect to Hub for WebRTC and discovery</p>
              <div class="hub-login-buttons">
                <button
                  on:click={() => sendCommand('login_to_hub', {provider: 'google'})}
                  class="btn-oauth btn-google"
                  title="Login with Google"
                >
                  <span class="oauth-icon">🔵</span>
                  Google
                </button>
                <button
                  on:click={() => sendCommand('login_to_hub', {provider: 'github'})}
                  class="btn-oauth btn-github"
                  title="Login with GitHub"
                >
                  <span class="oauth-icon">⚫</span>
                  GitHub
                </button>
              </div>
            </div>
          {/if}
        </div>

        <!-- Personal Context Button (Knowledge Architecture) -->
        <div class="context-section">
          <button class="btn-context" on:click={loadPersonalContext}>
            View Personal Context
          </button>

          <button class="btn-context" on:click={openInstructionsEditor}>
            AI Instructions
          </button>

          <button class="btn-context" on:click={openFirewallEditor}>
            Firewall and Privacy Rules
          </button>

          <button class="btn-context" on:click={openProvidersEditor}>
            AI Providers
          </button>

          <!-- Auto Knowledge Detection Toggle -->
          <div class="knowledge-toggle">
            <label class="toggle-container">
              <input
                id="auto-knowledge-detection"
                name="auto-knowledge-detection"
                type="checkbox"
                bind:checked={autoKnowledgeDetection}
                on:change={toggleAutoKnowledgeDetection}
              />
              <span class="toggle-slider"></span>
              <span class="toggle-label">
                Auto-detect knowledge in conversations
              </span>
            </label>
            <p class="toggle-hint">
              {autoKnowledgeDetection
                ? "✓ AI is monitoring conversations for knowledge"
                : "✗ Manual knowledge extraction only"}
            </p>
          </div>
        </div>

        <!-- Connect to Peer -->
        <div class="connect-section">
          <h3>Connect to Peer</h3>
          <input
            id="peer-input"
            name="peer-input"
            type="text"
            bind:value={peerInput}
            placeholder="node_id or dpc://IP:port?node_id=..."
            on:keydown={(e) => e.key === 'Enter' && handleConnectPeer()}
          />
          <button on:click={handleConnectPeer}>Connect</button>

          <!-- Connection Methods Help (Collapsible) -->
          <details class="connection-methods-details">
            <summary class="connection-methods-summary">
              <span class="uri-icon">ℹ️</span>
              <span class="uri-title">Connection Methods</span>
            </summary>
            <div class="connection-help-content">
              <p class="small">
                🔍 <strong>Auto-Discovery (DHT):</strong> <code>dpc-node-abc123...</code><br/>
                <span class="small-detail">Tries: DHT → Cache → Hub</span>
              </p>
              <p class="small">
                🏠 <strong>Direct TLS (Local):</strong> <code>dpc://192.168.1.100:8888?node_id=...</code>
              </p>
              <p class="small">
                🌍 <strong>Direct TLS (External):</strong> <code>dpc://203.0.113.5:8888?node_id=...</code>
              </p>
            </div>
          </details>
        </div>

        <!-- Chat List -->
        <div class="chat-list">
          <div class="chat-list-header">
            <h3>Chats</h3>
            <button
              class="btn-add-chat"
              on:click={handleAddAIChat}
              title="Add a new AI chat with a different provider"
            >
              + AI
            </button>
          </div>
          <ul>
            <!-- AI Chats -->
            {#each Array.from($aiChats.entries()) as [chatId, chatInfo] (chatId)}
              <li class="peer-item">
                <button
                  type="button"
                  class="chat-button"
                  class:active={activeChatId === chatId}
                  on:click={() => activeChatId = chatId}
                  title={chatInfo.provider ? `Provider: ${chatInfo.provider}` : 'Default AI Assistant'}
                >
                  {chatInfo.name}
                </button>
                {#if chatId !== 'local_ai'}
                  <button
                    type="button"
                    class="disconnect-btn"
                    on:click|stopPropagation={() => handleDeleteAIChat(chatId)}
                    title="Delete AI chat"
                  >
                    ×
                  </button>
                {/if}
              </li>
            {/each}

            <!-- P2P Peer Chats -->
            {#if $nodeStatus.p2p_peers && $nodeStatus.p2p_peers.length > 0}
              {#each $nodeStatus.p2p_peers as peerId (`${peerId}-${peerDisplayNames.get(peerId)}`)}
                <li class="peer-item">
                  <button
                    type="button"
                    class="chat-button"
                    class:active={activeChatId === peerId}
                    on:click={() => {
                      activeChatId = peerId;
                      resetUnreadCount(peerId);
                    }}
                    title={peerId}
                  >
                    <span class="peer-name">👤 {getPeerDisplayName(peerId)}</span>
                    {#if ($unreadMessageCounts.get(peerId) ?? 0) > 0}
                      <span class="unread-badge">{$unreadMessageCounts.get(peerId)}</span>
                    {/if}
                  </button>
                  <button
                    type="button"
                    class="disconnect-btn"
                    on:click|stopPropagation={() => handleDisconnectPeer(peerId)}
                    title="Disconnect from peer"
                  >
                    ×
                  </button>
                </li>
              {/each}
            {:else}
              <li class="no-peers">No connected peers</li>
            {/if}
          </ul>
        </div>
      {:else if $connectionStatus === 'connecting'}
        <div class="connecting">
          <p>🔄 Connecting...</p>
        </div>
      {:else}
        <div class="error">
          <p>⚠️ Not connected to Core Service</p>
          <p class="small">Please ensure the backend is running</p>
        </div>
      {/if}
    </div>

    <!-- Chat Panel -->
    <div class="chat-panel" style="height: {chatPanelHeight}px;">
      <div class="chat-header">
        <div class="chat-title-section">
          <h2>
            {#if $aiChats.has(activeChatId)}
              {$aiChats.get(activeChatId)?.name || 'AI Assistant'}
            {:else}
              Chat with {getPeerDisplayName(activeChatId)}
            {/if}
          </h2>

          {#if $aiChats.has(activeChatId) && $availableProviders && $availableProviders.providers && $availableProviders.providers.length >= 1}
            <div class="provider-selector">
              <label for="provider-select">Provider:</label>
              <select
                id="provider-select"
                value={$chatProviders.get(activeChatId) || $availableProviders.default_provider}
                on:change={(e) => {
                  chatProviders.update(map => {
                    const newMap = new Map(map);
                    newMap.set(activeChatId, e.currentTarget.value);
                    return newMap;
                  });
                }}
                disabled={$availableProviders.providers.length === 1}
              >
                {#each $availableProviders.providers as provider}
                  <option value={provider.alias}>
                    {provider.alias} ({provider.model})
                  </option>
                {/each}
              </select>
            </div>
          {/if}
        </div>

        {#if $aiChats.has(activeChatId) && currentTokenUsage.limit > 0}
          <div class="token-counter">
            <span class="token-value">
              {currentTokenUsage.used.toLocaleString()} / {currentTokenUsage.limit.toLocaleString()} tokens
            </span>
            <span class="token-percentage" class:warning={currentTokenUsage.used / currentTokenUsage.limit >= 0.8}>
              ({Math.round((currentTokenUsage.used / currentTokenUsage.limit) * 100)}%)
            </span>
          </div>
        {/if}

        <div class="chat-actions">
          <button class="btn-new-chat" on:click={() => handleNewChat(activeChatId)}>
            New Session
          </button>
          <button
            class="btn-end-session"
            on:click={() => handleEndSession(activeChatId)}
            disabled={!isPeerConnected && activeChatId !== 'local_ai' && !activeChatId.startsWith('ai_')}
            title={!isPeerConnected && activeChatId !== 'local_ai' && !activeChatId.startsWith('ai_') ? "Peer must be online to save knowledge (requires voting)" : "Extract and save knowledge from this conversation"}
          >
            End Session & Save Knowledge
          </button>
          {#if $aiChats.has(activeChatId)}
            <button
              class="btn-markdown-toggle"
              class:active={enableMarkdown}
              on:click={() => enableMarkdown = !enableMarkdown}
              title={enableMarkdown ? 'Disable markdown rendering' : 'Enable markdown rendering'}
            >
              {enableMarkdown ? 'Markdown' : 'Text'}
            </button>
          {/if}
        </div>
      </div>

      <div class="chat-window" bind:this={chatWindow}>
        {#if activeMessages.length > 0}
          {#each activeMessages as msg (msg.id)}
            <div class="message" class:user={msg.sender === 'user'} class:system={msg.sender === 'system'}>
              <div class="message-header">
                <strong>
                  {#if msg.sender === 'user'}
                    You
                  {:else if msg.sender === 'ai'}
                    {msg.model ? `AI (${msg.model})` : 'AI Assistant'}
                  {:else}
                    {msg.senderName ? `${msg.senderName} | ${msg.sender.slice(0, 20)}...` : msg.sender}
                  {/if}
                </strong>
                <span class="timestamp">{new Date(msg.timestamp).toLocaleTimeString()}</span>
              </div>
              <!-- Only show text if no attachments (avoid duplication) -->
              {#if !msg.attachments || msg.attachments.length === 0}
                {#if msg.sender === 'ai' && enableMarkdown}
                  <MarkdownMessage content={msg.text} />
                {:else}
                  <p>{msg.text}</p>
                {/if}
              {/if}

              <!-- File attachments (Week 1) -->
              {#if msg.attachments && msg.attachments.length > 0}
                <div class="message-attachments">
                  {#each msg.attachments as attachment}
                    <div class="file-attachment">
                      <div class="file-details">
                        <div class="file-name">{attachment.filename}</div>
                        <div class="file-meta">
                          {attachment.size_mb ? `${attachment.size_mb} MB` : `${(attachment.size_bytes / (1024 * 1024)).toFixed(2)} MB`}
                          {#if attachment.mime_type}
                            • {attachment.mime_type}
                          {/if}
                          {#if attachment.status}
                            • {attachment.status}
                          {/if}
                        </div>
                      </div>
                    </div>
                  {/each}
                </div>
              {/if}
            </div>
          {/each}
        {:else}
          <div class="empty-chat">
            <p>No messages yet. Start the conversation!</p>
          </div>
        {/if}
      </div>

      <div class="chat-input">
        {#if $aiChats.has(activeChatId)}
          <!-- Personal Context Toggle -->
          <div class="context-toggle">
            <button
              type="button"
              class="context-toggle-header"
              on:click={() => contextPanelCollapsed = !contextPanelCollapsed}
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
                      on:change={() => togglePeerContext(peer.node_id)}
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

        {#if activeChatId === 'local_ai'}
          <div class="compute-host-selector">
            <label for="compute-host">🖥️ Compute Host:</label>
            <select id="compute-host" bind:value={selectedComputeHost} on:change={() => {
              // Reset selected model when switching compute hosts
              selectedRemoteModel = "";
              // Auto-select first available model if switching to remote
              if (selectedComputeHost !== "local") {
                const providers = $peerProviders.get(selectedComputeHost);
                if (providers && providers.length > 0) {
                  selectedRemoteModel = providers[0].model;
                }
              }
            }}>
              <option value="local">Local (this device)</option>
              {#if $nodeStatus?.peer_info && $nodeStatus.peer_info.length > 0}
                <optgroup label="Remote Peers">
                  {#each $nodeStatus.peer_info as peer}
                    {@const displayName = peer.name
                      ? `${peer.name} | ${peer.node_id.slice(0, 20)}...`
                      : `${peer.node_id.slice(0, 20)}...`}
                    <option value={peer.node_id}>
                      {displayName}
                    </option>
                  {/each}
                </optgroup>
              {/if}
            </select>

            <!-- Model selector for remote compute host -->
            {#if selectedComputeHost !== "local"}
              {@const providers = $peerProviders.get(selectedComputeHost)}
              {#if providers && providers.length > 0}
                <label for="remote-model">Model:</label>
                <select id="remote-model" bind:value={selectedRemoteModel}>
                  {#each providers as provider}
                    <option value={provider.model}>
                      {provider.alias} ({provider.model})
                    </option>
                  {/each}
                </select>
              {/if}
            {/if}
          </div>
        {/if}
        <div class="input-row">
          <textarea
            id="message-input"
            name="message-input"
            bind:value={currentInput}
            placeholder={
              isContextWindowFull ? 'Context window full - End session to continue' :
              ($connectionStatus === 'connected' ? 'Type a message... (Enter to send, Shift+Enter for new line)' : 'Connect to Core Service first...')
            }
            disabled={$connectionStatus !== 'connected' || isLoading || isContextWindowFull}
            on:keydown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                // Only send if peer is connected (or local AI chat)
                if (isPeerConnected || activeChatId === 'local_ai' || activeChatId.startsWith('ai_')) {
                  handleSendMessage();
                }
              }
            }}
          ></textarea>
          <button
            class="file-button"
            on:click={handleSendFile}
            disabled={$connectionStatus !== 'connected' || isLoading || activeChatId === 'local_ai' || activeChatId.startsWith('ai_') || !isPeerConnected}
            title={isPeerConnected ? "Send file (P2P chat only)" : "Peer disconnected"}
          >
            📎
          </button>
          <button
            on:click={handleSendMessage}
            disabled={$connectionStatus !== 'connected' || isLoading || !currentInput.trim() || isContextWindowFull || !isPeerConnected}
            title={!isPeerConnected && activeChatId !== 'local_ai' && !activeChatId.startsWith('ai_') ? "Peer disconnected" : ""}
          >
            {#if isLoading}Sending...{:else}Send{/if}
          </button>
        </div>
      </div>

      <!-- Resize Handle -->
      <!-- svelte-ignore a11y-no-noninteractive-element-interactions -->
      <div
        class="resize-handle"
        class:resizing={isResizing}
        on:mousedown={startResize}
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
    on:click={() => showNotificationPermissionDialog = false}
    on:keydown={(e) => {
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
      on:click|stopPropagation
      on:keydown|stopPropagation
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
          on:click={async () => {
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
          on:click={() => {
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

<!-- Vote Result Details Dialog -->
<VoteResultDialog
  result={currentVoteResult}
  open={showVoteResultDialog}
  on:close={() => {
    showVoteResultDialog = false;
  }}
/>

<!-- File Transfer UI Components (Week 1) -->

<!-- File Offer Dialog -->
{#if showFileOfferDialog && currentFileOffer}
  <div class="modal-overlay" role="presentation" on:click={handleRejectFile} on:keydown={(e) => e.key === 'Escape' && handleRejectFile()}>
    <div class="modal-dialog" role="dialog" aria-modal="true" tabindex="-1" on:click|stopPropagation on:keydown|stopPropagation>
      <h3>Incoming File</h3>
      <p><strong>File:</strong> {currentFileOffer.filename}</p>
      <p><strong>Size:</strong> {(currentFileOffer.size_bytes / 1024 / 1024).toFixed(2)} MB</p>
      <p><strong>From:</strong> {currentFileOffer.node_id.slice(0, 20)}...</p>
      <div class="modal-buttons">
        <button class="accept-button" on:click={handleAcceptFile}>Accept</button>
        <button class="reject-button" on:click={handleRejectFile}>Reject</button>
      </div>
    </div>
  </div>
{/if}

<!-- Send File Confirmation Dialog -->
{#if showSendFileDialog && pendingFileSend}
  <div class="modal-overlay" role="presentation" on:click={handleCancelSendFile} on:keydown={(e) => e.key === 'Escape' && handleCancelSendFile()}>
    <div class="modal-dialog" role="dialog" aria-modal="true" tabindex="-1" on:click|stopPropagation on:keydown|stopPropagation>
      <h3>Send File</h3>
      <p><strong>File:</strong> {pendingFileSend.fileName}</p>
      <p><strong>To:</strong> {pendingFileSend.recipientName}</p>

      {#if $filePreparationStarted && isSendingFile}
        <p style="margin-top: 10px; font-size: 13px;">
          <strong>Size:</strong> {$filePreparationStarted.size_mb} MB
        </p>
      {/if}

      {#if $filePreparationProgress && isSendingFile}
        <div style="margin-top: 15px;">
          <p style="font-size: 13px; margin-bottom: 5px; color: #555;">
            {#if $filePreparationProgress.phase === 'hashing_file'}
              Computing file hash: {$filePreparationProgress.percent}%
            {:else if $filePreparationProgress.phase === 'computing_chunks'}
              Computing chunk hashes: {$filePreparationProgress.percent}%
            {:else}
              Preparing file: {$filePreparationProgress.percent}%
            {/if}
          </p>
          <div style="width: 100%; background-color: #e0e0e0; border-radius: 4px; height: 8px; overflow: hidden;">
            <div style="width: {$filePreparationProgress.percent}%; background-color: #4CAF50; height: 100%; transition: width 0.3s ease;"></div>
          </div>
        </div>
      {/if}

      <div class="modal-buttons">
        <button class="accept-button" on:click={handleConfirmSendFile} disabled={isSendingFile}>
          {#if $filePreparationCompleted && isSendingFile}
            Sending...
          {:else if isSendingFile}
            Preparing...
          {:else}
            Send
          {/if}
        </button>
        <button class="reject-button" on:click={handleCancelSendFile} disabled={isSendingFile}>Cancel</button>
      </div>
    </div>
  </div>
{/if}

<!-- File Transfer Toast -->
{#if showFileOfferToast}
  <Toast
    message={fileOfferToastMessage}
    type="info"
    duration={5000}
    dismissible={true}
    onDismiss={() => showFileOfferToast = false}
  />
{/if}

<!-- Active File Transfers Progress -->
{#if $activeFileTransfers.size > 0}
  <div class="active-transfers-panel">
    <h4>Active Transfers</h4>
    {#each Array.from($activeFileTransfers.values()) as transfer}
      <div class="transfer-item">
        <div class="transfer-info">
          <span class="transfer-filename">{transfer.filename}</span>
          <span class="transfer-status">{transfer.direction === 'upload' ? '↑' : '↓'} {transfer.status}</span>
          <button
            class="cancel-transfer-button"
            on:click={() => handleCancelTransfer(transfer.transfer_id, transfer.filename)}
            title="Cancel transfer"
            aria-label="Cancel transfer"
          >
            ×
          </button>
        </div>
        {#if transfer.progress !== undefined}
          <div class="progress-bar">
            <div class="progress-fill" style="width: {transfer.progress}%"></div>
          </div>
          <span class="progress-text">{transfer.progress}%</span>
        {/if}
      </div>
    {/each}
  </div>
{/if}

<!-- Add AI Chat Dialog -->
{#if showAddAIChatDialog}
  <!-- svelte-ignore a11y-click-events-have-key-events -->
  <!-- svelte-ignore a11y-no-static-element-interactions -->
  <div
    class="modal-overlay"
    role="presentation"
    on:click={cancelAddAIChat}
    on:keydown={(e) => e.key === 'Escape' && cancelAddAIChat()}
  >
    <!-- svelte-ignore a11y-click-events-have-key-events -->
    <!-- svelte-ignore a11y-no-static-element-interactions -->
    <div
      class="modal-content"
      role="dialog"
      aria-labelledby="modal-title"
      aria-modal="true"
      tabindex="-1"
      on:click|stopPropagation
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

      <div class="dialog-actions">
        <button class="btn-cancel" on:click={cancelAddAIChat}>Cancel</button>
        <button class="btn-confirm" on:click={confirmAddAIChat}>Create Chat</button>
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

  .status-bar {
    margin-bottom: 1.5rem;
    padding: 1rem;
    border: 1px solid #ccc;
    border-radius: 8px;
    background: #f9f9f9;
    text-align: center;
    font-size: 1.1rem;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 1rem;
  }
  
  .status-connected { color: #28a745; font-weight: bold; }
  .status-disconnected, .status-error { color: #dc3545; font-weight: bold; }
  .status-connecting { color: #ffc107; font-weight: bold; }
  
  .btn-small {
    padding: 0.4rem 0.8rem;
    font-size: 0.9rem;
    border: none;
    border-radius: 6px;
    background: #007bff;
    color: white;
    cursor: pointer;
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
  
  .sidebar {
    display: flex;
    flex-direction: column;
    gap: 1rem;
    overflow-y: auto;
    max-height: calc(100vh - 8rem);
    padding-right: 0.5rem;
  }

  .sidebar::-webkit-scrollbar {
    width: 6px;
  }

  .sidebar::-webkit-scrollbar-track {
    background: #f1f1f1;
    border-radius: 3px;
  }

  .sidebar::-webkit-scrollbar-thumb {
    background: #888;
    border-radius: 3px;
  }

  .sidebar::-webkit-scrollbar-thumb:hover {
    background: #555;
  }
  
  .node-info, .connect-section, .context-section, .chat-list {
    background: white;
    border: 1px solid #e0e0e0;
    border-radius: 8px;
    padding: 1rem;
  }

  /* Note: .node-info no longer has max-height restriction to prevent
     unwanted scrollbars on macOS/Ubuntu. Content naturally fits in sidebar. */

  h2, h3 {
    margin: 0 0 0.75rem 0;
    font-size: 1.1rem;
    border-bottom: 1px solid #eee;
    padding-bottom: 0.5rem;
  }

  /* Connection Methods Collapsible Section */
  .connection-methods-details {
    margin-top: 0.75rem;
    background: #ffffff;
    border: 1px solid #e0e0e0;
    border-radius: 8px;
    padding: 0;
    overflow: hidden;
  }

  .connection-methods-details[open] {
    border-color: #007bff;
  }

  .connection-methods-summary {
    padding: 0.75rem 1rem;
    background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%);
    cursor: pointer;
    list-style: none;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-weight: 600;
    color: #333;
    transition: background 0.2s;
  }

  .connection-methods-summary:hover {
    background: linear-gradient(135deg, #e9ecef 0%, #dee2e6 100%);
  }

  .connection-methods-summary::-webkit-details-marker {
    display: none;
  }

  .connection-help-content {
    padding: 1rem;
    background: #ffffff;
  }

  .connection-help-content p {
    margin: 0.5rem 0;
  }

  .node-id {
    font-family: monospace;
    font-size: 0.85rem;
    color: #555;
    word-break: break-all;
    margin: 0;
  }
  
  .node-id-container {
    display: flex;
    align-items: flex-start;
    gap: 0.5rem;
  }
  
  .copy-btn {
    width: auto;
    min-width: auto;
    padding: 0.3rem 0.5rem;
    font-size: 1rem;
    background: transparent;
    border: 1px solid #ddd;
    cursor: pointer;
    border-radius: 4px;
    transition: all 0.2s;
  }
  
  .copy-btn:hover {
    background: #f0f0f0;
    border-color: #007bff;
  }

  .info-text, .small {
    font-size: 0.9rem;
    color: #666;
    margin: 0.5rem 0;
    font-style: italic;
  }
  
  input, textarea {
    width: 100%;
    padding: 0.75rem;
    border: 1px solid #ccc;
    border-radius: 6px;
    font-size: 1rem;
    font-family: inherit;
    box-sizing: border-box;
  }
  
  input {
    margin-bottom: 0.5rem;
  }
  
  button {
    width: 100%;
    padding: 0.75rem;
    border: none;
    border-radius: 6px;
    background: #007bff;
    color: white;
    font-size: 1rem;
    cursor: pointer;
    transition: background 0.2s;
  }
  
  button:hover:not(:disabled) {
    background: #0056b3;
  }
  
  button:disabled {
    background: #a0a0a0;
    cursor: not-allowed;
  }

  /* OAuth login buttons */
  .hub-login-buttons {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
  }

  .btn-oauth {
    flex: 1;
    min-width: 120px;
    padding: 0.75rem 1rem;
    border: none;
    border-radius: 4px;
    font-size: 0.9rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.5rem;
  }

  .btn-oauth:hover {
    transform: translateY(-1px);
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.15);
  }

  .btn-oauth:active {
    transform: translateY(0);
  }

  .btn-google {
    background: #4285f4;
    color: white;
  }

  .btn-google:hover {
    background: #357ae8;
  }

  .btn-github {
    background: #24292e;
    color: white;
  }

  .btn-github:hover {
    background: #1b1f23;
  }

  .oauth-icon {
    font-size: 1.1rem;
    line-height: 1;
  }

  /* Knowledge Architecture - Context Button */
  .btn-context {
    width: 100%;
    padding: 0.75rem 1rem;
    margin-bottom: 0.5rem;
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    color: white;
    border: none;
    border-radius: 6px;
    font-size: 0.95rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.5rem;
  }

  .btn-context:hover {
    transform: translateY(-1px);
    box-shadow: 0 4px 12px rgba(102, 126, 234, 0.4);
  }

  .btn-context:active {
    transform: translateY(0);
  }

  /* Knowledge Architecture - Auto-Detection Toggle */
  .knowledge-toggle {
    margin-top: 1rem;
    padding-top: 1rem;
    border-top: 1px solid #e0e0e0;
  }

  .toggle-container {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    cursor: pointer;
    user-select: none;
  }

  .toggle-container input[type="checkbox"] {
    position: absolute;
    opacity: 0;
    width: 0;
    height: 0;
  }

  .toggle-slider {
    position: relative;
    width: 44px;
    height: 24px;
    background: #ccc;
    border-radius: 24px;
    transition: background 0.3s;
    flex-shrink: 0;
  }

  .toggle-slider::before {
    content: '';
    position: absolute;
    width: 18px;
    height: 18px;
    left: 3px;
    top: 3px;
    background: white;
    border-radius: 50%;
    transition: transform 0.3s;
  }

  .toggle-container input[type="checkbox"]:checked + .toggle-slider {
    background: #667eea;
  }

  .toggle-container input[type="checkbox"]:checked + .toggle-slider::before {
    transform: translateX(20px);
  }

  .toggle-label {
    font-size: 0.9rem;
    color: #333;
    line-height: 1.4;
  }

  .toggle-hint {
    font-size: 0.8rem;
    color: #666;
    margin: 0.5rem 0 0 0;
    padding-left: 3.5rem;
    line-height: 1.3;
  }

  .chat-list-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.5rem;
  }

  .chat-list-header h3 {
    margin: 0;
    padding: 0;
    border: none;
  }

  .btn-add-chat {
    padding: 0.3rem 0.6rem;
    background: linear-gradient(135deg, #4CAF50 0%, #45a049 100%);
    color: white;
    border: none;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.75rem;
    font-weight: 600;
    transition: all 0.2s;
    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
    white-space: nowrap;
    flex-shrink: 0;
    width: fit-content;
    min-width: auto;
  }

  .btn-add-chat:hover {
    background: linear-gradient(135deg, #45a049 0%, #4CAF50 100%);
    box-shadow: 0 4px 8px rgba(0, 0, 0, 0.15);
    transform: translateY(-1px);
  }

  .btn-add-chat:active {
    transform: translateY(0);
    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
  }

  .chat-list ul {
    list-style: none;
    padding: 0;
    margin: 0;
  }

  .chat-list li {
    margin-bottom: 0.5rem;
  }
  
  .peer-item {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  
  .chat-button {
    display: flex;
    align-items: center;
    text-align: left;
    background: transparent;
    color: #333;
    border: 1px solid transparent;
    padding: 0.6rem;
    transition: all 0.2s;
    flex: 1;
    position: relative;
  }

  .chat-button:hover {
    background: #f0f0f0;
  }

  .chat-button.active {
    background: #e0e7ff;
    border-color: #c7d2fe;
    font-weight: bold;
  }

  /* Peer name wrapper (v0.9.3) - handles overflow so badge stays visible */
  .peer-name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1;
  }

  /* Unread message badge (v0.9.3) */
  .unread-badge {
    display: inline-block;
    background: #dc3545;
    color: white;
    font-size: 0.7rem;
    font-weight: bold;
    padding: 0.15rem 0.4rem;
    border-radius: 10px;
    margin-left: 0.5rem;
    min-width: 1.2rem;
    text-align: center;
  }

  .disconnect-btn {
    padding: 0.3rem 0.6rem;
    background: transparent;
    color: #999;
    font-size: 1.5rem;
    border: 1px solid transparent;
    flex: 1;
  }

  .disconnect-btn:hover {
    background: #ffebee;
    color: #dc3545;
  }
  
  .no-peers {
    text-align: center;
    color: #999;
    font-style: italic;
    padding: 1rem;
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
    justify-content: space-between;
    align-items: center;
    padding: 1rem;
    border-bottom: 1px solid #eee;
  }

  .chat-title-section {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .chat-header h2 {
    margin: 0;
    border: none;
    padding: 0;
  }

  .provider-selector {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-size: 0.9rem;
  }

  .provider-selector label {
    font-weight: 500;
    color: #666;
  }

  .provider-selector select {
    padding: 0.4rem 0.6rem;
    border: 1px solid #ddd;
    border-radius: 4px;
    background: white;
    cursor: pointer;
    font-size: 0.85rem;
  }

  .provider-selector select:hover {
    border-color: #4CAF50;
  }

  .provider-selector select:focus {
    outline: none;
    border-color: #4CAF50;
    box-shadow: 0 0 0 2px rgba(76, 175, 80, 0.1);
  }

  .provider-selector select:disabled {
    background: #f5f5f5;
    cursor: not-allowed;
    opacity: 0.7;
  }

  .token-counter {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    font-size: 0.85rem;
    padding: 0.4rem 0.8rem;
    background: #f8f9fa;
    border-radius: 6px;
    border: 1px solid #e0e0e0;
  }

  .token-value {
    font-family: 'Courier New', monospace;
    color: #333;
    font-weight: 600;
  }

  .token-percentage {
    color: #4CAF50;
    font-weight: 500;
  }

  .token-percentage.warning {
    color: #ff9800;
    font-weight: 600;
  }

  .chat-actions {
    display: flex;
    gap: 0.75rem;
    align-items: center;
  }

  .btn-new-chat {
    padding: 0.6rem 1rem;
    background: linear-gradient(135deg, #6c757d 0%, #5a6268 100%);
    color: white;
    border: none;
    border-radius: 6px;
    font-size: 0.9rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s;
    white-space: nowrap;
  }

  .btn-new-chat:hover {
    transform: translateY(-1px);
    box-shadow: 0 4px 12px rgba(108, 117, 125, 0.4);
  }

  .btn-new-chat:active {
    transform: translateY(0);
  }

  .btn-end-session {
    padding: 0.6rem 1rem;
    background: linear-gradient(135deg, #28a745 0%, #20c997 100%);
    color: white;
    border: none;
    border-radius: 6px;
    font-size: 0.9rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s;
    white-space: nowrap;
  }

  .btn-end-session:hover {
    transform: translateY(-1px);
    box-shadow: 0 4px 12px rgba(40, 167, 69, 0.4);
  }

  .btn-end-session:active {
    transform: translateY(0);
  }

  .btn-markdown-toggle {
    padding: 0.6rem 1rem;
    background: linear-gradient(135deg, #6c757d 0%, #5a6268 100%);
    color: white;
    border: none;
    border-radius: 6px;
    font-size: 0.9rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s;
    white-space: nowrap;
    opacity: 0.7;
  }

  .btn-markdown-toggle.active {
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    opacity: 1;
  }

  .btn-markdown-toggle:hover {
    transform: translateY(-1px);
    opacity: 1;
  }

  .btn-markdown-toggle.active:hover {
    box-shadow: 0 4px 12px rgba(102, 126, 234, 0.4);
  }

  .btn-markdown-toggle:active {
    transform: translateY(0);
  }

  .chat-window {
    flex: 1;
    padding: 1rem;
    overflow-y: auto;
    overflow-x: hidden; /* Prevent horizontal overflow */
    background: #f9f9f9;
    max-width: 100%; /* Constrain to parent width */
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

  .empty-chat {
    display: flex;
    align-items: center;
    justify-content: center;
    height: 100%;
    color: #999;
    font-style: italic;
  }
  
  .message {
    margin-bottom: 1rem;
    padding: 0.75rem;
    border-radius: 12px;
    max-width: 80%;
    animation: slideIn 0.2s ease-out;
    overflow-wrap: break-word; /* Break long words */
    word-break: break-word; /* Break long unbreakable strings */
  }
  
  @keyframes slideIn {
    from { opacity: 0; transform: translateY(10px); }
    to { opacity: 1; transform: translateY(0); }
  }
  
  .message.user {
    background: #dcf8c6;
    margin-left: auto;
  }
  
  .message:not(.user):not(.system) {
    background: white;
    border: 1px solid #eee;
  }
  
  .message.system {
    background: #fff0f0;
    border: 1px solid #ffc0c0;
    font-style: italic;
    margin-left: auto;
    margin-right: auto;
  }
  
  .message-header {
    display: flex;
    justify-content: space-between;
    margin-bottom: 0.5rem;
    font-size: 0.85rem;
  }
  
  .message-header strong {
    color: #555;
  }
  
  .timestamp {
    color: #999;
    font-size: 0.75rem;
  }
  
  .message p {
    margin: 0;
    white-space: pre-wrap;
    word-wrap: break-word;
    overflow-wrap: break-word; /* Break long words */
    word-break: break-word; /* Break long unbreakable strings */
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
    background: linear-gradient(135deg, #10b981 0%, #059669 100%);
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

  .compute-host-selector {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.5rem;
    background: #f8f9fa;
    border-radius: 6px;
    border: 1px solid #e0e0e0;
  }

  .compute-host-selector label {
    font-size: 0.9rem;
    font-weight: 500;
    color: #555;
    margin: 0;
  }

  .compute-host-selector select {
    flex: 1;
    max-width: 400px;  /* Prevent selects from becoming too wide */
    padding: 0.4rem 0.6rem;
    border: 1px solid #ccc;
    border-radius: 4px;
    background: white;
    font-size: 0.9rem;
    cursor: pointer;
    transition: border-color 0.2s;
    /* Truncate long model names with ellipsis */
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .compute-host-selector select:hover {
    border-color: #999;
  }

  .compute-host-selector select:focus {
    outline: none;
    border-color: #4285f4;
    box-shadow: 0 0 0 2px rgba(66, 133, 244, 0.1);
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
  }

  .input-row button {
    width: 100px;
    align-self: flex-end;
  }
  
  .connecting, .error {
    text-align: center;
    padding: 2rem;
  }

  /* Connection Status Styles */
  .connection-mode {
    margin-top: 1rem;
    padding-top: 1rem;
    border-top: 1px solid #eee;
  }

  .mode-badge {
    display: inline-block;
    padding: 0.5rem 1rem;
    border-radius: 6px;
    font-weight: bold;
    font-size: 0.95rem;
    margin-bottom: 0.5rem;
  }

  .mode-badge.fully-online {
    background: #d4edda;
    color: #155724;
    border: 1px solid #c3e6cb;
  }

  .mode-badge.hub-offline {
    background: #fff3cd;
    color: #856404;
    border: 1px solid #ffeaa7;
  }

  .mode-badge.fully-offline {
    background: #f8d7da;
    color: #721c24;
    border: 1px solid #f5c6cb;
  }

  .mode-description {
    font-size: 0.85rem;
    color: #666;
    margin: 0.5rem 0;
    font-style: italic;
  }

  .features-details {
    margin-top: 0.75rem;
    font-size: 0.9rem;
  }

  .features-details summary {
    cursor: pointer;
    font-weight: 600;
    color: #555;
    padding: 0.5rem 0;
    user-select: none;
  }

  .features-details summary:hover {
    color: #007bff;
  }

  .features-list {
    list-style: none;
    padding: 0.5rem 0 0 1rem;
    margin: 0;
  }

  .features-list li {
    padding: 0.3rem 0;
    font-size: 0.85rem;
  }

  .features-list li.feature-available {
    color: #28a745;
  }

  .features-list li.feature-unavailable {
    color: #dc3545;
    text-decoration: line-through;
    opacity: 0.6;
  }

  .peer-count {
    color: #888;
    font-size: 0.9em;
    margin-left: 0.25rem;
  }

  .cached-info {
    font-size: 0.8rem;
    color: #666;
    margin-top: 0.5rem;
    padding: 0.4rem;
    background: #f0f0f0;
    border-radius: 4px;
    text-align: center;
  }

  /* DPC URI Styles - Redesigned for better UX */
  .dpc-uris-section {
    margin-top: 1rem;
    margin-bottom: 0.5rem;
  }

  .uri-details {
    background: #ffffff;
    border: 1px solid #e0e0e0;
    border-radius: 8px;
    padding: 0;
    overflow: hidden;
  }

  .uri-details[open] {
    border-color: #007bff;
  }

  .uri-summary {
    padding: 0.75rem 1rem;
    background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%);
    cursor: pointer;
    list-style: none;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-weight: 600;
    color: #495057;
    transition: background 0.2s;
    user-select: none;
  }

  .uri-summary::-webkit-details-marker {
    display: none;
  }

  .uri-summary:hover {
    background: linear-gradient(135deg, #e9ecef 0%, #dee2e6 100%);
  }

  .uri-icon {
    font-size: 1.2rem;
  }

  .uri-title {
    flex: 1;
    font-size: 0.9rem;
  }

  .uri-help-text {
    padding: 0.5rem 1rem 0.75rem;
    font-size: 0.75rem;
    color: #6c757d;
    background: #f8f9fa;
    border-bottom: 1px solid #e9ecef;
  }

  .uri-card {
    padding: 0.75rem 1rem;
    border-bottom: 1px solid #f0f0f0;
    transition: background 0.2s;
  }

  .uri-card:last-of-type {
    border-bottom: none;
  }

  .uri-card:hover {
    background: #f8f9fa;
  }

  .uri-card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
  }

  .ip-badge {
    flex: 1;
    font-family: 'Courier New', monospace;
    font-size: 0.9rem;
    font-weight: 600;
    color: #0056b3;
    padding: 0.4rem 0.6rem;
    background: #e7f1ff;
    border-radius: 6px;
    border: 1px solid #b3d7ff;
  }

  .ip-badge.external {
    color: #0d6e2b;
    background: #d1f4e0;
    border: 1px solid #7fd99f;
  }

  .copy-btn-icon {
    width: auto;
    min-width: auto;
    padding: 0.3rem 0.5rem;
    font-size: 1rem;
    background: transparent;
    border: 1px solid #ddd;
    cursor: pointer;
    border-radius: 4px;
    transition: all 0.2s;
    flex-shrink: 0;
  }

  .copy-btn-icon:hover {
    background: #f0f0f0;
    border-color: #007bff;
  }

  .copy-btn-icon:active {
    background: #e0e0e0;
  }

  .uri-full-details {
    margin-top: 0.5rem;
  }

  .uri-full-details summary {
    display: none;
  }

  .show-uri-btn {
    display: inline-block !important;
    font-size: 0.75rem;
    color: #007bff;
    cursor: pointer;
    padding: 0.3rem 0.6rem;
    background: #f0f7ff;
    border: 1px solid #cce5ff;
    border-radius: 4px;
    margin-top: 0.4rem;
    transition: all 0.2s;
    user-select: none;
  }

  .show-uri-btn:hover {
    background: #e0f0ff;
    border-color: #99ccff;
  }

  .uri-full-text {
    display: block;
    margin-top: 0.5rem;
    padding: 0.6rem;
    font-family: 'Courier New', monospace;
    font-size: 0.7rem;
    color: #495057;
    background: #f8f9fa;
    border: 1px solid #dee2e6;
    border-radius: 4px;
    word-break: break-all;
    line-height: 1.5;
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
    background: linear-gradient(135deg, #4CAF50 0%, #45a049 100%);
    color: white;
    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
  }

  .btn-confirm:hover {
    background: linear-gradient(135deg, #45a049 0%, #4CAF50 100%);
    box-shadow: 0 4px 8px rgba(0, 0, 0, 0.15);
    transform: translateY(-1px);
  }

  .btn-confirm:active {
    transform: translateY(0);
    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
  }

  /* Prevent text selection during resize */
  :global(body.resizing) {
    cursor: ns-resize !important;
    user-select: none !important;
  }

  /* File Transfer UI Styles (Week 1) */
  .file-button {
    min-width: 45px;
    padding: 8px 12px;
    font-size: 18px;
    margin-right: 8px;
    cursor: pointer;
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    border: none;
    border-radius: 4px;
    transition: all 0.2s;
  }

  .file-button:hover:not(:disabled) {
    transform: translateY(-1px);
    box-shadow: 0 4px 8px rgba(0, 0, 0, 0.15);
  }

  .file-button:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .modal-overlay {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: rgba(0, 0, 0, 0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }

  .modal-dialog {
    background: #2a2a2a;
    padding: 24px;
    border-radius: 8px;
    max-width: 500px;
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
  }

  .modal-dialog h3 {
    margin-top: 0;
    color: #e0e0e0;
  }

  .modal-dialog p {
    margin: 8px 0;
    color: #b0b0b0;
  }

  .modal-buttons {
    display: flex;
    gap: 12px;
    margin-top: 20px;
  }

  .accept-button {
    flex: 1;
    padding: 10px;
    background: linear-gradient(135deg, #4CAF50 0%, #45a049 100%);
    color: white;
    border: none;
    border-radius: 4px;
    cursor: pointer;
    font-weight: 500;
  }

  .accept-button:hover {
    transform: translateY(-1px);
    box-shadow: 0 4px 8px rgba(0, 0, 0, 0.15);
  }

  .reject-button {
    flex: 1;
    padding: 10px;
    background: linear-gradient(135deg, #f44336 0%, #d32f2f 100%);
    color: white;
    border: none;
    border-radius: 4px;
    cursor: pointer;
    font-weight: 500;
  }

  .reject-button:hover {
    transform: translateY(-1px);
    box-shadow: 0 4px 8px rgba(0, 0, 0, 0.15);
  }

  .active-transfers-panel {
    position: fixed;
    bottom: 20px;
    right: 20px;
    background: #2a2a2a;
    padding: 16px;
    border-radius: 8px;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.3);
    min-width: 300px;
    z-index: 999;
  }

  .active-transfers-panel h4 {
    margin: 0 0 12px 0;
    color: #e0e0e0;
    font-size: 14px;
  }

  .transfer-item {
    margin-bottom: 12px;
    padding-bottom: 12px;
    border-bottom: 1px solid #444;
  }

  .transfer-item:last-child {
    margin-bottom: 0;
    padding-bottom: 0;
    border-bottom: none;
  }

  .transfer-info {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 8px;
  }

  .transfer-filename {
    color: #b0b0b0;
    font-size: 13px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1;
    min-width: 0;
  }

  .transfer-status {
    color: #888;
    font-size: 12px;
    white-space: nowrap;
  }

  .cancel-transfer-button {
    background: transparent;
    border: none;
    color: #888;
    font-size: 20px;
    line-height: 1;
    padding: 0;
    width: 24px;
    height: 24px;
    cursor: pointer;
    border-radius: 4px;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: all 0.2s ease;
    flex-shrink: 0;
  }

  .cancel-transfer-button:hover {
    background: rgba(255, 68, 68, 0.2);
    color: #ff4444;
  }

  .cancel-transfer-button:active {
    transform: scale(0.95);
  }

  .progress-bar {
    width: 100%;
    height: 6px;
    background: #444;
    border-radius: 3px;
    overflow: hidden;
    margin-bottom: 4px;
  }

  .progress-fill {
    height: 100%;
    background: linear-gradient(90deg, #667eea 0%, #764ba2 100%);
    transition: width 0.3s ease;
  }

  .progress-text {
    font-size: 11px;
    color: #888;
  }

  /* File attachment display (Week 1) */
  .message-attachments {
    margin-top: 8px;
  }

  .file-attachment {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 12px;
    background: rgba(255, 255, 255, 0.05);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 8px;
    margin-top: 8px;
    transition: background 0.2s ease;
  }

  .file-attachment:hover {
    background: rgba(255, 255, 255, 0.08);
  }

  .file-details {
    flex: 1;
    min-width: 0;
  }

  .file-name {
    color: #1a1a1a;
    font-weight: 600;
    font-size: 14px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .file-meta {
    color: #4a4a4a;
    font-size: 12px;
    margin-top: 4px;
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