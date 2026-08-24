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
    isGroupChat = false,
    describeForAgents = false,

    // Voice preview state (v0.13.0+)
    voicePreview = null,
    onClearVoicePreview,
    onSendVoiceMessage,
    onTranscribeVoiceMessage,
    isTranscribing = false,
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
    onToggleDescribeForAgents,

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
    isGroupChat?: boolean;
    describeForAgents?: boolean;
    voicePreview?: { blob: Blob; duration: number } | null;
    onClearVoicePreview: () => void;
    onSendVoiceMessage: () => void;
    onTranscribeVoiceMessage?: () => Promise<void>;
    isTranscribing?: boolean;
    isLocalAIChat?: boolean;
    showFileOfferDialog?: boolean;
    currentFileOffer?: any;
    onAcceptFile: () => void;
    onRejectFile: () => void;
    showSendFileDialog?: boolean;
    pendingFileSend?: {
      filePath: string;
      fileName: string;
      recipientId: string;
      recipientName: string;
      imageData?: { dataUrl: string; filename: string; sizeBytes: number };
      caption?: string;
    } | null;
    isSendingFile?: boolean;
    filePreparationStarted?: any;
    filePreparationProgress?: any;
    filePreparationCompleted?: any;
    onConfirmSendFile: () => void;
    onCancelSendFile: () => void;
    onToggleDescribeForAgents?: (value: boolean) => void;
    activeFileTransfers?: Map<string, any>;
    onCancelTransfer: (transferId: string, filename: string) => void;
    showFileOfferToast?: boolean;
    fileOfferToastMessage?: string;
    onDismissToast: () => void;
  } = $props();

  // ── Active-transfers panel: movable and foldable ──────────────────────
  // It is fixed to the bottom-right corner and sits on top of whatever is
  // there, for as long as the transfer lasts — on a large file that is
  // minutes of covering the thing being worked on. Both the position and
  // the folded state are remembered, so it does not jump back on the next
  // transfer.
  const PANEL_STATE_KEY = 'dpc.activeTransfersPanel';
  const PANEL_MARGIN = 8;   // never let it be dragged fully off-screen
  const NUDGE_PX = 16;      // arrow-key step when the header has focus

  let panelCollapsed = $state(false);
  let panelPos = $state<{ x: number; y: number } | null>(null);  // null = default corner
  let panelEl = $state<HTMLDivElement | null>(null);
  let dragGrab: { dx: number; dy: number } | null = null;

  const panelSummary = $derived.by(() => {
    const items = Array.from(activeFileTransfers.values()) as any[];
    if (!items.length) return '';
    const withProgress = items.filter((t) => typeof t.progress === 'number');
    if (!withProgress.length) return `${items.length}`;
    const avg = Math.round(
      withProgress.reduce((sum, t) => sum + t.progress, 0) / withProgress.length
    );
    return `${items.length} · ${avg}%`;
  });

  const panelStyle = $derived(
    panelPos
      ? `left:${panelPos.x}px; top:${panelPos.y}px; right:auto; bottom:auto;`
      : ''
  );

  function clampToViewport(x: number, y: number) {
    const w = panelEl?.offsetWidth ?? 300;
    const h = panelEl?.offsetHeight ?? 80;
    return {
      x: Math.min(Math.max(x, PANEL_MARGIN), Math.max(PANEL_MARGIN, window.innerWidth - w - PANEL_MARGIN)),
      y: Math.min(Math.max(y, PANEL_MARGIN), Math.max(PANEL_MARGIN, window.innerHeight - h - PANEL_MARGIN)),
    };
  }

  function savePanelState() {
    try {
      localStorage.setItem(
        PANEL_STATE_KEY,
        JSON.stringify({ collapsed: panelCollapsed, pos: panelPos })
      );
    } catch {
      // Private windows and blocked site data throw here; a panel that
      // forgets where it was is better than one that fails to draw.
    }
  }

  function loadPanelState() {
    try {
      const raw = localStorage.getItem(PANEL_STATE_KEY);
      if (!raw) return;
      const saved = JSON.parse(raw);
      if (typeof saved?.collapsed === 'boolean') panelCollapsed = saved.collapsed;
      if (Number.isFinite(saved?.pos?.x) && Number.isFinite(saved?.pos?.y)) {
        panelPos = { x: saved.pos.x, y: saved.pos.y };
      }
    } catch {
      // Corrupt entry — start from the default corner rather than break.
    }
  }

  function togglePanelCollapsed() {
    panelCollapsed = !panelCollapsed;
    savePanelState();
  }

  function onPanelPointerDown(event: PointerEvent) {
    // The fold button lives in the same header; a click on it is not a drag.
    if ((event.target as HTMLElement)?.closest('button')) return;
    if (!panelEl) return;
    const rect = panelEl.getBoundingClientRect();
    dragGrab = { dx: event.clientX - rect.left, dy: event.clientY - rect.top };
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
    event.preventDefault();
  }

  function onPanelPointerMove(event: PointerEvent) {
    if (!dragGrab) return;
    panelPos = clampToViewport(event.clientX - dragGrab.dx, event.clientY - dragGrab.dy);
  }

  function onPanelPointerUp(event: PointerEvent) {
    if (!dragGrab) return;
    dragGrab = null;
    try {
      (event.currentTarget as HTMLElement).releasePointerCapture(event.pointerId);
    } catch {
      // Capture may already be gone (pointercancel) — nothing to release.
    }
    savePanelState();
  }

  function onPanelKeydown(event: KeyboardEvent) {
    const step: Record<string, [number, number]> = {
      ArrowLeft: [-NUDGE_PX, 0], ArrowRight: [NUDGE_PX, 0],
      ArrowUp: [0, -NUDGE_PX], ArrowDown: [0, NUDGE_PX],
    };
    const delta = step[event.key];
    if (delta) {
      const rect = panelEl?.getBoundingClientRect();
      const base = panelPos ?? { x: rect?.left ?? 0, y: rect?.top ?? 0 };
      panelPos = clampToViewport(base.x + delta[0], base.y + delta[1]);
      savePanelState();
      event.preventDefault();
    } else if (event.key === 'Enter' || event.key === ' ') {
      togglePanelCollapsed();
      event.preventDefault();
    }
  }

  $effect(() => {
    loadPanelState();
    const onResize = () => {
      if (panelPos) panelPos = clampToViewport(panelPos.x, panelPos.y);
    };
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  });
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
  {#if isGroupChat}
    <label style="display: flex; align-items: center; gap: 8px; margin: 6px 4px 0; font-size: 13px; cursor: pointer;">
      <input type="checkbox" checked={describeForAgents ?? false} onchange={(e) => onToggleDescribeForAgents?.((e.currentTarget as HTMLInputElement).checked)} />
      <span>Describe for agents (VL) — agents receive a text description of the image</span>
    </label>
  {/if}
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
        disabled={isTranscribing}
        title={isTranscribing ? "Loading Whisper model…" : "Transcribe and send to AI"}
      >
        {#if isTranscribing}
          <span class="transcribe-spinner" aria-hidden="true"></span> Loading…
        {:else}
          📝 Send
        {/if}
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
  <div class="modal-overlay" role="presentation" onkeydown={(e) => e.key === 'Escape' && onRejectFile()}>
    <div class="modal-dialog" role="dialog" aria-modal="true" tabindex="-1">
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
  <div class="modal-overlay" role="presentation" onkeydown={(e) => e.key === 'Escape' && onCancelSendFile()}>
    <div class="modal-dialog" role="dialog" aria-modal="true" tabindex="-1">
      <h3>{pendingFileSend.imageData ? 'Send Screenshot' : 'Send File'}</h3>
      <p><strong>{pendingFileSend.imageData ? 'Image' : 'File'}:</strong> {pendingFileSend.fileName}</p>
      <p><strong>To:</strong> {pendingFileSend.recipientName}</p>

      {#if pendingFileSend.imageData}
        <p style="margin-top: 10px; font-size: 13px;">
          <strong>Size:</strong> {(pendingFileSend.imageData.sizeBytes / (1024 * 1024)).toFixed(2)} MB
        </p>
        <!-- Show thumbnail for screenshots -->
        <div style="margin-top: 10px; text-align: center;">
          <img
            src={pendingFileSend.imageData.dataUrl}
            alt="Screenshot preview"
            style="max-width: 100%; max-height: 150px; border-radius: 4px; border: 1px solid #ddd;"
          />
        </div>
      {:else if filePreparationStarted && isSendingFile}
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
  <div
    class="active-transfers-panel"
    class:collapsed={panelCollapsed}
    style={panelStyle}
    bind:this={panelEl}
  >
    <!-- svelte-ignore a11y_no_noninteractive_element_to_interactive_role -->
    <div
      class="panel-header"
      role="toolbar"
      tabindex="0"
      aria-label="Active transfers — drag to move, arrow keys to nudge"
      onpointerdown={onPanelPointerDown}
      onpointermove={onPanelPointerMove}
      onpointerup={onPanelPointerUp}
      onpointercancel={onPanelPointerUp}
      onkeydown={onPanelKeydown}
    >
      <h4>Active Transfers{panelCollapsed && panelSummary ? ` · ${panelSummary}` : ''}</h4>
      <button
        class="panel-toggle"
        onclick={togglePanelCollapsed}
        title={panelCollapsed ? 'Expand' : 'Collapse'}
        aria-label={panelCollapsed ? 'Expand active transfers' : 'Collapse active transfers'}
        aria-expanded={!panelCollapsed}
      >
        {panelCollapsed ? '▲' : '▼'}
      </button>
    </div>
    {#each panelCollapsed ? [] : Array.from(activeFileTransfers.values()) as transfer}
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

  .voice-transcribe-button:disabled {
    background: #78909c;
    cursor: not-allowed;
    opacity: 0.8;
  }

  .transcribe-spinner {
    display: inline-block;
    width: 12px;
    height: 12px;
    border: 2px solid rgba(255,255,255,0.4);
    border-top-color: white;
    border-radius: 50%;
    animation: spin 0.7s linear infinite;
    vertical-align: middle;
  }

  @keyframes spin {
    to { transform: rotate(360deg); }
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

  .active-transfers-panel.collapsed {
    min-width: 0;
    padding-bottom: 12px;
  }

  .panel-header {
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 0 0 12px 0;
    cursor: move;
    /* A drag that starts on the header must not select the title text. */
    user-select: none;
    touch-action: none;
  }

  .panel-header:focus-visible {
    outline: 2px solid #6ea8fe;
    outline-offset: 2px;
    border-radius: 4px;
  }

  .active-transfers-panel.collapsed .panel-header {
    margin-bottom: 0;
  }

  .active-transfers-panel h4 {
    margin: 0;
    color: #e0e0e0;
    font-size: 14px;
    flex: 1;
    white-space: nowrap;
  }

  .panel-toggle {
    background: transparent;
    border: none;
    color: #b0b0b0;
    font-size: 12px;
    line-height: 1;
    padding: 4px 6px;
    border-radius: 4px;
    cursor: pointer;
  }

  .panel-toggle:hover {
    background: #3a3a3a;
    color: #e0e0e0;
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

  .modal-dialog {
    max-height: calc(100vh - 2rem);
    overflow-y: auto;
  }
</style>
