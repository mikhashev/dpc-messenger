<!-- NewGroupDialog.svelte - Create new group chat dialog -->
<!-- User enters group name, topic, and selects peers to add -->

<script lang="ts">
  import { createEventDispatcher } from 'svelte';

  export let open: boolean = false;
  export let connectedPeers: Array<{ node_id: string; name: string }> = [];

  const dispatch = createEventDispatcher();

  let groupName = '';
  let groupTopic = '';
  let selectedPeers: Set<string> = new Set();

  function togglePeer(nodeId: string) {
    if (selectedPeers.has(nodeId)) {
      selectedPeers.delete(nodeId);
    } else {
      selectedPeers.add(nodeId);
    }
    selectedPeers = new Set(selectedPeers); // trigger reactivity
  }

  function handleCreate() {
    if (!groupName.trim()) return;
    if (selectedPeers.size === 0) return;

    dispatch('create', {
      name: groupName.trim(),
      topic: groupTopic.trim(),
      member_node_ids: Array.from(selectedPeers)
    });

    // Reset form
    groupName = '';
    groupTopic = '';
    selectedPeers = new Set();
    open = false;
  }

  function handleCancel() {
    groupName = '';
    groupTopic = '';
    selectedPeers = new Set();
    open = false;
    dispatch('cancel');
  }
</script>

{#if open}
  <div class="modal-overlay" role="presentation" on:click|self={handleCancel}>
    <div class="modal" role="dialog" aria-labelledby="dialog-title" tabindex="-1">
      <div class="modal-header">
        <h2 id="dialog-title">New Group Chat</h2>
      </div>

      <div class="modal-body">
        <div class="field">
          <label for="group-name">Group Name</label>
          <input
            id="group-name"
            type="text"
            bind:value={groupName}
            placeholder="e.g. Project Planning"
            maxlength="100"
          />
        </div>

        <div class="field">
          <label for="group-topic">Topic (optional)</label>
          <input
            id="group-topic"
            type="text"
            bind:value={groupTopic}
            placeholder="e.g. Sprint 5 planning"
            maxlength="200"
          />
        </div>

        <div class="field">
          <span class="field-label">Add Members ({selectedPeers.size} selected)</span>
          {#if connectedPeers.length > 0}
            <div class="peer-list">
              {#each connectedPeers as peer}
                <button
                  type="button"
                  class="peer-option"
                  class:selected={selectedPeers.has(peer.node_id)}
                  on:click={() => togglePeer(peer.node_id)}
                  title={peer.node_id}
                >
                  <span class="check">{selectedPeers.has(peer.node_id) ? '✓' : ''}</span>
                  <span class="name">{peer.name}</span>
                </button>
              {/each}
            </div>
          {:else}
            <p class="no-peers">No connected peers available</p>
          {/if}
        </div>
      </div>

      <div class="modal-footer">
        <button class="btn-cancel" on:click={handleCancel}>Cancel</button>
        <button
          class="btn-create"
          on:click={handleCreate}
          disabled={!groupName.trim() || selectedPeers.size === 0}
        >
          Create Group
        </button>
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
    background: rgba(0, 0, 0, 0.6);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }

  .modal {
    background: #1e1e2e;
    border: 1px solid #45475a;
    border-radius: 12px;
    width: 420px;
    max-width: 90vw;
    max-height: 80vh;
    overflow-y: auto;
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
  }

  .modal-header {
    padding: 16px 20px;
    border-bottom: 1px solid #45475a;
  }

  .modal-header h2 {
    margin: 0;
    font-size: 1.1rem;
    color: #cdd6f4;
  }

  .modal-body {
    padding: 16px 20px;
  }

  .field {
    margin-bottom: 16px;
  }

  .field label,
  .field .field-label {
    display: block;
    margin-bottom: 6px;
    font-size: 0.85rem;
    color: #a6adc8;
    font-weight: 500;
  }

  .field input {
    width: 100%;
    padding: 8px 12px;
    border: 1px solid #45475a;
    border-radius: 6px;
    background: #313244;
    color: #cdd6f4;
    font-size: 0.9rem;
    box-sizing: border-box;
  }

  .field input:focus {
    outline: none;
    border-color: #89b4fa;
  }

  .peer-list {
    display: flex;
    flex-direction: column;
    gap: 4px;
    max-height: 200px;
    overflow-y: auto;
  }

  .peer-option {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 12px;
    border: 1px solid #45475a;
    border-radius: 6px;
    background: #313244;
    color: #cdd6f4;
    cursor: pointer;
    text-align: left;
    font-size: 0.85rem;
    transition: all 0.15s;
  }

  .peer-option:hover {
    border-color: #89b4fa;
    background: #3b3d52;
  }

  .peer-option.selected {
    border-color: #a6e3a1;
    background: #2d3a2d;
  }

  .check {
    width: 16px;
    text-align: center;
    color: #a6e3a1;
    font-weight: bold;
  }

  .no-peers {
    color: #6c7086;
    font-size: 0.85rem;
    font-style: italic;
    margin: 8px 0;
  }

  .modal-footer {
    padding: 12px 20px;
    border-top: 1px solid #45475a;
    display: flex;
    justify-content: flex-end;
    gap: 8px;
  }

  .btn-cancel {
    padding: 8px 16px;
    border: 1px solid #45475a;
    border-radius: 6px;
    background: transparent;
    color: #a6adc8;
    cursor: pointer;
    font-size: 0.85rem;
  }

  .btn-cancel:hover {
    background: #313244;
  }

  .btn-create {
    padding: 8px 16px;
    border: none;
    border-radius: 6px;
    background: #89b4fa;
    color: #1e1e2e;
    cursor: pointer;
    font-weight: 600;
    font-size: 0.85rem;
  }

  .btn-create:hover:not(:disabled) {
    background: #74c7ec;
  }

  .btn-create:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
</style>
