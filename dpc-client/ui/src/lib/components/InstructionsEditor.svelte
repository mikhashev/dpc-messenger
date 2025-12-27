<!-- InstructionsEditor.svelte -->
<!-- View and manage multiple AI instruction sets (v2.0) -->

<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { sendCommand, nodeStatus, peerProviders } from '$lib/coreService';

  // Svelte 5 runes mode - use $props() instead of export let
  let { open = $bindable(false) }: { open: boolean } = $props();

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

  let instructionSets = $state<InstructionSets | null>(null);
  let currentSetKey = $state<string>("general");
  let editMode = $state<boolean>(false);
  let editedInstructions = $state<InstructionBlock | null>(null);
  let isSaving = $state<boolean>(false);
  let isLoading = $state<boolean>(false);
  let saveMessage = $state<string>('');
  let saveMessageType = $state<'success' | 'error' | ''>('');

  // Template import state
  type Template = {
    file: string;
    filename: string;
    key: string;
    name: string;
    description: string;
  };
  let showTemplateDialog = $state<boolean>(false);
  let availableTemplates = $state<Template[]>([]);
  let selectedTemplate = $state<Template | null>(null);
  let newSetName = $state<string>('');

  // AI Wizard state
  let showCreationModeDialog = $state<boolean>(false);
  let showWizardDialog = $state<boolean>(false);
  let wizardQuestions = $state<Array<any>>([]);
  let currentQuestionIndex = $state<number>(0);
  let wizardResponses = $state<Record<string, string>>({});
  let wizardCurrentAnswer = $state<string>('');
  let wizardNewSetName = $state<string>('');
  let wizardComputeHost = $state<string>('local');  // "local" or node_id for remote inference
  let wizardSelectedProvider = $state<string>('');  // Selected provider alias
  let wizardLocalProviders = $state<Array<any>>([]);  // Cached local providers
  let isGenerating = $state<boolean>(false);

  // Merged providers (local + remote based on selected host) - matches Local AI chat pattern
  let wizardMergedProviders = $derived.by(() => {
    const local = wizardLocalProviders.map((p: any) => ({
      ...p,
      source: 'local' as const,
      displayText: `${p.alias} - ${p.model} - local`,
      uniqueId: `local:${p.alias}`
    }));

    if (wizardComputeHost === 'local') {
      return local;
    }

    // When remote host selected, add remote providers
    const remotePeerProviders = $peerProviders.get(wizardComputeHost) || [];
    const remote = remotePeerProviders.map((p: any) => ({
      ...p,
      source: 'remote' as const,
      displayText: `${p.alias} - ${p.model} - remote`,
      uniqueId: `remote:${wizardComputeHost}:${p.alias}`,
      nodeId: wizardComputeHost
    }));

    return [...local, ...remote];
  });

  // Load instruction sets when modal opens
  $effect(() => {
    if (open && !instructionSets) {
      loadInstructionSets();
    }
  });

  // Get current instruction set to display
  let currentInstructions = $derived(instructionSets?.sets[currentSetKey] || null);
  let displayInstructions = $derived(editMode && editedInstructions ? editedInstructions : currentInstructions);

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

  // Show creation mode choice dialog
  function createNewSet() {
    showCreationModeDialog = true;
  }

  // Create empty instruction set (from scratch)
  async function createFromScratch() {
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

        // Close choice dialog
        showCreationModeDialog = false;

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

  // Template import functions
  async function openTemplateDialog() {
    showTemplateDialog = true;
    selectedTemplate = null;
    newSetName = '';

    // Load available templates
    try {
      const result = await sendCommand('get_available_templates', {});
      if (result && result.status === 'success') {
        availableTemplates = result.templates || [];
      } else {
        console.error('Failed to load templates:', result?.message);
        availableTemplates = [];
      }
    } catch (error) {
      console.error('Error loading templates:', error);
      availableTemplates = [];
    }
  }

  async function importSelectedTemplate() {
    if (!selectedTemplate || !newSetName.trim()) {
      alert('Please select a template and enter a name for the new instruction set.');
      return;
    }

    const key = newSetName.toLowerCase().replace(/\s+/g, '-');

    try {
      const result = await sendCommand('import_instruction_template', {
        template_file: selectedTemplate.file,
        set_key: key,
        set_name: newSetName
      });

      if (result && result.status === 'success') {
        saveMessage = result.message;
        saveMessageType = 'success';

        // Close dialog
        showTemplateDialog = false;

        // Reload instruction sets to get the new one
        await loadInstructionSets();
        currentSetKey = key;

        setTimeout(() => {
          saveMessage = '';
          saveMessageType = '';
        }, 2000);
      } else {
        saveMessage = result?.message || 'Import failed';
        saveMessageType = 'error';
      }
    } catch (error) {
      console.error('Error importing template:', error);
      saveMessage = `Error: ${error}`;
      saveMessageType = 'error';
    }
  }

  function closeTemplateDialog() {
    showTemplateDialog = false;
    selectedTemplate = null;
    newSetName = '';
  }

  // AI Wizard functions
  async function startWizard() {
    // Close choice dialog
    showCreationModeDialog = false;

    // Ask for instruction set name first
    const name = prompt('Enter name for new instruction set:');
    if (!name) return;

    wizardNewSetName = name;

    // Load wizard template and available providers
    try {
      const [wizardResult, providersResult] = await Promise.all([
        sendCommand('get_wizard_template', {}),
        sendCommand('get_providers_list', {})
      ]);

      if (wizardResult && wizardResult.status === 'success' && wizardResult.wizard) {
        wizardQuestions = wizardResult.wizard.question_sequence;
        currentQuestionIndex = 0;
        wizardResponses = {};
        wizardCurrentAnswer = '';

        // Load available providers (cache local providers)
        wizardLocalProviders = [];
        if (providersResult && providersResult.providers && providersResult.providers.length > 0) {
          wizardLocalProviders = [...providersResult.providers];
        }

        // Set defaults: local host, first provider
        wizardComputeHost = 'local';
        if (wizardLocalProviders.length > 0) {
          wizardSelectedProvider = `local:${wizardLocalProviders[0].alias}`;
        }

        // Show wizard dialog
        showWizardDialog = true;
      } else {
        console.error('Failed to load wizard template:', wizardResult);
        alert('Failed to load wizard template');
      }
    } catch (error) {
      console.error('Error loading wizard:', error);
      alert(`Error loading wizard: ${error}`);
    }
  }

  // Helper function to parse provider selection (matches Local AI chat pattern)
  function parseProviderSelection(uniqueId: string): { source: 'local' | 'remote', alias: string, nodeId?: string } {
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

  async function submitWizardAnswer() {
    if (!wizardCurrentAnswer.trim()) {
      alert('Please provide an answer');
      return;
    }

    // Save answer
    wizardResponses[wizardQuestions[currentQuestionIndex].id] = wizardCurrentAnswer;

    // Move to next question or finish
    if (currentQuestionIndex < wizardQuestions.length - 1) {
      currentQuestionIndex++;
      wizardCurrentAnswer = '';
    } else {
      // All questions answered, generate instruction set
      await finishWizard();
    }
  }

  async function finishWizard() {
    isGenerating = true;

    try {
      // Parse provider selection
      const provider = parseProviderSelection(wizardSelectedProvider);
      let result;

      if (provider.source === 'remote' && provider.nodeId) {
        // Generate instruction set using remote inference
        result = await sendCommand('ai_assisted_instruction_creation_remote', {
          user_responses: wizardResponses,
          peer_node_id: provider.nodeId
        });
      } else {
        // Generate instruction set using local AI
        result = await sendCommand('ai_assisted_instruction_creation', {
          user_responses: wizardResponses,
          provider: provider.alias
          // model is optional - provider already has a configured model
        });
      }

      if (result && result.status === 'success') {
        // Create instruction set with generated data
        const instructionData = result.instruction_data;
        const key = wizardNewSetName.toLowerCase().replace(/\s+/g, '-');

        const createResult = await sendCommand('save_instructions', {
          set_key: key,
          instructions_dict: {
            ...instructionData,
            name: wizardNewSetName  // Use user-provided name
          }
        });

        if (createResult && createResult.status === 'success') {
          saveMessage = 'Instruction set created successfully via AI wizard!';
          saveMessageType = 'success';

          // Close wizard
          showWizardDialog = false;

          // Reload and switch to new set
          await loadInstructionSets();
          currentSetKey = key;

          setTimeout(() => {
            saveMessage = '';
            saveMessageType = '';
          }, 3000);
        } else {
          saveMessage = createResult?.message || 'Failed to save instruction set';
          saveMessageType = 'error';
        }
      } else {
        saveMessage = result?.message || 'Failed to generate instruction set';
        saveMessageType = 'error';
      }
    } catch (error) {
      console.error('Error in wizard:', error);
      saveMessage = `Error: ${error}`;
      saveMessageType = 'error';
    } finally {
      isGenerating = false;
    }
  }

  function closeWizardDialog() {
    if (confirm('Are you sure you want to cancel the wizard? Your progress will be lost.')) {
      showWizardDialog = false;
      wizardResponses = {};
      currentQuestionIndex = 0;
      wizardCurrentAnswer = '';
      wizardNewSetName = '';
    }
  }

  function closeCreationModeDialog() {
    showCreationModeDialog = false;
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
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="modal-overlay" onclick={close} onkeydown={handleKeydown} role="presentation">
    <div class="modal" onclick={(e) => e.stopPropagation()} role="dialog" aria-labelledby="instructions-dialog-title" tabindex="-1">
      <div class="modal-header">
        <h2 id="instructions-dialog-title">AI Instructions</h2>
        <div class="header-actions">
          {#if !editMode}
            <button class="btn btn-reload" onclick={reloadInstructions} disabled={isLoading}>
              {isLoading ? 'Loading...' : 'Reload from Disk'}
            </button>
            <button class="btn btn-edit" onclick={startEditing}>Edit</button>
          {:else}
            <button class="btn btn-save" onclick={saveChanges} disabled={isSaving}>
              {isSaving ? 'Saving...' : 'Save'}
            </button>
            <button class="btn btn-cancel" onclick={cancelEditing}>Cancel</button>
          {/if}
        </div>
        <button class="close-btn" onclick={close}>&times;</button>
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
                onclick={() => switchSet(key)}
              >
                {set.name}
                {#if instructionSets.default === key}
                  <span class="default-badge">⭐</span>
                {/if}
              </button>
            {/each}
          </div>
          <div class="tabs-actions">
            <button class="btn btn-new" onclick={createNewSet}>+ New</button>
            <button class="btn btn-import" onclick={openTemplateDialog}>📋 Import Template</button>
          </div>
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
                  <button class="btn btn-default" onclick={setAsDefault}>Set as Default</button>
                {/if}
                {#if currentSetKey !== 'general'}
                  <button class="btn btn-delete" onclick={deleteSet}>Delete</button>
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

<!-- Template Import Dialog -->
{#if showTemplateDialog}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="modal-overlay" onclick={closeTemplateDialog} role="presentation">
    <div class="template-dialog" onclick={(e) => e.stopPropagation()} role="dialog" aria-labelledby="template-dialog-title" tabindex="-1">
      <div class="modal-header">
        <h2 id="template-dialog-title">Import Instruction Template</h2>
        <button class="close-btn" onclick={closeTemplateDialog} aria-label="Close">×</button>
      </div>

      <div class="template-dialog-body">
        <p class="template-help">Select a template to create a new instruction set based on proven patterns.</p>

        {#if availableTemplates.length === 0}
          <div class="loading">No templates available</div>
        {:else}
          <div class="template-list">
            {#each availableTemplates as template}
              <label class="template-option" class:selected={selectedTemplate?.key === template.key}>
                <input
                  type="radio"
                  name="template"
                  value={template.key}
                  onchange={() => selectedTemplate = template}
                />
                <div class="template-info">
                  <strong>{template.name}</strong>
                  <p class="template-description">{template.description}</p>
                </div>
              </label>
            {/each}
          </div>

          <div class="template-name-input">
            <label for="new-set-name">
              <strong>Instruction Set Name:</strong>
              <input
                id="new-set-name"
                type="text"
                class="edit-input"
                placeholder="e.g., My Learning Sessions"
                bind:value={newSetName}
              />
            </label>
          </div>

          <div class="template-dialog-actions">
            <button class="btn btn-cancel" onclick={closeTemplateDialog}>Cancel</button>
            <button
              class="btn btn-save"
              disabled={!selectedTemplate || !newSetName.trim()}
              onclick={importSelectedTemplate}
            >
              Import Template
            </button>
          </div>
        {/if}
      </div>
    </div>
  </div>
{/if}

<!-- Creation Mode Choice Dialog -->
{#if showCreationModeDialog}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="modal-overlay" onclick={closeCreationModeDialog} role="presentation">
    <div class="choice-dialog" onclick={(e) => e.stopPropagation()} role="dialog" aria-labelledby="choice-dialog-title" tabindex="-1">
      <div class="modal-header">
        <h2 id="choice-dialog-title">Create New Instruction Set</h2>
        <button class="close-btn" onclick={closeCreationModeDialog} aria-label="Close">×</button>
      </div>

      <div class="choice-dialog-body">
        <p class="choice-help">Choose how you'd like to create your new instruction set:</p>

        <div class="choice-options">
          <button class="choice-option" onclick={createFromScratch}>
            <div class="choice-info">
              <strong>Create from Scratch</strong>
              <p>Start with an empty instruction set and customize it manually.</p>
            </div>
          </button>

          <button class="choice-option" onclick={startWizard}>
            <div class="choice-info">
              <strong>Use AI Wizard</strong>
              <p>Let an AI interview you and generate a custom instruction set tailored to your needs.</p>
            </div>
          </button>
        </div>
      </div>
    </div>
  </div>
{/if}

<!-- AI Wizard Dialog -->
{#if showWizardDialog}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="modal-overlay" onclick={() => {}} role="presentation">
    <div class="wizard-dialog" onclick={(e) => e.stopPropagation()} role="dialog" aria-labelledby="wizard-dialog-title" tabindex="-1">
      <div class="modal-header">
        <h2 id="wizard-dialog-title">AI Instruction Set Wizard</h2>
        <button class="close-btn" onclick={closeWizardDialog} aria-label="Close">×</button>
      </div>

      <div class="wizard-dialog-body">
        {#if !isGenerating}
          <!-- AI Host & Model Selection (matches Local AI chat pattern) -->
          <div class="wizard-model-selection">
            <!-- AI Host Dropdown -->
            <label for="wizard-host">
              <strong>AI Host:</strong>
              <select
                id="wizard-host"
                bind:value={wizardComputeHost}
                class="wizard-select"
              >
                <option value="local">Local</option>
                {#if $nodeStatus?.peer_info && $nodeStatus.peer_info.length > 0}
                  {#each $nodeStatus.peer_info as peer}
                    <option value={peer.node_id}>
                      {peer.name || `${peer.node_id.slice(0, 20)}...`}
                    </option>
                  {/each}
                {/if}
              </select>
            </label>

            <!-- Text Model Dropdown (filtered based on selected host) -->
            <label for="wizard-model" style="margin-top: 1rem;">
              <strong>Text Model:</strong>
              <select
                id="wizard-model"
                bind:value={wizardSelectedProvider}
                class="wizard-select"
              >
                {#if wizardMergedProviders.length === 0}
                  <option value="" disabled>No providers available</option>
                {:else}
                  {#each wizardMergedProviders as provider}
                    <option value={provider.uniqueId}>
                      {provider.displayText}
                    </option>
                  {/each}
                {/if}
              </select>
            </label>
          </div>

          <!-- Progress indicator -->
          <div class="wizard-progress">
            <div class="wizard-progress-text">
              Question {currentQuestionIndex + 1} of {wizardQuestions.length}
            </div>
            <div class="wizard-progress-bar">
              <div class="wizard-progress-fill" style="width: {((currentQuestionIndex + 1) / wizardQuestions.length) * 100}%"></div>
            </div>
          </div>

          <!-- Current question -->
          {#if wizardQuestions[currentQuestionIndex]}
            <div class="wizard-question">
              <div class="wizard-question-text">
                {wizardQuestions[currentQuestionIndex].question}
              </div>
            </div>

            <!-- Answer input -->
            <div class="wizard-answer">
              <textarea
                class="wizard-textarea"
                placeholder="Type your answer here..."
                bind:value={wizardCurrentAnswer}
                rows="6"
              ></textarea>
            </div>

            <!-- Navigation buttons -->
            <div class="wizard-actions">
              {#if currentQuestionIndex > 0}
                <button
                  class="btn btn-cancel"
                  onclick={() => {
                    currentQuestionIndex--;
                    wizardCurrentAnswer = wizardResponses[wizardQuestions[currentQuestionIndex].id] || '';
                  }}
                >
                  ← Previous
                </button>
              {/if}

              <button
                class="btn btn-save"
                disabled={!wizardCurrentAnswer.trim()}
                onclick={submitWizardAnswer}
              >
                {currentQuestionIndex === wizardQuestions.length - 1 ? 'Generate Instruction Set' : 'Next →'}
              </button>
            </div>
          {/if}
        {:else}
          <!-- Generating state -->
          <div class="wizard-generating">
            <div class="wizard-spinner"></div>
            <p>Generating your custom instruction set...</p>
            <p class="wizard-generating-sub">This may take a moment. The AI is analyzing your responses and creating tailored instructions.</p>
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

  /* Template Import Dialog Styles */
  .template-dialog {
    background: #1e1e1e;
    border: 1px solid #333;
    border-radius: 8px;
    width: 90%;
    max-width: 600px;
    max-height: 80vh;
    display: flex;
    flex-direction: column;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.5);
  }

  .template-dialog-body {
    padding: 1.5rem;
    overflow-y: auto;
    flex: 1;
  }

  .template-help {
    margin: 0 0 1.5rem 0;
    color: #aaa;
    font-size: 0.95rem;
  }

  .template-list {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    margin-bottom: 1.5rem;
  }

  .template-option {
    display: flex;
    align-items: flex-start;
    gap: 1rem;
    padding: 1rem;
    background: #252525;
    border: 2px solid #3a3a3a;
    border-radius: 6px;
    cursor: pointer;
    transition: all 0.2s;
  }

  .template-option:hover {
    background: #2a2a2a;
    border-color: #555;
  }

  .template-option.selected {
    border-color: #007acc;
    background: #2a2a2a;
  }

  .template-option input[type="radio"] {
    margin-top: 0.25rem;
    cursor: pointer;
  }

  .template-info {
    flex: 1;
  }

  .template-info strong {
    display: block;
    color: #fff;
    margin-bottom: 0.5rem;
    font-size: 1rem;
  }

  .template-description {
    margin: 0;
    color: #aaa;
    font-size: 0.9rem;
    line-height: 1.4;
  }

  .template-name-input {
    margin-bottom: 1.5rem;
  }

  .template-name-input label {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .template-name-input strong {
    color: #fff;
    font-size: 0.95rem;
  }

  .template-dialog-actions {
    display: flex;
    justify-content: flex-end;
    gap: 0.75rem;
    padding-top: 1rem;
    border-top: 1px solid #333;
  }

  .btn-import {
    background: #007acc;
    color: #fff;
    border: 1px solid #007acc;
    padding: 0.5rem 1rem;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.9rem;
    white-space: nowrap;
  }

  .btn-import:hover {
    background: #005a9e;
  }

  .tabs-actions {
    display: flex;
    gap: 0.5rem;
  }

  /* Choice Dialog Styles */
  .choice-dialog {
    background: #1e1e1e;
    border: 1px solid #333;
    border-radius: 8px;
    width: 90%;
    max-width: 550px;
    display: flex;
    flex-direction: column;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.5);
  }

  .choice-dialog-body {
    padding: 1.5rem;
  }

  .choice-help {
    margin: 0 0 1.5rem 0;
    color: #aaa;
    font-size: 0.95rem;
  }

  .choice-options {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .choice-option {
    display: flex;
    align-items: center;
    gap: 1.5rem;
    padding: 1.5rem;
    background: #252525;
    border: 2px solid #3a3a3a;
    border-radius: 8px;
    cursor: pointer;
    transition: all 0.2s;
    text-align: left;
  }

  .choice-option:hover {
    background: #2a2a2a;
    border-color: #007acc;
    transform: translateY(-2px);
    box-shadow: 0 4px 12px rgba(0, 122, 204, 0.2);
  }

  .choice-info {
    flex: 1;
  }

  .choice-info strong {
    display: block;
    color: #fff;
    font-size: 1.1rem;
    margin-bottom: 0.5rem;
  }

  .choice-info p {
    margin: 0;
    color: #aaa;
    font-size: 0.9rem;
    line-height: 1.4;
  }

  /* Wizard Dialog Styles */
  .wizard-dialog {
    background: #1e1e1e;
    border: 1px solid #333;
    border-radius: 8px;
    width: 90%;
    max-width: 700px;
    max-height: 85vh;
    display: flex;
    flex-direction: column;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.5);
  }

  .wizard-dialog-body {
    padding: 2rem;
    overflow-y: auto;
    flex: 1;
  }

  .wizard-model-selection {
    display: flex;
    gap: 1rem;
    margin-bottom: 2rem;
    padding-bottom: 2rem;
    border-bottom: 1px solid #333;
  }

  .wizard-model-selection label {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    flex: 1;
  }

  .wizard-model-selection strong {
    color: #aaa;
    font-size: 0.9rem;
    font-weight: 500;
  }

  .wizard-select {
    padding: 0.75rem;
    background: #1a1a1a;
    border: 2px solid #3a3a3a;
    border-radius: 6px;
    color: #e0e0e0;
    font-size: 0.95rem;
    font-family: inherit;
    transition: border-color 0.2s;
  }

  .wizard-select:focus {
    outline: none;
    border-color: #007acc;
    box-shadow: 0 0 0 3px rgba(0, 122, 204, 0.1);
  }

  .wizard-progress {
    margin-bottom: 2rem;
  }

  .wizard-progress-text {
    color: #aaa;
    font-size: 0.9rem;
    margin-bottom: 0.5rem;
  }

  .wizard-progress-bar {
    height: 6px;
    background: #2a2a2a;
    border-radius: 3px;
    overflow: hidden;
  }

  .wizard-progress-fill {
    height: 100%;
    background: linear-gradient(90deg, #007acc, #00a8ff);
    transition: width 0.3s ease;
  }

  .wizard-question {
    margin-bottom: 1.5rem;
  }

  .wizard-question-text {
    font-size: 1.1rem;
    color: #fff;
    line-height: 1.6;
    white-space: pre-wrap;
  }

  .wizard-answer {
    margin-bottom: 1.5rem;
  }

  .wizard-textarea {
    width: 100%;
    padding: 1rem;
    background: #1a1a1a;
    border: 2px solid #3a3a3a;
    border-radius: 6px;
    color: #e0e0e0;
    font-size: 1rem;
    font-family: inherit;
    line-height: 1.5;
    resize: vertical;
    min-height: 150px;
    transition: border-color 0.2s;
  }

  .wizard-textarea:focus {
    outline: none;
    border-color: #007acc;
    box-shadow: 0 0 0 3px rgba(0, 122, 204, 0.1);
  }

  .wizard-actions {
    display: flex;
    justify-content: flex-end;
    gap: 0.75rem;
    padding-top: 1rem;
    border-top: 1px solid #333;
  }

  .wizard-generating {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 3rem 2rem;
    text-align: center;
  }

  .wizard-spinner {
    width: 50px;
    height: 50px;
    border: 4px solid #2a2a2a;
    border-top-color: #007acc;
    border-radius: 50%;
    animation: wizard-spin 1s linear infinite;
    margin-bottom: 1.5rem;
  }

  @keyframes wizard-spin {
    to { transform: rotate(360deg); }
  }

  .wizard-generating p {
    color: #fff;
    font-size: 1.1rem;
    margin: 0 0 0.5rem 0;
  }

  .wizard-generating-sub {
    color: #aaa !important;
    font-size: 0.9rem !important;
  }
</style>
