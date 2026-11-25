<!-- MusicLyrics.svelte — the Lyrics area of the Music Player's Listen view (ADR-051 D6). -->
<!--
  Compact: the previous, current and next line of synced lyrics, the current one
  highlighted, and a source badge (lrc / tags / lrclib / transcribed / manual, plus
  "unsynced" and "edited"). "Show all" opens the full lyrics: every line, click a synced
  line to seek, the steps the chain tried, "Find on LRCLIB" with its egress note,
  "Transcribe", and "Edit" (an LRC textarea with "Stamp current time", Alt+L inside it,
  and Save).

  The lyrics come from the core (dpc_client_core/music/lyrics.py) through
  getMusicLyrics / lookupLyricsOnLrclib / saveMusicLyrics. Opening a track runs only
  getMusicLyrics, which reads the sidecar, the tags and the local LRCLIB cache and sends
  nothing; lrclib.net is asked only when the person clicks "Find on LRCLIB", and then by
  the core, never by the webview. Nothing replaces stored lyrics without the person's
  choice: an LRCLIB result over existing lyrics is offered with "Use LRCLIB lyrics
  (replaces current)", and over the person's own (manual or edited) lyrics that button
  asks first (confirmAsync) before it sends overwrite_manual.

  "Transcribe" is the chain's explicit step 4: the core runs DPC's speech-to-text on the
  track's file (transcribeMusicLyrics) and stores the lines with source "transcribe",
  shown as "transcribed", with the provider and model the record keeps
  (`transcription: {provider, model}`) beside the badge. A collapsed note ("Filtered N lines") says
  what the transcript filter dropped and how long segments were split, with the dropped lines: from the run's own answer
  while it is on screen, else read back on open from the kept raw transcript
  (getMusicLyricsTranscriptRaw, musicLyricsFilter.ts). Opening a track never runs it. Over the person's own lyrics it
  asks first (confirmAsync) before it sends overwrite_manual. A run can outlast the
  command's timeout; the panel then follows it through getMusicLyrics (runTranscribe).
-->

