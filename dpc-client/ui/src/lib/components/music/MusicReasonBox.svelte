<!-- MusicReasonBox.svelte — the inline reason of a Discard or "None of these" (SPEC §12 decision 35). -->
<!--
  A reason is required: free text, a failure class, or both; the box will not submit
  empty. Under the text it offers what was typed before (the core's Discard-reason
  dictionary, `list_music_discard_reasons {prefix}`, which grows with every typed
  reason; it starts empty). Picking one fills the text, and its class when it has one
  and none is chosen. Escape closes the box. Styles come from MusicCreate's root.
-->

<script lang="ts">
  import { onDestroy, onMount, tick } from 'svelte';
  import { discardReasons } from '$lib/services/musicJobs';
  import { reasonCheck, reasonSuggestions, type DiscardReason, type FailureClass } from '$lib/utils/musicCreate';

  let {
    id,
    label,
    submitLabel = 'Discard',
    classes = [],
    onsubmit,
    oncancel,
  }: {
    id: string;
    label: string;
    submitLabel?: string;
    classes?: FailureClass[];
    /** Resolves to an error message, or null when it went through. */
    onsubmit: (text: string, reasonClass: string | null) => Promise<string | null>;
    oncancel: () => void;
  } = $props();

  let text = $state('');
  let reasonClass = $state('');
  let error = $state('');
  let busy = $state(false);
  let reasons = $state<DiscardReason[]>([]);
  let inputEl = $state<HTMLInputElement | null>(null);
  let suggestions = $derived(reasonSuggestions(reasons, text));

  let lookupTimer: ReturnType<typeof setTimeout> | null = null;
  let lookupSeq = 0;
  async function lookup(prefix: string) {
    const seq = ++lookupSeq;
    const found = await discardReasons(prefix);
    if (seq === lookupSeq) reasons = found;
  }
  function onInput() {
    error = '';
    if (lookupTimer) clearTimeout(lookupTimer);
    lookupTimer = setTimeout(() => lookup(text), 200);
  }

  onMount(() => {
    void lookup('');
    tick().then(() => inputEl?.focus());
  });
  onDestroy(() => {
    if (lookupTimer) clearTimeout(lookupTimer);
  });

  function pick(r: DiscardReason) {
    text = r.text;
    if (!reasonClass && r.reason_class) reasonClass = r.reason_class;
    error = '';
    inputEl?.focus();
  }

  async function submit() {
    if (busy) return;
    const check = reasonCheck(text, reasonClass || null);
    if (!check.ok) {
      error = check.message;
      return;
    }
    busy = true;
    try {
      const failed = await onsubmit(text, reasonClass || null);
      if (failed) error = failed;
    } finally {
      busy = false;
    }
  }

  function onKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      oncancel();
    } else if (e.key === 'Enter' && (e.target as HTMLElement)?.tagName === 'INPUT') {
      e.preventDefault();
      submit();
    }
  }
</script>

<!-- Escape anywhere in the box closes it: the keys belong to the inputs and buttons inside. -->
<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<div class="reason-box" role="group" aria-labelledby="{id}-label" onkeydown={onKeydown}>
  <label id="{id}-label" class="small" for="{id}-text">{label}</label>
  <div class="reason-row">
    <input
      id="{id}-text"
      class="text-input"
      type="text"
      maxlength="2000"
      placeholder="Why? e.g. muddy mix, wrong language, cut-off ending"
      bind:this={inputEl}
      bind:value={text}
      oninput={onInput}
      disabled={busy}
      aria-invalid={!!error}
      aria-describedby="{id}-error"
      autocomplete="off"
    />
    <select class="list-select reason-class" bind:value={reasonClass} disabled={busy} aria-label="Failure class (optional)">
      <option value="">No class</option>
      {#each classes as c (c.id)}<option value={c.id} title={c.meaning}>{c.id}: {c.meaning}</option>{/each}
    </select>
  </div>
  {#if suggestions.length}
    <div class="reason-suggestions" role="group" aria-label="Reasons used before">
      <span class="muted tiny">Used before:</span>
      {#each suggestions as r (r.key)}
        <button type="button" class="chip" onclick={() => pick(r)} title="{r.uses} use{r.uses === 1 ? '' : 's'}{r.reason_class ? `, class ${r.reason_class}` : ''}">
          {r.text}{#if r.reason_class}<span class="muted"> · {r.reason_class}</span>{/if}
        </button>
      {/each}
    </div>
  {/if}
  <p id="{id}-error" class="field-error" role="alert">{error}</p>
  <div class="reason-row">
    <button type="button" class="small-btn danger" onclick={submit} disabled={busy}>{busy ? 'Saving…' : submitLabel}</button>
    <button type="button" class="small-btn" onclick={oncancel} disabled={busy}>Cancel</button>
    <span class="muted tiny">Text, a class, or both. Esc closes.</span>
  </div>
</div>
