// The geometry of a floating window (the Agent map): its default box, where a
// drag or a resize may take it, and what is remembered between openings.
//
// Pure: the component reads pointer events and the viewport and calls these.
// The box is kept inside the viewport; a minimised window is only its header
// bar, so only the bar has to stay in view.

export interface WindowRect {
    x: number;
    y: number;
    width: number;
    height: number;
}

export interface Viewport {
    width: number;
    height: number;
}

export interface WindowState {
    rect: WindowRect;
    minimised: boolean;
}

/** The smallest box a window can be resized to (a smaller viewport wins). */
export const MIN_WIDTH = 480;
export const MIN_HEIGHT = 320;
/** The height of the header bar; a minimised window is exactly this tall. */
export const HEADER_HEIGHT = 56;
/** The box a first opening gets, before the viewport trims it. */
export const DEFAULT_WIDTH = 1180;
export const DEFAULT_HEIGHT = 780;
/** The gap a default box leaves to the viewport's edge. */
export const VIEWPORT_MARGIN = 24;
/** A pointer that moves less than this between press and release is a click. */
export const CLICK_SLOP_PX = 4;
/** The browser storage key of the remembered window. */
export const WINDOW_STORAGE_KEY = 'dpc.agentMap.window';

function between(value: number, low: number, high: number): number {
    return Math.min(Math.max(value, low), Math.max(low, high));
}

/** The window's box, kept in the viewport: sizes within [min, viewport], and the
 *  whole box in view, or only the header bar when minimised. */
export function clampRect(rect: WindowRect, vp: Viewport, minimised = false): WindowRect {
    const width = between(rect.width, Math.min(MIN_WIDTH, vp.width), vp.width);
    const height = between(rect.height, Math.min(MIN_HEIGHT, vp.height), vp.height);
    const shown = minimised ? Math.min(HEADER_HEIGHT, vp.height) : height;
    return {
        x: between(rect.x, 0, vp.width - width),
        y: between(rect.y, 0, vp.height - shown),
        width,
        height,
    };
}

/** The first box: centred, leaving a margin, never smaller than the minimum. */
export function defaultRect(vp: Viewport): WindowRect {
    const width = Math.min(DEFAULT_WIDTH, vp.width - 2 * VIEWPORT_MARGIN);
    const height = Math.min(DEFAULT_HEIGHT, vp.height - 2 * VIEWPORT_MARGIN);
    const box = clampRect({ x: 0, y: 0, width, height }, vp);
    return { ...box, x: Math.round((vp.width - box.width) / 2), y: Math.round((vp.height - box.height) / 2) };
}

/** The box after a header drag of (dx, dy) from `start`. */
export function moveRect(start: WindowRect, dx: number, dy: number, vp: Viewport, minimised = false): WindowRect {
    return clampRect({ ...start, x: start.x + dx, y: start.y + dy }, vp, minimised);
}

/** The box after the bottom-right handle moved by (dx, dy): the top-left corner
 *  stays, and the box cannot grow past the viewport's edge. */
export function resizeRect(start: WindowRect, dx: number, dy: number, vp: Viewport): WindowRect {
    const width = between(start.width + dx, Math.min(MIN_WIDTH, vp.width), vp.width - start.x);
    const height = between(start.height + dy, Math.min(MIN_HEIGHT, vp.height), vp.height - start.y);
    return clampRect({ ...start, width, height }, vp);
}

/** True when a press-and-release moved too little to be a drag. */
export function isClick(dx: number, dy: number): boolean {
    return Math.hypot(dx, dy) < CLICK_SLOP_PX;
}

function finite(v: unknown): v is number {
    return typeof v === 'number' && Number.isFinite(v);
}

/** A stored window, or null when the text is missing, not JSON or not a box. */
export function parseWindowState(raw: string | null | undefined): WindowState | null {
    if (!raw) return null;
    try {
        const v = JSON.parse(raw);
        const r = v?.rect;
        if (!r || !finite(r.x) || !finite(r.y) || !finite(r.width) || !finite(r.height)) return null;
        if (r.width <= 0 || r.height <= 0) return null;
        return {
            rect: { x: r.x, y: r.y, width: r.width, height: r.height },
            minimised: v.minimised === true,
        };
    } catch {
        return null;
    }
}

type StorageLike = Pick<Storage, 'getItem' | 'setItem'>;

/** The remembered window fitted to this viewport, or the default box. Storage
 *  that throws or is absent is the same as nothing remembered. */
export function loadWindowState(storage: StorageLike | null | undefined, vp: Viewport): WindowState {
    let stored: WindowState | null = null;
    try {
        stored = parseWindowState(storage?.getItem(WINDOW_STORAGE_KEY));
    } catch {
        stored = null;
    }
    if (!stored) return { rect: defaultRect(vp), minimised: false };
    return { rect: clampRect(stored.rect, vp, stored.minimised), minimised: stored.minimised };
}

/** Remember the window; a storage that refuses is ignored. */
export function saveWindowState(storage: StorageLike | null | undefined, state: WindowState): void {
    try {
        storage?.setItem(WINDOW_STORAGE_KEY, JSON.stringify(state));
    } catch {
        /* private window, blocked or full storage: the window works without it */
    }
}
