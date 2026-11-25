<!-- MusicReview.svelte — the plain, level-matched A/B of a job's takes (SPEC flow 7, §12 decisions 35, 37). -->
<!--
  The takes are A, B (C, D with more variants), on one shared transport and a waveform
  (MusicWaveform: each take draws its own peaks and markers). Switching take keeps the position: the other take is loaded at the
  same time, clamped to its length (takes differ by a frame or more). Keys while focus is
  in this view: A / B / C / D pick a take (the letter of the take playing flips back to
  the one before, so B toggles A and B, SPEC §9), Space plays / pauses, ← → seek 5 s, R region.

  Level matching (decision 37, display gain only, the files are never changed): every
  take plays at the quietest take's integrated loudness (the core's ffmpeg ebur128
  measurement, `take.loudness`); the louder ones are turned down, nothing is turned up.
  A take without a measurement is measured on opening (`measure_music_take_loudness`);
  until all are measured the line says "not level-matched" and nothing is changed.

  Playback is services/music.ts's one audio element (`reviewPlay`): the review takes it
  from the queue while it plays and gives it back, paused where it was, when the view
  closes (`endReview`).

  QC (core music/qc.py, `mp.qc/1`): each take shows its stage-1 badge, and the judge's overall score,
  scores and issues once a judge has run. **Judge** (only when the core has a judge alias configured,
  `get_music_job_qc.judge_alias`) asks the audio judge on demand; **Measure QA** measures stage 1 for a
  take that is "not measured" (a take made before QA). Takes are shown in the core's display order only
  when ranking is on, otherwise in variant order. QC is advice: it never blocks Keep or Discard.

  Keep marks the take accepted, with an optional add-to-playlist (there is no track rename
  in the library yet). Discard and "None of these" need a reason (MusicReasonBox).

  A region edit's takes (ADR-051 row B4) are sibling files of the edited track, which stays as it
  is; the view names the region and "Play region" starts the take 2 s before it (SPEC flow 6).
-->

