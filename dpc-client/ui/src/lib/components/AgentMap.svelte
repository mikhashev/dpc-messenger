<!-- AgentMap.svelte — the Agent map: what each agent is doing and what it started. -->
<!--
  ADR-046 point 8. Rooted at the backend: one card per agent (its turn, the
  chats waiting for it, the processes it started), and the backend's own
  system branch (engines, the browser, probes, git helpers), collapsed by
  default. Processes have no push event, so the open map reads list_processes
  on open, every PROCESS_POLL_MS and on each agent_turn_updated; a closed map
  reads nothing. Every line is built in utils/agentMap.ts.
-->

<script lang="ts">
  import { untrack } from 'svelte';
  import { sendCommand, groupChats, nodeStatus } from '$lib/coreService';
  import { agentTurns, agentsList } from '$lib/services/agents';
  import {
    PROCESS_POLL_MS,
    SYSTEM_OPEN_BY_DEFAULT,
    buildAgentMap,
    pickSelected,
    plural,
    turnsKey,
    type ProcessListing,
  } from '$lib/utils/agentMap';
  import { freeCommitLine } from '$lib/utils/strataFields';

  let { open = $bindable(false) }: { open?: boolean } = $props();

  let listing = $state<ProcessListing | null>(null);
  let error = $state('');
  let now = $state(Date.now());
  let systemOpen = $state(SYSTEM_OPEN_BY_DEFAULT);
  let selectedId = $state<string | null>(null);
  let inFlight = false;

  async function refresh() {
    if (inFlight) return;
    inFlight = true;
    try {
      const r: any = await sendCommand('list_processes', {});
      if (r?.status === 'success') {
        listing = {
          processes: r.processes ?? [],
          backend_pid: r.backend_pid ?? null,
          backend_start_time: r.backend_start_time ?? null,
          free_commit: r.free_commit ?? null,
        };
        error = '';
      } else if (r && typeof r === 'object') {
        error = r.message || 'list_processes failed';
      }
    } catch (e) {
      error = `list_processes failed: ${e}`;
    } finally {
      inFlight = false;
    }
  }

  // Open: read now, then on the interval, with a clock for the durations. Closed: nothing runs.
  $effect(() => {
    if (!open) return;
    untrack(refresh);
    now = Date.now();
    const poll = setInterval(refresh, PROCESS_POLL_MS);
    const tick = setInterval(() => (now = Date.now()), 1000);
    return () => {
      clearInterval(poll);
      clearInterval(tick);
    };
  });

  // A turn that starts, ends or changes is when a process is likely to appear or end.
  let key = $derived(turnsKey($agentTurns));
  let lastKey = '';
  $effect(() => {
    const k = key;
    if (!open) return;
    if (lastKey && k !== lastKey) untrack(refresh);
    lastKey = k;
  });

  let map = $derived(buildAgentMap({
    agents: ($agentsList ?? []) as any[],
    turns: $agentTurns,
    listing,
    names: { groups: $groupChats, peers: ($nodeStatus as any)?.peer_info ?? [] },
    nowMs: now,
  }));
  let selected = $derived(map.agents.find(a => a.agent_id === pickSelected(map, selectedId)) ?? null);

  function close() {
    open = false;
  }

  function handleKeydown(e: KeyboardEvent) {
    if (open && e.key === 'Escape') close();
  }
</script>

<svelte:window onkeydown={handleKeydown} />

