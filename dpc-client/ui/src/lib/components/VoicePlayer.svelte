<script lang="ts">
  import { onMount } from 'svelte';

  // Detect if running in Tauri (check in onMount when window is ready)
  let isTauri = $state(false);
  let convertFileSrc = $state<((filePath: string, protocol?: string) => string) | undefined>(undefined);

  onMount(() => {
    // Check for Tauri environment (official method for Tauri 2.x)
    isTauri = typeof window !== 'undefined' && (
      (window as any).isTauri === true ||  // Tauri 2.x official detection
      !!(window as any).__TAURI__           // Fallback for older versions
    );
    console.log(`[VoicePlayer] Environment detected: ${isTauri ? 'Tauri' : 'Browser'}`);

    // Load convertFileSrc if in Tauri
    if (isTauri) {
      import('@tauri-apps/api/core').then((module) => {
        convertFileSrc = module.convertFileSrc;
        console.log('[VoicePlayer] Tauri convertFileSrc loaded');
      }).catch((err) => {
        console.error('[VoicePlayer] Failed to load Tauri API:', err);
      });
    }
  });

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
    console.log(`[VoicePlayer] PROPS RECEIVED: audioUrl="${audioUrl}", filePath="${filePath}", isTauri=${isTauri}`);
  });

  let audioElement: HTMLAudioElement;
  let isPlaying = $state(false);
  let currentTime = $state(0);
  let playbackRate = $state(1.0);
  let volume = $state(1.0);
  let actualAudioUrl = $state(audioUrl);  // Reactive state for converted URL

  // Update actualAudioUrl when filePath or audioUrl changes
  $effect(() => {
    console.log(`[VoicePlayer] Effect triggered: filePath="${filePath}", audioUrl="${audioUrl}", isTauri=${isTauri}`);

    // If audioUrl is already a blob URL, use it directly (just-recorded messages)
    if (audioUrl?.startsWith('blob:')) {
      console.log('[VoicePlayer] Using blob URL:', audioUrl);
      actualAudioUrl = audioUrl;
      return;
    }

    // Strip file:// protocol and codec suffix (e.g., ";codecs=opus") from file path
    const cleanPath = filePath
      ? filePath.replace(/^file:\/\/\//, '').replace(/^file:\/\//, '').split(';')[0]
      : null;

    if (!cleanPath) {
      console.log(`[VoicePlayer] No filePath, using audioUrl: "${audioUrl}"`);
      actualAudioUrl = audioUrl;
      return;
    }

    // In Tauri: Wait for convertFileSrc to load (effect will re-run when it's ready)
    if (isTauri && !convertFileSrc) {
      console.log('[VoicePlayer] Waiting for Tauri convertFileSrc to load...');
      return;
    }

    // In Tauri: Convert filesystem path to asset:// protocol
    if (isTauri && convertFileSrc) {
      try {
        const converted = convertFileSrc(cleanPath);
        console.log(`[VoicePlayer] ✅ TAURI: Converted ${cleanPath} -> ${converted}`);
        actualAudioUrl = converted;
      } catch (err) {
        console.error(`[VoicePlayer] ❌ TAURI CONVERSION FAILED:`, err);
        actualAudioUrl = audioUrl;
      }
      return;
    }

    // In Browser: Use HTTP file server to access local files (v0.13.3+)
    // Extract peer_id and filename from path: /home/mike/.dpc/conversations/{peer_id}/files/{filename}
    // or: C:\Users\mike\.dpc\conversations\{peer_id}\files\{filename}
    const pathParts = cleanPath.split(/[/\\]/);  // Split by / or \
    const conversationsIndex = pathParts.indexOf('conversations');
    if (conversationsIndex !== -1 && conversationsIndex + 3 < pathParts.length) {
      const peerId = pathParts[conversationsIndex + 1];
      const filename = pathParts[pathParts.length - 1];  // Last part is filename
      const httpUrl = `http://localhost:9998/files/${peerId}/${filename}`;
      console.log(`[VoicePlayer] ✅ BROWSER: Using HTTP file server: ${httpUrl}`);
      actualAudioUrl = httpUrl;
    } else {
      console.warn('[VoicePlayer] ⚠️ BROWSER: Could not parse file path:', cleanPath);
      actualAudioUrl = audioUrl;  // Will fail but prevents crash
    }
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
