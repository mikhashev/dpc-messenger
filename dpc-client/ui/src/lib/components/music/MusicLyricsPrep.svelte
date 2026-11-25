<!-- MusicLyricsPrep.svelte — "Prepare lyrics" in the Create panel (core music/lyrics_prep.py). -->
<!--
  Reads the lyrics in the Create form through the read-only core command `prepare_music_lyrics`
  and shows, per line, its syllables and whether it is off its section's meter, and per word the
  stress status: stress found (the dictionary has one stress for the word), ambiguous (a homograph:
  click the word, then the reading you mean), unknown, marked by you, a mark that is not usable.
  Nothing is changed in the text until "Use marked lyrics", which puts the text with U+0301 marks
  added (only where the dictionary has one stress, or where you picked one) into the lyrics box, and
  only for YuE2, the engine documented to read them (SPEC decision 39). The form offers Undo afterwards.
  Whatever the check found is recorded in the job's `lyrics_prep` (the form keeps it while the
  text it describes is the text sent). Styles come from MusicCreate's root.
-->

<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import { prepareMusicLyrics } from '$lib/coreService';
  import {
    canUseMarked,
    dictionaryLine,
    lineSegments,
    markedState,
    meterText,
    needsAttention,
    plainState,
    statusLabel,
    summaryText,
    useMarkedLabel,
    withChoice,
    type LyricsPrepState,
    type PrepReport,
    type PrepWord,
  } from '$lib/utils/musicLyricsPrep';

  let {
    text,
    engine,
    onprepared,
    onuse,
    onclose,
  }: {
    /** The lyrics now in the form. */
    text: string;
    /** 'yue2' or 'acestep'. */
    engine: string;
    /** The check ran on the text as written: its plain record. */
    onprepared: (state: LyricsPrepState) => void;
    /** "Use marked lyrics": the marked text and its record. */
    onuse: (markedText: string, state: LyricsPrepState) => void;
    onclose: () => void;
  } = $props();

  let report = $state<PrepReport | null>(null);
  let preparedText = $state<string | null>(null);
  let choices = $state<Record<string, string>>({});
  let openKey = $state<string | null>(null);
  let busy = $state(false);
  let error = $state('');
  let seq = 0;

  let stale = $derived(report !== null && preparedText !== text);
  let use = $derived(canUseMarked(report, stale));
  let needsMarks = $derived(report !== null && report.summary.counts.ambiguous > 0);

  async function run() {
    const mine = ++seq;
    const sent = text;
    busy = true;
    error = '';
    const a = await prepareMusicLyrics(sent, 'auto', choices, engine);
    if (mine !== seq) return;
    busy = false;
    if (!a.ok) {
      error = a.message;
      return;
    }
    report = a.data.report;
    preparedText = sent;
    onprepared(plainState(a.data.report, sent, engine));
  }

  onMount(() => {
    void run();
  });

  // The engine changed under an open panel: what may be offered changes with it.
  let lastEngine: string | undefined;
  $effect(() => {
    const e = engine;
    if (lastEngine !== undefined && e !== lastEngine) untrack(() => void run());
    lastEngine = e;
  });

  function pick(word: PrepWord, option: string | null) {
    choices = withChoice(choices, word.key, option);
    openKey = null;
    void run();
  }

  function toggle(word: PrepWord) {
    openKey = openKey === word.key ? null : word.key;
  }

  function useMarked() {
    if (!report || !use.ok) return;
    const state = markedState(report, engine);
    if (state) onuse(report.marked_text, state);
  }

  function optionsOf(word: PrepWord): string[] {
    return word.options ?? [];
  }
</script>

