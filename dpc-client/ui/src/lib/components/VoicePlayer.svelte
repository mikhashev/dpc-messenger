<script lang="ts">
  import { onMount } from 'svelte';

  interface VoicePlayerProps {
    audioUrl: string;
    duration: number;
    timestamp?: string;
    compact?: boolean;
  }

  let { audioUrl, duration, timestamp, compact = false }: VoicePlayerProps = $props();

  let audioElement: HTMLAudioElement;
  let isPlaying = $state(false);
  let currentTime = $state(0);
  let playbackRate = $state(1.0);
  let volume = $state(1.0);

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
    audioElement = new Audio(audioUrl);
    audioElement.volume = volume;

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

    audioElement.addEventListener('timeupdate', handleTimeUpdate);
    audioElement.addEventListener('play', handlePlay);
    audioElement.addEventListener('pause', handlePause);
    audioElement.addEventListener('ended', handleEnded);

    return () => {
      audioElement.removeEventListener('timeupdate', handleTimeUpdate);
      audioElement.removeEventListener('play', handlePlay);
      audioElement.removeEventListener('pause', handlePause);
      audioElement.removeEventListener('ended', handleEnded);
      audioElement.pause();
      audioElement = null as any;
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
</style>
