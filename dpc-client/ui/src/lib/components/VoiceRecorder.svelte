<script lang="ts">
  import { onMount } from 'svelte';

  interface VoiceRecorderProps {
    disabled?: boolean;
    maxDuration?: number;
    onRecordingComplete: (blob: Blob, duration: number) => void;
  }

  let { disabled = false, maxDuration = 300, onRecordingComplete }: VoiceRecorderProps = $props();

  let isRecording = $state(false);
  let recordingDuration = $state(0);
  let mediaRecorder: MediaRecorder | null = null;
  let audioChunks: Blob[] = [];
  let timerInterval: ReturnType<typeof setInterval> | null = null;

  // Format duration as MM:SS
  function formatDuration(seconds: number): string {
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins}:${secs.toString().padStart(2, '0')}`;
  }

  async function startRecording() {
    // Reset duration for new recording (fixes issue where cancelled recordings kept old duration)
    recordingDuration = 0;

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      mediaRecorder = new MediaRecorder(stream, {
        mimeType: getSupportedMimeType()
      });

      audioChunks = [];

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
      console.error('Error starting voice recording:', error);
      // Could emit an error event here for UI to display
      if (error instanceof Error) {
        if (error.name === 'NotAllowedError') {
          // Detect Linux for platform-specific guidance (check multiple sources for reliability)
          const isLinux = typeof navigator !== 'undefined' && (
            navigator.userAgent.includes('Linux') ||
            navigator.userAgent.includes('X11') ||
            navigator.platform.includes('Linux') ||
            navigator.platform.includes('X11')
          );
          console.log('[VoiceRecorder] Platform detection:', {
            userAgent: navigator.userAgent,
            platform: navigator.platform,
            detectedLinux: isLinux
          });

          if (isLinux) {
            alert('Microphone access on Linux requires additional setup:\n\n' +
                  '1. Install xdg-desktop-portal:\n' +
                  '   sudo apt install xdg-desktop-portal xdg-desktop-portal-gtk\n\n' +
                  '2. Ensure PipeWire is running:\n' +
                  '   systemctl --user status pipewire pipewire-pulse\n\n' +
                  '3. Restart the application\n\n' +
                  'See: https://wiki.archlinux.org/title/Xdg_desktop_portal');
          } else {
            alert('Microphone permission denied. Please allow microphone access to record voice messages.');
          }
        } else if (error.name === 'NotFoundError') {
          alert('No microphone found. Please connect a microphone to record voice messages.');
        } else {
          alert(`Failed to start recording: ${error.message}`);
        }
      }
    }
  }

  function stopRecording() {
    if (mediaRecorder && isRecording) {
      mediaRecorder.stop();
      isRecording = false;
      if (timerInterval) {
        clearInterval(timerInterval);
        timerInterval = null;
      }
    }
  }

  function cancelRecording() {
    stopRecording();
    recordingDuration = 0;
    audioChunks = [];
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
    // Stop all audio tracks
    if (mediaRecorder && mediaRecorder.stream) {
      mediaRecorder.stream.getTracks().forEach(track => track.stop());
    }
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
