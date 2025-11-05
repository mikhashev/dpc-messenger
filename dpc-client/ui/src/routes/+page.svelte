<!-- dpc-client/ui/src/routes/+page.svelte -->

<script lang="ts">
  import { writable } from "svelte/store";
  import { connectionStatus, nodeStatus, coreMessages, p2pMessages, sendCommand, resetReconnection, connectToCoreService } from "$lib/coreService";
  
  console.log("Full D-PC Messenger loading...");
  
  // --- STATE ---
  type Message = { sender: string; text: string; timestamp: number };
  const chatHistories = writable<Map<string, Message[]>>(new Map([
    ['local_ai', [{ sender: 'ai', text: 'Hello! I am your local AI assistant. How can I help you today?', timestamp: Date.now() }]]
  ]));
  
  let activeChatId: string = 'local_ai';
  let currentInput: string = "";
  let isLoading: boolean = false;
  let chatWindow: HTMLElement;
  let peerUri: string = "";
  
  // --- CHAT FUNCTIONS ---
  function handleSendMessage() {
    if (!currentInput.trim()) return;
    
    const text = currentInput.trim();
    currentInput = "";
    
    // Add user message
    chatHistories.update(h => {
      const hist = h.get(activeChatId) || [];
      h.set(activeChatId, [...hist, { sender: 'user', text, timestamp: Date.now() }]);
      return h;
    });
    
    if (activeChatId === 'local_ai') {
      // AI query
      isLoading = true;
      chatHistories.update(h => {
        const hist = h.get(activeChatId)!;
        h.set(activeChatId, [...hist, { sender: 'ai', text: 'Thinking...', timestamp: Date.now() }]);
        return h;
      });
      
      const success = sendCommand("execute_ai_query", { prompt: text });
      if (!success) {
        isLoading = false;
        chatHistories.update(h => {
          const hist = h.get('local_ai') || [];
          h.set('local_ai', hist.map(m => 
            m.text === 'Thinking...' ? { sender: 'system', text: 'Error: Not connected', timestamp: Date.now() } : m
          ));
          return h;
        });
      }
    } else {
      // P2P message
      sendCommand("send_p2p_message", { target_node_id: activeChatId, text });
    }
    
    // Auto-scroll
    setTimeout(() => {
      if (chatWindow) {
        chatWindow.scrollTop = chatWindow.scrollHeight;
      }
    }, 100);
  }
  
  // --- PEER CONNECTION FUNCTIONS ---
  function handleConnectPeer() {
    if (!peerUri.trim()) return;
    console.log("Connecting to peer:", peerUri);
    sendCommand("connect_to_peer", { uri: peerUri });
    peerUri = "";
  }
  
  function handleDisconnectPeer(nodeId: string) {
    if (confirm(`Disconnect from ${nodeId.slice(0, 15)}...?`)) {
      sendCommand("disconnect_from_peer", { node_id: nodeId });
      // Switch to local_ai if we were chatting with this peer
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
      
      chatHistories.update(h => {
        const hist = h.get('local_ai') || [];
        h.set('local_ai', hist.map(m => 
          m.text === 'Thinking...' ? { sender: newSender, text: newText, timestamp: Date.now() } : m
        ));
        return h;
      });
      
      // Auto-scroll
      setTimeout(() => {
        if (chatWindow) {
          chatWindow.scrollTop = chatWindow.scrollHeight;
        }
      }, 100);
    }
  }
  
  $: if ($p2pMessages) {
    const message = $p2pMessages;
    const { sender_node_id, text } = message;
    
    if (sender_node_id && text) {
      chatHistories.update(h => {
        if (!h.has(sender_node_id)) {
          h.set(sender_node_id, []);
        }
        const hist = h.get(sender_node_id)!;
        h.set(sender_node_id, [...hist, { sender: sender_node_id, text, timestamp: Date.now() }]);
        return h;
      });
      
      // Auto-scroll if this is the active chat
      if (activeChatId === sender_node_id) {
        setTimeout(() => {
          if (chatWindow) {
            chatWindow.scrollTop = chatWindow.scrollHeight;
          }
        }, 100);
      }
    }
  }
  
  $: activeMessages = $chatHistories.get(activeChatId) || [];
</script>

<main class="container">
  <h1>D-PC Messenger</h1>

  <!-- Status Bar -->
  <div class="status-bar">
    <strong>Status:</strong>
    <span class="status-{$connectionStatus}">{$connectionStatus}</span>
    {#if $connectionStatus !== 'connected'}
      <button on:click={handleReconnect} class="btn-small">Reconnect</button>
    {/if}
  </div>

  <div class="grid">
    <!-- Left Sidebar -->
    <div class="sidebar">
      {#if $connectionStatus === 'connected' && $nodeStatus}
        <!-- Node Info -->
        <div class="node-info">
          <h2>Your Node</h2>
          <p class="node-id" title={$nodeStatus.node_id}>
            <strong>ID:</strong> {$nodeStatus.node_id.slice(0, 20)}...
          </p>
          <p>
            <strong>Hub:</strong> 
            <span class:hub-connected={$nodeStatus.hub_status === 'Connected'}>
              {$nodeStatus.hub_status}
            </span>
          </p>
        </div>

        <!-- Direct Peer Connection -->
        <div class="connect-section">
          <h3>Connect to Peer</h3>
          <input 
            type="text" 
            bind:value={peerUri} 
            placeholder="dpc://... or node_id"
            on:keydown={(e) => e.key === 'Enter' && handleConnectPeer()}
          />
          <button on:click={handleConnectPeer}>Connect</button>
        </div>

        <!-- Optional Hub Login -->
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
                🤖 Local AI Assistant
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
                    👤 {peerId.slice(0, 12)}...
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
            🤖 Local AI Assistant
          {:else}
            👤 Chat with {activeChatId.slice(0, 15)}...
          {/if}
        </h2>
      </div>

      <div class="chat-window" bind:this={chatWindow}>
        {#if activeMessages.length > 0}
          {#each activeMessages as msg (msg.timestamp)}
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
  
  /* Status Bar */
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
  
  /* Grid Layout */
  .grid {
    display: grid;
    grid-template-columns: 320px 1fr;
    gap: 1.5rem;
  }
  
  @media (max-width: 968px) {
    .grid { grid-template-columns: 1fr; }
  }
  
  /* Sidebar */
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
  }
  
  .hub-connected {
    color: #28a745;
    font-weight: 600;
  }
  
  .info-text {
    font-size: 0.9rem;
    color: #666;
    margin-bottom: 0.5rem;
    font-style: italic;
  }
  
  /* Inputs and Buttons */
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
  
  /* Chat List */
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
  
  /* Chat Panel */
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
  
  /* Messages */
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
  
  /* Chat Input */
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
  
  /* Utility */
  .connecting, .error {
    text-align: center;
    padding: 2rem;
  }
  
  .small {
    font-size: 0.85rem;
    color: #666;
  }
</style>