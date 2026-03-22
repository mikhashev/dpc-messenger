<script lang="ts">
  /**
   * AgentPermissionsPanel - Unified panel for editing agent permissions
   * Works for both global settings (dpc_agent) and individual agent profiles
   */

  export let displaySettings: any = null;  // Settings for display mode
  export let editSettings: any = null;     // Settings for edit mode (bindable)
  export let editMode: boolean = false;
  export let isGlobal: boolean = false;    // True if editing global dpc_agent settings
  export let agentName: string = '';       // Name of the selected agent (for info text)
  export let hasCustomProfile: boolean = false;  // True if agent has its own profile
  export let onResetToGlobal: (() => void) | undefined = undefined;  // Reset button callback

  // Tool definitions by category
  const toolCategories = [
    {
      name: 'File Operations (sandboxed)',
      tools: [
        { key: 'repo_read', label: 'Read Files', desc: 'Read files in sandbox' },
        { key: 'repo_list', label: 'List Files', desc: 'List directory contents' },
        { key: 'repo_write_commit', label: 'Write Files', desc: 'Create/modify files in sandbox' },
      ]
    },
    {
      name: 'Search Tools (grep-like)',
      tools: [
        { key: 'search_files', label: 'Search Files', desc: 'Search for patterns across multiple files' },
        { key: 'search_in_file', label: 'Search in File', desc: 'Search in specific file with context' },
      ]
    },
    {
      name: 'Extended Sandbox (custom paths)',
      tools: [
        { key: 'extended_path_read', label: 'Extended Read', desc: 'Read from custom paths outside sandbox' },
        { key: 'extended_path_list', label: 'Extended List', desc: 'List directories in custom paths' },
        { key: 'extended_path_write', label: 'Extended Write', desc: 'Write to custom paths (requires read_write)' },
        { key: 'list_extended_sandbox_paths', label: 'List Extended Paths', desc: 'View configured extended paths' },
      ]
    },
    {
      name: 'Web Tools',
      tools: [
        { key: 'search_web', label: 'Web Search', desc: 'Search the web via DuckDuckGo' },
        { key: 'browse_page', label: 'Browse Page', desc: 'Fetch and parse web pages' },
        { key: 'fetch_json', label: 'Fetch JSON', desc: 'Fetch JSON from APIs' },
        { key: 'extract_links', label: 'Extract Links', desc: 'Extract links from pages' },
        { key: 'check_url', label: 'Check URL', desc: 'Check if URL is accessible' },
      ]
    },
    {
      name: 'Memory & Knowledge',
      tools: [
        { key: 'update_scratchpad', label: 'Update Scratchpad', desc: 'Update agent working memory' },
        { key: 'update_identity', label: 'Update Identity', desc: 'Update agent identity' },
        { key: 'deduplicate_identity', label: 'Deduplicate Identity', desc: 'Clean up duplicate sections in identity' },
        { key: 'chat_history', label: 'Chat History', desc: 'Access chat history' },
        { key: 'knowledge_read', label: 'Read Knowledge', desc: 'Read knowledge base' },
        { key: 'knowledge_write', label: 'Write Knowledge', desc: 'Write to knowledge base' },
        { key: 'knowledge_list', label: 'List Knowledge', desc: 'List knowledge topics' },
        { key: 'get_task_board', label: 'Progress Board', desc: 'Read task history and learning progress from the shared Agent Progress Board' },
        { key: 'extract_knowledge', label: 'Extract Knowledge', desc: 'Extract knowledge from conversation' },
        { key: 'get_dpc_context', label: 'Get DPC Context', desc: 'Access DPC personal/device context' },
      ]
    },
    {
      name: 'Git Tools',
      tools: [
        { key: 'git_status', label: 'Git Status', desc: 'Check git status' },
        { key: 'git_diff', label: 'Git Diff', desc: 'Show git diff' },
        { key: 'git_log', label: 'Git Log', desc: 'Show git log' },
        { key: 'git_add', label: 'Git Add', desc: 'Stage files' },
        { key: 'git_commit', label: 'Git Commit', desc: 'Create commits' },
        { key: 'git_branch', label: 'Git Branch', desc: 'List branches' },
        { key: 'git_init', label: 'Git Init', desc: 'Initialize repo' },
        { key: 'repo_commit_push', label: 'Git Push', desc: 'Push to remote' },
      ]
    },
    {
      name: 'Drive Operations (direct filesystem)',
      tools: [
        { key: 'drive_read', label: 'Drive Read', desc: 'Read files from drive' },
        { key: 'drive_list', label: 'Drive List', desc: 'List drive contents' },
        { key: 'drive_write', label: 'Drive Write', desc: 'Write files to drive' },
      ]
    },
    {
      name: 'Review Tools (safe analysis)',
      tools: [
        { key: 'self_review', label: 'Self Review', desc: 'Review own work' },
        { key: 'request_critique', label: 'Request Critique', desc: 'Request feedback' },
        { key: 'compare_approaches', label: 'Compare Approaches', desc: 'Compare solutions' },
        { key: 'quality_checklist', label: 'Quality Checklist', desc: 'Run quality checks' },
        { key: 'consensus_check', label: 'Consensus Check', desc: 'Check consensus' },
      ]
    },
    {
      name: 'Restricted Tools (security sensitive)',
      isDanger: true,
      tools: [
        { key: 'run_shell', label: 'Shell Access', desc: 'Execute shell commands' },
        { key: 'claude_code_edit', label: 'Code Editing', desc: 'Edit code via Claude Code' },
      ]
    },
    {
      name: 'Task Queue Tools (background scheduling)',
      tools: [
        { key: 'schedule_task', label: 'Schedule Task', desc: 'Schedule tasks for future execution' },
        { key: 'get_task_status', label: 'Task Status', desc: 'Check status of scheduled tasks' },
      ]
    },
    {
      name: 'Evolution Tools (agent self-modification)',
      tools: [
        { key: 'pause_evolution', label: 'Pause Evolution', desc: 'Pause automatic evolution cycles' },
        { key: 'resume_evolution', label: 'Resume Evolution', desc: 'Resume paused evolution' },
        { key: 'get_evolution_stats', label: 'Evolution Stats', desc: 'Get evolution statistics' },
        { key: 'approve_evolution_change', label: 'Approve Change', desc: 'Approve pending self-modification (dangerous)', isDanger: true },
        { key: 'reject_evolution_change', label: 'Reject Change', desc: 'Reject pending changes' },
      ]
    },
    {
      name: 'Messaging Tools (user communication)',
      tools: [
        { key: 'send_user_message', label: 'Send User Message', desc: 'Send Telegram messages to user (agent-initiated)' },
      ]
    },
  ];

  // Helper to get sandbox extensions safely
  function getSandboxExtensions(settings: any, type: 'read_only' | 'read_write'): string[] {
    return settings?.sandbox_extensions?.[type] || [];
  }

  // Helper to add a path
  function addPath(type: 'read_only' | 'read_write') {
    if (!editSettings) return;
    if (!editSettings.sandbox_extensions) {
      editSettings.sandbox_extensions = { read_only: [], read_write: [] };
    }
    if (!editSettings.sandbox_extensions[type]) {
      editSettings.sandbox_extensions[type] = [];
    }
    editSettings.sandbox_extensions[type] = [...editSettings.sandbox_extensions[type], ''];
    editSettings = editSettings;  // Trigger reactivity
  }

  // Helper to remove a path
  function removePath(type: 'read_only' | 'read_write', index: number) {
    if (!editSettings?.sandbox_extensions?.[type]) return;
    editSettings.sandbox_extensions[type] = editSettings.sandbox_extensions[type].filter((_: string, i: number) => i !== index);
    editSettings = editSettings;  // Trigger reactivity
  }

  // Initialize evolution object if missing
  function ensureEvolutionSettings() {
    if (!editSettings) return;
    if (!editSettings.evolution) {
      editSettings.evolution = {
        enabled: false,
        interval_minutes: 60,
        auto_apply: false
      };
    }
  }
