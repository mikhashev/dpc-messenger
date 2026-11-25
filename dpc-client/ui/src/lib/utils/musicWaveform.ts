// The pure parts of the waveform view (MusicWaveform.svelte): time <-> x, the time ruler's
// tick spacing, a drag turned into a region, which comment marker the pointer is on, and
// how the core's peaks become bars. No DOM here, so it all tests without a canvas.

import type { MusicComment } from './musicComments';

/** The core's peaks: one `[min, max]` per bucket, each in -1..1 (`music/peaks.py`). */
export type Peak = [number, number];

/** A region shorter than this is a click, not a region. */
export const MIN_REGION_S = 0.2;
/** A pointer that moved fewer pixels than this between down and up is a click. */
export const DRAG_THRESHOLD_PX = 4;
/** How close (px) the pointer must be to a point marker's line to hit it: a hit area of twice this
 *  (16 px), never under `MARKER_MIN_HIT_PX` for a pointer. */
export const MARKER_HIT_PX = 8;
/** The smallest hit area (px) a comment marker may have, in the waveform and in the strip. */
export const MARKER_MIN_HIT_PX = 12;

const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));

/** The x (px) of time `t`, clamped to 0..width. 0 when the duration or the width is unusable. */
export function timeToX(t: number, duration: number, width: number): number {
    if (!(duration > 0) || !(width > 0) || !Number.isFinite(t)) return 0;
    return clamp((t / duration) * width, 0, width);
}

/** The time (s) at x px, clamped to 0..duration. */
export function xToTime(x: number, duration: number, width: number): number {
    if (!(duration > 0) || !(width > 0) || !Number.isFinite(x)) return 0;
    return clamp((x / width) * duration, 0, duration);
}

/** Whether the static time ruler must be redrawn when the total duration goes from `prev` to
 *  `next`: the ruler cannot draw at 0, so a first real duration (or a changed one) needs a draw. */
export function rulerNeedsRedraw(prev: number, next: number): boolean {
    if (!(next > 0)) return false;
    return !(prev > 0) || Math.abs(next - prev) > 1e-6;
}

export interface TickPlan {
    /** Seconds between labelled (long) ticks. */
    label: number;
    /** Seconds between short ticks. */
    minor: number;
}

// label step -> minor step; a labelled tick is also a minor one.
const TICK_STEPS: TickPlan[] = [
    { label: 1, minor: 0.5 },
    { label: 2, minor: 1 },
    { label: 5, minor: 1 },
    { label: 10, minor: 5 },
    { label: 15, minor: 5 },
    { label: 30, minor: 10 },
    { label: 60, minor: 10 },
    { label: 120, minor: 30 },
    { label: 300, minor: 60 },
    { label: 600, minor: 120 },
    { label: 1800, minor: 300 },
];

/** The smallest step whose labels are at least `minLabelPx` apart at this zoom. */
export function chooseTicks(duration: number, width: number, minLabelPx = 64): TickPlan {
    if (!(duration > 0) || !(width > 0)) return TICK_STEPS[TICK_STEPS.length - 1];
    const pxPerS = width / duration;
    return TICK_STEPS.find(s => s.label * pxPerS >= minLabelPx) ?? TICK_STEPS[TICK_STEPS.length - 1];
}

/** The tick times in 0..duration for a plan: `{t, labelled}`, in order. */
export function tickTimes(duration: number, plan: TickPlan): { t: number; labelled: boolean }[] {
    const out: { t: number; labelled: boolean }[] = [];
    if (!(duration > 0)) return out;
    const count = Math.floor(duration / plan.minor + 1e-9);
    for (let i = 0; i <= count; i++) {
        const t = Math.round(i * plan.minor * 1000) / 1000;
        out.push({ t, labelled: Math.abs(t / plan.label - Math.round(t / plan.label)) < 1e-6 });
    }
    return out;
}

/** Two times (any order, any range) as a region inside 0..duration, or null when it is
 *  shorter than `minLength` (then it is a click). */
