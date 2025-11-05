<!-- dpc-client/ui/src/routes/+page.svelte -->

<script lang="ts">
  import { tick } from "svelte";
  import { connectionStatus, nodeStatus, coreMessages, p2pMessages, sendCommand } from "$lib/coreService";

  // --- STATE ---
  type Message = { sender: string; text: string; };
  let chatHistories: Map<string, Message[]> = new Map([
    ['local_ai', [{ sender: 'ai', text: 'Hello! How can I help you today?' }]]
  ]);
  let activeChatId: string = 'local_ai';
  let currentInput: string = "";
  let isLoading: boolean = false;
  let chatWindow: HTMLElement;
  let peerUri: string = "";

  // --- LOGIC ---
  async function handleSendMessage() {
    if (!currentInput.trim()) return;
    const text = currentInput;
    const currentHistory = chatHistories.get(activeChatId) || [];
    chatHistories.set(activeChatId, [...currentHistory, { sender: 'user', text }]);
    
    if (activeChatId === 'local_ai') {
      isLoading = true;
      const historyWithLoader = chatHistories.get(activeChatId)!;
      chatHistories.set(activeChatId, [...historyWithLoader, { sender: 'ai', text: 'Thinking...' }]);
      sendCommand("execute_ai_query", { prompt: text });
    } else {
      sendCommand("send_p2p_message", { target_node_id: activeChatId, text });
    }
    currentInput = "";
  }

  function handleConnect() {
    if (!peerUri.trim()) return;
    sendCommand("connect_to_peer", { uri: peerUri });
    peerUri = "";
  }

  function handleDisconnect(nodeId: string) {
    if (confirm(`Are you sure you want to disconnect from ${nodeId}?`)) {
      sendCommand("disconnect_from_peer", { node_id: nodeId });
    }
  }

  function handleReconnect() {
    sendCommand("get_status"); 
  }

  // --- SUBSCRIPTIONS ---
  coreMessages.subscribe(message => {
    if (!message || !message.id) return;
    if (message.command === "execute_ai_query") {
      isLoading = false;
      const history = chatHistories.get('local_ai') || [];
      const newText = message.status === "OK" ? message.payload.content : `Error: ${message.payload.message}`;
      const newSender = message.status === "OK" ? 'ai' : 'system';
      chatHistories.set('local_ai', history.map(m => m.text === 'Thinking...' ? { sender: newSender, text: newText } : m));
    }
  });

  p2pMessages.subscribe(message => {
    if (!message) return;
    const { sender_node_id, text } = message;
    if (!chatHistories.has(sender_node_id)) {
      chatHistories.set(sender_node_id, []);
    }
    const history = chatHistories.get(sender_node_id)!;
    chatHistories.set(sender_node_id, [...history, { sender: sender_node_id, text }]);
  });

  // --- REACTIVE STATEMENTS ---
  $: activeMessages = chatHistories.get(activeChatId) || [];
  
  // This reactive statement will automatically scroll the chat window down
  $: if (activeMessages && chatWindow) {
    // Use tick to wait for the DOM to update before scrolling
    tick().then(() => {
      chatWindow.scrollTop = chatWindow.scrollHeight;
    });
  }
</script>

