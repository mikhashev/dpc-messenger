<!-- MusicEvolution.svelte — how the current track evolved: its takes, edits and rejections as a branch graph (mp.lineage/1). -->
<!--
  Mike's direction (2026-11-20, SPEC §12 decision 36, ADR-051 D29): song creation is tracked as
  evolution, with branches and the reason a version was rejected. The core builds the graph
  (music/lineage.py, `get_music_lineage`, read-only) from the job store; this view only draws it.

  A node is a take (engine, QA badge, kept / discarded / pending) or the request that made a
  branch (intent), or a source file an edit started from. An edge says what was done (generated,
  repaint with its region) and, when it leads to a discarded take, the reason, so a dead branch says
  why. Click a take to select it and play it (the review's audio element, like the A/B view); the
  panel under the graph says what is known of it and can open it in the player. Nothing here
  keeps, discards or changes a take: that stays in the review (MusicReview.svelte).

  The graph is read when the card is opened and again when a job changes (Keep, Discard, a new edit
  take). "Save as files" writes lineage.json and a static lineage.html beside the job's folder.
  Not recorded yet, and said so in the card: a plain A/B preference without a Keep, what changed per edge.
-->

<script lang="ts">
  import { onDestroy } from 'svelte';
  import { get } from 'svelte/store';
  import { formatTime } from '$lib/utils/musicPlayer';
  import {
    NODE_H,
    NODE_W,
    clip,
    decisionWord,
    edgeLabelAt,
    edgeLabelText,
    edgePath,
    eventLine,
    focusNode,
    hasEvolution,
    layoutLineage,
    nodeLines,
    nodeTitle,
    nodeTone,
    playableNode,
    qaBadge,
    reasonText,
    splitNotes,
    type LineageNode,
    type MusicLineage,
  } from '$lib/utils/musicLineage';
  import { fetchMusicLineage, writeMusicLineageFiles } from '$lib/services/musicLineage';
  import {
    endReview,
    musicEntriesById,
    musicReview,
    openTrackInPlayer,
    reviewPlay,
    reviewTogglePlay,
    unhideTrack,
  } from '$lib/services/music';
  import { musicJobs } from '$lib/services/musicJobs';

  let { trackId = null }: { trackId?: string | null } = $props();

  let open = $state(false);
  let doc = $state<MusicLineage | null>(null);
  let loading = $state(false);
  let error = $state('');
  let notice = $state('');
  let selectedId = $state<string | null>(null);
  let startedPlayback = false;

  let layout = $derived(doc ? layoutLineage(doc) : null);
  let notes = $derived(doc ? splitNotes(doc) : { warnings: [], other: [] });
  let selected = $derived<LineageNode | null>(doc ? (doc.nodes.find(n => n.id === selectedId) ?? null) : null);
  let rv = $derived($musicReview);
  // A discarded take's library entry is hidden (Discard hides it); the card still plays it and can restore it.
  let selectedLib = $derived(selected ? playableNode(selected) : null);
  let selectedHidden = $derived(!!selectedLib && !!$musicEntriesById.get(selectedLib)?.hidden);
  // What changed in the jobs: a Keep, a Discard or a new take re-reads the graph while the card is open.
  let jobsSignature = $derived($musicJobs.jobs.map(j => `${j.id}:${j.state}:${j.updated_at ?? ''}`).join('|'));

  let loadedFor = '';
  let loadedSig = '';
  let ticket = 0;
  $effect(() => {
    const id = trackId;
    const sig = jobsSignature;
    if (!open || !id) return;
    if (id === loadedFor && sig === loadedSig) return;
    const changedTrack = id !== loadedFor;
    loadedFor = id;
    loadedSig = sig;
    if (changedTrack) {
      // Another track: the old graph goes, and the playback this view started is let go.
      doc = null;
      selectedId = null;
      notice = '';
      void stopOwnPlayback();
    }
    void load(id, changedTrack);
  });

  async function load(id: string, changedTrack: boolean) {
    const mine = ++ticket;
    loading = true;
    if (changedTrack) error = '';
    const a = await fetchMusicLineage(id);
    if (mine !== ticket) return;
    loading = false;
    if (!a.ok) {
      error = a.message;
      doc = null;
      return;
    }
    error = '';
    doc = a.data.lineage;
    if (!selectedId || !doc.nodes.some(n => n.id === selectedId)) selectedId = focusNode(doc)?.id ?? null;
  }

  async function stopOwnPlayback() {
    if (!startedPlayback) return;
    startedPlayback = false;
    await endReview();
  }

  function toggle() {
    open = !open;
    if (!open) {
      ticket++;
      loadedFor = '';
      loadedSig = '';
      doc = null;
      selectedId = null;
      void stopOwnPlayback();
    }
  }

  async function pick(node: LineageNode) {
    selectedId = node.id;
    notice = '';
    const lib = playableNode(node);
    if (!lib) return;
    startedPlayback = true;
    const ok = await reviewPlay(lib, { autoplay: true });
    if (!ok) notice = get(musicReview).notice || 'This take cannot play.';
  }

  function onNodeKey(e: KeyboardEvent, node: LineageNode) {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      void pick(node);
    }
  }

  async function openInPlayer(node: LineageNode) {
    const lib = playableNode(node);
    if (!lib) return;
    await stopOwnPlayback();
    await openTrackInPlayer(lib, { allowHidden: true });
  }

  async function restoreToLibrary(node: LineageNode) {
    const lib = playableNode(node);
    if (!lib) return;
    const a = await unhideTrack(lib);
    notice = a.ok ? 'Restored to the library: the take shows in the library again.' : a.message;
  }

  async function saveFiles() {
    if (!trackId) return;
    notice = '';
    const a = await writeMusicLineageFiles(trackId);
    notice = a.ok ? `Saved ${a.data.files.html} and lineage.json beside it.` : a.message;
  }

  function isPlaying(node: LineageNode): boolean {
    const lib = playableNode(node);
    return !!lib && rv.active && rv.trackId === lib && rv.playing;
  }

  function paramRows(node: LineageNode): Array<[string, string]> {
    const rows: Array<[string, string]> = [];
    for (const [k, v] of Object.entries(node.params ?? {})) {
      if (v === null || v === undefined) continue;
      rows.push([k.replace(/_/g, ' '), typeof v === 'object' ? JSON.stringify(v) : String(v)]);
    }
    return rows;
  }

  onDestroy(() => {
    ticket++;
    void stopOwnPlayback();
  });
