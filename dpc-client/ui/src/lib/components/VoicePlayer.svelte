<script lang="ts">
  import { onMount } from 'svelte';

  interface VoicePlayerProps {
    audioUrl: string;
    filePath?: string;  // Local file path (for Tauri desktop app)
    duration: number;
    timestamp?: string;
    compact?: boolean;
    transcription?: {  // v0.13.2+ auto-transcription
      text: string;
      provider: string;
      transcriber_node_id?: string;
      confidence?: number;
      language?: string;
      remote_provider_node_id?: string;
    };
    showTranscriberName?: boolean;  // Whether to show who transcribed (v0.13.2+)
  }

  let { audioUrl, filePath, duration, timestamp, compact = false, transcription, showTranscriberName = false }: VoicePlayerProps = $props();

  // Debug props immediately
  $effect(() => {
    console.error(`[VoicePlayer] PROPS RECEIVED: audioUrl="${audioUrl}", filePath="${filePath}"`);
  });

  let audioElement: HTMLAudioElement;
  let isPlaying = $state(false);
  let currentTime = $state(0);
  let playbackRate = $state(1.0);
  let volume = $state(1.0);
  let actualAudioUrl = $state(audioUrl);  // Reactive state for converted URL

  // Convert file path to Tauri asset URL (v0.13.0+)
  // Handles timing issues where Tauri API loads asynchronously
  function convertTauriPath(path: string): string | null {
    console.error(`[VoicePlayer] Attempting Tauri conversion for: "${path}"`);

    // Check if Tauri API is available
    if (typeof window === 'undefined') {
      console.error('[VoicePlayer] Window is undefined');
      return null;
    }

    const tauri = (window as any).__TAURI__;
    console.error(`[VoicePlayer] window.__TAURI__ = ${typeof tauri}`);

    if (!tauri) {
      console.error('[VoicePlayer] ❌ Tauri API not available');
      return null;
    }

    if (!tauri.core || !tauri.core.convertFileSrc) {
      console.error('[VoicePlayer] ❌ Tauri core.convertFileSrc not found');
      return null;
    }

    try {
      const converted = tauri.core.convertFileSrc(path);
      console.error(`[VoicePlayer] ✅ CONVERTED: ${path} -> ${converted}`);
      return converted;
    } catch (err) {
      console.error('[VoicePlayer] ❌ CONVERSION FAILED:', err);
      return null;
    }
  }

  // Update actualAudioUrl when filePath or audioUrl changes
  $effect(() => {
    console.error(`[VoicePlayer] Effect triggered: filePath="${filePath}", audioUrl="${audioUrl}"`);

    // Strip file:// protocol if present (Windows Tauri adds this incorrectly)
    const cleanPath = filePath ? filePath.replace(/^file:\/\/\//, '').replace(/^file:\/\//, '') : null;

    if (!cleanPath) {
      console.error(`[VoicePlayer] No filePath, using audioUrl: "${audioUrl}"`);
      actualAudioUrl = audioUrl;
      return;
    }

    // Try immediate conversion
    const converted = convertTauriPath(cleanPath);
    if (converted) {
      actualAudioUrl = converted;
      return;
    }

    // Tauri API not ready yet, retry after short delay
    console.error('[VoicePlayer] Tauri API not ready, retrying in 100ms...');
    setTimeout(() => {
      const retryConverted = convertTauriPath(cleanPath);
      if (retryConverted) {
        actualAudioUrl = retryConverted;
      } else {
        console.error(`[VoicePlayer] ⚠️ Tauri conversion failed after retry, using original: "${audioUrl}"`);
        actualAudioUrl = audioUrl;
      }
    }, 100);
  });

  function togglePlay() {
    if (!audioElement) return;

    if (isPlaying) {
      audioElement.pause();
    } else {
      audioElement.play();
    }
  }

  function handleSeek(e: Event) {
    const target = e.target as HTMLInputElement;
    if (audioElement) {
      audioElement.currentTime = parseFloat(target.value);
    }
  }

  function cyclePlaybackRate() {
    const rates = [1.0, 1.5, 2.0];
    const currentIndex = rates.indexOf(playbackRate);
    playbackRate = rates[(currentIndex + 1) % rates.length];
    if (audioElement) {
      audioElement.playbackRate = playbackRate;
    }
  }

  function handleVolumeChange(e: Event) {
    const target = e.target as HTMLInputElement;
    volume = parseFloat(target.value);
    if (audioElement) {
      audioElement.volume = volume;
    }
  }

  function formatTime(seconds: number): string {
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${mins}:${secs.toString().padStart(2, '0')}`;
  }

  onMount(() => {
    const handleTimeUpdate = () => {
      currentTime = audioElement.currentTime;
    };

    const handlePlay = () => {
      isPlaying = true;
    };

    const handlePause = () => {
      isPlaying = false;
    };

    const handleEnded = () => {
      isPlaying = false;
      currentTime = 0;
    };

    // Create audio element with converted URL
    audioElement = new Audio(actualAudioUrl);
    audioElement.volume = volume;
    audioElement.addEventListener('timeupdate', handleTimeUpdate);
    audioElement.addEventListener('play', handlePlay);
    audioElement.addEventListener('pause', handlePause);
    audioElement.addEventListener('ended', handleEnded);

    return () => {
      if (audioElement) {
        audioElement.removeEventListener('timeupdate', handleTimeUpdate);
        audioElement.removeEventListener('play', handlePlay);
        audioElement.removeEventListener('pause', handlePause);
        audioElement.removeEventListener('ended', handleEnded);
        audioElement.pause();
        audioElement = null as any;
      }
    };
  });
</script>

<div class="voice-player" class:compact={compact}>
  <button
    class="play-button"
    onmousedown={togglePlay}
    title={isPlaying ? 'Pause' : 'Play'}
  >
    {#if isPlaying}
      <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
        <path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/>
      </svg>
    {:else}
      <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
        <path d="M8 5v14l11-7z"/>
      </svg>
    {/if}
  </button>

  {#if !compact}
    <div class="progress-container">
      <span class="time-current">{formatTime(currentTime)}</span>
      <input
        type="range"
        min="0"
        max={duration}
        value={currentTime}
        oninput={handleSeek}
        class="progress-bar"
      />
      <span class="time-total">{formatTime(duration)}</span>
    </div>

    <button
      class="speed-button"
      onmousedown={cyclePlaybackRate}
      title="Playback speed"
    >
      {playbackRate}x
    </button>

    <div class="volume-container">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" class="volume-icon">
        <path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02z"/>
      </svg>
      <input
        type="range"
        min="0"
        max="1"
        step="0.1"
        value={volume}
        oninput={handleVolumeChange}
        class="volume-slider"
      />
    </div>
  {:else}
    <span class="compact-duration">{formatTime(duration)}</span>
  {/if}

  {#if !compact}
    <a
      href={audioUrl}
      download={timestamp ? `voice_${timestamp}.webm` : 'voice.webm'}
      class="download-button"
      title="Download voice message"
    >
      <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
        <path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/>
      </svg>
    </a>
  {/if}
</div>

<!-- Transcription display (v0.13.2+ auto-transcription) -->
{#if transcription && !compact}
  <div class="transcription-container">
    <div class="transcription-header">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" class="transcription-icon">
        <path d="M21 6.5l-9 9-3.5-3.5-1.5 1.5 5 5L22.5 8z"/>
      </svg>
      <span class="transcription-label">Transcription</span>

      {#if showTranscriberName && transcription.transcriber_node_id}
        <span class="transcription-attribution">
          by {transcription.transcriber_node_id.substring(9, 20)}... using {transcription.provider}
          {#if transcription.remote_provider_node_id}
            (compute: {transcription.remote_provider_node_id.substring(9, 20)}...)
          {/if}
        </span>
      {/if}

      {#if transcription.confidence && transcription.confidence < 0.8}
        <span class="transcription-warning" title="Low confidence transcription">⚠️</span>
      {/if}
    </div>

    <div class="transcription-text">
      {transcription.text}
    </div>
  </div>
{/if}

<style>
  .voice-player {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 10px;
    background: #f5f5f5;
    border-radius: 8px;
    min-width: 280px;
  }

  .play-button {
    background: none;
    border: none;
    color: #333;
    cursor: pointer;
    width: 32px;
    height: 32px;
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: 50%;
    transition: background 0.2s ease;
    flex-shrink: 0;
  }

  .play-button:hover {
    background: #e0e0e0;
  }

  .progress-container {
    flex: 1;
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }

  .progress-bar {
    flex: 1;
    height: 4px;
    cursor: pointer;
    border-radius: 2px;
    appearance: none;
    background: #ddd;
    min-width: 60px;
  }

  .progress-bar::-webkit-slider-thumb {
    appearance: none;
    width: 12px;
    height: 12px;
    border-radius: 50%;
    background: #4CAF50;
    cursor: pointer;
  }

  .progress-bar::-moz-range-thumb {
    width: 12px;
    height: 12px;
    border-radius: 50%;
    background: #4CAF50;
    cursor: pointer;
    border: none;
  }

  .time-current, .time-total {
    font-size: 11px;
    font-family: monospace;
    color: #666;
    white-space: nowrap;
  }

  .speed-button {
    background: none;
    border: 1px solid #ddd;
    border-radius: 4px;
    cursor: pointer;
    padding: 2px 6px;
    font-size: 11px;
    font-weight: 600;
    color: #555;
    transition: all 0.2s ease;
    white-space: nowrap;
  }

  .speed-button:hover {
    background: #e0e0e0;
    border-color: #ccc;
  }

  .volume-container {
    display: flex;
    align-items: center;
    gap: 4px;
  }

  .volume-icon {
    color: #666;
    flex-shrink: 0;
  }

  .volume-slider {
    width: 50px;
    height: 3px;
    cursor: pointer;
    border-radius: 2px;
    appearance: none;
    background: #ddd;
  }

  .volume-slider::-webkit-slider-thumb {
    appearance: none;
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: #666;
    cursor: pointer;
  }

  .volume-slider::-moz-range-thumb {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: #666;
    cursor: pointer;
    border: none;
  }

  .download-button {
    background: none;
    border: none;
    color: #666;
    cursor: pointer;
    padding: 4px;
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: 4px;
    transition: all 0.2s ease;
    text-decoration: none;
    flex-shrink: 0;
  }

  .download-button:hover {
    background: #e0e0e0;
    color: #333;
  }

  /* Compact mode for preview chip */
  .voice-player.compact {
    min-width: unset;
    padding: 4px 6px;
    background: transparent;
    gap: 6px;
  }

  .voice-player.compact .play-button {
    width: 28px;
    height: 28px;
    background: rgba(255, 255, 255, 0.9);
    border-radius: 50%;
  }

  .voice-player.compact .play-button:hover {
    background: white;
  }

  .compact-duration {
    font-size: 11px;
    font-family: monospace;
    color: #666;
    white-space: nowrap;
  }

  /* Transcription styles (v0.13.2+ auto-transcription) */
  .transcription-container {
    margin-top: 8px;
    padding: 8px 12px;
    background: #f9f9f9;
    border-left: 3px solid #4CAF50;
    border-radius: 4px;
    font-size: 13px;
  }

  .transcription-header {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-bottom: 6px;
    color: #666;
    font-size: 11px;
    font-weight: 600;
  }

  .transcription-icon {
    color: #4CAF50;
    flex-shrink: 0;
  }

  .transcription-label {
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .transcription-attribution {
    margin-left: auto;
    color: #999;
    font-size: 10px;
    font-style: italic;
  }

  .transcription-warning {
    margin-left: 4px;
    font-size: 12px;
    cursor: help;
  }

  .transcription-text {
    color: #333;
    line-height: 1.5;
    white-space: pre-wrap;
    word-wrap: break-word;
  }
</style>
