<!-- src/lib/panels/ChatPanel.svelte -->
<!-- Chat input orchestration panel (Phase 3 Step 4) -->
<!-- Owns: input state, draft, file transfer UI, context toggle, resize handle, mention autocomplete -->

<script lang="ts">
  import type { Writable } from 'svelte/store';
  import { get } from 'svelte/store';
  import FileTransferUI from '$lib/components/FileTransferUI.svelte';
  import VoiceRecorder from '$lib/components/VoiceRecorder.svelte';
  import TokenWarningBanner from '$lib/components/TokenWarningBanner.svelte';
  import IntegrityWarningBanner from '$lib/components/IntegrityWarningBanner.svelte';
  import AgentTaskBoard from '$lib/components/AgentTaskBoard.svelte';
  import {
    connectionStatus,
    nodeStatus,
    groupChats,
    fileTransferOffer,
    filePreparationStarted,
    filePreparationProgress,
    filePreparationCompleted,
    activeFileTransfers,
    fileTransferComplete,
    fileTransferCancelled,
    integrityWarnings,
    firewallRulesUpdated,
    telegramLinkedChats,
    sendCommand,
    sendFile,
    sendVoiceMessage,
    acceptFileTransfer,
    cancelFileTransfer,
    sendToTelegram,
    sendGroupMessage,
    sendGroupImage,
    sendGroupVoiceMessage,
    sendGroupFile,
  } from '$lib/coreService';
  import { estimateConversationUsage } from '$lib/tokenEstimator';
  import { showNotificationIfBackground } from '$lib/notificationService';
  import type { Message, Mention, MessageAttachment } from '$lib/types.js';

  type AIChatMeta = {
    name: string;
    provider: string;
    instruction_set_name?: string;
    profile_name?: string;
    llm_provider?: string;
    compute_host?: string;
  };

  type TokenUsage = {
    used: number;
    limit: number;
    historyTokens?: number;
    contextEstimated?: number;
  };

  type InstructionSets = {
    schema_version: string;
    default: string;
    sets: Record<string, { name: string; description: string }>;
  };

  const DEFAULT_TOKEN_LIMIT = 16384;

  // ---------------------------------------------------------------------------
  // Props
  // ---------------------------------------------------------------------------
  let {
    activeChatId,
    chatHistories,
    commandToChatMap,
    persistCommandToChatMap,
    agentChatToAgentId,
    aiChats,
    chatProviders,
    selectedTextProvider,
    selectedVisionProvider,
    selectedVoiceProvider,
    clearAgentStreaming,
    autoScroll,
    setChatLoading,
    isLoading,
    tokenUsageMap,
    availableInstructionSets,
    currentContextHash,
    lastSentContextHash,
    peerContextHashes,
    lastSentPeerHashes,
    peerDisplayNames,
    autoTranscribeEnabled,
    whisperModelLoading,
    groupPanelRef = null,
    chatPanelHeight = $bindable(600),
    showAgentBoard = $bindable(false),
    currentInput = $bindable(''),
    isSleeping = false,
  }: {
    activeChatId: string;
    chatHistories: Writable<Map<string, Message[]>>;
    commandToChatMap: Map<string, string>;
    persistCommandToChatMap?: () => void;
    agentChatToAgentId: Map<string, string>;
    aiChats: Writable<Map<string, AIChatMeta>>;
    chatProviders: Writable<Map<string, string>>;
    selectedTextProvider: string;
    selectedVisionProvider: string;
    selectedVoiceProvider: string;
    clearAgentStreaming: () => void;
    autoScroll: () => void;
    setChatLoading: (chatId: string, loading: boolean) => void;
    isLoading: boolean;
    tokenUsageMap: Map<string, TokenUsage>;
    availableInstructionSets: InstructionSets | null;
    currentContextHash: string;
    lastSentContextHash: Map<string, string>;
    peerContextHashes: Map<string, string>;
    lastSentPeerHashes: Map<string, Map<string, string>>;
    peerDisplayNames: Map<string, string>;
    autoTranscribeEnabled: boolean;
    whisperModelLoading: boolean;
    isSleeping?: boolean;
    groupPanelRef?: any;
    chatPanelHeight?: number;
    showAgentBoard?: boolean;
    currentInput?: string;
  } = $props();

  // Expose input value for GroupPanel's handleMentionSelect
  export function getInputValue(): string { return currentInput; }
  export function setInputValue(val: string) { currentInput = val; }

  // ---------------------------------------------------------------------------
  // State (owned by ChatPanel)
  // ---------------------------------------------------------------------------
  let chatDraftInputs = $state(new Map<string, string>());
  let voicePreview = $state<{ blob: Blob; duration: number; filePath?: string } | null>(null);
  let pendingImage = $state<{ dataUrl: string; filename: string; sizeBytes: number } | null>(null);

  // Resize
  let isResizing = $state(false);
  let resizeStartY = 0;
  let resizeStartHeight = 0;

  // File offer (incoming from peer)
  let showFileOfferDialog = $state(false);
  let currentFileOffer = $state<any>(null);
  let showFileOfferToast = $state(false);
  let fileOfferToastMessage = $state('');

  // Send file confirmation
  let showSendFileDialog = $state(false);
  let pendingFileSend = $state<{
    filePath: string;
    fileName: string;
    recipientId: string;
    recipientName: string;
    imageData?: { dataUrl: string; filename: string; sizeBytes: number };
    caption?: string;
  } | null>(null);
  let isSendingFile = $state(false);

  // Context toggle
  let contextPanelCollapsed = $state(false);
  let includePersonalContext = $state(false);
  let selectedAIScope = $state('');
  let availableAIScopes = $state<string[]>([]);
  let aiScopesLoaded = $state(false);
  let selectedPeerContexts = $state(new Set<string>());

  // Non-reactive draft tracker
  let previousChatId = '';

  // ---------------------------------------------------------------------------
  // Derived state
  // ---------------------------------------------------------------------------
  let currentTokenUsage = $derived(tokenUsageMap.get(activeChatId) || { used: 0, limit: 0 });
  let effectiveTokenUsage = $derived({
    used: currentTokenUsage.used,
    limit: currentTokenUsage.limit > 0 ? currentTokenUsage.limit : DEFAULT_TOKEN_LIMIT,
    historyTokens: currentTokenUsage.historyTokens ?? 0,
    contextEstimated: currentTokenUsage.contextEstimated ?? 0,
  });
  let estimatedUsage = $derived(estimateConversationUsage(effectiveTokenUsage, currentInput));
  let tokenWarningLevel = $derived(
    !$aiChats.has(activeChatId)
      ? 'none'
      : estimatedUsage.percentage >= 1.0
        ? 'critical'
        : estimatedUsage.percentage >= 0.9
          ? 'warning'
          : 'none'
  );
  let showTokenBanner = $derived(tokenWarningLevel === 'critical' || tokenWarningLevel === 'warning');

  let isPeerConnected = $derived(
    activeChatId.startsWith('ai_') || activeChatId === 'local_ai'
      ? true
      : activeChatId.startsWith('group-')
        ? true  // Group chats always allow sending — agents respond locally, peers optional
        : ($nodeStatus?.peer_info?.some((p: any) => p.node_id === activeChatId) ?? false)
  );

  let isTelegramChat = $derived(activeChatId.startsWith('telegram-'));
  let isActuallyAIChat = $derived($aiChats.has(activeChatId) && !activeChatId.startsWith('telegram-'));
  let isGroupChat = $derived(activeChatId.startsWith('group-'));
  let isContextWindowFull = $derived(isActuallyAIChat && estimatedUsage.percentage >= 1.0);

  let localContextUpdated = $derived(
    currentContextHash && lastSentContextHash.get(activeChatId) !== currentContextHash
  );
  let peerContextsUpdated = $derived(new Set(
    Array.from(peerContextHashes.keys()).filter(nodeId => {
      const convPeerHashes = lastSentPeerHashes.get(activeChatId);
      if (!convPeerHashes) return true;
      return convPeerHashes.get(nodeId) !== peerContextHashes.get(nodeId);
    })
  ));

  let selectedInstructionSet = $derived($aiChats.get(activeChatId)?.instruction_set_name || 'general');

  // ---------------------------------------------------------------------------
  // Effects
  // ---------------------------------------------------------------------------

  // Save / restore draft when chat switches
  $effect(() => {
    const currentChat = activeChatId;
    if (previousChatId === '') {
      previousChatId = currentChat;
      return;
    }
    if (currentChat !== previousChatId) {
      chatDraftInputs = new Map(chatDraftInputs).set(previousChatId, currentInput);
      const draft = chatDraftInputs.get(currentChat);
      currentInput = draft !== undefined ? draft : '';
      if (pendingImage !== null) pendingImage = null;
      if (voicePreview !== null) voicePreview = null;
      previousChatId = currentChat;
    }
  });

  // File offer received from peer
  $effect(() => {
    if ($fileTransferOffer) {
      const { node_id, filename, size_bytes, transfer_id, sender_name, group_id } = $fileTransferOffer;
      if (group_id) {
        fileOfferToastMessage = `Receiving file: ${filename} from ${sender_name || 'group member'}`;
        showFileOfferToast = true;
        setTimeout(() => (showFileOfferToast = false), 3000);
        return;
      }
      currentFileOffer = $fileTransferOffer;
      showFileOfferDialog = true;
      (async () => {
        await showNotificationIfBackground({
          title: `File from ${sender_name || node_id.slice(0, 16)}`,
          body: `${filename} (${(size_bytes / 1048576).toFixed(2)} MB)`,
        });
      })();
    }
  });

  // File transfer complete toast
  $effect(() => {
    if ($fileTransferComplete) {
      const { filename, direction } = $fileTransferComplete;
      fileOfferToastMessage = direction === 'download' ? `✓ File downloaded: ${filename}` : `✓ File sent: ${filename}`;
      showFileOfferToast = true;
      setTimeout(() => (showFileOfferToast = false), 5000);
      (async () => {
        await showNotificationIfBackground({ title: 'File Transfer Complete', body: `${filename} (${direction})` });
      })();
    }
  });

  // File transfer cancelled toast
  $effect(() => {
    if ($fileTransferCancelled) {
      const { filename, reason } = $fileTransferCancelled;
      fileOfferToastMessage = `✗ Transfer cancelled: ${filename} (${reason})`;
      showFileOfferToast = true;
      setTimeout(() => (showFileOfferToast = false), 5000);
      (async () => {
        await showNotificationIfBackground({ title: 'Transfer Cancelled', body: `${filename} (${reason})` });
      })();
    }
  });

  // Clear disconnected peers from selectedPeerContexts
  $effect(() => {
    if (selectedPeerContexts.size > 0 && $nodeStatus?.p2p_peers) {
      const connectedPeers = new Set($nodeStatus.p2p_peers);
      let needsUpdate = false;
      for (const peerId of selectedPeerContexts) {
        if (!connectedPeers.has(peerId)) {
          selectedPeerContexts.delete(peerId);
          needsUpdate = true;
        }
      }
      if (needsUpdate) selectedPeerContexts = new Set(selectedPeerContexts);
    }
  });

  // Load AI scopes on connect
  $effect(() => {
    if ($connectionStatus === 'connected' && !aiScopesLoaded) {
      loadAIScopes();
    }
  });
  $effect(() => {
    if ($connectionStatus === 'disconnected' || $connectionStatus === 'error') {
      aiScopesLoaded = false;
    }
  });
  $effect(() => {
    if ($firewallRulesUpdated && $connectionStatus === 'connected') {
      aiScopesLoaded = false;
      loadAIScopes();
    }
  });

  // ---------------------------------------------------------------------------
  // Helper: parse provider selection (pure function — no external deps)
  // ---------------------------------------------------------------------------
  function parseProviderSelection(uniqueId: string): { source: 'local' | 'remote'; alias: string; nodeId?: string } {
    if (!uniqueId) return { source: 'local', alias: '' };
    if (uniqueId.startsWith('remote:')) {
      const parts = uniqueId.split(':');
      return { source: 'remote', nodeId: parts[1], alias: parts.slice(2).join(':') };
    }
    return { source: 'local', alias: uniqueId.replace('local:', '') };
  }

  // ---------------------------------------------------------------------------
  // AI Scopes
  // ---------------------------------------------------------------------------
  async function loadAIScopes() {
    try {
      const result = await sendCommand('get_firewall_rules', {});
      if (result.status === 'success' && result.rules?.ai_scopes) {
        availableAIScopes = Object.keys(result.rules.ai_scopes).filter(k => !k.startsWith('_'));
      } else {
        availableAIScopes = [];
      }
    } catch {
      availableAIScopes = [];
    } finally {
      aiScopesLoaded = true;
    }
  }

  // ---------------------------------------------------------------------------
  // Context toggle helpers
  // ---------------------------------------------------------------------------
  function togglePeerContext(peerId: string) {
    if (selectedPeerContexts.has(peerId)) {
      selectedPeerContexts.delete(peerId);
    } else {
      selectedPeerContexts.add(peerId);
    }
    selectedPeerContexts = new Set(selectedPeerContexts);
  }

  function updateInstructionSet(newInstructionSet: string) {
    const chatMeta = get(aiChats).get(activeChatId);
    if (chatMeta) {
      chatMeta.instruction_set_name = newInstructionSet;
      aiChats.update(map => new Map(map));
    }
  }

  async function handleEndSession(conversationId: string) {
    // window.confirm() is non-blocking in Tauri WebView2 on Windows — see
    // +page.svelte:handleEndSession comment. Use Tauri ask() with fallback.
    let proceed: boolean;
    try {
      const { ask } = await import('@tauri-apps/plugin-dialog');
      proceed = await ask('End this conversation session and extract knowledge?', { title: 'dpc-messenger', kind: 'info' });
    } catch {
      proceed = window.confirm('End this conversation session and extract knowledge?');
    }
    if (proceed) {
      sendCommand('end_conversation_session', { conversation_id: conversationId });
    }
  }

  // ---------------------------------------------------------------------------
  // Resize
  // ---------------------------------------------------------------------------
  function startResize(e: MouseEvent) {
    isResizing = true;
    resizeStartY = e.clientY;
    resizeStartHeight = chatPanelHeight;
    e.preventDefault();
    document.body.classList.add('resizing');
    document.addEventListener('mousemove', handleResize);
    document.addEventListener('mouseup', stopResize);
  }

  function handleResize(e: MouseEvent) {
    if (!isResizing) return;
    chatPanelHeight = Math.max(300, resizeStartHeight + (e.clientY - resizeStartY));
  }

  function stopResize() {
    isResizing = false;
    document.body.classList.remove('resizing');
    document.removeEventListener('mousemove', handleResize);
    document.removeEventListener('mouseup', stopResize);
    localStorage.setItem('chatPanelHeight', chatPanelHeight.toString());
  }

  // ---------------------------------------------------------------------------
  // Send message
  // ---------------------------------------------------------------------------
  async function handleSendMessage() {
    if (pendingImage) {
      await _sendImageMessage();
      return;
    }
    if (!currentInput.trim()) return;

    const text = currentInput.trim();
    currentInput = '';
    chatDraftInputs = new Map(chatDraftInputs).set(activeChatId, '');
    clearAgentStreaming();

    chatHistories.update(h => {
      const m = new Map(h);
      const hist = m.get(activeChatId) || [];
      m.set(activeChatId, [...hist, { id: crypto.randomUUID(), sender: 'user', text, timestamp: Date.now() }]);
      return m;
    });

    if ($aiChats.has(activeChatId) && !activeChatId.startsWith('telegram-')) {
      await _sendAIQuery(text);
    } else if (activeChatId.startsWith('telegram-')) {
      await _sendTelegramMessage(text);
    } else if (activeChatId.startsWith('group-')) {
      sendGroupMessage(activeChatId, text);
    } else {
      sendCommand('send_p2p_message', { target_node_id: activeChatId, text });
    }

    autoScroll();
  }

  async function _sendImageMessage() {
    const text = currentInput.trim();
    const imageData = pendingImage!;
    currentInput = '';
    chatDraftInputs = new Map(chatDraftInputs).set(activeChatId, '');
    pendingImage = null;

    if ($aiChats.has(activeChatId) && !activeChatId.startsWith('telegram-')) {
      // AI vision
      chatHistories.update(h => {
        const m = new Map(h);
        const hist = m.get(activeChatId) || [];
        m.set(activeChatId, [...hist, {
          id: crypto.randomUUID(), sender: 'user', text: text || '[Image]', timestamp: Date.now(),
          attachments: [{ type: 'image', filename: imageData.filename, thumbnail: imageData.dataUrl, size_bytes: imageData.sizeBytes }],
        }]);
        return m;
      });
      try {
        setChatLoading(activeChatId, true);
        const visionProvider = parseProviderSelection(selectedVisionProvider);
        const payload: any = {
          conversation_id: agentChatToAgentId.get(activeChatId) ?? activeChatId,
          image_base64: imageData.dataUrl,
          filename: imageData.filename,
          caption: text,
          provider_alias: visionProvider.alias,
          chat_provider: get(chatProviders).get(activeChatId) || null,
        };
        if (visionProvider.source === 'remote' && visionProvider.nodeId) {
          payload.compute_host = visionProvider.nodeId;
        }
        await sendCommand('send_image', payload);
        autoScroll();
      } catch (error) {
        const errorStr = String(error);
        let userMessage = 'Failed to analyze image';
        let errorDetails = '';
        if (errorStr.includes('Failed to connect to Ollama')) {
          userMessage = 'Ollama Connection Failed'; errorDetails = 'Ollama is not running.';
        } else if (errorStr.includes('out of memory') || errorStr.includes('VRAM')) {
          userMessage = 'Out of Memory'; errorDetails = 'Not enough GPU memory for vision analysis.';
        } else {
          const match = errorStr.match(/RuntimeError:\s*(.+)/);
          errorDetails = match ? match[1] : errorStr.slice(0, 200);
        }
        chatHistories.update(h => {
          const m = new Map(h);
          const hist = m.get(activeChatId) || [];
          m.set(activeChatId, [...hist, { id: crypto.randomUUID(), sender: 'ai', text: `⚠️ **${userMessage}**\n\n${errorDetails}`, timestamp: Date.now(), isError: true }]);
          return m;
        });
        autoScroll();
        setChatLoading(activeChatId, false);
      }
    } else if (activeChatId.startsWith('telegram-')) {
      // Telegram screenshot
      try {
        const response = await fetch(imageData.dataUrl);
        const blob = await response.blob();
        const arrayBuffer = await blob.arrayBuffer();
        const uint8Array = new Uint8Array(arrayBuffer);
        const isTauriEnv = typeof window !== 'undefined' && ((window as any).isTauri === true || !!(window as any).__TAURI__);
        if (isTauriEnv) {
          const { writeFile, BaseDirectory, mkdir } = await import('@tauri-apps/plugin-fs');
          const { invoke } = await import('@tauri-apps/api/core');
          const filename = imageData.filename || `screenshot_${Date.now()}.png`;
          await mkdir('.dpc/temp', { baseDir: BaseDirectory.Home, recursive: true });
          await writeFile(`.dpc/temp/${filename}`, uint8Array, { baseDir: BaseDirectory.Home });
          const homeDir = await invoke<string>('get_home_directory');
          const fullPath = `${homeDir}/.dpc/temp/${filename}`;
          await sendToTelegram(activeChatId, text || '', [], undefined, undefined, undefined, fullPath);
          chatHistories.update(h => {
            const m = new Map(h);
            const hist = m.get(activeChatId) || [];
            m.set(activeChatId, [...hist, {
              id: crypto.randomUUID(), sender: 'user', text: text || '', timestamp: Date.now(),
              attachments: [{ type: 'image', filename, file_path: fullPath, size_bytes: uint8Array.length }],
            }]);
            return m;
          });
        } else {
          throw new Error('Telegram file transfer requires Tauri desktop app');
        }
      } catch (error) {
        fileOfferToastMessage = `Failed to send screenshot to Telegram: ${error}`;
        showFileOfferToast = true;
        setTimeout(() => (showFileOfferToast = false), 5000);
      }
    } else if (activeChatId.startsWith('group-')) {
      const group = get(groupChats).get(activeChatId);
      pendingFileSend = {
        filePath: '', fileName: imageData.filename,
        recipientId: activeChatId, recipientName: `group "${group?.name || 'group'}"`,
        imageData, caption: text,
      };
      showSendFileDialog = true;
    } else {
      // P2P screenshot
      try {
        const imageSizeMB = (imageData.dataUrl.length * 0.75) / (1024 * 1024);
        if (imageSizeMB > 25 && !window.confirm(`This screenshot is ${imageSizeMB.toFixed(1)} MB. Continue?`)) {
          pendingImage = null;
          return;
        }
        await sendCommand('send_p2p_image', { node_id: activeChatId, image_base64: imageData.dataUrl, filename: imageData.filename, text });
      } catch (error) {
        fileOfferToastMessage = `Failed to send screenshot: ${error instanceof Error ? error.message : String(error)}`;
        showFileOfferToast = true;
        setTimeout(() => (showFileOfferToast = false), 5000);
      }
    }
  }

  async function _sendAIQuery(text: string) {
    setChatLoading(activeChatId, true);
    const commandId = crypto.randomUUID();
    commandToChatMap.set(commandId, activeChatId);
    persistCommandToChatMap?.();

    chatHistories.update(h => {
      const m = new Map(h);
      const hist = m.get(activeChatId) || [];
      m.set(activeChatId, [...hist, { id: crypto.randomUUID(), sender: 'ai', text: 'Thinking...', timestamp: Date.now(), commandId }]);
      return m;
    });

    const chatMeta = get(aiChats).get(activeChatId);
    const backendConvId = agentChatToAgentId.get(activeChatId) ?? activeChatId;
    const payload: any = {
      prompt: text,
      include_context: includePersonalContext,
      conversation_id: backendConvId,
      ai_scope: selectedAIScope || null,
      instruction_set_name: chatMeta?.instruction_set_name || 'general',
    };
    if (selectedPeerContexts.size > 0) payload.context_ids = Array.from(selectedPeerContexts);

    const chatSpecificProvider = get(chatProviders).get(activeChatId);
    if (chatSpecificProvider) {
      payload.provider = chatSpecificProvider;
      if (chatMeta?.compute_host) payload.compute_host = chatMeta.compute_host;
      if (chatSpecificProvider === 'dpc_agent' && chatMeta?.llm_provider) {
        payload.agent_llm_provider = chatMeta.llm_provider;
      }
    } else {
      const textProvider = parseProviderSelection(selectedTextProvider);
      if (textProvider.source === 'remote' && textProvider.nodeId) {
        payload.compute_host = textProvider.nodeId;
        payload.provider = textProvider.alias;
      } else if (textProvider.alias) {
        payload.provider = textProvider.alias;
      }
    }

    const success = sendCommand('execute_ai_query', payload, commandId);
    if (!success) {
      setChatLoading(activeChatId, false);
      chatHistories.update(h => {
        const m = new Map(h);
        const hist = m.get(activeChatId) || [];
        m.set(activeChatId, hist.map(msg => msg.commandId === commandId ? { ...msg, sender: 'system', text: 'Error: Not connected' } : msg));
        return m;
      });
      commandToChatMap.delete(commandId);
      persistCommandToChatMap?.();
    }
  }

  async function _sendTelegramMessage(text: string) {
    try {
      const linkedChatId = $telegramLinkedChats.get(activeChatId);
      if (linkedChatId) await sendToTelegram(activeChatId, text);
    } catch (error) {
      console.error('[Telegram] Failed to send message:', error);
    }
  }

  // ---------------------------------------------------------------------------
  // File send
  // ---------------------------------------------------------------------------
  async function handleSendFile() {
    const isTauriEnv = typeof window !== 'undefined' && ((window as any).isTauri === true || !!(window as any).__TAURI__);
    if (!isTauriEnv) return;
    const { open } = await import('@tauri-apps/plugin-dialog');
    const filePath = await open({ multiple: false, title: 'Select file to send' });
    if (!filePath || typeof filePath !== 'string') return;

    const fileName = filePath.split(/[\\/]/).pop() || filePath;
    let recipientName = activeChatId;

    if (activeChatId.startsWith('telegram-')) {
      const linkedChatId = $telegramLinkedChats.get(activeChatId);
      if (!linkedChatId) return;
      recipientName = `Telegram (${linkedChatId})`;
      pendingFileSend = { filePath, fileName, recipientId: activeChatId, recipientName };
      showSendFileDialog = true;
    } else if (activeChatId.startsWith('group-')) {
      const group = get(groupChats).get(activeChatId);
      recipientName = group?.name || activeChatId;
      pendingFileSend = { filePath, fileName, recipientId: activeChatId, recipientName: `group "${recipientName}"` };
      showSendFileDialog = true;
    } else {
      recipientName = activeChatId;
      pendingFileSend = { filePath, fileName, recipientId: activeChatId, recipientName };
      showSendFileDialog = true;
    }
  }

  async function handleConfirmSendFile() {
    if (!pendingFileSend || isSendingFile) return;
    isSendingFile = true;
    try {
      if (pendingFileSend.imageData) {
        if (pendingFileSend.recipientId.startsWith('group-')) {
          await sendGroupImage(pendingFileSend.recipientId, pendingFileSend.imageData.dataUrl, pendingFileSend.imageData.filename, pendingFileSend.caption || '');
        } else {
          await sendCommand('send_p2p_image', { node_id: pendingFileSend.recipientId, image_base64: pendingFileSend.imageData.dataUrl, filename: pendingFileSend.imageData.filename, text: pendingFileSend.caption || '' });
        }
      } else {
        if (pendingFileSend.recipientId.startsWith('group-')) {
          await sendGroupFile(pendingFileSend.recipientId, pendingFileSend.filePath);
        } else {
          await sendFile(pendingFileSend.recipientId, pendingFileSend.filePath);
        }
      }
      showSendFileDialog = false;
      pendingFileSend = null;
      fileOfferToastMessage = 'Sending...';
      showFileOfferToast = true;
      setTimeout(() => (showFileOfferToast = false), 3000);
    } catch (error) {
      fileOfferToastMessage = `Failed to send: ${error}`;
      showFileOfferToast = true;
      setTimeout(() => (showFileOfferToast = false), 5000);
    } finally {
      isSendingFile = false;
      filePreparationStarted.set(null);
      filePreparationProgress.set(null);
      filePreparationCompleted.set(null);
    }
  }

  function handleCancelSendFile() {
    showSendFileDialog = false;
    pendingFileSend = null;
    filePreparationStarted.set(null);
    filePreparationProgress.set(null);
    filePreparationCompleted.set(null);
  }

  async function handleAcceptFile() {
    if (!currentFileOffer) return;
    try {
      const filename = currentFileOffer.filename;
      await acceptFileTransfer(currentFileOffer.transfer_id);
      showFileOfferDialog = false;
      currentFileOffer = null;
      fileOfferToastMessage = `Downloading: ${filename}`;
      showFileOfferToast = true;
      setTimeout(() => (showFileOfferToast = false), 3000);
    } catch (error) {
      fileOfferToastMessage = `Failed to accept: ${error}`;
      showFileOfferToast = true;
      setTimeout(() => (showFileOfferToast = false), 5000);
    }
  }

  async function handleRejectFile() {
    if (!currentFileOffer) return;
    try {
      await cancelFileTransfer(currentFileOffer.transfer_id, 'user_rejected');
      showFileOfferDialog = false;
      currentFileOffer = null;
    } catch (error) {
      console.error('Error rejecting file:', error);
    }
  }

  async function handleCancelTransfer(transferId: string, filename: string) {
    try {
      await cancelFileTransfer(transferId, 'user_cancelled');
      fileOfferToastMessage = `Cancelled: ${filename}`;
      showFileOfferToast = true;
      setTimeout(() => (showFileOfferToast = false), 3000);
    } catch (error) {
      fileOfferToastMessage = `Failed to cancel: ${error}`;
      showFileOfferToast = true;
      setTimeout(() => (showFileOfferToast = false), 5000);
    }
  }

  // ---------------------------------------------------------------------------
  // Paste (images)
  // ---------------------------------------------------------------------------
  function clearPendingImage() { pendingImage = null; }

  async function handlePaste(e: ClipboardEvent) {
    const items = e.clipboardData?.items;
    if (!items) return;

    for (const item of items) {
      if (item.type.startsWith('image/')) {
        e.preventDefault();
        const blob = item.getAsFile();
        if (!blob) continue;
        const reader = new FileReader();
        reader.onload = (ev) => {
          const dataUrl = ev.target?.result as string;
          pendingImage = { dataUrl, filename: `paste_${Date.now()}.${item.type.split('/')[1] || 'png'}`, sizeBytes: blob.size };
        };
        reader.readAsDataURL(blob);
        return;
      }
    }

    // Fallback: navigator.clipboard API
    try {
      const clipItems = await navigator.clipboard.read();
      for (const clipItem of clipItems) {
        for (const type of clipItem.types) {
          if (type.startsWith('image/')) {
            e.preventDefault();
            const blob = await clipItem.getType(type);
            const reader = new FileReader();
            reader.onload = (ev) => {
              const dataUrl = ev.target?.result as string;
              pendingImage = { dataUrl, filename: `paste_${Date.now()}.${type.split('/')[1] || 'png'}`, sizeBytes: blob.size };
            };
            reader.readAsDataURL(blob);
            return;
          }
        }
      }
    } catch { /* not supported or denied */ }
  }

  // ---------------------------------------------------------------------------
  // Voice
  // ---------------------------------------------------------------------------
  function handleRecordingComplete(blob: Blob, duration: number, filePath?: string) {
    voicePreview = { blob, duration, filePath };
  }

  async function handleSendVoiceMessage() {
    if (!voicePreview) return;
    const blob = voicePreview.blob;
    const duration = voicePreview.duration;

    try {
      if (activeChatId.startsWith('telegram-')) {
        const base64Audio = await _blobToBase64(blob);
        await sendToTelegram(activeChatId, '', [], base64Audio, duration, blob.type || 'audio/webm');
        chatHistories.update(h => {
          const m = new Map(h);
          const hist = m.get(activeChatId) || [];
          m.set(activeChatId, [...hist, {
            id: crypto.randomUUID(), sender: 'user', text: 'Voice message', timestamp: Date.now(),
            attachments: [{ type: 'voice', filename: `voice_${Date.now()}.${(blob.type || 'audio/webm').split('/')[1]}`, file_path: '', size_bytes: blob.size, mime_type: blob.type || 'audio/webm', voice_metadata: { duration_seconds: duration, sample_rate: 48000, channels: 1, codec: 'opus', recorded_at: new Date().toISOString() } }],
          }]);
          return m;
        });
      } else if (activeChatId.startsWith('group-')) {
        const base64Audio = await _blobToBase64(blob);
        await sendGroupVoiceMessage(activeChatId, base64Audio, duration, blob.type || 'audio/webm');
      } else if (activeChatId === 'local_ai' || activeChatId.startsWith('ai_') || activeChatId.startsWith('agent_')) {
        await handleTranscribeVoiceMessage();
        return;
      } else {
        await sendVoiceMessage(activeChatId, blob, duration);
      }

      const tempFilePath = voicePreview?.filePath;
      if (tempFilePath) {
        try {
          const { remove } = await import('@tauri-apps/plugin-fs');
          await remove(tempFilePath);
        } catch { /* ignore */ }
      }
      voicePreview = null;
    } catch (error) {
      fileOfferToastMessage = `Failed to send voice: ${error}`;
      showFileOfferToast = true;
      setTimeout(() => (showFileOfferToast = false), 5000);
    }
  }

  let isTranscribing = $state(false);

  async function handleTranscribeVoiceMessage() {
    if (!voicePreview) return;
    isTranscribing = true;
    try {
      fileOfferToastMessage = 'Transcribing voice message...';
      showFileOfferToast = true;
      const selectedProviderId = selectedVoiceProvider || selectedTextProvider;
      let transcribeArgs: Record<string, string>;
      if (voicePreview.filePath) {
        transcribeArgs = { file_path: voicePreview.filePath, mime_type: voicePreview.blob.type || 'audio/wav', provider_alias: selectedProviderId };
      } else {
        transcribeArgs = { audio_base64: await _blobToBase64(voicePreview.blob), mime_type: voicePreview.blob.type || 'audio/webm', provider_alias: selectedProviderId };
      }
      const response = await sendCommand('transcribe_audio', transcribeArgs);
      if (response.error) throw new Error(response.error);
      const transcription = response.text || '';
      if (transcription) currentInput = currentInput + (currentInput ? ' ' : '') + transcription;

      const tempFilePath = voicePreview?.filePath;
      if (tempFilePath) {
        try { const { remove } = await import('@tauri-apps/plugin-fs'); await remove(tempFilePath); } catch { /* ignore */ }
      }
      voicePreview = null;
      showFileOfferToast = false;

      const textarea = document.getElementById('message-input') as HTMLTextAreaElement;
      if (textarea) { textarea.focus(); textarea.setSelectionRange(currentInput.length, currentInput.length); }
    } catch (error) {
      fileOfferToastMessage = `Transcription failed: ${error}`;
      showFileOfferToast = true;
      setTimeout(() => (showFileOfferToast = false), 5000);
    } finally {
      isTranscribing = false;
    }
  }

  function handleCancelVoicePreview() {
    if (voicePreview?.filePath) {
      import('@tauri-apps/plugin-fs').then(({ remove }) => { remove(voicePreview!.filePath!).catch(() => {}); });
    }
    voicePreview = null;
  }

  async function _blobToBase64(blob: Blob): Promise<string> {
    const arrayBuffer = await blob.arrayBuffer();
    const uint8Array = new Uint8Array(arrayBuffer);
    let binaryString = '';
    const chunkSize = 8192;
    for (let i = 0; i < uint8Array.length; i += chunkSize) {
      binaryString += String.fromCharCode(...uint8Array.subarray(i, i + chunkSize));
    }
    return btoa(binaryString);
  }

