<!-- MentionAutocomplete.svelte - Dropdown for @-mention autocomplete in group chats -->

<script lang="ts">
  import { createEventDispatcher } from 'svelte';

  interface Member {
    node_id: string;
    name: string;
  }

  interface Props {
    visible: boolean;
    query: string;
    members: Member[];
    position: { top: number; left: number };
    selectedIndex: number;
  }

  let { visible = false, query = '', members = [], position = { top: 0, left: 0 }, selectedIndex = 0 }: Props = $props();

  const dispatch = createEventDispatcher();

  // Filter members by query
  let filteredMembers = $derived.by(() => {
    if (!query) return members;
    const lowerQuery = query.toLowerCase();
    return members.filter(
      (m) =>
        m.name?.toLowerCase().includes(lowerQuery) || m.node_id.toLowerCase().includes(lowerQuery)
    );
  });

  function handleSelect(member: Member) {
    dispatch('select', member);
  }

  function handleKeydown(e: KeyboardEvent) {
    if (!visible || filteredMembers.length === 0) return;

    if (e.key === 'ArrowDown') {
      e.preventDefault();
      dispatch('navigate', { direction: 'down' });
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      dispatch('navigate', { direction: 'up' });
    } else if (e.key === 'Enter' || e.key === 'Tab') {
      e.preventDefault();
      if (filteredMembers[selectedIndex]) {
        handleSelect(filteredMembers[selectedIndex]);
      }
    } else if (e.key === 'Escape') {
      e.preventDefault();
      dispatch('close');
    }
  }
</script>

<svelte:window on:keydown={handleKeydown} />

{#if visible && filteredMembers.length > 0}
  <div
    class="mention-dropdown"
    style="top: {position.top}px; left: {position.left}px;"
    role="listbox"
    aria-label="Mention suggestions"
  >
    {#each filteredMembers as member, index}
      <button
        type="button"
        class="mention-option"
        class:selected={index === selectedIndex}
        onclick={() => handleSelect(member)}
        role="option"
        aria-selected={index === selectedIndex}
      >
        <span class="mention-icon">@</span>
        <span class="mention-name">{member.name || member.node_id}</span>
        {#if member.name}
          <span class="mention-node-id">{member.node_id}</span>
        {/if}
      </button>
    {/each}
  </div>
{/if}

<style>
  .mention-dropdown {
    position: fixed;
    background: #1e1e2e;
    border: 1px solid #45475a;
    border-radius: 8px;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.4);
    max-height: 200px;
    overflow-y: auto;
    z-index: 1001;
    min-width: 180px;
    padding: 4px 0;
  }

  .mention-option {
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
    padding: 8px 12px;
    border: none;
    background: transparent;
    color: #cdd6f4;
    cursor: pointer;
    text-align: left;
    font-size: 0.85rem;
    transition: background 0.1s;
  }

  .mention-option:hover {
    background: #313244;
  }

  .mention-option.selected {
    background: #3b3d52;
    outline: none;
  }

  .mention-icon {
    color: #89b4fa;
    font-weight: 600;
    width: 16px;
    text-align: center;
  }

  .mention-name {
    flex: 1;
    font-weight: 500;
  }

  .mention-node-id {
    color: #6c7086;
    font-size: 0.75rem;
  }
</style>