<script lang="ts">
  import { tick, untrack } from 'svelte';
  import { getMusicLyrics, getMusicLyricsTranscriptRaw, saveMusicLyrics } from '$lib/coreService';
  import { lookupLyricsOnLrclib, seekTo, transcribeMusicLyrics } from '$lib/services/music';
  import { confirmAsync } from '$lib/utils/dialog';
  import { filterNote, filterReportOf, type RawTranscriptAnswer } from '$lib/utils/musicLyricsFilter';
  import {
    LRCLIB_EGRESS_TEXT,
    LYRICS_OVERWRITE_CONFIRM_TEXT,
    TRANSCRIBE_NOTE_TEXT,
    TRANSCRIBE_OVERWRITE_CONFIRM_TEXT,
    countUnstampedLines,
    formatLrc,
    formatTime,
    guessArtistTitle,
    lrclibOutcomeText,
    lyricsBadge,
    lyricsIsOwnEdit,
    lyricsSynced,
    lyricsWindow,
    runTranscribe,
    stampLrcLine,
    transcriptionText,
    type LibraryEntry,
    type LrclibOutcome,
    type Lyrics,
    type LyricsAnswer,
    type LyricsChainStep,
    type TranscribeRun,
    type TranscriptionInfo,
  } from '$lib/utils/musicPlayer';

  let {
    entry = null,
    currentTime = 0,
    duration = 0,
    loaded = false,
  }: { entry?: LibraryEntry | null; currentTime?: number; duration?: number; loaded?: boolean } = $props();

  let trackId = $derived(entry?.id ?? null);

  let status = $state<'idle' | 'loading' | 'ready' | 'error'>('idle');
  let message = $state('');
  let note = $state('');
  let lyrics = $state<Lyrics | null>(null);
  let chain = $state<LyricsChainStep[]>([]);
  let lrclib = $state<LrclibOutcome | null>(null);
  let lrclibEnabled = $state(true);
  let candidate = $state<Lyrics | null>(null);
  let expanded = $state(false);
  let editing = $state(false);
  let draft = $state('');
  let draftEl = $state<HTMLTextAreaElement | null>(null);
  let editMessage = $state('');
  let busy = $state<'' | 'lookup' | 'save' | 'refresh' | 'transcribe'>('');
  let transcribeRun = $state<TranscribeRun | null>(null);
  let transcribed = $state<TranscriptionInfo | null>(null);
  // The kept raw transcript's report (get_music_lyrics_transcript_raw): keeps the dropped lines visible after the run.
  let keptRaw = $state<RawTranscriptAnswer | null>(null);
  let qArtist = $state('');
  let qTitle = $state('');
  let token = 0;

  function take(a: LyricsAnswer) {
    lyrics = a.lyrics ?? null;
    if (a.chain && a.chain.length) chain = a.chain;
    if (a.lrclib !== undefined) lrclib = a.lrclib ?? null;
    if (typeof a.lrclib_enabled === 'boolean') lrclibEnabled = a.lrclib_enabled;
    if (a.transcribe !== undefined) transcribeRun = a.transcribe ?? null;
    candidate = a.candidate && a.candidate.source === 'lrclib' ? a.candidate : null;
    note = a.kept && a.candidate ? `Your edited lyrics are kept; the chain also found lyrics (${a.candidate.source}). Edit to change them.` : '';
    if (a.lrclib?.query) {
      qArtist = a.lrclib.query.artist;
      qTitle = a.lrclib.query.title;
    }
  }

  async function load(id: string, refresh = false) {
    const mine = ++token;
    status = 'loading';
    message = '';
    if (refresh) busy = 'refresh';
    const a = await getMusicLyrics(id, refresh);
    if (refresh) busy = '';
    if (mine !== token || trackId !== id) return;
    if (!a.ok) {
      status = 'error';
      message = a.unavailable ? 'Lyrics are not available on this backend yet.' : `Lyrics: ${a.message}`;
      return;
    }
    take(a.data);
    status = 'ready';
    if (a.data.lyrics?.source === 'transcribe') loadKeptRaw(id);
  }

  // Read only; a failure just leaves the stored counts in the note.
  async function loadKeptRaw(id: string) {
    const mine = token;
    const r = await getMusicLyricsTranscriptRaw(id);
    if (mine !== token || trackId !== id) return;
    keptRaw = r.ok ? r.data : null;
  }

  // Another track: start over, and read its lyrics (stored, or the chain: sidecar, tags,
  // the local LRCLIB cache). This runs with no click, so it never asks lrclib.net.
  $effect(() => {
    const id = trackId;
    untrack(() => {
      token++;
      lyrics = null;
      chain = [];
      lrclib = null;
      candidate = null;
      transcribeRun = null;
      transcribed = null;
      keptRaw = null;
      editing = false;
      editMessage = '';
      message = '';
      note = '';
      status = 'idle';
      const guess = guessArtistTitle(entry);
      qArtist = guess.artist;
      qTitle = guess.title;
      if (id) load(id);
    });
  });

  let synced = $derived(lyricsSynced(lyrics));
  let win = $derived(lyrics && synced ? lyricsWindow(lyrics.lines, currentTime, duration) : null);
  let curIndex = $derived(win?.current ?? -1);
  let unstamped = $derived(editing ? countUnstampedLines(draft) : 0);
  let showLookup = $derived(expanded || (status === 'ready' && !lyrics));

  // The full list follows the current line, but not for 4 s after a hand scroll.
  let listEl = $state<HTMLOListElement | null>(null);
  let handScrollAt = 0;
  let autoScrolling = false;
  $effect(() => {
    const i = curIndex;
    if (!expanded || editing || i < 0 || !listEl) return;
    untrack(() => {
      if (Date.now() - handScrollAt < 4000) return;
      const li = listEl?.querySelector<HTMLElement>(`[data-lyric="${i}"]`);
      if (!li || !listEl) return;
      autoScrolling = true;
      listEl.scrollTop = Math.max(0, li.offsetTop - listEl.clientHeight / 2 + li.offsetHeight / 2);
      requestAnimationFrame(() => (autoScrolling = false));
    });
  });

  function onListScroll() {
    if (!autoScrolling) handScrollAt = Date.now();
  }

  function seekLine(t: number | null) {
    if (t == null || !loaded) return;
    seekTo(t);
  }

  async function findOnLrclib(replace = false) {
    const id = trackId;
    if (!id || busy) return;
    // The person's own lyrics are replaced only after they say so; the core refuses
    // replace over them without overwrite_manual.
    let overwriteManual = false;
    if (replace && lyricsIsOwnEdit(lyrics)) {
      busy = 'lookup';
      const yes = await confirmAsync(LYRICS_OVERWRITE_CONFIRM_TEXT, { title: 'Replace lyrics', kind: 'warning' });
      busy = '';
      if (!yes || trackId !== id) return;
      overwriteManual = true;
    }
    busy = 'lookup';
    message = '';
    const a = await lookupLyricsOnLrclib(id, { artist: qArtist.trim(), title: qTitle.trim() }, { replace, overwriteManual });
    busy = '';
    if (trackId !== id) return;
    if (!a.ok) {
      message = a.unavailable
        ? 'Lyrics are not available on this backend yet.'
        : a.code === 'overwrite_manual_required'
          ? 'Your own lyrics were kept: replacing them needs your confirmation.'
          : `Find on LRCLIB: ${a.message}`;
      return;
    }
    lyrics = a.data.lyrics ?? null;
    lrclib = a.data.lrclib ?? null;
    candidate = a.data.candidate ?? null;
    status = 'ready';
  }

  async function startEdit() {
    draft = formatLrc(lyrics);
    editMessage = '';
    editing = true;
    expanded = true;
    await tick();
    draftEl?.focus();
  }

  function cancelEdit() {
    editing = false;
    editMessage = '';
  }

  async function stamp() {
    if (!draftEl || !loaded) return;
    const r = stampLrcLine(draft, draftEl.selectionStart ?? 0, currentTime);
    draft = r.text;
    await tick();
    draftEl?.focus();
    draftEl?.setSelectionRange(r.cursor, r.cursor);
  }

  function draftKeydown(e: KeyboardEvent) {
    if (e.altKey && (e.key === 'l' || e.key === 'L')) {
      e.preventDefault();
      stamp();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      cancelEdit();
    }
  }

  async function save() {
    const id = trackId;
    if (!id || busy) return;
    const n = countUnstampedLines(draft);
    if (n) {
      editMessage = `${n} line${n === 1 ? ' has' : 's have'} no time stamp: stamp ${n === 1 ? 'it' : 'them'} or delete ${n === 1 ? 'it' : 'them'} before saving.`;
      return;
    }
    busy = 'save';
    const a = await saveMusicLyrics(id, draft);
    busy = '';
    if (trackId !== id) return;
    if (!a.ok) {
      editMessage = a.message;
      return;
    }
    lyrics = a.data.lyrics ?? null;
    candidate = null;
    editing = false;
    editMessage = '';
  }

  async function transcribe() {
    const id = trackId;
    if (!id || busy) return;
    // The person's own lyrics are replaced only after they say so; the core refuses
    // over them without overwrite_manual.
    let overwriteManual = false;
    if (lyricsIsOwnEdit(lyrics)) {
      busy = 'transcribe';
      const yes = await confirmAsync(TRANSCRIBE_OVERWRITE_CONFIRM_TEXT, { title: 'Replace lyrics', kind: 'warning' });
      busy = '';
      if (!yes || trackId !== id) return;
      overwriteManual = true;
    }
    busy = 'transcribe';
    message = '';
    note = '';
    transcribed = null;
    const r = await runTranscribe(
      () => transcribeMusicLyrics(id, { overwriteManual }),
      () => getMusicLyrics(id),
      { cancelled: () => trackId !== id },
    );
    busy = '';
    if (!r || trackId !== id) return;
    if (!r.ok) {
      message = r.unavailable
        ? 'Transcribe is not available on this backend yet.'
        : r.code === 'overwrite_manual_required'
          ? 'Your own lyrics were kept: replacing them needs your confirmation.'
          : `Transcribe: ${r.message}`;
      return;
    }
    lyrics = r.lyrics;
    candidate = null;
    transcribed = r.info;
    transcribeRun = null;
    if (r.kept) note = `Your own lyrics are kept: ${r.kept}. Transcribe again to replace them.`;
    status = 'ready';
  }

  function stepText(s: LyricsChainStep): string {
    const names: Record<string, string> = { lrc: '.lrc sidecar', tags: 'File tags', lrclib: 'LRCLIB', transcribe: 'Transcribe', manual: 'Manual edit' };
    const state = s.state.replace(/_/g, ' ');
    return `${names[s.step] ?? s.step}: ${state}${s.detail ? ` (${s.detail})` : ''}`;
  }

  // What transcribed the stored lyrics (`mp.lyrics/1` `transcription`, only on source
  // "transcribe"; records written before it have none, and then nothing is shown).
  let transcribedBy = $derived.by(() => {
    const t = lyrics?.source === 'transcribe'
      ? (lyrics as Lyrics & { transcription?: { provider: string | null; model: string | null } }).transcription
      : undefined;
    return t ? [t.provider, t.model].filter(Boolean).join(' · ') : '';
  });

  // What the transcript filter did: the run's own report (it lists the dropped lines), else the counts
  // stored on the lyrics. Null for lyrics that were not transcribed or ran no filter.
  let filterView = $derived(filterNote(filterReportOf(transcribed, lyrics, keptRaw)));

  let emptyText = $derived(
    lrclib && lrclib.result !== 'synced' && lrclib.result !== 'plain'
      ? lrclibOutcomeText(lrclib)
      : !lrclibEnabled
        ? 'No lyrics in a sidecar or the file tags. Online lookup is off.'
        : 'No lyrics found. Find them on LRCLIB, transcribe the track, or write them in Edit.',
  );
