<!-- InstructionsEditor.svelte -->
<!-- View and manage multiple AI instruction sets (v2.0) -->

<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { sendCommand } from '$lib/coreService';

  export let open: boolean = false;

  const dispatch = createEventDispatcher();

  type InstructionBlock = {
    name: string;
    description: string;
    created: string;
    last_updated: string;
    primary: string;
    context_update: string;
    verification_protocol: string;
    learning_support: {
      explanations: string;
      practice: string;
      metacognition: string;
      connections: string;
    };
    bias_mitigation: {
      require_multi_perspective: boolean;
      challenge_status_quo: boolean;
      cultural_sensitivity: string;
      framing_neutrality: boolean;
      evidence_requirement: string;
    };
    collaboration_mode: string;
    consensus_required: boolean;
    ai_curation_enabled: boolean;
    dissent_encouraged: boolean;
  };

  type InstructionSets = {
    schema_version: string;
    default: string;
    sets: Record<string, InstructionBlock>;
  };

  let instructionSets: InstructionSets | null = null;
  let currentSetKey: string = "general";
  let editMode: boolean = false;
  let editedInstructions: InstructionBlock | null = null;
  let isSaving: boolean = false;
  let isLoading: boolean = false;
  let saveMessage: string = '';
  let saveMessageType: 'success' | 'error' | '' = '';

  // Load instruction sets when modal opens
  $: if (open && !instructionSets) {
    loadInstructionSets();
  }

  // Get current instruction set to display
  $: currentInstructions = instructionSets?.sets[currentSetKey] || null;
  $: displayInstructions = editMode && editedInstructions ? editedInstructions : currentInstructions;

  async function loadInstructionSets() {
    isLoading = true;
    try {
      const result = await sendCommand('get_instructions', {});
      if (result && result.status === 'success') {
        instructionSets = result.instruction_sets;
        // Set current set to default if not already set
        if (instructionSets && !instructionSets.sets[currentSetKey]) {
          currentSetKey = instructionSets.default || "general";
        }
      } else {
        console.error('Failed to load instruction sets:', result?.message || 'Unknown error');
      }
    } catch (error) {
      console.error('Error loading instruction sets:', error);
    } finally {
      isLoading = false;
    }
  }

  // Switch to a different instruction set
  function switchSet(setKey: string) {
    if (editMode) {
      const confirmed = confirm('You have unsaved changes. Discard them and switch sets?');
      if (!confirmed) return;
      editMode = false;
      editedInstructions = null;
    }
    currentSetKey = setKey;
    saveMessage = '';
    saveMessageType = '';
  }

  // Enter edit mode
  function startEditing() {
    if (!currentInstructions) return;
    editMode = true;
    editedInstructions = JSON.parse(JSON.stringify(currentInstructions));
  }

  // Cancel editing
  function cancelEditing() {
    editMode = false;
    editedInstructions = null;
    saveMessage = '';
    saveMessageType = '';
  }

  // Save changes
  async function saveChanges() {
    if (!editedInstructions) return;

    isSaving = true;
    saveMessage = '';
    saveMessageType = '';

    try {
      const result = await sendCommand('save_instructions', {
        set_key: currentSetKey,
        instructions_dict: editedInstructions
      });

      if (result && result.status === 'success') {
        saveMessage = result.message;
        saveMessageType = 'success';

        // Update the instruction sets in memory
        if (instructionSets) {
          instructionSets.sets[currentSetKey] = editedInstructions;
        }

        // Exit edit mode immediately
        editMode = false;
        editedInstructions = null;

        // Clear success message after short delay
        setTimeout(() => {
          saveMessage = '';
          saveMessageType = '';
        }, 2000);
      } else {
        saveMessage = result?.message || 'Save failed';
        saveMessageType = 'error';
      }
    } catch (error) {
      console.error('Error saving instruction set:', error);
      saveMessage = `Error: ${error}`;
      saveMessageType = 'error';
    } finally {
      isSaving = false;
    }
  }

  // Create new instruction set
  async function createNewSet() {
    const name = prompt('Enter name for new instruction set:');
    if (!name) return;

    const key = name.toLowerCase().replace(/\s+/g, '-');
    const description = prompt('Enter description (optional):') || '';

    try {
      const result = await sendCommand('create_instruction_set', {
        set_key: key,
        name: name,
        description: description
      });

      if (result && result.status === 'success') {
        saveMessage = result.message;
        saveMessageType = 'success';

        // Reload instruction sets to get the new one
        await loadInstructionSets();
        currentSetKey = key;

        setTimeout(() => {
          saveMessage = '';
          saveMessageType = '';
        }, 2000);
      } else {
        saveMessage = result?.message || 'Create failed';
        saveMessageType = 'error';
      }
    } catch (error) {
      console.error('Error creating instruction set:', error);
      saveMessage = `Error: ${error}`;
      saveMessageType = 'error';
    }
  }

  // Delete instruction set
  async function deleteSet() {
    if (currentSetKey === 'general') {
      alert('Cannot delete the default "general" instruction set.');
      return;
    }

    const confirmed = confirm(`Delete instruction set "${currentInstructions?.name}"? This cannot be undone.`);
    if (!confirmed) return;

    try {
      const result = await sendCommand('delete_instruction_set', {
        set_key: currentSetKey
      });

      if (result && result.status === 'success') {
        saveMessage = result.message;
        saveMessageType = 'success';

        // Reload and switch to default
        await loadInstructionSets();
        if (instructionSets) {
          currentSetKey = instructionSets.default || "general";
        }

        setTimeout(() => {
          saveMessage = '';
          saveMessageType = '';
        }, 2000);
      } else {
        saveMessage = result?.message || 'Delete failed';
        saveMessageType = 'error';
      }
    } catch (error) {
      console.error('Error deleting instruction set:', error);
      saveMessage = `Error: ${error}`;
      saveMessageType = 'error';
    }
  }

  // Set as default
  async function setAsDefault() {
    try {
      const result = await sendCommand('set_default_instruction_set', {
        set_key: currentSetKey
      });

      if (result && result.status === 'success') {
        saveMessage = result.message;
        saveMessageType = 'success';

        // Update default in memory
        if (instructionSets) {
          instructionSets.default = currentSetKey;
        }

        setTimeout(() => {
          saveMessage = '';
          saveMessageType = '';
        }, 2000);
      } else {
        saveMessage = result?.message || 'Failed to set default';
        saveMessageType = 'error';
      }
    } catch (error) {
      console.error('Error setting default instruction set:', error);
      saveMessage = `Error: ${error}`;
      saveMessageType = 'error';
    }
  }

  // Reload from disk
  async function reloadInstructions() {
    isLoading = true;
    try {
      const result = await sendCommand('reload_instructions', {});
      if (result.status === 'success') {
        instructionSets = result.instruction_sets;
        saveMessage = 'Instruction sets reloaded from disk';
        saveMessageType = 'success';
        setTimeout(() => {
          saveMessage = '';
          saveMessageType = '';
        }, 2000);
      } else {
        saveMessage = result.message;
        saveMessageType = 'error';
      }
    } catch (error) {
      console.error('Error reloading instruction sets:', error);
      saveMessage = `Error: ${error}`;
      saveMessageType = 'error';
    } finally {
      isLoading = false;
    }
  }

  function close() {
    if (editMode) {
      const confirmed = confirm('You have unsaved changes. Discard them and close?');
      if (!confirmed) return;
    }
    editMode = false;
    editedInstructions = null;
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
</script>

{#if open}
  <!-- svelte-ignore a11y-click-events-have-key-events -->
  <!-- svelte-ignore a11y-no-static-element-interactions -->
  <div class="modal-overlay" on:click={close} on:keydown={handleKeydown} role="presentation">
    <div class="modal" on:click|stopPropagation role="dialog" aria-labelledby="instructions-dialog-title" tabindex="-1">
      <div class="modal-header">
        <h2 id="instructions-dialog-title">AI Instructions</h2>
        <div class="header-actions">
          {#if !editMode}
            <button class="btn btn-reload" on:click={reloadInstructions} disabled={isLoading}>
              {isLoading ? 'Loading...' : 'Reload from Disk'}
            </button>
            <button class="btn btn-edit" on:click={startEditing}>Edit</button>
          {:else}
            <button class="btn btn-save" on:click={saveChanges} disabled={isSaving}>
              {isSaving ? 'Saving...' : 'Save'}
            </button>
            <button class="btn btn-cancel" on:click={cancelEditing}>Cancel</button>
          {/if}
        </div>
        <button class="close-btn" on:click={close}>&times;</button>
      </div>

      <!-- Save Message -->
      {#if saveMessage}
        <div class="save-message" class:success={saveMessageType === 'success'} class:error={saveMessageType === 'error'}>
          {saveMessage}
        </div>
      {/if}

      <!-- Tab Navigation -->
      {#if instructionSets}
        <div class="tabs-container">
          <div class="tabs">
            {#each Object.entries(instructionSets.sets) as [key, set]}
              <button
                class="tab"
                class:active={currentSetKey === key}
                on:click={() => switchSet(key)}
              >
                {set.name}
                {#if instructionSets.default === key}
                  <span class="default-badge">⭐</span>
                {/if}
              </button>
            {/each}
          </div>
          <button class="btn btn-new" on:click={createNewSet}>+ New</button>
        </div>
      {/if}

      <div class="modal-body">
        {#if isLoading && !instructionSets}
          <div class="loading">Loading instruction sets...</div>
        {:else if displayInstructions}
          <!-- Set Metadata -->
          <div class="section">
            <h3>Instruction Set Details</h3>
            {#if editMode && editedInstructions}
              <div class="metadata-edit">
                <label>
                  <strong>Name:</strong>
                  <input
                    type="text"
                    class="edit-input"
                    bind:value={editedInstructions.name}
                  />
                </label>
                <label>
                  <strong>Description:</strong>
                  <input
                    type="text"
                    class="edit-input"
                    bind:value={editedInstructions.description}
                  />
                </label>
              </div>
            {:else}
              <div class="metadata-display">
                <p><strong>Name:</strong> {displayInstructions.name}</p>
                <p><strong>Description:</strong> {displayInstructions.description || 'No description'}</p>
              </div>
            {/if}

            <!-- Set Management Actions -->
            {#if !editMode}
              <div class="set-actions">
                {#if instructionSets && instructionSets.default !== currentSetKey}
                  <button class="btn btn-default" on:click={setAsDefault}>Set as Default</button>
                {/if}
                {#if currentSetKey !== 'general'}
                  <button class="btn btn-delete" on:click={deleteSet}>Delete</button>
                {/if}
              </div>
            {/if}
          </div>

          <!-- Primary Instruction -->
          <div class="section">
            <h3>Primary Instruction</h3>
            <p class="help-text">Core instruction that guides AI behavior</p>
            {#if editMode && editedInstructions}
              <textarea
                class="edit-textarea"
                rows="4"
                bind:value={editedInstructions.primary}
              ></textarea>
            {:else}
              <div class="instruction-text">{displayInstructions.primary}</div>
            {/if}
          </div>

          <!-- Context Update Instruction -->
          <div class="section">
            <h3>Context Update Instruction</h3>
            <p class="help-text">How AI should suggest updates to knowledge</p>
            {#if editMode && editedInstructions}
              <textarea
                class="edit-textarea"
                rows="3"
                bind:value={editedInstructions.context_update}
              ></textarea>
            {:else}
              <div class="instruction-text">{displayInstructions.context_update}</div>
            {/if}
          </div>

          <!-- Verification Protocol -->
          <div class="section">
            <h3>Verification Protocol</h3>
            <p class="help-text">Evidence and reasoning requirements</p>
            {#if editMode && editedInstructions}
              <textarea
                class="edit-textarea"
                rows="2"
                bind:value={editedInstructions.verification_protocol}
              ></textarea>
            {:else}
              <div class="instruction-text">{displayInstructions.verification_protocol}</div>
            {/if}
          </div>

          <!-- Bias Mitigation -->
          <div class="section">
            <h3>Bias Mitigation Settings</h3>
            <p class="help-text">Controls for reducing cognitive biases</p>
            <div class="settings-grid">
              <div class="setting-item">
                <label>
                  {#if editMode && editedInstructions}
                    <input
                      type="checkbox"
                      bind:checked={editedInstructions.bias_mitigation.require_multi_perspective}
                    />
                  {:else}
                    <input
                      type="checkbox"
                      checked={displayInstructions?.bias_mitigation.require_multi_perspective}
                      disabled
                    />
                  {/if}
                  Require Multi-Perspective Analysis
                </label>
              </div>
              <div class="setting-item">
                <label>
                  {#if editMode && editedInstructions}
                    <input
                      type="checkbox"
                      bind:checked={editedInstructions.bias_mitigation.challenge_status_quo}
                    />
                  {:else}
                    <input
                      type="checkbox"
                      checked={displayInstructions?.bias_mitigation.challenge_status_quo}
                      disabled
                    />
                  {/if}
                  Challenge Status Quo
                </label>
              </div>
              <div class="setting-item">
                <label>
                  {#if editMode && editedInstructions}
                    <input
                      type="checkbox"
                      bind:checked={editedInstructions.bias_mitigation.framing_neutrality}
                    />
                  {:else}
                    <input
                      type="checkbox"
                      checked={displayInstructions?.bias_mitigation.framing_neutrality}
                      disabled
                    />
                  {/if}
                  Framing Neutrality
                </label>
              </div>
              <div class="setting-item">
                <strong>Cultural Sensitivity:</strong>
                {#if editMode && editedInstructions}
                  <input
                    type="text"
                    class="edit-input"
                    bind:value={editedInstructions.bias_mitigation.cultural_sensitivity}
                  />
                {:else}
                  <span>{displayInstructions.bias_mitigation.cultural_sensitivity}</span>
                {/if}
              </div>
              <div class="setting-item">
                <strong>Evidence Requirement:</strong>
                {#if editMode && editedInstructions}
                  <select class="edit-select" bind:value={editedInstructions.bias_mitigation.evidence_requirement}>
                    <option value="citations_required">Citations Required</option>
                    <option value="citations_preferred">Citations Preferred</option>
                    <option value="optional">Optional</option>
                  </select>
                {:else}
                  <span>{displayInstructions.bias_mitigation.evidence_requirement}</span>
                {/if}
              </div>
            </div>
          </div>

          <!-- Collaboration Settings -->
          <div class="section">
            <h3>Collaboration Settings</h3>
            <p class="help-text">DPC-specific collaboration controls</p>
            <div class="settings-grid">
              <div class="setting-item">
                <label>
                  {#if editMode && editedInstructions}
                    <input
                      type="checkbox"
                      bind:checked={editedInstructions.consensus_required}
                    />
                  {:else}
                    <input
                      type="checkbox"
                      checked={displayInstructions?.consensus_required}
                      disabled
                    />
                  {/if}
                  Consensus Required for Knowledge Commits
                </label>
              </div>
              <div class="setting-item">
                <label>
                  {#if editMode && editedInstructions}
                    <input
                      type="checkbox"
                      bind:checked={editedInstructions.ai_curation_enabled}
                    />
                  {:else}
                    <input
                      type="checkbox"
                      checked={displayInstructions?.ai_curation_enabled}
                      disabled
                    />
                  {/if}
                  AI Curation Enabled
                </label>
              </div>
              <div class="setting-item">
                <label>
                  {#if editMode && editedInstructions}
                    <input
                      type="checkbox"
                      bind:checked={editedInstructions.dissent_encouraged}
                    />
                  {:else}
                    <input
                      type="checkbox"
                      checked={displayInstructions?.dissent_encouraged}
                      disabled
                    />
                  {/if}
                  Encourage Dissent (Devil's Advocate)
                </label>
              </div>
              <div class="setting-item">
                <strong>Collaboration Mode:</strong>
                {#if editMode && editedInstructions}
                  <select class="edit-select" bind:value={editedInstructions.collaboration_mode}>
                    <option value="individual">Individual</option>
                    <option value="group">Group</option>
                    <option value="public">Public</option>
                  </select>
                {:else}
                  <span>{displayInstructions.collaboration_mode}</span>
                {/if}
              </div>
            </div>
          </div>
        {:else}
          <div class="loading">No instruction set selected</div>
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
    background: rgba(0, 0, 0, 0.7);
    display: flex;
    justify-content: center;
    align-items: center;
    z-index: 1000;
  }

  .modal {
    background: #1e1e1e;
    border: 1px solid #333;
    border-radius: 8px;
    width: 90%;
    max-width: 900px;
    max-height: 90vh;
    display: flex;
    flex-direction: column;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.5);
  }

  .modal-header {
    padding: 1rem 1.5rem;
    border-bottom: 1px solid #333;
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: #252525;
  }

  .modal-header h2 {
    margin: 0;
    font-size: 1.25rem;
    color: #fff;
  }

  .header-actions {
    display: flex;
    gap: 0.5rem;
  }

  .close-btn {
    background: none;
    border: none;
    color: #aaa;
    font-size: 2rem;
    cursor: pointer;
    padding: 0;
    line-height: 1;
    margin-left: 1rem;
  }

  .close-btn:hover {
    color: #fff;
  }

  .save-message {
    padding: 0.75rem 1.5rem;
    margin: 0;
    text-align: center;
    font-weight: 500;
  }

  .save-message.success {
    background: #2d5016;
    color: #a3d977;
  }

  .save-message.error {
    background: #5c1616;
    color: #f78181;
  }

  .tabs-container {
    display: flex;
    align-items: center;
    gap: 1rem;
    padding: 0.75rem 1.5rem;
    background: #252525;
    border-bottom: 1px solid #333;
    overflow-x: auto;
  }

  .tabs {
    display: flex;
    gap: 0.5rem;
    flex: 1;
    overflow-x: auto;
  }

  .tab {
    padding: 0.5rem 1rem;
    background: #1e1e1e;
    border: 1px solid #444;
    border-radius: 4px 4px 0 0;
    color: #aaa;
    cursor: pointer;
    white-space: nowrap;
    transition: all 0.2s;
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .tab:hover {
    background: #2a2a2a;
    color: #fff;
  }

  .tab.active {
    background: #1e1e1e;
    border-color: #007acc;
    color: #fff;
    border-bottom-color: #1e1e1e;
  }

  .default-badge {
    font-size: 0.9rem;
  }

  .btn-new {
    background: #007acc;
    color: #fff;
    border: 1px solid #007acc;
    padding: 0.5rem 1rem;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.9rem;
    white-space: nowrap;
  }

  .btn-new:hover {
    background: #005a9e;
  }

  .modal-body {
    padding: 1.5rem;
    overflow-y: auto;
    flex: 1;
  }

  .section {
    margin-bottom: 2rem;
    padding: 1.5rem;
    background: #252525;
    border: 1px solid #3a3a3a;
    border-radius: 10px;
  }

  .section h3 {
    margin: 0 0 0.5rem 0;
    font-size: 1.15rem;
    color: #ffffff;
    font-weight: 600;
    letter-spacing: 0.3px;
  }

  .help-text {
    margin: 0 0 1rem 0;
    font-size: 0.9rem;
    color: #999;
    line-height: 1.4;
  }

  .metadata-display p {
    margin: 0.5rem 0;
    color: #e0e0e0;
  }

  .metadata-edit {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .metadata-edit label {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .metadata-edit strong {
    color: #90caf9;
  }

  .set-actions {
    display: flex;
    gap: 0.5rem;
    margin-top: 1rem;
  }

  .btn-default {
    background: #007acc;
    color: #fff;
    border: 1px solid #007acc;
  }

  .btn-default:hover {
    background: #005a9e;
  }

  .btn-delete {
    background: #dc3545;
    color: #fff;
    border: 1px solid #dc3545;
  }

  .btn-delete:hover {
    background: #c82333;
  }

  .instruction-text {
    background: #1a1a1a;
    padding: 1rem;
    border-radius: 6px;
    border: 1px solid #3a3a3a;
    color: #e0e0e0;
    white-space: pre-wrap;
    line-height: 1.6;
    font-size: 0.95rem;
  }

  .settings-grid {
    display: grid;
    grid-template-columns: 1fr;
    gap: 1rem;
  }

  .setting-item {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding: 1rem;
    background: #2a2a2a;
    border: 2px solid #3a3a3a;
    border-radius: 8px;
    color: #e0e0e0;
    transition: all 0.2s;
  }

  .setting-item:hover {
    border-color: #4a4a4a;
    background: #2f2f2f;
  }

  .setting-item label {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    cursor: pointer;
    flex: 1;
    user-select: none;
    font-size: 0.95rem;
    line-height: 1.5;
  }

  .setting-item input[type="checkbox"] {
    width: 20px;
    height: 20px;
    cursor: pointer;
    accent-color: #007acc;
    flex-shrink: 0;
  }

  .setting-item input[type="checkbox"]:disabled {
    cursor: not-allowed;
    opacity: 0.6;
  }

  .setting-item strong {
    min-width: 160px;
    color: #90caf9;
    font-weight: 600;
    flex-shrink: 0;
  }

  .setting-item span {
    color: #e0e0e0;
  }

  .edit-textarea,
  .edit-input,
  .edit-select {
    width: 100%;
    background: #1a1a1a;
    color: #e0e0e0;
    border: 2px solid #3a3a3a;
    border-radius: 6px;
    padding: 0.75rem;
    font-family: inherit;
    font-size: 0.95rem;
    line-height: 1.5;
    transition: all 0.2s;
  }

  .edit-textarea {
    resize: vertical;
    min-height: 100px;
  }

  .edit-textarea:focus,
  .edit-input:focus,
  .edit-select:focus {
    outline: none;
    border-color: #007acc;
    background: #222;
    box-shadow: 0 0 0 3px rgba(0, 122, 204, 0.1);
  }

  .edit-input,
  .edit-select {
    flex: 1;
  }

  .btn {
    padding: 0.5rem 1rem;
    border: 1px solid #444;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.9rem;
    transition: all 0.2s;
  }

  .btn-edit {
    background: #007acc;
    color: #fff;
    border-color: #007acc;
  }

  .btn-edit:hover:not(:disabled) {
    background: #005a9e;
  }

  .btn-save {
    background: #28a745;
    color: #fff;
    border-color: #28a745;
  }

  .btn-save:hover:not(:disabled) {
    background: #218838;
  }

  .btn-cancel {
    background: #6c757d;
    color: #fff;
    border-color: #6c757d;
  }

  .btn-cancel:hover:not(:disabled) {
    background: #5a6268;
  }

  .btn-reload {
    background: #444;
    color: #fff;
  }

  .btn-reload:hover:not(:disabled) {
    background: #555;
  }

  .btn:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .loading {
    text-align: center;
    color: #888;
    padding: 2rem;
  }
</style>
