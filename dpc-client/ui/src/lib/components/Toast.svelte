<script lang="ts">
  import { onMount } from 'svelte';

  // Props
  export let message: string = '';
  export let type: 'info' | 'warning' | 'error' = 'info';
  export let duration: number = 5000; // Auto-dismiss after 5 seconds (0 = no auto-dismiss)
  export let dismissible: boolean = true;
  export let onDismiss: (() => void) | null = null;

  let visible = false;
  let timeoutId: ReturnType<typeof setTimeout> | null = null;

  onMount(() => {
    // Fade in
    setTimeout(() => {
      visible = true;
    }, 10);

    // Auto-dismiss if duration > 0
    if (duration > 0) {
      timeoutId = setTimeout(() => {
        handleDismiss();
      }, duration);
    }

    return () => {
      if (timeoutId) {
        clearTimeout(timeoutId);
      }
    };
  });

  function handleDismiss() {
    visible = false;
    setTimeout(() => {
      if (onDismiss) {
        onDismiss();
      }
    }, 300); // Wait for fade-out animation
  }

  // Map type to icon and color
  const typeConfig = {
    info: { icon: 'ℹ️', color: '#2196F3' },
    warning: { icon: '⚠️', color: '#ff9800' },
    error: { icon: '❌', color: '#f44336' }
  };

  $: config = typeConfig[type];
</script>

<div
  class="toast"
  class:visible
  class:info={type === 'info'}
  class:warning={type === 'warning'}
  class:error={type === 'error'}
  role="alert"
  aria-live="polite"
>
  <div class="toast-content">
    <span class="toast-icon">{config.icon}</span>
    <span class="toast-message">{message}</span>
  </div>

  {#if dismissible}
    <button
      class="toast-dismiss"
      on:click={handleDismiss}
      aria-label="Dismiss notification"
    >
      ✕
    </button>
  {/if}
</div>

<style>
  .toast {
    position: fixed;
    top: 80px;
    right: 20px;
    min-width: 300px;
    max-width: 500px;
    padding: 1rem 1.25rem;
    background: white;
    border-radius: 8px;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    z-index: 10000;
    opacity: 0;
    transform: translateX(400px);
    transition: opacity 0.3s ease, transform 0.3s ease;
    border-left: 4px solid #ccc;
  }

  .toast.visible {
    opacity: 1;
    transform: translateX(0);
  }

  .toast.info {
    border-left-color: #2196F3;
    background: #e3f2fd;
  }

  .toast.warning {
    border-left-color: #ff9800;
    background: #fff3e0;
  }

  .toast.error {
    border-left-color: #f44336;
    background: #ffebee;
  }

  .toast-content {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    flex: 1;
  }

  .toast-icon {
    font-size: 1.25rem;
    flex-shrink: 0;
  }

  .toast-message {
    color: #333;
    font-size: 0.95rem;
    line-height: 1.4;
  }

  .toast-dismiss {
    background: none;
    border: none;
    color: #666;
    font-size: 1.25rem;
    cursor: pointer;
    padding: 0.25rem;
    border-radius: 4px;
    transition: all 0.2s;
    flex-shrink: 0;
    line-height: 1;
  }

  .toast-dismiss:hover {
    background: rgba(0, 0, 0, 0.1);
    color: #333;
  }

  .toast-dismiss:focus {
    outline: 2px solid rgba(0, 0, 0, 0.2);
    outline-offset: 2px;
  }
</style>
