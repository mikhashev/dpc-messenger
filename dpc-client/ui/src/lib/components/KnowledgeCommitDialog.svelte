<!-- KnowledgeCommitDialog.svelte -->
<!-- Displays knowledge commit proposals for user approval -->

<script lang="ts">
  import { createEventDispatcher } from 'svelte';

  // Props
  export let proposal: KnowledgeCommitProposal | null = null;
  export let open: boolean = false;

  const dispatch = createEventDispatcher();

  type KnowledgeEntry = {
    content: string;
    tags: string[];
    confidence: number;
    cultural_specific: boolean;
    requires_context: string[];
    alternative_viewpoints: string[];
    edited_by?: string | null;  // Phase 5 - inline editing attribution
    edited_at?: string | null;  // Phase 5 - inline editing timestamp
  };

  type KnowledgeCommitProposal = {
    proposal_id: string;
    topic: string;
    summary: string;
    entries: KnowledgeEntry[];
    participants: string[];
    cultural_perspectives: string[];
    alternatives: string[];
    devil_advocate: string | null;
    avg_confidence: number;
  };

  let voteComment = '';
  let showDetails = false;

  // Phase 5: Inline editing state
  let editMode = false;
  let editedEntries: KnowledgeEntry[] = [];
  let currentUserId = ''; // Will be set from nodeStatus

  // Initialize edited entries when entering edit mode
  function startEditing() {
    if (proposal) {
      editedEntries = JSON.parse(JSON.stringify(proposal.entries)); // Deep copy
      editMode = true;
    }
  }

  function cancelEditing() {
    editMode = false;
    editedEntries = [];
  }

  function saveEdits() {
    if (proposal) {
      // Update entries with edit attribution
      const now = new Date().toISOString();
      editedEntries = editedEntries.map(entry => ({
        ...entry,
        edited_by: currentUserId || 'user',
        edited_at: now
      }));
      proposal.entries = editedEntries;
    }
    editMode = false;
  }

  function handleVote(vote: 'approve' | 'reject' | 'request_changes') {
    dispatch('vote', {
      proposal_id: proposal?.proposal_id,
      vote,
      comment: voteComment
    });
    voteComment = '';
  }

  function close() {
    dispatch('close');
  }

  function handleKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      close();
    }
  }
</script>

