<!-- dpc-client/ui/src/App.svelte -->

<script lang="ts">
  import { connectionStatus, nodeStatus, connectToCoreService, sendCommand } from "$lib/coreService";

  let peerUri: string = "";

  function handleConnect() {
    if (!peerUri) {
      alert("Please enter a peer URI.");
      return;
    }
    sendCommand("connect_to_peer", { uri: peerUri });
    peerUri = ""; // Clear the input
  }

  function handleDisconnect(nodeId: string) {
    if (confirm(`Are you sure you want to disconnect from ${nodeId}?`)) {
      sendCommand("disconnect_from_peer", { node_id: nodeId });
    }
  }

  function handleReconnect() {
    sendCommand("get_status"); // This will trigger a reconnect if needed
  }
</script>

<main class="container">
  <h1>D-PC Messenger</h1>

  <div class="status-bar">
    <strong>Core Service Status:</strong>
    <span class="status-{$connectionStatus}">{$connectionStatus}</span>
    {#if $connectionStatus !== 'connected'}
      <!-- The button now just tries to send a command, which will trigger a reconnect -->
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

    <!-- Right Panel: Chat Interface (Placeholder for now) -->
    <div class="panel">
      <h2>Chat</h2>
      <div class="chat-window">
        <p>Chat interface will be implemented in the next epic.</p>
      </div>
    </div>
  </div>

</main>

<style>
  .container {
    padding: 2rem;
    font-family: sans-serif;
    text-align: center;
  }
  .status-bar {
    margin-bottom: 2rem;
    padding: 1rem;
    border: 1px solid #ccc;
    border-radius: 8px;
    background-color: #f9f9f9;
  }
  .status-connected { color: green; font-weight: bold; }
  .status-disconnected { color: red; font-weight: bold; }
  .status-connecting { color: orange; font-weight: bold; }
  .status-error { color: red; font-weight: bold; }
  .node-info {
    text-align: left;
    max-width: 600px;
    margin: 0 auto;
    padding: 1rem;
    border: 1px solid #eee;
    border-radius: 8px;
  }
  .error {
    color: red;
  }
  .grid {
    display: grid;
    grid-template-columns: 1fr 2fr;
    gap: 2rem;
    margin-top: 2rem;
  }
  .panel {
    border: 1px solid #eee;
    border-radius: 8px;
    padding: 1rem;
    text-align: left;
  }
  .connection-manager input {
    width: 100%;
    padding: 0.5rem;
    margin-bottom: 0.5rem;
  }
  .peer-list ul {
    list-style: none;
    padding: 0;
  }
  .peer-list li {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.5rem;
    border-bottom: 1px solid #eee;
  }
  .peer-list li span {
    word-break: break-all;
  }
  .disconnect-btn {
    background-color: #ffcccc;
    border: 1px solid red;
    color: red;
    cursor: pointer;
  }
  .chat-window {
    height: 300px;
    border: 1px solid #ccc;
    padding: 1rem;
    background-color: #f9f9f9;
  }
</style>