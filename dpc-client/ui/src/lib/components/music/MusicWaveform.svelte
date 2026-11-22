<!-- MusicWaveform.svelte — the waveform view of a track: bars, time ruler, playhead, comment markers. -->
<!--
  Replaces the plain position slider on the Now playing card and in the A/B review. The core
  decodes the audio (ffmpeg) and sends `[min, max]` peaks (services/musicPeaks.ts); this draws
  them on a canvas: the bars once into two offscreen layers (unplayed grey, played colour), then
  every frame only composites the played part, the comment markers, the playhead and the hover
  line. Click seeks (`onseek`: the caller's own safe seek), a drag selects a region and opens the
  comment form on release (`onregion`); Region (R) arms a click to make a short region at that point.

  A real range input stays in the DOM, visually hidden while the waveform shows: arrows, Home/End
  and screen readers keep working through it. While the peaks load a skeleton shows; when they
  cannot be made the old slider and marker strip come back with the reason. The Spectrogram is
  planned: its button is disabled.
-->

<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import { formatTime } from '$lib/utils/musicPlayer';
  import { commentTimeLabel, type MusicComment } from '$lib/utils/musicComments';
  import {
    barCount,
    barRect,
    chooseTicks,
    dragToRegion,
    formatTimeFine,
    hitMarker,
    rulerNeedsRedraw,
    normalizeRegion,
    resamplePeaks,
    tickTimes,
    timeToX,
    xToTime,
    DRAG_THRESHOLD_PX,
  } from '$lib/utils/musicWaveform';
  import { loadPeaks, type TrackPeaks } from '$lib/services/musicPeaks';
  import CommentMarkers from './CommentMarkers.svelte';

  const HEIGHT = 72;
  const RULER_HEIGHT = 18;
  const BAR_PX = 2;
  const GAP_PX = 1;
  const DEFAULT_REGION_S = 4;

  let {
    trackId = null,
    comments = [],
    position = 0,
    duration = 0,
    seekable = true,
    ariaLabel = 'Position in the track',
    draft = null,
    regionSignal = 0,
    onseek,
    onregion,
  }: {
    trackId?: string | null;
    comments?: MusicComment[];
    position?: number;
    duration?: number;
    /** False while nothing is loaded to seek (clicks then do nothing; regions still work). */
    seekable?: boolean;
    ariaLabel?: string;
    /** A region chosen but not saved yet (the comment form is open on it). */
    draft?: { start: number; end: number } | null;
    /** Bumped by the parent (the R key outside this view) to toggle the region mode. */
    regionSignal?: number;
    onseek: (seconds: number) => void;
    onregion: (start: number, end: number) => void;
  } = $props();

  let status = $state<'empty' | 'loading' | 'ready' | 'failed'>('empty');
  let reason = $state('');
  let data = $state<TrackPeaks | null>(null);

  $effect(() => {
    const id = trackId;
    data = null;
    reason = '';
    if (!id) {
      status = 'empty';
      return;
    }
    status = 'loading';
    let live = true;
    loadPeaks(id).then(
      r => {
        if (!live) return;
        data = r;
        status = 'ready';
      },
      (e: unknown) => {
        if (!live) return;
        reason = e instanceof Error ? e.message : String(e);
        status = 'failed';
      },
    );
    return () => {
      live = false;
    };
  });

  // No track: nothing to seek in or select, whatever duration the parent still passes.
  let total = $derived(status === 'empty' ? 0 : duration > 0 ? duration : (data?.duration ?? 0));
  let showWave = $derived(status !== 'failed');

  // -- the range input: the accessible and the fallback control --------------------

  let scrub = $state<number | null>(null);
  function onScrub(e: Event) {
    scrub = parseFloat((e.currentTarget as HTMLInputElement).value);
  }
  function onSeekCommit(e: Event) {
    const v = parseFloat((e.currentTarget as HTMLInputElement).value);
    scrub = null;
    if (Number.isFinite(v)) onseek(v);
  }

  // -- canvas ----------------------------------------------------------------------

  let wrap = $state<HTMLDivElement | null>(null);
  let stage = $state<HTMLDivElement | null>(null);
  let canvas = $state<HTMLCanvasElement | null>(null);
  let rulerCanvas = $state<HTMLCanvasElement | null>(null);
  let width = $state(0);
  let dpr = 1;
  let layerGrey: HTMLCanvasElement | null = null;
  let layerPlayed: HTMLCanvasElement | null = null;
  let colors = { unplayed: '#4a505a', played: '#58a6ff', head: '#e8eaed', point: '#e3b341', region: 'rgba(88,166,255,0.22)', regionEdge: 'rgba(88,166,255,0.8)', text: '#a9b0bb', tick: '#4a505a' };
  let colorsVersion = $state(0);

  function readColors() {
    if (!wrap) return;
    const cs = getComputedStyle(wrap);
    const get = (name: string, fallback: string) => cs.getPropertyValue(name).trim() || fallback;
    colors = {
      unplayed: get('--wf-unplayed', colors.unplayed),
      played: get('--wf-played', colors.played),
      head: get('--wf-head', colors.head),
      point: get('--wf-point', colors.point),
      region: get('--wf-region', colors.region),
      regionEdge: get('--wf-region-edge', colors.regionEdge),
      text: get('--wf-text', colors.text),
      tick: get('--wf-tick', colors.tick),
    };
    colorsVersion += 1;
  }

  function makeLayer(color: string, bars: ReturnType<typeof resamplePeaks>, stepPx: number): HTMLCanvasElement {
    const layer = document.createElement('canvas');
    layer.width = Math.max(1, Math.round(width * dpr));
    layer.height = Math.max(1, Math.round(HEIGHT * dpr));
    const g = layer.getContext('2d');
    if (!g) return layer;
    g.scale(dpr, dpr);
    g.fillStyle = color;
    for (let i = 0; i < bars.length; i++) {
      const r = barRect(bars[i], HEIGHT);
      g.fillRect(i * stepPx, r.y, BAR_PX, r.h);
    }
    return layer;
  }

  /** Static layers: the bars, drawn once per size, colour or data change. */
  function rebuildLayers() {
    layerGrey = layerPlayed = null;
    if (!canvas || !data || width <= 0) return;
    dpr = Math.max(1, window.devicePixelRatio || 1);
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(HEIGHT * dpr);
    const n = barCount(width, BAR_PX, GAP_PX);
    const bars = resamplePeaks(data.peaks, n);
    // Spread the bars over the full width so the last one ends at the right edge.
    const step = bars.length > 1 ? (width - BAR_PX) / (bars.length - 1) : BAR_PX + GAP_PX;
    layerGrey = makeLayer(colors.unplayed, bars, step);
    layerPlayed = makeLayer(colors.played, bars, step);
  }

  function drawRuler() {
    const c = rulerCanvas;
    if (!c || width <= 0) return;
    dpr = Math.max(1, window.devicePixelRatio || 1);
    if (!(total > 0)) {
      c.width = Math.round(width * dpr); // resizing also clears a stale ruler
      c.height = Math.round(RULER_HEIGHT * dpr);
      return;
    }
    c.width = Math.round(width * dpr);
    c.height = Math.round(RULER_HEIGHT * dpr);
    const g = c.getContext('2d');
    if (!g) return;
    g.scale(dpr, dpr);
    g.clearRect(0, 0, width, RULER_HEIGHT);
    const plan = chooseTicks(total, width);
    g.strokeStyle = colors.tick;
    g.fillStyle = colors.text;
    g.font = '10px system-ui, sans-serif';
    g.textBaseline = 'top';
    g.lineWidth = 1;
    for (const t of tickTimes(total, plan)) {
      const x = Math.min(width - 0.5, Math.max(0.5, Math.round(timeToX(t.t, total, width)) + 0.5));
      g.beginPath();
      g.moveTo(x, 0);
      g.lineTo(x, t.labelled ? 6 : 3);
      g.stroke();
      if (t.labelled) {
        const label = formatTime(t.t);
        const w = g.measureText(label).width;
        const lx = Math.min(width - w, Math.max(0, x - w / 2));
        g.fillText(label, lx, 7);
      }
    }
  }

  let hoverX = $state<number | null>(null);
  let dragFrom = $state<number | null>(null);
  let dragTo = $state<number | null>(null);
  let regionMode = $state(false);

  function drawFrame() {
    raf = 0;
    const c = canvas;
    const g = c?.getContext('2d');
    if (!c || !g || width <= 0) return;
    dpr = Math.max(1, window.devicePixelRatio || 1);
    g.setTransform(1, 0, 0, 1, 0, 0);
    g.clearRect(0, 0, c.width, c.height);
    if (status === 'empty') {
      // A calm, static centre line: nothing is loaded, nothing is animated.
      g.fillStyle = colors.unplayed;
      g.globalAlpha = 0.45;
      g.fillRect(0, Math.round(c.height / 2) - 1, c.width, Math.max(1, Math.round(dpr)));
      g.globalAlpha = 1;
      return;
    }
    if (!layerGrey || !layerPlayed) return;
    g.drawImage(layerGrey, 0, 0);
    const px = timeToX(scrub ?? position, total, width);
    const pw = Math.round(px * dpr);
    if (pw > 0) g.drawImage(layerPlayed, 0, 0, pw, layerPlayed.height, 0, 0, pw, layerPlayed.height);
    g.setTransform(dpr, 0, 0, dpr, 0, 0);

    const band = (a: number, b: number, edge: boolean) => {
      const x0 = timeToX(a, total, width);
      const x1 = timeToX(b, total, width);
      g.fillStyle = colors.region;
      g.fillRect(x0, 0, Math.max(2, x1 - x0), HEIGHT);
      if (edge) {
        g.fillStyle = colors.regionEdge;
        g.fillRect(x0, 0, 1, HEIGHT);
        g.fillRect(Math.max(x0, x1 - 1), 0, 1, HEIGHT);
      }
    };
    for (const m of comments) if (m.t_end != null) band(m.t_start, m.t_end, false);
    if (draft) band(draft.start, draft.end, true);
    if (dragFrom != null && dragTo != null) {
      const r = normalizeRegion(xToTime(dragFrom, total, width), xToTime(dragTo, total, width), total, 0);
      if (r) band(r.start, r.end, true);
    }
    for (const m of comments) {
      if (m.t_end != null) continue;
      const x = Math.round(timeToX(m.t_start, total, width)) + 0.5;
      g.fillStyle = colors.point;
      g.fillRect(x - 0.5, 0, 1, HEIGHT);
      g.beginPath();
      g.arc(x, 4, 3.5, 0, Math.PI * 2);
      g.fill();
    }
    if (hoverX != null && dragFrom == null) {
      g.fillStyle = colors.text;
      g.globalAlpha = 0.5;
      g.fillRect(Math.round(hoverX), 0, 1, HEIGHT);
      g.globalAlpha = 1;
    }
    g.fillStyle = colors.head;
    g.fillRect(Math.min(width - 2, Math.max(0, px - 1)), 0, 2, HEIGHT);
  }

  let raf = 0;
  function schedule() {
    if (!raf) raf = requestAnimationFrame(drawFrame);
  }

  $effect(() => {
    void width;
    void data;
    void status;
    void colorsVersion;
    void canvas;
    untrack(() => {
      rebuildLayers();
      drawRuler();
      schedule();
    });
  });

  // The ruler is a static offscreen layer drawn from width/data only; it must also follow the
  // duration: peaks usually land before the element knows it (total 0), and it is refined later.
  let rulerTotal = 0;
  $effect(() => {
    const t = total;
    untrack(() => {
      const redraw = rulerNeedsRedraw(rulerTotal, t);
      rulerTotal = t;
      if (redraw) {
        drawRuler();
        schedule();
      }
    });
  });

  $effect(() => {
    void position;
    void scrub;
    void comments;
    void draft;
    void hoverX;
    void dragFrom;
    void dragTo;
    void total;
    void colorsVersion;
    schedule();
  });

  onMount(() => {
    readColors();
    const ro = new ResizeObserver(entries => {
      const w = Math.floor(entries[0]?.contentRect.width ?? 0);
      if (w !== width) width = w;
    });
    if (stage) ro.observe(stage);
    const mo = new MutationObserver(readColors);
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ['class', 'data-theme', 'style'] });
    const mq = window.matchMedia?.('(prefers-color-scheme: dark)');
    mq?.addEventListener?.('change', readColors);
    return () => {
      ro.disconnect();
      mo.disconnect();
      mq?.removeEventListener?.('change', readColors);
      if (raf) cancelAnimationFrame(raf);
    };
  });

  // -- pointer ---------------------------------------------------------------------

  let tip = $state<{ x: number; text: string } | null>(null);
  let downX: number | null = null;

  function localX(e: PointerEvent): number {
    const r = stage!.getBoundingClientRect();
    return Math.max(0, Math.min(r.width, e.clientX - r.left));
  }

  function updateHover(x: number) {
    hoverX = x;
    const id = hitMarker(x, comments, total, width);
    const c = id ? comments.find(m => m.id === id) : null;
    const text = c
      ? `${commentTimeLabel(c, formatTime)}: ${c.text.length > 140 ? c.text.slice(0, 140) + '…' : c.text}`
      : formatTimeFine(xToTime(x, total, width));
    tip = { x, text };
  }

  function onPointerDown(e: PointerEvent) {
    if (e.button !== 0 || !(total > 0) || !stage) return;
    stage.setPointerCapture(e.pointerId);
    downX = localX(e);
    dragFrom = downX;
    dragTo = downX;
    wrap?.focus({ preventScroll: true });
  }

  function onPointerMove(e: PointerEvent) {
    if (!stage || !(total > 0)) return;
    const x = localX(e);
    updateHover(x);
    if (downX != null) {
      dragTo = x;
      if (Math.abs(x - downX) < DRAG_THRESHOLD_PX) return;
    }
  }

  function defaultRegionAt(t: number) {
    const end = Math.min(total, t + DEFAULT_REGION_S);
    return normalizeRegion(Math.max(0, end - DEFAULT_REGION_S), end, total);
  }

  function onPointerUp(e: PointerEvent) {
    if (downX == null || !stage) return;
    const x = localX(e);
    const from = downX;
    downX = null;
    dragFrom = dragTo = null;
    if (stage.hasPointerCapture(e.pointerId)) stage.releasePointerCapture(e.pointerId);
    const region = dragToRegion(from, x, total, width);
    if (region) {
      regionMode = false;
      onregion(region.start, region.end);
      return;
    }
    if (Math.abs(x - from) >= DRAG_THRESHOLD_PX) return; // a drag too short to be a region: nothing
    if (regionMode) {
      const r = defaultRegionAt(xToTime(x, total, width));
      regionMode = false;
      if (r) onregion(r.start, r.end);
      return;
    }
    if (!seekable) return;
    const id = hitMarker(x, comments, total, width);
    const c = id ? comments.find(m => m.id === id) : null;
    onseek(c ? c.t_start : xToTime(x, total, width));
  }

  function onPointerCancel() {
    downX = null;
    dragFrom = dragTo = null;
  }

  function onPointerLeave() {
    if (downX != null) return;
    hoverX = null;
    tip = null;
  }

  // -- Region (R) ------------------------------------------------------------------

  let lastSignal = untrack(() => regionSignal);
  $effect(() => {
    const n = regionSignal;
    if (n !== lastSignal) {
      lastSignal = n;
      regionMode = !regionMode;
    }
  });

  function onKeydown(e: KeyboardEvent) {
    if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return;
    if (e.key.toLowerCase() === 'r') {
      if (!(total > 0)) return;
      e.preventDefault();
      e.stopPropagation();
      regionMode = !regionMode;
    } else if (e.key === 'Escape' && regionMode) {
      e.stopPropagation();
      regionMode = false;
    }
  }
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="wf" class:fallback={!showWave} bind:this={wrap} tabindex="-1" onkeydown={onKeydown}>
  {#if showWave}
    <div
      class="stage"
      class:region-mode={regionMode && status !== 'empty'}
      class:skeleton={status === 'loading'}
      class:empty={status === 'empty'}
      bind:this={stage}
      role="presentation"
      style="height:{HEIGHT}px"
      onpointerdown={onPointerDown}
      onpointermove={onPointerMove}
      onpointerup={onPointerUp}
      onpointercancel={onPointerCancel}
      onpointerleave={onPointerLeave}
    >
      <canvas bind:this={canvas} style="width:{width}px;height:{HEIGHT}px" aria-hidden="true"></canvas>
      {#if status === 'loading'}<span class="skel-note">Drawing the waveform…</span>{/if}
      {#if status === 'empty'}<span class="skel-note empty-note" role="status">No track selected — pick one from the library to see its waveform.</span>{/if}
      {#if tip && status === 'ready'}<span class="tip" style="left:{tip.x}px" class:flip={tip.x > width - 160}>{tip.text}</span>{/if}
    </div>
    <canvas class="ruler" bind:this={rulerCanvas} style="width:{width}px;height:{RULER_HEIGHT}px" aria-hidden="true"></canvas>
  {/if}

  <input
    class="seek"
    class:visually-hidden={showWave}
    type="range"
    min="0"
    max={total || 0}
    step={showWave ? 1 : 0.1}
    value={scrub ?? position}
    oninput={onScrub}
    onchange={onSeekCommit}
    onblur={() => (scrub = null)}
    disabled={!seekable || !(total > 0)}
    aria-label={ariaLabel}
    aria-valuetext="{formatTime(scrub ?? position)} of {formatTime(total || null)}"
  />

  {#if !showWave}
    <CommentMarkers {comments} duration={total} {onseek} />
    <p class="note" role="status">waveform unavailable: {reason}</p>
  {/if}

  <div class="tools">
    <button
      type="button"
      class="tool"
      class:on={regionMode}
      aria-pressed={regionMode}
      disabled={!(total > 0)}
      onclick={() => (regionMode = !regionMode)}
      title="Drag across the waveform to select a region, or press R and click for a {DEFAULT_REGION_S} s one"
    >Region <kbd>R</kbd></button>
    <button type="button" class="tool" disabled title="planned">Spectrogram <kbd>S</kbd></button>
    {#if regionMode}<span class="hint">Click or drag on the waveform</span>{/if}
  </div>
</div>

<style>
  .wf {
    --wf-unplayed: var(--text-secondary, #6b7280);
    --wf-played: var(--accent-color, #58a6ff);
    --wf-head: var(--text-primary, #e8eaed);
    --wf-point: #e3b341;
    --wf-region: rgba(88, 166, 255, 0.22);
    --wf-region-edge: rgba(88, 166, 255, 0.85);
    --wf-text: var(--text-secondary, #a9b0bb);
    --wf-tick: var(--border-color, #4a505a);
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
    width: 100%;
    outline: none;
  }
  .stage {
    position: relative;
    width: 100%;
    border-radius: 6px;
    background: var(--bg-primary, #14161a);
    border: 1px solid var(--border-color, #2c3036);
    box-sizing: content-box;
    cursor: pointer;
    touch-action: none;
    user-select: none;
  }
  .wf:focus-within .stage { outline: 2px solid var(--wf-played); outline-offset: 1px; }
  .stage.region-mode { cursor: crosshair; }
  .stage canvas { display: block; }
  .stage.empty { cursor: default; }
  .stage.skeleton {
    background: linear-gradient(90deg, var(--bg-primary, #14161a), var(--bg-secondary, #1d2025), var(--bg-primary, #14161a));
    background-size: 200% 100%;
    animation: wf-pulse 1.4s linear infinite;
    cursor: default;
  }
  .skel-note { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; font-size: 12px; color: var(--wf-text); }
  .empty-note { align-items: flex-end; padding: 0 8px 6px; text-align: center; pointer-events: none; }
  .ruler { display: block; }
  .tip {
    position: absolute;
    top: 4px;
    transform: translateX(6px);
    max-width: 280px;
    padding: 2px 6px;
    border-radius: 4px;
    background: var(--bg-secondary, #1d2025);
    border: 1px solid var(--border-color, #3a3f47);
    color: var(--text-primary, #e8eaed);
    font-size: 12px;
    font-variant-numeric: tabular-nums;
    pointer-events: none;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    z-index: 2;
  }
  .tip.flip { transform: translateX(calc(-100% - 6px)); }
  .seek { width: 100%; min-width: 0; }
  .visually-hidden {
    position: absolute;
    width: 1px;
    height: 1px;
    margin: -1px;
    padding: 0;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
    border: 0;
  }
  .note { margin: 0; font-size: 12px; color: var(--wf-text); }
  .tools { display: flex; align-items: center; gap: 6px; margin-top: 2px; }
  .tool {
    padding: 2px 8px;
    border-radius: 6px;
    border: 1px solid var(--border-color, #3a3f47);
    background: var(--bg-secondary, #22262c);
    color: var(--text-primary, #c9d1d9);
    font: inherit;
    font-size: 12px;
    cursor: pointer;
  }
  .tool:hover:not(:disabled) { background: var(--bg-hover, #2c3036); }
  .tool:disabled { opacity: 0.5; cursor: default; }
  .tool.on { border-color: var(--wf-played); color: var(--wf-played); }
  .tool kbd { font-family: inherit; font-size: 11px; opacity: 0.7; margin-left: 2px; }
  .hint { font-size: 12px; color: var(--wf-text); }
  @keyframes wf-pulse { from { background-position: 200% 0; } to { background-position: -200% 0; } }
</style>
