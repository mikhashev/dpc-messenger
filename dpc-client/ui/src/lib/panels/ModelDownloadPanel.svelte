<!-- src/lib/panels/ModelDownloadPanel.svelte -->
<!-- Whisper model download dialog + effects panel (Phase 3 Step 8) -->
<!-- Owns: showModelDownloadDialog, modelDownloadInfo, isDownloadingModel, toast state -->
<!-- Manages: whisperModelDownloadRequired/Started/Completed/Failed effects -->

<script lang="ts">
  import {
    whisperModelDownloadRequired,
    whisperModelDownloadStarted,
    whisperModelDownloadCompleted,
    whisperModelDownloadFailed,
    sendCommand,
  } from '$lib/coreService';
  import ModelDownloadDialog from '$lib/components/ModelDownloadDialog.svelte';
  import Toast from '$lib/components/Toast.svelte';

  // ---------------------------------------------------------------------------
  // State (all owned here — no external bindings needed)
  // ---------------------------------------------------------------------------
  let showModelDownloadDialog = $state(false);
  let modelDownloadInfo = $state<any>(null);
  let isDownloadingModel = $state(false);
  let showModelDownloadToast = $state(false);
  let modelDownloadToastMessage = $state('');
  let modelDownloadToastType = $state<'info' | 'error' | 'warning'>('info');

  // ---------------------------------------------------------------------------
  // Effects
  // ---------------------------------------------------------------------------

  // Open dialog when model download is required
  $effect(() => {
    if ($whisperModelDownloadRequired) {
      console.log('[ModelDownload] Model download required:', $whisperModelDownloadRequired);
      modelDownloadInfo = $whisperModelDownloadRequired;
      showModelDownloadDialog = true;
      isDownloadingModel = false;
    }
  });

  // Update download status when download starts
  $effect(() => {
    if ($whisperModelDownloadStarted) {
      console.log('[ModelDownload] Download started:', $whisperModelDownloadStarted);
      isDownloadingModel = true;
    }
  });

  // Close dialog and show success toast when download completes
  $effect(() => {
    if ($whisperModelDownloadCompleted) {
      console.log('[ModelDownload] Download completed:', $whisperModelDownloadCompleted);
      isDownloadingModel = false;
      showModelDownloadDialog = false;
      modelDownloadToastMessage = '✅ Model download successful! Voice transcription is now available.';
      modelDownloadToastType = 'info';
      showModelDownloadToast = true;
      whisperModelDownloadCompleted.set(null);
    }
  });

  // Show error toast when download fails
  $effect(() => {
    if ($whisperModelDownloadFailed) {
      console.error('[ModelDownload] Download failed:', $whisperModelDownloadFailed);
      isDownloadingModel = false;
      modelDownloadToastMessage = `❌ Model download failed: ${$whisperModelDownloadFailed.error}`;
      modelDownloadToastType = 'error';
      showModelDownloadToast = true;
      whisperModelDownloadFailed.set(null);
    }
  });

  // ---------------------------------------------------------------------------
  // Handlers
  // ---------------------------------------------------------------------------

  function handleModelDownload(event: CustomEvent) {
    // Fire-and-forget: the backend downloads in a thread pool; the started/
    // completed/failed effects above drive the UI. No blocking await.
    const { provider_alias } = event.detail;
    isDownloadingModel = true;
    sendCommand('download_whisper_model', { provider_alias });
  }

  function handleModelDownloadCancel() {
    console.log('[ModelDownload] User cancelled download');
    showModelDownloadDialog = false;
    whisperModelDownloadRequired.set(null);
  }
</script>

<!-- Model Download Dialog (v0.13.5) -->
<ModelDownloadDialog
  bind:open={showModelDownloadDialog}
  modelName={modelDownloadInfo?.model_name || ''}
  downloadSizeGb={modelDownloadInfo?.download_size_gb || 3.0}
  cachePath={modelDownloadInfo?.cache_path || ''}
  providerAlias={modelDownloadInfo?.provider_alias || ''}
  downloading={isDownloadingModel}
  on:download={handleModelDownload}
  on:cancel={handleModelDownloadCancel}
/>

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
