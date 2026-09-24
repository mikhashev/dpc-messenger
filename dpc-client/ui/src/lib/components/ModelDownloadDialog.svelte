<script lang="ts">
  import { createEventDispatcher } from 'svelte';

  // Props — generic across any model (Whisper, bge-m3, ...). No numeric
  // size default: size_bytes null means "unknown", shown as such rather
  // than guessed.
  export let open: boolean = false;
  export let modelName: string = '';
  export let purpose: string = '';
  export let consequenceIfDeclined: string = '';
  export let sizeBytes: number | null = null;
  export let sizeSource: 'measured' | 'hf_api' | null = null;
  export let cachePath: string = '';
  export let providerAlias: string | null = null;
  export let downloading: boolean = false;

  const dispatch = createEventDispatcher();

  $: sizeText = sizeBytes == null
    ? 'Size unknown'
    : `~${(sizeBytes / 1e9).toFixed(1)}GB${sizeSource === 'hf_api' ? ' (approximate, from Hugging Face)' : ''}`;

  function handleDownload() {
    dispatch('download', {
      provider_alias: providerAlias
    });
  }

  function handleNotNow() {
    dispatch('decline', { remember: false });
  }

  function handleDontRemind() {
    dispatch('decline', { remember: true });
  }

  // Esc/X closes the dialog with "not now" semantics, but sends nothing to
  // the backend — silence is already treated as not-persisted (only
  // remember=true is written to config.ini), so there's nothing to tell it.
  function handleClose() {
    dispatch('close');
  }
</script>

{#if open}
  <div class="modal-overlay" role="presentation">
    <div class="modal" role="dialog" aria-labelledby="dialog-title" tabindex="-1">
      <div class="modal-header">
        <h2 id="dialog-title">📥 Download Model</h2>
        <button class="btn-close" on:click={handleClose} disabled={downloading} aria-label="Close">✕</button>
      </div>

      <div class="modal-body">
        <!-- Model Info -->
        <div class="section">
          <p class="model-message">
            The model <strong>{modelName}</strong> is not found in your local cache.
          </p>
          {#if purpose}
            <p class="model-details">
              This model is required for {purpose}. Would you like to download it now?
            </p>
          {/if}
          {#if consequenceIfDeclined}
            <p class="model-details">{consequenceIfDeclined}</p>
          {/if}
        </div>

        <!-- Download Size Info -->
        <div class="section info-box">
          <p><strong>Download Size:</strong> {sizeText}</p>
          <p><strong>Cache Location:</strong> <code>{cachePath}</code></p>
          {#if providerAlias}
            <p><strong>Provider:</strong> {providerAlias}</p>
          {/if}
        </div>

        <!-- Instructions -->
        <div class="section instructions">
          <p><strong>What happens next:</strong></p>
          <ul>
            <li>The model will be downloaded from Hugging Face (may take a few minutes)</li>
            <li>After download, it will be cached locally for offline use</li>
            <li>You only need to download once</li>
          </ul>
        </div>

        <!-- Download Progress (if downloading) -->
        {#if downloading}
          <div class="section progress-section">
            <p class="downloading-text">⏳ Downloading model, please wait...</p>
            <p class="downloading-note">This may take several minutes depending on your connection.</p>
          </div>
        {/if}
      </div>

      <div class="modal-footer">
        <button
          class="btn-remind"
          on:click={handleDontRemind}
          disabled={downloading}
        >
          Don't remind me
        </button>
        <button
          class="btn-cancel"
          on:click={handleNotNow}
          disabled={downloading}
        >
          Not now
        </button>
        <button
          class="btn-download"
          on:click={handleDownload}
          disabled={downloading}
        >
          {downloading ? '⏳ Downloading...' : '📥 Download'}
        </button>
      </div>
    </div>
  </div>
{/if}

<style>
  .modal-overlay {
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    background: rgba(0, 0, 0, 0.7);
    display: flex;
    justify-content: center;
    align-items: center;
    z-index: 1000;
  }

  .modal {
    background: #1e1e1e;
    border: 1px solid #3c3c3c;
    border-radius: 8px;
    width: 90%;
    max-width: 600px;
    max-height: 80vh;
    display: flex;
    flex-direction: column;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.5);
  }

  .modal-header {
    padding: 16px 20px;
    border-bottom: 1px solid #3c3c3c;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  .modal-header h2 {
    margin: 0;
    font-size: 1.25rem;
    color: #e0e0e0;
  }

  .btn-close {
    background: transparent;
    border: none;
    color: #b0b0b0;
    font-size: 1rem;
    cursor: pointer;
    padding: 4px 8px;
    border-radius: 4px;
  }

  .btn-close:hover:not(:disabled) {
    background: #2a2a2a;
    color: #e0e0e0;
  }

  .btn-close:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .modal-body {
    padding: 20px;
    overflow-y: auto;
    flex: 1;
  }

  .section {
    margin-bottom: 16px;
  }

  .model-message {
    font-size: 1.1rem;
    color: #e0e0e0;
    margin-bottom: 8px;
  }

  .model-message strong {
    color: #4a9eff;
  }

  .model-details {
    color: #b0b0b0;
    font-size: 0.95rem;
    margin: 0 0 6px 0;
  }

  .info-box {
    background: #2a2a2a;
    border: 1px solid #3c3c3c;
    border-radius: 4px;
    padding: 12px;
  }

  .info-box p {
    margin: 6px 0;
    color: #d0d0d0;
    font-size: 0.9rem;
  }

  .info-box code {
    background: #1a1a1a;
    padding: 2px 6px;
    border-radius: 3px;
    font-family: 'Courier New', monospace;
    font-size: 0.85rem;
    color: #7ec699;
  }

  .instructions {
    background: #2a2a2a;
    border-left: 3px solid #4a9eff;
    padding: 12px;
    border-radius: 4px;
  }

  .instructions p {
    margin: 0 0 8px 0;
    color: #d0d0d0;
    font-size: 0.9rem;
  }

  .instructions ul {
    margin: 8px 0 0 0;
    padding-left: 20px;
    color: #b0b0b0;
    font-size: 0.9rem;
  }

  .instructions li {
    margin-bottom: 6px;
  }

  .progress-section {
    background: #2a2a2a;
    border-left: 3px solid #ffa500;
    padding: 12px;
    border-radius: 4px;
    text-align: center;
  }

  .downloading-text {
    margin: 0 0 8px 0;
    color: #ffa500;
    font-size: 1rem;
    font-weight: 500;
  }

  .downloading-note {
    margin: 0;
    color: #b0b0b0;
    font-size: 0.85rem;
  }

  .modal-footer {
    padding: 16px 20px;
    border-top: 1px solid #3c3c3c;
    display: flex;
    justify-content: flex-end;
    gap: 12px;
  }

  .modal-footer button {
    padding: 8px 20px;
    border: none;
    border-radius: 4px;
    font-size: 0.95rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s;
  }

  .modal-footer button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .btn-download {
    background: #28a745;
    color: white;
  }

  .btn-download:hover:not(:disabled) {
    background: #218838;
  }

  .btn-cancel {
    background: #6c757d;
    color: white;
  }

  .btn-cancel:hover:not(:disabled) {
    background: #5a6268;
  }

  .btn-remind {
    background: transparent;
    color: #b0b0b0;
    border: 1px solid #3c3c3c;
  }

  .btn-remind:hover:not(:disabled) {
    background: #2a2a2a;
    color: #e0e0e0;
  }

  .modal-footer button:active:not(:disabled) {
    transform: scale(0.98);
  }
</style>
