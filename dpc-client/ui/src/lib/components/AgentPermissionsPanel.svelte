<script lang="ts">
  /**
   * AgentPermissionsPanel - Unified panel for editing agent permissions
   * Works for both global settings (dpc_agent) and individual agent profiles
   */
  import { sendCommand } from '$lib/coreService';
  import { openPath } from '@tauri-apps/plugin-opener';

  export let displaySettings: any = null;  // Settings for display mode
  export let editSettings: any = null;     // Settings for edit mode (bindable)
  export let editMode: boolean = false;
  export let isGlobal: boolean = false;    // True if editing global dpc_agent settings
  export let agentName: string = '';       // Name of the selected agent (for info text)
  export let hasCustomProfile: boolean = false;  // True if agent has its own profile
  export let onResetToGlobal: (() => void) | undefined = undefined;  // Reset button callback
  // Archive status passed from FirewallEditor after get_session_archive_info
  export let archiveInfo: { count: number; max_sessions: number; archive_path: string; sessions: any[] } | null = null;
  export let conversationId: string = '';  // agent_id used as conversation_id for archive commands

  // Tool definitions by category
  const toolCategories = [
    {
      name: 'File Operations',
      tools: [
        { key: 'read_file', label: 'Read Files', desc: 'Read files from agent sandbox' },
        { key: 'write_file', label: 'Write Files', desc: 'Write files to agent sandbox' },
        { key: 'repo_list', label: 'List Files', desc: 'List directory contents' },
        { key: 'repo_delete', label: 'Delete Files', desc: 'Delete files/directories in sandbox' },
        { key: 'extended_path_list', label: 'Extended List', desc: 'List directories in custom paths' },
        { key: 'list_extended_sandbox_paths', label: 'List Extended Paths', desc: 'View configured extended paths' },
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
        { key: 'get_proposal_result', label: 'Get Proposal Result', desc: 'Poll for knowledge commit result after voting (required for Knowledge→Git linking)' },
        { key: 'execute_skill', label: 'Execute Skill', desc: 'Load and follow skill strategies (Memento-Skills router — Read phase)' },
        { key: 'list_local_agents', label: 'List Local Agents', desc: 'List all agents registered on this device (read-only)' },
        { key: 'list_agent_skills', label: 'List Agent Skills', desc: 'Browse shareable skills of another local agent (read-only)' },
        { key: 'import_skill_from_agent', label: 'Import Agent Skill', desc: 'Copy a skill from another local agent (requires accept_peer_skills to be enabled)' },
        { key: 'list_my_tools', label: 'List My Tools', desc: 'List own available tools filtered by current permissions (read-only introspection)' },
        { key: 'list_my_skills', label: 'List My Skills', desc: 'List own installed skills from skill store (read-only introspection)' },
        { key: 'get_dpc_context', label: 'Get DPC Context', desc: 'Access DPC personal/device context' },
      ]
    },
    {
      name: 'Git Tools',
      tools: [
        { key: 'git_status', label: 'Git Status', desc: 'Check git status (read-only)' },
        { key: 'git_diff', label: 'Git Diff', desc: 'Show git diff (read-only)' },
        { key: 'git_log', label: 'Git Log', desc: 'Show commit history (read-only)' },
        { key: 'git_branch', label: 'Git Branch', desc: 'List branches (read-only)' },
        { key: 'git_add', label: 'Git Add', desc: 'Stage files for commit' },
        { key: 'git_commit', label: 'Git Commit', desc: 'Create commits (enforces Conventional Commits format)' },
        { key: 'git_init', label: 'Git Init', desc: 'Initialize repo (auto-runs on agent creation)' },
        { key: 'git_checkout', label: 'Git Checkout', desc: 'Switch branches or create new ones' },
        { key: 'git_merge', label: 'Git Merge', desc: 'Merge a branch into current branch' },
        { key: 'git_tag', label: 'Git Tag', desc: 'Create milestone tags' },
        { key: 'git_reset', label: 'Git Reset', desc: 'Rollback files or commits (hard=true is destructive)', isDanger: true },
        { key: 'git_snapshot', label: 'Git Snapshot', desc: 'Quick save: stage all + commit with UTC timestamp' },
        { key: 'repo_commit_push', label: 'Git Push', desc: 'Push to remote (not used — local only)', isDanger: true },
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
        { key: 'register_task_type', label: 'Register Task Type', desc: 'Register new task type handlers (restricted)' },
        { key: 'list_task_types', label: 'List Task Types', desc: 'List all registered task types' },
        { key: 'unregister_task_type', label: 'Unregister Task Type', desc: 'Remove a registered task type handler (restricted)' },
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
  function ensureSandboxExtensions() {
    if (!editSettings) return;
    if (!editSettings.sandbox_extensions) {
      editSettings.sandbox_extensions = { read_only: [], read_write: [], extended_read_enabled: true, extended_write_enabled: false };
    }
    if (editSettings.sandbox_extensions.extended_read_enabled === undefined) editSettings.sandbox_extensions.extended_read_enabled = true;
    if (editSettings.sandbox_extensions.extended_write_enabled === undefined) editSettings.sandbox_extensions.extended_write_enabled = false;
  }

  function addPath(type: 'read_only' | 'read_write') {
    if (!editSettings) return;
    ensureSandboxExtensions();
    if (!editSettings.sandbox_extensions[type]) {
      editSettings.sandbox_extensions[type] = [];
    }
    editSettings.sandbox_extensions[type] = [...editSettings.sandbox_extensions[type], ''];
    editSettings = editSettings;  // Trigger reactivity
  }

  // Ensure sandbox_extensions has extended access fields when entering edit mode
  $: if (editMode && editSettings) { ensureSandboxExtensions(); }

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

  function ensureConsciousnessSettings() {
    if (!editSettings) return;
    if (!editSettings.consciousness) {
      editSettings.consciousness = {
        enabled: false,
        think_interval_min: 60,
        think_interval_max: 300,
        budget_fraction: 0.1
      };
    }
  }

  // Auto-initialize consciousness settings when entering edit mode
  $: if (editMode && editSettings && !editSettings.consciousness) {
    ensureConsciousnessSettings();
  }

  // Auto-initialize skills when entering edit mode so bind:checked doesn't crash
  $: if (editMode && editSettings && !editSettings.skills) {
    ensureSkillsSettings();
  }

  // Initialize skills object if missing
  function ensureSkillsSettings() {
    if (!editSettings) return;
    if (!editSettings.skills) {
      editSettings.skills = {
        self_modify: true,
        create_new: true,
        rewrite_existing: false,
        accept_peer_skills: false,
        auto_announce_to_dht: false,
      };
    }
  }

  // Auto-initialize history settings when entering edit mode
  $: if (editMode && editSettings && !editSettings.history) {
    ensureHistorySettings();
  }

  // Initialize history object if missing
  function ensureHistorySettings() {
    if (!editSettings) return;
    if (!editSettings.history) {
      editSettings.history = {
        preserve_on_reset: true,
        max_archived_sessions: 40,
      };
    }
  }

  // Archive action state
  let clearingArchives = false;
  let clearArchiveMessage = '';

  async function handleClearArchives() {
    if (!conversationId) return;
    clearingArchives = true;
    clearArchiveMessage = '';
    try {
      const result = await sendCommand('clear_session_archives', { conversation_id: conversationId, keep_latest: 0 });
      if (result.status === 'success') {
        clearArchiveMessage = `Deleted ${result.deleted_count} archive(s).`;
        // Update local archiveInfo count
        if (archiveInfo) archiveInfo = { ...archiveInfo, count: result.remaining ?? 0, sessions: [] };
      } else {
        clearArchiveMessage = `Error: ${result.message}`;
      }
    } catch (e) {
      clearArchiveMessage = `Error: ${e}`;
    } finally {
      clearingArchives = false;
    }
  }

  async function handleViewArchive() {
    if (!archiveInfo?.archive_path) return;
    try {
      await openPath(archiveInfo.archive_path);
    } catch (e) {
      console.error('Failed to open archive folder:', e);
    }
  }

  // Permissions summary (loaded on demand for transparency)
  let permissionsSummary: any = null;
  let permissionsLoading = false;
  let permissionsExpanded = false;

  async function loadPermissionsSummary() {
    if (permissionsSummary || permissionsLoading) return;
    permissionsLoading = true;
    try {
      const agentId = conversationId || 'agent_001';
      const result = await sendCommand('get_agent_permissions', { agent_id: agentId });
      if (result.status === 'ok') {
        permissionsSummary = result;
      }
    } catch (e) {
      console.error('Failed to load permissions summary:', e);
    }
    permissionsLoading = false;
  }

  function togglePermissions() {
    permissionsExpanded = !permissionsExpanded;
    if (permissionsExpanded && !permissionsSummary) {
      loadPermissionsSummary();
    }
  }

  // Derived: near-limit warning threshold
  $: archiveNearLimit = archiveInfo ? archiveInfo.count >= Math.floor(archiveInfo.max_sessions * 0.8) : false;
  $: archivePercent = archiveInfo && archiveInfo.max_sessions > 0
    ? Math.round((archiveInfo.count / archiveInfo.max_sessions) * 100)
    : 0;
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
      <!-- Permissions Summary (transparency) -->
      <div class="subsection">
        <button
          class="section-toggle"
          on:click={togglePermissions}
        >
          {permissionsExpanded ? '▼' : '▶'} Access & Paths
        </button>
        {#if permissionsExpanded}
          {#if permissionsLoading}
            <p class="help-text-small">Loading...</p>
          {:else if permissionsSummary}
            <div class="permissions-summary">
              <div class="perm-group">
                <strong>Agent Root:</strong>
                <code class="perm-path">{permissionsSummary.sandbox_paths?.agent_root || '~/.dpc/agents/agent_001'}</code>
              </div>

              {#if permissionsSummary.sandbox_paths?.read_only?.length > 0}
                <div class="perm-group">
                  <strong>Extended Read-Only Paths:</strong>
                  {#each permissionsSummary.sandbox_paths.read_only as path}
                    <code class="perm-path">{path}</code>
                  {/each}
                </div>
              {/if}

              {#if permissionsSummary.sandbox_paths?.read_write?.length > 0}
                <div class="perm-group">
                  <strong>Extended Read-Write Paths:</strong>
                  {#each permissionsSummary.sandbox_paths.read_write as path}
                    <code class="perm-path">{path}</code>
                  {/each}
                </div>
              {/if}

              <div class="perm-group">
                <strong>Tools:</strong>
                <span>{permissionsSummary.tools?.core_enabled?.length || 0} core</span>
                {#if permissionsSummary.tools?.restricted_enabled?.length > 0}
                  <span class="perm-warn"> + {permissionsSummary.tools.restricted_enabled.length} restricted ({permissionsSummary.tools.restricted_enabled.join(', ')})</span>
                {/if}
              </div>

              <div class="perm-group">
                <strong>Archive Access:</strong>
                <span class={permissionsSummary.archive_access ? 'perm-ok' : 'perm-deny'}>
                  {permissionsSummary.archive_access ? 'Enabled' : 'Disabled'}
                </span>
              </div>
            </div>
          {:else}
            <p class="help-text-small">Unable to load permissions</p>
          {/if}
        {/if}
      </div>

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

      {#if !isGlobal}
        <!-- Consciousness Settings Section (per-agent) -->
        <div class="subsection">
          <h4>Consciousness Settings</h4>
          <p class="help-text-small">Background thinking between user messages</p>

          <div class="setting-item">
            <label>
              {#if editMode && editSettings?.consciousness}
                <input
                  type="checkbox"
                  id="agent-consciousness-enabled"
                  bind:checked={editSettings.consciousness.enabled}
                />
              {:else}
                <input
                  type="checkbox"
                  id="agent-consciousness-enabled-display"
                  checked={displaySettings.consciousness?.enabled || false}
                  disabled
                />
              {/if}
              <span>Enable Consciousness</span>
            </label>
            <p class="help-text-small">Agent reflects and plans between conversations (uses up to 10% of budget)</p>
          </div>

          {#if (editMode ? editSettings?.consciousness?.enabled : displaySettings.consciousness?.enabled)}
            <div class="setting-item" style="margin-top: 0.75rem;">
              <span><strong>Think interval (sec):</strong></span>
              {#if editMode && editSettings?.consciousness}
                <input
                  type="number"
                  id="agent-consciousness-interval-min"
                  min="10"
                  max="600"
                  bind:value={editSettings.consciousness.think_interval_min}
                  style="width: 80px; padding: 0.25rem 0.5rem; border: 1px solid #ccc; border-radius: 4px;"
                />
                <span style="margin: 0 0.25rem;">to</span>
                <input
                  type="number"
                  id="agent-consciousness-interval-max"
                  min="10"
                  max="600"
                  bind:value={editSettings.consciousness.think_interval_max}
                  style="width: 80px; padding: 0.25rem 0.5rem; border: 1px solid #ccc; border-radius: 4px;"
                />
              {:else}
                <span class="value">{displaySettings.consciousness?.think_interval_min || 60} — {displaySettings.consciousness?.think_interval_max || 300}</span>
              {/if}
              <span class="help-text-small" style="margin-left: 0.5rem;">Random interval range between thoughts</span>
            </div>
          {/if}

          {#if !editMode}
            <div class="info-box" style="margin-top: 0.75rem; padding: 0.5rem;">
              <strong>Consciousness</strong> enables the agent to think autonomously between user messages —
              self-reflection, planning, memory consolidation. Budget-capped at 10% of agent budget.
            </div>
          {/if}
        </div>
      {/if}

      {#if !isGlobal}
        <!-- Skills Settings Section (per-agent) -->
        <div class="subsection">
          <h4>Skills Settings</h4>
          <p class="help-text-small">Configure Memento-Skills self-modification behavior (write phase)</p>

          {#each [
            { key: 'self_modify', label: 'Self-Modify Skills', desc: 'Allow agent to append improvements to its skill files after tasks' },
            { key: 'create_new', label: 'Create New Skills', desc: 'Allow agent to create new skill strategies it discovers' },
            { key: 'rewrite_existing', label: 'Rewrite Existing Skills', desc: 'Allow full skill rewrites (not just appends) — higher risk', isDanger: true },
            { key: 'accept_peer_skills', label: 'Accept Peer Skills', desc: 'Allow receiving skill files shared from connected peers', isDanger: true },
            { key: 'auto_announce_to_dht', label: 'Auto-Announce to DHT', desc: 'Automatically announce shareable skills to the DHT network (requires accept_peer_skills)', isDanger: true },
          ] as skillPerm}
            <div class="setting-item">
              <label>
                {#if editMode && editSettings}
                  <input
                    type="checkbox"
                    id="agent-skills-{skillPerm.key}"
                    bind:checked={editSettings.skills[skillPerm.key]}
                  />
                {:else}
                  <input
                    type="checkbox"
                    checked={displaySettings.skills?.[skillPerm.key] || false}
                    disabled
                  />
                {/if}
                <span style={skillPerm.isDanger ? 'color: var(--danger);' : ''}>{skillPerm.label}</span>
              </label>
              <p class="help-text-small">{skillPerm.desc}</p>
            </div>
          {/each}

          {#if !editMode}
            <div class="info-box" style="margin-top: 0.75rem; padding: 0.5rem;">
              <strong>Skills</strong> are markdown strategy files the agent learns to use better over time.
              Self-modify allows appending "Lessons Learned" sections after tasks with ≥5 LLM rounds.
              Peer skills require explicit opt-in and are sandboxed to the agent's storage.
            </div>
          {/if}
        </div>
      {/if}

      {#if !isGlobal}
        <!-- Session History Section (per-agent) -->
        <div class="subsection">
          <h4>Session History</h4>
          <p class="help-text-small">Control whether sessions are preserved when a conversation is reset.</p>

          <div class="setting-item">
            <label>
              {#if editMode && editSettings}
                <input
                  type="checkbox"
                  id="agent-history-preserve"
                  bind:checked={editSettings.history.preserve_on_reset}
                />
              {:else}
                <input
                  type="checkbox"
                  checked={displaySettings.history?.preserve_on_reset ?? true}
                  disabled
                />
              {/if}
              <span>Preserve session history on reset</span>
            </label>
            <p class="help-text-small">Archive the conversation before clearing it so you can review old sessions.</p>
          </div>

          {#if (editMode ? editSettings?.history?.preserve_on_reset : (displaySettings.history?.preserve_on_reset ?? true))}
            <div class="setting-item" style="margin-top: 0.75rem;">
              <span><strong>Max archived sessions:</strong></span>
              {#if editMode && editSettings?.history}
                <input
                  type="number"
                  id="agent-history-max"
                  min="1"
                  max="200"
                  bind:value={editSettings.history.max_archived_sessions}
                  style="width: 70px; padding: 0.25rem 0.5rem; border: 1px solid #ccc; border-radius: 4px;"
                />
              {:else}
                <span class="value">{displaySettings.history?.max_archived_sessions ?? 40}</span>
              {/if}
              <span class="help-text-small" style="margin-left: 0.5rem;">Oldest archives are pruned automatically (1–200)</span>
            </div>

            {#if archiveInfo && !editMode}
              <!-- Archive status display -->
              <div class="archive-status" style="margin-top: 1rem;">
                <div class="archive-count-row">
                  <span>Archived sessions:</span>
                  <strong style="color: {archiveNearLimit ? 'var(--warning, #f59e0b)' : 'var(--text-primary)'}">
                    {archiveInfo.count}/{archiveInfo.max_sessions}
                    {#if archiveNearLimit} &nbsp;⚠ approaching limit{/if}
                  </strong>
                </div>

                <!-- Progress bar -->
                <div class="archive-progress-track" title="{archivePercent}% of limit used">
                  <div
                    class="archive-progress-fill"
                    style="width: {Math.min(archivePercent, 100)}%; background: {archiveNearLimit ? 'var(--warning, #f59e0b)' : 'var(--primary, #2196F3)'};"
                  ></div>
                </div>

                {#if clearArchiveMessage}
                  <p class="help-text-small" style="margin-top: 0.25rem; color: var(--text-secondary);">{clearArchiveMessage}</p>
                {/if}

                <!-- Action buttons -->
                <div class="archive-actions">
                  <button
                    type="button"
                    class="btn-archive-action"
                    on:click={handleViewArchive}
                    title="Open archive folder in file manager"
                  >View archive</button>
                  <button
                    type="button"
                    class="btn-archive-action btn-archive-danger"
                    on:click={handleClearArchives}
                    disabled={clearingArchives || archiveInfo.count === 0}
                    title="Delete all archived sessions"
                  >{clearingArchives ? 'Clearing…' : 'Clear all archives'}</button>
                </div>
              </div>
            {:else if !editMode}
              <p class="help-text-small" style="font-style: italic; margin-top: 0.5rem;">No archive data — select an individual agent to view stats.</p>
            {/if}
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

          <!-- Extended path access gates (S31) -->
          <div class="extended-access-gates" style="display: flex; gap: 1.5rem; margin-bottom: 0.75rem;">
            <label style="display: flex; align-items: center; gap: 0.4rem; cursor: pointer;">
              {#if editMode && editSettings}
                <input type="checkbox" bind:checked={editSettings.sandbox_extensions.extended_read_enabled} />
              {:else}
                <input type="checkbox" checked={displaySettings?.sandbox_extensions?.extended_read_enabled ?? true} disabled />
              {/if}
              <span>Enable extended path reading</span>
            </label>
            <label style="display: flex; align-items: center; gap: 0.4rem; cursor: pointer;">
              {#if editMode && editSettings}
                <input type="checkbox" bind:checked={editSettings.sandbox_extensions.extended_write_enabled} />
              {:else}
                <input type="checkbox" checked={displaySettings?.sandbox_extensions?.extended_write_enabled ?? false} disabled />
              {/if}
              <span>Enable extended path writing</span>
            </label>
          </div>

          {#if editMode && editSettings}
            <div class="sandbox-paths-config">
              <!-- Read-Only Paths -->
              <div class="path-group-card">
                <div class="path-group-header">
                  <span class="path-label">📖 Read-Only Paths</span>
                  <button
                    type="button"
                    class="btn-path-add"
                    on:click={() => addPath('read_only')}
                  >+ Add Path</button>
                </div>
                <p class="help-text-small">Agent can read but not modify files in these directories</p>

                <div class="path-list-edit">
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
                        class="btn-path-remove"
                        on:click={() => removePath('read_only', i)}
                        title="Remove path"
                      >×</button>
                    </div>
                  {:else}
                    <p class="empty-small">No paths configured</p>
                  {/each}
                </div>
              </div>

              <!-- Read-Write Paths -->
              <div class="path-group-card">
                <div class="path-group-header">
                  <span class="path-label">✏️ Read-Write Paths</span>
                  <button
                    type="button"
                    class="btn-path-add"
                    on:click={() => addPath('read_write')}
                  >+ Add Path</button>
                </div>
                <p class="help-text-small">Agent can read and modify files in these directories</p>

                <div class="path-list-edit">
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
                        class="btn-path-remove"
                        on:click={() => removePath('read_write', i)}
                        title="Remove path"
                      >×</button>
                    </div>
                  {:else}
                    <p class="empty-small">No paths configured</p>
                  {/each}
                </div>
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
  .section-toggle {
    background: none;
    border: none;
    color: inherit;
    font-size: 1em;
    font-weight: bold;
    cursor: pointer;
    padding: 0;
    text-align: left;
  }
  .permissions-summary {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    padding: 0.5rem;
    background: var(--bg-secondary, #1e1e1e);
    border-radius: 4px;
    font-size: 0.85em;
  }
  .perm-group {
    display: flex;
    flex-wrap: wrap;
    gap: 0.3rem;
    align-items: center;
  }
  .perm-path {
    display: block;
    padding: 2px 6px;
    background: var(--bg-tertiary, #2a2a2a);
    border-radius: 3px;
    font-size: 0.85em;
    word-break: break-all;
  }
  .perm-ok { color: #4caf50; }
  .perm-deny { color: #f44336; }
  .perm-warn { color: #ff9800; }

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

  /* Sandbox path styles — mirrors FirewallEditor Node Groups pattern */
  .sandbox-paths-config {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    margin-top: 0.5rem;
  }

  .path-group-card {
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 0.75rem 1rem;
    background: var(--bg-primary);
  }

  .path-group-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.25rem;
  }

  .path-label {
    font-weight: 600;
  }

  .path-list-edit {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
    margin-top: 0.5rem;
  }

  .path-entry {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.4rem;
    padding: 0.4rem 0.5rem;
    background: var(--bg-secondary);
    border: 1px solid var(--border-color);
    border-radius: 4px;
  }

  .path-input {
    flex: 1;
    background: transparent;
    border: none;
    outline: none;
    font-family: monospace;
    font-size: 0.85rem;
    color: var(--text-primary);
    min-width: 0;
  }

  .btn-path-add {
    padding: 0.2rem 0.6rem;
    font-size: 0.82rem;
    background: var(--primary, #2196F3);
    color: white;
    border: none;
    border-radius: 4px;
    cursor: pointer;
    white-space: nowrap;
  }

  .btn-path-add:hover {
    opacity: 0.85;
  }

  .btn-path-remove {
    background: none;
    border: none;
    cursor: pointer;
    font-size: 1.3rem;
    line-height: 1;
    color: var(--text-secondary, #999);
    padding: 0 0.2rem;
    flex-shrink: 0;
  }

  .btn-path-remove:hover {
    color: var(--danger, #f44336);
  }

  .empty-small {
    font-size: 0.82rem;
    color: var(--text-secondary);
    font-style: italic;
    margin: 0.25rem 0 0 0;
  }

  .sandbox-paths-display {
    background: var(--bg-primary);
    padding: 0.75rem;
    border-radius: 8px;
    border: 1px solid var(--border-color);
    margin-top: 0.5rem;
  }

  .path-list {
    list-style: none;
    margin: 0.25rem 0 0 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }

  .path-list li {
    font-family: monospace;
    font-size: 0.8rem;
    background: var(--bg-secondary);
    border: 1px solid var(--border-color);
    border-radius: 3px;
    padding: 0.2rem 0.5rem;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  select {
    padding: 0.25rem 0.5rem;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    background: var(--bg-secondary);
    color: var(--text-primary);
  }

  /* Session History archive status */
  .archive-status {
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 0.75rem 1rem;
  }

  .archive-count-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.5rem;
    font-size: 0.9rem;
  }

  .archive-progress-track {
    height: 6px;
    background: var(--bg-tertiary, #e5e7eb);
    border-radius: 3px;
    overflow: hidden;
    margin-bottom: 0.75rem;
  }

  .archive-progress-fill {
    height: 100%;
    border-radius: 3px;
    transition: width 0.3s ease;
  }

  .archive-actions {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
  }

  .btn-archive-action {
    padding: 0.3rem 0.8rem;
    font-size: 0.82rem;
    background: var(--primary, #2196F3);
    color: white;
    border: none;
    border-radius: 4px;
    cursor: pointer;
  }

  .btn-archive-action:hover:not(:disabled) {
    opacity: 0.85;
  }

  .btn-archive-action:disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }

  .btn-archive-danger {
    background: var(--danger, #ef4444);
  }
</style>
