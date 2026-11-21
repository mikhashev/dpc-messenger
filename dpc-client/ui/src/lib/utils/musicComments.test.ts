import { describe, expect, it } from 'vitest';
import {
    COMMENT_ACTIONS,
    COMMENT_MAX_CHARS,
    commentTimeLabel,
    isCommentShortcut,
    markerGeometry,
    secondsText,
    validateCommentForm,
} from './musicComments';
import { canRevealFile, originBadgeTitle, type LibraryEntry } from './musicPlayer';

describe('markerGeometry', () => {
    it('places a point as a percentage of the duration', () => {
        expect(markerGeometry({ t_start: 30, t_end: null }, 120)).toEqual({ left: 25, width: null });
        expect(markerGeometry({ t_start: 0, t_end: null }, 120)).toEqual({ left: 0, width: null });
    });
    it('places a region by its start and length', () => {
        expect(markerGeometry({ t_start: 30, t_end: 60 }, 120)).toEqual({ left: 25, width: 25 });
    });
    it('clamps to the slider', () => {
        expect(markerGeometry({ t_start: 130, t_end: null }, 120)).toEqual({ left: 100, width: null });
        expect(markerGeometry({ t_start: -5, t_end: null }, 120)?.left).toBe(0);
        const r = markerGeometry({ t_start: 100, t_end: 200 }, 120)!;
        expect(r.left + (r.width as number)).toBeLessThanOrEqual(100);
    });
    it('has nowhere to place a marker without a duration', () => {
        for (const d of [0, -1, NaN, Infinity, null, undefined]) {
            expect(markerGeometry({ t_start: 5, t_end: null }, d as number)).toBeNull();
        }
    });
});

describe('labels', () => {
    const fmt = (t: number) => `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, '0')}`;
    it('shows a point and a region', () => {
        expect(commentTimeLabel({ t_start: 12, t_end: null }, fmt)).toBe('0:12');
        expect(commentTimeLabel({ t_start: 12, t_end: 78 }, fmt)).toBe('0:12\u20131:18');
    });
    it('writes seconds with at most two decimals', () => {
        expect(secondsText(12.3456)).toBe('12.35');
        expect(secondsText(4)).toBe('4');
        expect(secondsText(null)).toBe('');
        expect(secondsText(-1)).toBe('');
    });
    it('lists Note as the only supported action', () => {
        expect(COMMENT_ACTIONS.filter(a => a.supported).map(a => a.value)).toEqual(['note']);
        expect(COMMENT_ACTIONS.length).toBeGreaterThan(1);
    });
});

describe('validateCommentForm', () => {
    const ok = { start: '12.5', end: '', text: ' nice ' };
    it('accepts a point and trims the text', () => {
        expect(validateCommentForm(ok, 100)).toEqual({ error: null, value: { start_s: 12.5, end_s: null, text: 'nice' } });
    });
    it('accepts a region, and a decimal comma', () => {
        expect(validateCommentForm({ start: '1,5', end: '4', text: 'x' }, 100).value).toEqual({ start_s: 1.5, end_s: 4, text: 'x' });
    });
    it('needs a start number >= 0', () => {
        for (const start of ['', 'abc', '-1', '1e3']) expect(validateCommentForm({ ...ok, start }, 100).error).toMatch(/Start/);
    });
    it('keeps start and end within a known duration, and lifts that bound for an unknown one', () => {
        expect(validateCommentForm({ ...ok, start: '101' }, 100).error).toMatch(/past the end/);
        expect(validateCommentForm({ ...ok, start: '100' }, 100).error).toBeNull();
        expect(validateCommentForm({ ...ok, start: '10', end: '101' }, 100).error).toMatch(/End is past/);
        expect(validateCommentForm({ ...ok, start: '5000' }, 0).error).toBeNull();
        expect(validateCommentForm({ ...ok, start: '5000' }, null).error).toBeNull();
    });
    it('needs End greater than Start', () => {
        expect(validateCommentForm({ ...ok, start: '10', end: '10' }, 100).error).toMatch(/greater/);
        expect(validateCommentForm({ ...ok, start: '10', end: '5' }, 100).error).toMatch(/greater/);
        expect(validateCommentForm({ ...ok, end: 'x' }, 100).error).toMatch(/End/);
    });
    it('needs text within the limit', () => {
        expect(validateCommentForm({ ...ok, text: '   ' }, 100).error).toMatch(/required/);
        expect(validateCommentForm({ ...ok, text: 'x'.repeat(COMMENT_MAX_CHARS + 1) }, 100).error).toMatch(/limit/);
        expect(validateCommentForm({ ...ok, text: 'x'.repeat(COMMENT_MAX_CHARS) }, 100).error).toBeNull();
    });
});

describe('isCommentShortcut', () => {
    const press = (key: string, extra: Record<string, boolean> = {}) => ({ key, ...extra });
    it('fires on a bare C from a non-field element', () => {
        expect(isCommentShortcut(press('c'), { tagName: 'BUTTON' })).toBe(true);
        expect(isCommentShortcut(press('C'), { tagName: 'DIV' })).toBe(true);
    });
    it('does not fire inside a text field, a select or an editable element', () => {
        for (const tagName of ['INPUT', 'TEXTAREA', 'SELECT', 'input']) {
            expect(isCommentShortcut(press('c'), { tagName })).toBe(false);
        }
        expect(isCommentShortcut(press('c'), { tagName: 'DIV', isContentEditable: true })).toBe(false);
    });
    it('does not fire with a modifier (Ctrl+C is copy) or on another key', () => {
        expect(isCommentShortcut(press('c', { ctrlKey: true }), { tagName: 'DIV' })).toBe(false);
        expect(isCommentShortcut(press('c', { metaKey: true }), { tagName: 'DIV' })).toBe(false);
        expect(isCommentShortcut(press('c', { altKey: true }), { tagName: 'DIV' })).toBe(false);
        expect(isCommentShortcut(press('c', { shiftKey: true }), { tagName: 'DIV' })).toBe(false);
        expect(isCommentShortcut(press('n'), { tagName: 'DIV' })).toBe(false);
    });
});

describe('the origin badge opens the folder only for a file we can reach', () => {
    const base: LibraryEntry = {
        id: 'trk_0123456789abcdef', title: 't', duration_s: 1, origin: 'folder', path: 'C:\Music\a.mp3',
        source: { kind: 'local', name: 'a.mp3', uri: 'file:///C:/Music/a.mp3' },
    };
    it('is clickable for a present local file', () => {
        expect(canRevealFile(base)).toBe(true);
        expect(originBadgeTitle(base)).toBe('Show in folder');
        expect(canRevealFile({ ...base, origin: 'generated', source: { ...base.source, kind: 'generated' } })).toBe(true);
    });
    it('is not for a missing entry, a web track, or an entry without a path', () => {
        expect(canRevealFile({ ...base, missing: true })).toBe(false);
        expect(canRevealFile({ ...base, origin: 'web', source: { ...base.source, kind: 'web' }, path: null })).toBe(false);
        expect(canRevealFile({ ...base, origin: 'web' })).toBe(false);
        expect(canRevealFile({ ...base, path: null })).toBe(false);
        expect(canRevealFile({ ...base, path: '' })).toBe(false);
        expect(canRevealFile(null)).toBe(false);
        expect(originBadgeTitle({ ...base, missing: true })).toBe('folder');
    });
});
