<!-- ProviderSelector.svelte - Extracted provider selection controls -->
<!-- Displays compute host, text provider, vision provider, and voice provider dropdowns for AI chats -->

<script lang="ts">
  import { menuSummary, priceLine, settingsLines, type MenuRow } from './peerMenu';

  // Provider type definition: one row of PROVIDERS_RESPONSE (DPTP §3.5), which
  // since `ec686608` carries `tariff` and `settings` beside the capabilities.
  // The wire shape lives in peerMenu.ts, next to the reading of it.
  type ProviderInfo = MenuRow;

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
    agent_provider?: string;  // v0.18.0+
  };

  // Props (Svelte 5 runes mode)
  let {
    // Bindable selections (two-way binding with parent)
    selectedComputeHost = $bindable("local"),
    selectedTextProvider = $bindable(""),
    selectedVisionProvider = $bindable(""),
    selectedVoiceProvider = $bindable(""),  // v0.13.0+
    // Called only when a person changes the Text dropdown. `bind:` alone cannot
    // tell a choice from a restore — both are assignments — and the difference is
    // the whole of RETURNING-TO-A-CHAT-SILENTLY-PUTS-YOU-BACK-ON-THE-DEFAULT-PROVIDER:
    // what has to be remembered is the choice.
    onTextProviderChange = undefined,

    // Display control
    showForChatId,
    isAIChat,

    // Provider data (from stores)
    providersList = [],
    peerProviders = new Map(),
    nodeStatus = null,
    defaultProviders = null,

    // For agent chats with remote compute host: the LLM provider alias used by the agent
    agentLlmProvider = ""
  }: {
    selectedComputeHost?: string;
    selectedTextProvider?: string;
    onTextProviderChange?: (uniqueId: string) => void;
    selectedVisionProvider?: string;
    selectedVoiceProvider?: string;
    showForChatId: string;
    isAIChat: boolean;
    providersList?: ProviderInfo[];
    peerProviders?: Map<string, ProviderInfo[]>;
    nodeStatus?: { peer_info?: PeerInfo[] } | null;
    defaultProviders?: DefaultProviders | null;
    agentLlmProvider?: string;
  } = $props();

  // Merged provider lists (Phase 2: combines local + remote providers)
  // Phase 2.3: Add uniqueId to track provider source for remote vision routing
  const mergedProviders = $derived(() => {
    const local = (providersList || []).map(p => {
      // Special display for dpc_agent: show underlying provider
      let displayText = `${p.alias} (${p.model}) - local`;
      if (p.alias === 'dpc_agent') {
        if (agentLlmProvider) {
          // Show the actual LLM provider configured for this agent (local or remote)
          displayText = `Agent (uses ${agentLlmProvider})`;
        } else {
          const underlying = defaultProviders?.agent_provider || defaultProviders?.default_provider;
          displayText = `Agent (uses ${underlying || 'default'})`;
        }
      }
      return {
        ...p,
        source: 'local' as const,
        displayText,
        uniqueId: `local:${p.alias}`  // Unique identifier for selection tracking
      };
    });

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

  // What the guest is about to call on someone else's machine. A local alias
  // needs no such panel — the settings are this node's own and the call is
  // free by construction — so the door opens only on a `remote:` selection.
  const selectedPeerRow = $derived(() => {
    const chosen = mergedTextProviders().find(p => p.uniqueId === selectedTextProvider);
    return chosen && chosen.source === 'remote' ? chosen : null;
  });

  // Collapsed by default: the summary line answers «what does this cost and how
  // does it run» in one line, and the block behind it is for the choice itself.
  let menuExpanded = $state(false);

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
  <!-- One flex item in the chat header, so the peer panel stacks under the
       dropdowns instead of competing with them for the row. -->
  <div class="provider-selector-block">
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
        <select
          id="text-provider-header"
          bind:value={selectedTextProvider}
          onchange={(e) => onTextProviderChange?.((e.currentTarget as HTMLSelectElement).value)}
        >
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

    <!-- What a guest sees before it calls (DPTP §3.5, board entry
         A-PEER-LEARNS-THE-TARIFF-BEFORE-IT-CALLS): the host's price and the
         dials the call will run at. Mike's rule, 2026-09-14 — the guest gets
         the host's settings, and sees them before it chooses. -->
    {#if selectedPeerRow()}
      {@const row = selectedPeerRow()!}
      {@const price = priceLine(row.tariff)}
      <div class="peer-menu">
        <button
          type="button"
          class="peer-menu-summary"
          aria-expanded={menuExpanded}
          title="What this peer charges you for this alias and the settings the call will run at"
          onclick={() => (menuExpanded = !menuExpanded)}
        >
          <span class="peer-menu-caret">{menuExpanded ? '▼' : '▶'}</span>
          <span class="peer-menu-line">{menuSummary(row)}</span>
        </button>

        {#if menuExpanded}
          <div class="peer-menu-body">
            <div class="peer-menu-price">
              <span class="peer-menu-heading">Price</span>
              <span class="peer-price peer-price-{price.kind}">{price.text}</span>
            </div>

            <div class="peer-menu-settings">
              <span class="peer-menu-heading">Runs at the host's settings</span>
              <dl class="peer-settings-list">
                {#each settingsLines(row) as setting}
                  <dt>{setting.key}</dt>
                  <dd class:unstated={!setting.stated}>{setting.value}</dd>
                {/each}
              </dl>
              <p class="peer-menu-note">
                The host's settings are what you get. Reasoning effort is the one dial
                you own — the header's Reasoning control, under the host's own cap.
              </p>
            </div>
          </div>
        {/if}
      </div>
    {/if}
  </div>
{/if}

<style>
  /* Dual Provider Selector in Header (Phase 1) */
  .provider-selector-block {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
    min-width: 0;
  }

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

  /* The guest's view of a peer's alias (DPTP §3.5) */
  .peer-menu {
    margin-top: 0.5rem;
    border: 1px solid #e0e0e0;
    border-radius: 4px;
    background: #fafafa;
    font-size: 0.8rem;
  }

  .peer-menu-summary {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    width: 100%;
    padding: 0.35rem 0.6rem;
    border: none;
    background: none;
    cursor: pointer;
    text-align: left;
    font-size: 0.8rem;
    color: #444;
  }

  .peer-menu-summary:hover {
    color: #222;
  }

  .peer-menu-caret {
    color: #999;
    font-size: 0.7rem;
  }

  .peer-menu-line {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .peer-menu-body {
    padding: 0.1rem 0.6rem 0.6rem 1.5rem;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .peer-menu-heading {
    display: block;
    font-weight: 600;
    color: #666;
    margin-bottom: 0.25rem;
  }

  .peer-price {
    display: inline-block;
    padding: 0.15rem 0.45rem;
    border-radius: 3px;
    background: #eceff1;
    color: #37474f;
  }

  /* A declared zero is a decision somebody made; an absent tariff is not. */
  .peer-price-free {
    background: #e8f5e9;
    color: #2e7d32;
  }

  .peer-price-gift {
    background: #f3e5f5;
    color: #6a1b9a;
  }

  .peer-price-paid {
    background: #fff8e1;
    color: #8d6e00;
  }

  .peer-price-unpriceable {
    background: #ffebee;
    color: #b71c1c;
  }

  .peer-settings-list {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 0.15rem 0.6rem;
    margin: 0;
  }

  .peer-settings-list dt {
    color: #777;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.75rem;
  }

  .peer-settings-list dd {
    margin: 0;
    color: #333;
  }

  .peer-settings-list dd.unstated {
    color: #999;
    font-style: italic;
  }

  .peer-menu-note {
    margin: 0.25rem 0 0;
    color: #777;
    line-height: 1.35;
  }
</style>
