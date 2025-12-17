<script lang="ts">
  import { createEventDispatcher } from 'svelte';

  // Props
  export let proposal: any | null = null;
  export let open: boolean = false;

  const dispatch = createEventDispatcher();

  let voteComment = '';

  function handleVote(vote: boolean) {
    dispatch('vote', {
      proposal_id: proposal?.proposal_id,
      vote,
      comment: voteComment
    });
    voteComment = '';
  }

  function formatTimestamp(isoString: string): string {
    try {
      const date = new Date(isoString);
      return date.toLocaleString();
    } catch {
      return isoString;
    }
  }
</script>

{#if open && proposal}
  <div class="modal-overlay" role="presentation">
    <div class="modal" role="dialog" aria-labelledby="dialog-title" tabindex="-1">
      <div class="modal-header">
        <h2 id="dialog-title">🔄 New Session Request</h2>
      </div>

      <div class="modal-body">
        <!-- Proposal Info -->
        <div class="section">
          <p class="proposal-message">
            <strong>{proposal.initiator_node_id}</strong> wants to start a new session.
          </p>
          <p class="proposal-details">
            This will clear the conversation history for both participants if approved.
          </p>
        </div>

        <!-- Timestamp -->
        <div class="section timestamp-section">
          <small>Requested: {formatTimestamp(proposal.timestamp)}</small>
        </div>

        <!-- Optional Comment -->
        <div class="section">
          <label for="vote-comment">Comment (optional):</label>
          <textarea
            id="vote-comment"
            bind:value={voteComment}
            placeholder="Add a comment for your vote..."
            rows="2"
          ></textarea>
        </div>

        <!-- Voting Instructions -->
        <div class="section instructions">
          <p><strong>Important:</strong> You must explicitly approve or reject this request.</p>
          <p>All participants must vote before the session can be reset (60 second timeout).</p>
        </div>
      </div>

      <div class="modal-footer">
        <button class="btn-reject" on:click={() => handleVote(false)}>
          ❌ Reject
        </button>
        <button class="btn-approve" on:click={() => handleVote(true)}>
          ✅ Approve
        </button>
      </div>
    </div>
  </div>
{/if}

<style>
  .modal-overlay {
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    background: rgba(0, 0, 0, 0.7);
    display: flex;
    justify-content: center;
    align-items: center;
    z-index: 1000;
  }

  .modal {
    background: #1e1e1e;
    border: 1px solid #3c3c3c;
    border-radius: 8px;
    width: 90%;
    max-width: 500px;
    max-height: 80vh;
    display: flex;
    flex-direction: column;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.5);
  }

  .modal-header {
    padding: 16px 20px;
    border-bottom: 1px solid #3c3c3c;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  .modal-header h2 {
    margin: 0;
    font-size: 1.25rem;
    color: #e0e0e0;
  }

  .modal-body {
    padding: 20px;
    overflow-y: auto;
    flex: 1;
  }

  .section {
    margin-bottom: 16px;
  }

  .proposal-message {
    font-size: 1.1rem;
    color: #e0e0e0;
    margin-bottom: 8px;
  }

  .proposal-message strong {
    color: #4a9eff;
  }

  .proposal-details {
    color: #b0b0b0;
    font-size: 0.95rem;
    margin: 0;
  }

  .timestamp-section {
    color: #888;
    border-top: 1px solid #3c3c3c;
    padding-top: 12px;
  }

  .instructions {
    background: #2a2a2a;
    border-left: 3px solid #ffa500;
    padding: 12px;
    border-radius: 4px;
  }

  .instructions p {
    margin: 0 0 8px 0;
    color: #d0d0d0;
    font-size: 0.9rem;
  }

  .instructions p:last-child {
    margin-bottom: 0;
  }

  label {
    display: block;
    margin-bottom: 6px;
    color: #b0b0b0;
    font-size: 0.9rem;
  }

  textarea {
    width: 100%;
    padding: 8px;
    background: #2a2a2a;
    border: 1px solid #3c3c3c;
    border-radius: 4px;
    color: #e0e0e0;
    font-family: inherit;
    font-size: 0.9rem;
    resize: vertical;
  }

  textarea:focus {
    outline: none;
    border-color: #4a9eff;
  }

  .modal-footer {
    padding: 16px 20px;
    border-top: 1px solid #3c3c3c;
    display: flex;
    justify-content: flex-end;
    gap: 12px;
  }

  .modal-footer button {
    padding: 8px 20px;
    border: none;
    border-radius: 4px;
    font-size: 0.95rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s;
  }

  .btn-approve {
    background: #28a745;
    color: white;
  }

  .btn-approve:hover {
    background: #218838;
  }

  .btn-reject {
    background: #dc3545;
    color: white;
  }

  .btn-reject:hover {
    background: #c82333;
  }

  .modal-footer button:active {
    transform: scale(0.98);
  }
</style>
