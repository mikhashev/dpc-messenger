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
    position: fixed;
    /* Bottom-right is where Windows paints its "Activate Windows" watermark,
       and that is drawn by the OS above every window — no z-index reaches it.
       The card sat under it and the text was unreadable, so it moves aside
       rather than fighting for the same corner. */
    left: 1rem;
    bottom: 1rem;
    z-index: 1000;
    width: min(380px, calc(100vw - 2rem));
    padding: 0.9rem 1rem;
    border-radius: 10px;
    /* --panel-bg is defined nowhere; the literal was the only thing painting
       this. Use the variable the other dialogs use, and keep an opaque
       fallback so the card never goes translucent. */
    background: var(--bg-secondary, #1e1e2e);
    border: 1px solid rgba(255, 193, 7, 0.65);
    box-shadow: 0 8px 28px rgba(0, 0, 0, 0.55);
    color: var(--text-primary, #f2f2f5);
    font-size: 0.9rem;
  }
  .head { margin-bottom: 0.6rem; }
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