</script>

{#if displaySettings}
  <div class="compute-settings">
    <!-- Master Enable Toggle -->
    <div class="setting-item">
      <label>
        {#if editMode && editSettings}
          <input
            type="checkbox"
            id="agent-enabled"
            bind:checked={editSettings.enabled}
          />
        {:else}
          <input
            type="checkbox"
            id="agent-enabled-display"
            checked={displaySettings.enabled}
            disabled
          />
        {/if}
        <strong>Enable DPC Agent</strong>
      </label>
      <p class="help-text-small">Master toggle for the embedded autonomous AI agent</p>
    </div>

    {#if displaySettings.enabled}
      <!-- Context Access Section -->
      <div class="subsection">
        <h4>Context Access</h4>
        <p class="help-text-small">Control which DPC context the agent can read</p>

        <div class="setting-item">
          <label>
            {#if editMode && editSettings}
              <input
                type="checkbox"
                id="agent-personal"
                bind:checked={editSettings.personal_context_access}
              />
            {:else}
              <input
                type="checkbox"
                checked={displaySettings.personal_context_access}
                disabled
              />
            {/if}
            <span>Personal Context Access</span>
          </label>
        </div>

        <div class="setting-item">
          <label>
            {#if editMode && editSettings}
              <input
                type="checkbox"
                id="agent-device"
                bind:checked={editSettings.device_context_access}
              />
            {:else}
              <input
                type="checkbox"
                checked={displaySettings.device_context_access}
                disabled
              />
            {/if}
            <span>Device Context Access</span>
          </label>
        </div>

        <div class="setting-item">
          <span><strong>Knowledge Access:</strong></span>
          {#if editMode && editSettings}
            <select bind:value={editSettings.knowledge_access}>
              <option value="none">None</option>
              <option value="read_only">Read Only</option>
              <option value="read_write">Read & Write</option>
            </select>
          {:else}
            <span class="value">{displaySettings.knowledge_access}</span>
          {/if}
        </div>
      </div>

      {#if !isGlobal}
        <!-- Evolution Settings Section (per-agent) -->
        <div class="subsection">
          <h4>Evolution Settings</h4>
          <p class="help-text-small">Configure autonomous self-modification behavior</p>

          <div class="setting-item">
            <label>
              {#if editMode && editSettings?.evolution}
                <input
                  type="checkbox"
                  id="agent-evolution-enabled"
                  bind:checked={editSettings.evolution.enabled}
                />
              {:else}
                <input
                  type="checkbox"
                  id="agent-evolution-enabled-display"
                  checked={displaySettings.evolution?.enabled || false}
                  disabled
                />
              {/if}
              <span>Enable Evolution</span>
            </label>
            <p class="help-text-small">Allow agent to autonomously improve itself within sandbox</p>
          </div>

          {#if (editMode ? editSettings?.evolution?.enabled : displaySettings.evolution?.enabled)}
            <div class="setting-item" style="margin-top: 0.75rem;">
              <span><strong>Interval (minutes):</strong></span>
              {#if editMode && editSettings?.evolution}
                {#if !editSettings.evolution}
                  {@html ''}
                {/if}
                <input
                  type="number"
                  id="agent-evolution-interval"
                  min="1"
                  max="1440"
                  bind:value={editSettings.evolution.interval_minutes}
                  style="width: 80px; padding: 0.25rem 0.5rem; border: 1px solid #ccc; border-radius: 4px;"
                />
              {:else}
                <span class="value">{displaySettings.eolution?.interval_minutes || 60}</span>
              {/if}
              <span class="help-text-small" style="margin-left: 0.5rem;">Time between evolution cycles</span>
            </div>

            <div class="setting-item" style="margin-top: 0.5rem;">
              <label>
                {#if editMode && editSettings?.evolution}
                  <input
                    type="checkbox"
                    id="agent-evolution-auto-apply"
                    bind:checked={editSettings.evolution.auto_apply}
                  />
                {:else}
                  <input
                    type="checkbox"
                    id="agent-evolution-auto-apply-display"
                    checked={displaySettings.evolution?.auto_apply || false}
                    disabled
                  />
                {/if}
                <span>Auto-Apply Changes</span>
              </label>
              <p class="help-text-small">If disabled, changes require manual approval</p>
            </div>
          {:else if editMode && editSettings}
            <!-- Initialize evolution object when user enables it -->
            <button
              type="button"
              class="add-path-btn"
              on:click={ensureEvolutionSettings}
              style="display: none;"
            >Initialize Evolution</button>
          {/if}

          {#if !editMode}
            <div class="info-box" style="margin-top: 0.75rem; padding: 0.5rem;">
              <strong>Evolution</strong> allows the agent to modify its own memory files
              (identity.md, scratchpad.md, knowledge/*.md) within the ~/.dpc/agents/AGENT_ID/ sandbox.
              When auto-apply is disabled, you must manually approve each change.
            </div>
          {/if}
        </div>
      {/if}

      <!-- Tool Permissions Section -->
      <div class="subsection">
        <h4>Tool Permissions</h4>
        <p class="help-text-small">Control which tools the agent can use (enable/disable individually)</p>

        {#each toolCategories as category}
          <h5 style="margin-top: 1rem; margin-bottom: 0.5rem; color: {category.isDanger ? 'var(--danger)' : 'var(--text-secondary)'};">
            {category.name}
          </h5>
          <div class="notification-events">
            {#each category.tools as tool}
              <div class="notification-event-item">
                {#if editMode && editSettings?.tools}
                  <label for="agent-tool-{tool.key}">
                    <input
                      type="checkbox"
                      id="agent-tool-{tool.key}"
                      bind:checked={editSettings.tools[tool.key]}
                    />
                    <div>
                      <span class="event-name" style={'isDanger' in tool && tool.isDanger ? 'color: var(--danger);' : ''}>{tool.label}</span>
                      <p class="help-text-small" style="margin: 0;">{tool.desc}</p>
                    </div>
                  </label>
                {:else}
                  <label for="agent-tool-{tool.key}">
                    <input
                      type="checkbox"
                      id="agent-tool-{tool.key}"
                      checked={displaySettings.tools?.[tool.key]}
                      disabled
                    />
                    <div>
                      <span class="event-name" style={'isDanger' in tool && tool.isDanger ? 'color: var(--danger);' : ''}>{tool.label}</span>
                      <p class="help-text-small" style="margin: 0;">{tool.desc}</p>
                    </div>
                  </label>
                {/if}
              </div>
            {/each}
          </div>
        {/each}

        {#if !isGlobal}
          <!-- Sandbox Path Configuration (per-agent) -->
          <h5 style="margin-top: 1rem; margin-bottom: 0.5rem; color: var(--text-secondary);">Configure Extended Paths</h5>
          <p class="help-text-small" style="margin-bottom: 0.5rem;">Add directories outside the default sandbox that the agent can access</p>

          {#if editMode && editSettings}
            <div class="sandbox-paths-config">
              <!-- Read-Only Paths -->
              <div class="path-section">
                <span class="path-label">📖 Read-Only Paths</span>
                <p class="help-text-small">Agent can read but not modify files in these directories</p>

                {#each getSandboxExtensions(editSettings, 'read_only') as path, i}
                  <div class="path-entry">
                    <input
                      type="text"
                      class="path-input"
                      bind:value={editSettings.sandbox_extensions.read_only[i]}
                      placeholder="C:\Users\you\Documents\notes"
                    />
                    <button
                      type="button"
                      class="remove-path-btn"
                      on:click={() => removePath('read_only', i)}
                    >✕</button>
                  </div>
                {/each}
                <button
                  type="button"
                  class="add-path-btn"
                  on:click={() => addPath('read_only')}
                >+ Add Read-Only Path</button>
              </div>

              <!-- Read-Write Paths -->
              <div class="path-section" style="margin-top: 1rem;">
                <span class="path-label">✏️ Read-Write Paths</span>
                <p class="help-text-small">Agent can read and modify files in these directories</p>

                {#each getSandboxExtensions(editSettings, 'read_write') as path, i}
                  <div class="path-entry">
                    <input
                      type="text"
                      class="path-input"
                      bind:value={editSettings.sandbox_extensions.read_write[i]}
                      placeholder="C:\Users\you\projects\myapp"
                    />
                    <button
                      type="button"
                      class="remove-path-btn"
                      on:click={() => removePath('read_write', i)}
                    >✕</button>
                  </div>
                {/each}
                <button
                  type="button"
                  class="add-path-btn"
                  on:click={() => addPath('read_write')}
                >+ Add Read-Write Path</button>
              </div>
            </div>
          {:else if displaySettings?.sandbox_extensions}
            <div class="sandbox-paths-display">
              {#if (displaySettings.sandbox_extensions.read_only?.length ?? 0) > 0}
                <div class="path-section">
                  <span class="path-label">📖 Read-Only Paths</span>
                  <ul class="path-list">
                    {#each displaySettings.sandbox_extensions.read_only || [] as path}
                      <li>{path}</li>
                    {/each}
                  </ul>
                </div>
              {/if}
              {#if (displaySettings.sandbox_extensions.read_write?.length ?? 0) > 0}
                <div class="path-section">
                  <span class="path-label">✏️ Read-Write Paths</span>
                  <ul class="path-list">
                    {#each displaySettings.sandbox_extensions.read_write || [] as path}
                      <li>{path}</li>
                    {/each}
                  </ul>
                </div>
              {/if}
              {#if !displaySettings.sandbox_extensions.read_only?.length && !displaySettings.sandbox_extensions.read_write?.length}
                <p class="help-text-small" style="font-style: italic;">No extended paths configured</p>
              {/if}
            </div>
          {:else}
            <p class="help-text-small" style="font-style: italic;">No extended paths configured</p>
          {/if}
        {/if}
      </div>
    {/if}
  </div>
{:else}
  <p class="empty">Agent settings not found.</p>
{/if}

{#if !editMode}
  <div class="info-box" style="margin-top: 1.5rem">
    {#if isGlobal}
      <strong>Info:</strong> These are the <strong>global default settings</strong> for all DPC agents.
      Individual agents can override these settings with their own profiles.
      File operations are always sandboxed to ~/.dpc/agents/AGENT_ID/.
      Shell access and code editing are disabled by default for security.
    {:else if hasCustomProfile}
      <strong>Info:</strong> These are <strong>custom settings</strong> for agent <strong>{agentName || 'this agent'}</strong>.
      This agent overrides the global defaults with its own profile.
      Evolution and sandbox paths are configured individually for this agent.
      File operations are always sandboxed to ~/.dpc/agents/AGENT_ID/
    {:else}
      <strong>Info:</strong> Agent <strong>{agentName || 'this agent'}</strong> is <strong>inheriting global settings</strong>.
      Any edits will create a custom profile for this agent.
      File operations are always sandboxed to ~/.dpc/agents/AGENT_ID/
    {/if}
  </div>
{/if}

{#if !isGlobal && hasCustomProfile && onResetToGlobal && !editMode}
  <div class="reset-section" style="margin-top: 0.5rem; padding: 0.75rem; background: var(--bg-secondary); border-radius: 8px; border-left: 4px solid var(--warning)">
    <button
      type="button"
      class="btn-reset"
      on:click={onResetToGlobal}
      style="padding: 0.5rem 1rem; background: var(--warning); color: white; border: none; border-radius: 4px; cursor: pointer; font-weight: 500;"
    >
      Reset to Global Settings
    </button>
    <p class="help-text-small" style="margin-top: 0.5rem;">
      Remove custom profile and inherit from global defaults
    </p>
  </div>
{/if}

<style>
  .compute-settings {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .subsection {
    margin-top: 1rem;
    padding-left: 1rem;
    border-left: 3px solid var(--border-color);
  }

  .subsection h4 {
    margin-bottom: 0.5rem;
  }

  .setting-item {
    margin-bottom: 0.75rem;
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .setting-item label {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    cursor: pointer;
  }

  .help-text-small {
    font-size: 0.85rem;
    color: var(--text-secondary);
    margin: 0;
  }

  .notification-events {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .notification-event-item {
    padding: 0.5rem;
    background: var(--bg-primary);
    border-radius: 4px;
    border: 1px solid var(--border-color);
  }

  .notification-event-item label {
    display: flex;
    align-items: flex-start;
    gap: 0.75rem;
    cursor: pointer;
  }

  .event-name {
    font-weight: 500;
  }

  .info-box {
    background: var(--bg-tertiary);
    padding: 1rem;
    border-radius: 8px;
    border-left: 4px solid var(--primary);
    font-size: 0.9rem;
  }

  .empty {
    color: var(--text-secondary);
    font-style: italic;
    text-align: center;
    padding: 2rem;
  }

  .value {
    font-weight: 500;
    padding: 0.25rem 0.5rem;
    background: var(--bg-tertiary);
    border-radius: 4px;
  }

  /* Sandbox path styles */
  .sandbox-paths-config {
    background: var(--bg-primary);
    padding: 1rem;
    border-radius: 8px;
    border: 1px solid var(--border-color);
    margin-top: 0.5rem;
  }

  .path-section {
    margin-bottom: 1rem;
  }

  .path-label {
    font-weight: 600;
    display: block;
    margin-bottom: 0.25rem;
  }

  .path-entry {
    display: flex;
    gap: 0.5rem;
    margin-bottom: 0.5rem;
  }

  .path-input {
    flex: 1;
    padding: 0.5rem;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    background: var(--bg-secondary);
    color: var(--text-primary);
  }

  .add-path-btn {
    padding: 0.5rem 1rem;
    background: var(--primary);
    color: white;
    border: none;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.9rem;
  }

  .add-path-btn:hover {
    background: var(--primary-dark);
  }

  .remove-path-btn {
    padding: 0.5rem;
    background: var(--danger);
    color: white;
    border: none;
    border-radius: 4px;
    cursor: pointer;
  }

  .remove-path-btn:hover {
    opacity: 0.8;
  }

  .sandbox-paths-display {
    background: var(--bg-primary);
    padding: 1rem;
    border-radius: 8px;
    border: 1px solid var(--border-color);
    margin-top: 0.5rem;
  }

  .path-list {
    margin: 0.5rem 0 0 1.5rem;
    padding: 0;
  }

  .path-list li {
    margin-bottom: 0.25rem;
    font-family: monospace;
    font-size: 0.9rem;
  }

  select {
    padding: 0.25rem 0.5rem;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    background: var(--bg-secondary);
    color: var(--text-primary);
  }
</style>
