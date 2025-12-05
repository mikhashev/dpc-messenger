<!-- VoteResultDialog.svelte -->
<!-- Displays detailed voting results for knowledge commit proposals -->

<script lang="ts">
  import { createEventDispatcher } from 'svelte';

  // Props
  export let result: VoteResult | null = null;
  export let open: boolean = false;

  const dispatch = createEventDispatcher();

  type VoteDetail = {
    node_id: string;
    vote: 'approve' | 'reject' | 'request_changes';
    comment: string | null;
    is_required_dissent: boolean;
    timestamp: string;
  };

  type VoteResult = {
    proposal_id: string;
    topic: string;
    summary: string;
    status: 'approved' | 'rejected' | 'revision_needed' | 'timeout';
    vote_tally: {
      approve: number;
      reject: number;
      request_changes: number;
      total: number;
      threshold: number;
      approval_rate: number;
    };
    votes: VoteDetail[];
    commit_id?: string;
    timestamp: string;
  };

  function close() {
    dispatch('close');
  }

  function handleKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      close();
    }
  }

  function getVoteIcon(vote: string): string {
    switch (vote) {
      case 'approve': return '✅';
      case 'reject': return '❌';
      case 'request_changes': return '📝';
      default: return '❓';
    }
  }

  function getVoteLabel(vote: string): string {
    switch (vote) {
      case 'approve': return 'Approved';
      case 'reject': return 'Rejected';
      case 'request_changes': return 'Requested Changes';
      default: return 'Unknown';
    }
  }

  function getStatusIcon(status: string): string {
    switch (status) {
      case 'approved': return '✅';
      case 'rejected': return '❌';
      case 'revision_needed': return '📝';
      case 'timeout': return '⏱️';
      default: return '❓';
    }
  }

  function getStatusLabel(status: string): string {
    switch (status) {
      case 'approved': return 'Approved';
      case 'rejected': return 'Rejected';
      case 'revision_needed': return 'Revision Needed';
      case 'timeout': return 'Voting Timeout';
      default: return 'Unknown';
    }
  }

  function formatNodeId(nodeId: string): string {
    // Show first 20 characters of node ID
    return nodeId.length > 20 ? nodeId.substring(0, 20) + '...' : nodeId;
  }

  function formatTimestamp(timestamp: string): string {
    return new Date(timestamp).toLocaleString();
  }
</script>

