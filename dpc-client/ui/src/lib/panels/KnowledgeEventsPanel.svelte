<!-- src/lib/panels/KnowledgeEventsPanel.svelte -->
<!-- Knowledge/context lifecycle events: commit proposals, results, token warnings, context hash updates -->
<!-- Logic-only panel — no markup, no styles. -->

<script lang="ts">
  import {
    knowledgeCommitProposal,
    knowledgeCommitResult,
    tokenWarning,
    extractionFailure,
    contextUpdated,
    peerContextUpdated,
  } from '$lib/coreService';
  import { showNotificationIfBackground } from '$lib/notificationService';

  // ---------------------------------------------------------------------------
  // Props
  // ---------------------------------------------------------------------------
  let {
    onOpenCommitDialog,
    onUpdateTokenUsage,
    onShowTokenWarning,
    onShowExtractionFailure,
    onShowCommitResult,
    onCloseCommitDialog,
    onUpdateContextHash,
    onUpdatePeerContextHash,
  }: {
    onOpenCommitDialog: () => void;
    onUpdateTokenUsage: (conversationId: string, usage: { used: number; limit: number; historyTokens?: number; contextEstimated?: number }) => void;
    onShowTokenWarning: (message: string) => void;
    onShowExtractionFailure: (message: string) => void;
    onShowCommitResult: (message: string, type: 'info' | 'error' | 'warning', result: any) => void;
    onCloseCommitDialog: () => void;
    onUpdateContextHash: (hash: string) => void;
    onUpdatePeerContextHash: (nodeId: string, hash: string) => void;
  } = $props();

  // ---------------------------------------------------------------------------
  // Effects
  // ---------------------------------------------------------------------------

  // Open commit dialog when proposal received
  $effect(() => {
    if ($knowledgeCommitProposal) {
      onOpenCommitDialog();

      (async () => {
        const notified = await showNotificationIfBackground({
          title: 'Vote Requested',
          body: $knowledgeCommitProposal.proposal?.topic || 'Knowledge commit proposal'
        });
        console.log(`[Notifications] Knowledge proposal notification: ${notified ? 'system' : 'skip'}`);
      })();
    }
  });

  // Handle token warnings (Phase 2)
  $effect(() => {
    if ($tokenWarning) {
      const { conversation_id, tokens_used, token_limit, usage_percent,
              history_tokens, context_estimated } = $tokenWarning;

      onUpdateTokenUsage(conversation_id, {
        used: tokens_used,
        limit: token_limit,
        historyTokens: history_tokens ?? 0,
        contextEstimated: context_estimated ?? 0,
      });

      onShowTokenWarning(
        `Context window ${Math.round(usage_percent * 100)}% full. Consider ending session to save knowledge.`
      );
    }
  });

  // Handle knowledge extraction failures (Phase 4)
  $effect(() => {
    if ($extractionFailure) {
      const { conversation_id, reason } = $extractionFailure;
      onShowExtractionFailure(`Knowledge extraction failed for ${conversation_id}: ${reason}`);
    }
  });

  // Handle knowledge commit voting results
  $effect(() => {
    if ($knowledgeCommitResult) {
      const { status, topic, vote_tally } = $knowledgeCommitResult;

      let message: string;
      let type: 'info' | 'error' | 'warning';
      if (status === "approved") {
        message = `✅ Knowledge commit approved: ${topic} (${vote_tally.approve}/${vote_tally.total} votes) - Click for details`;
        type = "info";
      } else if (status === "rejected") {
        message = `❌ Knowledge commit rejected: ${topic} (${vote_tally.reject} reject, ${vote_tally.request_changes} change requests) - Click for details`;
        type = "error";
      } else if (status === "revision_needed") {
        message = `📝 Changes requested for: ${topic} (${vote_tally.request_changes}/${vote_tally.total} requested changes) - Click for details`;
        type = "warning";
      } else {
        // timeout
        message = `⏱️ Voting timeout for: ${topic} (${vote_tally.total} votes received) - Click for details`;
        type = "warning";
      }

      onShowCommitResult(message, type, $knowledgeCommitResult);
      onCloseCommitDialog();

      (async () => {
        const notified = await showNotificationIfBackground({
          title: 'Vote Complete',
          body: `${topic} - ${status}`
        });
        console.log(`[Notifications] Knowledge commit result notification: ${notified ? 'system' : 'skip'}`);
      })();

      knowledgeCommitResult.set(null);
    }
  });

  // Handle personal context updates (Phase 7 — "Updated" status indicator)
  $effect(() => {
    if ($contextUpdated) {
      const { context_hash } = $contextUpdated;
      if (context_hash) {
        onUpdateContextHash(context_hash);
        console.log(`[Context Updated] New hash: ${context_hash.slice(0, 8)}...`);
      }
    }
  });

  // Handle peer context updates (Phase 7 — "Updated" status indicators)
  $effect(() => {
    if ($peerContextUpdated) {
      const { node_id, context_hash } = $peerContextUpdated;
      if (node_id && context_hash) {
        onUpdatePeerContextHash(node_id, context_hash);
        console.log(`[Peer Context Updated] ${node_id.slice(0, 15)}... - hash: ${context_hash.slice(0, 8)}...`);
      }
    }
  });
</script>

<!-- No markup — logic-only panel -->
