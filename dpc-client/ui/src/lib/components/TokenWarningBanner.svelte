<!-- TokenWarningBanner.svelte - Token limit warning banner -->
<!-- Displays warnings at 90% (dismissible) and 100% (critical, non-dismissible) -->

<script lang="ts">
  // Props (Svelte 5 runes mode)
  let {
    severity,
    percentage,
    onEndSession,
    dismissible = true
  }: {
    severity: 'warning' | 'critical';
    percentage: number;
    onEndSession: () => void;
    dismissible?: boolean;
  } = $props();

  let dismissed = $state(false);

  // Computed properties
  let percentageText = $derived(Math.round(percentage * 100));

  let message = $derived(
    severity === 'critical'
      ? `Context window full (${percentageText}%). End session to continue chatting.`
      : `Context window at ${percentageText}%. Consider ending session to save knowledge.`
  );

  let iconEmoji = $derived(severity === 'critical' ? '🛑' : '⚠️');

  let colors = $derived({
    bg: severity === 'critical' ? '#f8d7da' : '#fff3cd',
    border: severity === 'critical' ? '#dc3545' : '#ffc107',
    text: severity === 'critical' ? '#721c24' : '#856404'
  });
</script>

{#if !dismissed}
  <div
    class="token-warning-banner"
    style="
      background: {colors.bg};
      border-color: {colors.border};
      color: {colors.text};
    "
    role="alert"
    aria-live="assertive"
    aria-atomic="true"
  >
    <div class="banner-content">
      <span class="banner-icon" aria-hidden="true">
        {iconEmoji}
      </span>
      <span class="banner-message">
        {message}
      </span>
    </div>
    <div class="banner-actions">
      <button
        class="btn-end-session-banner"
        onclick={onEndSession}
        aria-label="End session to save knowledge"
      >
        End Session Now
      </button>
      {#if dismissible}
        <button
          class="btn-dismiss"
          onclick={() => dismissed = true}
          aria-label="Dismiss warning"
        >
          ✕
        </button>
      {/if}
    </div>
  </div>
{/if}

<style>
  .token-warning-banner {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.75rem 1rem;
    border: 2px solid;
    border-radius: 6px;
    margin-bottom: 0.5rem;
    animation: slideIn 0.3s ease-out;
  }

  @keyframes slideIn {
    from {
      opacity: 0;
      transform: translateY(-10px);
    }
    to {
      opacity: 1;
      transform: translateY(0);
    }
  }

  .banner-content {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex: 1;
  }

  .banner-icon {
    font-size: 1.2rem;
    flex-shrink: 0;
  }

  .banner-message {
    font-weight: 500;
    font-size: 0.9rem;
    line-height: 1.4;
  }

  .banner-actions {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-shrink: 0;
  }

  .btn-end-session-banner {
    padding: 0.4rem 0.8rem;
    background: #28a745;
    color: white;
    border: none;
    border-radius: 4px;
    font-size: 0.85rem;
    font-weight: 500;
    cursor: pointer;
    white-space: nowrap;
    transition: all 0.2s;
    box-shadow: 0 2px 4px rgba(40, 167, 69, 0.2);
  }

  .btn-end-session-banner:hover {
    background: #20c997;
    transform: translateY(-1px);
    box-shadow: 0 3px 8px rgba(40, 167, 69, 0.3);
  }

  .btn-end-session-banner:active {
    transform: translateY(0);
    box-shadow: 0 1px 3px rgba(40, 167, 69, 0.2);
  }

  .btn-dismiss {
    padding: 0.2rem 0.5rem;
    background: transparent;
    border: none;
    font-size: 1.2rem;
    cursor: pointer;
    color: inherit;
    opacity: 0.6;
    transition: opacity 0.2s;
    line-height: 1;
  }

  .btn-dismiss:hover {
    opacity: 1;
  }

  .btn-dismiss:active {
    opacity: 0.8;
  }

  /* Responsive design */
  @media (max-width: 600px) {
    .token-warning-banner {
      flex-direction: column;
      align-items: stretch;
      gap: 0.75rem;
    }

    .banner-content {
      flex-direction: column;
      align-items: flex-start;
      gap: 0.4rem;
    }

    .banner-actions {
      justify-content: space-between;
    }

    .btn-end-session-banner {
      flex: 1;
    }
  }
</style>
