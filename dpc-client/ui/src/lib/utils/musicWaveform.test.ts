import { describe, expect, it } from 'vitest';
import {
    barCount,
    barRect,
    chooseTicks,
    dragToRegion,
    formatTimeFine,
    hitMarker,
    normalizeRegion,
    resamplePeaks,
    tickTimes,
    timeToX,
    xToTime,
    type Peak,
} from './musicWaveform';

describe('time and x', () => {
    it('maps both ways and clamps', () => {
        expect(timeToX(30, 120, 800)).toBe(200);
        expect(xToTime(200, 120, 800)).toBe(30);
        expect(timeToX(-5, 120, 800)).toBe(0);
        expect(timeToX(500, 120, 800)).toBe(800);
        expect(xToTime(-20, 120, 800)).toBe(0);
        expect(xToTime(9999, 120, 800)).toBe(120);
    });

    it('is 0 when the duration or width is not usable', () => {
        expect(timeToX(5, 0, 800)).toBe(0);
        expect(timeToX(5, 100, 0)).toBe(0);
        expect(xToTime(5, NaN, 800)).toBe(0);
        expect(timeToX(NaN, 100, 800)).toBe(0);
    });
});

describe('tick spacing', () => {
    it('labels a 30 s track every 5 s', () => {
        const p = chooseTicks(30, 800);
        expect(p).toEqual({ label: 5, minor: 1 });
    });
    it('labels a 3 min track every 15 s', () => {
        expect(chooseTicks(180, 800)).toEqual({ label: 15, minor: 5 });
    });
    it('labels a 10 min track every 60 s', () => {
        expect(chooseTicks(600, 800)).toEqual({ label: 60, minor: 10 });
    });
    it('keeps labels at least 64 px apart whatever the width', () => {
        for (const w of [300, 800, 1600]) {
            const p = chooseTicks(180, w);
            expect(p.label * (w / 180)).toBeGreaterThanOrEqual(64);
        }
    });
    it('falls back to the widest step for a track too long to label', () => {
        expect(chooseTicks(100000, 300).label).toBe(1800);
    });
    it('lists tick times with the labelled ones marked', () => {
        const ticks = tickTimes(30, { label: 10, minor: 5 });
        expect(ticks.map(t => t.t)).toEqual([0, 5, 10, 15, 20, 25, 30]);
        expect(ticks.filter(t => t.labelled).map(t => t.t)).toEqual([0, 10, 20, 30]);
        expect(tickTimes(0, { label: 10, minor: 5 })).toEqual([]);
        expect(tickTimes(2, { label: 1, minor: 0.5 }).map(t => t.t)).toEqual([0, 0.5, 1, 1.5, 2]);
    });
});

describe('drag to region', () => {
    it('normalizes a reverse drag', () => {
        expect(dragToRegion(400, 200, 120, 800)).toEqual({ start: 30, end: 60 });
    });
    it('treats a short drag as a click', () => {
        expect(dragToRegion(200, 202, 120, 800)).toBeNull();
    });
    it('rejects a region under the minimum length', () => {
        expect(normalizeRegion(10, 10.1, 100)).toBeNull();
        expect(normalizeRegion(10, 10.3, 100)).toEqual({ start: 10, end: 10.3 });
        expect(normalizeRegion(10, 10.3, 100, 0.5)).toBeNull();
    });
    it('clamps to the duration', () => {
        expect(normalizeRegion(-5, 400, 120)).toEqual({ start: 0, end: 120 });
        expect(dragToRegion(-100, 5000, 120, 800)).toEqual({ start: 0, end: 120 });
        expect(normalizeRegion(130, 140, 120)).toBeNull();
    });
    it('is null for a missing duration', () => {
        expect(normalizeRegion(1, 5, 0)).toBeNull();
        expect(normalizeRegion(NaN, 5, 10)).toBeNull();
    });
});

describe('marker hit-testing', () => {
    const cs = [
        { id: 'p1', t_start: 30, t_end: null },
        { id: 'p2', t_start: 31, t_end: null },
        { id: 'r1', t_start: 60, t_end: 90 },
        { id: 'r2', t_start: 70, t_end: 75 },
    ];
    // 120 s over 1200 px: 10 px per second
    it('hits a point within the tolerance and misses beyond it', () => {
        expect(hitMarker(303, cs, 120, 1200)).toBe('p1');
        expect(hitMarker(320, cs, 120, 1200)).toBeNull();
    });
    it('takes the nearer of two points', () => {
        expect(hitMarker(308, cs, 120, 1200, 8)).toBe('p2');
    });
    it('hits a region inside its band, and the narrowest of overlapping ones', () => {
        expect(hitMarker(650, cs, 120, 1200)).toBe('r1');
        expect(hitMarker(720, cs, 120, 1200)).toBe('r2');
        expect(hitMarker(950, cs, 120, 1200)).toBeNull();
    });
    it('prefers a point over a region under it', () => {
        const both = [{ id: 'r', t_start: 0, t_end: 120 }, { id: 'p', t_start: 60, t_end: null }];
        expect(hitMarker(600, both, 120, 1200)).toBe('p');
        expect(hitMarker(100, both, 120, 1200)).toBe('r');
    });
    it('hits nothing without a duration', () => {
        expect(hitMarker(10, cs, 0, 1200)).toBeNull();
    });
});

describe('bucket rendering scale', () => {
    it('counts the bars that fit', () => {
        expect(barCount(800, 2, 1)).toBe(267);
        expect(barCount(10, 2, 1)).toBe(3);
        expect(barCount(0, 2, 1)).toBe(0);
        expect(barCount(1, 3, 2)).toBe(1);
    });
    it('keeps the extremes when many peaks go into one bar', () => {
        const peaks: Peak[] = [[-0.1, 0.2], [-0.5, 0.1], [-0.2, 0.9], [0, 0.3]];
        expect(resamplePeaks(peaks, 2)).toEqual([[-0.5, 0.2], [-0.2, 0.9]]);
        expect(resamplePeaks(peaks, 1)).toEqual([[-0.5, 0.9]]);
    });
    it('repeats the nearest peak when there are fewer peaks than bars', () => {
        const out = resamplePeaks([[-1, 1], [0, 0]], 4);
        expect(out).toEqual([[-1, 1], [-1, 1], [0, 0], [0, 0]]);
    });
    it('answers nothing for no peaks or no bars', () => {
        expect(resamplePeaks([], 5)).toEqual([]);
        expect(resamplePeaks([[0, 1]], 0)).toEqual([]);
    });
    it('scales a bar to the canvas height around the middle', () => {
        expect(barRect([-1, 1], 100)).toEqual({ y: 0, h: 100 });
        expect(barRect([-0.5, 0.25], 100)).toEqual({ y: 25, h: 50 });
        expect(barRect([-3, 3], 100)).toEqual({ y: 0, h: 100 });
    });
    it('keeps a silent bar one pixel thick', () => {
        expect(barRect([0, 0], 100)).toEqual({ y: 49.5, h: 1 });
    });
});

describe('tooltip time', () => {
    it('shows minutes, seconds and tenths', () => {
        expect(formatTimeFine(26.3)).toBe('0:26.3');
        expect(formatTimeFine(75.99)).toBe('1:15.9');
        expect(formatTimeFine(0)).toBe('0:00.0');
        expect(formatTimeFine(-1)).toBe('–:––');
    });
});
