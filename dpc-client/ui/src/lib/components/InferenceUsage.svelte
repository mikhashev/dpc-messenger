<!-- InferenceUsage.svelte -->
<!-- The Usage block of the Inference Sharing tab: what this node served to
     peers, what it consumed from them, and what it ran for itself, each on
     its own list so a person sees plainly who spent what, how much and on
     what (Mike's call, 2026-09-14; board entry THE-LEDGER-COUNTS-EVERY-
     SHARED-CALL-AND-NEITHER-SIDE-CAN-SEE-IT-IN-THE-UI).

     Read-only, and shown in both modes: a ledger is a record, and editing the
     rules above does not edit what already happened. The shaping lives in
     inferenceUsage.ts, which is what the tests exercise; this file is the
     markup and the one call to the backend. -->

<script lang="ts">
  import { onMount } from 'svelte';
  import { sendCommand, firewallRulesUpdated } from '$lib/coreService';
  import {
    formatAmount,
    formatDuration,
    formatOwed,
    formatTokens,
    monthKey,
    monthLabel,
    shapeUsage,
    shiftMonth,
    type NamedNode,
    type UsageResponse,
    type UsageView,
  } from './inferenceUsage';

  /** The peers this tab already holds a name for — `knownNodes` of the same
   *  tab, so a node reads here exactly as it reads three blocks above. */
  let { nodes = [] }: { nodes?: NamedNode[] } = $props();

  const thisMonth = monthKey();
  let month = $state(thisMonth);
  let view = $state<UsageView>(shapeUsage(null, [], thisMonth));
  let loading = $state(false);
  let error = $state<string | null>(null);

  // Plain, not runes: nothing renders them. `asked` orders answers — a slow
  // answer about last month must not overwrite a fast answer about this one —
  // and `monthNow` lets the reload effect below name the month on screen
  // without reading the rune, which would make it depend on the month too.
  let asked = 0;
  let monthNow = thisMonth;

  async function load(forMonth: string) {
    const mine = ++asked;
    monthNow = forMonth;
    loading = true;
    error = null;
    try {
      const result = sendCommand('get_inference_usage', { month: forMonth });
      if (result === false) {
        if (mine === asked) { error = 'Not connected to the local service.'; loading = false; }
        return;
      }
      const response = (await (result as Promise<UsageResponse>)) ?? {};
      if (mine !== asked) return;
      if (response.status === 'error') {
        error = response.message || 'The ledger could not be read.';
      } else {
        view = shapeUsage(response, nodes, forMonth);
      }
    } catch (e) {
      if (mine === asked) error = e instanceof Error ? e.message : String(e);
    } finally {
      if (mine === asked) loading = false;
    }
  }

  function step(delta: number) {
    month = shiftMonth(month, delta);
    void load(month);
  }

  onMount(() => { void load(month); });

  // The same idiom as the rest of the dialog: a save of the rules rewrites
  // what the tab shows, and the ledger is re-read beside it. The guard keeps
  // the first store value, which arrives before any save, from asking twice.
  let seenRules = false;
  $effect(() => {
    const rules = $firewallRulesUpdated;
    if (!seenRules) { seenRules = true; return; }
    if (rules) void load(monthNow);
  });
</script>

