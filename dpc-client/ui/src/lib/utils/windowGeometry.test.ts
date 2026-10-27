import { describe, it, expect } from 'vitest';
import {
    DEFAULT_HEIGHT,
    DEFAULT_WIDTH,
    HEADER_HEIGHT,
    MIN_HEIGHT,
    MIN_WIDTH,
    VIEWPORT_MARGIN,
    WINDOW_STORAGE_KEY,
    clampRect,
    defaultRect,
    isClick,
    loadWindowState,
    moveRect,
    parseWindowState,
    resizeRect,
    saveWindowState,
} from './windowGeometry';

const VP = { width: 1600, height: 900 };

describe('the default box', () => {
    it('is the default size, centred', () => {
        expect(defaultRect(VP)).toEqual({
            x: (1600 - DEFAULT_WIDTH) / 2, y: (900 - DEFAULT_HEIGHT) / 2, width: DEFAULT_WIDTH, height: DEFAULT_HEIGHT,
        });
    });

    it('leaves a margin on a small viewport and stays centred', () => {
        const r = defaultRect({ width: 1000, height: 600 });
        expect(r.width).toBe(1000 - 2 * VIEWPORT_MARGIN);
        expect(r.height).toBe(600 - 2 * VIEWPORT_MARGIN);
        expect(r.x).toBe(VIEWPORT_MARGIN);
        expect(r.y).toBe(VIEWPORT_MARGIN);
    });

    it('is no smaller than the minimum when the viewport allows it', () => {
        const r = defaultRect({ width: 500, height: 340 });
        expect(r.width).toBe(MIN_WIDTH);
        expect(r.height).toBe(MIN_HEIGHT);
        expect(r.x).toBeGreaterThanOrEqual(0);
        expect(r.y).toBeGreaterThanOrEqual(0);
    });

    it('takes the whole viewport when the viewport is under the minimum', () => {
        expect(defaultRect({ width: 400, height: 300 })).toEqual({ x: 0, y: 0, width: 400, height: 300 });
    });
});

describe('clamping', () => {
    it('leaves a box that fits alone', () => {
        const r = { x: 100, y: 50, width: 800, height: 600 };
        expect(clampRect(r, VP)).toEqual(r);
    });

    it('pulls a box that sticks out back inside', () => {
        expect(clampRect({ x: -40, y: -10, width: 800, height: 600 }, VP)).toMatchObject({ x: 0, y: 0 });
        expect(clampRect({ x: 1500, y: 800, width: 800, height: 600 }, VP)).toMatchObject({ x: 800, y: 300 });
    });

    it('trims a box bigger than the viewport and raises a smaller one to the minimum', () => {
        expect(clampRect({ x: 0, y: 0, width: 5000, height: 5000 }, VP)).toMatchObject({ width: 1600, height: 900 });
        expect(clampRect({ x: 0, y: 0, width: 10, height: 10 }, VP)).toMatchObject({ width: MIN_WIDTH, height: MIN_HEIGHT });
    });

    it('keeps only the header bar in view for a minimised window', () => {
        const r = clampRect({ x: 100, y: 5000, width: 800, height: 600 }, VP, true);
        expect(r.y).toBe(900 - HEADER_HEIGHT);
        expect(r.height).toBe(600);
    });
});

describe('moving and resizing', () => {
    const start = { x: 200, y: 100, width: 800, height: 500 };

    it('moves by the pointer travel and stops at the viewport edges', () => {
        expect(moveRect(start, 50, -30, VP)).toEqual({ ...start, x: 250, y: 70 });
        expect(moveRect(start, -9999, -9999, VP)).toMatchObject({ x: 0, y: 0 });
        expect(moveRect(start, 9999, 9999, VP)).toMatchObject({ x: 800, y: 400 });
    });

    it('lets a minimised window go down to its bar', () => {
        expect(moveRect(start, 0, 9999, VP, true).y).toBe(900 - HEADER_HEIGHT);
    });

    it('resizes from the bottom-right handle, the top-left corner staying', () => {
        expect(resizeRect(start, 100, 50, VP)).toEqual({ x: 200, y: 100, width: 900, height: 550 });
    });

    it('does not shrink under the minimum or grow past the viewport edge', () => {
        expect(resizeRect(start, -9999, -9999, VP)).toEqual({ x: 200, y: 100, width: MIN_WIDTH, height: MIN_HEIGHT });
        expect(resizeRect(start, 9999, 9999, VP)).toEqual({ x: 200, y: 100, width: 1400, height: 800 });
    });
});

describe('a click is not a drag', () => {
    it('is a press that moved under the slop', () => {
        expect(isClick(0, 0)).toBe(true);
        expect(isClick(2, 2)).toBe(true);
        expect(isClick(4, 0)).toBe(false);
        expect(isClick(-3, 3)).toBe(false);
    });
});

describe('what is remembered', () => {
    const memory = () => {
        const data = new Map<string, string>();
        return { data, getItem: (k: string) => data.get(k) ?? null, setItem: (k: string, v: string) => void data.set(k, v) };
    };

    it('round-trips a box and the minimised flag', () => {
        const s = memory();
        saveWindowState(s, { rect: { x: 10, y: 20, width: 700, height: 500 }, minimised: true });
        expect(loadWindowState(s, VP)).toEqual({ rect: { x: 10, y: 20, width: 700, height: 500 }, minimised: true });
    });

    it('fits a remembered box to a smaller viewport', () => {
        const s = memory();
        saveWindowState(s, { rect: { x: 1200, y: 600, width: 1000, height: 800 }, minimised: false });
        const w = loadWindowState(s, { width: 1100, height: 700 });
        expect(w.rect).toEqual({ x: 100, y: 0, width: 1000, height: 700 });
    });

    it('gives the default box when nothing usable is stored', () => {
        const fresh = { rect: defaultRect(VP), minimised: false };
        expect(loadWindowState(memory(), VP)).toEqual(fresh);
        expect(loadWindowState(null, VP)).toEqual(fresh);
        const s = memory();
        s.data.set(WINDOW_STORAGE_KEY, '{broken');
        expect(loadWindowState(s, VP)).toEqual(fresh);
        s.data.set(WINDOW_STORAGE_KEY, JSON.stringify({ rect: { x: 'a', y: 0, width: 1, height: 1 } }));
        expect(loadWindowState(s, VP)).toEqual(fresh);
        s.data.set(WINDOW_STORAGE_KEY, JSON.stringify({ rect: { x: 0, y: 0, width: 0, height: 100 } }));
        expect(parseWindowState(s.data.get(WINDOW_STORAGE_KEY))).toBeNull();
    });

    it('works with a storage that throws', () => {
        const hostile = {
            getItem: () => { throw new Error('blocked'); },
            setItem: () => { throw new Error('full'); },
        };
        expect(() => saveWindowState(hostile, { rect: defaultRect(VP), minimised: false })).not.toThrow();
        expect(loadWindowState(hostile, VP)).toEqual({ rect: defaultRect(VP), minimised: false });
    });
});