{#if open && proposal}
  <!-- svelte-ignore a11y-click-events-have-key-events -->
  <!-- svelte-ignore a11y-no-static-element-interactions -->
  <div class="modal-overlay" on:click={close} on:keydown={handleKeydown} role="presentation">
    <div class="modal" on:click|stopPropagation role="dialog" aria-labelledby="dialog-title" tabindex="-1">
      <div class="modal-header">
        <h2 id="dialog-title">Knowledge Commit Proposal</h2>
        <div class="header-actions">
          {#if !editMode}
            <button class="btn-edit" on:click={startEditing}>Edit</button>
          {:else}
            <button class="btn-save" on:click={saveEdits}>Save</button>
            <button class="btn-cancel" on:click={cancelEditing}>Cancel</button>
          {/if}
          <button class="close-btn" on:click={close}>&times;</button>
        </div>
      </div>

      <div class="modal-body">
        <!-- Summary -->
        <div class="section">
          <h3>Summary</h3>
          <p class="summary">{proposal.summary}</p>
        </div>

        <!-- Topic -->
        <div class="section">
          <strong>Topic:</strong> {proposal.topic}
        </div>

        <!-- Knowledge Entries -->
        <div class="section">
          <h3>Proposed Knowledge ({editMode ? editedEntries.length : proposal.entries.length} entries)</h3>
          {#each (editMode ? editedEntries : proposal.entries) as entry, i}
            <div class="entry">
              <div class="entry-header">
                <span class="entry-number">#{i + 1}</span>
                <span class="confidence" class:high={entry.confidence >= 0.8} class:medium={entry.confidence >= 0.5 && entry.confidence < 0.8} class:low={entry.confidence < 0.5}>
                  {Math.round(entry.confidence * 100)}% confidence
                </span>
                {#if entry.edited_by}
                  <span class="edited-badge" title="Edited by {entry.edited_by} at {new Date(entry.edited_at || '').toLocaleString()}">
                    ✏️ Edited
                  </span>
                {/if}
              </div>

              {#if editMode}
                <textarea
                  class="entry-edit"
                  bind:value={entry.content}
                  rows="4"
                  placeholder="Edit knowledge entry..."
                ></textarea>
              {:else}
                <p class="entry-content">{entry.content}</p>
              {/if}

              {#if entry.tags.length > 0}
                <div class="tags">
                  {#each entry.tags as tag}
                    <span class="tag">{tag}</span>
                  {/each}
                </div>
              {/if}

              {#if entry.cultural_specific}
                <div class="warning">
                  Cultural Context Required: {entry.requires_context.join(', ')}
                </div>
              {/if}

              {#if entry.alternative_viewpoints.length > 0}
                <details class="alternatives">
                  <summary>Alternative Viewpoints ({entry.alternative_viewpoints.length})</summary>
                  <ul>
                    {#each entry.alternative_viewpoints as alt}
                      <li>{alt}</li>
                    {/each}
                  </ul>
                </details>
              {/if}
            </div>
          {/each}
        </div>

        <!-- Bias Mitigation Info -->
        <div class="section bias-section">
          <h3>Bias Mitigation</h3>

          {#if proposal.cultural_perspectives && proposal.cultural_perspectives.length > 0}
            <div class="bias-item">
              <strong>Cultural Perspectives Considered:</strong>
              <div class="perspectives">
                {#each proposal.cultural_perspectives as perspective}
                  <span class="perspective-badge">{perspective}</span>
                {/each}
              </div>
            </div>
          {/if}

          <div class="bias-item">
            <strong>AI Confidence:</strong>
            <span class="confidence-score">{Math.round(proposal.avg_confidence * 100)}%</span>
          </div>

          {#if proposal.devil_advocate}
            <details class="devil-advocate" open>
              <summary><strong>Devil's Advocate (Critical Analysis)</strong></summary>
              <p class="critique">{proposal.devil_advocate}</p>
            </details>
          {/if}

          {#if proposal.alternatives.length > 0}
            <details class="alternatives">
              <summary><strong>Alternative Interpretations</strong></summary>
              <ul>
                {#each proposal.alternatives as alt}
                  <li>{alt}</li>
                {/each}
              </ul>
            </details>
          {/if}
        </div>

        <!-- Review Checklist -->
        <div class="section checklist">
          <h3>Review Checklist</h3>
          <ul>
            {#if proposal.cultural_perspectives && proposal.cultural_perspectives.length > 0}
              <li>Is this culturally neutral or are assumptions flagged?</li>
            {/if}
            <li>Are confidence scores reasonable?</li>
            <li>Are alternative views represented?</li>
            {#if proposal.cultural_perspectives && proposal.cultural_perspectives.length > 0}
              <li>Would this work in different cultural contexts?</li>
            {/if}
          </ul>
        </div>

        <!-- Participants -->
        <div class="section participants">
          <strong>Participants:</strong> {proposal.participants.join(', ')}
        </div>

        <!-- Comment -->
        <div class="section">
          <label for="comment">Your Comment (optional):</label>
          <textarea
            id="comment"
            bind:value={voteComment}
            placeholder="Add any notes or suggestions..."
            rows="3"
          ></textarea>
        </div>
      </div>

      <div class="modal-footer">
        <button class="btn btn-approve" on:click={() => handleVote('approve')}>
          Approve
        </button>
        <button class="btn btn-changes" on:click={() => handleVote('request_changes')}>
          Request Changes
        </button>
        <button class="btn btn-reject" on:click={() => handleVote('reject')}>
          Reject
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
    max-width: 700px;
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

  .header-actions {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .btn-edit, .btn-save, .btn-cancel {
    padding: 0.5rem 1rem;
    border: none;
    border-radius: 4px;
    font-size: 0.9rem;
    cursor: pointer;
    transition: all 0.2s;
  }

  .btn-edit {
    background: #2196F3;
    color: white;
  }

  .btn-edit:hover {
    background: #1976D2;
  }

  .btn-save {
    background: #4CAF50;
    color: white;
  }

  .btn-save:hover {
    background: #45a049;
  }

  .btn-cancel {
    background: #f44336;
    color: white;
  }

  .btn-cancel:hover {
    background: #da190b;
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

  .summary {
    font-size: 1.1rem;
    font-weight: 500;
    color: #333;
    padding: 0.75rem;
    background: #f5f5f5;
    border-radius: 4px;
  }

  .entry {
    border: 1px solid #e0e0e0;
    border-radius: 6px;
    padding: 1rem;
    margin-bottom: 0.75rem;
    background: #fafafa;
  }

  .entry-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.5rem;
  }

  .entry-number {
    font-weight: bold;
    color: #666;
  }

  .confidence {
    font-size: 0.85rem;
    padding: 0.25rem 0.5rem;
    border-radius: 4px;
    font-weight: 500;
  }

  .confidence.high {
    background: #d4edda;
    color: #155724;
  }

  .confidence.medium {
    background: #fff3cd;
    color: #856404;
  }

  .confidence.low {
    background: #f8d7da;
    color: #721c24;
  }

  .entry-content {
    margin: 0.5rem 0;
    line-height: 1.5;
    color: #333;
  }

  .entry-edit {
    width: 100%;
    margin: 0.5rem 0;
    padding: 0.75rem;
    border: 2px solid #2196F3;
    border-radius: 4px;
    font-family: inherit;
    font-size: 1rem;
    line-height: 1.5;
    resize: vertical;
  }

  .entry-edit:focus {
    outline: none;
    border-color: #1976D2;
    box-shadow: 0 0 0 2px rgba(33, 150, 243, 0.1);
  }

  .edited-badge {
    background: #ffeb3b;
    color: #333;
    padding: 0.25rem 0.5rem;
    border-radius: 12px;
    font-size: 0.75rem;
    font-weight: 500;
    margin-left: 0.5rem;
  }

  .tags {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    margin-top: 0.5rem;
  }

  .tag {
    background: #e3f2fd;
    color: #0d47a1;
    padding: 0.25rem 0.5rem;
    border-radius: 12px;
    font-size: 0.85rem;
  }

  .warning {
    background: #fff8e1;
    border-left: 3px solid #ffa000;
    padding: 0.5rem;
    margin-top: 0.5rem;
    font-size: 0.9rem;
    color: #ff6f00;
  }

  .alternatives, .devil-advocate {
    margin-top: 0.75rem;
  }

  .alternatives summary, .devil-advocate summary {
    cursor: pointer;
    font-weight: 500;
    color: #1976d2;
    padding: 0.5rem;
    background: #f5f5f5;
    border-radius: 4px;
  }

  .alternatives ul {
    margin: 0.5rem 0 0 0;
    padding-left: 1.5rem;
  }

  .alternatives li {
    margin: 0.25rem 0;
  }

  .critique {
    margin: 0.5rem 0;
    padding: 0.75rem;
    background: #fff3e0;
    border-left: 3px solid #ff6f00;
    font-style: italic;
    color: #e65100;
  }

  .bias-section {
    background: #f0f7ff;
    padding: 1rem;
    border-radius: 6px;
    border: 1px solid #bbdefb;
  }

  .bias-item {
    margin-bottom: 0.75rem;
  }

  .perspectives {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    margin-top: 0.25rem;
  }

  .perspective-badge {
    background: #1976d2;
    color: white;
    padding: 0.25rem 0.75rem;
    border-radius: 12px;
    font-size: 0.85rem;
  }

  .confidence-score {
    font-weight: bold;
    color: #1976d2;
    font-size: 1.1rem;
  }

  .checklist ul {
    list-style: none;
    padding-left: 0;
  }

  .checklist li {
    padding: 0.5rem 0;
    border-bottom: 1px solid #e0e0e0;
  }

  .checklist li:last-child {
    border-bottom: none;
  }

  .participants {
    font-size: 0.9rem;
    color: #666;
  }

  textarea {
    width: 100%;
    padding: 0.75rem;
    border: 1px solid #ccc;
    border-radius: 4px;
    font-family: inherit;
    resize: vertical;
  }

  .modal-footer {
    display: flex;
    gap: 0.75rem;
    padding: 1rem 1.5rem;
    border-top: 1px solid #e0e0e0;
  }

  .btn {
    flex: 1;
    padding: 0.75rem 1.5rem;
    border: none;
    border-radius: 6px;
    font-size: 1rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s;
  }

  .btn-approve {
    background: #4caf50;
    color: white;
  }

  .btn-approve:hover {
    background: #45a049;
  }

  .btn-changes {
    background: #ff9800;
    color: white;
  }

  .btn-changes:hover {
    background: #f57c00;
  }

  .btn-reject {
    background: #f44336;
    color: white;
  }

  .btn-reject:hover {
    background: #d32f2f;
  }
</style>
