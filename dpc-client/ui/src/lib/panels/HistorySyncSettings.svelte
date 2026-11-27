<!-- HistorySyncSettings.svelte -->
<!-- The synced-history gate ([history] reject_unsigned, config-parity card, order 7). -->
<!-- A node-level switch that belongs beside the history surfaces: ChatPanel owns the -->
<!-- message list and the history-sync panels, and its composer area already carries the -->
<!-- conversation's other settings rows, so the gate lives here rather than in the -->
<!-- per-message list or the two logic-only panels. Read per synced record, so a save -->
<!-- applies to the next record that arrives. Off is the safe default the reader ships. -->

<script lang="ts">
  import { getHistorySyncConfig, saveHistorySyncConfig } from '$lib/coreService';

  let open = $state(false);
  let loaded = $state(false);
  let rejectUnsigned = $state(false);
  let busy = $state(false);
  let error = $state('');
  let saved = $state(false);

  function toggle() {
    open = !open;
    if (open && !loaded) void load();
  }

  async function load(): Promise<void> {
    loaded = true;
    try {
      const r = await getHistorySyncConfig();
      if (r?.status === 'success') rejectUnsigned = r.reject_unsigned === true;
    } catch { /* the default stands; saving still works */ }
  }

  async function save(): Promise<void> {
    busy = true;
    error = '';
    saved = false;
    try {
      const r = await saveHistorySyncConfig({ config: { reject_unsigned: rejectUnsigned } });
      if (r?.status === 'success') {
        saved = true;
      } else {
        error = r?.message || 'Could not save the history setting';
      }
    } catch (e) {
      error = `Error: ${e}`;
    } finally {
      busy = false;
    }
  }
</script>

<div class="history-settings">
  <button type="button" class="history-settings-header" onclick={toggle} aria-expanded={open}>
    <span class="history-settings-title">{open ? '▼' : '▶'} History settings</span>
  </button>
  {#if open}
    <label class="history-check">
      <input type="checkbox" bind:checked={rejectUnsigned} />
      <span>Reject synced history records this machine cannot check</span>
    </label>
    <p class="history-note">
      A synced record with no signature, or one signed in a form this machine cannot recompute,
      is refused when this is on and stored labelled "legacy" when it is off. Off is the default
      because history written before signatures existed carries none — refusing it would strand
      what is already on disk; on is the safer direction once that history has aged out. Applies
      to each synced record as it arrives; it never touches this node's own records.
    </p>
    {#if error}
      <p class="history-error" role="alert">{error}</p>
    {:else if saved}
      <p class="history-saved" role="status">Saved.</p>
    {/if}
    <button type="button" class="history-save" onclick={save} disabled={busy}>
      {busy ? 'Saving…' : 'Save history setting'}
    </button>
  {/if}
</div>

<style>
  .history-settings {
    padding: 0.5rem 0.75rem;
    background: #f7f7f9;
    border: 1px solid #e0e0e6;
    border-radius: 8px;
    margin-bottom: 0.5rem;
  }

  .history-settings-header {
    width: 100%;
    text-align: left;
    cursor: pointer;
    user-select: none;
    padding: 0;
    background: none;
    border: none;
  }

  .history-settings-title {
    font-weight: 600;
    color: #555;
    font-size: 0.85rem;
  }

  .history-check {
    display: flex;
    align-items: flex-start;
    gap: 0.5rem;
    margin: 0.6rem 0 0;
    cursor: pointer;
    user-select: none;
    font-size: 0.9rem;
    color: #333;
  }

  .history-note {
    margin: 0.5rem 0 0;
    font-size: 0.78rem;
    color: #777;
    line-height: 1.4;
  }

  .history-error,
  .history-saved {
    margin: 0.5rem 0 0;
    padding: 0.4rem 0.6rem;
    border-radius: 4px;
    font-size: 0.8rem;
  }

  .history-error {
    background: #f8d7da;
    color: #721c24;
    border: 1px solid #f5c6cb;
  }

  .history-saved {
    background: #d4edda;
    color: #155724;
    border: 1px solid #c3e6cb;
  }

  .history-save {
    margin-top: 0.6rem;
    padding: 0.4rem 0.9rem;
    background: #1976d2;
    color: white;
    border: none;
    border-radius: 4px;
    font-size: 0.85rem;
    cursor: pointer;
  }

  .history-save:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
</style>
