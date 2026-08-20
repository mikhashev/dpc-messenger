<!-- ScheduleApprovalDialog.svelte -->
<!-- An agent asks to queue a deferred wake-up. The person decides BEFORE it -->
<!-- is queued, so what is planned and for when is visible in advance. -->
<script lang="ts">
  import { pendingScheduleApprovals, respondToScheduleRequest } from '$lib/services/scheduleApproval';

  // Only the oldest is shown: two of these stacked is a decision no one reads.
  $: request = $pendingScheduleApprovals[0] ?? null;
  $: queued = $pendingScheduleApprovals.length;
</script>

{#if request}
  <div class="schedule-approval" role="dialog" aria-label="Agent wants to schedule a task">
    <div class="head">
      <strong>{request.agent_name}</strong> wants to come back to this later
      {#if request.conversation_title || request.conversation_id}<span class="origin"
        >in {request.conversation_title || request.conversation_id}</span
      >{/if}
      {#if queued > 1}<span class="more">+{queued - 1} more</span>{/if}
    </div>

    <dl>
      <dt>When</dt>
      <dd>{request.when}</dd>
      <dt>What it will do</dt>
      <dd class="about">{request.about || '(no description given)'}</dd>
    </dl>

    <div class="actions">
      <button class="allow" on:click={() => respondToScheduleRequest(request.request_id, true)}>
        Schedule it
      </button>
      <button class="deny" on:click={() => respondToScheduleRequest(request.request_id, false)}>
        Not now
      </button>
    </div>
  </div>
{/if}

<style>
  .schedule-approval {
    /* Same corner and offset as ShellApprovalDialog: bottom-right, lifted 80px.
       That lift is what clears the composer and the Windows watermark, which
       the OS paints above every window — no z-index reaches it. One approval
       card should not sit somewhere different from the other. */
    position: fixed;
    bottom: 80px;
    right: 20px;
    z-index: 1000;
    max-width: 420px;
    width: min(420px, calc(100vw - 40px));
    padding: 10px 12px;
    border-radius: 8px;
    background: var(--bg-secondary, #1e1e2e);
    border: 1px solid var(--border-warning, #ffc107);
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
    color: var(--text-primary, #cdd6f4);
    font-size: 0.9em;
  }
  .head { margin-bottom: 0.6rem; }
  /* Which chat the request came from — the card named the agent and nothing
     else, and one agent works in several chats. */
  .origin {
    opacity: 0.7;
    font-size: 0.9em;
    margin-left: 0.35em;
  }

  .more {
    margin-left: 0.4rem;
    opacity: 0.7;
    font-size: 0.8rem;
  }
  dl {
    margin: 0 0 0.8rem;
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 0.25rem 0.6rem;
  }
  dt { opacity: 0.8; font-size: 0.8rem; }
  dd { margin: 0; }
  .about {
    max-height: 5.5rem;
    overflow-y: auto;
    word-break: break-word;
  }
  .actions { display: flex; gap: 0.5rem; }
  button {
    flex: 1;
    padding: 0.45rem 0.6rem;
    border-radius: 6px;
    border: 1px solid transparent;
    cursor: pointer;
    font-size: 0.85rem;
  }
  .allow { background: #2e7d32; color: #fff; }
  .deny { background: transparent; border-color: rgba(255, 255, 255, 0.25); color: inherit; }
</style>
