<!-- src/lib/panels/GroupPanel.svelte -->
<!-- Group invite, deletion, member-left effects + mention autocomplete (Phase 3 Steps 7 & 8) -->
<!-- Has markup: GroupInviteDialog, MentionAutocomplete -->
<!-- Manages: $groupInviteReceived, $groupDeleted, $groupMemberLeft effects -->
<!-- Exports: handleMentionInput(), getMentionVisible() for ChatPanel textarea delegation -->

<script lang="ts">
  import type { Writable } from 'svelte/store';
  import GroupInviteDialog from '$lib/components/GroupInviteDialog.svelte';
  import MentionAutocomplete from '$lib/components/MentionAutocomplete.svelte';
  import {
    groupChats,
    groupInviteReceived,
    groupDeleted,
    groupMemberLeft,
    nodeStatus,
    leaveGroup,
    agentsList,
  } from '$lib/coreService';

  // ---------------------------------------------------------------------------
  // Props
  // ---------------------------------------------------------------------------
  let {
    activeChatId,
    chatHistories,
    peerDisplayNames,
    getCurrentInput,
    onSetCurrentInput,
    onSetActiveChatId,
    onAgentToast,
  }: {
    activeChatId: string;
    chatHistories: Writable<Map<string, any[]>>;
    peerDisplayNames: Map<string, string>;
    getCurrentInput: () => string;
    onSetCurrentInput: (val: string) => void;
    onSetActiveChatId: (chatId: string) => void;
    onAgentToast: (message: string, type: 'info' | 'warning' | 'error') => void;
  } = $props();

  // ---------------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------------
  let pendingGroupInvite = $state<any>(null);
  let showGroupInviteDialog = $state(false);

  // Mention autocomplete (moved from ChatPanel, Step 8)
  let mentionAutocompleteVisible = $state(false);
  let mentionQuery = $state('');
  let mentionStartPosition = $state(0);
  let mentionDropdownPosition = $state({ bottom: 0, left: 0 });
  let mentionSelectedIndex = $state(0);

  // ---------------------------------------------------------------------------
  // Derived
  // ---------------------------------------------------------------------------
  let filteredMentionMembers = $derived.by(() => {
    const members = getMentionableMembers();
    if (!mentionQuery) return members;
    const lowerQuery = mentionQuery.toLowerCase();
    return members.filter(
      m => m.name.toLowerCase().includes(lowerQuery) || m.node_id.toLowerCase().includes(lowerQuery)
    );
  });

  // ---------------------------------------------------------------------------
  // Effects
  // ---------------------------------------------------------------------------

  // Show invite dialog when group invite arrives
  $effect(() => {
    if ($groupInviteReceived) {
      pendingGroupInvite = $groupInviteReceived;
      showGroupInviteDialog = true;
    }
  });

  // Group deletion: show toast, redirect if viewing deleted group, clean up history
  $effect(() => {
    if ($groupDeleted) {
      const deleted = $groupDeleted;
      onAgentToast(`Group "${deleted.group_name || deleted.group_id}" was deleted`, 'info');

      if (activeChatId === deleted.group_id) {
        onSetActiveChatId('local_ai');
      }
      chatHistories.update(h => {
        const newMap = new Map(h);
        newMap.delete(deleted.group_id);
        return newMap;
      });
      groupDeleted.set(null);
    }
  });

  // Group member left: show toast
  $effect(() => {
    if ($groupMemberLeft) {
      const left = $groupMemberLeft;
      const memberName = left.member_name || left.node_id?.slice(0, 16) || 'A member';
      const group = $groupChats.get(left.group_id);
      if (group) {
        onAgentToast(`${memberName} left "${group.name}"`, 'info');
      }
      groupMemberLeft.set(null);
    }
  });

  // ---------------------------------------------------------------------------
  // Invite handlers
  // ---------------------------------------------------------------------------

  function handleGroupInviteAccept(event: CustomEvent<{ group_id: string }>) {
    const groupId = event.detail.group_id;
    chatHistories.update(h => {
      if (!h.has(groupId)) {
        const newMap = new Map(h);
        newMap.set(groupId, []);
        return newMap;
      }
      return h;
    });
    const name = pendingGroupInvite?.name || 'group';
    onAgentToast(`Joined group "${name}"`, 'info');
    onSetActiveChatId(groupId);
    pendingGroupInvite = null;
    showGroupInviteDialog = false;
  }

  async function handleGroupInviteDecline(event: CustomEvent<{ group_id: string }>) {
    const groupId = event.detail.group_id;
    await leaveGroup(groupId);
    pendingGroupInvite = null;
    showGroupInviteDialog = false;
  }

  // ---------------------------------------------------------------------------
  // Mention autocomplete (moved from ChatPanel)
  // ---------------------------------------------------------------------------

  function getMentionableMembers(): Array<{ node_id: string; name: string; mention_name: string }> {
    if (!activeChatId.startsWith('group-')) return [];
    const group = $groupChats.get(activeChatId);
    if (!group) return [];
    const selfId = $nodeStatus?.node_id || '';
    const result: Array<{ node_id: string; name: string; mention_name: string }> = [];
    if (group.members) {
      for (const nodeId of group.members) {
        if (nodeId !== selfId) {
          const peerName = peerDisplayNames.get(nodeId)?.split(' | ')[0] || nodeId;
          result.push({ node_id: nodeId, name: peerName, mention_name: peerName });
        }
      }
    }
    const seenAgents = new Set<string>();
    for (const [nodeId, agentIds] of Object.entries(group.agents || {})) {
      const ownerName = nodeId === selfId ? 'You' : (peerDisplayNames.get(nodeId)?.split(' | ')[0] || nodeId.substring(0, 12));
      for (const agentId of (agentIds as string[])) {
        if (seenAgents.has(agentId)) continue;
        seenAgents.add(agentId);
        const localAgent = $agentsList.find((a: any) => a.agent_id === agentId);
        const agentName = localAgent?.name || (group.agent_names as any)?.[nodeId]?.[agentId] || agentId;
        result.push({ node_id: agentId, name: `${agentName} (${ownerName})`, mention_name: agentName });
      }
    }
    return result;
  }

  export function handleMentionInput(event: Event) {
    const textarea = event.target as HTMLTextAreaElement;
    const value = textarea.value;
    const cursorPos = textarea.selectionStart;
    if (!activeChatId.startsWith('group-')) {
      mentionAutocompleteVisible = false;
      return;
    }
    const lastAtIndex = value.lastIndexOf('@', cursorPos - 1);
    if (lastAtIndex !== -1) {
      const textAfterAt = value.slice(lastAtIndex + 1, cursorPos);
      if (!textAfterAt.includes(' ') && !textAfterAt.includes('\n')) {
        mentionQuery = textAfterAt;
        mentionStartPosition = lastAtIndex;
        mentionSelectedIndex = 0;
        const rect = textarea.getBoundingClientRect();
        // Anchor above the textarea — chat input is at the bottom of the screen,
        // so "top: rect.bottom" would push the dropdown off-screen below.
        // Use CSS bottom offset to pin the dropdown's bottom edge above the textarea top.
        mentionDropdownPosition = { bottom: window.innerHeight - rect.top + 4, left: rect.left };
        mentionAutocompleteVisible = true;
        return;
      }
    }
    mentionAutocompleteVisible = false;
  }

  function handleMentionSelect(member: { node_id: string; name: string; mention_name: string }) {
    const currentInput = getCurrentInput();
    const before = currentInput.slice(0, mentionStartPosition);
    const after = currentInput.slice(mentionStartPosition + mentionQuery.length + 1);
    onSetCurrentInput(`${before}@${member.mention_name} ${after}`);
    mentionAutocompleteVisible = false;
    mentionSelectedIndex = 0;
  }

  export function handleMentionNavigate(direction: 'up' | 'down') {
    const maxIndex = filteredMentionMembers.length - 1;
    mentionSelectedIndex =
      direction === 'down'
        ? Math.min(mentionSelectedIndex + 1, maxIndex)
        : Math.max(mentionSelectedIndex - 1, 0);
  }

  export function closeMentionAutocomplete() {
    mentionAutocompleteVisible = false;
    mentionSelectedIndex = 0;
  }

  export function getMentionVisible(): boolean {
    return mentionAutocompleteVisible;
  }
</script>

<!-- Group Invite Accept/Decline Dialog -->
<GroupInviteDialog
  bind:open={showGroupInviteDialog}
  invite={pendingGroupInvite}
  on:accept={handleGroupInviteAccept}
  on:decline={handleGroupInviteDecline}
/>

<!-- Mention Autocomplete (moved from ChatPanel) -->
<MentionAutocomplete
  visible={mentionAutocompleteVisible}
  query={mentionQuery}
  members={filteredMentionMembers}
  position={mentionDropdownPosition}
  selectedIndex={mentionSelectedIndex}
  on:select={(e) => handleMentionSelect(e.detail)}
  on:navigate={(e) => handleMentionNavigate(e.detail.direction)}
  on:close={closeMentionAutocomplete}
/>
