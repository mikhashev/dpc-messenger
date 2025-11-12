<!-- dpc-client/ui/src/routes/+page.svelte -->
<!-- FIXED VERSION - Proper URI detection for Direct TLS vs WebRTC -->

<script lang="ts">
  import { writable } from "svelte/store";
  import { connectionStatus, nodeStatus, coreMessages, p2pMessages, sendCommand, resetReconnection, connectToCoreService } from "$lib/coreService";
  
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
  
  let processedMessageIds = new Set<string>();
  
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
      return peerId;
    }
    
    const peerInfo = $nodeStatus.peer_info.find(p => p.node_id === peerId);
    if (peerInfo && peerInfo.name) {
      return `${peerInfo.name} | ${peerId}`;
    }
    
    return peerId;
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
    
    if (activeChatId === 'local_ai') {
      isLoading = true;
      const commandId = crypto.randomUUID();
      
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
      
      const success = sendCommand("execute_ai_query", { prompt: text }, commandId);
      if (!success) {
        isLoading = false;
        chatHistories.update(h => {
          const newMap = new Map(h);
          const hist = newMap.get('local_ai') || [];
          newMap.set('local_ai', hist.map(m => 
            m.commandId === commandId ? { ...m, sender: 'system', text: 'Error: Not connected' } : m
          ));
          return newMap;
        });
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
    if (confirm(`Disconnect from ${nodeId.slice(0, 15)}...?`)) {
      sendCommand("disconnect_from_peer", { node_id: nodeId });
      if (activeChatId === nodeId) {
        activeChatId = 'local_ai';
      }
    }
  }
  
  function handleReconnect() {
    resetReconnection();
    connectToCoreService();
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
      
      chatHistories.update(h => {
        const newMap = new Map(h);
        const hist = newMap.get('local_ai') || [];
        newMap.set('local_ai', hist.map(m => 
          m.commandId === responseCommandId ? { ...m, sender: newSender, text: newText, commandId: undefined } : m
        ));
        return newMap;
      });
      
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
        processedMessageIds.delete(firstId);
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
            <p class="info-text">Optional: Connect to Hub for discovery</p>
            <button on:click={() => sendCommand('login_to_hub')} class="btn-secondary">
              Login to Hub
            </button>
          </div>
        {/if}

        <!-- Chat List -->
        <div class="chat-list">
          <h3>Chats</h3>
          <ul>
            <li>
              <button 
                class="chat-button" 
                class:active={activeChatId === 'local_ai'}
                on:click={() => activeChatId = 'local_ai'}
              >
                {getAIModelDisplay()}
              </button>
            </li>
            
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
        <h2>
          {#if activeChatId === 'local_ai'}
            {getAIModelDisplay()}
          {:else}
            👤 Chat with {getPeerDisplayName(activeChatId)}
          {/if}
        </h2>
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
</main>

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
  }
  
  .node-info, .connect-section, .hub-section, .chat-list {
    background: white;
    border: 1px solid #e0e0e0;
    border-radius: 8px;
    padding: 1rem;
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
  
  .btn-secondary {
    background: #6c757d;
  }
  
  .btn-secondary:hover {
    background: #545b62;
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
    padding: 1rem;
    border-bottom: 1px solid #eee;
  }
  
  .chat-header h2 {
    margin: 0;
    border: none;
    padding: 0;
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
    gap: 0.5rem;
  }
  
  .chat-input textarea {
    flex: 1;
    min-height: 60px;
    max-height: 120px;
    resize: vertical;
  }
  
  .chat-input button {
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
</style>