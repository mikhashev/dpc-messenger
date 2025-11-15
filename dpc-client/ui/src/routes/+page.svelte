<!-- dpc-client/ui/src/routes/+page.svelte -->
<!-- FIXED VERSION - Proper URI detection for Direct TLS vs WebRTC -->

<script lang="ts">
  import { writable } from "svelte/store";
  import { connectionStatus, nodeStatus, coreMessages, p2pMessages, sendCommand, resetReconnection, connectToCoreService, knowledgeCommitProposal, personalContext, availableProviders, peerProviders } from "$lib/coreService";
  import KnowledgeCommitDialog from "$lib/components/KnowledgeCommitDialog.svelte";
  import ContextViewer from "$lib/components/ContextViewer.svelte";
  
  console.log("Full D-PC Messenger loading...");
  
  // --- STATE ---
  type Message = { 
    id: string; 
    sender: string; 
    text: string; 
    timestamp: number;
    commandId?: string;
  };
  const chatHistories = writable<Map<string, Message[]>>(new Map([
    ['local_ai', [{ id: crypto.randomUUID(), sender: 'ai', text: 'Hello! I am your local AI assistant. How can I help you today?', timestamp: Date.now() }]]
  ]));
  
  let activeChatId: string = 'local_ai';
  let currentInput: string = "";
  let isLoading: boolean = false;
  let chatWindow: HTMLElement;
  let peerInput: string = "";  // RENAMED from peerUri for clarity
  let selectedComputeHost: string = "local";  // "local" or node_id for remote inference
  let selectedRemoteModel: string = "";  // Selected model when using remote compute host

  // Store provider selection per chat (chatId -> provider alias)
  const chatProviders = writable<Map<string, string>>(new Map());

  // Store AI chat metadata (chatId -> {name: string, provider: string})
  const aiChats = writable<Map<string, {name: string, provider: string}>>(
    new Map([['local_ai', {name: 'Local AI Assistant', provider: ''}]])
  );

  // Track which chat each AI command belongs to (commandId -> chatId)
  let commandToChatMap = new Map<string, string>();

  let processedMessageIds = new Set<string>();

  // Knowledge Architecture UI state
  let showContextViewer: boolean = false;
  let showCommitDialog: boolean = false;
  let autoKnowledgeDetection: boolean = true;  // Default: enabled

  // Add AI Chat dialog state
  let showAddAIChatDialog: boolean = false;
  let selectedProviderForNewChat: string = "";

  // Reactive: Open commit dialog when proposal received
  $: if ($knowledgeCommitProposal) {
    showCommitDialog = true;
  }
  
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
  
  function getPeerDisplayName(peerId: string): string {
    if (!$nodeStatus || !$nodeStatus.peer_info) {
      return peerId.slice(0, 20) + '...';
    }

    const peerInfo = $nodeStatus.peer_info.find((p: { node_id: string; name?: string }) => p.node_id === peerId);
    if (peerInfo && peerInfo.name) {
      // Show: "Name (dpc-node-abc123...)"
      return `${peerInfo.name} (${peerId.slice(0, 16)}...)`;
    }

    // Just show truncated node_id if no name
    return peerId.slice(0, 20) + '...';
  }
  
  function getAIModelDisplay(): string {
    if (!$nodeStatus) {
      return '🤖 Local AI Assistant';
    }
    
    if ($nodeStatus.active_ai_model) {
      return `🤖 Local AI Assistant (${$nodeStatus.active_ai_model})`;
    }
    
    return '🤖 Local AI Assistant';
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
      const payload: any = { prompt: text };

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
  // FIXED: Proper detection of dpc:// URI vs node_id
  function handleConnectPeer() {
    if (!peerInput.trim()) return;
    
    const input = peerInput.trim();
    console.log("Connecting to peer:", input);
    
    // Detect if input is a dpc:// URI (Direct TLS) or just a node_id (WebRTC via Hub)
    if (input.startsWith('dpc://')) {
      // Direct TLS connection
      console.log("Using Direct TLS connection");
      sendCommand("connect_to_peer", { uri: input });
    } else {
      // WebRTC connection via Hub (just node_id)
      console.log("Using WebRTC connection via Hub");
      sendCommand("connect_to_peer_by_id", { node_id: input });
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

  function handleEndSession(conversationId: string) {
    if (confirm("End this conversation session and extract knowledge?")) {
      sendCommand("end_conversation_session", {
        conversation_id: conversationId
      });
    }
  }

  function toggleAutoKnowledgeDetection() {
    // bind:checked already updates the variable, just sync to backend
    sendCommand("toggle_auto_knowledge_detection", {
      enabled: autoKnowledgeDetection
    });
  }

  function handleNewChat(chatId: string) {
    if (confirm("Start a new conversation? This will clear the current chat history and knowledge buffer.")) {
      // Clear message history for this chat
      chatHistories.update(h => {
        const newMap = new Map(h);
        newMap.set(chatId, []);  // Clear the message array for this chat
        return newMap;
      });

      // Backend will create a new monitor on next message
      // (Old monitor's buffer was already cleared by previous extraction)
    }
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
      newMap.set(chatId, [{
        id: crypto.randomUUID(),
        sender: 'ai',
        text: `Hello! I'm powered by ${chatName}. How can I help you today?`,
        timestamp: Date.now()
      }]);
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

  function handleDeleteAIChat(chatId: string) {
    if (chatId === 'local_ai') {
      alert("Cannot delete the default Local AI chat.");
      return;
    }

    if (confirm("Delete this AI chat? This will permanently remove the chat history.")) {
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

      const responseCommandId = message.id;

      // Find which chat this command belongs to
      const chatId = commandToChatMap.get(responseCommandId);
      if (chatId) {
        chatHistories.update(h => {
          const newMap = new Map(h);
          const hist = newMap.get(chatId) || [];
          newMap.set(chatId, hist.map(m =>
            m.commandId === responseCommandId ? { ...m, sender: newSender, text: newText, commandId: undefined } : m
          ));
          return newMap;
        });

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
        const hist = newMap.get(msg.sender_node_id) || [];
        newMap.set(msg.sender_node_id, [...hist, {
          id: crypto.randomUUID(),
          sender: msg.sender_node_id,
          text: msg.text,
          timestamp: Date.now()
        }]);
        return newMap;
      });
      
      if (wasNearBottom || activeChatId === msg.sender_node_id) {
        autoScroll();
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
  <h1>D-PC Messenger</h1>

  <!-- Status Bar -->
  <div class="status-bar">
    {#if $connectionStatus === 'connected'}
      <span class="status-connected">Status: connected</span>
    {:else if $connectionStatus === 'connecting'}
      <span class="status-connecting">Status: connecting...</span>
    {:else if $connectionStatus === 'error'}
      <span class="status-error">Status: error</span>
      <button class="btn-small" on:click={handleReconnect}>Retry</button>
    {:else}
      <span class="status-disconnected">Status: disconnected</span>
      <button class="btn-small" on:click={handleReconnect}>Connect</button>
    {/if}
  </div>

  <div class="grid">
    <!-- Sidebar -->
    <div class="sidebar">
      {#if $connectionStatus === 'connected' && $nodeStatus}
        <!-- Node Info -->
        <div class="node-info">
          <h2>Your Node</h2>
          <p><strong>ID:</strong></p>
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
          <p>
            <strong>Hub:</strong>
            <span class:hub-connected={$nodeStatus.hub_status === 'Connected'}>
              {$nodeStatus.hub_status}
            </span>
          </p>

          <!-- Direct TLS Connection URIs (NEW - Redesigned) -->
          {#if $nodeStatus.dpc_uris && $nodeStatus.dpc_uris.length > 0}
            <div class="dpc-uris-section">
              <details class="uri-details" open>
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
                      <li class:feature-available={available} class:feature-unavailable={!available}>
                        {available ? '✓' : '✗'} {feature.replace(/_/g, ' ')}
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
        </div>

        <!-- Personal Context Button (Knowledge Architecture) -->
        <div class="context-section">
          <button class="btn-context" on:click={loadPersonalContext}>
            📚 View Personal Context
          </button>

          <!-- Auto Knowledge Detection Toggle -->
          <div class="knowledge-toggle">
            <label class="toggle-container">
              <input
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

        <!-- Connect to Peer - UPDATED PLACEHOLDER -->
        <div class="connect-section">
          <h3>Connect to Peer</h3>
          <input 
            type="text" 
            bind:value={peerInput}
            placeholder="dpc://... or node_id"
            on:keydown={(e) => e.key === 'Enter' && handleConnectPeer()}
          />
          <button on:click={handleConnectPeer}>Connect</button>
          <p class="small">
            💡 Tip: Enter just node_id for WebRTC, or full dpc:// URI for Direct TLS
          </p>
        </div>

        <!-- Hub Login -->
        {#if $nodeStatus.hub_status !== 'Connected'}
          <div class="hub-section">
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
                  class="chat-button"
                  class:active={activeChatId === chatId}
                  on:click={() => activeChatId = chatId}
                  title={chatInfo.provider ? `Provider: ${chatInfo.provider}` : 'Default AI Assistant'}
                >
                  🤖 {chatInfo.name}
                </button>
                {#if chatId !== 'local_ai'}
                  <button
                    class="disconnect-btn"
                    on:click={() => handleDeleteAIChat(chatId)}
                    title="Delete AI chat"
                  >
                    ×
                  </button>
                {/if}
              </li>
            {/each}

            <!-- P2P Peer Chats -->
            {#if $nodeStatus.p2p_peers && $nodeStatus.p2p_peers.length > 0}
              {#each $nodeStatus.p2p_peers as peerId (peerId)}
                <li class="peer-item">
                  <button 
                    class="chat-button" 
                    class:active={activeChatId === peerId}
                    on:click={() => activeChatId = peerId}
                    title={peerId}
                  >
                    👤 {getPeerDisplayName(peerId)}
                  </button>
                  <button 
                    class="disconnect-btn" 
                    on:click={() => handleDisconnectPeer(peerId)}
                    title="Disconnect"
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
    <div class="chat-panel">
      <div class="chat-header">
        <div class="chat-title-section">
          <h2>
            {#if $aiChats.has(activeChatId)}
              🤖 {$aiChats.get(activeChatId).name}
            {:else}
              👤 Chat with {getPeerDisplayName(activeChatId)}
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
              {#if $availableProviders.providers.length === 1}
                <span class="provider-hint">(Configure more in ~/.dpc/providers.toml)</span>
              {/if}
            </div>
          {/if}
        </div>

        <div class="chat-actions">
          <button class="btn-new-chat" on:click={() => handleNewChat(activeChatId)}>
            🔄 New Chat
          </button>
          <button class="btn-end-session" on:click={() => handleEndSession(activeChatId)}>
            📚 End Session & Save Knowledge
          </button>
        </div>
      </div>

      <div class="chat-window" bind:this={chatWindow}>
        {#if activeMessages.length > 0}
          {#each activeMessages as msg (msg.id)}
            <div class="message" class:user={msg.sender === 'user'} class:system={msg.sender === 'system'}>
              <div class="message-header">
                <strong>{msg.sender === 'user' ? 'You' : msg.sender}</strong>
                <span class="timestamp">{new Date(msg.timestamp).toLocaleTimeString()}</span>
              </div>
              <p>{msg.text}</p>
            </div>
          {/each}
        {:else}
          <div class="empty-chat">
            <p>No messages yet. Start the conversation!</p>
          </div>
        {/if}
      </div>

      <div class="chat-input">
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
                      ? `${peer.name} (${peer.node_id.slice(0, 16)}...)`
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
            bind:value={currentInput}
            placeholder={$connectionStatus === 'connected' ? 'Type a message... (Enter to send, Shift+Enter for new line)' : 'Connect to Core Service first...'}
            disabled={$connectionStatus !== 'connected' || isLoading}
            on:keydown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                handleSendMessage();
              }
            }}
          ></textarea>
          <button
            on:click={handleSendMessage}
            disabled={$connectionStatus !== 'connected' || isLoading || !currentInput.trim()}
          >
            {#if isLoading}Sending...{:else}Send{/if}
          </button>
        </div>
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

<ContextViewer
  bind:open={showContextViewer}
  context={$personalContext}
  on:close={() => showContextViewer = false}
/>

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
    max-width: 1400px;
    margin: 0 auto;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  }
  
  h1 {
    text-align: center;
    font-size: 2.5rem;
    margin-bottom: 1.5rem;
    color: #1a1a1a;
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
    grid-template-columns: 320px 1fr;
    gap: 1.5rem;
  }
  
  @media (max-width: 968px) {
    .grid { grid-template-columns: 1fr; }
  }
  
  .sidebar {
    display: flex;
    flex-direction: column;
    gap: 1rem;
    overflow-y: auto;
    max-height: 100vh;
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
  
  .node-info, .connect-section, .hub-section, .context-section, .chat-list {
    background: white;
    border: 1px solid #e0e0e0;
    border-radius: 8px;
    padding: 1rem;
  }

  .node-info {
    max-height: 60vh;
    overflow-y: auto;
  }

  .node-info::-webkit-scrollbar {
    width: 4px;
  }

  .node-info::-webkit-scrollbar-track {
    background: transparent;
  }

  .node-info::-webkit-scrollbar-thumb {
    background: #ccc;
    border-radius: 2px;
  }
  
  h2, h3 {
    margin: 0 0 0.75rem 0;
    font-size: 1.1rem;
    border-bottom: 1px solid #eee;
    padding-bottom: 0.5rem;
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
  
  .hub-connected {
    color: #28a745;
    font-weight: 600;
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
    text-align: left;
    background: transparent;
    color: #333;
    border: 1px solid transparent;
    padding: 0.6rem;
    transition: all 0.2s;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1;
  }
  
  .chat-button:hover {
    background: #f0f0f0;
  }
  
  .chat-button.active {
    background: #e0e7ff;
    border-color: #c7d2fe;
    font-weight: bold;
  }
  
  .disconnect-btn {
    width: auto;
    padding: 0.3rem 0.6rem;
    background: transparent;
    color: #999;
    font-size: 1.5rem;
    border: 1px solid transparent;
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
    height: calc(100vh - 200px);
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

  .provider-hint {
    font-size: 0.75rem;
    color: #888;
    font-style: italic;
    margin-left: 0.5rem;
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

  .chat-window {
    flex: 1;
    padding: 1rem;
    overflow-y: auto;
    background: #f9f9f9;
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
  }
  
  .chat-input {
    padding: 1rem;
    border-top: 1px solid #eee;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
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
  }

  .input-row textarea {
    flex: 1;
    min-height: 60px;
    max-height: 120px;
    resize: vertical;
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
</style>