<script lang="ts">
  import { onDestroy } from 'svelte';
  import { formatTime } from '$lib/utils/musicPlayer';
  import {
    abKeyTarget,
    abSwitchPosition,
    formatDb,
    jobStateText,
    levelMatch,
    reasonPayload,
    takeDecidable,
    takeDecisionText,
    takeInfo,
    takeLetter,
    takeQaBadge,
    type FailureClass,
    type MusicJob,
    type MusicTake,
  } from '$lib/utils/musicCreate';
  import {
    endReview,
    musicLibrary,
    musicPlayback,
    musicReview,
    reviewPlay,
    reviewPosition,
    reviewSeek,
    reviewSetGain,
    reviewTogglePlay,
    setPlaylistTracks,
  } from '$lib/services/music';
  import {
    discardTake,
    fetchJobQc,
    fetchTakes,
    judgeTake,
    keepTake,
    measureTakeLoudness,
    measureTakeQa,
    musicJobQc,
    musicJobQcError,
    musicTakes,
    rejectAllTakes,
  } from '$lib/services/musicJobs';
  import { judgeOffered, lastAttemptLine, measureQaOffered, orderTakes, qcIssueCount, qcLine, qcUnavailableText } from '$lib/utils/musicQc';
  import MusicReasonBox from './MusicReasonBox.svelte';
  import MusicComments from './MusicComments.svelte';
  import MusicWaveform from './MusicWaveform.svelte';
  import { editJobLine } from '$lib/utils/musicEdit';
  import { musicComments } from '$lib/services/musicComments';

  let { job, classes = [], onclose }: { job: MusicJob; classes?: FailureClass[]; onclose: () => void } = $props();

  let rawTakes = $derived<MusicTake[]>($musicTakes[job.id] ?? []);
  let jobQc = $derived($musicJobQc[job.id] ?? null);
  // Why the QC view could not be read: the Judge button and the ranked order depend on it, so say so
  // (with a retry) instead of letting both vanish.
  let jobQcError = $derived($musicJobQcError[job.id] ?? '');
  let qcRetrying = $state(false);
  async function retryJobQc() {
    if (qcRetrying) return;
    qcRetrying = true;
    try {
      await fetchJobQc(job.id);
    } finally {
      qcRetrying = false;
    }
  }
  // Variant order, or the core's ranked order when ranking is on.
  let takes = $derived(orderTakes(rawTakes, jobQc));
  let loadError = $state('');
  let actionError = $state('');
  let notice = $state('');

  // Read the takes when the view opens on a job (and again when it opens on another).
  let openedFor = '';
  $effect(() => {
    const id = job.id;
    if (id === openedFor) return;
    // Another job's takes: the take of the last one is let go (the queue comes back).
    if (openedFor) void endReview();
    openedFor = id;
    loadError = '';
    currentId = null;
    previousId = null;
    qcError = {};
    fetchTakes(id).then(a => {
      if (!a.ok) loadError = a.message;
    });
    void fetchJobQc(id);
  });

  // A take with no loudness is measured once (the core answers it as stored when it has one).
  const asked = new Set<string>();
  let measureError = $state('');
  $effect(() => {
    for (const t of takes) {
      if (t.loudness || asked.has(t.id)) continue;
      asked.add(t.id);
      measureTakeLoudness(t.id).then(a => {
        if (a && !a.ok) measureError = `Loudness of ${t.title}: ${a.message}`;
      });
    }
  });

  let lm = $derived(levelMatch(takes.map(t => t.loudness)));

  // -- the transport ----------------------------------------------------------------

  // The picked take is kept by id: a new display order (a judge ranked them) moves the letters, not the pick.
  let currentId = $state<string | null>(null);
  let previousId = $state<string | null>(null);
  let current = $derived(Math.max(0, takes.findIndex(t => t.id === currentId)));
  let previousIndex = $derived(previousId === null ? -1 : takes.findIndex(t => t.id === previousId));
  let previous = $derived<number | null>(previousIndex >= 0 ? previousIndex : null);
  let rv = $derived($musicReview);
  let take = $derived<MusicTake | null>(takes[current] ?? null);
  let loaded = $derived(!!take?.library_track_id && rv.active && rv.trackId === take.library_track_id);
  // Where the bar stands before anything is loaded (a pick of another take keeps it).
  let parked = $state(0);
  let position = $derived(loaded ? rv.currentTime : parked);
  let duration = $derived(loaded && rv.duration > 0 ? rv.duration : (take?.file?.duration_s ?? 0));

  // The gain follows the measurement: a loudness that arrives while a take plays applies at once.
  $effect(() => {
    const g = lm.gain[current] ?? 1;
    if (loaded) reviewSetGain(g);
  });

  function playable(t: MusicTake | null): boolean {
    return !!t?.library_track_id;
  }

  async function load(i: number, at: number, autoplay: boolean) {
    const t = takes[i];
    if (!t?.library_track_id) {
      notice = `${takeLetter(i)} is not in the library, so it cannot play here.`;
      return;
    }
    notice = '';
    await reviewPlay(t.library_track_id, { at, autoplay, gain: lm.gain[i] ?? 1 });
  }

  async function togglePlay() {
    if (loaded) {
      await reviewTogglePlay();
      return;
    }
    await load(current, parked, true);
  }

  /** Switch to take `i` at the same position. */
  /** An edit's region with 2 s of context before it, on the take picked now. */
  async function playRegion() {
    if (!job.region) return;
    await load(current, Math.max(0, job.region.t_start - 2), true);
  }

  async function switchTo(i: number) {
    if (i < 0 || i >= takes.length) return;
    if (i === current && (loaded || !rv.active)) return;
    const at = loaded ? reviewPosition() : parked;
    const wasPlaying = loaded && rv.playing;
    previousId = takes[current]?.id ?? null;
    currentId = takes[i].id;
    parked = abSwitchPosition(at, takes[i]?.file?.duration_s ?? null);
    // Nothing is loaded yet: only pick it; the queue is not stopped until Play.
    if (!rv.active) return;
    await load(i, at, wasPlaying);
  }

  /** A comment's time was clicked: seek the take being heard (or park the bar before it loads). */
  function seekComment(t: number) {
    if (loaded) reviewSeek(t);
    else parked = abSwitchPosition(t, duration);
  }
  let regionSignal = $state(0);
  let regionRequest = $state<{ id: number; start: number; end: number } | null>(null);
  let draftRegion = $state<{ start: number; end: number } | null>(null);
  function pickRegion(start: number, end: number) {
    draftRegion = { start, end };
    regionRequest = { id: (regionRequest?.id ?? 0) + 1, start, end };
  }
  let takeComments = $derived($musicComments[take?.library_track_id ?? ''] ?? []);

  function seekBy(delta: number) {
    if (loaded) reviewSeek(reviewPosition() + delta);
    else parked = abSwitchPosition(parked + delta, duration);
  }

  function onKeydown(e: KeyboardEvent) {
    const t = e.target as HTMLElement;
    if (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)) return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === ' ') {
      if (t.tagName === 'BUTTON') return;
      e.preventDefault();
      togglePlay();
    } else if (e.key.toLowerCase() === 'r' && !e.shiftKey) {
      e.preventDefault();
      regionSignal += 1;
    } else if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      e.preventDefault();
      seekBy((e.key === 'ArrowLeft' ? -1 : 1) * (e.shiftKey ? 1 : 5));
    } else {
      const target = abKeyTarget(e.key, takes.length, current, previous);
      if (target !== null) {
        e.preventDefault();
        switchTo(target);
      }
    }
  }

  // -- QC: the judge and the stage-1 measurement ------------------------------------

  let qcBusy = $state<Record<string, 'judge' | 'measure'>>({});
  let qcError = $state<Record<string, string>>({});

  function qcSet(id: string, kind: 'judge' | 'measure' | null, error = '') {
    const busy = { ...qcBusy };
    if (kind) busy[id] = kind;
    else delete busy[id];
    qcBusy = busy;
    qcError = { ...qcError, [id]: error };
  }

  async function runJudge(t: MusicTake) {
    if (qcBusy[t.id]) return;
    qcSet(t.id, 'judge');
    try {
      const a = await judgeTake(t);
      qcSet(t.id, null, a.ok ? '' : `Judge: ${a.message}`);
    } catch (e) {
      qcSet(t.id, null, `Judge: ${e instanceof Error ? e.message : String(e)}`);
    }
  }

  async function runMeasure(t: MusicTake) {
    if (qcBusy[t.id]) return;
    qcSet(t.id, 'measure');
    try {
      const a = await measureTakeQa(t);
      qcSet(t.id, null, a.ok ? '' : a.message);
    } catch (e) {
      qcSet(t.id, null, `Measure QA: ${e instanceof Error ? e.message : String(e)}`);
    }
  }

  // -- decisions --------------------------------------------------------------------

  let reasonFor = $state<string | null>(null); // a take id, or 'all'
  let keepFor = $state<string | null>(null);
  let keepPlaylist = $state('');
  let keepBusy = $state(false);
  let userLists = $derived($musicLibrary.playlists.filter(p => p.kind === 'user'));
  let pendingCount = $derived(takes.filter(t => t.decision === 'pending').length);

  function openReason(target: string) {
    keepFor = null;
    reasonFor = target;
    actionError = '';
  }

  function openKeep(t: MusicTake) {
    reasonFor = null;
    keepFor = t.id;
    keepPlaylist = '';
    actionError = '';
  }

  async function submitDiscard(t: MusicTake, text: string, cls: string | null): Promise<string | null> {
    const a = await discardTake(t, reasonPayload(text, cls));
    if (!a.ok) return a.message;
    reasonFor = null;
    notice = a.data.library_hidden
      ? `Discarded ${t.title}: hidden from the library; the file stays.`
      : `Discarded ${t.title}${a.data.library_reason ? ` (library: ${a.data.library_reason})` : ''}.`;
    return null;
  }

  async function submitNone(text: string, cls: string | null): Promise<string | null> {
    const a = await rejectAllTakes(job.id, reasonPayload(text, cls));
    if (!a.ok) return a.message;
    reasonFor = null;
    notice = 'None of these: every pending take is discarded and hidden; the files stay.';
    return null;
  }

  async function submitKeep(t: MusicTake) {
    if (keepBusy) return;
    keepBusy = true;
    actionError = '';
    try {
      const a = await keepTake(t.id);
      if (!a.ok) {
        actionError = a.message;
        return;
      }
      let added = '';
      const list = userLists.find(p => p.id === keepPlaylist);
      if (list && t.library_track_id) {
        if (list.track_ids.includes(t.library_track_id)) added = ` It is already in ${list.name}.`;
        else {
          const r = await setPlaylistTracks(list.id, [...list.track_ids, t.library_track_id]);
          added = r.ok ? ` Added to ${list.name}.` : ` Kept, but adding to ${list.name} failed: ${r.message}`;
        }
      }
      keepFor = null;
      notice = `Kept ${t.title}.${added}`;
    } finally {
      keepBusy = false;
    }
  }

  function keepKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      keepFor = null;
    }
  }

  async function close() {
    await endReview();
    onclose();
  }

  onDestroy(() => {
    void endReview();
  });
