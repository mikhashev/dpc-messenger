<!-- MusicSimilar.svelte — the "Generate similar" dialog of the player (SPEC flows 10 and 17; ADR-051 row B5). -->
<!--
  Opened from "Same style…" / "Cover…" in the track header and "Similar…" in the mini-player, on the
  current track. One dialog with a mode switch:

  - Same style (`generate_similar`, acestep.cpp text2music): Keep checkboxes with the values the person
    types (there is no auto-caption yet), the style description built from them until edited, lyrics
    none / new (the engine's LM writes them, an optional theme) / reuse (the track's stored lyrics, the
    core fills them), and description or the source's audio as a reference (a timbre reference, marked
    unverified);
  - Cover (`cover`): "kept from the source: melody and lyrics", a required target style, the engine
    (acestep.cpp cover, or yue2.cpp's score-based chain with its caveats), the cover strength on acestep.

  Variants and duration (a cover on acestep is as long as its source). Create is off, with the reason,
  while the capability report says the mode cannot run (a web track cannot send its audio, a yue2 cover
  without the transcriber). The job joins the jobs list; its takes are reviewed in Create like any other.
-->

<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import { musicJobs, proposeSong, refreshCapabilities } from '$lib/services/musicJobs';
  import {
    KEEP_KEYS,
    KEEP_LABELS,
    REFERENCE_NOTE,
    YUE2_COVER_CAVEATS,
    captionFromKeep,
    emptySimilarForm,
    similarFieldOf,
    songSupport,
    validateSimilarForm,
    type SimilarErrors,
    type SimilarForm,
    type SongMode,
    type SongSource,
  } from '$lib/utils/musicSimilar';

  let { source, mode = 'same_style', onclose, oncreate }: {
    source: SongSource;
    mode?: SongMode;
    onclose: () => void;
    oncreate?: () => void;
  } = $props();

  let form = $state<SimilarForm>(untrack(() => emptySimilarForm(source, mode)));
  let errors = $state<SimilarErrors>({});
  let busy = $state(false);
  let done = $state('');
  let caps = $derived($musicJobs.capabilities);
  let support = $derived(songSupport(caps, form));
  let shownCaption = $derived(form.captionEdited || form.mode === 'cover' ? form.caption : captionFromKeep(form));

  onMount(() => { void refreshCapabilities(); });

  // Another track or mode asked for while open: start over on it.
  let lastKey = untrack(() => `${source.id}:${mode}`);
  $effect(() => {
    const key = `${source.id}:${mode}`;
    if (key !== lastKey) {
      lastKey = key;
      form = emptySimilarForm(source, mode);
      errors = {};
      done = '';
    }
  });

  function setMode(m: SongMode) {
    form.mode = m;
    form.caption = '';
    form.captionEdited = false;
    errors = {};
    done = '';
  }

  function onCaption(e: Event) {
    form.caption = (e.currentTarget as HTMLInputElement).value;
    form.captionEdited = true;
    errors = { ...errors, caption: undefined };
  }

  function newKey(): string {
    try {
      return crypto.randomUUID();
    } catch {
      return `k${Date.now()}${Math.random().toString(16).slice(2)}`;
    }
  }

  async function create() {
    if (busy) return;
    done = '';
    const v = validateSimilarForm({ ...form, caption: shownCaption, captionEdited: true });
    if (!v.ok) {
      errors = v.errors;
      return;
    }
    errors = {};
    busy = true;
    try {
      const a = await proposeSong(v.type, v.trackId, v.params, newKey());
      if (!a.ok) {
        errors = { [similarFieldOf(a.field)]: a.message };
        return;
      }
      done = a.data.job.state === 'unsupported'
        ? `Not queued: ${a.data.job.state_reason ?? 'unsupported'}`
        : 'Queued. The new tracks appear in Generated; review them in Create.';
      if (a.data.job.state !== 'unsupported') oncreate?.();
    } finally {
      busy = false;
    }
  }
</script>

<section class="card similar" aria-labelledby="similar-label">
  <div class="section-head">
    <span id="similar-label" class="section-label">GENERATE SIMILAR</span>
    <span class="muted small">from “{source.title}”{source.kind === 'web' ? ' (web track)' : ''}</span>
    <button type="button" class="small-btn head-btn" onclick={onclose} aria-label="Close the generate-similar dialog">×</button>
  </div>

  <div class="switch" role="radiogroup" aria-label="Mode">
    <button type="button" role="radio" aria-checked={form.mode === 'same_style'} class:on={form.mode === 'same_style'}
      onclick={() => setMode('same_style')} title="A new song in the style of this one">Same style</button>
    <button type="button" role="radio" aria-checked={form.mode === 'cover'} class:on={form.mode === 'cover'}
      onclick={() => setMode('cover')} title="The same melody and lyrics, a new arrangement">Cover</button>
  </div>

  {#if form.mode === 'same_style'}
    <fieldset class="keep">
      <legend class="field-label">Keep (type the value to keep)</legend>
      {#each KEEP_KEYS as k (k)}
        <label class="keep-row">
          <input type="checkbox" bind:checked={form.keep[k]} />
          <span class="small keep-name">{KEEP_LABELS[k]}</span>
          {#if k === 'vocals'}
            <select class="list-select" bind:value={form.keepValues.vocals} disabled={!form.keep.vocals} aria-label="Vocals value">
              <option value="">unknown</option>
              <option value="vocals">vocals</option>
              <option value="instrumental">instrumental</option>
            </select>
          {:else}
            <input class="text-input" type="text" bind:value={form.keepValues[k]} disabled={!form.keep[k]}
              aria-label="{KEEP_LABELS[k]} value" placeholder={k === 'bpm' ? 'e.g. 96' : ''} />
          {/if}
        </label>
      {/each}
      <p class="field-error" role="alert">{errors.keep ?? ''}</p>
    </fieldset>
  {:else}
    <p class="small">Kept from the source: melody{form.coverLyrics === 'reuse' ? ' and lyrics (the stored ones)' : ''}.</p>
  {/if}

  <label class="field-label" for="similar-caption">{form.mode === 'cover' ? 'Target style' : 'Style description'}</label>
  <input id="similar-caption" class="text-input" type="text" value={shownCaption} oninput={onCaption}
    placeholder={form.mode === 'cover' ? 'e.g. bossa nova, nylon guitar, soft female vocal' : 'built from the kept values; edit freely'}
    aria-invalid={!!errors.caption} aria-describedby="similar-caption-error" />
  <p id="similar-caption-error" class="field-error" role="alert">{errors.caption ?? ''}</p>
  {#if form.mode === 'same_style' && !form.captionEdited}
    <p class="muted tiny">Built from the kept values (there is no auto-caption yet); once edited, Keep no longer rewrites it.</p>
  {/if}

  {#if form.mode === 'same_style'}
    <div class="form-row">
      <label class="field">
        <span class="field-label">Lyrics</span>
        <select class="list-select" bind:value={form.lyricsMode}>
          <option value="none">None (instrumental)</option>
          <option value="new">New (the engine writes them)</option>
          <option value="reuse">Reuse this track's lyrics</option>
        </select>
      </label>
      {#if form.lyricsMode === 'new'}
        <label class="field">
          <span class="field-label">Theme (optional)</span>
          <input class="text-input" type="text" bind:value={form.lyricsTheme} placeholder="e.g. night roads" />
        </label>
      {/if}
      <label class="field">
        <span class="field-label">Reference</span>
        <select class="list-select" bind:value={form.reference}>
          <option value="description">Description only</option>
          <option value="audio">Use audio as reference (unverified)</option>
        </select>
      </label>
    </div>
    {#if form.reference === 'audio'}<p class="muted tiny">{REFERENCE_NOTE}</p>{/if}
    <p class="muted tiny">Engine: acestep.cpp · text2music.</p>
  {:else}
    <div class="form-row">
      <label class="field">
        <span class="field-label">Engine</span>
        <select class="list-select" bind:value={form.engine}>
          <option value="acestep">acestep.cpp · cover</option>
          <option value="yue2">yue2.cpp · SheetSage2 score</option>
        </select>
      </label>
      <label class="field">
        <span class="field-label">Lyrics</span>
        <select class="list-select" bind:value={form.coverLyrics}>
          <option value="reuse">Keep this track's lyrics</option>
          <option value="none">Without lyrics</option>
        </select>
      </label>
      {#if form.engine === 'acestep'}
        <label class="field">
          <span class="field-label">Cover strength</span>
          <input class="text-input num" type="number" min="0" max="1" step="0.05" bind:value={form.coverStrength}
            title="The share of the engine's steps that hear the source: lower is freer" />
        </label>
      {/if}
    </div>
    {#if form.engine === 'yue2'}<p class="muted tiny">{YUE2_COVER_CAVEATS}</p>{/if}
    {#if form.engine === 'acestep'}<p class="muted tiny">The cover is as long as the source; it works without lyrics.</p>{/if}
  {/if}

  <div class="form-row">
    <label class="field">
      <span class="field-label">Variants</span>
      <input class="text-input num" type="number" min="1" max="4" step="1" bind:value={form.variants} aria-invalid={!!errors.variants} />
    </label>
    {#if !(form.mode === 'cover' && form.engine === 'acestep')}
      <label class="field">
        <span class="field-label">Duration (s){form.mode === 'cover' ? ', a cap' : ''}</span>
        <input class="text-input num" type="text" inputmode="decimal" bind:value={form.durationS} aria-invalid={!!errors.duration} />
      </label>
    {/if}
  </div>
  <p class="field-error" role="alert">{errors.variants ?? errors.duration ?? ''}</p>
  <p class="field-error" role="alert">{errors.source ?? errors.form ?? ''}</p>

  <div class="form-row">
    <button type="button" class="small-btn primary" onclick={create} disabled={busy || !support.ok}
      title={support.ok ? support.reason : `Not available: ${support.reason}`}>{busy ? 'Creating…' : 'Create'}</button>
    {#if !support.ok}<span class="muted tiny" role="status">{support.reason}</span>{/if}
  </div>
  {#if done}<p class="small" role="status">{done}</p>{/if}
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
  .head-btn { margin-left: auto; }
  .muted { color: var(--text-secondary, #a9b0bb); }
  .small { font-size: 13px; margin: 0; }
  .tiny { font-size: 12px; margin: 0; }
  .field-error { margin: 0; color: #f5a5a5; font-size: 12px; }
  .field-error:empty { display: none; }
  .field-label { font-size: 12px; color: var(--text-secondary, #a9b0bb); }
  .switch { display: inline-flex; gap: 0; align-self: flex-start; border: 1px solid #3a3f46; border-radius: 6px; overflow: hidden; }
  .switch button { padding: 4px 14px; background: transparent; color: #c9d1d9; border: 0; cursor: pointer; font-size: 13px; }
  .switch button.on { background: #1f3a5f; color: #ffffff; }
  .keep { display: flex; flex-direction: column; gap: 4px; border: 0; padding: 0; margin: 0; }
  .keep-row { display: grid; grid-template-columns: auto 140px minmax(0, 1fr); align-items: center; gap: 8px; }
  .keep-name { white-space: nowrap; }
  .form-row { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; }
  .field { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
  .text-input, .list-select {
    padding: 4px 8px;
    border-radius: 6px;
    border: 1px solid #3a3f46;
    background: var(--bg-primary, #14161a);
    color: var(--text-primary, #e8eaed);
    font-size: 13px;
    min-width: 0;
  }
  .text-input.num { width: 90px; }
  .text-input[aria-invalid='true'] { border-color: #8b3a3a; }
  .text-input:disabled, .list-select:disabled { opacity: 0.55; }
  .small-btn {
    padding: 3px 10px;
    border-radius: 6px;
    border: 1px solid #3a3f46;
    background: transparent;
    color: #c9d1d9;
    font-size: 13px;
    cursor: pointer;
  }
  .small-btn:hover:not(:disabled) { background: #2c3036; color: #ffffff; }
  .small-btn:disabled { opacity: 0.5; cursor: default; }
  .small-btn.primary { border-color: #58a6ff; color: #58a6ff; }
  button:focus-visible, input:focus-visible, select:focus-visible { outline: 2px solid #58a6ff; outline-offset: 1px; }
  @media (max-width: 480px) {
    .keep-row { grid-template-columns: auto minmax(0, 1fr); }
    .keep-row .text-input, .keep-row .list-select { grid-column: 1 / -1; }
  }
</style>
