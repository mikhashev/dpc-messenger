<script lang="ts">
  import { onMount } from "svelte";
  import { connectionStatus, nodeStatus, coreMessages, sendCommand } from "$lib/coreService";
  import { tick } from "svelte";

  // --- CHAT STATE ---
  type Message = {
    sender: 'user' | 'ai' | 'system';
    text: string;
  };
  let messages: Message[] = [
    { sender: 'ai', text: 'Hello! How can I help you today?' }
  ];
  let currentInput: string = "";
  let isLoading: boolean = false;
  let chatWindow: HTMLElement;

  async function handleSendMessage() {
    if (!currentInput.trim() || isLoading) return;

    const text = currentInput;
    messages = [...messages, { sender: 'user', text }];
    
    messages = [...messages, { sender: 'ai', text: 'Thinking...' }];
    isLoading = true;
    currentInput = "";

    await tick(); // Wait for the DOM to update
    chatWindow.scrollTop = chatWindow.scrollHeight; // Scroll to bottom

    sendCommand("execute_ai_query", { prompt: text });
  }

  // --- CONNECTION MANAGEMENT STATE ---
  let peerUri: string = "";

  function handleConnect() {
    if (!peerUri) {
      alert("Please enter a peer URI.");
      return;
    }
    sendCommand("connect_to_peer", { uri: peerUri });
    peerUri = "";
  }

  function handleDisconnect(nodeId: string) {
    if (confirm(`Are you sure you want to disconnect from ${nodeId}?`)) {
      sendCommand("disconnect_from_peer", { node_id: nodeId });
    }
  }

  function handleReconnect() {
    // This will trigger a reconnect if needed by the singleton logic
    sendCommand("get_status"); 
  }

  // --- WebSocket Message Handling ---
  coreMessages.subscribe(async message => {
    if (!message) return;

    if (message.command === "execute_ai_query") {
      isLoading = false;
      if (message.status === "OK") {
        messages = messages.map(m => 
          m.sender === 'ai' && m.text === 'Thinking...' 
            ? { sender: 'ai', text: message.payload.content }
            : m
        );
      } else { // Handle ERROR status
        messages = messages.map(m => 
          m.sender === 'ai' && m.text === 'Thinking...' 
            ? { sender: 'system', text: `Error: ${message.payload.message}` }
            : m
        );
      }
      await tick();
      chatWindow.scrollTop = chatWindow.scrollHeight;
    }
  });

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
    <!-- Left Panel: Node Info and Connection Management -->
    <div class="panel">
      {#if $connectionStatus === 'connected' && $nodeStatus}
        <div class="node-info">
          <h2>Your Node</h2>
          <p><strong>Node ID:</strong> {$nodeStatus.node_id}</p>
          <p><strong>Hub Status:</strong> {$nodeStatus.hub_status}</p>
        </div>

        <div class="connection-manager">
          <h3>Connect to Peer</h3>
          <input type="text" bind:value={peerUri} placeholder="dpc://..." />
          <button on:click={handleConnect}>Connect</button>
        </div>

        <div class="peer-list">
          <h3>Connected Peers ({$nodeStatus.p2p_peers.length})</h3>
          {#if $nodeStatus.p2p_peers.length > 0}
            <ul>
              {#each $nodeStatus.p2p_peers as peerId}
                <li>
                  <span>{peerId}</span>
                  <button class="disconnect-btn" on:click={() => handleDisconnect(peerId)}>
                    Disconnect
                  </button>
                </li>
              {/each}
            </ul>
          {:else}
            <p>No active connections.</p>
          {/if}
        </div>
      {:else if $connectionStatus === 'connecting'}
        <p>Connecting to backend service...</p>
      {:else if $connectionStatus === 'error' || $connectionStatus === 'disconnected'}
        <p class="error">
          Could not connect to the D-PC Core Service. Please ensure it is running.
        </p>
      {/if}
    </div>

    <!-- Right Panel: Chat Interface -->
    <div class="panel">
      <h2>Chat with Local AI</h2>
      <div class="chat-window" bind:this={chatWindow}>
        {#each messages as msg, i (i)}
          <div class="message {msg.sender}">
            <strong>{msg.sender.toUpperCase()}:</strong>
            <p>{msg.text}</p>
          </div>
        {/each}
      </div>
      <div class="chat-input">
        <textarea
          bind:value={currentInput}
          placeholder="Type your message..."
          on:keydown={(e) => e.key === 'Enter' && !e.shiftKey && (e.preventDefault(), handleSendMessage())}
          disabled={isLoading}
        ></textarea>
        <button on:click={handleSendMessage} disabled={isLoading}>
          {#if isLoading}Sending...{:else}Send{/if}
        </button>
      </div>
    </div>
  </div>

</main>

<style>
  .container { padding: 2rem; font-family: sans-serif; }
  h1, h2, h3 { text-align: center; }
  h2, h3 { text-align: left; }
  .status-bar { margin-bottom: 2rem; padding: 1rem; border: 1px solid #ccc; border-radius: 8px; background-color: #f9f9f9; text-align: center; }
  .status-connected { color: green; font-weight: bold; }
  .status-disconnected, .status-error { color: red; font-weight: bold; }
  .status-connecting { color: orange; font-weight: bold; }
  .grid { display: grid; grid-template-columns: 1fr 2fr; gap: 2rem; margin-top: 2rem; }
  .panel { border: 1px solid #eee; border-radius: 8px; padding: 1rem; text-align: left; }
  .connection-manager input { width: 100%; box-sizing: border-box; padding: 0.5rem; margin-bottom: 0.5rem; }
  .peer-list ul { list-style: none; padding: 0; }
  .peer-list li { display: flex; justify-content: space-between; align-items: center; padding: 0.5rem; border-bottom: 1px solid #eee; }
  .peer-list li span { word-break: break-all; margin-right: 1rem; }
  .disconnect-btn { background-color: #ffcccc; border: 1px solid red; color: red; cursor: pointer; }
  .error { color: red; }
  .chat-window { height: 400px; border: 1px solid #ccc; padding: 1rem; background-color: #f9f9f9; overflow-y: auto; display: flex; flex-direction: column; }
  .message { margin-bottom: 1rem; padding: 0.5rem 1rem; border-radius: 8px; max-width: 80%; }
  .message p { margin: 0; white-space: pre-wrap; }
  .message.user { background-color: #dcf8c6; align-self: flex-end; }
  .message.ai { background-color: #fff; align-self: flex-start; }
  .message.system { background-color: #fdd; align-self: center; font-style: italic; }
  .chat-input { display: flex; margin-top: 1rem; }
  .chat-input textarea { flex-grow: 1; padding: 0.5rem; resize: none; border-radius: 4px; border: 1px solid #ccc; }
  .chat-input button { margin-left: 0.5rem; padding: 0.5rem 1rem; }
</style>