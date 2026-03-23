<!-- AgentTaskBoard.svelte -->
<!-- Agent Progress Board: Tasks + Learning tabs -->
<!-- v0.20.0 - Layer 5 Agent System -->

<script lang="ts">
  import { createEventDispatcher, onDestroy } from 'svelte';
  import { sendCommand } from '$lib/coreService';

  export let open: boolean = false;
  export let agentId: string = "agent_001";
  // Called when user clicks "Continue Task X.Y" in Learning tab
  export let onSendToAgent: ((msg: string) => void) | null = null;

  const dispatch = createEventDispatcher();

  // --- Types ---

  type TaskEntry = {
    id: string;
    type: string;
    preview: string;
    status: 'running' | 'scheduled' | 'completed' | 'failed';
    started_at: string | null;
    completed_at: string | null;
    scheduled_at: string | null;
    result_preview: string | null;
    has_full_result?: boolean;
  };

  type TaskData = {
    running: TaskEntry[];
    scheduled: TaskEntry[];
    completed: TaskEntry[];
    failed: TaskEntry[];
  };

  type LearningTask = {
    id: string;
    title: string;
    status: 'completed' | 'in_progress' | 'stalled' | 'pending';
    completed_at: string | null;
    started_at: string | null;
    last_activity: string | null;
    days_stalled: number | null;
    session_summary: string | null;
    next_step: string | null;
  };

  type LearningPhase = {
    title: string;
    tasks: LearningTask[];
  };

  type LearningData = {
    phases: LearningPhase[];
    streak_days: number;
    last_session: string | null;
  };

  // --- State ---

  let activeTab: 'tasks' | 'learning' = 'tasks';
  let taskData: TaskData | null = null;
  let learningData: LearningData | null = null;
  let tasksLoading = false;
  let learningLoading = false;
  let tasksError: string | null = null;
  let learningError: string | null = null;

  // Agent selector
  let selectedAgentId: string = agentId;
  let agentList: string[] = [];

  // Task expand state: task_id -> { loading, response }
  let expandedTasks: Record<string, { loading: boolean; response: string | null }> = {};

  let cancellingTask: string | null = null;
  let pollInterval: ReturnType<typeof setInterval> | null = null;
  let collapsedSections: Record<string, boolean> = {};

  function toggleSection(name: string) {
    collapsedSections = { ...collapsedSections, [name]: !collapsedSections[name] };
  }

  function formatScheduledDate(iso: string | null): string {
    if (!iso) return 'Pending';
    try {
      const d = new Date(iso);
      const diffMs = d.getTime() - Date.now();
      if (diffMs < -60000) return `Overdue · ${formatDateTime(iso)}`;
      if (diffMs < 60000) return 'In <1 min';
      const mins = Math.round(diffMs / 60000);
      if (mins < 60) return `In ${mins} min`;
      return formatDateTime(iso);
    } catch { return iso ?? 'Pending'; }
  }

  function managePoll() {
    const hasActive = (taskData?.running.length ?? 0) + (taskData?.scheduled.length ?? 0) > 0;
    if (hasActive && !pollInterval) {
      pollInterval = setInterval(() => loadTasks(), 3000);
    } else if (!hasActive && pollInterval) {
      clearInterval(pollInterval);
      pollInterval = null;
    }
  }

  onDestroy(() => {
    if (pollInterval) clearInterval(pollInterval);
  });

  async function toggleExpand(task: TaskEntry) {
    if (!task.has_full_result) return;
    if (expandedTasks[task.id]) {
      const { [task.id]: _, ...rest } = expandedTasks;
      expandedTasks = rest;
      return;
    }
    expandedTasks = { ...expandedTasks, [task.id]: { loading: true, response: null } };
    try {
      const result = await sendCommand('get_agent_task_result', {
        agent_id: selectedAgentId,
        task_id: task.id,
      }) as any;
      expandedTasks = {
        ...expandedTasks,
        [task.id]: { loading: false, response: result?.response ?? result?.message ?? 'No content' },
      };
    } catch (e: any) {
      expandedTasks = {
        ...expandedTasks,
        [task.id]: { loading: false, response: `Error: ${e?.message}` },
      };
    }
  }

  // Schedule task form state
  let showScheduleForm = false;
  let scheduleType = 'chat';
  let scheduleMessage = '';
  let scheduleDelay = 0;
  let scheduling = false;

  // Reload on open
  $: if (open) {
    selectedAgentId = agentId;
    loadAgents();
    loadTasks();
    loadLearning();
  }

  async function loadAgents() {
    try {
      const result = await sendCommand('list_agents', {}) as any;
      if (result?.agents && Array.isArray(result.agents)) {
        agentList = result.agents.map((a: any) => a.agent_id ?? a);
        // Ensure current agent is in list
        if (!agentList.includes(selectedAgentId)) {
          agentList = [selectedAgentId, ...agentList];
        }
      }
    } catch {
      // If list_agents unavailable, fall back to just the prop value
      agentList = [selectedAgentId];
    }
  }

  function onAgentChange(e: Event) {
    selectedAgentId = (e.target as HTMLSelectElement).value;
    loadTasks();
    loadLearning();
  }

  async function loadTasks() {
    tasksLoading = true;
    tasksError = null;
    try {
      const result = await sendCommand('get_agent_tasks', { agent_id: selectedAgentId }) as any;
      if (result?.status === 'success') {
        taskData = {
          running: result.running ?? [],
          scheduled: result.scheduled ?? [],
          completed: result.completed ?? [],
          failed: result.failed ?? [],
        };
        managePoll();
      } else {
        tasksError = result?.message || 'Failed to load tasks';
      }
    } catch (e: any) {
      tasksError = e?.message || 'Failed to load tasks';
    } finally {
      tasksLoading = false;
    }
  }

  async function loadLearning() {
    learningLoading = true;
    learningError = null;
    try {
      const result = await sendCommand('get_agent_learning', { agent_id: selectedAgentId }) as any;
      if (result?.status === 'success') {
        learningData = {
          phases: result.phases ?? [],
          streak_days: result.streak_days ?? 0,
          last_session: result.last_session ?? null,
        };
      } else {
        learningError = result?.message || 'Failed to load learning data';
      }
    } catch (e: any) {
      learningError = e?.message || 'Failed to load learning data';
    } finally {
      learningLoading = false;
    }
  }

  async function scheduleTask() {
    if (!scheduleMessage.trim()) return;
    scheduling = true;
    try {
      await sendCommand('schedule_agent_task', {
        agent_id: selectedAgentId,
        task_type: scheduleType,
        data: { message: scheduleMessage.trim() },
        priority: 'NORMAL',
        delay_seconds: scheduleDelay,
      });
      showScheduleForm = false;
      scheduleMessage = '';
      scheduleDelay = 0;
      // Refresh tasks after a short delay
      setTimeout(() => loadTasks(), 800);
    } catch (e: any) {
      console.error('Failed to schedule task:', e);
    } finally {
      scheduling = false;
    }
  }

  function continueTask(task: LearningTask) {
    if (onSendToAgent) {
      onSendToAgent(`Continue with ${task.id}: ${task.title}`);
    }
    close();
  }

  async function cancelTask(task: TaskEntry) {
    if (cancellingTask) return;
    cancellingTask = task.id;
    try {
      await sendCommand('cancel_agent_task', {
        agent_id: selectedAgentId,
        task_id: task.id,
      });
      await loadTasks();
    } catch (e: any) {
      console.error('Failed to cancel task:', e);
    } finally {
      cancellingTask = null;
    }
  }

  function close() {
    if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
    dispatch('close');
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') close();
  }

  function formatDate(iso: string | null): string {
    if (!iso) return '';
    try {
      const d = new Date(iso);
      return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
    } catch {
      return iso;
    }
  }

  function formatDateTime(iso: string | null): string {
    if (!iso) return '';
    try {
      const d = new Date(iso);
      return (
        d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' }) +
        ' ' +
        d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })
      );
    } catch {
      return iso;
    }
  }
