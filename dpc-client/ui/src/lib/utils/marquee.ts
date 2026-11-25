// The ticker ("running line") of a title that does not fit its box (Music Player: the sidebar
// button's "Playing: <title>" and the mini-player bar). The rules live here so they are tested
// without a DOM: a line scrolls only when it overflows, by exactly the overflow, at a steady
// speed, with a pause at each end. The component (MarqueeText.svelte) measures and applies them;
// the CSS keyframes use the same split of the cycle (see MARQUEE_HOLD_START, MARQUEE_HOLD_END).

/** Pixels per second the text moves while it scrolls. */
export const MARQUEE_SPEED_PX_S = 38;
/** Never a cycle shorter than this, so a title that overflows by a few pixels does not twitch. */
export const MARQUEE_MIN_CYCLE_S = 6;
/** Overflow below this many pixels is rounding, not an overflow (sub-pixel layout). */
export const MARQUEE_OVERFLOW_EPSILON_PX = 1;
/** The share of one cycle spent forward, and then back: 32 % each; the rest is the two pauses. */
export const MARQUEE_TRAVEL_SHARE = 0.32;
/** Keyframe stops of the cycle, in percent (kept next to the share they are derived from). */
export const MARQUEE_HOLD_START = 18;
export const MARQUEE_HOLD_END = 68;

/** How far the text overflows its box in pixels, or 0 when it fits. A zero-width box (not laid
 *  out yet, or hidden) is never an overflow: there is nothing to scroll in. */
export function marqueeOverflow(contentWidth: number, boxWidth: number): number {
    if (!isFinite(contentWidth) || !isFinite(boxWidth) || boxWidth <= 0 || contentWidth <= 0) return 0;
    const over = contentWidth - boxWidth;
    return over > MARQUEE_OVERFLOW_EPSILON_PX ? Math.ceil(over) : 0;
}

/** Whether a line of this overflow runs: only when it overflows, and never when the person asked
 *  for reduced motion (then it keeps the ellipsis and the full title is in the tooltip). */
export function marqueeShouldRun(overflowPx: number, reducedMotion: boolean): boolean {
    return overflowPx > 0 && !reducedMotion;
}

/** The length of one cycle (forward, pause, back, pause) in seconds for an overflow of `overflowPx`. */
export function marqueeCycleSeconds(overflowPx: number): number {
    if (!(overflowPx > 0)) return 0;
    const travel = overflowPx / MARQUEE_SPEED_PX_S;
    return Math.max(MARQUEE_MIN_CYCLE_S, Math.round((travel / MARQUEE_TRAVEL_SHARE) * 10) / 10);
}
