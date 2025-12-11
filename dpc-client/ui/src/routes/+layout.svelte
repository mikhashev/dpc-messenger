<!-- dpc-client/ui/src/routes/+layout.svelte -->

<script lang="ts">
  import { onMount } from 'svelte';
  import { connectToCoreService } from '$lib/coreService';

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