export function normalizeRegion(
    a: number,
    b: number,
    duration: number,
    minLength = MIN_REGION_S,
): { start: number; end: number } | null {
    if (!(duration > 0) || !Number.isFinite(a) || !Number.isFinite(b)) return null;
    const start = clamp(Math.min(a, b), 0, duration);
    const end = clamp(Math.max(a, b), 0, duration);
    if (end - start < minLength) return null;
    return { start, end };
}

/** A pointer drag from `x0` to `x1` (px) as a region, or null for a click. */
export function dragToRegion(
    x0: number,
    x1: number,
    duration: number,
    width: number,
    minLength = MIN_REGION_S,
): { start: number; end: number } | null {
    if (Math.abs(x1 - x0) < DRAG_THRESHOLD_PX) return null;
    return normalizeRegion(xToTime(x0, duration, width), xToTime(x1, duration, width), duration, minLength);
}

/** The comment under the pointer at `x` px, or null. A point wins over a region and the
 *  nearer point over a farther one; of several regions the narrowest wins. */
export function hitMarker(
    x: number,
    comments: Pick<MusicComment, 'id' | 't_start' | 't_end'>[],
    duration: number,
    width: number,
    tolerance = MARKER_HIT_PX,
): string | null {
    if (!(duration > 0) || !(width > 0)) return null;
    let point: { id: string; d: number } | null = null;
    let region: { id: string; w: number } | null = null;
    for (const c of comments) {
        const left = timeToX(c.t_start, duration, width);
        if (c.t_end == null) {
            const d = Math.abs(x - left);
            if (d <= tolerance && (!point || d < point.d)) point = { id: c.id, d };
        } else {
            const right = timeToX(c.t_end, duration, width);
            if (x >= left - 1 && x <= right + 1 && (!region || right - left < region.w)) {
                region = { id: c.id, w: right - left };
            }
        }
    }
    return point?.id ?? region?.id ?? null;
}

/** How many bars fit in `widthPx` for a bar `barPx` wide with `gapPx` between them. */
export function barCount(widthPx: number, barPx: number, gapPx: number): number {
    if (!(widthPx > 0) || !(barPx > 0)) return 0;
    return Math.max(1, Math.floor((widthPx + gapPx) / (barPx + gapPx)));
}

/** The peaks resampled to `bars` bars. Several peaks into one bar keep the extremes (the
 *  lowest min, the highest max); fewer peaks than bars repeat the nearest one. */
export function resamplePeaks(peaks: readonly Peak[], bars: number): Peak[] {
    const n = peaks.length;
    if (n === 0 || bars <= 0) return [];
    const out: Peak[] = [];
    for (let i = 0; i < bars; i++) {
        const lo = Math.floor((i * n) / bars);
        const hi = Math.max(lo + 1, Math.floor(((i + 1) * n) / bars));
        let min = 1;
        let max = -1;
        for (let k = lo; k < Math.min(hi, n); k++) {
            if (peaks[k][0] < min) min = peaks[k][0];
            if (peaks[k][1] > max) max = peaks[k][1];
        }
        out.push([min, max]);
    }
    return out;
}

/** A bar's vertical extent on a canvas `height` px tall, the zero line in the middle. A
 *  full-scale peak reaches the edge; a silent bar is still `minBarPx` thick so the line
 *  shows. */
export function barRect(peak: Peak, height: number, minBarPx = 1): { y: number; h: number } {
    const mid = height / 2;
    const amp = Math.max(Math.abs(peak[0]), Math.abs(peak[1]));
    const half = Math.max(minBarPx / 2, clamp(amp, 0, 1) * mid);
    return { y: mid - half, h: half * 2 };
}

/** "0:26.3": minutes, seconds and tenths, for the hover tooltip. */
export function formatTimeFine(t: number | null | undefined): string {
    if (t == null || !Number.isFinite(t) || t < 0) return '–:––';
    const tenths = Math.floor(t * 10 + 1e-6);
    const s = Math.floor(tenths / 10);
    const m = Math.floor(s / 60);
    return `${m}:${String(s % 60).padStart(2, '0')}.${tenths % 10}`;
}
