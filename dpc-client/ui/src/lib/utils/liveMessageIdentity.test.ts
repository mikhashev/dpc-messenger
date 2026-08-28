import { describe, it, expect } from 'vitest';
import { liveBubbleId, mergeBackfillWithLive } from './liveMessageIdentity';

describe('liveBubbleId', () => {
    it('keeps the id the backend already gave the message', () => {
        expect(liveBubbleId({ message_id: 'a1b2c3d4' })).toBe('a1b2c3d4');
    });

    it('mints one only when the event carries none', () => {
        expect(liveBubbleId({}, () => 'minted')).toBe('minted');
        expect(liveBubbleId({ message_id: '' }, () => 'minted')).toBe('minted');
        expect(liveBubbleId(null, () => 'minted')).toBe('minted');
    });

    it('does not spend an id it does not need', () => {
        let calls = 0;
        liveBubbleId({ message_id: 'carried' }, () => { calls += 1; return 'minted'; });
        expect(calls).toBe(0);
    });
});

describe('the join between a live bubble and the stored record', () => {
    // This is the defect itself: the same message, announced over the socket and
    // then read back from disk, must survive the merge once.
    it('draws a message once when the bubble kept the backend id', () => {
        const payload = { message_id: 'same-id', text: 'ppoo' };
        const bubble = { id: liveBubbleId(payload), text: payload.text };
        const stored = [{ id: 'same-id', text: 'ppoo' }];

        expect(mergeBackfillWithLive(stored, [bubble])).toHaveLength(1);
    });

    it('draws it twice when the bubble threw that id away', () => {
        const stored = [{ id: 'same-id', text: 'ppoo' }];
        const bubbleWithRandomId = { id: 'a-fresh-uuid', text: 'ppoo' };

        expect(mergeBackfillWithLive(stored, [bubbleWithRandomId])).toHaveLength(2);
    });

    it('keeps a live message the snapshot does not carry, and keeps it last', () => {
        const stored = [{ id: 'older', text: 'first' }];
        const newer = { id: 'newer', text: 'arrived mid-fetch' };

        expect(mergeBackfillWithLive(stored, [newer])).toEqual([stored[0], newer]);
    });

    it('keeps a live message that has no id at all', () => {
        const stored = [{ id: 'older', text: 'first' }];
        const idless = { text: 'no id' } as { id?: string; text: string };

        expect(mergeBackfillWithLive(stored, [idless])).toHaveLength(2);
    });

    it('returns the snapshot itself when nothing live survives', () => {
        const stored = [{ id: 'x', text: 'one' }];
        expect(mergeBackfillWithLive(stored, [{ id: 'x', text: 'one' }])).toEqual(stored);
    });
});
