<script lang="ts">
  /**
   * ImageMessage Component - Display images in chat (Phase 2.5: Screenshot + Vision)
   *
   * Features:
   * - Thumbnail display with click to expand
   * - Dimensions badge
   * - Full image modal
   * - AI Chat: Show vision analysis result (if available)
   * - P2P Chat: Just display image (no vision analysis)
   */

  import { convertFileSrc } from '@tauri-apps/api/core';

  interface ImageAttachment {
    type: 'image' | 'file' | 'voice';  // Accept union type from parent (v0.13.0: added voice)
    filename: string;
    thumbnail?: string;  // Base64 data URL (optional for backward compat)
    file_path?: string;  // Full-size image file path (for P2P file transfers)
    dimensions?: { width: number; height: number };
    vision_analyzed?: boolean;  // AI chat only
    vision_result?: string;  // AI chat only
    size_bytes?: number;
    size_mb?: number;
    mime_type?: string;
    transfer_id?: string;
    hash?: string;
    status?: string;
    voice_metadata?: {  // v0.13.0: Voice metadata (not used in ImageMessage but part of union)
      duration_seconds: number;
      sample_rate: number;
      channels: number;
      codec: string;
      recorded_at: string;
    };
  }

  interface Props {
    attachment: ImageAttachment;
    conversationId: string;
  }

  let { attachment, conversationId }: Props = $props();

  // Modal state
  let showFullImage = $state(false);

  function openFullImage() {
    showFullImage = true;
  }

  function closeFullImage() {
    showFullImage = false;
  }

  // Check if this is an AI conversation
  const isAIChat = $derived(conversationId === 'local_ai' || conversationId.startsWith('ai_'));

  // Get full-size image source (prefer file_path, fall back to thumbnail)
  const fullImageSrc = $derived(() => {
    if (attachment.file_path) {
      // Convert file path to Tauri asset URL for full-size display
      return convertFileSrc(attachment.file_path);
    }
    // Fall back to thumbnail (for AI chat or if file was deleted)
    return attachment.thumbnail || '';
  });
</script>

<div class="image-message">
  {#if attachment.thumbnail}
    <!-- Thumbnail with click to expand -->
    <div class="image-thumbnail-container">
      <button
        class="thumbnail-button"
        onclick={openFullImage}
        aria-label="Open full image: {attachment.filename}"
      >
        <img
          src={attachment.thumbnail}
          alt={attachment.filename}
          class="image-thumbnail"
        />

        <!-- Dimensions badge (if available) -->
        {#if attachment.dimensions}
          <div class="dimensions-badge">
            {attachment.dimensions.width}×{attachment.dimensions.height}
          </div>
        {/if}
      </button>
    </div>
  {/if}

  <!-- Filename and size -->
  <div class="image-info">
    <span class="filename">{attachment.filename}</span>
    {#if attachment.size_mb}
      <span class="file-size">({attachment.size_mb} MB)</span>
    {/if}
  </div>

  <!-- Vision analysis result (AI chat only) -->
  {#if isAIChat && attachment.vision_analyzed && attachment.vision_result}
    <div class="vision-result">
      <div class="vision-result-header">
        <span class="vision-icon">🔍</span>
        <span class="vision-label">Vision Analysis:</span>
      </div>
      <div class="vision-text">
        {attachment.vision_result}
      </div>
    </div>
  {/if}
</div>

<!-- Full Image Modal -->
{#if showFullImage && (attachment.thumbnail || attachment.file_path)}
  <div
    class="full-image-overlay"
    role="presentation"
    onclick={closeFullImage}
    onkeydown={(e) => e.key === 'Escape' && closeFullImage()}
  >
    <div class="full-image-container" role="dialog" aria-modal="true" tabindex="-1" onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()}>
      <button class="close-button" onclick={closeFullImage} aria-label="Close full image">
        ✕
      </button>
      <img
        src={fullImageSrc()}
        alt={attachment.filename}
        class="full-image"
      />
      <div class="full-image-info">
        <span class="full-image-filename">{attachment.filename}</span>
        {#if attachment.dimensions}
          <span class="full-image-dimensions">
            {attachment.dimensions.width}×{attachment.dimensions.height}
          </span>
        {/if}
      </div>
    </div>
  </div>
{/if}

<style>
  /* Image Message Container */
  .image-message {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    max-width: 500px;
  }

  /* Thumbnail Container */
  .image-thumbnail-container {
    position: relative;
    display: inline-block;
  }

  .thumbnail-button {
    position: relative;
    padding: 0;
    border: none;
    background: none;
    cursor: pointer;
    transition: transform 0.2s ease, box-shadow 0.2s ease;
    border-radius: 8px;
    overflow: hidden;
  }

  .thumbnail-button:hover {
    transform: scale(1.02);
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
  }

  .image-thumbnail {
    max-width: 300px;
    max-height: 300px;
    border-radius: 8px;
    display: block;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
  }

  /* Dimensions Badge */
  .dimensions-badge {
    position: absolute;
    bottom: 8px;
    right: 8px;
    background: rgba(0, 0, 0, 0.7);
    color: white;
    padding: 4px 8px;
    border-radius: 4px;
    font-size: 0.75rem;
    font-weight: 600;
    pointer-events: none;
  }

  /* Image Info */
  .image-info {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    font-size: 0.875rem;
    color: #666;
  }

  .filename {
    font-weight: 600;
    color: #333;
  }

  .file-size {
    color: #999;
  }

  /* Vision Result */
  .vision-result {
    margin-top: 0.5rem;
    padding: 1rem;
    background: #f0f7ff;
    border-left: 4px solid #2196F3;
    border-radius: 4px;
  }

  .vision-result-header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.5rem;
    font-weight: 600;
    color: #1976D2;
  }

  .vision-icon {
    font-size: 1.2rem;
  }

  .vision-label {
    font-size: 0.875rem;
  }

  .vision-text {
    font-size: 0.875rem;
    line-height: 1.5;
    color: #333;
    white-space: pre-wrap;
  }

  /* Full Image Modal */
  .full-image-overlay {
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    background: rgba(0, 0, 0, 0.9);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 10000;
    cursor: pointer;
  }

  .full-image-container {
    position: relative;
    max-width: 90vw;
    max-height: 90vh;
    cursor: default;
  }

  .close-button {
    position: absolute;
    top: -40px;
    right: 0;
    background: rgba(255, 255, 255, 0.2);
    border: none;
    color: white;
    font-size: 2rem;
    width: 40px;
    height: 40px;
    border-radius: 50%;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: background 0.2s ease;
  }

  .close-button:hover {
    background: rgba(255, 255, 255, 0.3);
  }

  .full-image {
    max-width: 90vw;
    max-height: 80vh;
    border-radius: 8px;
    box-shadow: 0 10px 40px rgba(0, 0, 0, 0.5);
  }

  .full-image-info {
    margin-top: 1rem;
    text-align: center;
    color: white;
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }

  .full-image-filename {
    font-size: 1rem;
    font-weight: 600;
  }

  .full-image-dimensions {
    font-size: 0.875rem;
    color: rgba(255, 255, 255, 0.7);
  }
</style>
