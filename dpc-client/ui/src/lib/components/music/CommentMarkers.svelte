<!-- CommentMarkers.svelte — the comment markers under a position slider (mp.comment/1); the fallback when the waveform cannot be drawn (MusicWaveform.svelte draws markers on the waveform). -->
<!--
  A thin strip under the slider: a dot for a point comment, a bar for a region, placed by
  markerGeometry (time as a percentage of the duration, clamped). Hovering a marker shows
  its text; clicking it seeks there. It sits under the slider, not over it, so it never
  takes a drag meant for the thumb.
-->

<script lang="ts">
  import { formatTime } from '$lib/utils/musicPlayer';
  import { commentTimeLabel, markerGeometry, type MusicComment } from '$lib/utils/musicComments';

  let {
    comments = [],
    duration = 0,
    onseek,
  }: { comments?: MusicComment[]; duration?: number; onseek: (seconds: number) => void } = $props();

  let placed = $derived(
    comments.flatMap(c => {
      const g = markerGeometry(c, duration);
      return g ? [{ c, g }] : [];
    }),
  );
</script>

{#if placed.length}
  <div class="strip" role="group" aria-label="Comments on the track">
    {#each placed as { c, g } (c.id)}
      <button
        type="button"
        class="mk"
        class:region={g.width !== null}
        style={g.width === null ? `left:${g.left}%` : `left:${g.left}%;width:max(12px,${g.width}%)`}
        title="{commentTimeLabel(c, formatTime)}: {c.text}"
        aria-label="Comment at {commentTimeLabel(c, formatTime)}: {c.text}"
        onclick={() => onseek(c.t_start)}
      ></button>
    {/each}
  </div>
{/if}

<style>
  .strip { position: relative; height: 12px; margin-top: 2px; }
  /* The button is 12 px square (the pointer's hit area, MARKER_MIN_HIT_PX); only its 8 px centre is
     painted (padding + background-clip), so the dot looks as before and is easier to hit. */
  .mk {
    position: absolute;
    top: 0;
    width: 12px;
    height: 12px;
    margin-left: -6px;
    padding: 2px;
    box-sizing: border-box;
    border: 0;
    border-radius: 50%;
    background: #e3b341;
    background-clip: content-box;
    cursor: pointer;
  }
  .mk.region {
    margin-left: 0;
    min-width: 12px;
    padding: 3px 0;
    border-radius: 3px;
    background: rgba(88, 166, 255, 0.75);
    background-clip: content-box;
  }
  .mk:hover, .mk:focus-visible { outline: 2px solid #ffffff; outline-offset: 1px; }
</style>
