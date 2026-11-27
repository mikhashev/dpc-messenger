<!-- FileTransferUI.svelte - Extracted file transfer UI components -->
<!-- Displays image/voice preview, file transfer dialogs, and active transfers panel -->

<script lang="ts">
  import Toast from './Toast.svelte';
  import VoicePlayer from './VoicePlayer.svelte';
  import { alwaysAllowLabel, alwaysAllowLimits, offerQuestion, senderLabel } from '$lib/fileTransferPermission';
  import { speedAndTimeLeft } from '$lib/utils/transferSpeed';
  import { transferRowLabel } from '$lib/utils/transferRowLabel';
  import { formatMegabytes, pendingFileTypeLabel, type PendingFile } from '$lib/utils/pendingAttachment';
  import { voiceSendButton } from '$lib/utils/groupRecording';
  import { getVisionConfig, saveVisionConfig } from '$lib/coreService';
  import { imageMbOr } from '$lib/utils/featureKeys';

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
    // A 1:1 agent chat: the recording is either dictated or sent to the agent as audio
    isAgentChat = false,
    // A group whose only node is this one, with agents: the recording is sent as
    // the group's audio file for them, or dictated (Mike 2026-11-26)
    sendsAudioToGroup = false,
    canSendAudio = true,

    // File offer dialog state
    showFileOfferDialog = false,
    currentFileOffer = null,
    firewallRules = null,
    nameOf,
    groupNameOf,
    onAcceptFile,
    onRejectFile,

    // The file picked with the paperclip, waiting in the composer for Send
    pendingFile = null,
    onClearPendingFile,
    isSendingFile = false,
    filePreparationProgress = null,
    filePreparationCompleted = null,
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
    isAgentChat?: boolean;
    sendsAudioToGroup?: boolean;
    canSendAudio?: boolean;
    showFileOfferDialog?: boolean;
    currentFileOffer?: any;
    firewallRules?: any;
    nameOf?: (nodeId: string) => string | undefined;
    groupNameOf?: (groupId: string) => string | undefined;
    onAcceptFile: (remember?: boolean) => void;
    onRejectFile: () => void;
    pendingFile?: PendingFile | null;
    onClearPendingFile: () => void;
    isSendingFile?: boolean;
    filePreparationProgress?: any;
    filePreparationCompleted?: any;
    onToggleDescribeForAgents?: (value: boolean) => void;
    activeFileTransfers?: Map<string, any>;
    onCancelTransfer: (transferId: string, filename: string) => void;
    showFileOfferToast?: boolean;
    fileOfferToastMessage?: string;
    onDismissToast: () => void;
  } = $props();

  // "Always allow files from this peer" on an asked offer; a new offer starts unchecked
  let rememberSender = $state(false);
  const question = $derived(currentFileOffer ? offerQuestion(currentFileOffer, nameOf) : null);
  const rememberText = $derived(currentFileOffer ? alwaysAllowLabel(currentFileOffer, alwaysAllowLimits(firewallRules)) : '');
  $effect(() => {
    void currentFileOffer?.transfer_id;
    rememberSender = false;
  });

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

  // ── The composer's image cap ([vision] max_image_size_mb, config-parity card order 7) ──
  // Shown beside the image a paste just held, because that is where the cap bites. Read
  // once per pending image; a save applies to every image at once, with one caveat the
  // note says: the gateway's HTTP body limit is fixed when the gateway is built.
  let maxImageMb = $state(5);
  let capLoaded = $state(false);
  let capEditing = $state(false);
  let capDraft = $state(5);
  let capBusy = $state(false);
  let capError = $state('');
  let capSaved = $state(false);

  $effect(() => {
    if (pendingImage && !capLoaded) void loadCap();
  });

  async function loadCap(): Promise<void> {
    capLoaded = true;
    try {
      const r = await getVisionConfig();
      if (r?.status === 'success') maxImageMb = imageMbOr(r.max_image_size_mb, 5);
    } catch { /* the cap line simply keeps the default */ }
  }

  async function saveCap(): Promise<void> {
    capBusy = true;
    capError = '';
    capSaved = false;
    try {
      const r = await saveVisionConfig({ config: { max_image_size_mb: imageMbOr(capDraft, maxImageMb) } });
      if (r?.status === 'success') {
        maxImageMb = imageMbOr(r.max_image_size_mb, maxImageMb);
        capSaved = true;
      } else {
        capError = r?.message || 'Could not save the image size cap';
      }
    } catch (error) {
      capError = `Error: ${error}`;
    } finally {
      capBusy = false;
    }
  }
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
  <div class="image-cap-line">
    <span>Images are checked against a {maxImageMb} MB cap.</span>
    <button
      type="button"
      class="image-cap-change"
      onclick={() => { capDraft = maxImageMb; capSaved = false; capError = ''; capEditing = !capEditing; }}
      aria-expanded={capEditing}
    >Change</button>
  </div>
  {#if capEditing}
    <div class="image-cap-editor">
      <label for="image-cap-mb">Max image size (MB)</label>
      <input id="image-cap-mb" class="image-cap-input" type="number" min="1" step="1" bind:value={capDraft} />
      <button type="button" class="image-cap-save" onclick={saveCap} disabled={capBusy}>
        {capBusy ? 'Saving…' : 'Save'}
      </button>
      {#if capError}
        <span class="image-cap-error" role="alert">{capError}</span>
      {:else if capSaved}
        <span class="image-cap-saved" role="status">Saved.</span>
      {/if}
      <span class="image-cap-note">
        Checked for every image at once; the gateway's HTTP body limit is fixed when the
        gateway is built and follows the new value only after the next backend start.
      </span>
    </div>
  {/if}
  {#if isGroupChat}
    <label style="display: flex; align-items: center; gap: 8px; margin: 6px 4px 0; font-size: 13px; cursor: pointer;">
      <input type="checkbox" checked={describeForAgents ?? false} onchange={(e) => onToggleDescribeForAgents?.((e.currentTarget as HTMLInputElement).checked)} />
      <span>Describe for agents (VL) — agents receive a text description of the image</span>
    </label>
  {/if}
{/if}

<!-- File Chip: a picked file waits here with the words typed below it; Send sends both as one message -->
{#if pendingFile}
  <div class="image-preview-chip">
    <div class="file-chip-icon" aria-hidden="true">{pendingFile.mimeType.startsWith('audio/') ? '🎵' : '📄'}</div>
    <div class="preview-info">
      <span class="preview-filename" title={pendingFile.filePath}>{pendingFile.fileName}</span>
      <span class="preview-size">
        {formatMegabytes(pendingFile.sizeBytes)} • {pendingFileTypeLabel(pendingFile)}
        {#if isSendingFile}
          •
          {#if filePreparationCompleted}
            Sending...
          {:else if filePreparationProgress?.phase === 'hashing_file'}
            Computing file hash: {filePreparationProgress.percent}%
          {:else if filePreparationProgress?.phase === 'computing_chunks'}
            Computing chunk hashes: {filePreparationProgress.percent}%
          {:else if filePreparationProgress}
            Preparing file: {filePreparationProgress.percent}%
          {:else}
            Preparing...
          {/if}
        {/if}
      </span>
    </div>
    <button class="preview-remove" onclick={onClearPendingFile} disabled={isSendingFile} aria-label="Remove file">✕</button>
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
    {#if onTranscribeVoiceMessage}
      <button
        class="voice-transcribe-button"
        onclick={onTranscribeVoiceMessage}
        disabled={isTranscribing}
        title={isTranscribing
          ? "Loading Whisper model…"
          : (isLocalAIChat
            ? "Transcribe and send to AI"
            : "Put the words in the message box — nothing is sent, and you can edit them first")}
      >
        {#if isTranscribing}
          <span class="transcribe-spinner" aria-hidden="true"></span> Loading…
        {:else if isAgentChat}
          📝 Dictate
        {:else if isLocalAIChat}
          📝 Send
        {:else}
          📝 To input box
        {/if}
      </button>
    {/if}
    {#if canSendAudio}
      <button
        class="voice-send-button"
        onclick={onSendVoiceMessage}
        disabled={(isAgentChat || sendsAudioToGroup) && isTranscribing}
        title={voiceSendButton({ isAgentChat, sendsAudioToGroup }).title}
      >
        {voiceSendButton({ isAgentChat, sendsAudioToGroup }).label}
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
      <h3>{question ? question.title : 'Incoming File'}</h3>
      {#if question}
        <p class="offer-question">{question.detail}</p>
      {/if}
      <p><strong>File:</strong> {currentFileOffer.filename}</p>
      <p><strong>Size:</strong> {(currentFileOffer.size_bytes / 1024 / 1024).toFixed(2)} MB</p>
      <p><strong>From:</strong> {senderLabel(currentFileOffer)}</p>
      {#if currentFileOffer.text}
        <p class="offer-text"><strong>Message:</strong> {currentFileOffer.text}</p>
      {/if}
      {#if question}
        <label class="remember-sender">
          <input type="checkbox" bind:checked={rememberSender} />
          {rememberText}
        </label>
      {/if}
      <div class="modal-buttons">
        <button class="accept-button" onclick={() => onAcceptFile(question ? rememberSender : false)}>Accept</button>
        <button class="reject-button" onclick={onRejectFile}>Reject</button>
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
        <div class="transfer-party">{transferRowLabel(transfer, nameOf, groupNameOf)}</div>
        {#if transfer.progress !== undefined}
          {@const rate = speedAndTimeLeft(transfer)}
          <div class="progress-bar">
            <div class="progress-fill" style="width: {transfer.progress}%"></div>
          </div>
          <span class="progress-text">{transfer.progress}%{rate ? ` · ${rate}` : ''}</span>
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

  /* The image cap line under the chip ([vision] max_image_size_mb, config-parity card
     order 7): one sentence and a small editor, matching the chip's own shapes. */
  .image-cap-line {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin: -0.15rem 0 0.5rem;
    font-size: 0.8rem;
    color: #666;
  }

  .image-cap-change {
    padding: 0.1rem 0.6rem;
    background: transparent;
    border: 1px solid #ccc;
    border-radius: 4px;
    color: #1976d2;
    font-size: 0.8rem;
    cursor: pointer;
  }

  .image-cap-change:hover {
    background: #ebebeb;
  }

  .image-cap-editor {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
    margin: -0.15rem 0 0.5rem;
    font-size: 0.85rem;
    color: #333;
  }

  .image-cap-input {
    width: 5.5rem;
    padding: 0.25rem 0.4rem;
    border: 1px solid #ddd;
    border-radius: 4px;
    font-size: 0.9rem;
  }

  .image-cap-save {
    padding: 0.25rem 0.75rem;
    background: #1976d2;
    color: white;
    border: none;
    border-radius: 4px;
    font-size: 0.85rem;
    cursor: pointer;
  }

  .image-cap-save:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .image-cap-error {
    color: #dc3545;
  }

  .image-cap-saved {
    color: #155724;
  }

  .image-cap-note {
    flex-basis: 100%;
    font-size: 0.75rem;
    color: #888;
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

  .transfer-party {
    color: #888;
    font-size: 12px;
    margin: -4px 0 8px;
    overflow-wrap: anywhere;
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

  .offer-question {
    font-size: 13px;
    line-height: 1.4;
  }

  .offer-text {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  .file-chip-icon {
    width: 40px;
    height: 40px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 1.5rem;
    flex-shrink: 0;
  }

  .preview-remove:disabled {
    background: #9e9e9e;
    cursor: not-allowed;
  }

  .remember-sender {
    display: flex;
    gap: 8px;
    align-items: flex-start;
    margin: 10px 0;
    font-size: 13px;
    cursor: pointer;
  }
</style>
