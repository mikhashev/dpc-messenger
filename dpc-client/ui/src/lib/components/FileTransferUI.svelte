<!-- FileTransferUI.svelte - Extracted file transfer UI components -->
<!-- Displays image/voice preview, file transfer dialogs, and active transfers panel -->

<script lang="ts">
  import Toast from './Toast.svelte';
  import VoicePlayer from './VoicePlayer.svelte';

  // Props (Svelte 5 runes mode)
  let {
    // Image preview state
    pendingImage = null,
    onClearPendingImage,

    // Voice preview state (v0.13.0+)
    voicePreview = null,
    onClearVoicePreview,
    onSendVoiceMessage,
    onTranscribeVoiceMessage,
    isLocalAIChat = false,

    // File offer dialog state
    showFileOfferDialog = false,
    currentFileOffer = null,
    onAcceptFile,
    onRejectFile,

    // Send file confirmation dialog state
    showSendFileDialog = false,
    pendingFileSend = null,
    isSendingFile = false,
    filePreparationStarted = null,
    filePreparationProgress = null,
    filePreparationCompleted = null,
    onConfirmSendFile,
    onCancelSendFile,

    // Active transfers state
    activeFileTransfers = new Map(),
    onCancelTransfer,

    // Toast notification state
    showFileOfferToast = false,
    fileOfferToastMessage = '',
    onDismissToast
  }: {
    pendingImage?: { dataUrl: string; filename: string; sizeBytes: number } | null;
    onClearPendingImage: () => void;
    voicePreview?: { blob: Blob; duration: number } | null;
    onClearVoicePreview: () => void;
    onSendVoiceMessage: () => void;
    onTranscribeVoiceMessage?: () => Promise<void>;
    isLocalAIChat?: boolean;
    showFileOfferDialog?: boolean;
    currentFileOffer?: any;
    onAcceptFile: () => void;
    onRejectFile: () => void;
    showSendFileDialog?: boolean;
    pendingFileSend?: { filePath: string; fileName: string; recipientId: string; recipientName: string } | null;
    isSendingFile?: boolean;
    filePreparationStarted?: any;
    filePreparationProgress?: any;
    filePreparationCompleted?: any;
    onConfirmSendFile: () => void;
    onCancelSendFile: () => void;
    activeFileTransfers?: Map<string, any>;
    onCancelTransfer: (transferId: string, filename: string) => void;
    showFileOfferToast?: boolean;
    fileOfferToastMessage?: string;
    onDismissToast: () => void;
  } = $props();
</script>