</script>

<section class="card lyrics" aria-labelledby="lyrics-label">
  <div class="section-head">
    <span id="lyrics-label" class="section-label">LYRICS</span>
    {#if entry}
      <span class="tag" class:none={!lyrics} title="Where these lyrics came from">source: {lyricsBadge(lyrics)}</span>
      {#if transcribedBy}<span class="muted tiny transcribed-by" title="The provider and model that transcribed these lyrics">{transcribedBy}</span>{/if}
    {/if}
    <span class="head-actions">
      <button type="button" class="small-btn" onclick={() => (expanded = !expanded)} disabled={!entry} aria-expanded={expanded}>
        {expanded ? 'Hide' : 'Show all'}
      </button>
      <button type="button" class="small-btn" onclick={startEdit} disabled={!entry || editing || status === 'loading'}>Edit</button>
    </span>
  </div>

  {#if !entry}
    <p class="muted small">Nothing playing.</p>
  {:else if status === 'loading' && !lyrics}
    <p class="muted small">Looking for lyrics…</p>
  {:else if status === 'error'}
    <p class="error small" role="alert">{message}</p>
  {:else if !editing}
    {#if lyrics && synced && win}
      <div class="compact" aria-live="off">
        <p class="line prev">{win.prev >= 0 ? lyrics.lines[win.prev].text : ''}</p>
        <p class="line cur" class:between={win.current < 0}>{win.current >= 0 ? lyrics.lines[win.current].text : '…'}</p>
        <p class="line next">{win.next >= 0 ? lyrics.lines[win.next].text : ''}</p>
      </div>
    {:else if lyrics}
      <p class="muted small">Unsynced lyrics ({lyrics.lines.length} lines, no times): no highlight or seek. Edit and stamp each line while it plays to sync them.</p>
      {#if !expanded}<p class="line">{lyrics.lines[0]?.text ?? ''}</p>{/if}
    {:else if status === 'ready'}
      <p class="muted small" role="status">{emptyText}</p>
    {/if}
  {/if}

  {#if entry && message && status !== 'error'}<p class="error small" role="alert">{message}</p>{/if}
  {#if entry && note}<p class="muted tiny" role="status">{note}</p>{/if}
  {#if entry && transcribed}<p class="muted tiny" role="status">{transcriptionText(transcribed)}</p>{/if}
  {#if entry && filterView}
    <details class="filter-note">
      <summary class="muted tiny">{filterView.summary}</summary>
      <ul class="tiny muted">
        {#each filterView.facts as fact (fact)}<li>{fact}</li>{/each}
      </ul>
      {#if filterView.lines.length}
        <ol class="tiny dropped">
          {#each filterView.lines as dropped, i (i)}
            <li><span class="t">{dropped.time}</span> <span class="x">{dropped.text}</span> <span class="muted">({dropped.reason})</span></li>
          {/each}
        </ol>
        {#if filterView.more > 0}<p class="muted tiny">and {filterView.more} more.</p>{/if}
      {/if}
    </details>
  {/if}
  {#if entry && busy !== 'transcribe' && transcribeRun?.state === 'running'}
    <p class="muted tiny" role="status">This track is being transcribed; Look again when it ends.</p>
  {/if}

  {#if entry && candidate}
    <div class="candidate" role="status">
      <span class="small">{lrclibOutcomeText(lrclib)} Current lyrics are kept until you choose.</span>
      <button type="button" class="small-btn primary" onclick={() => findOnLrclib(true)} disabled={!!busy}>Use LRCLIB lyrics (replaces current)</button>
      <button type="button" class="small-btn" onclick={() => (candidate = null)}>Discard</button>
    </div>
  {/if}

  {#if entry && expanded && !editing && lyrics}
    <ol class="full" bind:this={listEl} onscroll={onListScroll}>
      {#each lyrics.lines as line, i (i)}
        <li data-lyric={i} class:cur={i === curIndex}>
          {#if line.t_start != null}
            <button
              type="button"
              class="lyric"
              onclick={() => seekLine(line.t_start)}
              disabled={!loaded}
              title={loaded ? `Seek to ${formatTime(line.t_start)}` : 'Play the track to seek'}
              aria-current={i === curIndex ? 'true' : undefined}
            ><span class="t">{formatTime(line.t_start)}</span><span class="x">{line.text}</span></button>
          {:else}
            <span class="lyric untimed" title="No time yet: stamp it in Edit"><span class="t">--:--</span><span class="x">{line.text}</span></span>
          {/if}
        </li>
      {/each}
    </ol>
  {/if}

  {#if entry && editing}
    <div class="edit">
      <textarea
        bind:this={draftEl}
        bind:value={draft}
        onkeydown={draftKeydown}
        spellcheck="false"
        rows="10"
        aria-label="Lyrics in LRC"
        placeholder={'[00:12.50] First line\n[00:15.00] Second line <00:17.20>\nA line still to stamp'}
      ></textarea>
      <p class="muted tiny">Put the cursor on a line and stamp it while the track plays (Alt+L in the box); the cursor moves to the next line. Lines are [mm:ss.xx] text, with an optional end &lt;mm:ss.xx&gt;.</p>
      <div class="row-btns">
        <button type="button" class="small-btn" onclick={stamp} disabled={!loaded} title={loaded ? 'Stamp current time (Alt+L)' : 'Play the track to stamp its time'}>
          Stamp current time {loaded ? formatTime(currentTime) : ''}
        </button>
        <span class="muted tiny">{unstamped ? `${unstamped} line${unstamped === 1 ? '' : 's'} without a stamp` : ''}</span>
        <button type="button" class="small-btn primary" onclick={save} disabled={!!busy}>{busy === 'save' ? 'Saving…' : 'Save'}</button>
        <button type="button" class="small-btn" onclick={cancelEdit} disabled={busy === 'save'}>Cancel</button>
      </div>
      {#if editMessage}<p class="error small" role="alert">{editMessage}</p>{/if}
    </div>
  {/if}

  {#if entry && showLookup && !editing}
    <div class="lookup">
      <input class="text-input" type="text" bind:value={qArtist} placeholder="Artist" aria-label="Artist for LRCLIB" spellcheck="false" />
      <input class="text-input" type="text" bind:value={qTitle} placeholder="Title" aria-label="Title for LRCLIB" spellcheck="false" />
      <button
        type="button"
        class="small-btn"
        onclick={() => findOnLrclib(false)}
        disabled={!!busy || !lrclibEnabled}
        title={lrclibEnabled ? LRCLIB_EGRESS_TEXT : 'Online lookup is off ([music] lrclib_enabled)'}
      >{busy === 'lookup' ? 'Searching…' : 'Find on LRCLIB'}</button>
      <button
        type="button"
        class="small-btn"
        onclick={transcribe}
        disabled={!!busy}
        aria-busy={busy === 'transcribe'}
        title={TRANSCRIBE_NOTE_TEXT}
      >{busy === 'transcribe' ? 'Transcribing…' : 'Transcribe'}</button>
    </div>
    {#if busy === 'transcribe'}<p class="muted tiny" role="status">Transcribing this track; a song can take a minute or more (longer the first time, while the model loads).</p>{/if}
    <p class="muted tiny egress">{lrclibEnabled ? `${LRCLIB_EGRESS_TEXT}, an external service, only when you click Find on LRCLIB (no audio, no file path).` : 'Online lookup is off ([music] lrclib_enabled = false): nothing is sent to lrclib.net.'}</p>
    <p class="muted tiny">Transcribe: {TRANSCRIBE_NOTE_TEXT}</p>
    {#if lrclib && expanded}<p class="muted tiny" role="status">{lrclibOutcomeText(lrclib)}</p>{/if}
  {/if}

  {#if entry && expanded && !editing && chain.length}
    <details class="chain">
      <summary class="muted tiny">What the chain tried</summary>
      <ol>
        {#each chain as s (s.step)}<li class="tiny" class:hit={s.state === 'found'}>{stepText(s)}</li>{/each}
      </ol>
    </details>
  {/if}
  {#if entry && expanded && !editing && status !== 'loading'}
    <!-- Runs the chain again (a changed sidecar, a retag), sending nothing; a manual edit is never replaced. -->
    <button type="button" class="small-btn again" onclick={() => trackId && load(trackId, true)} disabled={!!busy}
      title="Read the .lrc sidecar, the file tags and the local LRCLIB cache again (nothing is sent; Find on LRCLIB asks lrclib.net); your own edits are kept">{busy === 'refresh' ? 'Looking…' : 'Look again'}</button>
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
  .head-actions { margin-left: auto; display: flex; gap: 4px; }
  .tag { font-size: 11px; padding: 1px 6px; border-radius: 5px; background: #1d2a3d; color: #b6d4ff; white-space: nowrap; }
  .tag.none { background: #31353d; color: #c9d1d9; }
  .filter-note summary { cursor: pointer; }
  .filter-note ul, .filter-note ol { margin: 4px 0 0; padding-left: 18px; }
  .filter-note .dropped li { overflow-wrap: anywhere; }
  .transcribed-by { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  .compact { display: flex; flex-direction: column; gap: 2px; text-align: center; }
  .line { margin: 0; min-height: 1.3em; font-size: 13px; color: var(--text-secondary, #a9b0bb); overflow-wrap: anywhere; }
  .line.cur { font-size: 16px; font-weight: 600; color: var(--text-primary, #e8eaed); }
  .line.cur.between { color: var(--text-secondary, #a9b0bb); font-weight: 400; }

  .full {
    list-style: none;
    margin: 0;
    padding: 0;
    max-height: 260px;
    overflow: auto;
    display: flex;
    flex-direction: column;
    gap: 1px;
    position: relative;
  }
  .lyric {
    display: flex;
    gap: 10px;
    width: 100%;
    padding: 3px 6px;
    border: 0;
    border-radius: 6px;
    background: transparent;
    color: inherit;
    font: inherit;
    font-size: 13px;
    text-align: left;
    cursor: pointer;
  }
  button.lyric:hover:not(:disabled) { background: #22262c; }
  button.lyric:disabled { cursor: default; }
  .lyric.untimed { cursor: default; color: var(--text-secondary, #a9b0bb); }
  li.cur .lyric { background: #232a35; color: #ffffff; font-weight: 600; }
  .t { flex: 0 0 auto; font-size: 12px; color: var(--text-secondary, #a9b0bb); font-variant-numeric: tabular-nums; }
  .x { overflow-wrap: anywhere; }

  .candidate, .lookup, .row-btns { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .text-input {
    flex: 1 1 120px;
    min-width: 0;
    padding: 4px 8px;
    border-radius: 6px;
    border: 1px solid var(--border-color, #2c3036);
    background: var(--bg-primary, #14161a);
    color: var(--text-primary, #e8eaed);
    font: inherit;
    font-size: 13px;
  }
  .edit { display: flex; flex-direction: column; gap: 6px; }
  textarea {
    width: 100%;
    box-sizing: border-box;
    resize: vertical;
    padding: 8px;
    border-radius: 6px;
    border: 1px solid var(--border-color, #2c3036);
    background: var(--bg-primary, #14161a);
    color: var(--text-primary, #e8eaed);
    font-family: ui-monospace, 'Cascadia Mono', Consolas, monospace;
    font-size: 12px;
  }
  .row-btns > .muted { margin-right: auto; }

  .small-btn {
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
  .small-btn:hover:not(:disabled) { background: #2c3036; color: #ffffff; }
  .small-btn:disabled { opacity: 0.5; cursor: default; }
  .small-btn.primary { border-color: #58a6ff; color: #58a6ff; }

  .chain ol { margin: 4px 0; padding-left: 18px; }
  .chain li.hit { color: #9fd49f; }
  .chain summary { cursor: pointer; }
  .small-btn.again { align-self: flex-start; }

  .muted { color: var(--text-secondary, #a9b0bb); }
  .error { color: #f5a5a5; margin: 0; }
  .small { font-size: 13px; }
  .tiny { font-size: 12px; }
  p.muted { margin: 0; }
</style>
