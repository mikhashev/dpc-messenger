<!--
  MarqueeText.svelte — one line of text that scrolls like a ticker, but only when it does not
  fit its box (Music Player: the sidebar button's "Playing: <title>" and the mini-player title;
  backlog THE-MUSIC-PLAYER-BUTTON-CUTS-A-LONG-TRACK-TITLE-INSTEAD-OF-SCROLLING-IT).

  - It fits: plain text, no animation, nothing to measure again until the text or the box changes.
  - It overflows: the text moves left by exactly the overflow, waits, and moves back, waits
    (utils/marquee.ts holds the numbers). The box keeps its size, so nothing around it shifts.
  - Pointer over the line (or focus in it): the motion pauses.
  - prefers-reduced-motion: reduce: no motion; the ellipsis stays and `title` shows the whole text.
  The box is as wide as its parent gives it (`min-width: 0`, `flex` friendly); `class` styles it.
-->
<script lang="ts">
  import { tick } from 'svelte';
  import { marqueeCycleSeconds, marqueeOverflow } from '$lib/utils/marquee';

  let { text, title = undefined, class: className = '' }: { text: string; title?: string; class?: string } = $props();

  let box: HTMLSpanElement | undefined = $state();
  let overflow = $state(0);

  function measure() {
    overflow = box ? marqueeOverflow(box.scrollWidth, box.clientWidth) : 0;
  }

  // The text changed (a new track): measure after the DOM has it.
  $effect(() => {
    void text;
    void tick().then(measure);
  });

  // The box changed size (a resize of the bar, a webview zoom): measure again.
  $effect(() => {
    const el = box;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(() => measure());
    ro.observe(el);
    return () => ro.disconnect();
  });

  let cycle = $derived(marqueeCycleSeconds(overflow));
</script>

<span
  class="marquee {className}"
  class:run={overflow > 0}
  bind:this={box}
  title={title ?? text}
  style:--mq-shift={overflow > 0 ? `-${overflow}px` : undefined}
  style:--mq-dur={cycle > 0 ? `${cycle}s` : undefined}
><span class="marquee-inner">{text}</span></span>

<style>
  .marquee {
    display: block;
    min-width: 0;
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
  }
  .marquee-inner { display: inline; }

  @media (prefers-reduced-motion: no-preference) {
    .marquee.run { text-overflow: clip; }
    .marquee.run .marquee-inner {
      display: inline-block;
      will-change: transform;
      animation: marquee-run var(--mq-dur, 8s) ease-in-out infinite;
    }
    .marquee.run:hover .marquee-inner,
    .marquee.run:focus-within .marquee-inner { animation-play-state: paused; }
  }

  /* Hold at the start, travel, hold at the end, travel back. The stops match utils/marquee.ts. */
  @keyframes marquee-run {
    0%, 18% { transform: translateX(0); }
    50%, 68% { transform: translateX(var(--mq-shift, 0px)); }
    100% { transform: translateX(0); }
  }
</style>