{#if open}
  <div class="agent-map" role="dialog" aria-modal="true" aria-labelledby="agent-map-title">
    <div class="map-body">
      <div class="map-head">
        <div class="map-title">
          <h1 id="agent-map-title">Agent map</h1>
          <p class="muted">{map.summaryLine}</p>
        </div>
        <button type="button" class="map-close" onclick={close} aria-label="Close the agent map">×</button>
      </div>

      {#if error}
        <div class="map-error" role="alert">{error}</div>
      {/if}

      {#if listing}
        <div class="memory" title={freeCommitLine(listing.free_commit)}>
          {#if map.memoryBars.length}
            {#each map.memoryBars as bar (bar.kind)}
              <div class="memory-bar">
                <span>{bar.label}</span>
                <div class="bar-track"><div class="bar-fill {bar.kind}" style="width: {Math.round(bar.fraction * 100)}%"></div></div>
              </div>
            {/each}
            <span class="memory-source">{map.memorySource}</span>
          {:else}
            <span class="muted">{freeCommitLine(listing.free_commit)}</span>
          {/if}
        </div>
      {/if}

      <div class="map-columns">
        <div class="tree">
          <div class="root-node">
            <span class="node-title">D-PC backend</span>
            {#if map.backendLine}<span class="muted small">{map.backendLine}</span>{/if}
          </div>
          <div class="branch">
            {#each map.agents as a (a.agent_id)}
              <div class="twig">
                <div class="twig-line"></div>
                <button
                  type="button"
                  class="agent-card"
                  class:selected={selected?.agent_id === a.agent_id}
                  onclick={() => (selectedId = a.agent_id)}
                >
                  <span class="card-top">
                    <span class="dot {a.dot}"></span>
                    <span class="agent-name">{a.name}</span>
                    <span class="model-line">{[a.model, a.effort].filter(Boolean).join(' · ')}</span>
                  </span>
                  <span class="muted small">{a.stateLine}</span>
                  {#if a.alive > 0 || a.waiting.length > 0}
                    <span class="chips">
                      {#if a.alive > 0}<span class="chip procs">{plural(a.alive, 'process', 'processes')}</span>{/if}
                      {#if a.waiting.length > 0}<span class="chip waits">{plural(a.waiting.length, 'chat waiting', 'chats waiting')}</span>{/if}
                    </span>
                  {/if}
                </button>
              </div>
            {/each}
            <div class="twig">
              <div class="twig-line"></div>
              <button type="button" class="system-card" aria-expanded={systemOpen} onclick={() => (systemOpen = !systemOpen)}>
                <span class="system-text">
                  <span class="system-title">System</span>
                  <span class="muted small">{map.systemSummary}</span>
                </span>
                <span class="toggle-word">{systemOpen ? 'Hide' : 'Show'}</span>
              </button>
            </div>
            {#if systemOpen}
              <div class="system-branch">
                {#each map.system as s}
                  <div class="twig">
                    <div class="twig-line short"></div>
                    <div class="system-item">
                      <span class="system-name"><span class="dot {s.alive ? 'working' : 'idle'}"></span>{s.name}</span>
                      <span class="muted tiny">{s.line}</span>
                    </div>
                  </div>
                {/each}
              </div>
            {/if}
          </div>
          <div class="legend">
            <span><span class="dot working"></span>working</span>
            <span><span class="dot waiting"></span>waiting (model or approval)</span>
            <span><span class="dot idle"></span>idle or ended</span>
          </div>
        </div>

        {#if selected}
          <div class="detail">
            <div class="detail-head">
              <span class="dot big {selected.dot}"></span>
              <h2>{selected.name}</h2>
              {#if selected.model}<span class="tag">{selected.model}</span>{/if}
              {#if selected.effort}<span class="tag">{selected.effort}</span>{/if}
            </div>

            <div class="now">
              <span class="section-label">NOW</span>
              <span class="now-line">{selected.nowLine}</span>
              {#if selected.nowDetail}<span class="muted small">{selected.nowDetail}</span>{/if}
            </div>

            <div class="section">
              <span class="section-label">CHATS WAITING FOR {selected.name.toUpperCase()}</span>
              {#if selected.waiting.length === 0}
                <span class="muted">No chat is waiting.</span>
              {/if}
              {#each selected.waiting as q (q.conversation_id)}
                <div class="queue-row">
                  <span class="queue-chat">{q.chat}</span>
                  {#if q.place}<span class="muted">{q.place}</span>{/if}
                  <span class="chip waits wait-reason">{q.reason} · {q.wait}</span>
                </div>
              {/each}
            </div>

            <div class="section">
              <span class="section-label">PROCESSES</span>
              {#if listing && selected.processes.length === 0}
                <span class="muted">This agent has started no process in the last hour.</span>
              {/if}
              {#if selected.processes.length > 0}
                <div class="table-scroll">
                  <div class="proc-table">
                    <div class="proc-row proc-head">
                      <span></span><span>pid</span><span>command</span><span>chat</span><span>started</span><span>for</span><span>RSS</span><span>commit</span><span>state</span>
                    </div>
                    {#each selected.processes as p (p.pid + p.started)}
                      <div class="proc-row">
                        <span class="dot {p.alive ? 'working' : 'idle'}"></span>
                        <span>{p.pid}</span>
                        <span class="cmd" title={p.command}>{p.command}</span>
                        <span>{p.chat}</span>
                        <span>{p.started}</span>
                        <span>{p.duration}</span>
                        <span>{p.rss}</span>
                        <span>{p.commit}</span>
                        <span class="end {p.endTone}">{p.end}</span>
                      </div>
                    {/each}
                  </div>
                </div>
              {/if}
            </div>
          </div>
        {/if}
      </div>
    </div>
  </div>
{/if}

<style>
  .agent-map {
    position: fixed;
    inset: 0;
    z-index: 1000;
    overflow-y: auto;
    box-sizing: border-box;
    padding: 28px 24px 40px;
    background: var(--bg-primary, #15171a);
    color: var(--text-primary, #e8eaed);
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
  }

  .map-body {
    max-width: 1320px;
    margin: 0 auto;
    display: flex;
    flex-direction: column;
    gap: 20px;
  }

  .map-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
  }

  .map-title { display: flex; flex-direction: column; gap: 6px; }
  .map-title h1 { margin: 0; font-size: 26px; font-weight: 600; }
  .map-title p { margin: 0; font-size: 15px; }

  .map-close {
    width: 44px;
    height: 44px;
    display: flex;
    align-items: center;
    justify-content: center;
    border: 0;
    border-radius: 8px;
    background: transparent;
    color: #c9d1d9;
    font-size: 26px;
    cursor: pointer;
  }
  .map-close:hover { color: #ffffff; background: var(--bg-hover, #22262c); }

  .muted { color: var(--text-secondary, #a9b0bb); }
  .small { font-size: 13px; }
  .tiny { font-size: 12px; }

  .map-error {
    padding: 10px 14px;
    border-radius: 8px;
    background: #3d1d1d;
    color: #f5a5a5;
    font-size: 14px;
  }

  .memory {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 12px 28px;
    padding: 12px 16px;
    background: var(--bg-secondary, #1d2025);
    border: 1px solid var(--border-color, #2c3036);
    border-radius: 10px;
    font-size: 14px;
    color: #c9d1d9;
  }
  .memory-bar { display: flex; flex-direction: column; gap: 6px; flex: 1 1 260px; }
  .bar-track { height: 6px; background: #2c3036; border-radius: 3px; }
  .bar-fill { height: 6px; background: #58a6ff; border-radius: 3px; }
  .bar-fill.physical { background: #d29922; }
  .memory-source { font-size: 13px; color: var(--text-secondary, #a9b0bb); }

  .map-columns {
    display: flex;
    flex-wrap: wrap;
    gap: 24px;
    align-items: flex-start;
  }

  .tree {
    flex: 1 1 400px;
    max-width: 460px;
    min-width: 0;
    display: flex;
    flex-direction: column;
  }

  .root-node {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 12px 14px;
    background: var(--bg-secondary, #1d2025);
    border: 1px solid var(--border-color, #2c3036);
    border-radius: 10px;
  }
  .node-title { font-size: 16px; font-weight: 600; }

  .branch {
    margin-left: 22px;
    border-left: 1px solid #3a3f47;
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 10px 0;
  }
  .system-branch {
    margin-left: 40px;
    border-left: 1px solid #3a3f47;
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 4px 0;
  }

  .twig { display: flex; align-items: center; }
  .twig-line { width: 18px; height: 1px; background: #3a3f47; flex: 0 0 18px; }
  .twig-line.short { width: 16px; flex-basis: 16px; }

  .agent-card, .system-card {
    flex: 1 1 auto;
    min-width: 0;
    text-align: left;
    padding: 10px 12px;
    border-radius: 10px;
    color: var(--text-primary, #e8eaed);
    font: inherit;
    cursor: pointer;
  }
  .agent-card {
    display: flex;
    flex-direction: column;
    gap: 4px;
    border: 1px solid var(--border-color, #2c3036);
    background: var(--bg-secondary, #1d2025);
  }
  .agent-card.selected { border-color: #58a6ff; background: #232a35; }
  .card-top { display: flex; align-items: center; gap: 8px; width: 100%; }
  .agent-name { font-size: 16px; font-weight: 600; }
  .model-line { margin-left: auto; font-size: 12px; color: var(--text-secondary, #a9b0bb); }

  .chips { display: flex; gap: 6px; flex-wrap: wrap; font-size: 12px; }
  .chip { padding: 1px 7px; border-radius: 5px; }
  .chip.procs { background: #1f3a28; color: #7ee2a0; }
  .chip.waits { background: #3d3016; color: #f0c674; }

  .system-card {
    display: flex;
    align-items: center;
    gap: 8px;
    border: 1px dashed #4a505a;
    background: #181a1e;
  }
  .system-text { display: flex; flex-direction: column; gap: 4px; }
  .system-title { font-size: 15px; font-weight: 600; }
  .toggle-word { margin-left: auto; font-size: 13px; color: #58a6ff; }

  .system-item {
    flex: 1 1 auto;
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 8px 10px;
    border-radius: 8px;
    background: var(--bg-secondary, #1d2025);
    border: 1px solid var(--border-color, #2c3036);
  }
  .system-name { display: flex; align-items: center; gap: 8px; font-size: 14px; }

  .legend {
    display: flex;
    gap: 14px;
    flex-wrap: wrap;
    font-size: 12px;
    color: var(--text-secondary, #a9b0bb);
    padding-left: 4px;
  }
  .legend > span { display: flex; align-items: center; gap: 6px; }

  .dot { width: 8px; height: 8px; border-radius: 50%; flex: 0 0 8px; display: inline-block; }
  .dot.big { width: 10px; height: 10px; flex-basis: 10px; }
  .dot.working { background: #3fb950; }
  .dot.waiting { background: #d29922; }
  .dot.idle { background: #6e7681; }

  .detail {
    flex: 999 1 560px;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 16px;
    background: var(--bg-secondary, #1d2025);
    border: 1px solid var(--border-color, #2c3036);
    border-radius: 12px;
    padding: 20px;
  }
  .detail-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .detail-head h2 { margin: 0; font-size: 22px; font-weight: 600; }
  .tag { font-size: 13px; padding: 2px 8px; border-radius: 6px; background: #31353d; color: #c9d1d9; }

  .now {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 12px 14px;
    background: #22262c;
    border-radius: 10px;
  }
  .now-line { font-size: 16px; }

  .section { display: flex; flex-direction: column; gap: 8px; font-size: 14px; }
  .section-label { font-size: 12px; font-weight: 700; letter-spacing: 0.06em; color: var(--text-secondary, #a9b0bb); }

  .queue-row {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
    padding: 10px 12px;
    border-radius: 8px;
    background: #22262c;
    font-size: 14px;
  }
  .queue-chat { font-weight: 600; }
  .wait-reason { margin-left: auto; }

  .table-scroll { overflow-x: auto; }
  .proc-table { min-width: 860px; display: flex; flex-direction: column; gap: 4px; }
  .proc-row {
    display: grid;
    grid-template-columns: 14px 64px minmax(220px, 2fr) 120px 76px 72px 76px 84px 110px;
    gap: 10px;
    align-items: center;
    padding: 8px 10px;
    border-radius: 8px;
    background: #22262c;
    font-size: 13px;
    color: #c9d1d9;
    font-variant-numeric: tabular-nums;
  }
  .proc-head { padding: 4px 10px; background: transparent; font-size: 12px; color: var(--text-secondary, #a9b0bb); }
  .cmd {
    font-family: ui-monospace, 'Cascadia Mono', Consolas, monospace;
    color: var(--text-primary, #e8eaed);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .end.alive { color: #3fb950; }
  .end.ended { color: var(--text-secondary, #a9b0bb); }
  .end.warn { color: #f0c674; }
</style>