</script>

<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<section class="card review" aria-labelledby="mc-review-label" onkeydown={onKeydown}>
  <div class="section-head">
    <span id="mc-review-label" class="section-label">REVIEW</span>
    <span class="muted small">{jobStateText(job)}</span>
    <button type="button" class="small-btn" onclick={close} aria-label="Close the review (the queue comes back where it was)">Close review</button>
  </div>
  {#if loadError}<p class="field-error" role="alert">Could not read the takes: {loadError}</p>{/if}
  {#if !takes.length && !loadError}<p class="muted small">Reading the takes…</p>{/if}
  {#if jobQcError}
    <p class="field-error qc-unavailable" role="alert">{qcUnavailableText(jobQcError)}
      <button type="button" class="small-btn" onclick={retryJobQc} disabled={qcRetrying}>{qcRetrying ? 'Retrying…' : 'Retry'}</button>
    </p>
  {/if}

  {#if job.type === 'edit' && job.region}
    <p class="muted small edit-line">
      {editJobLine(job)}: each take is a new file; the edited track stays as it is.
      <button type="button" class="small-btn" onclick={playRegion} disabled={!playable(take)}
        title="Play the picked take from 2 s before the region">Play region</button>
    </p>
  {/if}

  {#if takes.length}
    <div class="ab-bar" role="group" aria-label="Takes">
      {#each takes as t, i (t.id)}
        <button
          type="button"
          class="ab-btn"
          class:on={i === current}
          class:decided={t.decision !== 'pending'}
          aria-pressed={i === current}
          aria-label="Take {takeLetter(i)}: {t.title}{t.decision !== 'pending' ? ` (${takeDecisionText(t)})` : ''}"
          title="Take {takeLetter(i)} (key {takeLetter(i)})"
          onclick={() => switchTo(i)}
        >{takeLetter(i)}</button>
      {/each}
      <button
        type="button"
        class="t-btn play"
        onclick={togglePlay}
        disabled={!playable(take)}
        aria-label={loaded && rv.playing ? 'Pause the take' : `Play take ${takeLetter(current)}`}
        title="Play / pause (Space)"
      >
        <svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          {#if loaded && rv.playing}<path d="M7 5v14M17 5v14" stroke-width="3.5" />{:else}<path d="m7 4 13 8-13 8z" fill="currentColor" />{/if}
        </svg>
      </button>
    </div>
    <div class="seek-row">
      <span class="time">{formatTime(position)}</span>
      <div class="seek-col">
        <MusicWaveform
          trackId={take?.library_track_id ?? null}
          comments={takeComments}
          {position}
          {duration}
          seekable={!!duration}
          ariaLabel="Position in take {takeLetter(current)}"
          draft={draftRegion}
          {regionSignal}
          onseek={seekComment}
          onregion={pickRegion}
        />
      </div>
      <span class="time">{formatTime(duration || null)}</span>
    </div>
    <p class="level" class:warn={!lm.matched} role="status">{lm.label}</p>
    {#if measureError}<p class="muted tiny">{measureError}</p>{/if}
    {#if rv.loading}<p class="muted tiny">Loading take {takeLetter(current)}…</p>{/if}
    {#if rv.notice || notice}<p class="muted small" role="status" aria-live="polite">{rv.notice || notice}</p>{/if}
    {#if actionError}<p class="field-error" role="alert">{actionError}</p>{/if}
    <p class="muted tiny">A / B{takes.length > 2 ? ` / ${takes.slice(2).map((_, i) => takeLetter(i + 2)).join(' / ')}` : ''} pick a take at the same position · Space play/pause · ← → seek · R region · your volume applies on top</p>

    {#if take?.library_track_id}
      <MusicComments
        trackId={take.library_track_id}
        currentTime={position}
        {duration}
        {loaded}
        onseek={seekComment}
        {regionRequest}
        onclose={() => (draftRegion = null)}
        label="COMMENTS ON {takeLetter(current)}"
      />
    {/if}

    <ol class="rows takes">
      {#each takes as t, i (t.id)}
        {@const qa = takeQaBadge(t)}
        <li class="take" class:current={i === current}>
          <div class="take-head">
            <span class="take-letter">{takeLetter(i)}</span>
            <span class="track-title">{t.title}</span>
            {#if t.decision !== 'pending'}<span class="tag" class:warn={t.decision === 'rejected'}>{takeDecisionText(t)}</span>{/if}
            {#if lm.matched && (lm.db[i] ?? 0) <= -0.05}<span class="tag">{formatDb(lm.db[i])} dB</span>{/if}
            <span class="tag qa" class:ok={qa.kind === 'passed'} class:warn={qa.kind === 'failed'} title={qa.details.join(' | ')}>{qa.label}</span>
          </div>
          {#if qa.kind === 'failed'}
            <ul class="muted tiny qa-details">
              {#each qa.details.slice(0, 4) as d}<li>{d}</li>{/each}
            </ul>
          {/if}
          {#if qcLine(t.qc)}
            {@const ql = qcLine(t.qc)!}
            <p class="qc-line" class:fail={ql.kind === 'failed'} title={ql.details.join(' | ')}>
              <span class="qc-main">{ql.text}</span>
              {#each ql.scores as sc (sc.name)}<span class="qc-score">{sc.name} {sc.value}</span>{/each}
              {#if qcIssueCount(t.qc) > 0}<span class="qc-score">{qcIssueCount(t.qc)} issue{qcIssueCount(t.qc) === 1 ? '' : 's'}</span>{/if}
            </p>
            {#if ql.kind === 'done' && t.qc?.reasons}<p class="muted tiny qc-reasons">{t.qc.reasons}</p>{/if}
            {#if lastAttemptLine(t.qc)}<p class="muted tiny qc-attempt">{lastAttemptLine(t.qc)}</p>{/if}
          {/if}
          <p class="muted tiny">{takeInfo(t)}</p>
          {#if judgeOffered(jobQc) || measureQaOffered(qa.kind)}
            <div class="take-actions qc-actions">
              {#if measureQaOffered(qa.kind)}
                <button type="button" class="small-btn" onclick={() => runMeasure(t)} disabled={qcBusy[t.id] !== undefined}
                  title="Measure this take's stage-1 QA (length, clipping, true peak, silence) from its file">{qcBusy[t.id] === 'measure' ? 'Measuring…' : 'Measure QA'}</button>
              {/if}
              {#if judgeOffered(jobQc)}
                <button type="button" class="small-btn" onclick={() => runJudge(t)} disabled={qcBusy[t.id] !== undefined || !t.library_track_id}
                  title="Ask the audio judge ({jobQc?.judge_alias}) to listen to this take; it can take a while, it waits for the GPU card">{qcBusy[t.id] === 'judge' ? 'Judging…' : t.qc?.judge?.status === 'done' ? 'Judge again' : 'Judge'}</button>
              {/if}
            </div>
          {/if}
          {#if qcError[t.id]}<p class="field-error" role="alert">{qcError[t.id]}</p>{/if}
          {#if !t.library_track_id}<p class="muted tiny">Not in the library: it cannot play here.</p>{/if}
          {#if takeDecidable(job, t)}
            <div class="take-actions">
              <button type="button" class="small-btn primary" onclick={() => openKeep(t)} aria-expanded={keepFor === t.id}>Keep {takeLetter(i)}</button>
              <button type="button" class="small-btn danger" onclick={() => openReason(t.id)} aria-expanded={reasonFor === t.id}>Discard {takeLetter(i)}…</button>
            </div>
          {/if}
          {#if keepFor === t.id}
            <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
            <div class="reason-box" role="group" aria-label="Keep take {takeLetter(i)}" onkeydown={keepKeydown}>
              {#if userLists.length}
                <label class="small" for="mc-keep-{t.id}">Also add to a playlist (optional)</label>
                <select id="mc-keep-{t.id}" class="list-select" bind:value={keepPlaylist} disabled={keepBusy}>
                  <option value="">No playlist</option>
                  {#each userLists as l (l.id)}<option value={l.id}>{l.name}</option>{/each}
                </select>
              {:else}
                <p class="muted tiny">Keep marks the take as yours; it stays in the library (Generated).</p>
              {/if}
              <div class="reason-row">
                <button type="button" class="small-btn primary" onclick={() => submitKeep(t)} disabled={keepBusy}>{keepBusy ? 'Keeping…' : `Keep ${takeLetter(i)}`}</button>
                <button type="button" class="small-btn" onclick={() => (keepFor = null)} disabled={keepBusy}>Cancel</button>
                <span class="muted tiny">Esc closes.</span>
              </div>
            </div>
          {/if}
          {#if reasonFor === t.id}
            <MusicReasonBox
              id="mc-reason-{t.id}"
              label="Why discard take {takeLetter(i)}?"
              {classes}
              onsubmit={(text, cls) => submitDiscard(t, text, cls)}
              oncancel={() => (reasonFor = null)}
            />
          {/if}
        </li>
      {/each}
    </ol>

    {#if job.state === 'review' && pendingCount > 0}
      <div class="take-actions">
        <button type="button" class="small-btn danger" onclick={() => openReason('all')} aria-expanded={reasonFor === 'all'}>None of these…</button>
        <span class="muted tiny">Discards every pending take with one reason.</span>
      </div>
      {#if reasonFor === 'all'}
        <MusicReasonBox
          id="mc-reason-all-{job.id}"
          label="Why none of these?"
          submitLabel="None of these"
          {classes}
          onsubmit={submitNone}
          oncancel={() => (reasonFor = null)}
        />
      {/if}
    {/if}
    {#if job.state === 'accepted' || job.state === 'rejected'}
      <p class="outcome" role="status">Outcome: {jobStateText(job)}{job.state === 'accepted' && pendingCount ? ` · ${pendingCount} take${pendingCount === 1 ? '' : 's'} still undecided` : ''}</p>
    {/if}
  {/if}
  {#if $musicPlayback.queue.ids.length && rv.active}
    <p class="muted tiny">The queue is paused while you review; it comes back where it was when the review closes.</p>
  {/if}
</section>

<style>
  /* The judge line under a take's QA badge (the other review classes live in MusicCreate.svelte). */
  .qc-line { display: flex; flex-wrap: wrap; gap: 4px 8px; align-items: baseline; margin: 2px 0 0; font-size: 12px; color: #c9d1d9; }
  .qc-line.fail { color: #f0c674; }
  .qc-main { font-weight: 600; }
  .qc-score { font-size: 11px; color: #9aa4af; white-space: nowrap; }
  .qc-reasons { margin: 2px 0 0; }
  .qc-attempt { margin: 2px 0 0; }
  .qc-unavailable { display: flex; flex-wrap: wrap; gap: 4px 8px; align-items: baseline; margin: 4px 0; }
  .qc-actions { margin-top: 4px; }
</style>
