<script lang="ts">
  /**
   * AgentPermissionsPanel - Unified panel for editing agent permissions
   * Works for both global settings (dpc_agent) and individual agent profiles
   */
  import { sendCommand, registerPendingWebAuthLogin } from '$lib/coreService';
  import { openPath } from '@tauri-apps/plugin-opener';
  import { onMount, onDestroy } from 'svelte';

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

  // Backend-discovered registry tools (populated on mount via list_all_tools).
  // Lets the panel surface tools that exist in code but aren't in any
  // hardcoded category below — closes the AGENT-TOOL-FIREWALL-DEFAULT-DRIFT
  // gap from S145 backlog (Mike picked Option 2 in S147 chat).
  let allRegisteredTools: Array<{name: string; description: string; default_enabled: boolean; is_restricted: boolean}> = [];

  // Tool definitions by category
  const toolCategories = [
    {
      name: 'File Operations',
      tools: [
        { key: 'read_file', label: 'Read Files', desc: 'Read files from sandbox or absolute extended paths' },
        { key: 'write_file', label: 'Write Files', desc: 'Write files to sandbox or absolute extended paths' },
        { key: 'list_dir', label: 'List Directory', desc: 'List sandbox or absolute extended path contents' },
        { key: 'repo_delete', label: 'Delete Files', desc: 'Delete files/directories in sandbox' },
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
        { key: 'search_web', label: 'Web Search', desc: 'Search the web (DuckDuckGo, Bing, Brave, Google, Yandex, and more)' },
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
        { key: 'knowledge_list', label: 'List Knowledge', desc: 'List knowledge topics' },
        { key: 'memory_search', label: 'Memory Search', desc: 'Search knowledge using hybrid BM25 + semantic search' },
        { key: 'get_task_board', label: 'Progress Board', desc: 'Read task history and learning progress from the shared Agent Progress Board' },
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
      name: 'Messaging Tools (user communication)',
      tools: [
        { key: 'send_user_message', label: 'Send User Message', desc: 'Send Telegram messages to user (agent-initiated)' },
      ]
    },
  ];

  // Set of every tool key already rendered by the hardcoded categories.
  // Anything in allRegisteredTools NOT in this set is shown in a separate
  // "Other Registered Tools" section so new ToolEntry registrations
  // never go invisible (the S145 lesson — see AGENT-TOOL-FIREWALL-DEFAULT-DRIFT).
  const hardcodedToolKeys: Set<string> = new Set(
    toolCategories.flatMap(cat => cat.tools.map((t: { key: string }) => t.key))
  );

  // Derived: tools known to the backend registry but not in any hardcoded category.
  // Reactive — repopulates when allRegisteredTools arrives from list_all_tools.
  $: unmanagedTools = allRegisteredTools.filter(t => !hardcodedToolKeys.has(t.name));

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
  $: if (editMode && editSettings) { ensureSandboxExtensions(); ensureMemorySettings(); }

  // Helper to remove a path
  function removePath(type: 'read_only' | 'read_write', index: number) {
    if (!editSettings?.sandbox_extensions?.[type]) return;
    editSettings.sandbox_extensions[type] = editSettings.sandbox_extensions[type].filter((_: string, i: number) => i !== index);
    editSettings = editSettings;  // Trigger reactivity
  }

  // Initialize memory object if missing
  function ensureMemorySettings() {
    if (!editSettings) return;
    if (!editSettings.memory) {
      editSettings.memory = {
        enabled: false,
        embedding_model: 'BAAI/bge-m3',
        active_recall: true,
      };
    }
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

  // Initialize history object if missing.
  // ARCH-19: max_archived_sessions = 0 means unlimited (keep all archives).
  function ensureHistorySettings() {
    if (!editSettings) return;
    if (!editSettings.history) {
      editSettings.history = {
        preserve_on_reset: true,
        max_archived_sessions: 0,
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

  // Derived: near-limit warning threshold.
  // ARCH-19: max_sessions === 0 means unlimited — no limit to approach.
  $: archiveNearLimit = archiveInfo && archiveInfo.max_sessions > 0
    ? archiveInfo.count >= Math.floor(archiveInfo.max_sessions * 0.8)
    : false;
  $: archivePercent = archiveInfo && archiveInfo.max_sessions > 0
    ? Math.round((archiveInfo.count / archiveInfo.max_sessions) * 100)
    : 0;
  $: archiveUnlimited = archiveInfo ? archiveInfo.max_sessions === 0 : false;

  // ─────────────────────────────────────────────────────────────
  // ADR-028 T8 — Web Auth per-agent UI section
  // ─────────────────────────────────────────────────────────────
  // Parallel to "Configure Extended Paths" but with immediate-action
  // semantics: each add/remove/login is its own WS call, no edit-mode
  // batching. Domain whitelist is read from the firewall via
  // web_auth_list_domains and mutated via web_auth_{add,remove}_domain.
  // Per-agent scope: uses conversationId as agent_id (same convention
  // as the archive commands already in this component).

  type WebAuthEntry = {
    domain: string;
    has_cookies: boolean;
    expires: number | null;
    authenticated_at: string | null;
  };

  let webAuthEntries: WebAuthEntry[] = [];
  let webAuthLoading: boolean = false;
  let webAuthError: string = '';
  let newWebAuthDomain: string = '';
  let webAuthBusy: Record<string, boolean> = {};
  let _webAuthMessageHandler: ((event: MessageEvent) => void) | null = null;

  async function loadWebAuthDomains() {
    if (isGlobal || !conversationId) return;
    webAuthLoading = true;
    webAuthError = '';
    try {
      const result: any = await sendCommand('web_auth_list_domains',
        { agent_id: conversationId });
      if (result && result.status === 'success') {
        webAuthEntries = result.domains || [];
      } else {
        webAuthError = result?.message || 'failed to load domains';
      }
    } catch (e: any) {
      webAuthError = String(e?.message || e);
    } finally {
      webAuthLoading = false;
    }
  }

  async function addWebAuthDomain() {
    const domain = newWebAuthDomain.trim().toLowerCase();
    if (!domain) return;
    webAuthError = '';
    try {
      const result: any = await sendCommand('web_auth_add_domain',
        { agent_id: conversationId, domain });
      if (result && result.status === 'success') {
        newWebAuthDomain = '';
        await loadWebAuthDomains();
      } else {
        webAuthError = result?.message || 'failed to add domain';
      }
    } catch (e: any) {
      webAuthError = String(e?.message || e);
    }
  }

  async function removeWebAuthDomain(domain: string) {
    webAuthBusy = { ...webAuthBusy, [domain]: true };
    webAuthError = '';
    try {
      const result: any = await sendCommand('web_auth_remove_domain',
        { agent_id: conversationId, domain });
      if (result && result.status === 'success') {
        await loadWebAuthDomains();
      } else {
        webAuthError = result?.message || 'failed to remove domain';
      }
    } catch (e: any) {
      webAuthError = String(e?.message || e);
    } finally {
      const { [domain]: _, ...rest } = webAuthBusy;
      webAuthBusy = rest;
    }
  }

  async function loginWebAuthDomain(domain: string) {
    webAuthBusy = { ...webAuthBusy, [domain]: true };
    webAuthError = '';
    try {
      // Register the (agent_id, domain) pair so the central Tauri
      // event listener in coreService.ts can route the resulting
      // cookies to the right agent (see ADR-028 Wiring).
      registerPendingWebAuthLogin(conversationId, domain);
      const { invoke } = await import('@tauri-apps/api/core');
      const result = await invoke('web_auth_open_login_window', { domain });
      // The Rust scaffold may currently return Err with a TODO hint —
      // surface it to the user rather than silently swallowing.
      if (typeof result === 'string' && result.includes('TODO')) {
        webAuthError = `WebView2 cookie extraction not yet implemented: ${result}`;
      }
    } catch (e: any) {
      webAuthError = `login popup failed: ${String(e?.message || e)}`;
    } finally {
      const { [domain]: _, ...rest } = webAuthBusy;
      webAuthBusy = rest;
    }
  }

  // Re-load whenever the selected agent changes
  $: if (!isGlobal && conversationId) {
    loadWebAuthDomains();
  }

  // Subscribe to backend broadcasts so the UI reflects vault changes
  // initiated elsewhere (Tauri popup completion, JSON edit on disk,
  // another panel).
  onMount(() => {
    _webAuthMessageHandler = (event: MessageEvent) => {
      try {
        const data = JSON.parse(event.data);
        if (data.event === 'web_auth_status_changed' ||
            data.event === 'web_auth_domains_changed' ||
            data.event === 'firewall_rules_updated') {
          loadWebAuthDomains();
        }
      } catch (_) {
        // not JSON — ignore
      }
    };
    window.addEventListener('message', _webAuthMessageHandler);

    // Query the backend for the full ToolRegistry list. Backend restart
    // is what brings new ToolEntry registrations into the registry, so
    // a single startup-time fetch is sufficient (no live reactive sync
    // — Mike's S147 simplification).
    (async () => {
      try {
        const result = sendCommand('list_all_tools', {});
        if (result === false) {
          // Socket not open yet — graceful skip; AgentPermissionsPanel
          // remounts after connection, the next mount will retry.
          return;
        }
        const resp = await (result as Promise<any>);
        if (resp?.status === 'success' && Array.isArray(resp.tools)) {
          allRegisteredTools = resp.tools;
        }
      } catch (e) {
        console.warn('[AgentPermissionsPanel] list_all_tools failed:', e);
        // Graceful fallback — panel works without the dynamic section.
      }
    })();
  });

  onDestroy(() => {
    if (_webAuthMessageHandler) {
      window.removeEventListener('message', _webAuthMessageHandler);
      _webAuthMessageHandler = null;
    }
  });

  function formatExpiry(unix: number | null): string {
    // Some cookie frameworks emit `expires: 0` as a session-cookie
    // sentinel (Ark S140 [#89] review). Treat the same as null so the
    // UI doesn't show "expired" for a cookie that's really session-only.
    if (unix === null || unix === undefined || unix === 0) return 'session-only';
    const ms = unix * 1000;
    const now = Date.now();
    if (ms <= now) return 'expired';
    const days = Math.round((ms - now) / 86_400_000);
    if (days >= 1) return `${days}d`;
    const hours = Math.round((ms - now) / 3_600_000);
    if (hours >= 1) return `${hours}h`;
    return '<1h';
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
          <label>
            {#if editMode && editSettings}
              <input type="checkbox" bind:checked={editSettings.human_knowledge_access} />
            {:else}
              <input type="checkbox" checked={displaySettings.human_knowledge_access} disabled />
            {/if}
            <span>Human Knowledge Access</span>
          </label>
        </div>
      </div>

      {#if !isGlobal}
        <!-- Memory Settings Section (ADR-010) -->
        <div class="subsection">
          <h4>Memory Settings</h4>
          <p class="help-text-small">Configure agent memory system (hybrid search, Active Recall)</p>

          <div class="setting-item">
            <label>
              {#if editMode && editSettings?.memory}
                <input
                  type="checkbox"
                  id="agent-memory-enabled"
                  bind:checked={editSettings.memory.enabled}
                />
              {:else}
                <input
                  type="checkbox"
                  id="agent-memory-enabled-display"
                  checked={displaySettings.memory?.enabled || false}
                  disabled
                />
              {/if}
              <span>Enable Memory System</span>
            </label>
            <p class="help-text-small">Enables embedding-based knowledge search and Active Recall hints</p>
          </div>

          <div class="setting-item">
            <label>
              {#if editMode && editSettings?.memory}
                <input
                  type="checkbox"
                  id="agent-memory-active-recall"
                  bind:checked={editSettings.memory.active_recall}
                />
              {:else}
                <input
                  type="checkbox"
                  id="agent-memory-active-recall-display"
                  checked={displaySettings.memory?.active_recall || false}
                  disabled
                />
              {/if}
              <span>Active Recall</span>
            </label>
            <p class="help-text-small">Inject relevant memory hints into agent context automatically</p>
          </div>
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
                  min="0"
                  bind:value={editSettings.history.max_archived_sessions}
                  style="width: 70px; padding: 0.25rem 0.5rem; border: 1px solid #ccc; border-radius: 4px;"
                />
              {:else}
                <span class="value">
                  {#if (displaySettings.history?.max_archived_sessions ?? 0) === 0}
                    Unlimited
                  {:else}
                    {displaySettings.history?.max_archived_sessions}
                  {/if}
                </span>
              {/if}
              <span class="help-text-small" style="margin-left: 0.5rem;">0 = unlimited (keep all archives); any positive value caps retention</span>
            </div>

            {#if archiveInfo && !editMode}
              <!-- Archive status display -->
              <div class="archive-status" style="margin-top: 1rem;">
                <div class="archive-count-row">
                  <span>Archived sessions:</span>
                  <strong style="color: {archiveNearLimit ? 'var(--warning, #f59e0b)' : 'var(--text-primary)'}">
                    {#if archiveUnlimited}
                      {archiveInfo.count} &nbsp;(unlimited retention)
                    {:else}
                      {archiveInfo.count}/{archiveInfo.max_sessions}
                      {#if archiveNearLimit} &nbsp;⚠ approaching limit{/if}
                    {/if}
                  </strong>
                </div>

                {#if !archiveUnlimited}
                  <!-- Progress bar (hidden when retention is unlimited) -->
                  <div class="archive-progress-track" title="{archivePercent}% of limit used">
                    <div
                      class="archive-progress-fill"
                      style="width: {Math.min(archivePercent, 100)}%; background: {archiveNearLimit ? 'var(--warning, #f59e0b)' : 'var(--primary, #2196F3)'};"
                    ></div>
                  </div>
                {/if}

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

        <!--
          Unmanaged tools — anything in the ToolRegistry not covered by
          the hardcoded categories above. New ToolEntry registrations
          land here automatically after backend restart so they're never
          invisible. Toggling them writes to privacy_rules.json via the
          same editSettings.tools path; on next save they're "managed".
          Backlog: AGENT-TOOL-FIREWALL-DEFAULT-DRIFT.
        -->
        {#if unmanagedTools.length > 0}
          <h5 style="margin-top: 1rem; margin-bottom: 0.5rem; color: var(--text-secondary);">
            Other Registered Tools
            <span class="help-text-small" style="font-weight: normal;">
              ({unmanagedTools.length} discovered from backend registry, not in categories above)
            </span>
          </h5>
          <div class="notification-events">
            {#each unmanagedTools as tool}
              <div class="notification-event-item">
                {#if editMode && editSettings?.tools}
                  <label for="agent-tool-{tool.name}">
                    <input
                      type="checkbox"
                      id="agent-tool-{tool.name}"
                      bind:checked={editSettings.tools[tool.name]}
                    />
                    <div>
                      <span class="event-name" style={tool.is_restricted ? 'color: var(--danger);' : ''}>
                        {tool.name}
                        {#if tool.default_enabled}
                          <span class="help-text-small" style="font-weight: normal; margin-left: 0.5rem;">(recommended: on)</span>
                        {/if}
                        {#if !(tool.name in (editSettings?.tools ?? {}))}
                          <span class="help-text-small" style="font-weight: normal; margin-left: 0.5rem; color: var(--text-secondary);" title="Not in privacy_rules.json yet — toggle to persist">⚠ unmanaged</span>
                        {/if}
                      </span>
                      <p class="help-text-small" style="margin: 0;">{tool.description || 'No description'}</p>
                    </div>
                  </label>
                {:else}
                  <label for="agent-tool-{tool.name}">
                    <input
                      type="checkbox"
                      id="agent-tool-{tool.name}"
                      checked={displaySettings?.tools?.[tool.name] ?? false}
                      disabled
                    />
                    <div>
                      <span class="event-name" style={tool.is_restricted ? 'color: var(--danger);' : ''}>
                        {tool.name}
                        {#if tool.default_enabled}
                          <span class="help-text-small" style="font-weight: normal; margin-left: 0.5rem;">(recommended: on)</span>
                        {/if}
                        {#if !(tool.name in (displaySettings?.tools ?? {}))}
                          <span class="help-text-small" style="font-weight: normal; margin-left: 0.5rem; color: var(--text-secondary);" title="Not in privacy_rules.json yet — toggle to persist">⚠ unmanaged</span>
                        {/if}
                      </span>
                      <p class="help-text-small" style="margin: 0;">{tool.description || 'No description'}</p>
                    </div>
                  </label>
                {/if}
              </div>
            {/each}
          </div>
        {/if}

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
                      <label class="index-toggle" title="Index this path for agent memory search">
                        <input
                          type="checkbox"
                          checked={(editSettings.sandbox_extensions.indexed_paths || []).includes(editSettings.sandbox_extensions.read_only[i])}
                          on:change={(e) => {
                            if (!editSettings.sandbox_extensions.indexed_paths) editSettings.sandbox_extensions.indexed_paths = [];
                            const p = editSettings.sandbox_extensions.read_only[i];
                            const checked = (e.target as HTMLInputElement).checked;
                            if (checked) {
                              if (!editSettings.sandbox_extensions.indexed_paths.includes(p)) editSettings.sandbox_extensions.indexed_paths = [...editSettings.sandbox_extensions.indexed_paths, p];
                            } else {
                              editSettings.sandbox_extensions.indexed_paths = editSettings.sandbox_extensions.indexed_paths.filter((x: string) => x !== p);
                            }
                          }}
                        />
                        <span class="index-label">Index</span>
                      </label>
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

              <!-- Excluded Directories for Indexing -->
              {#if (editSettings.sandbox_extensions.indexed_paths || []).length > 0}
              <div class="path-group-card">
                <div class="path-group-header">
                  <span class="path-label">🚫 Excluded Directories</span>
                </div>
                <p class="help-text-small">Directories skipped during indexing (one per line). Defaults: node_modules, .git, __pycache__, etc.</p>
                <textarea
                  class="excluded-dirs-input"
                  placeholder="node_modules&#10;.git&#10;__pycache__&#10;dist&#10;build"
                  value={(editSettings.sandbox_extensions.excluded_dirs || []).join('\n')}
                  on:input={(e) => {
                    const val = (e.target as HTMLTextAreaElement).value;
                    editSettings.sandbox_extensions.excluded_dirs = val.trim() ? val.split('\n').map((s: string) => s.trim()).filter((s: string) => s) : undefined;
                  }}
                  rows="4"
                ></textarea>
                <p class="help-text-small" style="margin-top: 4px; opacity: 0.6;">Leave empty to use defaults. Custom list replaces defaults entirely.</p>
              </div>
              {/if}

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
                      <li>{path} {#if (displaySettings.sandbox_extensions.indexed_paths || []).includes(path)}<span class="indexed-badge">📇 Indexed</span>{/if}</li>
                    {/each}
                  </ul>
                </div>
              {/if}
              {#if (displaySettings.sandbox_extensions.excluded_dirs?.length ?? 0) > 0}
                <div class="path-section">
                  <span class="path-label">🚫 Excluded Directories</span>
                  <ul class="path-list">
                    {#each displaySettings.sandbox_extensions.excluded_dirs || [] as dir}
                      <li>{dir}</li>
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

          <!-- ADR-028 T8: Web Authentication (per-agent) -->
          <h5 style="margin-top: 1.5rem; margin-bottom: 0.5rem; color: var(--text-secondary);">🔐 Web Authentication</h5>
          <p class="help-text-small" style="margin-bottom: 0.5rem;">
            Domains the agent can authenticate to via <code>browse_page(use_auth=...)</code>.
            Each domain requires (a) being on this whitelist and (b) a completed login via the popup below.
            Removing a domain also revokes any stored cookies.
          </p>

          {#if webAuthError}
            <p class="help-text-small" style="color: var(--danger-color, #c0392b); margin-bottom: 0.5rem;">
              ⚠️ {webAuthError}
            </p>
          {/if}

          <div class="sandbox-paths-config">
            <div class="path-group-card">
              <div class="path-group-header">
                <span class="path-label">Authorized domains</span>
              </div>

              <div class="path-list-edit">
                {#each webAuthEntries as entry (entry.domain)}
                  <div class="path-entry">
                    <input
                      type="text"
                      class="path-input"
                      readonly
                      value={entry.domain}
                    />
                    <span class="web-auth-status"
                          style="font-size: 0.85em; padding: 0 0.5rem; white-space: nowrap;
                                 color: {entry.has_cookies ? 'var(--success-color, #27ae60)' : 'var(--text-secondary, #888)'};">
                      {#if entry.has_cookies}
                        ✓ logged in ({formatExpiry(entry.expires)})
                      {:else}
                        ○ not logged in
                      {/if}
                    </span>
                    <button
                      type="button"
                      class="btn-path-add"
                      disabled={webAuthBusy[entry.domain]}
                      on:click={() => loginWebAuthDomain(entry.domain)}
                      title={entry.has_cookies ? 'Re-login (replace cookies)' : 'Open login window'}
                    >{entry.has_cookies ? 'Re-login' : 'Login'}</button>
                    <button
                      type="button"
                      class="btn-path-remove"
                      disabled={webAuthBusy[entry.domain]}
                      on:click={() => removeWebAuthDomain(entry.domain)}
                      title="Remove from whitelist + revoke cookies"
                    >×</button>
                  </div>
                {:else}
                  {#if webAuthLoading}
                    <p class="empty-small">Loading…</p>
                  {:else}
                    <p class="empty-small">No web auth domains configured</p>
                  {/if}
                {/each}
              </div>

              <!-- Add new domain -->
              <div class="path-entry" style="margin-top: 0.5rem;">
                <input
                  type="text"
                  class="path-input"
                  placeholder="e.g. example.com"
                  bind:value={newWebAuthDomain}
                  on:keydown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addWebAuthDomain(); } }}
                />
                <button
                  type="button"
                  class="btn-path-add"
                  disabled={!newWebAuthDomain.trim()}
                  on:click={addWebAuthDomain}
                >+ Add</button>
              </div>
            </div>
          </div>
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
      Sandbox paths are configured individually for this agent.
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

  .excluded-dirs-input {
    width: 100%;
    background: var(--bg-secondary, #1e1e1e);
    border: 1px solid var(--border-color, #333);
    border-radius: 4px;
    padding: 0.4rem;
    font-family: monospace;
    font-size: 0.82rem;
    color: var(--text-primary);
    resize: vertical;
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