<section class="card prep" aria-labelledby="mp-label" data-lyrics-prep>
  <div class="section-head">
    <span id="mp-label" class="section-label">PREPARE LYRICS</span>
    <button type="button" class="small-btn" onclick={run} disabled={busy}>{busy ? 'Checking…' : 'Check again'}</button>
    <button type="button" class="small-btn" onclick={onclose}>Close</button>
  </div>

  {#if error}<p class="field-error" role="alert">{error}</p>{/if}

  {#if report}
    <p class="muted tiny" data-prep-dictionary>{dictionaryLine(report.dictionary)}</p>
    <p class="small" role="status" data-prep-summary>{summaryText(report)}</p>
    {#if stale}
      <p class="warn-text" role="status">The lyrics changed after this check. Check again before using the marks.</p>
    {/if}

    {#if report.issues.length}
      <ul class="prep-issues" aria-label="Stress mark problems">
        {#each report.issues as issue, i (i)}
          <li class="warn-text">Line {issue.line}{issue.word ? `, ${issue.word}` : ''}: {issue.message}</li>
        {/each}
      </ul>
    {/if}

    <ol class="prep-lines" aria-label="Lines">
      {#each report.lines as line (line.n)}
        {#if line.kind === 'section'}
          {@const sec = report.sections.find(s => s.index === line.section)}
          <li class="prep-section">
            <span class="tag">{line.text.trim()}</span>
            {#if sec?.checked}
              <span class="muted tiny">typical {sec.typical} syllables, ±{sec.tolerance}</span>
            {:else if sec}
              <span class="muted tiny">meter not checked: {sec.reason}</span>
            {/if}
          </li>
        {:else}
          <li class="prep-line" class:flag={!!line.meter?.flag}>
            <span class="prep-n muted tiny">{line.n}</span>
            <span class="prep-syl tiny" class:warn={!!line.meter?.flag} title={meterText(line, report.sections)}>{line.syllables}</span>
            <span class="prep-text">
              {#each lineSegments(line) as seg, i (i)}
                {#if seg.word && seg.word.status === 'ambiguous'}
                  <button type="button" class="w ambiguous" aria-expanded={openKey === seg.word.key}
                    aria-label="{seg.word.text}: ambiguous, {optionsOf(seg.word).length} readings. Choose one."
                    title={statusLabel(seg.word)} onclick={() => toggle(seg.word!)}>{seg.text}</button>
                {:else if seg.word && seg.word.status === 'resolved' && seg.word.source === 'person'}
                  <button type="button" class="w person" aria-expanded={openKey === seg.word.key}
                    aria-label="{seg.word.text}: your pick. Change it."
                    title={statusLabel(seg.word)} onclick={() => toggle(seg.word!)}>{seg.text}</button>
                {:else if seg.word && needsAttention(seg.word)}
                  <span class="w {seg.word.status}" title={statusLabel(seg.word)}>{seg.text}</span>
                {:else if seg.word && seg.word.status === 'resolved'}
                  <span class="w resolved" title={statusLabel(seg.word)}>{seg.text}</span>
                {:else if seg.word && seg.word.status === 'author_marked'}
                  <span class="w author" title={statusLabel(seg.word)}>{seg.text}</span>
                {:else}
                  <span class="plain-seg">{seg.text}</span>
                {/if}
              {/each}
            </span>
            {#if line.meter?.flag}
              <span class="tag warn" title={meterText(line, report.sections)}>{line.meter.flag === 'long' ? `+${line.meter.delta}` : line.meter.delta}</span>
            {/if}
          </li>
          {#each line.words ?? [] as word (word.key)}
            {#if openKey === word.key}
              <li class="prep-options" aria-label="Readings of {word.text}">
                <span class="muted tiny">{word.text}:</span>
                {#each optionsOf(word) as option (option)}
                  <button type="button" class="chip" class:on={choices[word.key] === option} onclick={() => pick(word, option)}>{option}</button>
                {/each}
                {#if choices[word.key]}
                  <button type="button" class="chip" onclick={() => pick(word, null)}>Clear pick</button>
                {/if}
              </li>
            {/if}
          {/each}
        {/if}
      {/each}
    </ol>

    <p class="muted tiny" data-prep-engine>
      {report.engine.accepts_marks ? 'Stress marks go to the engine only if you use the marked lyrics.' : report.engine.why}
    </p>
    {#if needsMarks}
      <p class="muted tiny">An ambiguous word gets no mark until you pick its reading.</p>
    {/if}

    <div class="form-row">
      <button type="button" class="small-btn primary" onclick={useMarked} disabled={!use.ok || busy}
        aria-describedby="mp-use-why">{useMarkedLabel(report)}</button>
      <span id="mp-use-why" class="muted tiny">{use.ok ? '' : use.reason}</span>
    </div>
  {:else if busy}
    <p class="muted small" role="status">Checking…</p>
  {/if}
</section>

<style>
  .prep-lines { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; max-height: 320px; overflow: auto; }
  .prep-section { display: flex; align-items: center; gap: 8px; padding: 4px 0 0; }
  .prep-line { display: grid; grid-template-columns: 28px 28px 1fr auto; gap: 8px; align-items: baseline; padding: 2px 6px; border-radius: 6px; font-size: 14px; }
  .prep-line.flag { background: #2c2616; }
  .prep-n { text-align: right; }
  .prep-syl { text-align: right; color: #c9d1d9; font-variant-numeric: tabular-nums; }
  .prep-syl.warn { color: #f0c674; font-weight: 700; }
  .prep-text { min-width: 0; overflow-wrap: anywhere; line-height: 1.5; }
  .prep-issues { margin: 0; padding-left: 18px; display: flex; flex-direction: column; gap: 2px; }
  .prep-options { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; padding: 2px 6px 4px 64px; }
  .w { font: inherit; border-radius: 4px; padding: 0 2px; }
  .w.resolved { color: #9fd8ac; }
  .w.author { color: #b6d4ff; }
  .w.not_checked, .w.not_needed { color: inherit; }
  .w.unknown { color: #f5a5a5; text-decoration: underline dotted; }
  .w.invalid_mark { color: #f5a5a5; text-decoration: underline wavy; }
  button.w { border: 1px solid transparent; background: transparent; cursor: pointer; }
  button.w.ambiguous { color: #f0c674; border-color: #6b5a24; background: #2c2616; }
  button.w.person { color: #9fd8ac; border-color: #2f5b3b; background: #1b3323; }
  button.w:hover { border-color: #58a6ff; }
  button.w:focus-visible { outline: 2px solid #58a6ff; outline-offset: 1px; }
  .prep :global(.chip.on) { border-color: #58a6ff; color: #58a6ff; }
</style>
