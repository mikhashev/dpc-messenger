<!-- MusicComments.svelte — timecoded listening notes on a library track (mp.comment/1, SPEC §4). -->
<!--
  The comment form and the comment list for one track. A comment is a point or a region
  with a text; the core stores it per library track (music/comments.py), so it follows the
  track through a rescan or a relink. The slider markers live with the slider (the Now
  playing card and the A/B review) and read the same store (services/musicComments.ts).

  "Comment" (or C in the player, outside text fields) opens the form with Start at the
  current position and End empty (a point). "Mark end" sets End to the position now;
  Save is Ctrl+Enter, Cancel is Esc. Only the Note action works; the others are listed as
  planned. Sending a comment to an agent is planned too (agents have no music tools yet),
  so there is no button for it. Clicking a time in the list seeks there (`onseek`: the
  player's safe seek by default, the review's own for a take).
-->

<script lang="ts">
  import { tick, untrack } from 'svelte';
  import { confirmAsync } from '$lib/utils/dialog';
  import { formatTime } from '$lib/utils/musicPlayer';
  import {
    COMMENT_ACTIONS,
    COMMENT_MAX_CHARS,
    commentTimeLabel,
    secondsText,
    validateCommentForm,
    type MusicComment,
  } from '$lib/utils/musicComments';
  import { seekTo } from '$lib/services/music';
  import { ensureComments, musicComments, musicCommentsError, removeComment, saveComment } from '$lib/services/musicComments';

  let {
    trackId = null,
    currentTime = 0,
    duration = 0,
    loaded = false,
    openSignal = 0,
    onseek = (t: number) => seekTo(t, 'ui'),
    label = 'COMMENTS',
  }: {
    trackId?: string | null;
    currentTime?: number;
    duration?: number;
    loaded?: boolean;
    /** Bumped by the parent (the C key, the card's Comment button) to open the form. */
    openSignal?: number;
    onseek?: (seconds: number) => void;
    label?: string;
  } = $props();

  let comments = $derived<MusicComment[]>(trackId ? ($musicComments[trackId] ?? []) : []);
  let loadError = $derived(trackId ? ($musicCommentsError[trackId] ?? '') : '');

  $effect(() => {
    ensureComments(trackId);
  });

  type Form = { id: string | null; start: string; end: string; text: string; action: string };
  let form = $state<Form | null>(null);
  let formError = $state('');
  let busy = $state(false);
  let textEl = $state<HTMLTextAreaElement | null>(null);

  // The form belongs to a track: another track, another list.
  let formFor = '';
  $effect(() => {
    const id = trackId ?? '';
    if (id !== formFor) {
      formFor = id;
      form = null;
      formError = '';
    }
  });

  async function openNew() {
    if (!trackId) return;
    form = { id: null, start: secondsText(loaded ? currentTime : 0) || '0', end: '', text: '', action: 'note' };
    formError = '';
    await tick();
    textEl?.focus();
  }

  async function openEdit(c: MusicComment) {
    form = { id: c.id, start: secondsText(c.t_start), end: secondsText(c.t_end), text: c.text, action: c.action };
    formError = '';
    await tick();
    textEl?.focus();
  }

  let lastSignal = untrack(() => openSignal);
  $effect(() => {
    const n = openSignal;
    if (n !== lastSignal) {
      lastSignal = n;
      void openNew();
    }
  });

  function markEnd() {
    if (form && loaded) form.end = secondsText(currentTime);
  }

  function cancel() {
    form = null;
    formError = '';
  }

  async function save() {
    if (!form || !trackId || busy) return;
    const r = validateCommentForm(form, duration > 0 ? duration : null);
    if (r.error || !r.value) {
      formError = r.error ?? '';
      return;
    }
    busy = true;
    formError = '';
    try {
      const err = await saveComment(trackId, r.value, form.id ?? undefined);
      if (err) formError = err;
      else form = null;
    } finally {
      busy = false;
    }
  }

  async function remove(c: MusicComment) {
    if (!trackId) return;
    const ok = await confirmAsync(`Delete the comment at ${commentTimeLabel(c, formatTime)}?`, {
      title: 'Delete comment',
      kind: 'warning',
    });
    if (!ok) return;
    const err = await removeComment(trackId, c.id);
    if (err) formError = err;
  }

  function onFormKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      cancel();
    } else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      e.stopPropagation();
      void save();
    }
  }
</script>

