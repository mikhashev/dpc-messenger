<!-- ExtractionConsentDialog.svelte - the question Extract Knowledge asks before a conversation
     leaves this machine, with the "Don't ask again" box (pattern of FileTransferUI's offer dialog). -->
<script lang="ts">
  let {
    question = null,
    onAnswer,
  }: {
    question?: { text: string; rememberLabel: string } | null;
    onAnswer: (proceed: boolean, remember: boolean) => void;
  } = $props();

  let remember = $state(false);

  // A new question starts with the box clear: a yes is per target.
  $effect(() => {
    if (question) remember = false;
  });
</script>

{#if question}
  <div class="modal-overlay" role="presentation" onkeydown={(e) => e.key === 'Escape' && onAnswer(false, false)}>
    <div class="modal-dialog" role="dialog" aria-modal="true" aria-labelledby="extraction-consent-title" tabindex="-1">
      <h3 id="extraction-consent-title">Extract Knowledge</h3>
      <p class="question">{question.text}</p>
      <label class="remember">
        <input type="checkbox" bind:checked={remember} />
        {question.rememberLabel}
      </label>
      <div class="modal-buttons">
        <button class="accept-button" onclick={() => onAnswer(true, remember)}>Continue</button>
        <button class="reject-button" onclick={() => onAnswer(false, false)}>Cancel</button>
      </div>
    </div>
  </div>
{/if}

<style>
  .modal-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.7);
    display: flex;
    justify-content: center;
    align-items: center;
    z-index: 1000;
  }

  .modal-dialog {
    background: #1e1e1e;
    border: 1px solid #3c3c3c;
    border-radius: 8px;
    width: 90%;
    max-width: 480px;
    max-height: calc(100vh - 2rem);
    overflow-y: auto;
    padding: 16px 20px;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.5);
    color: #e0e0e0;
  }

  h3 {
    margin: 0 0 12px 0;
    font-size: 1.1rem;
  }

  .question {
    font-size: 13px;
    line-height: 1.4;
    margin: 0 0 12px 0;
  }

  .remember {
    display: flex;
    gap: 8px;
    align-items: flex-start;
    margin: 10px 0;
    font-size: 13px;
    cursor: pointer;
  }

  .modal-buttons {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
    margin-top: 12px;
  }

  .accept-button,
  .reject-button {
    padding: 6px 14px;
    border-radius: 4px;
    border: 1px solid #3c3c3c;
    cursor: pointer;
    color: #fff;
  }

  .accept-button {
    background: #2d7d46;
  }

  .reject-button {
    background: #3a3a3a;
  }
</style>
