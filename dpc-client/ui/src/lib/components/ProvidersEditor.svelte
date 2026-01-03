<!-- ProvidersEditor.svelte -->
<!-- View and manage AI provider configuration -->

<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { sendCommand } from '$lib/coreService';

  export let open: boolean = false;

  const dispatch = createEventDispatcher();

  type ProviderType = 'ollama' | 'openai_compatible' | 'anthropic' | 'zai';

  type Provider = {
    alias: string;
    type: ProviderType;
    model: string;
    host?: string;           // Ollama only
    base_url?: string;       // OpenAI only
    api_key?: string;        // Plaintext (local providers only)
    api_key_env?: string;    // Environment variable (cloud providers)
    context_window?: number; // Optional override
  };

  type ProvidersConfig = {
    default_provider: string;
    vision_provider?: string;  // Optional vision provider for image queries
    providers: Provider[];
  };

  let config: ProvidersConfig | null = null;
  let selectedTab: 'list' | 'add' = 'list';
  let editMode: boolean = false;
  let editedConfig: ProvidersConfig | null = null;
  let isSaving: boolean = false;
  let saveMessage: string = '';
  let saveMessageType: 'success' | 'error' | '' = '';

  // Model info query state
  let showModelInfo: boolean = false;
  let modelInfoData: any = null;
  let modelInfoLoading: boolean = false;
  let modelInfoError: string = '';
  let queriedProviderAlias: string = '';

  // Context window presets
  const CONTEXT_WINDOW_PRESETS = [
    { label: '2K tokens', value: 2048 },
    { label: '4K tokens', value: 4096 },
    { label: '8K tokens', value: 8192 },
    { label: '16K tokens', value: 16384 },
    { label: '32K tokens', value: 32768 },
    { label: '64K tokens', value: 65536 },
    { label: '128K tokens', value: 131072 },
    { label: '256K tokens', value: 262144 },
  ];

  // New provider form
  let newProvider: Provider = {
    alias: '',
    type: 'ollama',
    model: '',
  };

  // Load config when modal opens
  $: if (open && !config) {
    loadConfig();
  }

  async function loadConfig() {
    try {
      const result = await sendCommand('get_providers_config', {});
      if (result.status === 'success') {
        config = result.config;
      } else {
        console.error('Failed to load providers config:', result.message);
      }
    } catch (error) {
      console.error('Error loading providers config:', error);
    }
  }

  // Cancel editing
  function cancelEditing() {
    editMode = false;
    editedConfig = null;
    selectedTab = 'list';
    saveMessage = '';
    saveMessageType = '';
    resetNewProviderForm();
  }

  // Save changes
  async function saveChanges() {
    if (!editedConfig) return;

    isSaving = true;
    saveMessage = '';
    saveMessageType = '';

    try {
      const result = await sendCommand('save_providers_config', {
        config_dict: editedConfig
      });

      if (result.status === 'success') {
        saveMessage = result.message;
        saveMessageType = 'success';

        // Update the displayed config
        config = JSON.parse(JSON.stringify(editedConfig));

        // Exit edit mode
        editMode = false;
        editedConfig = null;
        selectedTab = 'list';

        // Clear success message after short delay
        setTimeout(() => {
          saveMessage = '';
          saveMessageType = '';
        }, 2000);
      } else {
        saveMessage = result.message;
        if (result.errors && result.errors.length > 0) {
          saveMessage += ':\n' + result.errors.join('\n');
        }
        saveMessageType = 'error';
      }
    } catch (error) {
      console.error('Error saving providers config:', error);
      saveMessage = `Error: ${error}`;
      saveMessageType = 'error';
    } finally {
      isSaving = false;
    }
  }

  function close() {
    if (editMode) {
      const confirmed = confirm('You have unsaved changes. Discard them and close?');
      if (!confirmed) return;
    }
    editMode = false;
    editedConfig = null;
    config = null;
    selectedTab = 'list';
    resetNewProviderForm();
    dispatch('close');
  }

  function handleKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      if (editMode) {
        cancelEditing();
      } else {
        close();
      }
    }
  }

  // Get the config to display (edited or original)
  $: displayConfig = editedConfig || config;

  // Delete provider
  function deleteProvider(index: number) {
    if (!editedConfig) return;
    const provider = editedConfig.providers[index];
    const confirmed = confirm(`Delete provider '${provider.alias}'?`);
    if (confirmed) {
      editedConfig.providers.splice(index, 1);
      // If deleted provider was default, reset default
      if (editedConfig.default_provider === provider.alias) {
        editedConfig.default_provider = editedConfig.providers[0]?.alias || '';
      }
      // If deleted provider was vision default, reset vision default
      if (editedConfig.vision_provider === provider.alias) {
        editedConfig.vision_provider = editedConfig.providers[0]?.alias || '';
      }
      editedConfig = editedConfig; // Trigger reactivity
    }
  }

  // Set default provider
  function setDefault(alias: string) {
    if (!editedConfig) return;
    editedConfig.default_provider = alias;
    editedConfig = editedConfig; // Trigger reactivity
  }

  // Set vision default provider
  function setVisionDefault(alias: string) {
    if (!editedConfig) return;
    editedConfig.vision_provider = alias;
    editedConfig = editedConfig; // Trigger reactivity
  }

  // Track original aliases to detect changes on blur
  let originalAliases = new Map<number, string>();

  // Handle alias change with auto-update of defaults (triggered on blur)
  function handleAliasBlur(index: number) {
    if (!editedConfig) return;
    const newAlias = editedConfig.providers[index].alias;
    const oldAlias = originalAliases.get(index);

    // Only update if alias actually changed
    if (!oldAlias || newAlias === oldAlias) {
      originalAliases.set(index, newAlias);
      return;
    }

    // Auto-update default_provider if this was the default
    if (editedConfig.default_provider === oldAlias) {
      editedConfig.default_provider = newAlias;
    }

    // Auto-update vision_provider if this was the vision default
    if (editedConfig.vision_provider === oldAlias) {
      editedConfig.vision_provider = newAlias;
    }

    // Update the tracked alias
    originalAliases.set(index, newAlias);
    editedConfig = editedConfig; // Trigger reactivity
  }

  // Initialize original aliases when entering edit mode
  function startEditing() {
    if (!config) return;
    editMode = true;
    editedConfig = JSON.parse(JSON.stringify(config));
    if (!editedConfig) return; // Guard against null
    // Track original aliases
    originalAliases.clear();
    editedConfig.providers.forEach((p, i) => {
      originalAliases.set(i, p.alias);
    });
  }

  // API key source switching
  function switchToEnv(index: number) {
    if (!editedConfig) return;
    const provider = editedConfig.providers[index];
    delete provider.api_key;
    provider.api_key_env = provider.api_key_env || '';
    editedConfig = editedConfig; // Trigger reactivity
  }

  function switchToPlaintext(index: number) {
    if (!editedConfig) return;
    const provider = editedConfig.providers[index];
    delete provider.api_key_env;
    provider.api_key = provider.api_key || '';
    editedConfig = editedConfig; // Trigger reactivity
  }

  // Reset new provider form
  function resetNewProviderForm() {
    newProvider = {
      alias: '',
      type: 'ollama',
      model: '',
    };
  }

  // Add new provider
  function addNewProvider() {
    if (!editedConfig) return;

    // Add type-specific defaults
    const provider: Provider = {
      alias: newProvider.alias,
      type: newProvider.type,
      model: newProvider.model,
    };

    if (newProvider.type === 'ollama') {
      provider.host = 'http://127.0.0.1:11434';
    } else if (newProvider.type === 'openai_compatible') {
      provider.base_url = 'https://api.openai.com/v1';
      provider.api_key_env = 'OPENAI_API_KEY';
    } else if (newProvider.type === 'anthropic') {
      provider.api_key_env = 'ANTHROPIC_API_KEY';
    } else if (newProvider.type === 'zai') {
      provider.api_key_env = 'ZAI_API_KEY';
      provider.model = 'glm-4.7';
      provider.context_window = 128000;
    }

    editedConfig.providers.push(provider);
    editedConfig = editedConfig; // Trigger reactivity

    // Reset form and switch to list tab
    resetNewProviderForm();
    selectedTab = 'list';
  }

  // Query Ollama model info
  async function queryModelInfo(providerAlias: string) {
    modelInfoLoading = true;
    modelInfoError = '';
    modelInfoData = null;
    queriedProviderAlias = providerAlias;
    showModelInfo = true;

    try {
      const result = await sendCommand('query_ollama_model_info', {
        provider_alias: providerAlias
      });

      if (result.status === 'success') {
        modelInfoData = result.model_info;
      } else {
        modelInfoError = result.message || 'Failed to query model info';
      }
    } catch (error) {
      modelInfoError = `Error: ${error}`;
    } finally {
      modelInfoLoading = false;
    }
  }

  // Close model info modal
  function closeModelInfo() {
    showModelInfo = false;
    modelInfoData = null;
    modelInfoError = '';
    queriedProviderAlias = '';
  }

  // Use detected context window value
  function useDetectedContextWindow(providerAlias: string, numCtx: number) {
    if (!editedConfig) {
      // Not in edit mode - enter edit mode first
      startEditing();
      // Wait for next tick to ensure editedConfig is set
      setTimeout(() => {
        const index = editedConfig?.providers.findIndex(p => p.alias === providerAlias);
        if (index !== undefined && index !== -1 && editedConfig) {
          editedConfig.providers[index].context_window = numCtx;
          editedConfig = editedConfig; // Trigger reactivity
          closeModelInfo();
        }
      }, 0);
    } else {
      const index = editedConfig.providers.findIndex(p => p.alias === providerAlias);
      if (index !== -1) {
        editedConfig.providers[index].context_window = numCtx;
        editedConfig = editedConfig; // Trigger reactivity
        closeModelInfo();
      }
    }
  }