<div class="subsection">
  <h4>4. Usage</h4>
  <p class="help-text-small">
    One call, one row in this node's ledger, read three ways. Amounts are shown as the rows
    carry them and are never recomputed: a call is priced once, by the node that made it.
  </p>

  <div class="inline-input-row period-row">
    <button class="btn-small" onclick={() => step(-1)} title="The month before">&lsaquo;</button>
    <strong class="period-name">{monthLabel(view.month)}</strong>
    <button class="btn-small" onclick={() => step(1)} disabled={month >= thisMonth} title="The month after">&rsaquo;</button>
    <button class="btn-small" onclick={() => load(month)} disabled={loading}>Refresh</button>
    {#if loading}<span class="muted">Reading the ledger&hellip;</span>{/if}
  </div>

  {#if error}
    <div class="refusal" role="alert">The ledger could not be read: {error}</div>
  {/if}

  {#each view.lists as list (list.role)}
    <div class="peer-card usage-card">
      <div class="group-header">
        <h5>{list.title}</h5>
        <span class="muted">
          {#if list.role === 'served'}to whom, how much, for what
          {:else if list.role === 'consumed'}from whom, how much, for what
          {:else}this node's own vendor and local burn{/if}
        </span>
      </div>

      <div class="rule-list">
        {#each list.rows as row (row.key)}
          <div class="rule-row">
            <span class="alias-cell">
              <code class="rule-path">{row.label}</code>
              {#if row.alias && row.id}<span class="muted">{row.alias}</span>{/if}
              {#each row.badges as badge, i (badge.text + i)}
                <span class="badge badge-{badge.kind}">{badge.text}</span>
              {/each}
            </span>
            <span class="numbers-cell muted">
              <span>{row.calls} {row.calls === 1 ? 'call' : 'calls'}</span>
              <span>{formatTokens(row.promptTokens)} in / {formatTokens(row.completionTokens)} out</span>
              {#if row.thinkingTokens > 0}<span>{formatTokens(row.thinkingTokens)} thinking</span>{/if}
              <span>{formatDuration(row.durationS)}</span>
              {#if list.role === 'served'}
                <span class="money">owed to this node {formatOwed(row.owed)}</span>
                {#if row.costUsd > 0}<span>cost here ${formatAmount(row.costUsd)}</span>{/if}
              {:else if list.role === 'consumed'}
                <span class="money">owed to the host {formatOwed(row.owed)}</span>
              {:else}
                <span class="money">${formatAmount(row.costUsd)}</span>
              {/if}
              {#if list.role !== 'consumed' && row.unpriced > 0}
                <span>{row.unpriced} unpriced</span>
              {/if}
            </span>
          </div>
          {#each row.parts as part (part.key)}
            <div class="rule-row part-row">
              <span class="alias-cell">
                <code class="rule-path">{part.label}</code>
                {#each part.badges as badge, i (badge.text + i)}
                  <span class="badge badge-{badge.kind}">{badge.text}</span>
                {/each}
              </span>
              <span class="numbers-cell muted">
                <span>{part.calls} {part.calls === 1 ? 'call' : 'calls'}</span>
                <span>{formatTokens(part.promptTokens)} in / {formatTokens(part.completionTokens)} out</span>
                <span class="money">{formatOwed(part.owed)}</span>
              </span>
            </div>
          {/each}
        {:else}
          <p class="empty-small">{list.empty}</p>
        {/each}
      </div>
    </div>
  {/each}

  {#if view.hasUnpriceable}
    <p class="help-text-small">
      Unpriced rows counted, not summed: a tariff applied over counts whose convention nobody
      could read prices nothing, and such a row is counted apart rather than added in as zero.
    </p>
  {/if}
</div>

<style>
  /* Scoped copies of the tab's own classes: a child component does not see
     InferenceSharingEditor's styles, and the block must read as one dialog.
     Same declarations as there — this block adds no look of its own. */
  .subsection { margin-top: 1rem; padding-left: 1rem; border-left: 3px solid #e0e0e0; }
  .subsection h4 { margin: 0 0 0.5rem 0; font-size: 1rem; color: #555; }
  .subsection h5 { margin: 0.75rem 0 0.25rem; font-size: 0.85rem; color: #555; }
  .help-text-small { color: #666; font-size: 0.85rem; margin: 0.25rem 0 0.5rem 0; }
  .peer-card { border: 1px solid #e0e0e0; border-radius: 8px; padding: 1rem; margin-bottom: 1rem; background: #fafafa; }
  .peer-card h5 { margin: 0; color: #333; font-family: monospace; display: flex; gap: 0.5rem; align-items: center; }
  .group-header { display: flex; justify-content: space-between; align-items: center; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 0.75rem; }
  .rule-list { display: flex; flex-direction: column; gap: 0.5rem; }
  .rule-row { display: flex; justify-content: space-between; align-items: center; gap: 0.5rem; padding: 0.5rem; background: white; border-radius: 4px; flex-wrap: wrap; }
  .rule-path { font-size: 0.85rem; color: #555; }
  .empty-small { color: #999; font-style: italic; font-size: 0.9rem; margin: 0.25rem 0; }
  .btn-small { padding: 0.25rem 0.75rem; font-size: 0.85rem; background: #2196F3; color: white; border: none; border-radius: 4px; cursor: pointer; }
  .btn-small:disabled { opacity: 0.5; cursor: not-allowed; }
  .inline-input-row { display: flex; align-items: center; gap: 0.5rem; margin-top: 0.5rem; flex-wrap: wrap; }
  .alias-cell { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
  .muted { color: #777; font-size: 0.85rem; }
  .badge { padding: 0.1rem 0.5rem; border-radius: 10px; font-size: 0.75rem; white-space: nowrap; }
  .badge-first { background: #e3f2fd; color: #0d47a1; }
  .badge-missing { background: #fff3cd; color: #856404; }
  .badge-quota { background: #e8f5e9; color: #1b5e20; }
  .badge-gift { background: #f3e5f5; color: #4a148c; }
  .refusal { background: #f8d7da; color: #721c24; border-left: 3px solid #dc3545; padding: 0.75rem; font-size: 0.9rem; white-space: pre-wrap; }

  /* This block's own: the numbers column, and the alias lines under a caller. */
  .usage-card { margin-top: 0.5rem; }
  .period-row { margin-bottom: 0.75rem; }
  .period-name { color: #333; font-size: 0.9rem; }
  .numbers-cell { display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; justify-content: flex-end; }
  .numbers-cell .money { color: #0d47a1; }
  .part-row { margin-left: 1.5rem; background: #f5f5f5; }
</style>