</script>

<section class="card" aria-labelledby="evolution-label">
  <div class="section-head">
    <span id="evolution-label" class="section-label">EVOLUTION {#if doc && hasEvolution(doc)}<span class="count">{doc.stats.takes}</span>{/if}</span>
    <span class="muted small">how this song came about: takes, edits and rejections</span>
    <button type="button" class="small-btn head-btn" onclick={toggle} aria-expanded={open} disabled={!trackId}>{open ? 'Hide' : 'Show'}</button>
  </div>

  {#if open}
    {#if loading && !doc}
      <p class="muted small" role="status">Reading the lineage…</p>
    {/if}
    {#if error}<p class="field-error" role="alert">{error}</p>{/if}

    {#if doc && !hasEvolution(doc)}
      <p class="muted small">No evolution recorded for this track: it was not made by a DPC job, and no edit of it was made here.</p>
    {:else if doc && layout}
      <p class="muted small">
        {doc.stats.jobs} job{doc.stats.jobs === 1 ? '' : 's'}, {doc.stats.takes} take{doc.stats.takes === 1 ? '' : 's'}:
        {doc.stats.kept} kept, {doc.stats.discarded} discarded, {doc.stats.pending} pending.
        Dashed red edges lead to a discarded take and say why. Click a take to hear it.
      </p>
      {#each notes.warnings as w (w)}<p class="small qa-failed" role="status">{w}</p>{/each}
      <div class="graph" tabindex="-1">
        <svg width={layout.width} height={layout.height} viewBox="0 0 {layout.width} {layout.height}" role="group" aria-label="Lineage graph of this track">
          {#each doc.edges as e (e.id)}
            {@const a = layout.positions[e.from]}
            {@const b = layout.positions[e.to]}
            {#if a && b}
              {@const at = edgeLabelAt(a, b)}
              <path class="edge" class:rejected={e.rejected} d={edgePath(a, b)} />
              <text class="elabel" class:rejected={e.rejected} x={at.x} y={at.y} text-anchor="middle">{clip(edgeLabelText(e), 58)}<title>{edgeLabelText(e)}</title></text>
            {/if}
          {/each}
          {#each doc.nodes as n (n.id)}
            {@const p = layout.positions[n.id]}
            {@const lines = nodeLines(n)}
            {@const q = n.kind === 'take' ? qaBadge(n.qa) : null}
            <g class="node {nodeTone(n)}" class:kept-path={n.on_kept_path} class:selected={n.id === selectedId} class:playing={isPlaying(n)}
              role="button" tabindex="0" aria-label={nodeTitle(n)} aria-pressed={n.id === selectedId}
              onclick={() => pick(n)} onkeydown={(e) => onNodeKey(e, n)}>
              <title>{nodeTitle(n)}</title>
              <rect x={p.x} y={p.y} width={NODE_W} height={NODE_H} rx="6" />
              <text x={p.x + 8} y={p.y + 18} class="l1">{lines[0]}</text>
              <text x={p.x + 8} y={p.y + 34} class="l2">{lines[1]}</text>
              <text x={p.x + 8} y={p.y + 50} class="l3" class:qa-failed={q?.kind === 'failed'}>{lines[2]}</text>
            </g>
          {/each}
        </svg>
      </div>

      {#if selected}
        <div class="detail">
          <div class="detail-head">
            <strong>{selected.label}</strong>
            <span class="muted small">{selected.kind === 'take' ? (selected.take_kind === 'edit' ? 'edit take' : 'song take') : selected.kind === 'intent' ? 'what was asked' : 'source file'}</span>
            {#if selected.kind === 'take'}<span class="tag tone-{nodeTone(selected)}">{decisionWord(selected.decision)}</span>{/if}
          </div>
          {#if selected.kind === 'take'}
            <p class="small">
              {#if selected.qa}<span class:qa-failed={qaBadge(selected.qa).kind === 'failed'}>{selected.qa.text}</span>{/if}
              {#if typeof selected.duration_s === 'number'} · {formatTime(selected.duration_s)}{/if}
              {#if typeof selected.lufs_i === 'number'} · {selected.lufs_i.toFixed(1)} LUFS{/if}
            </p>
            {#if selected.decision === 'discard'}
              <p class="small reason">Rejected: {reasonText(selected.reason) || 'no reason recorded'}</p>
            {/if}
          {:else if selected.kind === 'intent' && selected.state_reason}
            <p class="muted small">{selected.state_reason}</p>
          {/if}
          {#if paramRows(selected).length}
            <dl class="params">
              {#each paramRows(selected) as [k, v] (k)}<dt>{k}</dt><dd>{v}</dd>{/each}
            </dl>
          {/if}
          <div class="form-row">
            {#if playableNode(selected)}
              <button type="button" class="small-btn" onclick={() => (isPlaying(selected!) ? reviewTogglePlay() : pick(selected!))}>{isPlaying(selected) ? 'Pause' : 'Play'}</button>
              <button type="button" class="small-btn" onclick={() => openInPlayer(selected!)}>Open in player</button>
              {#if selectedHidden}
                <button type="button" class="small-btn" onclick={() => restoreToLibrary(selected!)}
                  title="Unhide this take's library entry; it stays hidden unless you choose this">Restore to library</button>
              {/if}
            {:else if selected.kind === 'take'}
              <span class="muted small">This take has no library entry to play.</span>
            {/if}
          </div>
        </div>
      {/if}

      {#if doc.events.length}
        <ol class="events">
          {#each doc.events as ev (ev.id)}<li>{eventLine(doc, ev)}</li>{/each}
        </ol>
      {/if}
      <div class="form-row">
        <button type="button" class="small-btn" onclick={saveFiles} title="Write lineage.json and a static lineage.html beside the job's generated folder">Save as files</button>
        <span class="muted tiny">{notes.other.join(' ')}</span>
      </div>
    {/if}
    {#if notice}<p class="muted small" role="status" aria-live="polite">{notice}</p>{/if}
  {/if}
</section>

<style>
  .card {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 12px 14px;
    background: var(--bg-secondary, #1d2025);
    border: 1px solid var(--border-color, #2c3036);
    border-radius: 10px;
  }
  .section-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .section-label { font-size: 12px; font-weight: 700; letter-spacing: 0.06em; color: var(--text-secondary, #a9b0bb); }
  .count { font-weight: 400; margin-left: 4px; }
  .head-btn { margin-left: auto; }
  .muted { color: var(--text-secondary, #a9b0bb); }
  .small { font-size: 13px; margin: 0; }
  .tiny { font-size: 11px; }
  .field-error { margin: 0; color: #f85149; font-size: 13px; }
  .graph { overflow: auto; max-height: 420px; border: 1px solid var(--border-color, #2c3036); border-radius: 8px; background: var(--bg-primary, #14161a); }
  svg { display: block; }
  .edge { fill: none; stroke: #6b7280; stroke-width: 1.5; }
  .edge.rejected { stroke: #f85149; stroke-dasharray: 5 4; }
  .elabel { font-size: 11px; fill: var(--text-secondary, #a9b0bb); }
  .elabel.rejected { fill: #f85149; }
  .node { cursor: pointer; outline: none; }
  .node rect { fill: var(--bg-secondary, #1d2025); stroke: #4b5260; stroke-width: 1; }
  .node.keep rect { fill: #17321f; stroke: #3fb950; }
  .node.discard rect { fill: #24262b; stroke: #6b7280; stroke-dasharray: 4 3; }
  .node.pending rect { stroke-dasharray: 2 3; }
  .node.intent rect, .node.source rect { stroke: #58a6ff; }
  .node.kept-path rect { stroke-width: 2.5; }
  .node.selected rect, .node:focus-visible rect { stroke: #e8eaed; stroke-width: 2.5; }
  .node.playing rect { fill: #1b2c46; }
  .node text { fill: var(--text-primary, #e8eaed); font-size: 12px; pointer-events: none; }
  .node .l1 { font-weight: 600; }
  .node .l2, .node .l3 { fill: var(--text-secondary, #a9b0bb); font-size: 11px; }
  .node .l3.qa-failed, .qa-failed { fill: #f0b429; color: #f0b429; }
  .detail { display: flex; flex-direction: column; gap: 6px; padding: 8px 10px; border-radius: 8px; background: var(--bg-primary, #14161a); border: 1px solid var(--border-color, #2c3036); }
  .detail-head { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
  .tag { padding: 1px 8px; border-radius: 999px; font-size: 11px; border: 1px solid #4b5260; }
  .tag.tone-keep { border-color: #3fb950; color: #3fb950; }
  .tag.tone-discard { border-color: #f85149; color: #f85149; }
  .reason { color: #f85149; }
  .params { display: grid; grid-template-columns: max-content 1fr; gap: 2px 12px; margin: 0; font-size: 12px; }
  .params dt { color: var(--text-secondary, #a9b0bb); }
  .params dd { margin: 0; overflow-wrap: anywhere; }
  .events { margin: 0; padding-left: 18px; font-size: 13px; display: flex; flex-direction: column; gap: 2px; }
  .form-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .small-btn {
    padding: 3px 10px;
    border-radius: 6px;
    border: 1px solid #3a3f47;
    background: #22262c;
    color: var(--text-primary, #e8eaed);
    font: inherit;
    font-size: 12px;
    cursor: pointer;
  }
  .small-btn:hover:not(:disabled) { background: #2a2f37; }
  .small-btn:disabled { opacity: 0.5; cursor: default; }
</style>