<main class="container">
  <h1>D-PC Messenger</h1>

  <div class="status-bar">
    <strong>Core Service Status:</strong>
    <span class="status-{$connectionStatus}">{$connectionStatus}</span>
    {#if $connectionStatus !== 'connected'}
      <button on:click={handleReconnect}>Retry Connection</button>
    {/if}
  </div>

  <div class="grid">
    <!-- Left Panel -->
    <div class="panel">
      {#if $connectionStatus === 'connected'}
        <div class="node-info">
          <h2>Your Node</h2>
          {#if $nodeStatus}
            <p><strong>Node ID:</strong> {$nodeStatus.node_id}</p>
            <p><strong>Hub Status:</strong> {$nodeStatus.hub_status}</p>
          {:else}
            <p>Loading node status...</p>
          {/if}
        </div>

        <div class="connection-manager">
          <h3>Connect to Peer</h3>
          <input type="text" bind:value={peerUri} placeholder="dpc://..." />
          <button on:click={handleConnect}>Connect</button>
        </div>

        <div class="peer-list">
          <h3>Chats</h3>
          <ul>
            <li>
              <button class:active={activeChatId === 'local_ai'} on:click={() => activeChatId = 'local_ai'}>
                <span>🤖 Local AI Assistant</span>
              </button>
            </li>
            <!-- THE CORE FIX: This block will now correctly re-render -->
            {#if $nodeStatus && $nodeStatus.p2p_peers}
              {#each $nodeStatus.p2p_peers as peerId (peerId)}
                <li>
                  <button class:active={activeChatId === peerId} on:click={() => activeChatId = peerId}>
                    <span title={peerId}>👤 {peerId.slice(0, 15)}...</span>
                  </button>
                  <button class="disconnect-btn" on:click|stopPropagation={() => handleDisconnect(peerId)}>
                    &times;
                  </button>
                </li>
              {/each}
            {/if}
          </ul>
        </div>
      {:else}
        <p class="error">
          Could not connect to the D-PC Core Service. Please ensure it is running.
        </p>
      {/if}
    </div>

    <!-- Right Panel -->
    <div class="panel">
      <h2>Chat with: <span class="chat-title">{activeChatId}</span></h2>
      <div class="chat-window" bind:this={chatWindow}>
        {#each activeMessages as msg, i (i)}
          <div class="message" class:user={msg.sender === 'user'}>
            <strong>{msg.sender === 'user' ? 'You' : msg.sender}:</strong>
            <p>{msg.text}</p>
          </div>
        {/each}
      </div>
      <div class="chat-input">
        <textarea bind:value={currentInput} on:keydown={(e) => e.key === 'Enter' && !e.shiftKey && (e.preventDefault(), handleSendMessage())} disabled={isLoading && activeChatId === 'local_ai'}></textarea>
        <button on:click={handleSendMessage} disabled={isLoading && activeChatId === 'local_ai'}>
          {#if isLoading && activeChatId === 'local_ai'}Sending...{:else}Send{/if}
        </button>
      </div>
    </div>
  </div>
</main>

<style>
  :root {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen,
      Ubuntu, Cantarell, 'Open Sans', 'Helvetica Neue', sans-serif;
  }
  .container { padding: 2rem; max-width: 1200px; margin: 0 auto; }
  h1, h2, h3 { text-align: center; }
  h2, h3 { text-align: left; }
  .status-bar { margin-bottom: 2rem; padding: 1rem; border: 1px solid #ccc; border-radius: 8px; background-color: #f9f9f9; text-align: center; }
  .status-connected { color: #28a745; font-weight: bold; }
  .status-disconnected, .status-error { color: #dc3545; font-weight: bold; }
  .status-connecting { color: #ffc107; font-weight: bold; }
  .grid { display: grid; grid-template-columns: 300px 1fr; gap: 2rem; margin-top: 2rem; }
  .panel { border: 1px solid #eee; border-radius: 8px; padding: 1.5rem; }
  .node-info, .connection-manager { margin-bottom: 1.5rem; }
  .connection-manager input { width: 100%; box-sizing: border-box; padding: 0.5rem; margin-bottom: 0.5rem; }
  .peer-list ul { list-style: none; padding: 0; margin-top: 1rem; }
  .peer-list li { 
    display: flex; 
    justify-content: space-between; 
    align-items: center; 
    padding: 0.75rem; 
    border-radius: 6px;
    cursor: pointer; 
    transition: background-color 0.2s;
  }
  .peer-list li:hover { background-color: #f0f0f0; }
  .peer-list li.active { background-color: #e0e7ff; font-weight: bold; }
  .peer-list li span { word-break: break-all; }
  .disconnect-btn { 
    background: none; 
    border: none; 
    color: #999; 
    cursor: pointer; 
    font-size: 1.2rem;
    padding: 0 0.5rem;
  }
  .disconnect-btn:hover { color: #dc3545; }
  .error { color: #dc3545; }
  .chat-title { font-family: monospace; font-size: 0.9em; color: #555; }
  .chat-window { height: 500px; border: 1px solid #ccc; padding: 1rem; background-color: #f9f9f9; overflow-y: auto; display: flex; flex-direction: column; gap: 1rem; }
  .message { padding: 0.75rem 1rem; border-radius: 12px; max-width: 80%; line-height: 1.4; }
  .message p { margin: 0; white-space: pre-wrap; }
  .message strong { display: block; margin-bottom: 0.25rem; font-size: 0.8em; color: #666; }
  .message.user { background-color: #dcf8c6; align-self: flex-end; }
  .message:not(.user) { background-color: #fff; align-self: flex-start; border: 1px solid #eee; }
  .message.system { background-color: #fff0f0; align-self: center; font-style: italic; border-color: #ffc0c0; }
  .chat-input { display: flex; margin-top: 1rem; gap: 0.5rem; }
  .chat-input textarea { flex-grow: 1; padding: 0.75rem; resize: none; border-radius: 8px; border: 1px solid #ccc; font-family: inherit; font-size: 1rem; }
  .chat-input button { padding: 0.75rem 1.5rem; border-radius: 8px; border: none; background-color: #007bff; color: white; cursor: pointer; }
  .chat-input button:disabled { background-color: #a0a0a0; }
    .peer-list li button {
    background: none;
    border: none;
    padding: 0;
    margin: 0;
    font: inherit;
    cursor: pointer;
    text-align: left;
    width: 100%;
    display: block;
    padding: 0.75rem;
    border-radius: 6px;
  }
  .peer-list li button:hover {
    background-color: #f0f0f0;
  }
  .peer-list li button.active {
    background-color: #e0e7ff;
    font-weight: bold;
  }
  .peer-list li {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
</style>