</script>

<!-- Context toggle + file transfer UI + input row -->
<div class="chat-input">
  {#if $aiChats.has(activeChatId) && !activeChatId.startsWith('telegram-')}
    <div class="context-toggle">
      <button
        type="button"
        class="context-toggle-header"
        onclick={() => (contextPanelCollapsed = !contextPanelCollapsed)}
        aria-expanded={!contextPanelCollapsed}
      >
        <span class="context-toggle-title">{contextPanelCollapsed ? '▶' : '▼'} Context Settings</span>
      </button>

      {#if !contextPanelCollapsed}
        <label class="context-checkbox">
          <input id="include-personal-context" name="include-personal-context" type="checkbox" bind:checked={includePersonalContext} />
          <span>
            Include Personal Context (profile, instructions, device info)
            {#if localContextUpdated}<span class="status-badge updated">Updated</span>{/if}
          </span>
        </label>
        {#if !includePersonalContext}
          <span class="context-hint">⚠️ AI won't know your preferences or device specs</span>
        {/if}

        {#if includePersonalContext && availableAIScopes.length > 0}
          <div class="ai-scope-selector">
            <label for="ai-scope-select">AI Context Mode:</label>
            <select id="ai-scope-select" bind:value={selectedAIScope}>
              <option value="">Full Access (no filtering)</option>
              {#each availableAIScopes as scopeName}
                <option value={scopeName}>{scopeName}</option>
              {/each}
            </select>
            <span class="context-hint">
              {#if selectedAIScope}🔒 AI can only access: {selectedAIScope} scope{:else}🔓 AI has full context access{/if}
            </span>
          </div>
        {/if}

        {#if includePersonalContext && (activeChatId === 'local_ai' || activeChatId.startsWith('ai_'))}
          <div class="ai-scope-selector">
            <label for="instruction-set-select">AI Instruction Set:</label>
            <select id="instruction-set-select" value={selectedInstructionSet} onchange={(e) => updateInstructionSet((e.target as HTMLSelectElement).value)}>
              <option value="none">None (No Instructions)</option>
              {#if availableInstructionSets}
                {#each Object.entries(availableInstructionSets.sets) as [key, set]}
                  <option value={key}>{set.name} {availableInstructionSets.default === key ? '⭐' : ''}</option>
                {/each}
              {:else}
                <option value="general">General Purpose</option>
              {/if}
            </select>
            <span class="context-hint">Controls AI behavior and responses</span>
          </div>
        {/if}

        {#if $nodeStatus?.peer_info && $nodeStatus.peer_info.length > 0}
          <div class="peer-context-selector">
            <div class="peer-context-header">
              <span class="peer-context-label">Include Peer Context:</span>
              <span class="peer-context-hint">({selectedPeerContexts.size} selected)</span>
            </div>
            <div class="peer-context-checkboxes">
              {#each $nodeStatus.peer_info as peer}
                {@const displayName = peer.name ? `${peer.name} | ${peer.node_id.slice(0, 15)}...` : `${peer.node_id.slice(0, 20)}...`}
                {@const isPeerContextUpdated = peerContextsUpdated.has(peer.node_id)}
                <label class="peer-context-checkbox">
                  <input
                    id={`peer-context-${peer.node_id}`}
                    name={`peer-context-${peer.node_id}`}
                    type="checkbox"
                    checked={selectedPeerContexts.has(peer.node_id)}
                    onchange={() => togglePeerContext(peer.node_id)}
                  />
                  <span>{displayName}{#if isPeerContextUpdated}<span class="status-badge updated">Updated</span>{/if}</span>
                </label>
              {/each}
            </div>
          </div>
        {/if}
      {/if}
    </div>
  {/if}

  <FileTransferUI
    pendingImage={pendingImage}
    onClearPendingImage={clearPendingImage}
    voicePreview={voicePreview}
    onClearVoicePreview={handleCancelVoicePreview}
    onSendVoiceMessage={handleSendVoiceMessage}
    onTranscribeVoiceMessage={handleTranscribeVoiceMessage}
    {isTranscribing}
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
    onDismissToast={() => (showFileOfferToast = false)}
  />

  <div class="input-row">
    {#if $integrityWarnings && $integrityWarnings.count > 0 && !$integrityWarnings.dismissed}
      <IntegrityWarningBanner
        count={$integrityWarnings.count}
        warnings={$integrityWarnings.warnings}
        onDismiss={() => integrityWarnings.update(w => w ? { ...w, dismissed: true } : w)}
      />
    {/if}

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
      disabled={$connectionStatus !== 'connected' || isLoading || isSleeping}
      oninput={(e) => { groupPanelRef?.handleMentionInput(e); }}
      onkeydown={(e) => {
        if (groupPanelRef?.getMentionVisible() && (e.key === 'ArrowUp' || e.key === 'ArrowDown' || e.key === 'Tab' || e.key === 'Enter' || e.key === 'Escape')) {
          if (e.key === 'Enter' || e.key === 'Tab' || e.key === 'Escape') e.preventDefault();
          return;
        }
        if (e.key === 'Enter' && !e.shiftKey) {
          e.preventDefault();
          if ((isPeerConnected || isTelegramChat || activeChatId === 'local_ai' || activeChatId.startsWith('ai_') || activeChatId.startsWith('agent_') || activeChatId === 'default') && (currentInput.trim() || pendingImage)) {
            handleSendMessage();
          }
        }
      }}
      onpaste={handlePaste}
    ></textarea>

    <VoiceRecorder
      disabled={$connectionStatus !== 'connected' || isLoading || (autoTranscribeEnabled && whisperModelLoading)}
      maxDuration={300}
      onRecordingComplete={handleRecordingComplete}
    />

    <button
      class="file-button"
      onclick={handleSendFile}
      disabled={$connectionStatus !== 'connected' || isLoading || activeChatId === 'local_ai' || activeChatId.startsWith('ai_') || activeChatId.startsWith('agent_') || activeChatId === 'default' || (!isPeerConnected && !isTelegramChat)}
      title={isPeerConnected || isTelegramChat || activeChatId.startsWith('agent_') || activeChatId === 'default' ? 'Send file' : 'Peer disconnected'}
    >📎</button>

    <button
      onclick={handleSendMessage}
      disabled={$connectionStatus !== 'connected' || isLoading || (!currentInput.trim() && !pendingImage) || isContextWindowFull || (!isPeerConnected && !isTelegramChat && activeChatId !== 'local_ai' && !activeChatId.startsWith('ai_') && !activeChatId.startsWith('agent_') && activeChatId !== 'default')}
      title={!isPeerConnected && !isTelegramChat && activeChatId !== 'local_ai' && !activeChatId.startsWith('ai_') && !activeChatId.startsWith('agent_') && activeChatId !== 'default' ? 'Peer disconnected' : ''}
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

<!-- Agent Task Board (needs currentInput + handleSendMessage, so lives in ChatPanel) -->
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
  on:close={() => (showAgentBoard = false)}
/>

<style>
  @import "./panels.css";
</style>
