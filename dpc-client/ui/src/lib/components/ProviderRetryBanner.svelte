<!-- ProviderRetryBanner.svelte — what a provider is doing while it waits. -->
<!--
  The retry ladder in providers/base.py keeps a ten-minute wall-clock budget for
  anything the far end can recover from. That patience is right when nobody is
  watching and wrong when someone is: on 2026-09-02 a DeepSeek outage lasted
  hours and came back on its own, but until this strip existed the interface
  showed nothing at all for ten minutes and then a failure.

  So: say which attempt, how long the wait is, how much of the budget is spent,
  and offer the way out. Cancel ends the request, not just the sleep.

  One row per wait: an agent and a chat can be waiting on different providers at
  once, and each row carries the `retry_id` its own Cancel is sent with.
-->

<script lang="ts">
  import { providerRetries, type ProviderRetry } from '$lib/services/providers';
  import { sendCommand } from '$lib/coreService';
  import { onDestroy } from 'svelte';
  import { secondsLeft } from '$lib/utils/retryCountdown';

  // The countdown is derived from a clock rather than decremented into state:
  // an effect that both reads and writes the same rune re-runs itself.
  let now = $state(Date.now());
  let ticker: ReturnType<typeof setInterval> | null = null;

  // Plain, not reactive — written from inside an effect on purpose.
  const arrivedAt = new Map<string, number>();

  let cancelling = $state<Record<string, boolean>>({});
  let notes = $state<Record<string, string>>({});

  $effect(() => {
    const rows = [...$providerRetries.values()];
    // Each announcement is one sleep; a new attempt restarts that row's clock.
    for (const r of rows) {
      const key = `${r.retry_id}:${r.attempt}`;
      if (!arrivedAt.has(key)) arrivedAt.set(key, Date.now());
    }
    if (rows.length === 0) {
      arrivedAt.clear();
      cancelling = {};
      notes = {};
    }
    if (rows.length > 0 && !ticker) {
      ticker = setInterval(() => (now = Date.now()), 1000);
    } else if (rows.length === 0 && ticker) {
      clearInterval(ticker);
      ticker = null;
    }
  });

  onDestroy(() => { if (ticker) clearInterval(ticker); });

  function leftFor(r: ProviderRetry): number {
    return secondsLeft(r.waiting_seconds, arrivedAt.get(`${r.retry_id}:${r.attempt}`) ?? now, now);
  }

  function drop(retryId: string) {
    providerRetries.update(m => {
      const next = new Map(m);
      next.delete(retryId);
      return next;
    });
  }

  async function cancel(r: ProviderRetry) {
    if (cancelling[r.retry_id]) return;
    cancelling = { ...cancelling, [r.retry_id]: true };
    try {
      const res: any = await sendCommand('cancel_provider_retry', { retry_id: r.retry_id });
      const st = res?.status || res?.payload?.status;
      if (st === 'cancelled' || st === 'not_waiting') {
        // The backend closes the notice too (outcome «cancelled»); dropping the
        // row here makes the click feel immediate rather than round-trip-shaped.
        drop(r.retry_id);
      } else {
        cancelling = { ...cancelling, [r.retry_id]: false };
        notes = { ...notes, [r.retry_id]: res?.message || 'could not stop it' };
      }
    } catch (err: any) {
      cancelling = { ...cancelling, [r.retry_id]: false };
      notes = { ...notes, [r.retry_id]: err?.message || 'send failed' };
    }
  }
</script>

{#each [...$providerRetries.values()] as r (r.retry_id)}
  <div class="retry-banner" role="status" aria-live="polite">
    <div class="retry-content">
      <span class="retry-icon" aria-hidden="true">⏳</span>
      <span class="retry-text">
        <strong>{r.provider}</strong>
        <span class="alias">({r.alias})</span>
        — retry {r.attempt},
        {#if leftFor(r) > 0}
          waiting {leftFor(r)}s
        {:else}
          trying again
        {/if}
        <span class="budget">· {r.elapsed_seconds}s of {r.budget_seconds}s</span>
      </span>
    </div>
    <div class="retry-detail" title={r.error}>
      {r.unreachable ? 'cannot reach the host' : 'the service refused'}: {r.error}
    </div>
    <div class="retry-actions">
      {#if notes[r.retry_id]}<span class="cancel-note">{notes[r.retry_id]}</span>{/if}
      <button class="btn-cancel-retry" onclick={() => cancel(r)} disabled={cancelling[r.retry_id]}
              aria-label="Stop waiting and abandon the request">
        {cancelling[r.retry_id] ? 'stopping…' : 'Cancel'}
      </button>
    </div>
  </div>
{/each}

<style>
  .retry-banner {
    display: grid;
    grid-template-columns: 1fr auto;
    grid-template-areas: "content actions" "detail actions";
    align-items: center;
    gap: 0.25rem 1rem;
    padding: 0.6rem 1rem;
    border: 2px solid #ffc107;
    background: #fff3cd;
    color: #856404;
    border-radius: 6px;
    margin-bottom: 0.5rem;
  }

  .retry-content { grid-area: content; display: flex; align-items: center; gap: 0.5rem; }
  .retry-icon { font-size: 1.1rem; flex-shrink: 0; }
  .retry-text { font-size: 0.9rem; line-height: 1.4; }
  .alias { opacity: 0.75; }
  .budget { opacity: 0.7; font-variant-numeric: tabular-nums; }

  .retry-detail {
    grid-area: detail;
    font-size: 0.78rem;
    opacity: 0.8;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .retry-actions { grid-area: actions; display: flex; align-items: center; gap: 0.5rem; }
  .cancel-note { font-size: 0.78rem; opacity: 0.85; }

  .btn-cancel-retry {
    padding: 0.35rem 0.8rem;
    background: transparent;
    color: #856404;
    border: 1px solid #856404;
    border-radius: 4px;
    font-size: 0.85rem;
    cursor: pointer;
    white-space: nowrap;
    transition: all 0.2s;
  }
  .btn-cancel-retry:hover:not(:disabled) { background: #856404; color: #fff3cd; }
  .btn-cancel-retry:disabled { opacity: 0.6; cursor: default; }

  @media (max-width: 600px) {
    .retry-banner {
      grid-template-columns: 1fr;
      grid-template-areas: "content" "detail" "actions";
    }
    .retry-actions { justify-content: flex-end; }
  }
</style>
