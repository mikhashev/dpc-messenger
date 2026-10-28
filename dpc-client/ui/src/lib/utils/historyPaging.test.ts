import { describe, it, expect } from 'vitest';
import { get } from 'svelte/store';
import {
    HISTORY_PAGE_LIMIT,
    historyPagers,
    setPager,
    pageFacts,
    oldestRecordId,
    prependOlderPage,
    olderThanPage,
    shouldLoadOlder,
    pickAnchorIndex,
    scrollTopKeepingAnchor,
    readerIsAtBottom,
    boundaryLineText,
    type HistoryPager,
} from './historyPaging';

const m = (id: string, timestamp: number, extra: Record<string, unknown> = {}) => ({ id, timestamp, ...extra });
const open: HistoryPager = { hasOlder: true, boundary: null, stopped: false, loading: false };

describe('the page size', () => {
    it('is the backend default, so every caller asks for the same page', () => {
        expect(HISTORY_PAGE_LIMIT).toBe(100);
    });
});

describe('pageFacts', () => {
    it('reads has_older and the boundary', () => {
        expect(pageFacts({ has_older: true, live_history_boundary: '2026-10-27T17:31:25+00:00' }))
            .toEqual({ hasOlder: true, boundary: '2026-10-27T17:31:25+00:00' });
    });

    it('treats an older backend that sends neither as "nothing older"', () => {
        expect(pageFacts({ messages: [] })).toEqual({ hasOlder: false, boundary: null });
        expect(pageFacts(undefined)).toEqual({ hasOlder: false, boundary: null });
        expect(pageFacts({ has_older: 'yes', live_history_boundary: '' })).toEqual({ hasOlder: false, boundary: null });
    });
});

describe('setPager', () => {
    it('merges into the chat\'s state and leaves other chats alone', () => {
        setPager('chat-a', { hasOlder: true });
        setPager('chat-b', { boundary: 'x' });
        setPager('chat-a', { loading: true });
        const map = get(historyPagers);
        expect(map.get('chat-a')).toEqual({ hasOlder: true, boundary: null, stopped: false, loading: true });
        expect(map.get('chat-b')?.boundary).toBe('x');
    });
});

describe('oldestRecordId', () => {
    it('is the first stored message, skipping placeholders', () => {
        const list = [m('', 1), m('p', 2, { commandId: 'c1' }), m('q', 3, { pending: true }), m('r1', 4), m('r2', 5)];
        expect(oldestRecordId(list)).toBe('r1');
    });

    it('is null for an empty chat', () => {
        expect(oldestRecordId([])).toBeNull();
    });
});

describe('prependOlderPage', () => {
    it('puts the older page in front, in its own order', () => {
        const { merged, added } = prependOlderPage([m('c', 3), m('d', 4)], [m('a', 1), m('b', 2)]);
        expect(merged.map((x) => x.id)).toEqual(['a', 'b', 'c', 'd']);
        expect(added).toBe(2);
    });

    it('never draws a message twice', () => {
        const { merged, added } = prependOlderPage([m('b', 2), m('c', 3)], [m('a', 1), m('b', 2)]);
        expect(merged.map((x) => x.id)).toEqual(['a', 'b', 'c']);
        expect(added).toBe(1);
    });

    it('returns the same list when the page brings nothing new', () => {
        const existing = [m('a', 1)];
        const { merged, added } = prependOlderPage(existing, [m('a', 1)]);
        expect(merged).toBe(existing);
        expect(added).toBe(0);
    });
});

describe('olderThanPage', () => {
    it('keeps what a reload of the newest page does not cover', () => {
        const existing = [m('a', 1), m('b', 2), m('c', 3), m('d', 4)];
        const page = [m('c', 3), m('d', 4), m('e', 5)];
        expect(olderThanPage(existing, page).map((x) => x.id)).toEqual(['a', 'b']);
    });

    it('drops placeholders and anything the page carries', () => {
        const existing = [m('a', 1), m('x', 1, { commandId: 'c' }), m('c', 2)];
        const page = [m('c', 2)];
        expect(olderThanPage(existing, page).map((x) => x.id)).toEqual(['a']);
    });

    it('keeps nothing when the page is the whole history or empty', () => {
        const existing = [m('a', 1), m('b', 2)];
        expect(olderThanPage(existing, [m('a', 1), m('b', 2)])).toEqual([]);
        expect(olderThanPage(existing, [])).toEqual([]);
    });
});

describe('shouldLoadOlder', () => {
    const base = { pager: open, scrollTop: 10, movedUp: true, userScrolled: true };

    it('loads when the reader scrolls up to the top and more is left', () => {
        expect(shouldLoadOlder(base)).toBe(true);
    });

    it('does not load on opening the chat, before the reader touched it', () => {
        expect(shouldLoadOlder({ ...base, userScrolled: false })).toBe(false);
    });

    it('does not load on a scroll down, or away from the top', () => {
        expect(shouldLoadOlder({ ...base, movedUp: false })).toBe(false);
        expect(shouldLoadOlder({ ...base, scrollTop: 5000 })).toBe(false);
    });

    it('holds one request per chat, and stops on an error or at the end', () => {
        expect(shouldLoadOlder({ ...base, pager: { ...open, loading: true } })).toBe(false);
        expect(shouldLoadOlder({ ...base, pager: { ...open, stopped: true } })).toBe(false);
        expect(shouldLoadOlder({ ...base, pager: { ...open, hasOlder: false } })).toBe(false);
        expect(shouldLoadOlder({ ...base, pager: undefined })).toBe(false);
    });
});

describe('the anchor', () => {
    it('is the first message still showing below the top edge', () => {
        const rects = [{ top: -300, bottom: -100 }, { top: -100, bottom: 40 }, { top: 40, bottom: 200 }];
        expect(pickAnchorIndex(rects, 0)).toBe(1);
        expect(pickAnchorIndex([], 0)).toBe(-1);
    });

    it('stays where it was after older messages are inserted above it', () => {
        // Anchor 60 px above the top edge; 1200 px of older messages pushed it down.
        const before = -60;
        const after = before + 1200;
        expect(scrollTopKeepingAnchor(15, before, after)).toBe(1215);
    });

    it('needs no correction when the webview already kept it in place', () => {
        expect(scrollTopKeepingAnchor(1215, -60, -60)).toBe(1215);
    });
});

describe('readerIsAtBottom', () => {
    it('is true near the bottom and false when scrolled up', () => {
        expect(readerIsAtBottom({ scrollHeight: 5000, scrollTop: 4400, clientHeight: 500 })).toBe(true);
        expect(readerIsAtBottom({ scrollHeight: 5000, scrollTop: 1000, clientHeight: 500 })).toBe(false);
        expect(readerIsAtBottom(null)).toBe(true);
    });
});

describe('boundaryLineText', () => {
    it('says when the session started and where the rest is', () => {
        expect(boundaryLineText('2026-10-27T17:31:25+00:00', (d) => d.toISOString()))
            .toBe('Session started 2026-10-27T17:31:25.000Z — earlier messages are in the archive');
    });

    it('is nothing for a chat never reset, or an unreadable moment', () => {
        expect(boundaryLineText(null)).toBeNull();
        expect(boundaryLineText('not a date')).toBeNull();
    });
});
