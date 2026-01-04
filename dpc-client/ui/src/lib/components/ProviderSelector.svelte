<!-- ProviderSelector.svelte - Extracted provider selection controls -->
<!-- Displays compute host, text provider, vision provider, and voice provider dropdowns for AI chats -->

<script lang="ts">
  // Provider type definition (base type from store)
  type ProviderInfo = {
    alias: string;
    model: string;
    supports_vision?: boolean;
    supports_voice?: boolean;  // v0.13.0+: Voice transcription support
  };

  // Extended provider type with source tracking
  type Provider = ProviderInfo & {
    source: 'local' | 'remote';
    displayText: string;
    uniqueId: string;
  };

  type PeerInfo = {
    node_id: string;
    name?: string;
  };

  type DefaultProviders = {
    default_provider: string;
    vision_provider: string;
    voice_provider?: string;  // v0.13.0+
  };

  // Props (Svelte 5 runes mode)
  let {
    // Bindable selections (two-way binding with parent)
    selectedComputeHost = $bindable("local"),
    selectedTextProvider = $bindable(""),
    selectedVisionProvider = $bindable(""),
    selectedVoiceProvider = $bindable(""),  // v0.13.0+

    // Display control
    showForChatId,
    isAIChat,

    // Provider data (from stores)
    providersList = [],
    peerProviders = new Map(),
    nodeStatus = null,
    defaultProviders = null
  }: {
    selectedComputeHost?: string;
    selectedTextProvider?: string;
    selectedVisionProvider?: string;
    selectedVoiceProvider?: string;
    showForChatId: string;
    isAIChat: boolean;
    providersList?: ProviderInfo[];
    peerProviders?: Map<string, ProviderInfo[]>;
    nodeStatus?: { peer_info?: PeerInfo[] } | null;
    defaultProviders?: DefaultProviders | null;
  } = $props();

  // Merged provider lists (Phase 2: combines local + remote providers)
  // Phase 2.3: Add uniqueId to track provider source for remote vision routing
  const mergedProviders = $derived(() => {
    const local = (providersList || []).map(p => ({
      ...p,
      source: 'local' as const,
      displayText: `${p.alias} (${p.model}) - local`,
      uniqueId: `local:${p.alias}`  // Unique identifier for selection tracking
    }));

    if (selectedComputeHost === "local") {
      return local;
    }

    const remote = (peerProviders.get(selectedComputeHost) || []).map(p => ({
      ...p,
      source: 'remote' as const,
      displayText: `${p.alias} (${p.model}) - remote`,
      uniqueId: `remote:${selectedComputeHost}:${p.alias}`  // Include node_id for routing
    }));

    return [...local, ...remote];
  });

  const mergedTextProviders = $derived(() => mergedProviders());
  const mergedVisionProviders = $derived(() => mergedProviders().filter(p => p.supports_vision));
  const mergedVoiceProviders = $derived(() => mergedProviders().filter(p => p.supports_voice));

  // Helper function to parse provider selection (Phase 2.3)
  // Exported for parent to use if needed
  export function parseProviderSelection(uniqueId: string): { source: 'local' | 'remote', alias: string, nodeId?: string } {
    if (!uniqueId) return { source: 'local', alias: '' };

    if (uniqueId.startsWith('remote:')) {
      const parts = uniqueId.split(':');
      return {
        source: 'remote',
        nodeId: parts[1],  // Extract node_id
        alias: parts.slice(2).join(':')  // Rejoin alias (in case it contains ':')
      };
    }

    return { source: 'local', alias: uniqueId.replace('local:', '') };
  }

  // Initialize provider selections from defaults (Phase 2.3: use uniqueId format)
  $effect(() => {
    if (defaultProviders) {
      if (!selectedTextProvider) {
        selectedTextProvider = `local:${defaultProviders.default_provider}`;
      }
      if (!selectedVisionProvider) {
        selectedVisionProvider = `local:${defaultProviders.vision_provider}`;
      }
      if (!selectedVoiceProvider && defaultProviders.voice_provider) {
        selectedVoiceProvider = `local:${defaultProviders.voice_provider}`;
      }
    }
  });
</script>

{#if isAIChat && providersList.length > 0}
  <div class="provider-selector-header">
    <!-- AI Host Selector (Phase 2: Remote Vision) -->
    <div class="provider-row-header">
      <label for="ai-host-header">AI Host:</label>
      <select id="ai-host-header" bind:value={selectedComputeHost}>
        <option value="local">Local</option>
        {#if nodeStatus?.peer_info && nodeStatus.peer_info.length > 0}
          <optgroup label="Remote Peers">
            {#each nodeStatus.peer_info as peer}
              {@const displayName = peer.name
                ? `${peer.name} | ${peer.node_id.slice(0, 20)}...`
                : `${peer.node_id.slice(0, 20)}...`}
              <option value={peer.node_id}>
                {displayName}
              </option>
            {/each}
          </optgroup>
        {/if}
      </select>
    </div>

    <!-- Text Provider Selector (Phase 2.3: uses uniqueId for local/remote tracking) -->
    <div class="provider-row-header">
      <label for="text-provider-header">Text:</label>
      <select id="text-provider-header" bind:value={selectedTextProvider}>
        {#each mergedTextProviders() as provider}
          <option value={provider.uniqueId}>
            {provider.displayText}
          </option>
        {/each}
      </select>
    </div>

    <!-- Vision Provider Selector (Phase 2.3: uses uniqueId for local/remote tracking) -->
    <div class="provider-row-header">
      <label for="vision-provider-header">Vision:</label>
      <select id="vision-provider-header" bind:value={selectedVisionProvider}>
        {#each mergedVisionProviders() as provider}
          <option value={provider.uniqueId}>
            {provider.displayText}
          </option>
        {/each}
      </select>
    </div>

    <!-- Voice Provider Selector (v0.13.0+) -->
    {#if mergedVoiceProviders().length > 0}
      <div class="provider-row-header">
        <label for="voice-provider-header">Voice:</label>
        <select id="voice-provider-header" bind:value={selectedVoiceProvider}>
          {#each mergedVoiceProviders() as provider}
            <option value={provider.uniqueId}>
              {provider.displayText}
            </option>
          {/each}
        </select>
      </div>
    {/if}
  </div>
{/if}

<style>
  /* Dual Provider Selector in Header (Phase 1) */
  .provider-selector-header {
    display: flex;
    flex-wrap: wrap;  /* Wrap items naturally when they don't fit */
    gap: 0.75rem;
    align-items: center;
  }

  .provider-row-header {
    display: flex;
    align-items: center;
    gap: 0.4rem;
  }

  .provider-row-header label {
    font-size: 0.85rem;
    font-weight: 500;
    color: #666;
    white-space: nowrap;
  }

  .provider-row-header select {
    padding: 0.4rem 0.6rem;
    border: 1px solid #ddd;
    border-radius: 4px;
    background: white;
    cursor: pointer;
    font-size: 0.85rem;
    min-width: 150px;
    max-width: 220px;
    /* Handle text overflow */
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .provider-row-header select:hover {
    border-color: #4CAF50;
  }

  .provider-row-header select:focus {
    outline: none;
    border-color: #4CAF50;
    box-shadow: 0 0 0 2px rgba(76, 175, 80, 0.1);
  }
</style>
