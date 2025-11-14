<!-- ContextViewer.svelte -->
<!-- View and manage personal context (v2.0 format) -->

<script lang="ts">
  import { createEventDispatcher } from 'svelte';

  export let context: PersonalContext | null = null;
  export let open: boolean = false;

  const dispatch = createEventDispatcher();

  type PersonalContext = {
    profile: {
      name: string;
      description: string;
      core_values: string[];
    };
    knowledge: Record<string, Topic>;
    instruction: InstructionBlock;
    cognitive_profile: CognitiveProfile | null;
    version: number;
    commit_history: CommitHistoryItem[];
    metadata: {
      format_version: string;
      last_updated: string;
    };
  };

  type Topic = {
    summary: string;
    entries: KnowledgeEntry[];
    mastery_level: string;
    version: number;
    markdown_file: string | null;
  };

  type KnowledgeEntry = {
    content: string;
    tags: string[];
    confidence: number;
  };

  type InstructionBlock = {
    primary: string;
    bias_mitigation: {
      require_multi_perspective: boolean;
      challenge_status_quo: boolean;
      cultural_sensitivity: string;
    };
  };

  type CognitiveProfile = {
    cultural_background: string;
    memory_strengths: string[];
  };

  type CommitHistoryItem = {
    commit_id: string;
    message: string;
    timestamp: string;
  };

  let selectedTab: 'profile' | 'knowledge' | 'instructions' | 'history' = 'profile';

  function close() {
    dispatch('close');
  }

  function openMarkdownFile(filename: string) {
    dispatch('open-markdown', { filename });
  }

  function handleKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      close();
    }
  }
</script>

