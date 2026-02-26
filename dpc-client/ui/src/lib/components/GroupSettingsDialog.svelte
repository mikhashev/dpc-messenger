<!-- GroupSettingsDialog.svelte - View/edit group settings and manage members -->

<script lang="ts">
  import { createEventDispatcher } from 'svelte';

  export let open: boolean = false;
  export let group: {
    group_id: string;
    name: string;
    topic?: string;
    created_by: string;
    members: string[];
  } | null = null;
  export let selfNodeId: string = '';
  export let connectedPeers: Array<{ node_id: string; name: string }> = [];
  export let peerDisplayNames: Map<string, string> = new Map();

  const dispatch = createEventDispatcher();

  let showAddMember = false;

  // Peers that are connected but not yet in the group
  $: availablePeers = connectedPeers.filter(
    p => group && !group.members.includes(p.node_id)
  );

  $: isCreator = group?.created_by === selfNodeId;

  function getMemberName(nodeId: string): string {
    if (nodeId === selfNodeId) return 'You';
    return peerDisplayNames.get(nodeId) || nodeId.slice(0, 20) + '...';
  }

  function handleAddMember(nodeId: string) {
    dispatch('addMember', { group_id: group?.group_id, node_id: nodeId });
    showAddMember = false;
  }

  function handleRemoveMember(nodeId: string) {
    dispatch('removeMember', { group_id: group?.group_id, node_id: nodeId });
  }

  function handleClose() {
    showAddMember = false;
    open = false;
  }
</script>

{#if open && group}
  <div class="modal-overlay" role="presentation" on:click|self={handleClose}>
    <div class="modal" role="dialog" aria-labelledby="settings-title" tabindex="-1">
      <div class="modal-header">
        <h2 id="settings-title">Group Settings</h2>
        <button class="close-btn" on:click={handleClose} title="Close">&times;</button>
      </div>

      <div class="modal-body">
        <!-- Group Info -->
        <div class="section">
          <div class="info-row">
            <span class="label">Name</span>
            <span class="value">{group.name}</span>
          </div>
          {#if group.topic}
            <div class="info-row">
              <span class="label">Topic</span>
              <span class="value">{group.topic}</span>
            </div>
          {/if}
        </div>

        <!-- Members -->
        <div class="section">
          <div class="section-header">
            <h3>Members ({group.members?.length || 0})</h3>
            {#if availablePeers.length > 0}
              <button
                class="btn-add"
                on:click={() => showAddMember = !showAddMember}
              >
                {showAddMember ? 'Cancel' : '+ Add'}
              </button>
            {/if}
          </div>

          <!-- Add Member Panel -->
          {#if showAddMember}
            <div class="add-member-panel">
              {#each availablePeers as peer}
                <button
                  class="peer-option"
                  on:click={() => handleAddMember(peer.node_id)}
                  title={peer.node_id}
                >
                  <span class="peer-add-icon">+</span>
                  <span>{peer.name}</span>
                </button>
              {/each}
            </div>
          {/if}

          <!-- Member List -->
          <div class="member-list">
            {#each group.members as memberId}
              <div class="member-row">
                <span class="member-name">
                  {getMemberName(memberId)}
                  {#if memberId === group.created_by}
                    <span class="creator-badge">creator</span>
                  {/if}
                </span>
                {#if isCreator && memberId !== selfNodeId}
                  <button
                    class="btn-remove"
                    on:click={() => handleRemoveMember(memberId)}
                    title="Remove from group"
                  >
                    &times;
                  </button>
                {/if}
              </div>
            {/each}
          </div>
        </div>
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
    width: 400px;
    max-width: 90vw;
    max-height: 80vh;
    overflow-y: auto;
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
  }

  .modal-header {
    padding: 16px 20px;
    border-bottom: 1px solid #45475a;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  .modal-header h2 {
    margin: 0;
    font-size: 1.1rem;
    color: #cdd6f4;
  }

  .close-btn {
    background: transparent;
    border: none;
    color: #6c7086;
    font-size: 1.4rem;
    cursor: pointer;
    padding: 0 4px;
    line-height: 1;
  }

  .close-btn:hover {
    color: #cdd6f4;
  }

  .modal-body {
    padding: 16px 20px;
  }

  .section {
    margin-bottom: 16px;
  }

  .section-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 8px;
  }

  .section-header h3 {
    margin: 0;
    font-size: 0.9rem;
    color: #a6adc8;
    font-weight: 600;
  }

  .info-row {
    display: flex;
    gap: 8px;
    align-items: baseline;
    margin-bottom: 6px;
  }

  .label {
    font-size: 0.75rem;
    color: #6c7086;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    min-width: 50px;
    flex-shrink: 0;
  }

  .value {
    font-size: 0.9rem;
    color: #cdd6f4;
  }

  .btn-add {
    padding: 4px 10px;
    border: 1px solid #45475a;
    border-radius: 4px;
    background: transparent;
    color: #89b4fa;
    cursor: pointer;
    font-size: 0.75rem;
    font-weight: 600;
  }

  .btn-add:hover {
    background: #313244;
    border-color: #89b4fa;
  }

  .add-member-panel {
    display: flex;
    flex-direction: column;
    gap: 4px;
    margin-bottom: 8px;
    padding: 8px;
    background: #313244;
    border-radius: 6px;
    border: 1px solid #45475a;
  }

  .peer-option {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 10px;
    border: 1px solid #45475a;
    border-radius: 4px;
    background: #1e1e2e;
    color: #cdd6f4;
    cursor: pointer;
    font-size: 0.85rem;
    text-align: left;
  }

  .peer-option:hover {
    border-color: #a6e3a1;
    background: #2d3a2d;
  }

  .peer-add-icon {
    color: #a6e3a1;
    font-weight: bold;
  }

  .member-list {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .member-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 8px 10px;
    background: #313244;
    border-radius: 6px;
    border: 1px solid #45475a;
  }

  .member-name {
    font-size: 0.85rem;
    color: #cdd6f4;
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .creator-badge {
    font-size: 0.65rem;
    color: #f9e2af;
    background: rgba(249, 226, 175, 0.1);
    padding: 1px 6px;
    border-radius: 3px;
    border: 1px solid rgba(249, 226, 175, 0.3);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .btn-remove {
    background: transparent;
    border: none;
    color: #6c7086;
    font-size: 1.2rem;
    cursor: pointer;
    padding: 0 4px;
    line-height: 1;
  }

  .btn-remove:hover {
    color: #f38ba8;
  }
</style>