{#if open && result}
  <!-- svelte-ignore a11y-click-events-have-key-events -->
  <!-- svelte-ignore a11y-no-static-element-interactions -->
  <div class="modal-overlay" on:click={close} on:keydown={handleKeydown} role="presentation">
    <div class="modal" on:click|stopPropagation role="dialog" aria-labelledby="dialog-title" tabindex="-1">
      <div class="modal-header">
        <h2 id="dialog-title">Voting Results</h2>
        <button class="close-btn" on:click={close}>&times;</button>
      </div>

      <div class="modal-body">
        <!-- Summary Section -->
        <div class="section summary-section">
          <div class="status-badge" class:approved={result.status === 'approved'}
               class:rejected={result.status === 'rejected'}
               class:revision={result.status === 'revision_needed'}
               class:timeout={result.status === 'timeout'}>
            {getStatusIcon(result.status)} {getStatusLabel(result.status)}
          </div>
          <h3>{result.topic}</h3>
          <p class="summary">{result.summary}</p>
        </div>

        <!-- Vote Tally -->
        <div class="section tally-section">
          <h3>Vote Statistics</h3>
          <div class="tally-grid">
            <div class="tally-item approve">
              <div class="tally-icon">✅</div>
              <div class="tally-count">{result.vote_tally.approve}</div>
              <div class="tally-label">Approve</div>
            </div>
            <div class="tally-item reject">
              <div class="tally-icon">❌</div>
              <div class="tally-count">{result.vote_tally.reject}</div>
              <div class="tally-label">Reject</div>
            </div>
            <div class="tally-item changes">
              <div class="tally-icon">📝</div>
              <div class="tally-count">{result.vote_tally.request_changes}</div>
              <div class="tally-label">Changes</div>
            </div>
          </div>
          <div class="threshold-info">
            <strong>Approval Rate:</strong> {Math.round(result.vote_tally.approval_rate * 100)}%
            (Threshold: {Math.round(result.vote_tally.threshold * 100)}%)
          </div>
        </div>

        <!-- Individual Votes -->
        <div class="section votes-section">
          <h3>Individual Votes ({result.votes.length})</h3>
          {#each result.votes as vote}
            <div class="vote-card" class:dissent={vote.is_required_dissent}>
              <div class="vote-header">
                <div class="vote-icon">{getVoteIcon(vote.vote)}</div>
                <div class="vote-info">
                  <div class="vote-label">{getVoteLabel(vote.vote)}</div>
                  <div class="voter-id" title={vote.node_id}>{formatNodeId(vote.node_id)}</div>
                  {#if vote.is_required_dissent}
                    <span class="dissent-badge" title="Devil's Advocate - Required dissenting voice">
                      👿 Devil's Advocate
                    </span>
                  {/if}
                </div>
                <div class="vote-time">{formatTimestamp(vote.timestamp)}</div>
              </div>
              {#if vote.comment}
                <div class="vote-comment">
                  <strong>Comment:</strong> {vote.comment}
                </div>
              {/if}
            </div>
          {/each}
        </div>

        <!-- Metadata -->
        <div class="section metadata-section">
          <div class="metadata-item">
            <strong>Proposal ID:</strong> {result.proposal_id}
          </div>
          {#if result.commit_id}
            <div class="metadata-item">
              <strong>Commit ID:</strong> {result.commit_id}
            </div>
          {/if}
          <div class="metadata-item">
            <strong>Finalized:</strong> {formatTimestamp(result.timestamp)}
          </div>
        </div>
      </div>

      <div class="modal-footer">
        <button class="btn btn-primary" on:click={close}>Close</button>
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
    background: rgba(0, 0, 0, 0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }

  .modal {
    background: white;
    border-radius: 8px;
    width: 90%;
    max-width: 800px;
    max-height: 90vh;
    display: flex;
    flex-direction: column;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
  }

  .modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 1rem 1.5rem;
    border-bottom: 1px solid #e0e0e0;
  }

  .modal-header h2 {
    margin: 0;
    font-size: 1.5rem;
    color: #333;
  }

  .close-btn {
    background: none;
    border: none;
    font-size: 2rem;
    cursor: pointer;
    color: #999;
    line-height: 1;
  }

  .close-btn:hover {
    color: #333;
  }

  .modal-body {
    padding: 1.5rem;
    overflow-y: auto;
    flex: 1;
  }

  .section {
    margin-bottom: 1.5rem;
  }

  .section h3 {
    margin: 0 0 0.75rem 0;
    font-size: 1.1rem;
    color: #555;
  }

  .summary-section {
    text-align: center;
    padding: 1rem;
    background: #f5f5f5;
    border-radius: 6px;
  }

  .status-badge {
    display: inline-block;
    padding: 0.5rem 1rem;
    border-radius: 20px;
    font-weight: 600;
    font-size: 1rem;
    margin-bottom: 1rem;
  }

  .status-badge.approved {
    background: #d4edda;
    color: #155724;
  }

  .status-badge.rejected {
    background: #f8d7da;
    color: #721c24;
  }

  .status-badge.revision {
    background: #fff3cd;
    color: #856404;
  }

  .status-badge.timeout {
    background: #d1ecf1;
    color: #0c5460;
  }

  .summary {
    font-size: 1rem;
    color: #666;
    margin: 0.5rem 0 0 0;
  }

  .tally-section {
    background: #f0f7ff;
    padding: 1rem;
    border-radius: 6px;
  }

  .tally-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 1rem;
    margin-bottom: 1rem;
  }

  .tally-item {
    text-align: center;
    padding: 1rem;
    border-radius: 6px;
    background: white;
  }

  .tally-item.approve {
    border: 2px solid #4caf50;
  }

  .tally-item.reject {
    border: 2px solid #f44336;
  }

  .tally-item.changes {
    border: 2px solid #ff9800;
  }

  .tally-icon {
    font-size: 2rem;
    margin-bottom: 0.5rem;
  }

  .tally-count {
    font-size: 2rem;
    font-weight: bold;
    color: #333;
  }

  .tally-label {
    font-size: 0.9rem;
    color: #666;
    margin-top: 0.25rem;
  }

  .threshold-info {
    text-align: center;
    padding: 0.75rem;
    background: white;
    border-radius: 4px;
    font-size: 0.95rem;
  }

  .votes-section {
    background: #fafafa;
    padding: 1rem;
    border-radius: 6px;
  }

  .vote-card {
    background: white;
    border: 1px solid #e0e0e0;
    border-radius: 6px;
    padding: 1rem;
    margin-bottom: 0.75rem;
  }

  .vote-card.dissent {
    border: 2px solid #ff6f00;
    background: #fff8e1;
  }

  .vote-header {
    display: flex;
    align-items: center;
    gap: 0.75rem;
  }

  .vote-icon {
    font-size: 1.5rem;
  }

  .vote-info {
    flex: 1;
  }

  .vote-label {
    font-weight: 600;
    font-size: 1rem;
    color: #333;
  }

  .voter-id {
    font-size: 0.85rem;
    color: #666;
    font-family: monospace;
  }

  .dissent-badge {
    display: inline-block;
    background: #ff6f00;
    color: white;
    padding: 0.2rem 0.5rem;
    border-radius: 12px;
    font-size: 0.75rem;
    font-weight: 500;
    margin-top: 0.25rem;
  }

  .vote-time {
    font-size: 0.85rem;
    color: #999;
  }

  .vote-comment {
    margin-top: 0.75rem;
    padding: 0.75rem;
    background: #f5f5f5;
    border-left: 3px solid #2196F3;
    border-radius: 4px;
    font-size: 0.9rem;
    color: #333;
  }

  .metadata-section {
    font-size: 0.85rem;
    color: #666;
    padding: 1rem;
    background: #f5f5f5;
    border-radius: 6px;
  }

  .metadata-item {
    margin-bottom: 0.5rem;
  }

  .metadata-item:last-child {
    margin-bottom: 0;
  }

  .modal-footer {
    display: flex;
    justify-content: flex-end;
    padding: 1rem 1.5rem;
    border-top: 1px solid #e0e0e0;
  }

  .btn {
    padding: 0.75rem 1.5rem;
    border: none;
    border-radius: 6px;
    font-size: 1rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s;
  }

  .btn-primary {
    background: #2196F3;
    color: white;
  }

  .btn-primary:hover {
    background: #1976D2;
  }
</style>
