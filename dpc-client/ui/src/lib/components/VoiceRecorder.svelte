<script lang="ts">
  import { onMount } from 'svelte';

  interface VoiceRecorderProps {
    disabled?: boolean;
    maxDuration?: number;
    onRecordingComplete: (blob: Blob, duration: number) => void;
  }

  let { disabled = false, maxDuration = 300, onRecordingComplete }: VoiceRecorderProps = $props();

  let isTauri = $state(false);
  let isRecording = $state(false);
  let recordingDuration = $state(0);
  let recordingStartTime = $state<number | null>(null);
  let timerInterval: ReturnType<typeof setInterval> | null = null;

  onMount(() => {
    // Check for Tauri environment
    isTauri = typeof window !== 'undefined' && (
      (window as any).isTauri === true ||
      !!(window as any).__TAURI__
    );
    console.log('[VoiceRecorder] Environment:', isTauri ? 'Tauri (using Rust backend)' : 'Browser (using getUserMedia)');
  });

  // Format duration as MM:SS
  function formatDuration(seconds: number): string {
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins}:${secs.toString().padStart(2, '0')}`;
  }

  async function startRecording() {
    // Reset duration for new recording
    recordingDuration = 0;
    recordingStartTime = Date.now();

    if (isTauri) {
      await startRecordingTauri();
    } else {
      await startRecordingBrowser();
    }
  }

  // Tauri/Rust backend recording (works on Linux)
  async function startRecordingTauri() {
    try {
      const { invoke } = await import('@tauri-apps/api/core');

      // Get DPC home directory for temp files
      const homeDir = await invoke('get_home_directory') as string;
      const outputDir = `${homeDir}/.dpc/temp`;

      console.log('[VoiceRecorder] Starting Rust backend recording to:', outputDir);

      const result = await invoke('tauri_start_recording', {
        outputDir,
        maxDurationSeconds: maxDuration
      });

      console.log('[VoiceRecorder] Recording started:', result);

      isRecording = true;

      // Start timer
      timerInterval = setInterval(() => {
        recordingDuration = Math.floor((Date.now() - (recordingStartTime || Date.now())) / 1000);
        if (recordingDuration >= maxDuration) {
          stopRecording();
        }
      }, 1000);

    } catch (error) {
      console.error('[VoiceRecorder] Error starting recording (Tauri):', error);
      alert(`Failed to start recording: ${error}`);
    }
  }

  // Browser getUserMedia recording (original implementation)
  async function startRecordingBrowser() {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mediaRecorder = new MediaRecorder(stream, {
        mimeType: getSupportedMimeType()
      });

      const audioChunks: Blob[] = [];

      mediaRecorder.ondataavailable = (e) => {
        if (e.data.size > 0) {
          audioChunks.push(e.data);
        }
      };

      mediaRecorder.onstop = () => {
        const audioBlob = new Blob(audioChunks, { type: mediaRecorder?.mimeType || 'audio/webm' });
        onRecordingComplete(audioBlob, recordingDuration);
        cleanup();
      };

      mediaRecorder.start();
      isRecording = true;

      // Start timer
      timerInterval = setInterval(() => {
        recordingDuration++;
        if (recordingDuration >= maxDuration) {
          stopRecording();
        }
      }, 1000);

    } catch (error) {
      console.error('[VoiceRecorder] Error starting recording (Browser):', error);
      if (error instanceof Error) {
        if (error.name === 'NotAllowedError') {
          alert('Microphone permission denied. Please allow microphone access to record voice messages.');
        } else if (error.name === 'NotFoundError') {
          alert('No microphone found. Please connect a microphone to record voice messages.');
        } else {
          alert(`Failed to start recording: ${error.message}`);
        }
      }
    }
  }

  async function stopRecording() {
    if (!isRecording) return;

    if (isTauri) {
      await stopRecordingTauri();
    } else {
      stopRecordingBrowser();
    }
  }

  async function stopRecordingTauri() {
    try {
      const { invoke } = await import('@tauri-apps/api/core');

      console.log('[VoiceRecorder] Stopping recording...');
      const outputPath = await invoke('tauri_stop_recording') as string;

      console.log('[VoiceRecorder] Recording saved to:', outputPath);

      // Read the WAV file as binary using Tauri 2.x fs plugin
      // Need to convert absolute path to relative path with BaseDirectory.Home
      // Path format: C:\Users\username\.dpc\temp\voice_xxx.wav
      const dpcIndex = outputPath.indexOf('.dpc');
      if (dpcIndex === -1) {
        throw new Error(`Invalid output path format: ${outputPath}`);
      }

      const { readFile, BaseDirectory } = await import('@tauri-apps/plugin-fs');
      // Get path starting from .dpc (including the directory)
      // Windows: C:\Users\username\.dpc\temp\voice.wav -> .dpc\temp\voice.wav
      // Unix: /home/username/.dpc/temp/voice.wav -> .dpc/temp/voice.wav
      const relativePath = outputPath.slice(dpcIndex); // e.g., ".dpc/temp/voice_xxx.wav"
      const contents = await readFile(relativePath, { baseDir: BaseDirectory.Home });

      // Create a blob from the WAV file
      const blob = new Blob([contents], { type: 'audio/wav' });
      onRecordingComplete(blob, recordingDuration);

    } catch (error) {
      console.error('[VoiceRecorder] Error stopping recording:', error);
      alert(`Failed to stop recording: ${error}`);
    } finally {
      isRecording = false;
      if (timerInterval) {
        clearInterval(timerInterval);
        timerInterval = null;
      }
    }
  }

  function stopRecordingBrowser() {
    // This would be handled by the mediaRecorder.onstop callback
    isRecording = false;
    if (timerInterval) {
      clearInterval(timerInterval);
      timerInterval = null;
    }
  }

  function cancelRecording() {
    if (isRecording && isTauri) {
      // Try to stop and delete the recording
      stopRecording();
    } else {
      stopRecordingBrowser();
    }
    recordingDuration = 0;
    recordingStartTime = null;
  }

  function getSupportedMimeType(): string {
    const types = [
      'audio/webm;codecs=opus',
      'audio/webm',
      'audio/ogg;codecs=opus',
      'audio/ogg',
      'audio/mp4',
      'audio/mpeg'
    ];

    for (const type of types) {
      if (MediaRecorder.isTypeSupported(type)) {
        return type;
      }
    }
    return 'audio/webm'; // fallback
  }

  function cleanup() {
    // Browser-specific cleanup
  }

  // Cleanup on unmount
  onMount(() => {
    return () => {
      if (timerInterval) {
        clearInterval(timerInterval);
      }
      cleanup();
    };
  });
</script>

<div class="voice-recorder" class:disabled>
  {#if !isRecording}
    <button
      class="mic-button"
      onmousedown={startRecording}
      disabled={disabled}
      title="Record voice message"
    >
      <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
        <path d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3z"/>
        <path d="M17 11c0 2.76-2.24 5-5 5s-5-2.24-5-5H5c0 3.53 2.61 6.43 6 6.92V21h2v-3.08c3.39-.49 6-3.39 6-6.92h-2z"/>
      </svg>
    </button>
  {:else}
    <div class="recording-controls">
      <div class="recording-indicator">
        <span class="red-dot"></span>
        <span class="duration">{formatDuration(recordingDuration)}</span>
      </div>
      <button
        class="stop-button"
        onmousedown={stopRecording}
        title="Stop recording"
      >
        <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
          <rect x="6" y="6" width="12" height="12" rx="2"/>
        </svg>
      </button>
      <button
        class="cancel-button"
        onmousedown={cancelRecording}
        title="Cancel recording"
      >
        <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
          <path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/>
        </svg>
      </button>
    </div>
  {/if}
</div>

<style>
  .voice-recorder {
    display: flex;
    align-items: center;
  }

  .voice-recorder.disabled {
    opacity: 0.5;
    pointer-events: none;
  }

  .mic-button {
    background: transparent;
    border: none;
    border-radius: 50%;
    width: 36px;
    height: 36px;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    color: #666;
    transition: all 0.2s ease;
  }

  .mic-button:hover {
    background: #f0f0f0;
    color: #333;
  }

  .mic-button:disabled {
    cursor: not-allowed;
    opacity: 0.5;
  }

  .recording-controls {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .recording-indicator {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 4px 8px;
    background: #fff5f5;
    border-radius: 12px;
  }

  .red-dot {
    width: 8px;
    height: 8px;
    background: #ff4444;
    border-radius: 50%;
    animation: pulse 1s infinite;
  }

  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.5; }
  }

  .duration {
    font-family: monospace;
    font-size: 13px;
    color: #333;
    font-weight: 600;
  }

  .stop-button, .cancel-button {
    border: none;
    border-radius: 6px;
    width: 32px;
    height: 32px;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: all 0.2s ease;
  }

  .stop-button {
    background: #4CAF50;
    color: white;
  }

  .stop-button:hover {
    background: #45a049;
  }

  .cancel-button {
    background: #f44336;
    color: white;
  }

  .cancel-button:hover {
    background: #da190b;
  }
</style>
