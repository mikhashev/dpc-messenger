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
    modelDownloadProgress,
    connectionStatus,
    sendCommand,
  } from '$lib/coreService';
  import {
    downloadModel,
    declineModelDownload,
    resetModelDownloadDecline,
    getModelDownloadStatus,
  } from '$lib/services/modelDownload';
  import type { ModelDownloadRequiredEvent, ModelDownloadStatusEntry, ModelDownloadProgressEvent } from '$lib/types';
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
  // model_name -> latest progress, for every download running now. Kept
  // apart from `current`: the dialog may be closed while the download runs,
  // and the notice below keeps the bar.
  let progressMap = $state<Record<string, ModelDownloadProgressEvent>>({});
  let runningModels = $state<string[]>([]);

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
        // A download that was running before this UI (re)connected.
        for (const [name, entry] of Object.entries(result.models)) {
          if (entry.state === 'downloading' && !runningModels.includes(name)) {
            runningModels = [...runningModels, name];
            if (entry.bytes_done != null) {
              progressMap = { ...progressMap, [name]: {
                model_name: name,
                bytes_done: entry.bytes_done,
                total_bytes: entry.total_bytes ?? null,
                progress_percent: entry.progress_percent ?? null,
              } };
            }
          }
        }
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
    const payload = $modelDownloadStarted;
    if (payload) {
      console.log('[ModelDownload] Download started:', payload);
      isDownloadingModel = true;
      if (!runningModels.includes(payload.model_name)) {
        runningModels = [...runningModels, payload.model_name];
      }
    }
  });

  $effect(() => {
    const payload = $modelDownloadProgress;
    if (payload) {
      progressMap = { ...progressMap, [payload.model_name]: payload };
      if (!runningModels.includes(payload.model_name)) {
        runningModels = [...runningModels, payload.model_name];
      }
    }
  });

  function finishRunning(modelName: string) {
    runningModels = runningModels.filter((n) => n !== modelName);
    const { [modelName]: _done, ...rest } = progressMap;
    progressMap = rest;
  }

  // Close dialog and show success toast when download completes
  $effect(() => {
    const payload = $modelDownloadCompleted;
    if (payload) {
      console.log('[ModelDownload] Download completed:', payload);
      const info = rememberedInfo[payload.model_name];
      isDownloadingModel = false;
      finishRunning(payload.model_name);
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
      finishRunning(payload.model_name);
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
    console.log(isDownloadingModel
      ? '[ModelDownload] Dialog hidden; the download continues'
      : '[ModelDownload] Dialog dismissed without a decision');
    showModelDownloadDialog = false;
    isDownloadingModel = false;
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

  // Running downloads get a notice with a bar, except the one whose dialog is
  // open and already shows it.
  let visibleRunning = $derived(
    runningModels.filter((n) => !(showModelDownloadDialog && current?.model_name === n))
  );

  // Models worth a compact "ask again" notice: declined or missing, and not
  // already the one being shown or queued.
  let askAgainModels = $derived(
    Object.entries(statusMap).filter(([name, entry]) =>
      (entry.state === 'declined' || entry.state === 'missing') &&
      current?.model_name !== name &&
      !runningModels.includes(name) &&
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
  progressPercent={current ? (progressMap[current.model_name]?.progress_percent ?? null) : null}
  bytesDone={current ? (progressMap[current.model_name]?.bytes_done ?? null) : null}
  totalBytes={current ? (progressMap[current.model_name]?.total_bytes ?? null) : null}
  on:download={handleModelDownload}
  on:decline={handleModelDecline}
  on:close={handleModelClose}
/>

<!-- Running downloads, shown whether or not the dialog is open, and ask-again
     notices for models the user declined or never decided on -->
{#if askAgainModels.length > 0 || visibleRunning.length > 0}
  <div class="model-download-notices">
    {#if visibleRunning.length > 0}
      {#each visibleRunning as name (name)}
        {@const p = progressMap[name]}
        <div class="model-download-notice running">
          <span class="notice-text">
            Downloading <strong>{name}</strong>{p?.progress_percent != null ? ` — ${p.progress_percent.toFixed(0)}%` : '…'}
          </span>
          <div class="notice-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow={p?.progress_percent ?? undefined}>
            <div class="notice-fill" style="width: {p?.progress_percent ?? 0}%"></div>
          </div>
        </div>
      {/each}
    {/if}
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

  .model-download-notice.running {
    flex-direction: column;
    align-items: stretch;
    border-left-color: #4a9eff;
  }

  .notice-bar {
    height: 6px;
    background: #3c3c3c;
    border-radius: 3px;
    overflow: hidden;
  }

  .notice-fill {
    height: 100%;
    background: #4a9eff;
    transition: width 0.4s ease;
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
