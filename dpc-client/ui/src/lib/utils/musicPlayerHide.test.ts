import { describe, it, expect } from 'vitest';
import {
    canDeleteFile,
    hiddenEntries,
    missingEntries,
    playableIds,
    queueDrop,
    type LibraryEntry,
} from './musicPlayer';

const e = (id: string, extra: Partial<LibraryEntry> = {}): LibraryEntry => ({
    id,
    title: id,
    source: { kind: 'local', name: `${id}.wav`, uri: '' },
    duration_s: 1,
    ...extra,
});

describe('hiding and removing tracks', () => {
    it('leaves hidden tracks out of what can be played, and keeps unknown ids', () => {
        const byId = new Map([e('a'), e('b', { hidden: true }), e('c')].map(x => [x.id, x]));
        expect(playableIds(['a', 'b', 'c', 'zzz'], byId)).toEqual(['a', 'c', 'zzz']);
    });

    it('lists hidden and missing entries', () => {
        const all = [e('a'), e('b', { hidden: true }), e('c', { missing: true }), e('d', { hidden: true, missing: true })];
        expect(hiddenEntries(all).map(x => x.id)).toEqual(['b', 'd']);
        expect(missingEntries(all).map(x => x.id)).toEqual(['c', 'd']);
    });

    it('offers Delete file only for a DPC-made file outside the watched folders', () => {
        const gen = { origin: 'generated', path: '/h/music/generated/j/t.wav' };
        expect(canDeleteFile(e('a', gen))).toBe(true);
        expect(canDeleteFile(e('a', { ...gen, missing: true }))).toBe(false);
        expect(canDeleteFile(e('a', { ...gen, folder_id: 'mf_1' }))).toBe(false);
        expect(canDeleteFile(e('a', { origin: 'folder', path: '/m/a.mp3', folder_id: 'mf_1' }))).toBe(false);
        expect(canDeleteFile(null)).toBe(false);
    });

    it('takes tracks out of the queue and moves the index with them', () => {
        const q = { ids: ['a', 'b', 'c', 'd'], index: 2, context: null };
        expect(queueDrop(q, ['a'])).toEqual({ ids: ['b', 'c', 'd'], index: 1, context: null });
        expect(queueDrop(q, ['d'])).toEqual({ ids: ['a', 'b', 'c'], index: 2, context: null });
        expect(queueDrop(q, ['x'])).toBe(q);
    });

    it('points at the next track when the current one is dropped, and empties cleanly', () => {
        const q = { ids: ['a', 'b', 'c'], index: 1, context: null };
        expect(queueDrop(q, ['b'])).toEqual({ ids: ['a', 'c'], index: 1, context: null });
        expect(queueDrop({ ids: ['a', 'b'], index: 1, context: null }, ['b']).index).toBe(0);
        expect(queueDrop(q, ['a', 'b', 'c'])).toEqual({ ids: [], index: -1, context: null });
    });
});
