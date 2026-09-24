<!-- src/lib/panels/ModelDownloadPanel.svelte -->
<!-- Generic model download consent panel (2026-09-24) — any model (Whisper, -->
<!-- bge-m3, ...) can ask for download consent; this panel queues requests   -->
<!-- one dialog at a time, keyed by model_name, and offers "ask again" for   -->
<!-- models the user declined or dismissed without deciding.                -->
<!-- Owns: dialog queue/current, toast state, download-status map           -->
<!-- Manages: modelDownloadRequired/Started/Completed/Failed effects        -->

<script lang="ts">
  import {
    modelDownloadRequired,
    modelDownloadStarted,
    modelDownloadCompleted,
    modelDownloadFailed,
    connectionStatus,
    sendCommand,
  } from '$lib/coreService';
  import {
    downloadModel,
    declineModelDownload,
    resetModelDownloadDecline,
    getModelDownloadStatus,
  } from '$lib/services/modelDownload';
  import type { ModelDownloadRequiredEvent, ModelDownloadStatusEntry } from '$lib/types';
  import ModelDownloadDialog from '$lib/components/ModelDownloadDialog.svelte';
  import Toast from '$lib/components/Toast.svelte';

  // ---------------------------------------------------------------------------
  // State (all owned here — no external bindings needed)
  // ---------------------------------------------------------------------------
  let showModelDownloadDialog = $state(false);
  let current = $state<ModelDownloadRequiredEvent | null>(null);
  let queue = $state<ModelDownloadRequiredEvent[]>([]);
  // Every model_name we've ever queued/shown this session — dedupes repeat
  // `model_download_required` sets on the same store (defensive; the backend
  // itself only emits once per session per model).
  let seenModels = new Set<string>();
  // Remembered payloads, keyed by model_name — used to build toast text and
  // to re-open the dialog for "ask again" without another round trip.
  let rememberedInfo: Record<string, ModelDownloadRequiredEvent> = {};
  let isDownloadingModel = $state(false);

  let showModelDownloadToast = $state(false);
  let modelDownloadToastMessage = $state('');
  let modelDownloadToastType = $state<'info' | 'error' | 'warning'>('info');

  // model_name -> status entry, from get_model_download_status
  let statusMap = $state<Record<string, ModelDownloadStatusEntry>>({});
  let lastConnectionState: string | null = null;

  // ---------------------------------------------------------------------------
  // Status refresh
  // ---------------------------------------------------------------------------
  async function refreshStatus() {
    try {
      const result = await getModelDownloadStatus(sendCommand);
      if (result && result.models) {
        statusMap = result.models;
      }
    } catch (e) {
      console.error('[ModelDownload] Failed to fetch download status:', e);
    }
  }

  // Fetch once on WS connect (guard flag prevents refetching on every
  // reactive tick — see CLAUDE.md's "UI Integration Pattern" note).
  $effect(() => {
    const state = $connectionStatus;
    if (state === 'connected' && lastConnectionState !== 'connected') {
      refreshStatus();
    }
    lastConnectionState = state;
  });

  // ---------------------------------------------------------------------------
  // Effects
  // ---------------------------------------------------------------------------

  // Enqueue newly-required models
  $effect(() => {
    const payload = $modelDownloadRequired;
    if (payload && !seenModels.has(payload.model_name)) {
      console.log('[ModelDownload] Model download required:', payload);
      seenModels.add(payload.model_name);
      rememberedInfo[payload.model_name] = payload;
      queue = [...queue, payload];
    }
  });

  // Show one dialog at a time
  $effect(() => {
    if (!current && queue.length > 0) {
      current = queue[0];
      queue = queue.slice(1);
      showModelDownloadDialog = true;
      isDownloadingModel = false;
    }
  });

  // Update download status when download starts
  $effect(() => {
    if ($modelDownloadStarted) {
      console.log('[ModelDownload] Download started:', $modelDownloadStarted);
      isDownloadingModel = true;
    }
  });

  // Close dialog and show success toast when download completes
  $effect(() => {
    const payload = $modelDownloadCompleted;
    if (payload) {
      console.log('[ModelDownload] Download completed:', payload);
      const info = rememberedInfo[payload.model_name];
      isDownloadingModel = false;
      if (current?.model_name === payload.model_name) {
        showModelDownloadDialog = false;
        current = null;
      }
      modelDownloadToastMessage = `✅ ${payload.model_name} downloaded. ${info?.purpose || 'It'} is now available.`;
      modelDownloadToastType = 'info';
      showModelDownloadToast = true;
      modelDownloadCompleted.set(null);
      refreshStatus();
    }
  });

  // Show error toast when download fails (dialog stays open for a retry)
  $effect(() => {
    const payload = $modelDownloadFailed;
    if (payload) {
      console.error('[ModelDownload] Download failed:', payload);
      isDownloadingModel = false;
      modelDownloadToastMessage = `❌ ${payload.model_name} download failed: ${payload.error}`;
      modelDownloadToastType = 'error';
      showModelDownloadToast = true;
      modelDownloadFailed.set(null);
      refreshStatus();
    }
  });

  // ---------------------------------------------------------------------------
  // Handlers
  // ---------------------------------------------------------------------------

  function handleModelDownload(event: CustomEvent) {
    if (!current) return;
    // Fire-and-forget: the backend downloads in a thread pool; the started/
    // completed/failed effects above drive the UI. No blocking await.
    const providerAlias = event.detail?.provider_alias ?? current.provider_alias ?? undefined;
    isDownloadingModel = true;
    downloadModel(current.model_name, providerAlias, sendCommand);
  }

  // "Not now" (remember=false) or "Don't remind me" (remember=true), both
  // dispatched by the dialog's decline buttons.
  function handleModelDecline(event: CustomEvent) {
    if (!current) return;
    const remember = !!event.detail?.remember;
    declineModelDownload(current.model_name, remember, sendCommand).then(refreshStatus);
    showModelDownloadDialog = false;
    current = null;
  }

  // Esc/X: same as "not now" but sends nothing — the backend already treats
  // silence as not-persisted (only remember=true is ever written to disk),
  // so there is nothing useful to tell it. Next start (or "ask again") asks
  // once more.
  function handleModelClose() {
    console.log('[ModelDownload] Dialog dismissed without a decision');
    showModelDownloadDialog = false;
    current = null;
    refreshStatus();
  }

  // "Ask again" — for a declined model, clear the decline server-side (which
  // re-asks if still missing); for a merely-missing one (dialog dismissed,
  // nothing persisted), just re-open the dialog we already have data for.
  function handleAskAgain(modelName: string, state: string) {
    if (state === 'declined') {
      seenModels.delete(modelName);
      resetModelDownloadDecline(modelName, sendCommand).then(refreshStatus);
    } else {
      const payload = rememberedInfo[modelName];
      if (payload && !queue.some((p) => p.model_name === modelName) && current?.model_name !== modelName) {
        queue = [...queue, payload];
      }
    }
  }

  // Models worth a compact "ask again" notice: declined or missing, and not
  // already the one being shown or queued.
  let askAgainModels = $derived(
    Object.entries(statusMap).filter(([name, entry]) =>
      (entry.state === 'declined' || entry.state === 'missing') &&
      current?.model_name !== name &&
      !queue.some((p) => p.model_name === name)
    )
  );