{#if open && context}
  <!-- svelte-ignore a11y-click-events-have-key-events -->
  <!-- svelte-ignore a11y-no-static-element-interactions -->
  <div class="modal-overlay" on:click={close} on:keydown={handleKeydown} role="presentation">
    <div class="modal" on:click|stopPropagation role="dialog" aria-labelledby="context-dialog-title" tabindex="-1">
      <div class="modal-header">
        <h2 id="context-dialog-title">Personal Context - {context.profile.name}</h2>
        <button class="close-btn" on:click={close}>&times;</button>
      </div>

      <!-- Tabs -->
      <div class="tabs">
        <button
          class="tab"
          class:active={selectedTab === 'profile'}
          on:click={() => selectedTab = 'profile'}
        >
          Profile
        </button>
        <button
          class="tab"
          class:active={selectedTab === 'knowledge'}
          on:click={() => selectedTab = 'knowledge'}
        >
          Knowledge ({Object.keys(context.knowledge).length})
        </button>
        <button
          class="tab"
          class:active={selectedTab === 'instructions'}
          on:click={() => selectedTab = 'instructions'}
        >
          AI Instructions
        </button>
        <button
          class="tab"
          class:active={selectedTab === 'history'}
          on:click={() => selectedTab = 'history'}
        >
          History ({context.commit_history.length})
        </button>
      </div>

      <div class="modal-body">
        {#if selectedTab === 'profile'}
          <div class="section">
            <h3>Profile</h3>
            <div class="info-grid">
              <div class="info-item">
                <strong>Name:</strong>
                <span>{context.profile.name}</span>
              </div>
              <div class="info-item">
                <strong>Description:</strong>
                <span>{context.profile.description}</span>
              </div>
              {#if context.profile.core_values.length > 0}
                <div class="info-item">
                  <strong>Core Values:</strong>
                  <div class="tags">
                    {#each context.profile.core_values as value}
                      <span class="tag">{value}</span>
                    {/each}
                  </div>
                </div>
              {/if}
            </div>
          </div>

          {#if context.cognitive_profile}
            <div class="section">
              <h3>Cognitive Profile</h3>
              <div class="info-grid">
                <div class="info-item">
                  <strong>Cultural Background:</strong>
                  <span>{context.cognitive_profile.cultural_background || 'Not specified'}</span>
                </div>
                {#if context.cognitive_profile.memory_strengths.length > 0}
                  <div class="info-item">
                    <strong>Memory Strengths:</strong>
                    <div class="tags">
                      {#each context.cognitive_profile.memory_strengths as strength}
                        <span class="tag">{strength}</span>
                      {/each}
                    </div>
                  </div>
                {/if}
              </div>
            </div>
          {/if}

          <div class="section">
            <h3>Metadata</h3>
            <div class="info-grid">
              <div class="info-item">
                <strong>Format Version:</strong>
                <span class="version-badge">{context.metadata.format_version}</span>
              </div>
              <div class="info-item">
                <strong>Context Version:</strong>
                <span>{context.version}</span>
              </div>
              <div class="info-item">
                <strong>Last Updated:</strong>
                <span>{new Date(context.metadata.last_updated).toLocaleString()}</span>
              </div>
            </div>
          </div>

        {:else if selectedTab === 'knowledge'}
          <div class="section">
            <h3>Knowledge Topics</h3>
            {#each Object.entries(context.knowledge) as [topicName, topic]}
              <div class="topic-card">
                <div class="topic-header">
                  <h4>{topicName.replace(/_/g, ' ')}</h4>
                  <span class="mastery-badge">{topic.mastery_level}</span>
                </div>
                <p class="topic-summary">{topic.summary}</p>
                <div class="topic-meta">
                  <span>Version {topic.version}</span>
                  <span>{topic.entries.length} entries</span>
                  {#if topic.markdown_file}
                    <button class="link-btn" on:click={() => openMarkdownFile(topic.markdown_file)}>
                      View Markdown
                    </button>
                  {/if}
                </div>

                {#if topic.entries.length > 0}
                  <details class="topic-entries">
                    <summary>Show Entries</summary>
                    {#each topic.entries as entry}
                      <div class="entry-item">
                        <p>{entry.content}</p>
                        {#if entry.tags.length > 0}
                          <div class="tags">
                            {#each entry.tags as tag}
                              <span class="tag-small">{tag}</span>
                            {/each}
                          </div>
                        {/if}
                      </div>
                    {/each}
                  </details>
                {/if}
              </div>
            {:else}
              <p class="empty">No knowledge topics yet. They'll appear here as you approve commits.</p>
            {/each}
          </div>

        {:else if selectedTab === 'instructions'}
          <div class="section">
            <h3>AI Behavior Instructions</h3>
            <div class="instruction-card">
              <strong>Primary Instruction:</strong>
              <p class="instruction-text">{context.instruction.primary}</p>
            </div>

            <h4>Bias Mitigation Settings</h4>
            <div class="settings-grid">
              <div class="setting-item">
                <label>
                  <input
                    type="checkbox"
                    checked={context.instruction.bias_mitigation.require_multi_perspective}
                    disabled
                  />
                  Require Multi-Perspective Analysis
                </label>
              </div>
              <div class="setting-item">
                <label>
                  <input
                    type="checkbox"
                    checked={context.instruction.bias_mitigation.challenge_status_quo}
                    disabled
                  />
                  Challenge Status Quo
                </label>
              </div>
              <div class="setting-item">
                <strong>Cultural Sensitivity:</strong>
                <span>{context.instruction.bias_mitigation.cultural_sensitivity}</span>
              </div>
            </div>

            <div class="info-box">
              <strong>Note:</strong> To edit instructions, modify the personal.json file directly at ~/.dpc/personal.json
            </div>
          </div>

        {:else if selectedTab === 'history'}
          <div class="section">
            <h3>Commit History</h3>
            {#each context.commit_history as commit}
              <div class="commit-card">
                <div class="commit-header">
                  <code class="commit-id">{commit.commit_id}</code>
                  <span class="commit-date">{new Date(commit.timestamp).toLocaleString()}</span>
                </div>
                <p class="commit-message">{commit.message}</p>
              </div>
            {:else}
              <p class="empty">No commits yet. History will appear here as knowledge commits are approved.</p>
            {/each}
          </div>
        {/if}
      </div>

      <div class="modal-footer">
        <button class="btn btn-close" on:click={close}>Close</button>
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

  .tabs {
    display: flex;
    border-bottom: 2px solid #e0e0e0;
    background: #f9f9f9;
  }

  .tab {
    flex: 1;
    padding: 0.75rem 1rem;
    border: none;
    background: none;
    cursor: pointer;
    font-size: 0.95rem;
    color: #666;
    transition: all 0.2s;
  }

  .tab:hover {
    background: #f0f0f0;
  }

  .tab.active {
    color: #1976d2;
    border-bottom: 3px solid #1976d2;
    background: white;
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
    margin: 0 0 1rem 0;
    font-size: 1.2rem;
    color: #333;
  }

  .section h4 {
    margin: 1rem 0 0.75rem 0;
    font-size: 1rem;
    color: #555;
  }

  .info-grid {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .info-item {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }

  .info-item strong {
    color: #666;
    font-size: 0.9rem;
  }

  .info-item span {
    color: #333;
  }

  .tags {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
  }

  .tag {
    background: #e3f2fd;
    color: #0d47a1;
    padding: 0.25rem 0.75rem;
    border-radius: 12px;
    font-size: 0.9rem;
  }

  .tag-small {
    background: #f5f5f5;
    color: #666;
    padding: 0.2rem 0.5rem;
    border-radius: 8px;
    font-size: 0.8rem;
  }

  .version-badge {
    background: #4caf50;
    color: white;
    padding: 0.25rem 0.75rem;
    border-radius: 12px;
    font-size: 0.85rem;
    font-weight: 500;
  }

  .topic-card {
    border: 1px solid #e0e0e0;
    border-radius: 8px;
    padding: 1rem;
    margin-bottom: 1rem;
    background: #fafafa;
  }

  .topic-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.5rem;
  }

  .topic-header h4 {
    margin: 0;
    text-transform: capitalize;
    color: #333;
  }

  .mastery-badge {
    background: #fff3e0;
    color: #e65100;
    padding: 0.25rem 0.75rem;
    border-radius: 12px;
    font-size: 0.85rem;
  }

  .topic-summary {
    color: #666;
    margin: 0.5rem 0;
  }

  .topic-meta {
    display: flex;
    gap: 1rem;
    font-size: 0.85rem;
    color: #999;
    margin-top: 0.5rem;
  }

  .link-btn {
    background: none;
    border: none;
    color: #1976d2;
    cursor: pointer;
    text-decoration: underline;
    padding: 0;
  }

  .topic-entries {
    margin-top: 0.75rem;
  }

  .topic-entries summary {
    cursor: pointer;
    color: #1976d2;
    font-size: 0.9rem;
  }

  .entry-item {
    border-left: 3px solid #e0e0e0;
    padding-left: 0.75rem;
    margin: 0.75rem 0;
  }

  .entry-item p {
    margin: 0 0 0.5rem 0;
    color: #333;
  }

  .instruction-card {
    background: #f5f5f5;
    padding: 1rem;
    border-radius: 6px;
    margin-bottom: 1rem;
  }

  .instruction-text {
    margin: 0.5rem 0 0 0;
    color: #333;
    line-height: 1.5;
  }

  .settings-grid {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    margin-top: 0.75rem;
  }

  .setting-item {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .info-box {
    background: #e3f2fd;
    border-left: 3px solid #1976d2;
    padding: 0.75rem;
    margin-top: 1rem;
    font-size: 0.9rem;
  }

  .commit-card {
    border-left: 3px solid #4caf50;
    padding: 0.75rem;
    margin-bottom: 0.75rem;
    background: #f9f9f9;
  }

  .commit-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.5rem;
  }

  .commit-id {
    background: #333;
    color: #fff;
    padding: 0.25rem 0.5rem;
    border-radius: 4px;
    font-size: 0.85rem;
    font-family: monospace;
  }

  .commit-date {
    font-size: 0.85rem;
    color: #999;
  }

  .commit-message {
    margin: 0;
    color: #333;
  }

  .empty {
    color: #999;
    font-style: italic;
    text-align: center;
    padding: 2rem;
  }

  .modal-footer {
    padding: 1rem 1.5rem;
    border-top: 1px solid #e0e0e0;
    display: flex;
    justify-content: flex-end;
  }

  .btn-close {
    padding: 0.75rem 2rem;
    border: none;
    border-radius: 6px;
    background: #666;
    color: white;
    font-size: 1rem;
    cursor: pointer;
  }

  .btn-close:hover {
    background: #555;
  }
</style>
