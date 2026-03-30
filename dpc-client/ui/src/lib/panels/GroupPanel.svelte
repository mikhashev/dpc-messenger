<!-- src/lib/panels/GroupPanel.svelte -->
<!-- Group invite, deletion, and member-left effects (Phase 3 Step 7) -->
<!-- Has markup: GroupInviteDialog -->
<!-- Manages: $groupInviteReceived, $groupDeleted, $groupMemberLeft effects -->

<script lang="ts">
  import type { Writable } from 'svelte/store';
  import GroupInviteDialog from '$lib/components/GroupInviteDialog.svelte';
  import {
    groupChats,
    groupInviteReceived,
    groupDeleted,
    groupMemberLeft,
    leaveGroup,
  } from '$lib/coreService';

  // ---------------------------------------------------------------------------
  // Props
  // ---------------------------------------------------------------------------
  let {
    activeChatId,
    chatHistories,
    onSetActiveChatId,
    onAgentToast,
  }: {
    activeChatId: string;
    chatHistories: Writable<Map<string, any[]>>;
    onSetActiveChatId: (chatId: string) => void;
    onAgentToast: (message: string, type: 'info' | 'warning' | 'error') => void;
  } = $props();

  // ---------------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------------
  let pendingGroupInvite = $state<any>(null);
  let showGroupInviteDialog = $state(false);

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
</script>

<!-- Group Invite Accept/Decline Dialog -->
<GroupInviteDialog
  bind:open={showGroupInviteDialog}
  invite={pendingGroupInvite}
  on:accept={handleGroupInviteAccept}
  on:decline={handleGroupInviteDecline}
/>