</script>

{#if open}
  <!-- svelte-ignore a11y-click-events-have-key-events -->
  <!-- svelte-ignore a11y-no-static-element-interactions -->
  <div
    class="board-overlay"
    on:click={close}
    on:keydown={handleKeydown}
    role="presentation"
  >
    <div class="board-panel" on:click|stopPropagation role="dialog" aria-label="Agent Progress Board" tabindex="-1">

      <!-- Header -->
      <div class="panel-header">
        <div class="header-title-row">
          <span class="panel-title">Agent Progress Board</span>
          {#if agentList.length > 1}
            <select class="agent-select" value={selectedAgentId} on:change={onAgentChange}>
              {#each agentList as aid}
                <option value={aid}>{aid}</option>
              {/each}
            </select>
          {:else}
            <span class="panel-agent-id">{selectedAgentId}</span>
          {/if}
        </div>
        <div class="header-actions">
          <button class="btn-reload" on:click={() => { loadTasks(); loadLearning(); }} title="Refresh">
            Refresh
          </button>
          <button class="btn-close-panel" on:click={close}>Close</button>
        </div>
      </div>

      <!-- Tab Bar -->
      <div class="tab-bar">
        <button
          class="tab-btn"
          class:active={activeTab === 'tasks'}
          on:click={() => activeTab = 'tasks'}
        >
          Tasks
        </button>
        <button
          class="tab-btn"
          class:active={activeTab === 'learning'}
          on:click={() => activeTab = 'learning'}
        >
          Learning
        </button>
      </div>

      <!-- Panel Body -->
      <div class="panel-body">

        <!-- Tasks Tab -->
        {#if activeTab === 'tasks'}
          {#if tasksLoading}
            <div class="loading-msg">Loading tasks...</div>
          {:else if tasksError}
            <div class="error-msg">{tasksError}</div>
          {:else if taskData}

            <!-- RUNNING -->
            <div class="task-section">
              <button class="section-header" on:click={() => toggleSection('running')}>
                <span class="section-chevron">{collapsedSections['running'] ? '▶' : '▼'}</span>
                <span class="section-label-text">Running ({taskData.running.length})</span>
              </button>
              {#if !collapsedSections['running']}
                {#if taskData.running.length === 0}
                  <div class="empty-hint">No running tasks</div>
                {:else}
                  {#each taskData.running as task}
                    <div class="task-row running">
                      <div class="task-row-main">
                        <span class="task-type">{task.type}</span>
                        <div class="task-row-right">
                          {#if task.started_at}
                            <span class="task-date">Started {formatDateTime(task.started_at)}</span>
                          {/if}
                          <button class="btn-cancel-task" on:click={() => cancelTask(task)} disabled={cancellingTask === task.id} title="Cancel task">
                            {cancellingTask === task.id ? '…' : '✕'}
                          </button>
                        </div>
                      </div>
                      <div class="task-id-row">
                        <span class="task-id">{task.id}</span>
                      </div>
                      {#if task.preview}
                        <div class="task-preview">{task.preview}</div>
                      {/if}
                    </div>
                  {/each}
                {/if}
              {/if}
            </div>

            <!-- SCHEDULED -->
            <div class="task-section">
              <button class="section-header" on:click={() => toggleSection('scheduled')}>
                <span class="section-chevron">{collapsedSections['scheduled'] ? '▶' : '▼'}</span>
                <span class="section-label-text">Scheduled ({taskData.scheduled.length})</span>
              </button>
              {#if !collapsedSections['scheduled']}
                {#if taskData.scheduled.length === 0}
                  <div class="empty-hint">No scheduled tasks</div>
                {:else}
                  {#each taskData.scheduled as task}
                    <div class="task-row scheduled">
                      <div class="task-row-main">
                        <span class="task-type">{task.type}</span>
                        <div class="task-row-right">
                          <span class="task-date">{formatScheduledDate(task.scheduled_at)}</span>
                          <button class="btn-cancel-task" on:click={() => cancelTask(task)} disabled={cancellingTask === task.id} title="Cancel task">
                            {cancellingTask === task.id ? '…' : '✕'}
                          </button>
                        </div>
                      </div>
                      <div class="task-id-row">
                        <span class="task-id">{task.id}</span>
                      </div>
                      {#if task.preview}
                        <div class="task-preview">{task.preview}</div>
                      {/if}
                    </div>
                  {/each}
                {/if}
              {/if}
            </div>

            <!-- COMPLETED -->
            <div class="task-section">
              <button class="section-header" on:click={() => toggleSection('completed')}>
                <span class="section-chevron">{collapsedSections['completed'] ? '▶' : '▼'}</span>
                <span class="section-label-text">Completed ({taskData.completed.length})</span>
              </button>
              {#if !collapsedSections['completed']}
                {#if taskData.completed.length === 0}
                  <div class="empty-hint">No completed tasks yet</div>
                {:else}
                  {#each taskData.completed as task}
                    <div class="task-row completed" class:expanded={!!expandedTasks[task.id]}>
                      <div class="task-row-main">
                        <span class="task-type">{task.type}</span>
                        <div class="task-row-right">
                          {#if task.completed_at}
                            <span class="task-date">{formatDateTime(task.completed_at)}</span>
                          {/if}
                          {#if task.has_full_result}
                            <button class="btn-expand" on:click={() => toggleExpand(task)} title={expandedTasks[task.id] ? 'Collapse' : 'Expand'}>
                              {expandedTasks[task.id] ? '▲' : '▼'}
                            </button>
                          {/if}
                        </div>
                      </div>
                      {#if task.preview}
                        <div class="task-preview">{task.preview}</div>
                      {/if}
                      {#if expandedTasks[task.id]}
                        <div class="task-full-result">
                          {#if expandedTasks[task.id].loading}
                            <span class="result-loading">Loading...</span>
                          {:else}
                            <pre class="result-text">{expandedTasks[task.id].response}</pre>
                          {/if}
                        </div>
                      {/if}
                    </div>
                  {/each}
                {/if}
              {/if}
            </div>

            <!-- FAILED -->
            <div class="task-section">
              <button class="section-header" on:click={() => toggleSection('failed')}>
                <span class="section-chevron">{collapsedSections['failed'] ? '▶' : '▼'}</span>
                <span class="section-label-text">Failed ({taskData.failed.length})</span>
              </button>
              {#if !collapsedSections['failed']}
                {#if taskData.failed.length === 0}
                  <div class="empty-hint">No failed tasks</div>
                {:else}
                  {#each taskData.failed as task}
                    <div class="task-row failed">
                      <div class="task-row-main">
                        <span class="task-type">{task.type}</span>
                        <div class="task-row-right">
                          {#if task.completed_at}
                            <span class="task-date">{formatDateTime(task.completed_at)}</span>
                          {/if}
                        </div>
                      </div>
                      {#if task.preview}
                        <div class="task-preview">{task.preview}</div>
                      {/if}
                    </div>
                  {/each}
                {/if}
              {/if}
            </div>

          {:else}
            <div class="empty-hint">No task data available</div>
          {/if}

          <!-- Schedule Task Form -->
          <div class="schedule-section">
            {#if !showScheduleForm}
              <button class="btn-schedule" on:click={() => showScheduleForm = true}>
                Schedule Task
              </button>
            {:else}
              <div class="schedule-form">
                <div class="form-row">
                  <label for="sched-type">Type</label>
                  <select id="sched-type" bind:value={scheduleType}>
                    <option value="chat">chat</option>
                    <option value="improvement">improvement</option>
                    <option value="review">review</option>
                  </select>
                </div>
                <div class="form-row">
                  <label for="sched-msg">Message</label>
                  <input
                    id="sched-msg"
                    type="text"
                    bind:value={scheduleMessage}
                    placeholder="Task description..."
                  />
                </div>
                <div class="form-row">
                  <label for="sched-delay">Delay (seconds)</label>
                  <input
                    id="sched-delay"
                    type="number"
                    bind:value={scheduleDelay}
                    min="0"
                    max="86400"
                  />
                </div>
                <div class="form-actions">
                  <button class="btn-cancel" on:click={() => showScheduleForm = false}>Cancel</button>
                  <button
                    class="btn-confirm"
                    on:click={scheduleTask}
                    disabled={scheduling || !scheduleMessage.trim()}
                  >
                    {scheduling ? 'Scheduling...' : 'Schedule'}
                  </button>
                </div>
              </div>
            {/if}
          </div>
        {/if}

        <!-- Learning Tab -->
        {#if activeTab === 'learning'}
          {#if learningLoading}
            <div class="loading-msg">Loading learning data...</div>
          {:else if learningError}
            <div class="error-msg">{learningError}</div>
          {:else if learningData}

            <div class="learning-meta">
              <span class="meta-item">Streak: {learningData.streak_days} day{learningData.streak_days !== 1 ? 's' : ''}</span>
              {#if learningData.last_session}
                <span class="meta-item">Last session: {formatDate(learningData.last_session)}</span>
              {/if}
            </div>

            {#each learningData.phases as phase}
              <div class="task-section">
                <div class="section-label">{phase.title}</div>
                {#each phase.tasks as task}
                  <div class="learning-task {task.status}">
                    <div class="learning-task-header">
                      <span class="ltask-id">{task.id}</span>
                      <span class="ltask-title">{task.title}</span>
                      <span class="ltask-status {task.status}">
                        {task.status === 'stalled'
                          ? `stalled (${task.days_stalled ?? 0} days)`
                          : task.status}
                      </span>
                    </div>
                    {#if task.status === 'stalled' && onSendToAgent}
                      <button class="btn-continue" on:click={() => continueTask(task)}>
                        Continue {task.id}
                      </button>
                    {/if}
                    {#if task.next_step && (task.status === 'in_progress' || task.status === 'stalled')}
                      <div class="next-step-hint">Next: {task.next_step}</div>
                    {/if}
                  </div>
                {/each}
              </div>
            {/each}

          {:else}
            <div class="empty-hint">
              No learning data found. Ask the agent to update knowledge/llm_learning_schedule.md with a Progress Tracking section.
            </div>
          {/if}
        {/if}

      </div><!-- end panel-body -->
    </div><!-- end board-panel -->
  </div><!-- end board-overlay -->
{/if}

<style>
  .board-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.5);
    z-index: 1000;
    display: flex;
    justify-content: flex-end;
  }

  .board-panel {
    width: 420px;
    max-width: 95vw;
    height: 100%;
    background: var(--bg-secondary, #1e1e2e);
    border-left: 1px solid var(--border-color, #333);
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  /* Header */
  .panel-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 16px;
    border-bottom: 1px solid var(--border-color, #333);
    flex-shrink: 0;
    gap: 8px;
  }

  .header-title-row {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .panel-title {
    font-size: 14px;
    font-weight: 600;
    color: var(--text-primary, #cdd6f4);
    white-space: nowrap;
  }

  .panel-agent-id {
    font-size: 11px;
    color: var(--text-muted, #6c7086);
    font-family: monospace;
  }

  .agent-select {
    font-size: 11px;
    font-family: monospace;
    color: var(--text-muted, #6c7086);
    background: transparent;
    border: 1px solid var(--border-color, #444);
    border-radius: 3px;
    padding: 2px 4px;
    cursor: pointer;
    max-width: 180px;
  }

  .agent-select:focus {
    outline: none;
    border-color: var(--accent, #89b4fa);
    color: var(--text-primary, #cdd6f4);
  }

  .header-actions {
    display: flex;
    gap: 6px;
    flex-shrink: 0;
  }

  .btn-reload {
    padding: 4px 10px;
    font-size: 12px;
    background: transparent;
    border: 1px solid var(--border-color, #444);
    border-radius: 4px;
    color: var(--text-secondary, #a6adc8);
    cursor: pointer;
  }

  .btn-reload:hover {
    background: var(--bg-hover, #313244);
  }

  .btn-close-panel {
    padding: 4px 10px;
    font-size: 12px;
    background: transparent;
    border: 1px solid var(--border-color, #444);
    border-radius: 4px;
    color: var(--text-secondary, #a6adc8);
    cursor: pointer;
  }

  .btn-close-panel:hover {
    background: var(--bg-hover, #313244);
  }

  /* Tabs */
  .tab-bar {
    display: flex;
    border-bottom: 1px solid var(--border-color, #333);
    flex-shrink: 0;
  }

  .tab-btn {
    flex: 1;
    padding: 8px;
    font-size: 13px;
    font-weight: 500;
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    color: var(--text-secondary, #a6adc8);
    cursor: pointer;
    transition: color 0.15s, border-color 0.15s;
  }

  .tab-btn:hover {
    color: var(--text-primary, #cdd6f4);
  }

  .tab-btn.active {
    color: var(--accent, #89b4fa);
    border-bottom-color: var(--accent, #89b4fa);
  }

  /* Body */
  .panel-body {
    flex: 1;
    overflow-y: auto;
    padding: 12px 16px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  /* Task sections */
  .task-section {
    margin-bottom: 12px;
  }

  .section-header {
    display: flex;
    align-items: center;
    gap: 6px;
    width: 100%;
    background: none;
    border: none;
    border-bottom: 1px solid var(--border-color, #333);
    padding: 0 0 4px 0;
    margin-bottom: 6px;
    cursor: pointer;
    text-align: left;
  }

  .section-chevron {
    font-size: 9px;
    color: var(--text-muted, #6c7086);
    flex-shrink: 0;
    line-height: 1;
  }

  .section-label-text {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-muted, #6c7086);
  }

  .section-header:hover .section-label-text,
  .section-header:hover .section-chevron {
    color: var(--text-secondary, #a6adc8);
  }

  .task-row {
    padding: 6px 8px;
    border-radius: 4px;
    margin-bottom: 4px;
    background: var(--bg-tertiary, #181825);
  }

  .task-row-right {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-shrink: 0;
  }

  .btn-expand {
    padding: 0 4px;
    font-size: 10px;
    background: transparent;
    border: none;
    color: var(--text-muted, #6c7086);
    cursor: pointer;
    line-height: 1;
  }

  .btn-expand:hover {
    color: var(--text-primary, #cdd6f4);
  }

  .btn-cancel-task {
    background: none;
    border: 1px solid #c0392b;
    color: #c0392b;
    border-radius: 3px;
    padding: 1px 5px;
    font-size: 10px;
    cursor: pointer;
    line-height: 1.4;
    flex-shrink: 0;
  }
  .btn-cancel-task:hover:not(:disabled) {
    background: rgba(192, 57, 43, 0.15);
  }
  .btn-cancel-task:disabled {
    opacity: 0.5;
    cursor: default;
  }

  .task-full-result {
    margin-top: 6px;
    padding: 8px;
    background: var(--bg-input, #11111b);
    border-radius: 4px;
    border-left: 2px solid var(--accent, #89b4fa);
    max-height: 300px;
    overflow-y: auto;
  }

  .result-text {
    font-size: 11px;
    color: var(--text-secondary, #a6adc8);
    white-space: pre-wrap;
    word-break: break-word;
    margin: 0;
    font-family: inherit;
  }

  .result-loading {
    font-size: 11px;
    color: var(--text-muted, #6c7086);
    font-style: italic;
  }

  .task-row.expanded {
    background: var(--bg-hover, #313244);
  }

  .task-row-main {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }

  .task-type {
    font-size: 11px;
    font-weight: 600;
    color: var(--text-muted, #6c7086);
    text-transform: uppercase;
    flex-shrink: 0;
  }

  .task-date {
    font-size: 11px;
    color: var(--text-muted, #6c7086);
    flex-shrink: 0;
  }

  .task-preview {
    font-size: 12px;
    color: var(--text-secondary, #a6adc8);
    margin-top: 2px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 100%;
  }

  .task-id-row {
    margin-top: 1px;
  }
  .task-id {
    font-size: 10px;
    font-family: monospace;
    color: var(--text-secondary, #585b70);
    opacity: 0.7;
  }

  .task-row.running .task-type { color: var(--yellow, #f9e2af); }
  .task-row.scheduled .task-type { color: var(--blue, #89b4fa); }
  .task-row.completed .task-type { color: var(--green, #a6e3a1); }
  .task-row.failed .task-type { color: var(--red, #f38ba8); }

  .empty-hint {
    font-size: 12px;
    color: var(--text-muted, #6c7086);
    padding: 8px 0;
    font-style: italic;
  }

  /* Schedule form */
  .schedule-section {
    margin-top: auto;
    padding-top: 12px;
    border-top: 1px solid var(--border-color, #333);
    flex-shrink: 0;
  }

  .btn-schedule {
    width: 100%;
    padding: 8px;
    font-size: 13px;
    background: transparent;
    border: 1px dashed var(--border-color, #444);
    border-radius: 4px;
    color: var(--text-secondary, #a6adc8);
    cursor: pointer;
  }

  .btn-schedule:hover {
    background: var(--bg-hover, #313244);
  }

  .schedule-form {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .form-row {
    display: flex;
    flex-direction: column;
    gap: 3px;
  }

  .form-row label {
    font-size: 11px;
    font-weight: 600;
    color: var(--text-muted, #6c7086);
    text-transform: uppercase;
  }

  .form-row input,
  .form-row select {
    padding: 5px 8px;
    font-size: 13px;
    background: var(--bg-input, #11111b);
    border: 1px solid var(--border-color, #444);
    border-radius: 4px;
    color: var(--text-primary, #cdd6f4);
  }

  .form-actions {
    display: flex;
    gap: 8px;
    justify-content: flex-end;
  }

  .btn-cancel {
    padding: 5px 12px;
    font-size: 12px;
    background: transparent;
    border: 1px solid var(--border-color, #444);
    border-radius: 4px;
    color: var(--text-secondary, #a6adc8);
    cursor: pointer;
  }

  .btn-confirm {
    padding: 5px 12px;
    font-size: 12px;
    background: var(--accent, #89b4fa);
    border: none;
    border-radius: 4px;
    color: #1e1e2e;
    font-weight: 600;
    cursor: pointer;
  }

  .btn-confirm:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  /* Learning tab */
  .learning-meta {
    display: flex;
    gap: 16px;
    margin-bottom: 12px;
    padding: 8px 10px;
    background: var(--bg-tertiary, #181825);
    border-radius: 4px;
  }

  .meta-item {
    font-size: 12px;
    color: var(--text-secondary, #a6adc8);
  }

  .learning-task {
    padding: 8px;
    border-radius: 4px;
    margin-bottom: 6px;
    background: var(--bg-tertiary, #181825);
    border-left: 3px solid transparent;
  }

  .learning-task.completed { border-left-color: var(--green, #a6e3a1); }
  .learning-task.in_progress { border-left-color: var(--blue, #89b4fa); }
  .learning-task.stalled { border-left-color: var(--yellow, #f9e2af); }
  .learning-task.pending { border-left-color: var(--border-color, #444); }

  .learning-task-header {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }

  .ltask-id {
    font-size: 11px;
    font-weight: 700;
    color: var(--text-muted, #6c7086);
    flex-shrink: 0;
  }

  .ltask-title {
    font-size: 13px;
    color: var(--text-primary, #cdd6f4);
    flex: 1;
    min-width: 0;
  }

  .ltask-status {
    font-size: 11px;
    font-weight: 600;
    flex-shrink: 0;
  }

  .ltask-status.completed { color: var(--green, #a6e3a1); }
  .ltask-status.in_progress { color: var(--blue, #89b4fa); }
  .ltask-status.stalled { color: var(--yellow, #f9e2af); }
  .ltask-status.pending { color: var(--text-muted, #6c7086); }

  .btn-continue {
    margin-top: 6px;
    padding: 4px 10px;
    font-size: 12px;
    background: var(--yellow, #f9e2af);
    border: none;
    border-radius: 3px;
    color: #1e1e2e;
    font-weight: 600;
    cursor: pointer;
  }

  .btn-continue:hover {
    opacity: 0.85;
  }

  .next-step-hint {
    margin-top: 4px;
    font-size: 11px;
    color: var(--text-muted, #6c7086);
    font-style: italic;
  }

  /* States */
  .loading-msg {
    font-size: 13px;
    color: var(--text-muted, #6c7086);
    padding: 16px 0;
    text-align: center;
  }

  .error-msg {
    font-size: 12px;
    color: var(--red, #f38ba8);
    padding: 8px 10px;
    background: rgba(243, 139, 168, 0.1);
    border-radius: 4px;
  }
</style>
