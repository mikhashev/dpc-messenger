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
    file_transfer?: {
      _comment?: string;
      groups?: Record<string, any>;
      nodes?: Record<string, any>;
    };
    image_transfer?: {
      _comment?: string;
      auto_accept_threshold_mb: number;
      allowed_sources: string[];
      max_size_mb: number;
      save_screenshots_to_disk: boolean;
    };
    notifications?: {
      _comment?: string;
      enabled: boolean;
      events: Record<string, boolean>;
    };
    nodes?: Record<string, Record<string, string>>;
    groups?: Record<string, Record<string, string>>;
    ai_scopes?: Record<string, Record<string, string>>;
    device_sharing?: Record<string, Record<string, string>>;
  };

  let rules: FirewallRules | null = null;
  let selectedTab: 'hub' | 'groups' | 'file-groups' | 'ai-scopes' | 'device-sharing' | 'compute' | 'file-transfer' | 'image-transfer' | 'notifications' | 'peers' = 'hub';
  let editMode: boolean = false;
  let editedRules: FirewallRules | null = null;
  let isSaving: boolean = false;
  let saveMessage: string = '';
  let saveMessageType: 'success' | 'error' | '' = '';

  // Intermediate string variables for textarea editing (compute sharing)
  let allowNodesText: string = '';
  let allowGroupsText: string = '';
  let allowedModelsText: string = '';

  // Intermediate string variables for textarea editing (transcription sharing)
  let transcriptionAllowNodesText: string = '';
  let transcriptionAllowGroupsText: string = '';
  let transcriptionAllowedModelsText: string = '';

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

  // Sync transcription string variables with arrays when entering edit mode
  $: if (editMode && editedRules?.transcription) {
    transcriptionAllowNodesText = editedRules.transcription.allow_nodes.join('\n');
    transcriptionAllowGroupsText = editedRules.transcription.allow_groups.join('\n');
    transcriptionAllowedModelsText = editedRules.transcription.allowed_models.join('\n');
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
      // Check for duplicates
      if (editedRules.node_groups[groupName]) {
        alert('This group already exists');
        return;
      }
      editedRules.node_groups[groupName] = [];
    }
  }

  function removeNodeGroup(groupName: string) {
    if (!editedRules || !editedRules.node_groups) return;
    delete editedRules.node_groups[groupName];
    editedRules = editedRules;  // Trigger Svelte reactivity
  }

  function addNodeToGroup(groupName: string) {
    if (!editedRules || !editedRules.node_groups) return;
    const nodeId = prompt('Enter node ID (e.g., dpc-node-alice-123):');
    const trimmedNodeId = nodeId?.trim();
    if (trimmedNodeId && trimmedNodeId.startsWith('dpc-node-')) {
      // Check for duplicates
      if (editedRules.node_groups[groupName].includes(trimmedNodeId)) {
        alert('This node is already in the group');
        return;
      }
      // Use immutable update to trigger Svelte reactivity
      editedRules.node_groups[groupName] = [
        ...editedRules.node_groups[groupName],
        trimmedNodeId
      ];
    } else if (nodeId) {
      alert('Node ID must start with "dpc-node-"');
    }
  }

  function removeNodeFromGroup(groupName: string, nodeId: string) {
    if (!editedRules || !editedRules.node_groups) return;
    editedRules.node_groups[groupName] = editedRules.node_groups[groupName].filter(id => id !== nodeId);
  }

  // Peer Permissions - Node Management Functions
  function addNodePermission() {
    if (!editedRules) return;
    const nodeId = prompt('Enter node ID (e.g., dpc-node-alice-123):');
    const trimmedNodeId = nodeId?.trim();
    if (trimmedNodeId && trimmedNodeId.startsWith('dpc-node-')) {
      if (!editedRules.nodes) editedRules.nodes = {};
      // Check for duplicates
      if (editedRules.nodes[trimmedNodeId]) {
        alert('This node already has permission rules');
        return;
      }
      editedRules.nodes[trimmedNodeId] = {};
    } else if (nodeId) {
      alert('Node ID must start with "dpc-node-"');
    }
  }

  function removeNodePermission(nodeId: string) {
    if (!editedRules || !editedRules.nodes) return;
    delete editedRules.nodes[nodeId];
    editedRules = editedRules;  // Trigger Svelte reactivity
  }

  function addRuleToNode(nodeId: string) {
    if (!editedRules || !editedRules.nodes) return;
    const resourcePath = prompt('Enter resource path (e.g., personal.json:profile.*, device_context.json:hardware.*, or personal.json:*):', 'personal.json:profile.*');
    if (resourcePath) {
      // Check for duplicates
      if (editedRules.nodes[nodeId][resourcePath]) {
        alert('This rule already exists for this node');
        return;
      }
      editedRules.nodes[nodeId][resourcePath] = 'allow';
    }
  }

  function removeRuleFromNode(nodeId: string, path: string) {
    if (!editedRules || !editedRules.nodes) return;
    delete editedRules.nodes[nodeId][path];
    editedRules = editedRules;  // Trigger Svelte reactivity
  }

  // Peer Permissions - Group Management Functions
  function addGroupPermission() {
    if (!editedRules) return;
    const groupName = prompt('Enter group name:');
    if (groupName) {
      if (!editedRules.groups) editedRules.groups = {};
      // Check for duplicates
      if (editedRules.groups[groupName]) {
        alert('This group already has permission rules');
        return;
      }
      editedRules.groups[groupName] = {};
    }
  }

  function removeGroupPermission(groupName: string) {
    if (!editedRules || !editedRules.groups) return;
    delete editedRules.groups[groupName];
    editedRules = editedRules;  // Trigger Svelte reactivity
  }

  function addRuleToGroup(groupName: string) {
    if (!editedRules || !editedRules.groups) return;
    const resourcePath = prompt('Enter resource path (e.g., personal.json:profile.*, device_context.json:hardware.*, or personal.json:*):', 'personal.json:profile.*');
    if (resourcePath) {
      // Check for duplicates
      if (editedRules.groups[groupName][resourcePath]) {
        alert('This rule already exists for this group');
        return;
      }
      editedRules.groups[groupName][resourcePath] = 'allow';
    }
  }

  function removeRuleFromGroup(groupName: string, path: string) {
    if (!editedRules || !editedRules.groups) return;
    delete editedRules.groups[groupName][path];
    editedRules = editedRules;  // Trigger Svelte reactivity
  }

  // File Groups Management Functions
  function addFileGroup() {
    if (!editedRules) return;
    const groupName = prompt('Enter file group name (e.g., "work", "personal"):');
    if (groupName) {
      if (!editedRules.file_groups) editedRules.file_groups = {};
      // Check for duplicates
      if (editedRules.file_groups[groupName]) {
        alert('This file group already exists');
        return;
      }
      editedRules.file_groups[groupName] = [];
    }
  }

  function removeFileGroup(groupName: string) {
    if (!editedRules || !editedRules.file_groups) return;
    delete editedRules.file_groups[groupName];
    editedRules = editedRules;  // Trigger Svelte reactivity
  }

  function addFilePatternToGroup(groupName: string) {
    if (!editedRules || !editedRules.file_groups) return;
    const pattern = prompt('Enter file pattern (e.g., "work_*.json", "personal.json"):');
    if (pattern) {
      // Check for duplicates
      if (editedRules.file_groups[groupName].includes(pattern)) {
        alert('This pattern already exists in the group');
        return;
      }
      // Use immutable update to trigger Svelte reactivity
      editedRules.file_groups[groupName] = [
        ...editedRules.file_groups[groupName],
        pattern
      ];
    }
  }

  function removeFilePatternFromGroup(groupName: string, pattern: string) {
    if (!editedRules || !editedRules.file_groups) return;
    editedRules.file_groups[groupName] = editedRules.file_groups[groupName].filter(p => p !== pattern);
  }

  // AI Scopes Management Functions
  function addAIScope() {
    if (!editedRules) return;
    const scopeName = prompt('Enter AI scope name (e.g., "work", "personal"):');
    if (scopeName) {
      if (!editedRules.ai_scopes) editedRules.ai_scopes = {};
      // Check for duplicates
      if (editedRules.ai_scopes[scopeName]) {
        alert('This AI scope already exists');
        return;
      }
      editedRules.ai_scopes[scopeName] = {};
    }
  }

  function removeAIScope(scopeName: string) {
    if (!editedRules || !editedRules.ai_scopes) return;
    delete editedRules.ai_scopes[scopeName];
    editedRules = editedRules;  // Trigger Svelte reactivity
  }

  function addRuleToAIScope(scopeName: string) {
    if (!editedRules || !editedRules.ai_scopes) return;
    const resourcePath = prompt('Enter resource path (e.g., @work:*, @personal:*, personal.json:*, device_context.json:*):', '@work:*');
    if (resourcePath) {
      // Check for duplicates
      if (editedRules.ai_scopes[scopeName][resourcePath]) {
        alert('This rule already exists for this AI scope');
        return;
      }
      editedRules.ai_scopes[scopeName][resourcePath] = 'allow';
    }
  }

  function removeRuleFromAIScope(scopeName: string, path: string) {
    if (!editedRules || !editedRules.ai_scopes) return;
    delete editedRules.ai_scopes[scopeName][path];
    editedRules = editedRules;  // Trigger Svelte reactivity
  }

  // Device Sharing Management Functions
  function addDeviceSharingPreset() {
    if (!editedRules) return;
    const presetName = prompt('Enter device sharing preset name (e.g., "basic", "compute"):');
    if (presetName) {
      if (!editedRules.device_sharing) editedRules.device_sharing = {};
      // Check for duplicates
      if (editedRules.device_sharing[presetName]) {
        alert('This device sharing preset already exists');
        return;
      }
      editedRules.device_sharing[presetName] = {};
    }
  }

  function removeDeviceSharingPreset(presetName: string) {
    if (!editedRules || !editedRules.device_sharing) return;
    delete editedRules.device_sharing[presetName];
    editedRules = editedRules;  // Trigger Svelte reactivity
  }

  function addRuleToDeviceSharingPreset(presetName: string) {
    if (!editedRules || !editedRules.device_sharing) return;
    const resourcePath = prompt('Enter resource path (e.g., device_context.json:hardware.gpu.*):','device_context.json:software.os.*');
    if (resourcePath) {
      // Check for duplicates
      if (editedRules.device_sharing[presetName][resourcePath]) {
        alert('This rule already exists for this preset');
        return;
      }
      editedRules.device_sharing[presetName][resourcePath] = 'allow';
    }
  }

  function removeRuleFromDeviceSharingPreset(presetName: string, path: string) {
    if (!editedRules || !editedRules.device_sharing) return;
    delete editedRules.device_sharing[presetName][path];
    editedRules = editedRules;  // Trigger Svelte reactivity
  }

  // File Transfer Management Functions
  function addFileTransferGroup() {
    if (!editedRules) return;
    const groupName = prompt('Enter group name (e.g., friends, colleagues):');
    if (groupName) {
      if (!editedRules.file_transfer) editedRules.file_transfer = { groups: {}, nodes: {} };
      if (!editedRules.file_transfer.groups) editedRules.file_transfer.groups = {};
      // Check for duplicates
      if (editedRules.file_transfer.groups[groupName]) {
        alert('This group already has file transfer settings');
        return;
      }
      editedRules.file_transfer.groups[groupName] = {
        'file_transfer.allow': 'deny',
        'file_transfer.max_size_mb': 100,
        'file_transfer.allowed_mime_types': ['*']
      };
    }
  }

  function removeFileTransferGroup(groupName: string) {
    if (!editedRules?.file_transfer?.groups) return;
    delete editedRules.file_transfer.groups[groupName];
    editedRules = editedRules;  // Trigger Svelte reactivity
  }

  function addFileTransferNode() {
    if (!editedRules) return;
    const nodeId = prompt('Enter node ID (e.g., dpc-node-alice-123):');
    const trimmedNodeId = nodeId?.trim();
    if (trimmedNodeId && trimmedNodeId.startsWith('dpc-node-')) {
      if (!editedRules.file_transfer) editedRules.file_transfer = { groups: {}, nodes: {} };
      if (!editedRules.file_transfer.nodes) editedRules.file_transfer.nodes = {};
      // Check for duplicates
      if (editedRules.file_transfer.nodes[trimmedNodeId]) {
        alert('This node already has file transfer settings');
        return;
      }
      editedRules.file_transfer.nodes[trimmedNodeId] = {
        'file_transfer.allow': 'deny',
        'file_transfer.max_size_mb': 100,
        'file_transfer.allowed_mime_types': ['*']
      };
    } else if (nodeId) {
      alert('Node ID must start with "dpc-node-"');
    }
  }

  function removeFileTransferNode(nodeId: string) {
    if (!editedRules?.file_transfer?.nodes) return;
    delete editedRules.file_transfer.nodes[nodeId];
    editedRules = editedRules;  // Trigger Svelte reactivity
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
          class:active={selectedTab === 'file-groups'}
          on:click={() => selectedTab = 'file-groups'}
        >
          File Groups
        </button>
        <button
          class="tab"
          class:active={selectedTab === 'ai-scopes'}
          on:click={() => selectedTab = 'ai-scopes'}
        >
          AI Scopes
        </button>
        <button
          class="tab"
          class:active={selectedTab === 'device-sharing'}
          on:click={() => selectedTab = 'device-sharing'}
        >
          Device Sharing
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
          class:active={selectedTab === 'file-transfer'}
          on:click={() => selectedTab = 'file-transfer'}
        >
          File Transfer
        </button>
        <button
          class="tab"
          class:active={selectedTab === 'image-transfer'}
          on:click={() => selectedTab = 'image-transfer'}
        >
          Image Transfer
        </button>
        <button
          class="tab"
          class:active={selectedTab === 'notifications'}
          on:click={() => selectedTab = 'notifications'}
        >
          Notifications
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
                        <select id="hub-rule-{path}" name="hub-rule-{path}" bind:value={editedRules.hub[path]}>
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

        {:else if selectedTab === 'file-groups'}
          <div class="section">
            <h3>File Groups</h3>
            <p class="help-text">Define aliases for groups of context files for easier permission management.</p>

            {#if editMode && editedRules}
              <button class="btn btn-add" on:click={addFileGroup}>+ Add File Group</button>
            {/if}

            <div class="groups-list">
              {#if displayRules?.file_groups}
                {#each Object.entries(displayRules.file_groups) as [groupName, patternList]}
                  {#if !groupName.startsWith('_')}
                    <div class="group-card">
                      <div class="group-header">
                        <h4>{groupName}</h4>
                        {#if editMode}
                          <button class="btn-icon" on:click={() => removeFileGroup(groupName)}>
                            🗑️
                          </button>
                        {/if}
                      </div>

                      <div class="node-list">
                        {#each patternList as pattern}
                          <div class="node-item">
                            <code>{pattern}</code>
                            {#if editMode}
                              <button class="btn-icon-small" on:click={() => removeFilePatternFromGroup(groupName, pattern)}>
                                ×
                              </button>
                            {/if}
                          </div>
                        {:else}
                          <p class="empty-small">No file patterns in this group</p>
                        {/each}

                        {#if editMode}
                          <button class="btn-small" on:click={() => addFilePatternToGroup(groupName)}>
                            + Add Pattern
                          </button>
                        {/if}
                      </div>
                    </div>
                  {/if}
                {:else}
                  <p class="empty">No file groups defined yet.</p>
                {/each}
              {:else}
                <p class="empty">No file groups defined yet.</p>
              {/if}
            </div>

            {#if !editMode}
              <div class="info-box">
                <strong>Info:</strong> File groups allow you to reference multiple context files with a single alias (e.g., @work, @personal). Use wildcards like "work_*.json" to match multiple files.
              </div>
            {/if}
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
                      <input id="compute-enabled" name="compute-enabled" type="checkbox" bind:checked={editedRules.compute.enabled} />
                    {:else}
                      <input id="compute-enabled-display" name="compute-enabled-display" type="checkbox" checked={displayRules.compute.enabled} disabled />
                    {/if}
                    <strong>Enable Compute Sharing</strong>
                  </label>
                </div>

                {#if displayRules.compute.enabled}
                  <div class="subsection">
                    <h4>Allowed Nodes</h4>
                    {#if editMode && editedRules}
                      <textarea
                        id="compute-allow-nodes"
                        name="compute-allow-nodes"
                        class="edit-textarea"
                        rows="3"
                        placeholder="Enter node IDs (one per line)"
                        bind:value={allowNodesText}
                        on:blur={() => {
                          if (editedRules?.compute) {
                            // Remove duplicates using Set
                            const nodes = allowNodesText.split('\n').map(s => s.trim()).filter(s => s.length > 0);
                            editedRules.compute.allow_nodes = [...new Set(nodes)];
                            // Update textarea to show deduplicated list
                            allowNodesText = editedRules.compute.allow_nodes.join('\n');
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
                        id="compute-allow-groups"
                        name="compute-allow-groups"
                        class="edit-textarea"
                        rows="2"
                        placeholder="Enter group names (one per line)"
                        bind:value={allowGroupsText}
                        on:blur={() => {
                          if (editedRules?.compute) {
                            // Remove duplicates using Set
                            const groups = allowGroupsText.split('\n').map(s => s.trim()).filter(s => s.length > 0);
                            editedRules.compute.allow_groups = [...new Set(groups)];
                            // Update textarea to show deduplicated list
                            allowGroupsText = editedRules.compute.allow_groups.join('\n');
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
                        id="compute-allowed-models"
                        name="compute-allowed-models"
                        class="edit-textarea"
                        rows="3"
                        placeholder="Enter model names (one per line)"
                        bind:value={allowedModelsText}
                        on:blur={() => {
                          if (editedRules?.compute) {
                            // Remove duplicates using Set
                            const models = allowedModelsText.split('\n').map(s => s.trim()).filter(s => s.length > 0);
                            editedRules.compute.allowed_models = [...new Set(models)];
                            // Update textarea to show deduplicated list
                            allowedModelsText = editedRules.compute.allowed_models.join('\n');
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

          <!-- Transcription Sharing Section -->
          <div class="section">
            <h3>Transcription Sharing (Remote Whisper)</h3>
            <p class="help-text">Allow peers to use your local Whisper model for voice transcription.</p>

            {#if displayRules?.transcription}
              <div class="compute-settings">
                <div class="setting-item">
                  <label>
                    {#if editMode && editedRules && editedRules.transcription}
                      <input id="transcription-enabled" name="transcription-enabled" type="checkbox" bind:checked={editedRules.transcription.enabled} />
                    {:else}
                      <input id="transcription-enabled-display" name="transcription-enabled-display" type="checkbox" checked={displayRules.transcription.enabled} disabled />
                    {/if}
                    <strong>Enable Transcription Sharing</strong>
                  </label>
                </div>

                {#if displayRules.transcription.enabled}
                  <div class="subsection">
                    <h4>Allowed Nodes</h4>
                    {#if editMode && editedRules}
                      <textarea
                        id="transcription-allow-nodes"
                        name="transcription-allow-nodes"
                        class="edit-textarea"
                        rows="3"
                        placeholder="Enter node IDs (one per line)"
                        bind:value={transcriptionAllowNodesText}
                        on:blur={() => {
                          if (editedRules?.transcription) {
                            const nodes = transcriptionAllowNodesText.split('\n').map(s => s.trim()).filter(s => s.length > 0);
                            editedRules.transcription.allow_nodes = [...new Set(nodes)];
                            transcriptionAllowNodesText = editedRules.transcription.allow_nodes.join('\n');
                          }
                        }}
                      ></textarea>
                    {:else}
                      <div class="tags">
                        {#each displayRules.transcription.allow_nodes as nodeId}
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
                        id="transcription-allow-groups"
                        name="transcription-allow-groups"
                        class="edit-textarea"
                        rows="2"
                        placeholder="Enter group names (one per line)"
                        bind:value={transcriptionAllowGroupsText}
                        on:blur={() => {
                          if (editedRules?.transcription) {
                            const groups = transcriptionAllowGroupsText.split('\n').map(s => s.trim()).filter(s => s.length > 0);
                            editedRules.transcription.allow_groups = [...new Set(groups)];
                            transcriptionAllowGroupsText = editedRules.transcription.allow_groups.join('\n');
                          }
                        }}
                      ></textarea>
                    {:else}
                      <div class="tags">
                        {#each displayRules.transcription.allow_groups as groupName}
                          <span class="tag">{groupName}</span>
                        {:else}
                          <span class="empty-small">No groups allowed</span>
                        {/each}
                      </div>
                    {/if}
                  </div>

                  <div class="subsection">
                    <h4>Allowed Models</h4>
                    <p class="help-text-small">Leave empty to allow all Whisper models.</p>
                    {#if editMode && editedRules}
                      <textarea
                        id="transcription-allowed-models"
                        name="transcription-allowed-models"
                        class="edit-textarea"
                        rows="3"
                        placeholder="Enter model names (one per line, e.g., openai/whisper-large-v3)"
                        bind:value={transcriptionAllowedModelsText}
                        on:blur={() => {
                          if (editedRules?.transcription) {
                            const models = transcriptionAllowedModelsText.split('\n').map(s => s.trim()).filter(s => s.length > 0);
                            editedRules.transcription.allowed_models = [...new Set(models)];
                            transcriptionAllowedModelsText = editedRules.transcription.allowed_models.join('\n');
                          }
                        }}
                      ></textarea>
                    {:else}
                      <div class="tags">
                        {#each displayRules.transcription.allowed_models as model}
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
              <p class="empty">Transcription sharing not configured.</p>
            {/if}
          </div>

        {:else if selectedTab === 'ai-scopes'}
          <div class="section">
            <h3>AI Scopes</h3>
            <p class="help-text">Control what your local AI can access in different modes (e.g., work mode vs personal mode).</p>

            {#if editMode && editedRules}
              <button class="btn btn-add" on:click={addAIScope}>+ Add AI Scope</button>
            {/if}

            {#if displayRules?.ai_scopes && Object.keys(displayRules.ai_scopes).length > 0}
              {#each Object.entries(displayRules.ai_scopes) as [scopeName, rules]}
                {#if !scopeName.startsWith('_')}
                  <div class="peer-card">
                    <div class="group-header">
                      <h5>Scope: {scopeName}</h5>
                      {#if editMode}
                        <button class="btn-icon" on:click={() => removeAIScope(scopeName)} title="Delete scope">
                          🗑️
                        </button>
                      {/if}
                    </div>

                    <div class="rule-list">
                      {#each Object.entries(rules) as [path, action]}
                        {#if !path.startsWith('_')}
                          <div class="rule-row">
                            <code class="rule-path">{path}</code>
                            {#if editMode && editedRules && editedRules.ai_scopes && editedRules.ai_scopes[scopeName]}
                              <div style="display: flex; gap: 0.5rem; align-items: center;">
                                <select id="ai-scope-rule-{scopeName}-{path}" name="ai-scope-rule-{scopeName}-{path}" bind:value={editedRules.ai_scopes[scopeName][path]}>
                                  <option value="allow">allow</option>
                                  <option value="deny">deny</option>
                                </select>
                                <button class="btn-icon-small" on:click={() => removeRuleFromAIScope(scopeName, path)} title="Delete rule">
                                  ×
                                </button>
                              </div>
                            {:else}
                              <span class="action-badge" class:allow={action === 'allow'} class:deny={action === 'deny'}>
                                {action}
                              </span>
                            {/if}
                          </div>
                        {/if}
                      {:else}
                        <p class="empty-small">No rules defined for this scope</p>
                      {/each}
                    </div>

                    {#if editMode}
                      <button class="btn-small" on:click={() => addRuleToAIScope(scopeName)} style="margin-top: 0.5rem;">
                        + Add Rule
                      </button>
                    {/if}
                  </div>
                {/if}
              {/each}
            {:else}
              <p class="empty">No AI scopes defined.</p>
            {/if}

            {#if !editMode}
              <div class="info-box">
                <strong>Info:</strong> AI scopes allow you to restrict what context your local AI can access in different modes. Supports file groups (@work:*, @personal:*) AND field-level filtering (personal.json:profile.name, device_context.json:hardware.gpu.*). NEW in v0.12.1: Device context filtering.
              </div>
            {/if}
          </div>

        {:else if selectedTab === 'device-sharing'}
          <div class="section">
            <h3>Device Sharing Presets</h3>
            <p class="help-text">Define presets for sharing device context information (hardware, software, etc.).</p>

            {#if editMode && editedRules}
              <button class="btn btn-add" on:click={addDeviceSharingPreset}>+ Add Preset</button>
            {/if}

            {#if displayRules?.device_sharing && Object.keys(displayRules.device_sharing).length > 0}
              {#each Object.entries(displayRules.device_sharing) as [presetName, rules]}
                {#if !presetName.startsWith('_')}
                  <div class="peer-card">
                    <div class="group-header">
                      <h5>Preset: {presetName}</h5>
                      {#if editMode}
                        <button class="btn-icon" on:click={() => removeDeviceSharingPreset(presetName)} title="Delete preset">
                          🗑️
                        </button>
                      {/if}
                    </div>

                    <div class="rule-list">
                      {#each Object.entries(rules) as [path, action]}
                        {#if !path.startsWith('_')}
                          <div class="rule-row">
                            <code class="rule-path">{path}</code>
                            {#if editMode && editedRules && editedRules.device_sharing && editedRules.device_sharing[presetName]}
                              <div style="display: flex; gap: 0.5rem; align-items: center;">
                                <select id="device-sharing-rule-{presetName}-{path}" name="device-sharing-rule-{presetName}-{path}" bind:value={editedRules.device_sharing[presetName][path]}>
                                  <option value="allow">allow</option>
                                  <option value="deny">deny</option>
                                </select>
                                <button class="btn-icon-small" on:click={() => removeRuleFromDeviceSharingPreset(presetName, path)} title="Delete rule">
                                  ×
                                </button>
                              </div>
                            {:else}
                              <span class="action-badge" class:allow={action === 'allow'} class:deny={action === 'deny'}>
                                {action}
                              </span>
                            {/if}
                          </div>
                        {/if}
                      {:else}
                        <p class="empty-small">No rules defined for this preset</p>
                      {/each}
                    </div>

                    {#if editMode}
                      <button class="btn-small" on:click={() => addRuleToDeviceSharingPreset(presetName)} style="margin-top: 0.5rem;">
                        + Add Rule
                      </button>
                    {/if}
                  </div>
                {/if}
              {/each}
            {:else}
              <p class="empty">No device sharing presets defined.</p>
            {/if}

            {#if !editMode}
              <div class="info-box">
                <strong>Info:</strong> Device sharing presets define what device context information can be shared. Use device_context.json:* paths (e.g., device_context.json:hardware.gpu.*, device_context.json:software.os.*).
              </div>
            {/if}
          </div>

        {:else if selectedTab === 'file-transfer'}
          <div class="section">
            <h3>File Transfer Permissions</h3>
            <p class="help-text">Control who can send/receive files with you and set size/type limits.</p>

            {#if displayRules?.file_transfer}
              <!-- Group Permissions Section -->
              <h4>Group Permissions</h4>
              {#if editMode && editedRules}
                <button class="btn btn-add" on:click={addFileTransferGroup}>+ Add Group</button>
              {/if}

              {#if displayRules.file_transfer.groups && Object.keys(displayRules.file_transfer.groups).length > 0}
                {#each Object.entries(displayRules.file_transfer.groups) as [groupName, groupSettings]}
                  {#if !groupName.startsWith('_')}
                    <div class="peer-card">
                      <div class="group-header">
                        <h5>Group: {groupName}</h5>
                        {#if editMode}
                          <button class="btn-icon" on:click={() => removeFileTransferGroup(groupName)} title="Delete group">
                            🗑️
                          </button>
                        {/if}
                      </div>
                      <div class="subsection">
                        <div class="setting-item">
                          <span><strong>File Transfer Allowed:</strong></span>
                          {#if editMode && editedRules?.file_transfer?.groups?.[groupName]}
                            <select bind:value={editedRules.file_transfer.groups[groupName]['file_transfer.allow']}>
                              <option value="allow">allow</option>
                              <option value="deny">deny</option>
                            </select>
                          {:else}
                            <span class="value">{groupSettings['file_transfer.allow'] || 'deny'}</span>
                          {/if}
                        </div>
                        <div class="setting-item">
                          <span><strong>Max File Size (MB):</strong></span>
                          {#if editMode && editedRules?.file_transfer?.groups?.[groupName]}
                            <input
                              type="number"
                              min="1"
                              max="10000"
                              bind:value={editedRules.file_transfer.groups[groupName]['file_transfer.max_size_mb']}
                              placeholder="No limit"
                            />
                          {:else}
                            <span class="value">{groupSettings['file_transfer.max_size_mb'] || 'No limit'} {groupSettings['file_transfer.max_size_mb'] ? 'MB' : ''}</span>
                          {/if}
                        </div>
                        <div class="setting-item">
                          <span><strong>Allowed File Types:</strong></span>
                          {#if editMode && editedRules?.file_transfer?.groups?.[groupName]}
                            <textarea
                              class="edit-textarea"
                              rows="3"
                              placeholder="Enter MIME types (one per line, e.g., image/*, application/pdf) or * for all"
                              value={Array.isArray(editedRules.file_transfer.groups[groupName]['file_transfer.allowed_mime_types']) ? editedRules.file_transfer.groups[groupName]['file_transfer.allowed_mime_types'].join('\n') : '*'}
                              on:input={(e) => {
                                if (editedRules?.file_transfer?.groups?.[groupName]) {
                                  const target = e.currentTarget as HTMLTextAreaElement;
                                  const types = target.value.split('\n').map((s: string) => s.trim()).filter((s: string) => s.length > 0);
                                  editedRules.file_transfer.groups[groupName]['file_transfer.allowed_mime_types'] = types.length > 0 ? types : ['*'];
                                }
                              }}
                            ></textarea>
                          {:else}
                            <div class="tags">
                              {#if groupSettings['file_transfer.allowed_mime_types'] && Array.isArray(groupSettings['file_transfer.allowed_mime_types'])}
                                {#each groupSettings['file_transfer.allowed_mime_types'] as mimeType}
                                  <span class="tag">{mimeType}</span>
                                {/each}
                              {:else}
                                <span class="tag">*</span>
                              {/if}
                            </div>
                          {/if}
                        </div>
                      </div>
                    </div>
                  {/if}
                {/each}
              {/if}

              <!-- Node Permissions Section -->
              <h4>Individual Node Permissions</h4>
              {#if editMode && editedRules}
                <button class="btn btn-add" on:click={addFileTransferNode}>+ Add Node</button>
              {/if}

              {#if displayRules.file_transfer.nodes && Object.keys(displayRules.file_transfer.nodes).length > 0}
                {#each Object.entries(displayRules.file_transfer.nodes) as [nodeId, nodeSettings]}
                  {#if !nodeId.startsWith('_')}
                    <div class="peer-card">
                      <div class="group-header">
                        <h5>{nodeId}</h5>
                        {#if editMode}
                          <button class="btn-icon" on:click={() => removeFileTransferNode(nodeId)} title="Delete node">
                            🗑️
                          </button>
                        {/if}
                      </div>
                      <div class="subsection">
                        <div class="setting-item">
                          <span><strong>File Transfer Allowed:</strong></span>
                          {#if editMode && editedRules?.file_transfer?.nodes?.[nodeId]}
                            <select bind:value={editedRules.file_transfer.nodes[nodeId]['file_transfer.allow']}>
                              <option value="allow">allow</option>
                              <option value="deny">deny</option>
                            </select>
                          {:else}
                            <span class="value">{nodeSettings['file_transfer.allow'] || 'deny'}</span>
                          {/if}
                        </div>
                        <div class="setting-item">
                          <span><strong>Max File Size (MB):</strong></span>
                          {#if editMode && editedRules?.file_transfer?.nodes?.[nodeId]}
                            <input
                              type="number"
                              min="1"
                              max="10000"
                              bind:value={editedRules.file_transfer.nodes[nodeId]['file_transfer.max_size_mb']}
                              placeholder="No limit"
                            />
                          {:else}
                            <span class="value">{nodeSettings['file_transfer.max_size_mb'] || 'No limit'} {nodeSettings['file_transfer.max_size_mb'] ? 'MB' : ''}</span>
                          {/if}
                        </div>
                        <div class="setting-item">
                          <span><strong>Allowed File Types:</strong></span>
                          {#if editMode && editedRules?.file_transfer?.nodes?.[nodeId]}
                            <textarea
                              class="edit-textarea"
                              rows="3"
                              placeholder="Enter MIME types (one per line, e.g., image/*, application/pdf) or * for all"
                              value={Array.isArray(editedRules.file_transfer.nodes[nodeId]['file_transfer.allowed_mime_types']) ? editedRules.file_transfer.nodes[nodeId]['file_transfer.allowed_mime_types'].join('\n') : '*'}
                              on:input={(e) => {
                                if (editedRules?.file_transfer?.nodes?.[nodeId]) {
                                  const target = e.currentTarget as HTMLTextAreaElement;
                                  const types = target.value.split('\n').map((s: string) => s.trim()).filter((s: string) => s.length > 0);
                                  editedRules.file_transfer.nodes[nodeId]['file_transfer.allowed_mime_types'] = types.length > 0 ? types : ['*'];
                                }
                              }}
                            ></textarea>
                          {:else}
                            <div class="tags">
                              {#if nodeSettings['file_transfer.allowed_mime_types'] && Array.isArray(nodeSettings['file_transfer.allowed_mime_types'])}
                                {#each nodeSettings['file_transfer.allowed_mime_types'] as mimeType}
                                  <span class="tag">{mimeType}</span>
                                {/each}
                              {:else}
                                <span class="tag">*</span>
                              {/if}
                            </div>
                          {/if}
                        </div>
                      </div>
                    </div>
                  {/if}
                {/each}
              {/if}
            {:else}
              <p class="empty">No file transfer permissions configured.</p>
            {/if}

            {#if !editMode}
              <div class="info-box">
                <strong>Info:</strong> File transfer permissions control who can send you files and what types/sizes are allowed. Supports per-node and per-group settings with MIME type wildcards (e.g., "image/*", "application/pdf"). Use Edit mode to modify permissions.
              </div>
            {/if}
          </div>

        {:else if selectedTab === 'image-transfer'}
          <div class="section">
            <h3>Image Transfer Settings</h3>
            <p class="help-text">Configure screenshot/image transfer behavior for P2P chats (Ctrl+V clipboard paste).</p>

            {#if displayRules?.image_transfer}
              <div class="compute-settings">
                <div class="setting-item">
                  <span><strong>Auto-Accept Threshold (MB):</strong></span>
                  {#if editMode && editedRules?.image_transfer}
                    <input
                      type="number"
                      min="0"
                      max="1000"
                      bind:value={editedRules.image_transfer.auto_accept_threshold_mb}
                      placeholder="25"
                    />
                  {:else}
                    <span class="value">{displayRules.image_transfer.auto_accept_threshold_mb} MB</span>
                  {/if}
                </div>
                <p class="help-text-small">Images smaller than this will be auto-accepted and displayed inline. Larger images will show an acceptance dialog.</p>

                <div class="setting-item">
                  <span><strong>Max Image Size (MB):</strong></span>
                  {#if editMode && editedRules?.image_transfer}
                    <input
                      type="number"
                      min="1"
                      max="10000"
                      bind:value={editedRules.image_transfer.max_size_mb}
                      placeholder="100"
                    />
                  {:else}
                    <span class="value">{displayRules.image_transfer.max_size_mb} MB</span>
                  {/if}
                </div>
                <p class="help-text-small">Maximum allowed image size. Images larger than this will be rejected.</p>

                <div class="subsection">
                  <h4>Allowed Sources</h4>
                  <p class="help-text-small">Control which image sources are permitted for transfer.</p>
                  <div class="notification-events">
                    {#each ['clipboard', 'file', 'camera'] as source}
                      <div class="notification-event-item">
                        {#if editMode && editedRules?.image_transfer}
                          <label for="source-{source}">
                            <input
                              type="checkbox"
                              id="source-{source}"
                              checked={editedRules.image_transfer.allowed_sources.includes(source)}
                              on:change={(e) => {
                                if (editedRules?.image_transfer) {
                                  const target = e.currentTarget as HTMLInputElement;
                                  if (target.checked) {
                                    if (!editedRules.image_transfer.allowed_sources.includes(source)) {
                                      editedRules.image_transfer.allowed_sources = [
                                        ...editedRules.image_transfer.allowed_sources,
                                        source
                                      ];
                                    }
                                  } else {
                                    editedRules.image_transfer.allowed_sources =
                                      editedRules.image_transfer.allowed_sources.filter(s => s !== source);
                                  }
                                }
                              }}
                            />
                            <span class="event-name">{source}</span>
                          </label>
                        {:else}
                          <label for="source-{source}">
                            <input
                              type="checkbox"
                              id="source-{source}"
                              checked={displayRules.image_transfer.allowed_sources.includes(source)}
                              disabled
                            />
                            <span class="event-name">{source}</span>
                          </label>
                        {/if}
                      </div>
                    {/each}
                  </div>
                </div>

                <div class="setting-item" style="margin-top: 1rem;">
                  <label>
                    {#if editMode && editedRules?.image_transfer}
                      <input
                        type="checkbox"
                        id="save-screenshots"
                        bind:checked={editedRules.image_transfer.save_screenshots_to_disk}
                      />
                    {:else}
                      <input
                        type="checkbox"
                        id="save-screenshots-display"
                        checked={displayRules.image_transfer.save_screenshots_to_disk}
                        disabled
                      />
                    {/if}
                    <strong>Save Screenshots to Disk</strong>
                  </label>
                </div>
                <p class="help-text-small">When disabled (default), screenshots are sent/received but not permanently saved. Only thumbnails are kept for display.</p>
              </div>
            {:else}
              <p class="empty">Image transfer settings not configured.</p>
            {/if}

            {#if !editMode}
              <div class="info-box" style="margin-top: 1.5rem;">
                <strong>Info:</strong> Image transfer settings control screenshot/image sharing behavior in P2P chats. Auto-accept threshold determines which images are displayed inline vs. requiring user approval. When "Save Screenshots to Disk" is disabled, images are transmitted but not stored permanently (privacy-conscious default).
              </div>
            {/if}
          </div>

        {:else if selectedTab === 'notifications'}
          <div class="section">
            <h3>Notification Settings</h3>
            <p class="help-text">Control desktop notifications for different events.</p>

            <div class="permission-request" style="margin: 1rem 0;">
              <button
                class="btn btn-edit"
                on:click={async () => {
                  const { requestNotificationPermission } = await import('$lib/notificationService');
                  const granted = await requestNotificationPermission();
                  if (granted) {
                    alert('Notification permission granted!');
                  } else {
                    alert('Notification permission denied. You can enable it in your OS settings.');
                  }
                }}
              >
                Request OS Notification Permission
              </button>
              <p class="help-text-small" style="margin-top: 0.5rem;">
                Click to request permission from your operating system to show desktop notifications.
              </p>
            </div>

            {#if editMode && editedRules?.notifications}
              <div class="form-group">
                <label for="notifications-enabled">
                  <input
                    type="checkbox"
                    id="notifications-enabled"
                    bind:checked={editedRules.notifications.enabled}
                  />
                  Enable Desktop Notifications
                </label>
                <p class="help-text-small">Master toggle for all desktop notifications</p>
              </div>

              <h4 style="margin-top: 1.5rem;">Event Notifications</h4>
              <div class="notification-events">
                {#each Object.entries(editedRules.notifications.events) as [event, enabled]}
                  <div class="notification-event-item">
                    <label for="notif-{event}">
                      <input
                        type="checkbox"
                        id="notif-{event}"
                        bind:checked={editedRules.notifications.events[event]}
                        disabled={!editedRules.notifications.enabled}
                      />
                      <span class="event-name">{event.replace(/_/g, ' ')}</span>
                    </label>
                  </div>
                {/each}
              </div>
            {:else if displayRules?.notifications}
              <div class="form-group">
                <label for="notifications-enabled">
                  <input
                    type="checkbox"
                    id="notifications-enabled"
                    checked={displayRules.notifications.enabled}
                    disabled
                  />
                  Enable Desktop Notifications
                </label>
                <p class="help-text-small">Master toggle for all desktop notifications</p>
              </div>

              {#if displayRules.notifications.events}
                <h4 style="margin-top: 1.5rem;">Event Notifications</h4>
                <div class="notification-events">
                  {#each Object.entries(displayRules.notifications.events) as [event, enabled]}
                    <div class="notification-event-item">
                      <label for="notif-{event}">
                        <input
                          type="checkbox"
                          id="notif-{event}"
                          checked={enabled}
                          disabled
                        />
                        <span class="event-name">{event.replace(/_/g, ' ')}</span>
                      </label>
                    </div>
                  {/each}
                </div>
              {/if}
            {/if}

            {#if !editMode}
              <div class="info-box" style="margin-top: 1.5rem;">
                <strong>Info:</strong> Notification settings control when desktop notifications appear when the app is in the background. Master toggle must be enabled for individual event notifications to work. Operating system permission must also be granted.
              </div>
            {/if}
          </div>

        {:else if selectedTab === 'peers'}
          <div class="section">
            <h3>Peer Permissions</h3>
            <p class="help-text">Individual and group-based access rules for nodes.</p>

            <!-- Individual Nodes Section -->
            <h4>Individual Nodes</h4>
            {#if editMode && editedRules}
              <button class="btn btn-add" on:click={addNodePermission}>+ Add Node</button>
            {/if}

            {#if displayRules?.nodes && Object.keys(displayRules.nodes).length > 0}
              {#each Object.entries(displayRules.nodes) as [nodeId, rules]}
                {#if !nodeId.startsWith('_')}
                  <div class="peer-card">
                    <div class="group-header">
                      <h5>{nodeId}</h5>
                      {#if editMode}
                        <button class="btn-icon" on:click={() => removeNodePermission(nodeId)} title="Delete node">
                          🗑️
                        </button>
                      {/if}
                    </div>

                    <div class="rule-list">
                      {#each Object.entries(rules) as [path, action]}
                        {#if !path.startsWith('_')}
                          <div class="rule-row">
                            <code class="rule-path">{path}</code>
                            {#if editMode && editedRules && editedRules.nodes && editedRules.nodes[nodeId]}
                              <div style="display: flex; gap: 0.5rem; align-items: center;">
                                <select id="node-rule-{nodeId}-{path}" name="node-rule-{nodeId}-{path}" bind:value={editedRules.nodes[nodeId][path]}>
                                  <option value="allow">allow</option>
                                  <option value="deny">deny</option>
                                </select>
                                <button class="btn-icon-small" on:click={() => removeRuleFromNode(nodeId, path)} title="Delete rule">
                                  ×
                                </button>
                              </div>
                            {:else}
                              <span class="action-badge" class:allow={action === 'allow'} class:deny={action === 'deny'}>
                                {action}
                              </span>
                            {/if}
                          </div>
                        {/if}
                      {:else}
                        <p class="empty-small">No rules defined for this node</p>
                      {/each}
                    </div>

                    {#if editMode}
                      <button class="btn-small" on:click={() => addRuleToNode(nodeId)} style="margin-top: 0.5rem;">
                        + Add Rule
                      </button>
                    {/if}
                  </div>
                {/if}
              {/each}
            {:else}
              <p class="empty">No individual node permissions defined.</p>
            {/if}

            <!-- Group Permissions Section -->
            <h4 style="margin-top: 1.5rem;">Group Permissions</h4>
            {#if editMode && editedRules}
              <button class="btn btn-add" on:click={addGroupPermission}>+ Add Group</button>
            {/if}

            {#if displayRules?.groups && Object.keys(displayRules.groups).length > 0}
              {#each Object.entries(displayRules.groups) as [groupName, rules]}
                {#if !groupName.startsWith('_')}
                  <div class="peer-card">
                    <div class="group-header">
                      <h5>Group: {groupName}</h5>
                      {#if editMode}
                        <button class="btn-icon" on:click={() => removeGroupPermission(groupName)} title="Delete group">
                          🗑️
                        </button>
                      {/if}
                    </div>

                    <div class="rule-list">
                      {#each Object.entries(rules) as [path, action]}
                        {#if !path.startsWith('_')}
                          <div class="rule-row">
                            <code class="rule-path">{path}</code>
                            {#if editMode && editedRules && editedRules.groups && editedRules.groups[groupName]}
                              <div style="display: flex; gap: 0.5rem; align-items: center;">
                                <select id="group-rule-{groupName}-{path}" name="group-rule-{groupName}-{path}" bind:value={editedRules.groups[groupName][path]}>
                                  <option value="allow">allow</option>
                                  <option value="deny">deny</option>
                                </select>
                                <button class="btn-icon-small" on:click={() => removeRuleFromGroup(groupName, path)} title="Delete rule">
                                  ×
                                </button>
                              </div>
                            {:else}
                              <span class="action-badge" class:allow={action === 'allow'} class:deny={action === 'deny'}>
                                {action}
                              </span>
                            {/if}
                          </div>
                        {/if}
                      {:else}
                        <p class="empty-small">No rules defined for this group</p>
                      {/each}
                    </div>

                    {#if editMode}
                      <button class="btn-small" on:click={() => addRuleToGroup(groupName)} style="margin-top: 0.5rem;">
                        + Add Rule
                      </button>
                    {/if}
                  </div>
                {/if}
              {/each}
            {:else}
              <p class="empty">No group permissions defined.</p>
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
    width: 95%;
    max-width: 1100px;
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

  /* Notification Settings Styles */
  .form-group {
    margin-bottom: 1rem;
  }

  .form-group label {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-weight: 500;
    cursor: pointer;
  }

  .help-text-small {
    color: #666;
    font-size: 0.85rem;
    margin: 0.25rem 0 0 1.75rem;
  }

  .notification-events {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
    gap: 0.75rem;
    margin-top: 0.5rem;
  }

  .notification-event-item {
    padding: 0.5rem;
    background: #f5f5f5;
    border-radius: 4px;
  }

  .notification-event-item label {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    cursor: pointer;
  }

  .notification-event-item input[type="checkbox"]:disabled {
    cursor: not-allowed;
    opacity: 0.5;
  }

  .event-name {
    text-transform: capitalize;
    font-size: 0.9rem;
  }
</style>