<section class="card" aria-labelledby="mc-comments-label">
  <div class="section-head">
    <span id="mc-comments-label" class="section-label">{label} {#if comments.length}<span class="count">{comments.length}</span>{/if}</span>
    <button type="button" class="small-btn head-btn" onclick={openNew} disabled={!trackId} title="Add a comment at the current position (C)">Comment</button>
  </div>

  {#if loadError}<p class="muted small" role="status">{loadError}</p>{/if}

  {#if form}
    <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
    <div class="form" role="group" aria-label={form.id ? 'Edit comment' : 'New comment'} onkeydown={onFormKeydown}>
      <div class="form-row">
        <label class="field">
          <span class="muted tiny">Start (s)</span>
          <input class="text-input num" type="text" inputmode="decimal" bind:value={form.start} disabled={busy} />
        </label>
        <label class="field">
          <span class="muted tiny">End (s, empty = point)</span>
          <input class="text-input num" type="text" inputmode="decimal" bind:value={form.end} disabled={busy} />
        </label>
        <button type="button" class="small-btn" onclick={markEnd} disabled={!loaded || busy} title="Set End to the current position">Mark end</button>
        <label class="field">
          <span class="muted tiny">Action</span>
          <select class="text-input" bind:value={form.action} disabled={busy} aria-label="Action">
            {#each COMMENT_ACTIONS as a (a.value)}
              <option value={a.value} disabled={!a.supported}>{a.label}{a.supported ? '' : ' (planned)'}</option>
            {/each}
          </select>
        </label>
      </div>
      <textarea
        bind:this={textEl}
        bind:value={form.text}
        rows="3"
        maxlength={COMMENT_MAX_CHARS}
        placeholder="What do you hear here?"
        aria-label="Comment text"
        disabled={busy}
      ></textarea>
      {#if formError}<p class="field-error" role="alert">{formError}</p>{/if}
      <div class="form-row">
        <button type="button" class="small-btn primary" onclick={save} disabled={busy}>{busy ? 'Saving…' : 'Save'}</button>
        <button type="button" class="small-btn" onclick={cancel} disabled={busy}>Cancel</button>
        <span class="muted tiny">Ctrl+Enter saves · Esc cancels · sending to an agent is planned</span>
      </div>
    </div>
  {/if}

  {#if !comments.length && !form}
    <p class="muted small">{trackId ? 'No comments yet. Press C while listening to note what you hear.' : 'Nothing to comment on yet.'}</p>
  {/if}

  {#if comments.length}
    <ol class="list">
      {#each comments as c (c.id)}
        <li class="item">
          <button type="button" class="when" onclick={() => onseek(c.t_start)} title="Seek to {formatTime(c.t_start)}">{commentTimeLabel(c, formatTime)}</button>
          <span class="what">{c.text}</span>
          <span class="acts">
            <button type="button" class="small-btn" onclick={() => openEdit(c)}>Edit</button>
            <button type="button" class="small-btn danger" onclick={() => remove(c)}>Delete</button>
          </span>
        </li>
      {/each}
    </ol>
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
  .form { display: flex; flex-direction: column; gap: 6px; padding: 8px; border-radius: 8px; background: var(--bg-primary, #14161a); border: 1px solid var(--border-color, #2c3036); }
  .form-row { display: flex; align-items: flex-end; gap: 8px; flex-wrap: wrap; }
  .field { display: flex; flex-direction: column; gap: 2px; }
  .text-input {
    padding: 4px 8px;
    border-radius: 6px;
    border: 1px solid var(--border-color, #2c3036);
    background: var(--bg-secondary, #1d2025);
    color: var(--text-primary, #e8eaed);
    font: inherit;
    font-size: 13px;
  }
  .text-input.num { width: 90px; }
  textarea {
    width: 100%;
    box-sizing: border-box;
    resize: vertical;
    padding: 8px;
    border-radius: 6px;
    border: 1px solid var(--border-color, #2c3036);
    background: var(--bg-secondary, #1d2025);
    color: var(--text-primary, #e8eaed);
    font: inherit;
    font-size: 13px;
  }
  .list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; max-height: 280px; overflow: auto; }
  .item { display: flex; align-items: baseline; gap: 10px; padding: 3px 6px; border-radius: 6px; }
  .item:hover { background: #22262c; }
  .when {
    flex: 0 0 auto;
    padding: 0;
    border: 0;
    background: transparent;
    color: #58a6ff;
    font: inherit;
    font-size: 12px;
    font-variant-numeric: tabular-nums;
    cursor: pointer;
  }
  .when:hover { text-decoration: underline; }
  .what { flex: 1 1 auto; min-width: 0; font-size: 13px; overflow-wrap: anywhere; white-space: pre-wrap; }
  .acts { flex: 0 0 auto; display: flex; gap: 4px; }
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
  .small-btn.danger { border-color: #7a3a3a; color: #f5a5a5; }
  .muted { color: var(--text-secondary, #a9b0bb); }
  .field-error { color: #f5a5a5; margin: 0; font-size: 13px; }
  .small { font-size: 13px; }
  .tiny { font-size: 12px; }
  p.muted { margin: 0; }
</style>