</script>

<!-- Model Download Dialog (generic, 2026-09-24) -->
<ModelDownloadDialog
  open={showModelDownloadDialog}
  modelName={current?.model_name || ''}
  purpose={current?.purpose || ''}
  consequenceIfDeclined={current?.consequence_if_declined || ''}
  sizeBytes={current?.size_bytes ?? null}
  sizeSource={current?.size_source ?? null}
  cachePath={current?.cache_path || ''}
  providerAlias={current?.provider_alias ?? null}
  downloading={isDownloadingModel}
  on:download={handleModelDownload}
  on:decline={handleModelDecline}
  on:close={handleModelClose}
/>

<!-- Ask-again notices for models the user declined or never decided on -->
{#if askAgainModels.length > 0}
  <div class="model-download-notices">
    {#each askAgainModels as [name, entry] (name)}
      <div class="model-download-notice">
        <span class="notice-text">
          {rememberedInfo[name]?.purpose || 'A feature'} is off: <strong>{name}</strong> not downloaded
        </span>
        <button class="notice-action" onclick={() => handleAskAgain(name, entry.state)}>
          Download…
        </button>
      </div>
    {/each}
  </div>
{/if}

<!-- Model Download Toast (v0.13.5) -->
{#if showModelDownloadToast}
  <Toast
    message={modelDownloadToastMessage}
    type={modelDownloadToastType}
    duration={modelDownloadToastType === 'error' ? 10000 : 5000}
    dismissible={true}
    onDismiss={() => {
      showModelDownloadToast = false;
      modelDownloadToastMessage = '';
    }}
  />
{/if}

<style>
  .model-download-notices {
    position: fixed;
    bottom: 16px;
    left: 16px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    z-index: 900;
    max-width: 360px;
  }

  .model-download-notice {
    background: #2a2a2a;
    border: 1px solid #3c3c3c;
    border-left: 3px solid #ffa500;
    border-radius: 4px;
    padding: 8px 12px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    font-size: 0.85rem;
    color: #d0d0d0;
  }

  .notice-text strong {
    color: #4a9eff;
  }

  .notice-action {
    background: transparent;
    border: 1px solid #4a9eff;
    color: #4a9eff;
    border-radius: 4px;
    padding: 4px 10px;
    font-size: 0.8rem;
    cursor: pointer;
    white-space: nowrap;
  }

  .notice-action:hover {
    background: #4a9eff;
    color: white;
  }
</style>
