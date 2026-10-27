<!-- AgentTurnNotice.svelte — why an agent has not answered in this chat yet. -->
<!--
  ADR-046 point 8: a chat waiting behind another chat, or behind another agent
  in the same room, or a turn waiting for the model or for an approval, looked
  the same as a chat nobody answers. One small line per agent, under the input;
  never a message in history. The text is built in utils/agentTurnNotice.ts.
-->

<script lang="ts">
  import { onDestroy } from 'svelte';
  import { agentTurns, agentsList } from '$lib/services/agents';
  import { turnNoticeLines } from '$lib/utils/agentTurnNotice';

  let { chatId }: { chatId: string } = $props();

  // The waits are counted from a clock, not decremented into state.
  let now = $state(Date.now());
  const ticker = setInterval(() => (now = Date.now()), 1000);
  onDestroy(() => clearInterval(ticker));

  function nameOf(agentId: string): string {
    return $agentsList?.find((a: any) => a.agent_id === agentId)?.name || agentId;
  }

  let lines = $derived(chatId ? turnNoticeLines(chatId, $agentTurns, nameOf, now) : []);
</script>

{#each lines as line}
  <div class="agent-turn-notice" role="status" aria-live="polite">{line}</div>
{/each}

<style>
  .agent-turn-notice {
    margin: 0 0 0.5rem;
    padding: 0.4rem 0.75rem;
    border-radius: 6px;
    background: rgba(100, 149, 237, 0.1);
    border: 1px solid rgba(100, 149, 237, 0.35);
    color: #4169e1;
    font-size: 0.85rem;
    line-height: 1.35;
    font-variant-numeric: tabular-nums;
  }
</style>
