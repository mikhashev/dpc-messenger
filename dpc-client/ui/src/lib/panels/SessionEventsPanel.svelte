<!-- src/lib/panels/SessionEventsPanel.svelte -->
<!-- Session lifecycle events: proposal, result, conversation reset -->
<!-- Logic-only panel — no markup, no styles. -->

<script lang="ts">
  import type { Writable } from 'svelte/store';
  import {
    newSessionProposal,
    newSessionResult,
    conversationReset,
  } from '$lib/coreService';
  import { showNotificationIfBackground } from '$lib/notificationService';

  // ---------------------------------------------------------------------------
  // Props
  // ---------------------------------------------------------------------------
  let {
    chatHistories,
    getPeerDisplayName,
    onOpenNewSessionDialog,
    onClearStateForConversation,
  }: {
    chatHistories: Writable<Map<string, any[]>>;
    getPeerDisplayName: (id: string) => string;
    onOpenNewSessionDialog: () => void;
    onClearStateForConversation: (conversationId: string) => void;
  } = $props();

  // ---------------------------------------------------------------------------
  // Effects
  // ---------------------------------------------------------------------------

  // Open new session dialog when proposal received (v0.11.3)
  $effect(() => {
    if ($newSessionProposal) {
      onOpenNewSessionDialog();

      (async () => {
        const initiatorName = getPeerDisplayName($newSessionProposal.initiator_node_id);
        const notified = await showNotificationIfBackground({
          title: 'New Session Requested',
          body: `${initiatorName} wants to start a new session`
        });
        console.log(`[Notifications] New session proposal notification: ${notified ? 'system' : 'skip'}`);
      })();
    }
  });

  // Clear frontend state when new session approved (v0.11.3)
  $effect(() => {
    if ($newSessionResult && $newSessionResult.result === "approved") {
      // v0.20.0 FIX: Prioritize conversation_id over sender_node_id
      // For group chats, conversation_id is the group_id (correct)
      // sender_node_id is only used as fallback for legacy peer chats
      const conversationId = $newSessionResult.conversation_id || $newSessionResult.sender_node_id;

      (async () => {
        const notified = await showNotificationIfBackground({
          title: `Session ${$newSessionResult.result}`,
          body: `New session ${$newSessionResult.result}`
        });
        console.log(`[Notifications] New session result notification: ${notified ? 'system' : 'skip'}`);
      })();

      console.log('[NewSession] Clearing chat for:', conversationId);
      console.log('[NewSession] sender_node_id:', $newSessionResult.sender_node_id);
      console.log('[NewSession] conversation_id:', $newSessionResult.conversation_id);

      chatHistories.update(h => {
        const newMap = new Map(h);
        newMap.set(conversationId, []);
        return newMap;
      });

      onClearStateForConversation(conversationId);
      newSessionResult.set(null);
    }
  });

  // Clear chat window on conversation reset (v0.11.3 — AI chats and P2P resets)
  $effect(() => {
    if ($conversationReset) {
      const conversationId = $conversationReset.conversation_id;
      console.log('[ConversationReset] Clearing chat for:', conversationId);

      chatHistories.update(h => {
        const newMap = new Map(h);
        newMap.set(conversationId, []);
        return newMap;
      });

      onClearStateForConversation(conversationId);
      conversationReset.set(null);
    }
  });
</script>

<!-- No markup — logic-only panel -->