</script>

<svelte:window on:keydown={handleKeydown} />

{#if open && displayConfig}
  <!-- svelte-ignore a11y-click-events-have-key-events -->
  <!-- svelte-ignore a11y-no-static-element-interactions -->
  <div class="modal-overlay" on:click={close} role="presentation">
    <div class="modal" on:click|stopPropagation role="dialog" aria-labelledby="modal-title" tabindex="-1">
      <div class="modal-header">
        <h2 id="modal-title">AI Providers Configuration</h2>
        <div class="header-actions">
          {#if !editMode}
            <button class="btn btn-edit" on:click={startEditing}>Edit</button>
          {:else}
            <button class="btn btn-save" on:click={saveChanges} disabled={isSaving}>
              {isSaving ? 'Saving...' : 'Save'}
            </button>
            <button class="btn btn-cancel" on:click={cancelEditing} disabled={isSaving}>Cancel</button>
          {/if}
        </div>
        <button class="close-btn" on:click={close} aria-label="Close">&times;</button>
      </div>

      <!-- Tabs -->
      <div class="tabs">
        <button
          class="tab"
          class:active={selectedTab === 'list'}
          on:click={() => selectedTab = 'list'}
        >
          Providers ({displayConfig.providers.length})
        </button>
        {#if editMode}
          <button
            class="tab"
            class:active={selectedTab === 'add'}
            on:click={() => selectedTab = 'add'}
          >
            Add Provider
          </button>
        {/if}
      </div>

      <div class="modal-body">
        {#if selectedTab === 'list'}
          <!-- Provider Cards -->
          <div class="providers-list">
            {#each displayConfig.providers as provider, i (i)}
              <div class="provider-card" class:default={provider.alias === displayConfig.default_provider}>
                <div class="provider-header">
                  <h3>
                    {provider.alias}
                    {#if provider.alias === displayConfig.default_provider}<span class="default-badge">⭐ Text Default</span>{/if}
                    {#if provider.alias === displayConfig.vision_provider}<span class="default-badge vision-badge">👁️ Vision Default</span>{/if}
                  </h3>
                  {#if editMode}
                    <button class="btn-delete" on:click={() => deleteProvider(i)}>Delete</button>
                  {/if}
                </div>

                {#if editMode && editedConfig}
                  <!-- Edit Form -->
                  <div class="provider-form">
                    <div class="form-group">
                      <label for="alias-{i}">Alias</label>
                      <input
                        id="alias-{i}"
                        type="text"
                        bind:value={editedConfig.providers[i].alias}
                        on:blur={() => handleAliasBlur(i)}
                        placeholder="my_provider"
                      />
                      {#if editedConfig.default_provider === provider.alias || editedConfig.vision_provider === provider.alias}
                        <p class="help-text">💡 Renaming will automatically update default settings</p>
                      {/if}
                    </div>

                    <div class="form-group">
                      <label for="type-{i}">Type</label>
                      <select id="type-{i}" bind:value={editedConfig.providers[i].type}>
                        <option value="ollama">Ollama</option>
                        <option value="openai_compatible">OpenAI Compatible</option>
                        <option value="anthropic">Anthropic</option>
                        <option value="zai">Z.AI</option>
                      </select>
                    </div>

                    <div class="form-group">
                      <label for="model-{i}">Model</label>
                      <input
                        id="model-{i}"
                        type="text"
                        bind:value={editedConfig.providers[i].model}
                        placeholder="llama3.1:8b"
                      />
                    </div>

                    <!-- Type-specific fields -->
                    {#if editedConfig.providers[i].type === 'ollama'}
                      <div class="form-group">
                        <label for="host-{i}">Host</label>
                        <input
                          id="host-{i}"
                          type="text"
                          bind:value={editedConfig.providers[i].host}
                          placeholder="http://127.0.0.1:11434"
                        />
                        <p class="help-text">No API key needed for Ollama</p>
                      </div>
                    {/if}

                    {#if editedConfig.providers[i].type === 'openai_compatible'}
                      <div class="form-group">
                        <label for="base-url-{i}">Base URL</label>
                        <input
                          id="base-url-{i}"
                          type="text"
                          bind:value={editedConfig.providers[i].base_url}
                          placeholder="https://api.openai.com/v1"
                        />
                      </div>

                      <div class="form-group">
                        <strong class="form-label">API Key Source</strong>
                        <div class="radio-group">
                          <label>
                            <input
                              type="radio"
                              name="key-{i}"
                              checked={!!editedConfig.providers[i].api_key_env}
                              on:change={() => switchToEnv(i)}
                            />
                            Environment Variable (Recommended)
                          </label>
                          <label>
                            <input
                              type="radio"
                              name="key-{i}"
                              checked={!!editedConfig.providers[i].api_key}
                              on:change={() => switchToPlaintext(i)}
                            />
                            Plaintext (Local providers only)
                          </label>
                        </div>

                        {#if editedConfig.providers[i].api_key_env}
                          <input
                            type="text"
                            bind:value={editedConfig.providers[i].api_key_env}
                            placeholder="OPENAI_API_KEY"
                          />
                          <p class="help-text">Set this environment variable before starting the service</p>
                        {:else if editedConfig.providers[i].api_key !== undefined}
                          <form on:submit|preventDefault>
                            <input
                              type="password"
                              bind:value={editedConfig.providers[i].api_key}
                              placeholder="sk-..."
                              autocomplete="off"
                            />
                          </form>
                          <p class="help-text warn">⚠️ Stored in plaintext - only use for local providers</p>
                        {/if}
                      </div>
                    {/if}

                    {#if editedConfig.providers[i].type === 'anthropic'}
                      <div class="form-group">
                        <label for="api-key-env-{i}">API Key Environment Variable</label>
                        <input
                          id="api-key-env-{i}"
                          type="text"
                          bind:value={editedConfig.providers[i].api_key_env}
                          placeholder="ANTHROPIC_API_KEY"
                        />
                        <p class="help-text">Set this environment variable before starting the service</p>
                      </div>
                    {/if}

                    {#if editedConfig.providers[i].type === 'zai'}
                      <div class="form-group">
                        <label for="api-key-env-{i}">API Key Environment Variable</label>
                        <input
                          id="api-key-env-{i}"
                          type="text"
                          bind:value={editedConfig.providers[i].api_key_env}
                          placeholder="ZAI_API_KEY"
                        />
                        <p class="help-text">Recommended: Store API key in environment variable</p>
                      </div>

                      <div class="form-group">
                        <label for="api-key-{i}">API Key (plaintext, alternative)</label>
                        <form on:submit|preventDefault>
                          <input
                            id="api-key-{i}"
                            type="password"
                            bind:value={editedConfig.providers[i].api_key}
                            placeholder="Leave blank to use environment variable"
                            autocomplete="off"
                          />
                        </form>
                        <p class="help-text warn">⚠️ Not recommended: Stores key in config file</p>
                      </div>
                    {/if}

                    <div class="form-group">
                      <label for="context-window-{i}">Context Window (optional)</label>
                      <select
                        id="context-window-select-{i}"
                        value={editedConfig.providers[i].context_window || ''}
                        on:change={(e) => {
                          if (!editedConfig) return;
                          const val = (e.target as HTMLSelectElement).value;
                          if (val === 'custom') {
                            editedConfig.providers[i].context_window = undefined;
                          } else if (val === '') {
                            editedConfig.providers[i].context_window = undefined;
                          } else {
                            editedConfig.providers[i].context_window = parseInt(val);
                          }
                          editedConfig = editedConfig;
                        }}
                      >
                        <option value="">Auto-detected</option>
                        {#each CONTEXT_WINDOW_PRESETS as preset}
                          <option value={preset.value}>{preset.label}</option>
                        {/each}
                        <option value="custom">Custom...</option>
                      </select>

                      {#if editedConfig.providers[i].context_window && !CONTEXT_WINDOW_PRESETS.some(p => p.value === editedConfig?.providers[i].context_window)}
                        <input
                          id="context-window-custom-{i}"
                          type="number"
                          bind:value={editedConfig.providers[i].context_window}
                          placeholder="Enter custom value"
                          class="custom-context-input"
                        />
                      {/if}
                    </div>

                    <div class="default-buttons">
                      <button
                        class="btn-set-default"
                        class:active={provider.alias === displayConfig.default_provider}
                        on:click={() => setDefault(provider.alias)}
                      >
                        {provider.alias === displayConfig.default_provider ? '✓ Text Default' : 'Set as Text Default'}
                      </button>
                      <button
                        class="btn-set-default"
                        class:active={provider.alias === displayConfig.vision_provider}
                        on:click={() => setVisionDefault(provider.alias)}
                      >
                        {provider.alias === displayConfig.vision_provider ? '✓ Vision Default' : 'Set as Vision Default'}
                      </button>
                    </div>
                  </div>
                {:else}
                  <!-- Display Mode -->
                  <div class="provider-info">
                    <p><strong>Type:</strong> {provider.type}</p>
                    <p><strong>Model:</strong> {provider.model}</p>

                    {#if provider.type === 'ollama'}
                      <p><strong>Host:</strong> {provider.host}</p>
                    {/if}

                    {#if provider.type === 'openai_compatible'}
                      <p><strong>Base URL:</strong> {provider.base_url}</p>
                      {#if provider.api_key_env}
                        <p><strong>API Key:</strong> ${provider.api_key_env} (env var)</p>
                      {:else if provider.api_key}
                        <p><strong>API Key:</strong> ••••••••</p>
                      {:else}
                        <p class="warn">⚠️ No API key configured</p>
                      {/if}
                    {/if}

                    {#if provider.type === 'anthropic'}
                      {#if provider.api_key_env}
                        <p><strong>API Key:</strong> ${provider.api_key_env} (env var)</p>
                      {:else}
                        <p class="warn">⚠️ No API key configured</p>
                      {/if}
                    {/if}

                    {#if provider.context_window}
                      <p><strong>Context Window:</strong> {provider.context_window.toLocaleString()} tokens</p>
                    {/if}

                    {#if provider.type === 'ollama'}
                      <button class="btn-query-info" on:click={() => queryModelInfo(provider.alias)}>
                        🔍 Query Model Info
                      </button>
                    {/if}
                  </div>
                {/if}
              </div>
            {/each}
          </div>
        {:else if selectedTab === 'add'}
          <!-- Add Provider Form -->
          <div class="add-provider-form">
            <h3>Add New Provider</h3>

            <div class="form-group">
              <label for="new-alias">Alias</label>
              <input
                id="new-alias"
                type="text"
                bind:value={newProvider.alias}
                placeholder="my_provider"
              />
            </div>

            <div class="form-group">
              <label for="new-type">Type</label>
              <select id="new-type" bind:value={newProvider.type}>
                <option value="ollama">Ollama</option>
                <option value="openai_compatible">OpenAI Compatible</option>
                <option value="anthropic">Anthropic</option>
              </select>
            </div>

            <div class="form-group">
              <label for="new-model">Model</label>
              <input
                id="new-model"
                type="text"
                bind:value={newProvider.model}
                placeholder={
                  newProvider.type === 'ollama' ? 'llama3.1:8b' :
                  newProvider.type === 'openai_compatible' ? 'gpt-4o' :
                  'claude-3-5-sonnet-20240620'
                }
              />
            </div>

            <div class="form-info">
              <p><strong>Note:</strong> After adding the provider, you can configure additional settings in the edit mode.</p>
              {#if newProvider.type === 'ollama'}
                <p>Default host will be: http://127.0.0.1:11434</p>
              {:else if newProvider.type === 'openai_compatible'}
                <p>Default base URL will be: https://api.openai.com/v1</p>
                <p>API key will use environment variable: OPENAI_API_KEY</p>
              {:else if newProvider.type === 'anthropic'}
                <p>API key will use environment variable: ANTHROPIC_API_KEY</p>
              {/if}
            </div>

            <button
              class="btn btn-primary"
              on:click={addNewProvider}
              disabled={!newProvider.alias || !newProvider.model}
            >
              Add Provider
            </button>
          </div>
        {/if}
      </div>

      {#if saveMessage}
        <div class="save-message {saveMessageType}">{saveMessage}</div>
      {/if}
    </div>
  </div>
{/if}

<!-- Model Info Modal -->
{#if showModelInfo}
  <!-- svelte-ignore a11y-click-events-have-key-events -->
  <!-- svelte-ignore a11y-no-static-element-interactions -->
  <div class="modal-overlay" on:click={closeModelInfo} role="presentation">
    <div class="modal model-info-modal" on:click|stopPropagation role="dialog" aria-labelledby="model-info-title" tabindex="-1">
      <div class="modal-header">
        <h2 id="model-info-title">🔍 Model Information</h2>
        <button class="close-btn" on:click={closeModelInfo} aria-label="Close">×</button>
      </div>

      <div class="modal-body">
        {#if modelInfoLoading}
          <div class="loading-state">
            <p>Querying Ollama for model information...</p>
          </div>
        {:else if modelInfoError}
          <div class="error-state">
            <p class="error-text">❌ {modelInfoError}</p>
          </div>
        {:else if modelInfoData}
          <div class="model-info-content">
            <div class="info-section">
              <h3>Context Window</h3>
              {#if modelInfoData.num_ctx}
                <p class="detected-value">
                  <strong>Detected:</strong> {modelInfoData.num_ctx.toLocaleString()} tokens
                </p>
                <button class="btn-use-detected" on:click={() => useDetectedContextWindow(queriedProviderAlias, modelInfoData.num_ctx)}>
                  Use This Value
                </button>
              {:else}
                <div class="info-box warning-box">
                  <p class="info-title">⚠️ No Custom Context Window Detected</p>
                  <p class="info-text">
                    This model doesn't have a custom <code>num_ctx</code> parameter in its modelfile.
                    It will use Ollama's default (typically <strong>2,048 tokens</strong>).
                  </p>
                  <p class="info-text">
                    To increase it, use the dropdown in edit mode to select a larger context window.
                  </p>
                </div>
              {/if}
            </div>

            {#if modelInfoData.details && Object.keys(modelInfoData.details).length > 0}
              <div class="info-section">
                <h3>Model Details</h3>
                <div class="details-grid">
                  {#if modelInfoData.details.family}
                    <p><strong>Family:</strong> {modelInfoData.details.family}</p>
                  {/if}
                  {#if modelInfoData.details.parameter_size}
                    <p><strong>Parameter Size:</strong> {modelInfoData.details.parameter_size}</p>
                  {/if}
                  {#if modelInfoData.details.quantization_level}
                    <p><strong>Quantization:</strong> {modelInfoData.details.quantization_level}</p>
                  {/if}
                  {#if modelInfoData.details.format}
                    <p><strong>Format:</strong> {modelInfoData.details.format}</p>
                  {/if}
                </div>
              </div>
            {/if}

            {#if modelInfoData.modelfile}
              <details class="modelfile-section">
                <summary><h3>Raw Modelfile</h3></summary>
                <pre class="modelfile-content">{modelInfoData.modelfile}</pre>
              </details>
            {/if}

            {#if modelInfoData.parameters}
              <details class="parameters-section">
                <summary><h3>Parameters</h3></summary>
                <pre class="parameters-content">{modelInfoData.parameters}</pre>
              </details>
            {/if}
          </div>
        {/if}
      </div>
    </div>
  </div>
{/if}

<style>
  .modal-overlay {
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    background: rgba(0, 0, 0, 0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }

  .modal {
    background: #1e1e1e;
    border-radius: 8px;
    width: 90%;
    max-width: 800px;
    max-height: 90vh;
    display: flex;
    flex-direction: column;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
  }

  .modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 20px;
    border-bottom: 1px solid #333;
  }

  .modal-header h2 {
    margin: 0;
    font-size: 1.5rem;
    color: #fff;
  }

  .header-actions {
    display: flex;
    gap: 10px;
  }

  .close-btn {
    background: none;
    border: none;
    color: #999;
    font-size: 2rem;
    cursor: pointer;
    padding: 0;
    width: 30px;
    height: 30px;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .close-btn:hover {
    color: #fff;
  }

  .btn {
    padding: 8px 16px;
    border-radius: 4px;
    border: none;
    cursor: pointer;
    font-size: 0.9rem;
  }

  .btn-edit {
    background: #007acc;
    color: #fff;
  }

  .btn-edit:hover {
    background: #005a9e;
  }

  .btn-save {
    background: #28a745;
    color: #fff;
  }

  .btn-save:hover:not(:disabled) {
    background: #218838;
  }

  .btn-save:disabled {
    background: #666;
    cursor: not-allowed;
  }

  .btn-cancel {
    background: #6c757d;
    color: #fff;
  }

  .btn-cancel:hover:not(:disabled) {
    background: #5a6268;
  }

  .btn-primary {
    background: #007acc;
    color: #fff;
    padding: 10px 20px;
  }

  .btn-primary:hover:not(:disabled) {
    background: #005a9e;
  }

  .btn-primary:disabled {
    background: #666;
    cursor: not-allowed;
  }

  .tabs {
    display: flex;
    border-bottom: 1px solid #333;
    padding: 0 20px;
  }

  .tab {
    background: none;
    border: none;
    color: #999;
    padding: 12px 20px;
    cursor: pointer;
    font-size: 1rem;
    border-bottom: 2px solid transparent;
  }

  .tab:hover {
    color: #fff;
  }

  .tab.active {
    color: #fff;
    border-bottom-color: #007acc;
  }

  .modal-body {
    padding: 20px;
    overflow-y: auto;
    flex: 1;
  }

  .providers-list {
    display: flex;
    flex-direction: column;
    gap: 15px;
  }

  .provider-card {
    background: #252525;
    border-radius: 6px;
    padding: 15px;
    border: 1px solid #333;
  }

  .provider-card.default {
    border-color: #007acc;
  }

  .provider-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 10px;
  }

  .provider-header h3 {
    margin: 0;
    color: #fff;
    font-size: 1.1rem;
  }

  .default-badge {
    color: #ffd700;
    margin-left: 8px;
  }

  .btn-delete {
    background: #dc3545;
    color: #fff;
    padding: 6px 12px;
    border-radius: 4px;
    border: none;
    cursor: pointer;
    font-size: 0.85rem;
  }

  .btn-delete:hover {
    background: #c82333;
  }

  .btn-set-default {
    background: #007acc;
    color: #fff;
    padding: 8px 16px;
    border-radius: 4px;
    border: none;
    cursor: pointer;
    margin-top: 10px;
  }

  .btn-set-default:hover {
    background: #005a9e;
  }

  .provider-form {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .provider-info {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .provider-info p {
    margin: 0;
    color: #ccc;
  }

  .form-group {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .form-group label {
    color: #fff;
    font-size: 0.9rem;
  }

  .form-label {
    display: block;
    color: #fff;
    font-size: 0.9rem;
    font-weight: 600;
    margin-bottom: 0.5rem;
  }

  .form-group form {
    margin: 0;
    padding: 0;
  }

  .form-group input,
  .form-group select {
    background: #1e1e1e;
    border: 1px solid #333;
    color: #fff;
    padding: 8px;
    border-radius: 4px;
    font-size: 0.9rem;
  }

  .form-group input:focus,
  .form-group select:focus {
    outline: none;
    border-color: #007acc;
  }

  .radio-group {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .radio-group label {
    display: flex;
    align-items: center;
    gap: 8px;
    color: #ccc;
  }

  .help-text {
    margin: 0;
    font-size: 0.85rem;
    color: #999;
  }

  .help-text.warn {
    color: #ffc107;
  }

  .warn {
    color: #ffc107;
  }

  .add-provider-form {
    display: flex;
    flex-direction: column;
    gap: 15px;
    max-width: 500px;
  }

  .add-provider-form h3 {
    margin: 0;
    color: #fff;
  }

  .form-info {
    background: #252525;
    border-radius: 4px;
    padding: 12px;
    border: 1px solid #333;
  }

  .form-info p {
    margin: 0;
    margin-bottom: 8px;
    color: #ccc;
    font-size: 0.9rem;
  }

  .form-info p:last-child {
    margin-bottom: 0;
  }

  .save-message {
    padding: 12px 20px;
    margin: 0 20px 20px;
    border-radius: 4px;
    font-size: 0.9rem;
  }

  .save-message.success {
    background: #28a745;
    color: #fff;
  }

  .save-message.error {
    background: #dc3545;
    color: #fff;
    white-space: pre-line;
  }

  /* Query Model Info Button */
  .btn-query-info {
    background: #007bff;
    color: #fff;
    border: none;
    padding: 8px 16px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.9rem;
    margin-top: 10px;
  }

  .btn-query-info:hover {
    background: #0056b3;
  }

  /* Custom Context Window Input */
  .custom-context-input {
    margin-top: 8px;
    width: 100%;
    padding: 8px;
    border: 1px solid #444;
    background: #2a2a2a;
    color: #fff;
    border-radius: 4px;
  }

  /* Model Info Modal */
  .model-info-modal {
    max-width: 700px;
  }

  .model-info-content {
    padding: 0;
  }

  .info-section {
    padding: 20px;
    border-bottom: 1px solid #333;
  }

  .info-section:last-child {
    border-bottom: none;
  }

  .info-section h3 {
    margin: 0 0 15px 0;
    font-size: 1.1rem;
    color: #fff;
  }

  .detected-value {
    font-size: 1.1rem;
    margin: 10px 0;
    padding: 14px;
    background: rgba(0, 123, 255, 0.1);
    border-radius: 6px;
    border-left: 4px solid #007bff;
    color: #e0e0e0;
    line-height: 1.6;
  }

  .detected-value strong {
    color: #90caf9;
  }

  .btn-use-detected {
    background: #28a745;
    color: #fff;
    border: none;
    padding: 10px 20px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.9rem;
    margin-top: 10px;
  }

  .btn-use-detected:hover {
    background: #218838;
  }

  .details-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 10px;
  }

  .details-grid p {
    margin: 5px 0;
    padding: 12px;
    background: #2a2a2a;
    border-radius: 4px;
    color: #e0e0e0;
    line-height: 1.5;
  }

  .details-grid p strong {
    color: #90caf9;
  }

  .modelfile-section,
  .parameters-section {
    padding: 15px 20px;
    border-top: 1px solid #333;
  }

  .modelfile-section summary,
  .parameters-section summary {
    cursor: pointer;
    font-weight: bold;
    user-select: none;
    color: #e0e0e0;
  }

  .modelfile-section summary:hover,
  .parameters-section summary:hover {
    color: #90caf9;
  }

  .modelfile-section summary h3,
  .parameters-section summary h3 {
    display: inline;
    font-size: 1rem;
  }

  .modelfile-content,
  .parameters-content {
    background: #1a1a1a;
    padding: 15px;
    border-radius: 4px;
    overflow-x: auto;
    font-size: 0.85rem;
    line-height: 1.6;
    margin-top: 10px;
    white-space: pre-wrap;
    word-wrap: break-word;
    color: #d0d0d0;
    border: 1px solid #333;
  }

  /* Info boxes */
  .info-box {
    padding: 16px;
    border-radius: 6px;
    margin: 10px 0;
    border: 2px solid;
  }

  .warning-box {
    background: rgba(255, 193, 7, 0.1);
    border-color: #ffc107;
  }

  .info-title {
    font-weight: bold;
    font-size: 1rem;
    margin: 0 0 10px 0;
    color: #ffc107;
  }

  .info-text {
    margin: 8px 0;
    line-height: 1.6;
    color: #e0e0e0;
  }

  .info-text code {
    background: rgba(255, 255, 255, 0.1);
    padding: 2px 6px;
    border-radius: 3px;
    font-family: 'Courier New', monospace;
    color: #90caf9;
  }

  .info-text strong {
    color: #fff;
  }

  .loading-state,
  .error-state {
    padding: 40px 20px;
    text-align: center;
  }

  .loading-state p {
    color: #999;
    font-size: 1rem;
  }

  .error-text {
    color: #dc3545;
    font-size: 1rem;
  }
</style>
