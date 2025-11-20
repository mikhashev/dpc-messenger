<!-- FirewallEditor.svelte -->
<!-- View and manage firewall access control rules -->

<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { sendCommand } from '$lib/coreService';

  export let open: boolean = false;

  const dispatch = createEventDispatcher();

  type FirewallRules = {
    _comment?: string;
    hub?: Record<string, string>;
    node_groups?: Record<string, string[]>;
    file_groups?: Record<string, string[]>;
    compute?: {
      _comment?: string;
      enabled: boolean;
      allow_nodes: string[];
      allow_groups: string[];
      allowed_models: string[];
    };
    nodes?: Record<string, Record<string, string>>;
    groups?: Record<string, Record<string, string>>;
    ai_scopes?: Record<string, Record<string, string>>;
    device_sharing?: Record<string, Record<string, string>>;
  };

  let rules: FirewallRules | null = null;
  let selectedTab: 'hub' | 'groups' | 'compute' | 'peers' = 'hub';
  let editMode: boolean = false;
  let editedRules: FirewallRules | null = null;
  let isSaving: boolean = false;
  let saveMessage: string = '';
  let saveMessageType: 'success' | 'error' | '' = '';

  // Intermediate string variables for textarea editing
  let allowNodesText: string = '';
  let allowGroupsText: string = '';
  let allowedModelsText: string = '';

  // Load rules when modal opens
  $: if (open && !rules) {
    loadRules();
  }

  // Sync string variables with arrays when entering edit mode
  $: if (editMode && editedRules?.compute) {
    allowNodesText = editedRules.compute.allow_nodes.join('\n');
    allowGroupsText = editedRules.compute.allow_groups.join('\n');
    allowedModelsText = editedRules.compute.allowed_models.join('\n');
  }

  async function loadRules() {
    try {
      const result = await sendCommand('get_firewall_rules', {});
      if (result.status === 'success') {
        rules = result.rules;
      } else {
        console.error('Failed to load firewall rules:', result.message);
      }
    } catch (error) {
      console.error('Error loading firewall rules:', error);
    }
  }

  // Enter edit mode
  function startEditing() {
    if (!rules) return;
    editMode = true;
    // Deep copy the rules for editing
    editedRules = JSON.parse(JSON.stringify(rules));
  }

  // Cancel editing
  function cancelEditing() {
    editMode = false;
    editedRules = null;
    saveMessage = '';
    saveMessageType = '';
  }

  // Save changes
  async function saveChanges() {
    if (!editedRules) return;

    isSaving = true;
    saveMessage = '';
    saveMessageType = '';

    try {
      const result = await sendCommand('save_firewall_rules', {
        rules_dict: editedRules
      });

      if (result.status === 'success') {
        saveMessage = result.message;
        saveMessageType = 'success';

        // Update the displayed rules
        rules = JSON.parse(JSON.stringify(editedRules));

        // Exit edit mode immediately (so close button works correctly)
        editMode = false;
        editedRules = null;

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
      console.error('Error saving firewall rules:', error);
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
    editedRules = null;
    rules = null;
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

  // Get the rules to display (edited or original)
  $: displayRules = editMode && editedRules ? editedRules : rules;

  // Helper functions for editing
  function addNodeGroup() {
    if (!editedRules) return;
    const groupName = prompt('Enter group name:');
    if (groupName) {
      if (!editedRules.node_groups) editedRules.node_groups = {};
      editedRules.node_groups[groupName] = [];
    }
  }

  function removeNodeGroup(groupName: string) {
    if (!editedRules || !editedRules.node_groups) return;
    delete editedRules.node_groups[groupName];
  }

  function addNodeToGroup(groupName: string) {
    if (!editedRules || !editedRules.node_groups) return;
    const nodeId = prompt('Enter node ID (e.g., dpc-node-alice-123):');
    if (nodeId && nodeId.startsWith('dpc-node-')) {
      editedRules.node_groups[groupName].push(nodeId);
    } else if (nodeId) {
      alert('Node ID must start with "dpc-node-"');
    }
  }

  function removeNodeFromGroup(groupName: string, nodeId: string) {
    if (!editedRules || !editedRules.node_groups) return;
    editedRules.node_groups[groupName] = editedRules.node_groups[groupName].filter(id => id !== nodeId);
  }
</script>

{#if open && rules}
  <!-- svelte-ignore a11y-click-events-have-key-events -->
  <!-- svelte-ignore a11y-no-static-element-interactions -->
  <div class="modal-overlay" on:click={close} on:keydown={handleKeydown} role="presentation">
    <div class="modal" on:click|stopPropagation role="dialog" aria-labelledby="firewall-dialog-title" tabindex="-1">
      <div class="modal-header">
        <h2 id="firewall-dialog-title">Firewall Access Control</h2>
        <div class="header-actions">
          {#if !editMode}
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

      <!-- Tabs -->
      <div class="tabs">
        <button
          class="tab"
          class:active={selectedTab === 'hub'}
          on:click={() => selectedTab = 'hub'}
        >
          Hub Sharing
        </button>
        <button
          class="tab"
          class:active={selectedTab === 'groups'}
          on:click={() => selectedTab = 'groups'}
        >
          Node Groups
        </button>
        <button
          class="tab"
          class:active={selectedTab === 'compute'}
          on:click={() => selectedTab = 'compute'}
        >
          Compute Sharing
        </button>
        <button
          class="tab"
          class:active={selectedTab === 'peers'}
          on:click={() => selectedTab = 'peers'}
        >
          Peer Permissions
        </button>
      </div>

      <div class="modal-body">
        {#if selectedTab === 'hub'}
          <div class="section">
            <h3>Hub Sharing Rules</h3>
            <p class="help-text">Control what information the Hub can see for discovery.</p>

            <div class="info-grid">
              {#if displayRules?.hub}
                {#each Object.entries(displayRules.hub) as [path, action]}
                  {#if !path.startsWith('_')}
                    <div class="rule-item">
                      <strong>{path}:</strong>
                      {#if editMode && editedRules && editedRules.hub}
                        <select bind:value={editedRules.hub[path]}>
                          <option value="allow">allow</option>
                          <option value="deny">deny</option>
                        </select>
                      {:else}
                        <span class="action-badge" class:allow={action === 'allow'} class:deny={action === 'deny'}>
                          {action}
                        </span>
                      {/if}
                    </div>
                  {/if}
                {/each}
              {:else}
                <p class="empty">No hub sharing rules defined.</p>
              {/if}
            </div>

            {#if !editMode}
              <div class="info-box">
                <strong>Default:</strong> By default, the Hub can see your name and description for discovery. All other access is denied.
              </div>
            {/if}
          </div>

        {:else if selectedTab === 'groups'}
          <div class="section">
            <h3>Node Groups</h3>
            <p class="help-text">Define groups of nodes for easier permission management.</p>

            {#if editMode && editedRules}
              <button class="btn btn-add" on:click={addNodeGroup}>+ Add Group</button>
            {/if}

            <div class="groups-list">
              {#if displayRules?.node_groups}
                {#each Object.entries(displayRules.node_groups) as [groupName, nodeList]}
                  {#if !groupName.startsWith('_')}
                    <div class="group-card">
                      <div class="group-header">
                        <h4>{groupName}</h4>
                        {#if editMode}
                          <button class="btn-icon" on:click={() => removeNodeGroup(groupName)}>
                            🗑️
                          </button>
                        {/if}
                      </div>

                      <div class="node-list">
                        {#each nodeList as nodeId}
                          <div class="node-item">
                            <code>{nodeId}</code>
                            {#if editMode}
                              <button class="btn-icon-small" on:click={() => removeNodeFromGroup(groupName, nodeId)}>
                                ×
                              </button>
                            {/if}
                          </div>
                        {:else}
                          <p class="empty-small">No nodes in this group</p>
                        {/each}

                        {#if editMode}
                          <button class="btn-small" on:click={() => addNodeToGroup(groupName)}>
                            + Add Node
                          </button>
                        {/if}
                      </div>
                    </div>
                  {/if}
                {:else}
                  <p class="empty">No node groups defined yet.</p>
                {/each}
              {:else}
                <p class="empty">No node groups defined yet.</p>
              {/if}
            </div>
          </div>

        {:else if selectedTab === 'compute'}
          <div class="section">
            <h3>Compute Sharing (Remote Inference)</h3>
            <p class="help-text">Allow peers to use your local AI models for inference.</p>

            {#if displayRules?.compute}
              <div class="compute-settings">
                <div class="setting-item">
                  <label>
                    {#if editMode && editedRules && editedRules.compute}
                      <input type="checkbox" bind:checked={editedRules.compute.enabled} />
                    {:else}
                      <input type="checkbox" checked={displayRules.compute.enabled} disabled />
                    {/if}
                    <strong>Enable Compute Sharing</strong>
                  </label>
                </div>

                {#if displayRules.compute.enabled}
                  <div class="subsection">
                    <h4>Allowed Nodes</h4>
                    {#if editMode && editedRules}
                      <textarea
                        class="edit-textarea"
                        rows="3"
                        placeholder="Enter node IDs (one per line)"
                        bind:value={allowNodesText}
                        on:blur={() => {
                          if (editedRules?.compute) {
                            editedRules.compute.allow_nodes = allowNodesText.split('\n').map(s => s.trim()).filter(s => s.length > 0);
                          }
                        }}
                      ></textarea>
                    {:else}
                      <div class="tags">
                        {#each displayRules.compute.allow_nodes as nodeId}
                          <span class="tag">{nodeId}</span>
                        {:else}
                          <span class="empty-small">No specific nodes allowed</span>
                        {/each}
                      </div>
                    {/if}
                  </div>

                  <div class="subsection">
                    <h4>Allowed Groups</h4>
                    {#if editMode && editedRules}
                      <textarea
                        class="edit-textarea"
                        rows="2"
                        placeholder="Enter group names (one per line)"
                        bind:value={allowGroupsText}
                        on:blur={() => {
                          if (editedRules?.compute) {
                            editedRules.compute.allow_groups = allowGroupsText.split('\n').map(s => s.trim()).filter(s => s.length > 0);
                          }
                        }}
                      ></textarea>
                    {:else}
                      <div class="tags">
                        {#each displayRules.compute.allow_groups as groupName}
                          <span class="tag">{groupName}</span>
                        {:else}
                          <span class="empty-small">No groups allowed</span>
                        {/each}
                      </div>
                    {/if}
                  </div>

                  <div class="subsection">
                    <h4>Allowed Models</h4>
                    <p class="help-text-small">Leave empty to allow all models.</p>
                    {#if editMode && editedRules}
                      <textarea
                        class="edit-textarea"
                        rows="3"
                        placeholder="Enter model names (one per line)"
                        bind:value={allowedModelsText}
                        on:blur={() => {
                          if (editedRules?.compute) {
                            editedRules.compute.allowed_models = allowedModelsText.split('\n').map(s => s.trim()).filter(s => s.length > 0);
                          }
                        }}
                      ></textarea>
                    {:else}
                      <div class="tags">
                        {#each displayRules.compute.allowed_models as model}
                          <span class="tag">{model}</span>
                        {:else}
                          <span class="empty-small">All models allowed</span>
                        {/each}
                      </div>
                    {/if}
                  </div>
                {/if}
              </div>
            {:else}
              <p class="empty">Compute sharing not configured.</p>
            {/if}
          </div>

        {:else if selectedTab === 'peers'}
          <div class="section">
            <h3>Peer Permissions</h3>
            <p class="help-text">Individual and group-based access rules for nodes.</p>

            <div class="info-box">
              <strong>Note:</strong> Peer permission editing via UI is coming soon. For now, edit the JSON file directly at <code>~/.dpc/.dpc_access.json</code>
            </div>

            {#if displayRules?.nodes}
              <h4>Individual Nodes</h4>
              {#each Object.entries(displayRules.nodes) as [nodeId, rules]}
                {#if !nodeId.startsWith('_')}
                  <div class="peer-card">
                    <h5>{nodeId}</h5>
                    <div class="rule-list">
                      {#each Object.entries(rules) as [path, action]}
                        {#if !path.startsWith('_')}
                          <div class="rule-row">
                            <code class="rule-path">{path}</code>
                            <span class="action-badge" class:allow={action === 'allow'} class:deny={action === 'deny'}>
                              {action}
                            </span>
                          </div>
                        {/if}
                      {/each}
                    </div>
                  </div>
                {/if}
              {:else}
                <p class="empty">No individual node permissions defined.</p>
              {/each}
            {/if}

            {#if displayRules?.groups}
              <h4>Group Permissions</h4>
              {#each Object.entries(displayRules.groups) as [groupName, rules]}
                {#if !groupName.startsWith('_')}
                  <div class="peer-card">
                    <h5>Group: {groupName}</h5>
                    <div class="rule-list">
                      {#each Object.entries(rules) as [path, action]}
                        {#if !path.startsWith('_')}
                          <div class="rule-row">
                            <code class="rule-path">{path}</code>
                            <span class="action-badge" class:allow={action === 'allow'} class:deny={action === 'deny'}>
                              {action}
                            </span>
                          </div>
                        {/if}
                      {/each}
                    </div>
                  </div>
                {/if}
              {:else}
                <p class="empty">No group permissions defined.</p>
              {/each}
            {/if}
          </div>
        {/if}
      </div>

      <div class="modal-footer">
        <button class="btn btn-close" on:click={close}>Close</button>
      </div>
    </div>
  </div>
{/if}

<style>
  /* Copy all styles from ContextViewer */
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
    background: white;
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
    padding: 1rem 1.5rem;
    border-bottom: 1px solid #e0e0e0;
  }

  .modal-header h2 {
    margin: 0;
    font-size: 1.5rem;
    color: #333;
  }

  .close-btn {
    background: none;
    border: none;
    font-size: 2rem;
    cursor: pointer;
    color: #999;
    line-height: 1;
  }

  .close-btn:hover {
    color: #333;
  }

  .tabs {
    display: flex;
    border-bottom: 2px solid #e0e0e0;
    background: #f9f9f9;
  }

  .tab {
    flex: 1;
    padding: 0.75rem 1rem;
    border: none;
    background: none;
    cursor: pointer;
    font-size: 0.95rem;
    color: #666;
    transition: all 0.2s;
  }

  .tab:hover {
    background: #f0f0f0;
  }

  .tab.active {
    color: #1976d2;
    border-bottom: 3px solid #1976d2;
    background: white;
  }

  .modal-body {
    padding: 1.5rem;
    overflow-y: auto;
    flex: 1;
  }

  .section {
    margin-bottom: 1.5rem;
  }

  .section h3 {
    margin: 0 0 0.5rem 0;
    font-size: 1.2rem;
    color: #333;
  }

  .section h4 {
    margin: 1rem 0 0.5rem 0;
    font-size: 1rem;
    color: #555;
  }

  .help-text {
    color: #666;
    font-size: 0.9rem;
    margin: 0 0 1rem 0;
  }

  .help-text-small {
    color: #666;
    font-size: 0.85rem;
    margin: 0.25rem 0 0.5rem 0;
  }

  .info-grid {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .rule-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.5rem;
    background: #f9f9f9;
    border-radius: 4px;
  }

  .rule-item strong {
    color: #666;
    font-size: 0.9rem;
    font-family: monospace;
  }

  .action-badge {
    padding: 0.25rem 0.75rem;
    border-radius: 12px;
    font-size: 0.85rem;
    font-weight: 500;
  }

  .action-badge.allow {
    background: #d4edda;
    color: #155724;
  }

  .action-badge.deny {
    background: #f8d7da;
    color: #721c24;
  }

  .groups-list {
    margin-top: 1rem;
  }

  .group-card {
    border: 1px solid #e0e0e0;
    border-radius: 8px;
    padding: 1rem;
    margin-bottom: 1rem;
    background: #fafafa;
  }

  .group-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.75rem;
  }

  .group-header h4 {
    margin: 0;
    color: #333;
  }

  .node-list {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .node-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.5rem;
    background: white;
    border-radius: 4px;
    border: 1px solid #e0e0e0;
  }

  .node-item code {
    font-size: 0.9rem;
    color: #333;
  }

  .compute-settings {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .setting-item {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .subsection {
    margin-top: 1rem;
    padding-left: 1rem;
    border-left: 3px solid #e0e0e0;
  }

  .subsection h4 {
    margin-top: 0;
  }

  .tags {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    margin-top: 0.5rem;
  }

  .tag {
    background: #e3f2fd;
    color: #0d47a1;
    padding: 0.25rem 0.75rem;
    border-radius: 12px;
    font-size: 0.9rem;
  }

  .peer-card {
    border: 1px solid #e0e0e0;
    border-radius: 8px;
    padding: 1rem;
    margin-bottom: 1rem;
    background: #fafafa;
  }

  .peer-card h5 {
    margin: 0 0 0.75rem 0;
    color: #333;
    font-family: monospace;
  }

  .rule-list {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .rule-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.5rem;
    background: white;
    border-radius: 4px;
  }

  .rule-path {
    font-size: 0.85rem;
    color: #555;
  }

  .empty {
    color: #999;
    font-style: italic;
    text-align: center;
    padding: 2rem;
  }

  .empty-small {
    color: #999;
    font-style: italic;
    font-size: 0.9rem;
  }

  .info-box {
    background: #e3f2fd;
    border-left: 3px solid #1976d2;
    padding: 0.75rem;
    margin-top: 1rem;
    font-size: 0.9rem;
  }

  .modal-footer {
    padding: 1rem 1.5rem;
    border-top: 1px solid #e0e0e0;
    display: flex;
    justify-content: flex-end;
  }

  /* Edit Mode Styles */
  .header-actions {
    display: flex;
    gap: 0.5rem;
    margin: 0 1rem;
  }

  .btn {
    padding: 0.5rem 1rem;
    border: none;
    border-radius: 4px;
    font-size: 0.9rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s;
  }

  .btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .btn-edit {
    background: #4CAF50;
    color: white;
  }

  .btn-edit:hover {
    background: #45a049;
  }

  .btn-save {
    background: #4CAF50;
    color: white;
  }

  .btn-save:hover:not(:disabled) {
    background: #45a049;
  }

  .btn-cancel {
    background: #999;
    color: white;
  }

  .btn-cancel:hover {
    background: #777;
  }

  .btn-add {
    background: #2196F3;
    color: white;
    margin-bottom: 1rem;
  }

  .btn-add:hover {
    background: #0b7dda;
  }

  .btn-small {
    padding: 0.25rem 0.75rem;
    font-size: 0.85rem;
    background: #2196F3;
    color: white;
    border: none;
    border-radius: 4px;
    cursor: pointer;
  }

  .btn-icon {
    background: none;
    border: none;
    cursor: pointer;
    font-size: 1.2rem;
  }

  .btn-icon-small {
    background: none;
    border: none;
    cursor: pointer;
    font-size: 1.5rem;
    color: #999;
  }

  .btn-icon-small:hover {
    color: #f44336;
  }

  .save-message {
    padding: 0.75rem 1.5rem;
    margin: 0;
    font-size: 0.9rem;
  }

  .save-message.success {
    background: #d4edda;
    color: #155724;
    border-bottom: 1px solid #c3e6cb;
  }

  .save-message.error {
    background: #f8d7da;
    color: #721c24;
    border-bottom: 1px solid #f5c6cb;
    white-space: pre-wrap;
  }

  .edit-textarea {
    width: 100%;
    padding: 0.5rem;
    border: 1px solid #ccc;
    border-radius: 4px;
    font-size: 0.9rem;
    font-family: monospace;
    resize: vertical;
    transition: border-color 0.2s;
  }

  .edit-textarea:focus {
    outline: none;
    border-color: #4CAF50;
  }

  .btn-close {
    padding: 0.75rem 2rem;
    border: none;
    border-radius: 6px;
    background: #666;
    color: white;
    font-size: 1rem;
    cursor: pointer;
  }

  .btn-close:hover {
    background: #555;
  }

  select {
    padding: 0.25rem 0.5rem;
    border: 1px solid #ccc;
    border-radius: 4px;
    font-size: 0.9rem;
    cursor: pointer;
  }
</style>
