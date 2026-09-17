import { describe, it, expect } from 'vitest';
import { dedupeMessagesById } from './messageMapper';

/**
 * ChatMessageList.svelte keys its {#each} on `msg.id`. Svelte 5.56.4 throws
 * `each_key_duplicate` on a repeated key in production too, and the chat
 * panel has no <svelte:boundary>, so one repeated id in a history batch
 * crashes the whole panel. HistorySyncPanel builds both the 1:1 restore and
 * the group sync batch from the backend and used to trust it was clean; a
 * Linux node's history.json held 38 rows for 21 unique ids from an old
 * backend dedup bug, which is the shape this guards against.
 */

describe('dedupeMessagesById', () => {
    it('collapses a repeated id to its first occurrence, keeping order', () => {
        const a = { id: 'x', text: 'first' };
        const b = { id: 'y', text: 'middle' };
        const aAgain = { id: 'x', text: 'first, resent' };
        const result = dedupeMessagesById([a, b, aAgain]);
        expect(result.kept).toEqual([a, b]);
    });

    it('reports how many rows were dropped', () => {
        const result = dedupeMessagesById([
            { id: 'x', text: '1' },
            { id: 'x', text: '2' },
            { id: 'x', text: '3' },
            { id: 'y', text: '4' },
        ]);
        expect(result.droppedCount).toBe(2);
        expect(result.kept).toHaveLength(2);
    });

    it('keeps id-less messages instead of collapsing them together', () => {
        const noId1 = { id: undefined, text: 'a' };
        const noId2 = { id: undefined, text: 'b' };
        const result = dedupeMessagesById([noId1, noId2]);
        expect(result.kept).toEqual([noId1, noId2]);
        expect(result.droppedCount).toBe(0);
    });

    it('leaves input with no duplicates unchanged', () => {
        const messages = [{ id: 'a' }, { id: 'b' }, { id: 'c' }];
        const result = dedupeMessagesById(messages);
        expect(result.kept).toEqual(messages);
        expect(result.droppedCount).toBe(0);
    });

    it('treats an empty-string id like no id at all', () => {
        const first = { id: '', text: 'a' };
        const second = { id: '', text: 'b' };
        const result = dedupeMessagesById([first, second]);
        expect(result.kept).toEqual([first, second]);
        expect(result.droppedCount).toBe(0);
    });

    it('does not count an identical-content duplicate as a conflict', () => {
        const first = { id: 'x', text: 'same' };
        const second = { id: 'x', text: 'same' };
        const result = dedupeMessagesById([first, second]);
        expect(result.droppedCount).toBe(1);
        expect(result.conflictCount).toBe(0);
        expect(result.kept).toEqual([first]);
    });

    it('counts a same-id different-text row as a conflict and keeps the first', () => {
        const first = { id: 'x', text: 'original' };
        const second = { id: 'x', text: 'resent with different wording' };
        const result = dedupeMessagesById([first, second]);
        expect(result.droppedCount).toBe(1);
        expect(result.conflictCount).toBe(1);
        expect(result.kept).toEqual([first]);
    });

    it('separates true duplicates from conflicts in a mixed batch', () => {
        const kept = { id: 'a', text: 'hello' };
        const trueDup = { id: 'a', text: 'hello' };
        const conflict = { id: 'a', text: 'hello!' };
        const other = { id: 'b', text: 'other' };
        const result = dedupeMessagesById([kept, trueDup, conflict, other]);
        expect(result.droppedCount).toBe(2);
        expect(result.conflictCount).toBe(1);
        expect(result.kept).toEqual([kept, other]);
    });

    it('ignores msg_index when comparing content — only text is a conflict signal', () => {
        const first = { id: 'x', text: 'same', msg_index: 5 };
        const second = { id: 'x', text: 'same', msg_index: 9 };
        const result = dedupeMessagesById([first, second]);
        expect(result.conflictCount).toBe(0);
    });
});
