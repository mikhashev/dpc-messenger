<!-- dpc-client/ui/src/App.svelte -->

<script lang="ts">
  import { onMount } from "svelte";
  import { connectionStatus, nodeStatus, connectToCoreService } from "$lib/coreService";

  // Attempt to connect when the component is first loaded
  onMount(() => {
    connectToCoreService();
  });
</script>

<main class="container">
  <h1>D-PC Messenger</h1>

  <div class="status-bar">
    <strong>Core Service Status:</strong>
    <span class="status-{$connectionStatus}">{$connectionStatus}</span>
    {#if $connectionStatus !== 'connected'}
      <button on:click={connectToCoreService}>Reconnect</button>
    {/if}
  </div>

  {#if $connectionStatus === 'connected' && $nodeStatus}
    <div class="node-info">
      <h2>Your Node</h2>
      <p><strong>Node ID:</strong> {$nodeStatus.node_id}</p>
      <p><strong>Hub Status:</strong> {$nodeStatus.hub_status}</p>
      <p><strong>Connected Peers:</strong> {$nodeStatus.p2p_peers.length}</p>
    </div>
  {:else if $connectionStatus === 'connecting'}
    <p>Connecting to backend service...</p>
  {:else if $connectionStatus === 'error' || $connectionStatus === 'disconnected'}
    <p class="error">
      Could not connect to the D-PC Core Service. Please ensure it is running.
    </p>
  {/if}

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
</style>