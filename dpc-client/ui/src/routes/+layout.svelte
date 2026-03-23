<!-- dpc-client/ui/src/routes/+layout.svelte -->

<script lang="ts">
  import { onMount } from 'svelte';
  import { connectToCoreService } from '$lib/coreService';
  import { setupConsoleRelay } from '$lib/logger';

  // Install global console intercept immediately — captures all subsequent console.*
  // calls (including pre-connection startup logs) and relays them to ~/.dpc/logs/ui.log.
  setupConsoleRelay();

  console.log("LAYOUT: Script loading");

  onMount(() => {
    console.log("LAYOUT: onMount called");
    connectToCoreService();
  });

  console.log("LAYOUT: Script loaded");
</script>

<div id="app-root">
  <slot />
</div>

<style>
  #app-root {
    width: 100%;
    height: 100vh;
    overflow-x: hidden; /* Prevent horizontal overflow */
    box-sizing: border-box;
  }
</style>