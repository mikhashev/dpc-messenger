<!-- AgentMap.svelte — the Agent map: what each agent is doing and what it started. -->
<!--
  ADR-046 point 8. Rooted at the backend: one card per agent (its turn, the
  chats waiting for it, the processes it started), and the backend's own
  system branch (engines, the browser, probes, git helpers), collapsed by
  default. Processes have no push event, so the open map reads list_processes
  on open, every PROCESS_POLL_MS and on each agent_turn_updated; a closed or
  minimised map reads nothing, and restoring it reads at once. Every line is
  built in utils/agentMap.ts.

  A window the viewer can drag by its header, resize by the corner handle and
  minimise to the header bar; its box and the minimised flag are remembered in
  localStorage (utils/windowGeometry.ts), and the map works without it.
-->

<script lang="ts">
  import { untrack } from 'svelte';
  import { sendCommand, groupChats, nodeStatus } from '$lib/coreService';
  import { agentTurns, agentsList } from '$lib/services/agents';
  import {
    PROCESS_POLL_MS,
    SYSTEM_OPEN_BY_DEFAULT,
    buildAgentMap,
    buttonSummary,
    pickSelected,
    plural,
    turnsKey,
    type ProcessListing,
  } from '$lib/utils/agentMap';
  import { freeCommitLine } from '$lib/utils/strataFields';
  import {
    HEADER_HEIGHT,
    clampRect,
    isClick,
    loadWindowState,
    moveRect,
    resizeRect,
    saveWindowState,
    type WindowRect,
  } from '$lib/utils/windowGeometry';

  let { open = $bindable(false) }: { open?: boolean } = $props();

  let listing = $state<ProcessListing | null>(null);
  let error = $state('');
  let now = $state(Date.now());
  let systemOpen = $state(SYSTEM_OPEN_BY_DEFAULT);
  let selectedId = $state<string | null>(null);
  let inFlight = false;

  // The window: its box, whether it is only the header bar, and the gesture under the pointer.
  let rect = $state<WindowRect>({ x: 0, y: 0, width: 0, height: 0 });
  let minimised = $state(false);
  let gesture: { kind: 'move' | 'resize'; px: number; py: number; start: WindowRect } | null = null;
  let dragged = false;

  const viewport = () => ({ width: window.innerWidth, height: window.innerHeight });
  function storage(): Storage | null {
    try {
      return window.localStorage;
    } catch {
      return null;
    }
  }
  const remember = () => saveWindowState(storage(), { rect, minimised });

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
          vram_free_mib: r.vram_free_mib ?? null,
          vram_total_mib: r.vram_total_mib ?? null,
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

  // Each opening takes the remembered box, fitted to the viewport as it is now,
  // and opens expanded: a press on AGENTS MAP asks to see the map, not its bar.
  $effect(() => {
    if (!open) return;
    untrack(() => {
      const w = loadWindowState(storage(), viewport());
      rect = clampRect(w.rect, viewport(), false);
      minimised = false;
    });
  });

  // Reading: open and not minimised. Read now, then on the interval, with a clock
  // for the durations. Closed or minimised: nothing runs.
  let active = $derived(open && !minimised);
  $effect(() => {
    if (!active) return;
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
    if (!active) return;
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

  function toggleMinimised() {
    minimised = !minimised;
    rect = clampRect(rect, viewport(), minimised);
    remember();
  }

  function beginGesture(e: PointerEvent, kind: 'move' | 'resize') {
    if (e.button !== 0) return;
    if (kind === 'move' && (e.target as HTMLElement).closest('button')) return;
    gesture = { kind, px: e.clientX, py: e.clientY, start: { ...rect } };
    dragged = false;
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    e.preventDefault();
  }

  function moveGesture(e: PointerEvent) {
    if (!gesture) return;
    const dx = e.clientX - gesture.px;
    const dy = e.clientY - gesture.py;
    if (!isClick(dx, dy)) dragged = true;
    rect = gesture.kind === 'move'
      ? moveRect(gesture.start, dx, dy, viewport(), minimised)
      : resizeRect(gesture.start, dx, dy, viewport());
  }

  function endGesture(e: PointerEvent) {
    if (!gesture) return;
    const kind = gesture.kind;
    gesture = null;
    const el = e.currentTarget as HTMLElement;
    if (el.hasPointerCapture?.(e.pointerId)) el.releasePointerCapture(e.pointerId);
    // A press on the minimised bar that did not drag is a click: restore.
    if (kind === 'move' && minimised && !dragged) {
      toggleMinimised();
      return;
    }
    remember();
  }

  function refit() {
    if (open) rect = clampRect(rect, viewport(), minimised);
  }

  function handleKeydown(e: KeyboardEvent) {
    if (open && e.key === 'Escape') close();
  }
</script>

<svelte:window onkeydown={handleKeydown} onresize={refit} />

{#if open}
  {#if !minimised}<div class="agent-map-backdrop" role="presentation"></div>{/if}
  <div
    class="agent-map"
    class:minimised
    role="dialog"
    aria-modal={!minimised}
    aria-labelledby="agent-map-title"
    style="left: {rect.x}px; top: {rect.y}px; width: {rect.width}px; height: {minimised ? HEADER_HEIGHT : rect.height}px"
  >
    <div
      class="map-head"
      role="presentation"
      title={minimised ? 'Click to restore, drag to move' : 'Drag to move'}
      onpointerdown={(e) => beginGesture(e, 'move')}
      onpointermove={moveGesture}
      onpointerup={endGesture}
      onpointercancel={endGesture}
    >
      <div class="map-title">
        <h1 id="agent-map-title">Agent map</h1>
        <p class="muted">{minimised ? buttonSummary($agentTurns) : map.summaryLine}</p>
      </div>
      <div class="map-buttons">
        <button
          type="button"
          class="map-button"
          onclick={toggleMinimised}
          aria-label={minimised ? 'Restore the agent map' : 'Minimise the agent map'}
          title={minimised ? 'Restore' : 'Minimise'}
        >{minimised ? '▢' : '–'}</button>
        <button type="button" class="map-button" onclick={close} aria-label="Close the agent map" title="Close">×</button>
      </div>
    </div>

    {#if !minimised}
    <div class="map-body">

      {#if error}
        <div class="map-error" role="alert">{error}</div>
      {/if}

      {#if listing}
        <div class="memory" title={freeCommitLine(listing.free_commit)}>
          {#if map.memoryBars.length}
            {#each map.memoryBars as bar (bar.kind)}
              <div class="memory-bar">
                <span>{bar.label}{#if bar.note} <span class="bar-note">· {bar.note}</span>{/if}</span>
                <div class="bar-track"><div class="bar-fill {bar.kind}" style="width: {Math.round(bar.fraction * 100)}%"></div></div>
              </div>
            {/each}
            {#if map.memorySource}<span class="memory-source">{map.memorySource}</span>{/if}
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
              {#if map.finishedCapNote}
                <span class="muted small">{map.finishedCapNote}</span>
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
    <div
      class="resize-handle"
      role="presentation"
      title="Drag to resize"
      onpointerdown={(e) => beginGesture(e, 'resize')}
      onpointermove={moveGesture}
      onpointerup={endGesture}
      onpointercancel={endGesture}
    ></div>
    {/if}
  </div>
{/if}

<style>
  .agent-map-backdrop {
    position: fixed;
    inset: 0;
    z-index: 999;
    background: rgba(0, 0, 0, 0.5);
  }

  .agent-map {
    position: fixed;
    z-index: 1000;
    box-sizing: border-box;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    background: var(--bg-primary, #15171a);
    color: var(--text-primary, #e8eaed);
    border: 1px solid var(--border-color, #2c3036);
    border-radius: 10px;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.45);
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
  }

  .map-head {
    flex: 0 0 auto;
    box-sizing: border-box;
    height: 56px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 0 8px 0 16px;
    border-bottom: 1px solid var(--border-color, #2c3036);
    cursor: move;
    user-select: none;
    touch-action: none;
  }
  .agent-map.minimised .map-head { border-bottom: 0; cursor: pointer; }

  .map-title { display: flex; align-items: baseline; gap: 14px; min-width: 0; }
  .map-title h1 { margin: 0; font-size: 18px; font-weight: 600; white-space: nowrap; }
  .map-title p {
    margin: 0;
    font-size: 13px;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .map-buttons { display: flex; gap: 2px; flex: 0 0 auto; }
  .map-button {
    width: 40px;
    height: 40px;
    display: flex;
    align-items: center;
    justify-content: center;
    border: 0;
    border-radius: 8px;
    background: transparent;
    color: #c9d1d9;
    font-size: 22px;
    cursor: pointer;
  }
  .map-button:hover { color: #ffffff; background: var(--bg-hover, #22262c); }

  .map-body {
    flex: 1 1 auto;
    min-height: 0;
    overflow: auto;
    box-sizing: border-box;
    padding: 16px 20px 28px;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .resize-handle {
    position: absolute;
    right: 0;
    bottom: 0;
    width: 18px;
    height: 18px;
    cursor: nwse-resize;
    touch-action: none;
    background: linear-gradient(135deg, transparent 0 50%, #6e7681 50% 56%, transparent 56% 70%, #6e7681 70% 76%, transparent 76%);
    border-bottom-right-radius: 10px;
  }

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
  .bar-fill.vram { background: #a371f7; }
  .memory-source { font-size: 13px; color: var(--text-secondary, #a9b0bb); }
  .bar-note { font-size: 12px; color: var(--text-secondary, #a9b0bb); opacity: 0.8; }

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
