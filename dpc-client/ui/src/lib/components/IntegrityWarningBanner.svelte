<!-- IntegrityWarningBanner.svelte - Knowledge integrity warning banner -->
<!-- Shown on startup when tampered/corrupted knowledge commits are detected -->

<script lang="ts">
  let {
    count,
    warnings,
    onDismiss
  }: {
    count: number;
    warnings: Array<{severity: string; file?: string; topic?: string; message: string; commit_id?: string}>;
    onDismiss: () => void;
  } = $props();

  let expanded = $state(false);

  let highCount = $derived(warnings.filter(w => w.severity === 'error').length);
  let label = $derived(
    highCount > 0
      ? `${highCount} corrupted commit${highCount > 1 ? 's' : ''} detected`
      : `${count} integrity issue${count > 1 ? 's' : ''} detected`
  );
</script>

<div class="integrity-banner" role="alert" aria-live="assertive" aria-atomic="true">
  <div class="banner-header">
    <span class="banner-icon" aria-hidden="true">
      {highCount > 0 ? '🔴' : '⚠️'}
    </span>
    <span class="banner-message">
      <strong>Knowledge integrity:</strong> {label}
    </span>
    <div class="banner-actions">
      <button
        class="btn-expand"
        onclick={() => expanded = !expanded}
        aria-label={expanded ? 'Collapse details' : 'Show details'}
      >
        {expanded ? 'Hide' : 'Details'}
      </button>
      <button
        class="btn-dismiss"
        onclick={onDismiss}
        aria-label="Dismiss integrity warning"
      >
        ✕
      </button>
    </div>
  </div>

  {#if expanded}
    <ul class="warning-list">
      {#each warnings as w}
        <li class="warning-item" class:error={w.severity === 'error'}>
          <span class="warning-icon" aria-hidden="true">{w.severity === 'error' ? '🔴' : '⚠️'}</span>
          <span class="warning-text">
            <strong>{w.file ?? w.topic ?? 'unknown'}</strong>: {w.message}
            {#if w.commit_id}
              <code class="commit-id">{w.commit_id.slice(0, 8)}</code>
            {/if}
          </span>
        </li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .integrity-banner {
    background: #fff3cd;
    border: 2px solid #e6a817;
    border-radius: 6px;
    margin-bottom: 0.5rem;
    animation: slideIn 0.3s ease-out;
    overflow: hidden;
  }

  .integrity-banner:has(.warning-item.error) {
    background: #f8d7da;
    border-color: #dc3545;
  }

  @keyframes slideIn {
    from { opacity: 0; transform: translateY(-8px); }
    to   { opacity: 1; transform: translateY(0); }
  }

  .banner-header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.65rem 1rem;
    color: #856404;
  }

  .integrity-banner:has(.warning-item.error) .banner-header {
    color: #721c24;
  }

  .banner-icon {
    font-size: 1.1rem;
    flex-shrink: 0;
  }

  .banner-message {
    flex: 1;
    font-size: 0.9rem;
    line-height: 1.4;
  }

  .banner-actions {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    flex-shrink: 0;
  }

  .btn-expand {
    padding: 0.25rem 0.6rem;
    background: transparent;
    border: 1px solid currentColor;
    border-radius: 4px;
    font-size: 0.8rem;
    cursor: pointer;
    color: inherit;
    opacity: 0.8;
    transition: opacity 0.2s;
  }

  .btn-expand:hover { opacity: 1; }

  .btn-dismiss {
    padding: 0.2rem 0.5rem;
    background: transparent;
    border: none;
    font-size: 1.1rem;
    cursor: pointer;
    color: inherit;
    opacity: 0.6;
    transition: opacity 0.2s;
    line-height: 1;
  }

  .btn-dismiss:hover { opacity: 1; }

  .warning-list {
    list-style: none;
    margin: 0;
    padding: 0 1rem 0.75rem;
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
    border-top: 1px solid rgba(0,0,0,0.1);
    padding-top: 0.5rem;
  }

  .warning-item {
    display: flex;
    align-items: flex-start;
    gap: 0.4rem;
    font-size: 0.82rem;
    color: #5a4000;
    line-height: 1.4;
  }

  .warning-item.error { color: #721c24; }

  .warning-icon { flex-shrink: 0; font-size: 0.85rem; }

  .commit-id {
    background: rgba(0,0,0,0.08);
    border-radius: 3px;
    padding: 0 3px;
    font-size: 0.78rem;
    margin-left: 0.3rem;
    font-family: monospace;
  }
</style>
