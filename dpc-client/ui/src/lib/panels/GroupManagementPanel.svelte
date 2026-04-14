<!-- src/lib/panels/GroupManagementPanel.svelte -->
<!-- Group create/leave/delete/add-member/remove-member handlers (Phase 3 Step 8 continuation) -->
<!-- Logic-only panel — no markup, no styles. -->
<!-- Exports: handleCreateGroup(), handleLeaveGroup(), handleDeleteGroup(), handleGroupAddMember(), handleGroupRemoveMember() -->

<script lang="ts">
  import type { Writable } from 'svelte/store';
  import {
    groupChats,
    createGroupChat,
    leaveGroup,
    deleteGroup,
    addGroupMember,
    removeGroupMember,
  } from '$lib/coreService';

  // ---------------------------------------------------------------------------
  // Props
  // ---------------------------------------------------------------------------
  let {
    chatHistories,
    onSetActiveChatId,
    onCloseNewGroupDialog,
  }: {
    chatHistories: Writable<Map<string, any[]>>;
    onSetActiveChatId: (chatId: string) => void;
    onCloseNewGroupDialog: () => void;
  } = $props();

  // ---------------------------------------------------------------------------
  // Public API (called from +page.svelte via bind:this)
  // ---------------------------------------------------------------------------

  export async function handleCreateGroup(event: CustomEvent) {
    const { name, topic, member_node_ids } = event.detail;
    try {
      const result = await createGroupChat(name, topic, member_node_ids);
      if (result && result.status === 'success' && result.group) {
        const groupId = result.group.group_id;

        groupChats.update(map => {
          const newMap = new Map(map);
          newMap.set(groupId, result.group);
          return newMap;
        });

        chatHistories.update(h => {
          if (!h.has(groupId)) {
            const newMap = new Map(h);
            newMap.set(groupId, []);
            return newMap;
          }
          return h;
        });

        onSetActiveChatId(groupId);
      }
      onCloseNewGroupDialog();
    } catch (e) {
      console.error('Failed to create group:', e);
    }
  }

  export async function handleLeaveGroup(groupId: string, activeChatId: string) {
    try {
      await leaveGroup(groupId);
      if (activeChatId === groupId) {
        onSetActiveChatId('local_ai');
      }
      groupChats.update(map => {
        const newMap = new Map(map);
        newMap.delete(groupId);
        return newMap;
      });
      chatHistories.update(h => {
        const newMap = new Map(h);
        newMap.delete(groupId);
        return newMap;
      });
    } catch (e) {
      console.error('Failed to leave group:', e);
    }
  }

  export async function handleDeleteGroup(groupId: string, activeChatId: string, ask: any) {
    let shouldDelete = false;
    if (ask) {
      shouldDelete = await ask(
        'Delete this group chat? This will permanently remove all messages and data for all members.',
        { title: 'Confirm Group Deletion', kind: 'warning' }
      );
    } else {
      shouldDelete = confirm('Delete this group chat? This will permanently remove all messages and data for all members.');
    }

    if (!shouldDelete) return;

    try {
      await deleteGroup(groupId);
      if (activeChatId === groupId) {
        onSetActiveChatId('local_ai');
      }
      groupChats.update(map => {
        const newMap = new Map(map);
        newMap.delete(groupId);
        return newMap;
      });
      chatHistories.update(h => {
        const newMap = new Map(h);
        newMap.delete(groupId);
        return newMap;
      });
    } catch (e) {
      console.error('Failed to delete group:', e);
    }
  }

  export async function handleGroupAddMember(event: CustomEvent<{ group_id: string; node_id: string }>) {
    const { group_id, node_id } = event.detail;
    try {
      await addGroupMember(group_id, node_id);
    } catch (e) {
      console.error('Failed to add group member:', e);
    }
  }

  export async function handleGroupRemoveMember(event: CustomEvent<{ group_id: string; node_id: string }>) {
    const { group_id, node_id } = event.detail;
    try {
      await removeGroupMember(group_id, node_id);
    } catch (e) {
      console.error('Failed to remove group member:', e);
    }
  }
</script>

<!-- No markup — logic-only panel -->
