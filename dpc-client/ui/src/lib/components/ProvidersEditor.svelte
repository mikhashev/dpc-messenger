<!-- ProvidersEditor.svelte -->
<!-- View and manage AI provider configuration -->

<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { sendCommand } from '$lib/coreService';

  export let open: boolean = false;

  const dispatch = createEventDispatcher();

  type ProviderType = 'ollama' | 'openai_compatible' | 'anthropic';

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
    providers: Provider[];
  };

  let config: ProvidersConfig | null = null;
  let selectedTab: 'list' | 'add' = 'list';
  let editMode: boolean = false;
  let editedConfig: ProvidersConfig | null = null;
  let isSaving: boolean = false;
  let saveMessage: string = '';
  let saveMessageType: 'success' | 'error' | '' = '';

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

  // Enter edit mode
  function startEditing() {
    if (!config) return;
    editMode = true;
    editedConfig = JSON.parse(JSON.stringify(config));
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
      editedConfig = editedConfig; // Trigger reactivity
    }
  }

  // Set default provider
  function setDefault(alias: string) {
    if (!editedConfig) return;
    editedConfig.default_provider = alias;
    editedConfig = editedConfig; // Trigger reactivity
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
    }

    editedConfig.providers.push(provider);
    editedConfig = editedConfig; // Trigger reactivity

    // Reset form and switch to list tab
    resetNewProviderForm();
    selectedTab = 'list';
  }
</script>

<svelte:window on:keydown={handleKeydown} />

{#if open && displayConfig}
  <!-- svelte-ignore a11y-click-events-have-key-events -->
  <!-- svelte-ignore a11y-no-static-element-interactions -->
  <div class="modal-overlay" on:click={close} role="presentation">
    <div class="modal" on:click|stopPropagation role="dialog" aria-labelledby="modal-title" tabindex="-1">
      <div class="modal-header">
        <h2 id="modal-title">🤖 AI Providers Configuration</h2>
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
            {#each displayConfig.providers as provider, i (provider.alias)}
              <div class="provider-card" class:default={provider.alias === displayConfig.default_provider}>
                <div class="provider-header">
                  <h3>
                    {provider.alias}
                    {#if provider.alias === displayConfig.default_provider}<span class="default-badge">⭐ Default</span>{/if}
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
                        placeholder="my_provider"
                      />
                    </div>

                    <div class="form-group">
                      <label for="type-{i}">Type</label>
                      <select id="type-{i}" bind:value={editedConfig.providers[i].type}>
                        <option value="ollama">Ollama</option>
                        <option value="openai_compatible">OpenAI Compatible</option>
                        <option value="anthropic">Anthropic</option>
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

                    <div class="form-group">
                      <label for="context-window-{i}">Context Window (optional)</label>
                      <input
                        id="context-window-{i}"
                        type="number"
                        bind:value={editedConfig.providers[i].context_window}
                        placeholder="Auto-detected"
                      />
                    </div>

                    {#if provider.alias !== displayConfig.default_provider}
                      <button class="btn-set-default" on:click={() => setDefault(provider.alias)}>
                        Set as Default
                      </button>
                    {/if}
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
</style>
