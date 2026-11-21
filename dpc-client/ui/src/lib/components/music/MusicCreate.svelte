<!-- MusicCreate.svelte — the Music Player's Create panel: a song from your own text (SPEC flow "song from own text", slice 1). -->
<!--
  Opened from the window header's Create button; Listen stays the default view. Four parts:

  - the engine line of the engine the form has selected (yue2.cpp, the default since Mike
    #198, or acestep.cpp; both supervised by DPC, one on the card at a time): not installed
    (with the reason), stopped, starting, running or error, live from `music_engine_status`,
    with Start / Stop for that engine;
  - the form: the engine; for YuE2 the backbone and an optional voice adapter (none by
    default; voice adapters are for local listening only, the note says why) with its music
    and sound strengths; lyrics (section tags such as [verse] allowed, sent verbatim), style,
    variants, duration (for YuE2 a cap, with a warning when it looks short for the lyrics:
    an estimate) and an optional seed, checked against the core's limits before sending; a
    core refusal is shown on the field it names. Generate is off, with the reason, while the
    selected engine is not available;
  - the jobs: state and progress live from `music_job_updated` (polled when quiet), Cancel,
    "Waiting for the GPU: X holds it" with a "Stop X" button (the person's press, the
    existing stop_local_engine; nothing is stopped by itself), failures with the reason;
  - the review of a job's takes (MusicReview.svelte).

  The state lives in services/musicJobs.ts, so a job is followed and the draft kept while
  this panel or the window is closed. The shared control styles of the three music
  components are here, under `.mc-root`.
-->

<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { connectionStatus } from '$lib/coreService';
  import {
    BACKBONE_CHOICES,
    ENGINE_CHOICES,
    VOICE_NOTE,
    backbonePresent,
    cardHolders,
    defaultEngineOf,
    engineCanStart,
    engineCanStop,
    engineKeyOf,
    engineStatusText,
    formFieldOf,
    formForEngine,
    fromTextSupport,
    isActive,
    jobStateText,
    jobTitle,
    limitsFrom,
    shortCapWarning,
    validateFromTextForm,
    yue2Adapters,
    type EngineKey,
    type FormErrors,
    type MusicEngine,
    type MusicJob,
  } from '$lib/utils/musicCreate';
  import {
    cancelJob,
    loadMusicJobs,
    musicCreateDraft,
    musicJobs,
    musicReviewJobId,
    proposeFromText,
    startEngine,
    startJobWatch,
    stopCardHolder,
    stopEngine,
  } from '$lib/services/musicJobs';
  import MusicReview from './MusicReview.svelte';

  const JOB_ROWS = 20;

  let jobsState = $derived($musicJobs);
  let caps = $derived(jobsState.capabilities);
  let selectedEngine = $derived<EngineKey>($musicCreateDraft.engine ?? defaultEngineOf(caps));
  let engineChoice = $derived(ENGINE_CHOICES.find(e => e.key === selectedEngine) ?? ENGINE_CHOICES[0]);
  let engine = $derived<MusicEngine | null>(
    jobsState.engines[selectedEngine]
      ?? (engineKeyOf(jobsState.engine) === selectedEngine ? jobsState.engine : null)
      ?? caps?.engines?.[selectedEngine]
      ?? null);
  let limits = $derived(limitsFrom(caps, selectedEngine));
  let support = $derived(fromTextSupport(caps, selectedEngine));
  let adapters = $derived(yue2Adapters(caps));
  let capWarning = $derived(shortCapWarning($musicCreateDraft, caps));
  let classes = $derived(caps?.take_decisions?.failure_classes ?? []);
  let jobs = $derived(jobsState.jobs);
  let showAllJobs = $state(false);
  let shownJobs = $derived(showAllJobs ? jobs : jobs.slice(0, JOB_ROWS));
  let reviewJob = $derived<MusicJob | null>(jobs.find(j => j.id === $musicReviewJobId) ?? null);

  let stopWatch: (() => void) | null = null;
  onMount(() => {
    void loadMusicJobs();
    stopWatch = startJobWatch();
  });
  onDestroy(() => stopWatch?.());

  // A reconnected backend: read again.
  let lastConnection = $connectionStatus;
  $effect(() => {
    const c = $connectionStatus;
    if (c === 'connected' && lastConnection !== 'connected') void loadMusicJobs();
    lastConnection = c;
  });

  // -- the engine ------------------------------------------------------------------

  let engineBusy = $state(false);
  let engineError = $state('');

  async function engineAction(start: boolean) {
    engineBusy = true;
    engineError = '';
    try {
      const a = start
        ? await startEngine(selectedEngine, selectedEngine === 'yue2' ? $musicCreateDraft.backbone : undefined)
        : await stopEngine(selectedEngine);
      // A start can outlast the call's wait; the status event says how it ends.
      if (!a.ok && !/timed out/i.test(a.message)) engineError = a.message;
    } finally {
      engineBusy = false;
    }
  }

  // -- the form --------------------------------------------------------------------

  let errors = $state<FormErrors>({});
  let generating = $state(false);
  let generated = $state('');

  function clearError(field: keyof FormErrors) {
    if (errors[field] || errors.form) errors = { ...errors, [field]: undefined, form: undefined };
  }

  function pickEngine(value: string) {
    const key: EngineKey = value === 'acestep' ? 'acestep' : 'yue2';
    musicCreateDraft.update(f => formForEngine(f, key, caps));
    errors = {};
    engineError = '';
  }

  function newKey(): string {
    try {
      return crypto.randomUUID();
    } catch {
      return `k${Date.now()}${Math.random().toString(16).slice(2)}`;
    }
  }

  async function generate() {
    if (generating) return;
    generated = '';
    const v = validateFromTextForm($musicCreateDraft, limits);
    if (!v.ok) {
      errors = v.errors;
      return;
    }
    errors = {};
    generating = true;
    try {
      const a = await proposeFromText(v.params, newKey());
      if (!a.ok) {
        errors = { [formFieldOf(a.field)]: a.message };
        return;
      }
      generated = a.data.job.state === 'unsupported'
        ? `Not queued: ${a.data.job.state_reason ?? 'unsupported'}`
        : 'Queued. Follow it in Jobs below.';
    } finally {
      generating = false;
    }
  }

  // -- jobs ------------------------------------------------------------------------

  let jobBusy = $state<string | null>(null);
  let jobError = $state('');

  async function cancel(job: MusicJob) {
    jobBusy = `cancel:${job.id}`;
    jobError = '';
    try {
      const a = await cancelJob(job.id);
      if (!a.ok) jobError = a.message;
    } finally {
      jobBusy = null;
    }
  }

  async function stopHolder(alias: string) {
    jobBusy = `stop:${alias}`;
    jobError = '';
    try {
      const a = await stopCardHolder(alias);
      if (!a.ok) jobError = `Stop ${alias}: ${a.message}`;
    } finally {
      jobBusy = null;
    }
  }

  function openReview(job: MusicJob) {
    musicReviewJobId.set(job.id);
  }

  function hasTakes(job: MusicJob): boolean {
    return ['review', 'accepted', 'rejected'].includes(job.state) && (job.take_ids?.length ?? 0) > 0;
  }
</script>

<div class="mc-root" data-music-create>
  {#if jobsState.status === 'unavailable' || jobsState.status === 'error'}
    <div class="banner {jobsState.status === 'error' ? 'error' : 'info'}" role={jobsState.status === 'error' ? 'alert' : 'status'}>
      <span>{jobsState.message}</span>
      <button type="button" class="small-btn" onclick={() => loadMusicJobs()}>Try again</button>
    </div>
  {/if}

  <section class="card" aria-labelledby="mc-engine-label">
    <div class="section-head">
      <span id="mc-engine-label" class="section-label">ENGINE</span>
      <span class="engine-line" class:warn={engine?.state === 'not_installed' || engine?.state === 'error' || engine?.state === 'not_built'} role="status" aria-live="polite">
        {engineStatusText(engine)}
      </span>
      <span class="muted tiny">{engineChoice.label}</span>
      {#if engineCanStop(engine)}
        <button type="button" class="small-btn" onclick={() => engineAction(false)} disabled={engineBusy} aria-label="Stop the {engineChoice.label} engine">{engineBusy ? 'Stopping…' : 'Stop'}</button>
      {:else}
        <button type="button" class="small-btn" onclick={() => engineAction(true)} disabled={engineBusy || !engineCanStart(engine)} aria-label="Start the {engineChoice.label} engine">{engineBusy ? 'Starting…' : 'Start'}</button>
      {/if}
    </div>
    {#if engineError}<p class="field-error" role="alert">{engineError}</p>{/if}
  </section>

  {#if reviewJob}
    <MusicReview job={reviewJob} {classes} onclose={() => musicReviewJobId.set(null)} />
  {/if}

  <section class="card" aria-labelledby="mc-form-label">
    <div class="section-head">
      <span id="mc-form-label" class="section-label">NEW SONG FROM YOUR TEXT</span>
      <span class="muted tiny">{engineChoice.detail}</span>
    </div>

    <div class="form-row">
      <div class="field">
        <label class="field-label" for="mc-engine">Engine</label>
        <select id="mc-engine" class="list-select" value={selectedEngine} onchange={(e) => pickEngine((e.currentTarget as HTMLSelectElement).value)}
          aria-invalid={!!errors.engine} aria-describedby="mc-engine-error">
          {#each ENGINE_CHOICES as choice (choice.key)}
            <option value={choice.key}>{choice.label}{choice.key === defaultEngineOf(caps) ? ' (default)' : ''}</option>
          {/each}
        </select>
        <p id="mc-engine-error" class="field-error" role="alert">{errors.engine ?? ''}</p>
      </div>
      {#if selectedEngine === 'yue2'}
        <div class="field">
          <label class="field-label" for="mc-backbone">Backbone</label>
          <select id="mc-backbone" class="list-select" bind:value={$musicCreateDraft.backbone} onchange={() => clearError('backbone')}
            aria-invalid={!!errors.backbone} aria-describedby="mc-backbone-error">
            {#each BACKBONE_CHOICES as b (b.key)}
              <option value={b.key} disabled={!backbonePresent(caps, b.key)}>{b.label}{backbonePresent(caps, b.key) ? '' : ' (not installed)'}</option>
            {/each}
          </select>
          <p id="mc-backbone-error" class="field-error" role="alert">{errors.backbone ?? ''}</p>
        </div>
        <div class="field">
          <label class="field-label" for="mc-voice">Voice (optional)</label>
          <select id="mc-voice" class="list-select" bind:value={$musicCreateDraft.voice} onchange={() => clearError('voice')}
            aria-invalid={!!errors.voice} aria-describedby="mc-voice-note mc-voice-error">
            <option value="">None</option>
            {#each adapters as a (a.name)}
              <option value={a.name}>{a.name}</option>
            {/each}
          </select>
          <p id="mc-voice-error" class="field-error" role="alert">{errors.voice ?? ''}</p>
        </div>
      {/if}
    </div>
    {#if selectedEngine === 'yue2'}
      {#if $musicCreateDraft.voice}
        <div class="form-row">
          <div class="field">
            <label class="field-label" for="mc-voice-music">Voice strength, music half</label>
            <input id="mc-voice-music" class="text-input num" type="number" min="0" max="2" step="0.05"
              bind:value={$musicCreateDraft.voiceMusic} oninput={() => clearError('voice')} />
          </div>
          <div class="field">
            <label class="field-label" for="mc-voice-sound">Voice strength, sound half</label>
            <input id="mc-voice-sound" class="text-input num" type="number" min="0" max="2" step="0.05"
              bind:value={$musicCreateDraft.voiceSound} oninput={() => clearError('voice')} />
          </div>
        </div>
      {/if}
      <p id="mc-voice-note" class="muted tiny">{caps?.engines?.yue2?.adapter_note ?? VOICE_NOTE}{adapters.length ? '' : ' No adapters were found in the adapters folder.'}</p>
    {/if}

    <label class="field-label" for="mc-lyrics">Lyrics</label>
    <textarea
      id="mc-lyrics"
      class="text-input lyrics"
      rows="8"
      placeholder={'[verse]\nFirst line of the song\n\n[chorus]\nThe chorus line'}
      bind:value={$musicCreateDraft.lyrics}
      oninput={() => clearError('lyrics')}
      disabled={$musicCreateDraft.instrumental}
      aria-invalid={!!errors.lyrics}
      aria-describedby="mc-lyrics-help mc-lyrics-error"
    ></textarea>
    <p id="mc-lyrics-help" class="muted tiny">Section tags such as [verse], [chorus], [bridge] are allowed. The text is sent exactly as written (line breaks and stress marks stay).</p>
    <label class="check">
      <input type="checkbox" bind:checked={$musicCreateDraft.instrumental} onchange={() => clearError('lyrics')} />
      <span class="small">Instrumental (no lyrics)</span>
    </label>
    <p id="mc-lyrics-error" class="field-error" role="alert">{errors.lyrics ?? ''}</p>

    <label class="field-label" for="mc-style">Style</label>
    <input
      id="mc-style"
      class="text-input"
      type="text"
      placeholder="e.g. indie rock, warm guitars, female vocal"
      bind:value={$musicCreateDraft.style}
      oninput={() => clearError('style')}
      aria-invalid={!!errors.style}
      aria-describedby="mc-style-error"
    />
    <p id="mc-style-error" class="field-error" role="alert">{errors.style ?? ''}</p>

    <div class="form-row">
      <div class="field">
        <label class="field-label" for="mc-variants">Variants</label>
        <input
          id="mc-variants"
          class="text-input num"
          type="number"
          min={limits.variants.min}
          max={limits.variants.max}
          step="1"
          bind:value={$musicCreateDraft.variants}
          oninput={() => clearError('variants')}
          aria-invalid={!!errors.variants}
          aria-describedby="mc-variants-error"
        />
        <p id="mc-variants-error" class="field-error" role="alert">{errors.variants ?? ''}</p>
      </div>
      <div class="field">
        <label class="field-label" for="mc-duration">{selectedEngine === 'yue2' ? 'Length cap (s)' : 'Duration (s)'}</label>
        <input
          id="mc-duration"
          class="text-input num"
          type="number"
          min={limits.duration_s.min}
          max={limits.duration_s.max}
          step="1"
          bind:value={$musicCreateDraft.duration}
          oninput={() => clearError('duration')}
          aria-invalid={!!errors.duration}
          aria-describedby="mc-duration-error mc-duration-warn"
        />
        <p id="mc-duration-error" class="field-error" role="alert">{errors.duration ?? ''}</p>
      </div>
      <div class="field">
        <label class="field-label" for="mc-seed">Seed (optional)</label>
        <input
          id="mc-seed"
          class="text-input num"
          type="text"
          inputmode="numeric"
          placeholder="random"
          bind:value={$musicCreateDraft.seed}
          oninput={() => clearError('seed')}
          aria-invalid={!!errors.seed}
          aria-describedby="mc-seed-error"
        />
        <p id="mc-seed-error" class="field-error" role="alert">{errors.seed ?? ''}</p>
      </div>
    </div>
    {#if capWarning}<p id="mc-duration-warn" class="warn-text" role="status">{capWarning}</p>{/if}
    <p class="muted tiny">Variants {limits.variants.min}–{limits.variants.max} · {selectedEngine === 'yue2' ? 'length cap' : 'duration'} {limits.duration_s.min}–{limits.duration_s.max} s{selectedEngine === 'yue2' ? ' (the song may end earlier; each variant runs on its own seed, seed + 1, …)' : ''} · an empty seed is drawn by DPC.</p>

    <div class="form-row">
      <button type="button" class="small-btn primary big" onclick={generate} disabled={generating || !support.supported}>
        {generating ? 'Sending…' : 'Generate'}
      </button>
      {#if errors.form}<span class="field-error" role="alert">{errors.form}</span>{/if}
      {#if generated}<span class="muted small" role="status">{generated}</span>{/if}
    </div>
    {#if !support.supported}
      <p class="muted small" role="status">Generate is off: {support.reason}</p>
    {/if}
  </section>

  <section class="card" aria-labelledby="mc-jobs-label">
    <div class="section-head">
      <span id="mc-jobs-label" class="section-label">JOBS {#if jobs.length}<span class="count">{jobs.length}</span>{/if}</span>
      {#if jobsState.status === 'loading'}<span class="muted small">Loading…</span>{/if}
    </div>
    {#if jobError}<p class="field-error" role="alert">{jobError}</p>{/if}
    {#if jobsState.status === 'ready' && !jobs.length}<p class="muted small">No songs made yet.</p>{/if}
    <ol class="rows">
      {#each shownJobs as job (job.id)}
        {@const holders = cardHolders(job)}
        <li class="row job" class:current={job.id === $musicReviewJobId} class:failed={job.state === 'failed' || job.state === 'unsupported'}>
          <div class="job-main">
            <span class="track-title" title={job.params?.style ?? ''}>{jobTitle(job)}</span>
            <span class="muted tiny job-state" role="status" aria-live="polite">{jobStateText(job)}</span>
            {#if job.state === 'running'}
              <progress class="job-progress" max="1" value={job.progress ?? 0} aria-label="Progress of {jobTitle(job, 40)}"></progress>
            {/if}
            <span class="muted tiny">{job.params?.engine === 'yue2' ? 'YuE2' : 'ACE-Step'} · {job.params?.variants ?? '?'} × {job.params?.duration_s ?? '?'} s{job.params?.seed != null ? ` · seed ${job.params.seed}` : ''}{job.params?.adapters?.length ? ` · voice ${job.params.adapters[0].name}` : ''} · {new Date(job.created_at).toLocaleString()}</span>
          </div>
          <span class="row-actions">
            {#each holders as h (h.alias)}
              <button type="button" class="small-btn danger" onclick={() => stopHolder(h.alias)} disabled={jobBusy === `stop:${h.alias}`}
                aria-label="Stop {h.alias} to free the GPU for this job">{jobBusy === `stop:${h.alias}` ? `Stopping ${h.alias}…` : `Stop ${h.alias}`}</button>
            {/each}
            {#if isActive(job)}
              <button type="button" class="small-btn" onclick={() => cancel(job)} disabled={jobBusy === `cancel:${job.id}`} aria-label="Cancel {jobTitle(job, 40)}">Cancel</button>
            {/if}
            {#if hasTakes(job)}
              <button type="button" class="small-btn primary" onclick={() => openReview(job)} aria-pressed={job.id === $musicReviewJobId}>
                {job.state === 'review' ? 'Review' : 'Open'}
              </button>
            {/if}
          </span>
        </li>
      {/each}
    </ol>
    {#if jobs.length > JOB_ROWS}
      <button type="button" class="small-btn more" onclick={() => (showAllJobs = !showAllJobs)}>
        {showAllJobs ? 'Show fewer' : `Show all (${jobs.length})`}
      </button>
    {/if}
  </section>
</div>

<style>
  .mc-root { display: flex; flex-direction: column; gap: 12px; min-width: 0; }

  .mc-root :global(.card) {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 12px 14px;
    background: var(--bg-secondary, #1d2025);
    border: 1px solid var(--border-color, #2c3036);
    border-radius: 10px;
  }
  .mc-root :global(.section-head) { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .mc-root :global(.section-head > .section-label) { margin-right: auto; }
  .mc-root :global(.section-label) { font-size: 12px; font-weight: 700; letter-spacing: 0.06em; color: var(--text-secondary, #a9b0bb); }
  .mc-root :global(.count) { margin-left: 4px; color: #c9d1d9; }
  .mc-root :global(.muted) { color: var(--text-secondary, #a9b0bb); }
  .mc-root :global(.small) { font-size: 13px; }
  .mc-root :global(.tiny) { font-size: 12px; }
  .mc-root :global(p) { margin: 0; }
  .mc-root :global(.field-error) { margin: 0; min-height: 0; font-size: 12px; color: #f5a5a5; }
  .mc-root :global(.field-error:empty) { display: none; }

  .mc-root :global(.banner) { display: flex; align-items: center; gap: 12px; justify-content: space-between; padding: 10px 14px; border-radius: 8px; font-size: 14px; }
  .mc-root :global(.banner.info) { background: #1d2a3d; color: #b6d4ff; }
  .mc-root :global(.banner.error) { background: #3d1d1d; color: #f5a5a5; }

  .mc-root :global(.small-btn) {
    padding: 3px 10px;
    border-radius: 6px;
    border: 1px solid #3a3f47;
    background: #22262c;
    color: #c9d1d9;
    font: inherit;
    font-size: 12px;
    cursor: pointer;
    white-space: nowrap;
  }
  .mc-root :global(.small-btn:hover:not(:disabled)) { background: #2c3036; color: #ffffff; }
  .mc-root :global(.small-btn:disabled) { opacity: 0.5; cursor: default; }
  .mc-root :global(.small-btn.primary) { border-color: #58a6ff; color: #58a6ff; }
  .mc-root :global(.small-btn.danger) { border-color: #8b3a3a; color: #f5a5a5; }
  .mc-root :global(.small-btn.big) { padding: 6px 18px; font-size: 14px; }
  .mc-root :global(.small-btn.more) { align-self: center; }
  .mc-root :global(button:focus-visible),
  .mc-root :global(input:focus-visible),
  .mc-root :global(select:focus-visible),
  .mc-root :global(textarea:focus-visible) { outline: 2px solid #58a6ff; outline-offset: 1px; }

  .mc-root :global(.text-input), .mc-root :global(.list-select) {
    padding: 4px 8px;
    border-radius: 6px;
    border: 1px solid var(--border-color, #2c3036);
    background: var(--bg-primary, #14161a);
    color: var(--text-primary, #e8eaed);
    font: inherit;
    font-size: 13px;
    min-width: 0;
    box-sizing: border-box;
  }
  .mc-root :global(.text-input[aria-invalid='true']) { border-color: #8b3a3a; }
  .mc-root :global(.text-input:disabled) { opacity: 0.55; }
  /* The dark select of MusicPlayer (debe5546): WebKitGTK paints its light GTK menulist
     unless native appearance is dropped; background-color keeps the arrow image. */
  .mc-root :global(.list-select) {
    color-scheme: dark;
    -webkit-appearance: none;
    appearance: none;
    background-color: var(--bg-primary, #14161a);
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%23aaa'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 8px center;
    padding-right: 24px;
  }
  .mc-root :global(.text-input.num) { color-scheme: dark; width: 100%; }

  .field-label { font-size: 13px; color: #c9d1d9; }
  .lyrics { width: 100%; resize: vertical; min-height: 120px; font-family: ui-monospace, 'Cascadia Mono', Consolas, monospace; line-height: 1.45; }
  .check { display: flex; align-items: center; gap: 6px; cursor: pointer; }
  .form-row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
  .field { display: flex; flex-direction: column; gap: 4px; flex: 1 1 120px; min-width: 0; }
  .engine-line { font-size: 13px; color: #c9d1d9; }
  .engine-line.warn { color: #f0c674; }
  .warn-text { font-size: 12px; color: #f0c674; }

  .mc-root :global(.rows) { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
  .mc-root :global(.row) {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 8px;
    border-radius: 8px;
    background: #22262c;
    border: 1px solid transparent;
    font-size: 13px;
    min-width: 0;
  }
  .mc-root :global(.row.current) { border-color: #58a6ff; background: #232a35; }
  .mc-root :global(.row-actions) { display: flex; gap: 4px; align-items: center; flex: 0 0 auto; flex-wrap: wrap; justify-content: flex-end; }
  .mc-root :global(.track-title) { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .mc-root :global(.tag) { font-size: 11px; padding: 1px 6px; border-radius: 5px; background: #31353d; color: #c9d1d9; white-space: nowrap; }
  .mc-root :global(.tag.warn) { background: #3d3016; color: #f0c674; }
  .job { align-items: flex-start; }
  .job.failed .job-state { color: #f5a5a5; }
  .job-main { flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column; gap: 3px; }
  .job-progress { width: 100%; max-width: 320px; height: 6px; accent-color: #58a6ff; }

  /* The review (MusicReview.svelte) and the reason box (MusicReasonBox.svelte). */
  .mc-root :global(.ab-bar) { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .mc-root :global(.ab-btn) {
    min-width: 40px;
    height: 36px;
    border-radius: 8px;
    border: 1px solid #3a3f47;
    background: #22262c;
    color: #e8eaed;
    font: inherit;
    font-size: 16px;
    font-weight: 700;
    cursor: pointer;
  }
  .mc-root :global(.ab-btn.on) { border-color: #58a6ff; background: #1d2a3d; color: #58a6ff; }
  .mc-root :global(.ab-btn.decided) { opacity: 0.7; }
  .mc-root :global(.t-btn) {
    width: 44px;
    height: 44px;
    border-radius: 50%;
    border: 1px solid #58a6ff;
    background: #58a6ff;
    color: #0d1117;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    margin-left: 6px;
  }
  .mc-root :global(.t-btn:disabled) { opacity: 0.5; cursor: default; }
  .mc-root :global(.seek-row) { display: flex; align-items: center; gap: 10px; }
  .mc-root :global(.seek) { flex: 1 1 auto; min-width: 0; }
  .mc-root :global(.seek-col) { flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column; }
  .mc-root :global(.seek-col .seek) { width: 100%; }
  .mc-root :global(.time) { font-size: 12px; color: var(--text-secondary, #a9b0bb); font-variant-numeric: tabular-nums; white-space: nowrap; }
  .mc-root :global(.level) { font-size: 13px; color: #9fd29f; }
  .mc-root :global(.level.warn) { color: #f0c674; }
  .mc-root :global(.outcome) { font-size: 13px; color: #c9d1d9; }
  .mc-root :global(.take) {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 8px 10px;
    border-radius: 8px;
    background: #22262c;
    border: 1px solid transparent;
  }
  .mc-root :global(.take.current) { border-color: #58a6ff; }
  .mc-root :global(.take-head) { display: flex; align-items: center; gap: 8px; min-width: 0; }
  .mc-root :global(.take-letter) { font-weight: 700; color: #58a6ff; width: 16px; flex: 0 0 auto; }
  .mc-root :global(.take-actions), .mc-root :global(.reason-row) { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
  .mc-root :global(.reason-box) {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 8px 10px;
    border-radius: 8px;
    border: 1px solid #3a3f47;
    background: #1a1d22;
  }
  .mc-root :global(.reason-row > .text-input) { flex: 1 1 220px; }
  .mc-root :global(.reason-class) { flex: 0 1 260px; }
  .mc-root :global(.reason-suggestions) { display: flex; gap: 4px; flex-wrap: wrap; align-items: center; }
  .mc-root :global(.chip) {
    padding: 2px 8px;
    border-radius: 12px;
    border: 1px solid #3a3f47;
    background: #22262c;
    color: #c9d1d9;
    font: inherit;
    font-size: 12px;
    cursor: pointer;
  }
  .mc-root :global(.chip:hover) { background: #2c3036; color: #ffffff; }
</style>