<!-- Image Preview Chip -->
{#if pendingImage}
  <div class="image-preview-chip">
    <img src={pendingImage.dataUrl} alt={pendingImage.filename} class="preview-thumbnail" />
    <div class="preview-info">
      <span class="preview-filename">{pendingImage.filename}</span>
      <span class="preview-size">{(pendingImage.sizeBytes / (1024 * 1024)).toFixed(2)} MB</span>
    </div>
    <button class="preview-remove" onclick={onClearPendingImage} aria-label="Remove image">✕</button>
  </div>
{/if}

<!-- Voice Preview Chip (v0.13.0+) -->
{#if voicePreview}
  <div class="voice-preview-chip">
    <div class="voice-icon">🎤</div>
    <div class="preview-info">
      <span class="preview-filename">Voice Message</span>
      <span class="preview-size">{voicePreview.duration.toFixed(1)}s</span>
    </div>
    <VoicePlayer
      audioUrl={URL.createObjectURL(voicePreview.blob)}
      duration={voicePreview.duration}
      compact={true}
    />
    {#if isLocalAIChat && onTranscribeVoiceMessage}
      <button
        class="voice-transcribe-button"
        onclick={onTranscribeVoiceMessage}
        title="Transcribe and send to AI"
      >
        📝 Send
      </button>
    {:else}
      <button
        class="voice-send-button"
        onclick={onSendVoiceMessage}
        title="Send voice message"
      >
        Send
      </button>
    {/if}
    <button
      class="preview-remove"
      onclick={onClearVoicePreview}
      aria-label="Remove voice"
    >✕</button>
  </div>
{/if}

<!-- File Offer Dialog -->
{#if showFileOfferDialog && currentFileOffer}
  <div class="modal-overlay" role="presentation" onclick={onRejectFile} onkeydown={(e) => e.key === 'Escape' && onRejectFile()}>
    <div class="modal-dialog" role="dialog" aria-modal="true" tabindex="-1" onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()}>
      <h3>Incoming File</h3>
      <p><strong>File:</strong> {currentFileOffer.filename}</p>
      <p><strong>Size:</strong> {(currentFileOffer.size_bytes / 1024 / 1024).toFixed(2)} MB</p>
      <p><strong>From:</strong> {currentFileOffer.node_id.slice(0, 20)}...</p>
      <div class="modal-buttons">
        <button class="accept-button" onclick={onAcceptFile}>Accept</button>
        <button class="reject-button" onclick={onRejectFile}>Reject</button>
      </div>
    </div>
  </div>
{/if}

<!-- Send File Confirmation Dialog -->
{#if showSendFileDialog && pendingFileSend}
  <div class="modal-overlay" role="presentation" onclick={onCancelSendFile} onkeydown={(e) => e.key === 'Escape' && onCancelSendFile()}>
    <div class="modal-dialog" role="dialog" aria-modal="true" tabindex="-1" onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()}>
      <h3>Send File</h3>
      <p><strong>File:</strong> {pendingFileSend.fileName}</p>
      <p><strong>To:</strong> {pendingFileSend.recipientName}</p>

      {#if filePreparationStarted && isSendingFile}
        <p style="margin-top: 10px; font-size: 13px;">
          <strong>Size:</strong> {filePreparationStarted.size_mb} MB
        </p>
      {/if}

      {#if filePreparationProgress && isSendingFile}
        <div style="margin-top: 15px;">
          <p style="font-size: 13px; margin-bottom: 5px; color: #555;">
            {#if filePreparationProgress.phase === 'hashing_file'}
              Computing file hash: {filePreparationProgress.percent}%
            {:else if filePreparationProgress.phase === 'computing_chunks'}
              Computing chunk hashes: {filePreparationProgress.percent}%
            {:else}
              Preparing file: {filePreparationProgress.percent}%
            {/if}
          </p>
          <div style="width: 100%; background-color: #e0e0e0; border-radius: 4px; height: 8px; overflow: hidden;">
            <div style="width: {filePreparationProgress.percent}%; background-color: #4CAF50; height: 100%; transition: width 0.3s ease;"></div>
          </div>
        </div>
      {/if}

      <div class="modal-buttons">
        <button class="accept-button" onclick={onConfirmSendFile} disabled={isSendingFile}>
          {#if filePreparationCompleted && isSendingFile}
            Sending...
          {:else if isSendingFile}
            Preparing...
          {:else}
            Send
          {/if}
        </button>
        <button class="reject-button" onclick={onCancelSendFile} disabled={isSendingFile}>Cancel</button>
      </div>
    </div>
  </div>
{/if}

<!-- File Transfer Toast -->
{#if showFileOfferToast}
  <Toast
    message={fileOfferToastMessage}
    type="info"
    duration={5000}
    dismissible={true}
    onDismiss={onDismissToast}
  />
{/if}

<!-- Active File Transfers Progress -->
{#if activeFileTransfers.size > 0}
  <div class="active-transfers-panel">
    <h4>Active Transfers</h4>
    {#each Array.from(activeFileTransfers.values()) as transfer}
      <div class="transfer-item">
        <div class="transfer-info">
          <span class="transfer-filename">{transfer.filename}</span>
          <span class="transfer-status">{transfer.direction === 'upload' ? '↑' : '↓'} {transfer.status}</span>
          <button
            class="cancel-transfer-button"
            onclick={() => onCancelTransfer(transfer.transfer_id, transfer.filename)}
            title="Cancel transfer"
            aria-label="Cancel transfer"
          >
            ×
          </button>
        </div>
        {#if transfer.progress !== undefined}
          <div class="progress-bar">
            <div class="progress-fill" style="width: {transfer.progress}%"></div>
          </div>
          <span class="progress-text">{transfer.progress}%</span>
        {/if}
      </div>
    {/each}
  </div>
{/if}

<style>
  /* Image Preview Chip (Phase 2.4: improved UX) */
  .image-preview-chip {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding: 0.5rem;
    margin-bottom: 0.5rem;
    background: #f5f5f5;
    border: 1px solid #ddd;
    border-radius: 8px;
    transition: all 0.2s ease;
  }

  .image-preview-chip:hover {
    background: #ebebeb;
    border-color: #ccc;
  }

  .preview-thumbnail {
    width: 60px;
    height: 60px;
    object-fit: cover;
    border-radius: 4px;
    border: 1px solid #ccc;
  }

  .preview-info {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    min-width: 0; /* Enable text truncation */
  }

  .preview-filename {
    font-size: 0.875rem;
    font-weight: 600;
    color: #333;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .preview-size {
    font-size: 0.75rem;
    color: #666;
  }

  .preview-remove {
    flex-shrink: 0;
    width: 28px;
    height: 28px;
    padding: 0;
    background: #f44336;
    color: white;
    border: none;
    border-radius: 50%;
    font-size: 1.2rem;
    line-height: 1;
    cursor: pointer;
    transition: background 0.2s ease;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .preview-remove:hover {
    background: #d32f2f;
  }

  .preview-remove:active {
    transform: scale(0.95);
  }

  /* Voice Preview Chip (v0.13.0+) */
  .voice-preview-chip {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.5rem;
    margin-bottom: 0.5rem;
    background: #f0f8ff;
    border: 1px solid #b3d9ff;
    border-radius: 8px;
    transition: all 0.2s ease;
  }

  .voice-preview-chip:hover {
    background: #e6f3ff;
    border-color: #99ccff;
  }

  .voice-icon {
    width: 40px;
    height: 40px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 1.5rem;
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    border-radius: 50%;
    flex-shrink: 0;
  }

  .voice-send-button,
  .voice-transcribe-button {
    padding: 0.4rem 0.8rem;
    background: #4CAF50;
    color: white;
    border: none;
    border-radius: 6px;
    font-size: 0.875rem;
    font-weight: 600;
    cursor: pointer;
    transition: background 0.2s ease;
    white-space: nowrap;
  }

  .voice-transcribe-button {
    background: #2196F3;
  }

  .voice-send-button:hover {
    background: #45a049;
  }

  .voice-transcribe-button:hover {
    background: #1976D2;
  }

  .voice-send-button:active,
  .voice-transcribe-button:active {
    transform: scale(0.95);
  }

  /* Active Transfers Panel */
  .active-transfers-panel {
    position: fixed;
    bottom: 20px;
    right: 20px;
    background: #2a2a2a;
    padding: 16px;
    border-radius: 8px;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.3);
    min-width: 300px;
    z-index: 999;
  }

  .active-transfers-panel h4 {
    margin: 0 0 12px 0;
    color: #e0e0e0;
    font-size: 14px;
  }

  .transfer-item {
    margin-bottom: 12px;
    padding-bottom: 12px;
    border-bottom: 1px solid #444;
  }

  .transfer-item:last-child {
    margin-bottom: 0;
    padding-bottom: 0;
    border-bottom: none;
  }

  .transfer-info {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 8px;
  }

  .transfer-filename {
    color: #b0b0b0;
    font-size: 13px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1;
    min-width: 0;
  }

  .transfer-status {
    color: #888;
    font-size: 12px;
    white-space: nowrap;
  }

  .cancel-transfer-button {
    background: transparent;
    border: none;
    color: #888;
    font-size: 20px;
    line-height: 1;
    padding: 0;
    width: 24px;
    height: 24px;
    cursor: pointer;
    border-radius: 4px;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: all 0.2s ease;
    flex-shrink: 0;
  }

  .cancel-transfer-button:hover {
    background: rgba(255, 68, 68, 0.2);
    color: #ff4444;
  }

  .cancel-transfer-button:active {
    transform: scale(0.95);
  }

  .progress-bar {
    width: 100%;
    height: 6px;
    background: #444;
    border-radius: 3px;
    overflow: hidden;
    margin-bottom: 4px;
  }

  .progress-fill {
    height: 100%;
    background: #17a2b8;
    transition: width 0.3s ease;
  }

  .progress-text {
    font-size: 11px;
    color: #888;
  }

  /* Modal styles are inherited from parent global styles (.modal-overlay, .modal-dialog, .accept-button, .reject-button) */
</style>
