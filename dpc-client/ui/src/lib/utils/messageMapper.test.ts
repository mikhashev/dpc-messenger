/**
 * An undated record used to be stamped with the clock at load time.
 *
 * `Date.now() - (totalCount - index) * 1000` put it a few seconds in the past,
 * which is later than every message in an old history — so it sorted to the
 * bottom, and to a different place on every reload. Two file notes written
 * without a timestamp (2026-08-07) therefore appeared as "sent a moment ago"
 * days after the fact, and jumped again on every sync once the list started
 * reloading (`cb5dee81`).
 *
 * The backend no longer writes such records. These cover the ones already on
 * disk, which cannot be dated from anything but their neighbours: a record
 * arrives in index order, so the message before it is the closest truth
 * available, and using it keeps the row where it belongs instead of at the end.
 */

import { describe, it, expect } from 'vitest';
import { mapBackendMessage } from './messageMapper';

const T1 = '2026-08-05T10:00:00.000Z';
const T2 = '2026-08-05T11:00:00.000Z';

describe('mapBackendMessage timestamps', () => {
    it('uses the timestamp the message carries', () => {
        const m = mapBackendMessage({ id: 'a', content: 'hi', timestamp: T1 });
        expect(m.timestamp).toBe(new Date(T1).getTime());
    });

    it('gives an undated message the time of the one before it', () => {
        const previous = new Date(T1).getTime();
        const m = mapBackendMessage({ id: 'b', content: 'Received file: x' }, { previousTimestamp: previous });
        expect(m.timestamp).toBe(previous);
    });

    it('keeps an undated message out of the future', () => {
        const previous = new Date(T1).getTime();
        const m = mapBackendMessage({ id: 'b', content: 'note' }, { previousTimestamp: previous });
        expect(m.timestamp).toBeLessThan(Date.now());
    });

    it('does not reorder a run of undated messages', () => {
        // Three notes in a row, all undated: they must stay in the order they
        // were stored rather than fan out around the current clock.
        const first = new Date(T1).getTime();
        const a = mapBackendMessage({ id: 'a', content: '1' }, { previousTimestamp: first });
        const b = mapBackendMessage({ id: 'b', content: '2' }, { previousTimestamp: a.timestamp });
        const c = mapBackendMessage({ id: 'c', content: '3' }, { previousTimestamp: b.timestamp });
        expect([a.timestamp, b.timestamp, c.timestamp]).toEqual([first, first, first]);
    });

    it('falls back to the clock only when there is nothing before it', () => {
        // The very first message of a conversation, undated: there is no
        // neighbour to borrow from, and a stable order is impossible anyway.
        const now = Date.now();
        const m = mapBackendMessage({ id: 'a', content: 'note' });
        expect(Math.abs(m.timestamp - now)).toBeLessThan(10_000);
    });

    it('an undated note sorts before a later real message', () => {
        // The defect in one line: the note belongs between T1 and T2, and used
        // to land after both.
        const t1 = new Date(T1).getTime();
        const note = mapBackendMessage({ id: 'n', content: 'Received file' }, { previousTimestamp: t1 });
        const later = mapBackendMessage({ id: 'l', content: 'after', timestamp: T2 });
        expect(note.timestamp).toBeLessThan(later.timestamp);
    });
});
