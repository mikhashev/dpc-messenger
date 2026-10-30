import { describe, it, expect } from 'vitest';
import { applyUserMessageConfirmed, liveBubbleId, mergeBackfillWithLive } from './liveMessageIdentity';
import { oldestRecordId, olderThanPage } from './historyPaging';

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

describe('applyUserMessageConfirmed', () => {
    // dpc-client.log, 2026-10-30: the question was the first record of the page
    // after New Session; the bubble, minted a few ms before the record, survived
    // the reload in front of it and was drawn as a second #1.
    const minted = { id: 'minted-67ebe49d', sender: 'user', text: 'what now?', timestamp: 1000 };
    const placeholder = { id: 'ph', sender: 'ai', text: 'Thinking...', timestamp: 1001, commandId: 'cmd-1' };
    const confirmed = { msg_index: 1, message_id: 'stored-0e0a559b' };
    const stored = { id: 'stored-0e0a559b', sender: 'user', text: 'what now?', timestamp: 1003, msg_index: 1 };
    const answer = { id: 'stored-answer', sender: 'agent_x', text: 'this', timestamp: 2000, msg_index: 2 };

    it('gives the bubble the record id together with its index', () => {
        const after = applyUserMessageConfirmed([minted, placeholder], confirmed);
        expect(after[0]).toEqual({ ...minted, id: 'stored-0e0a559b', msg_index: 1 });
        expect(after[1]).toBe(placeholder);
    });

    it('lets a reload of the page that starts with the question draw it once', () => {
        const live = applyUserMessageConfirmed([minted, placeholder], confirmed);
        const page = [stored, answer];

        expect(olderThanPage(live, page)).toEqual([]);
        const unconfirmedOnly = applyUserMessageConfirmed([minted, placeholder], { msg_index: 1 });
        expect(olderThanPage(unconfirmedOnly, page)).toHaveLength(1); // the defect: the bubble survives
    });

    it('makes the older-page cursor the stored id, not the minted one', () => {
        const live = applyUserMessageConfirmed([minted, placeholder], confirmed);
        expect(oldestRecordId(live)).toBe('stored-0e0a559b');
    });

    it('drops the bubble when a reload already brought the record in, leaving one entry with that id', () => {
        // olderThanPage kept the bubble right in front of the page's first record
        const reloaded = [minted, stored, answer];
        const after = applyUserMessageConfirmed(reloaded, confirmed);

        expect(after.filter((m) => m.id === 'stored-0e0a559b')).toHaveLength(1);
        expect(after).toEqual([stored, answer]);
    });

    it('does not number an older bubble when the last one is already numbered', () => {
        // a confirmation applied before, seen again (a remount re-runs the effect)
        const olderUnconfirmed = { id: 'failed-send', sender: 'user', text: 'lost', timestamp: 10 };
        const error = { id: 'err', sender: 'ai', text: 'failed', timestamp: 11, isError: true };
        const list = [olderUnconfirmed, error, { ...minted, id: 'stored-0e0a559b', msg_index: 1 }, answer];

        expect(applyUserMessageConfirmed(list, confirmed)).toBe(list);
        const later = { msg_index: 3, message_id: 'stored-later' };
        const withLaterNumbered = [olderUnconfirmed, error, { ...minted, id: 'q3', msg_index: 3 }];
        expect(applyUserMessageConfirmed(withLaterNumbered, later)).toBe(withLaterNumbered);
    });

    it('still numbers the bubble when an older backend sends no id', () => {
        const after = applyUserMessageConfirmed([minted, placeholder], { msg_index: 4 });
        expect(after[0]).toEqual({ ...minted, msg_index: 4 });
    });

    it('returns the list itself when there is nothing to confirm', () => {
        const list = [minted, placeholder];
        expect(applyUserMessageConfirmed(list, null)).toBe(list);
        expect(applyUserMessageConfirmed(list, { msg_index: 0, message_id: 'x' })).toBe(list);
        expect(applyUserMessageConfirmed([answer], confirmed)).toEqual([answer]);
    });
});

describe('applyUserMessageConfirmed with two sends before the first confirmation', () => {
    // Both questions are on screen before either record is confirmed; the
    // last-unconfirmed rule would hand the first record to the second bubble.
    const first = { id: 'minted-a', sender: 'user', text: 'one', timestamp: 1000 };
    const second = { id: 'minted-b', sender: 'user', text: 'two', timestamp: 1100 };
    const confirmFirst = { msg_index: 1, message_id: 'stored-a', command_id: 'cmd-a' };
    const confirmSecond = { msg_index: 2, message_id: 'stored-b', command_id: 'cmd-b' };
    const sent = () => new Map<string, string | null>([['minted-a', 'cmd-a'], ['minted-b', 'cmd-b']]);

    it('gives each bubble its own record when the confirmations arrive in order', () => {
        const unconfirmed = sent();
        let list = applyUserMessageConfirmed([first, second], confirmFirst, unconfirmed);
        list = applyUserMessageConfirmed(list, confirmSecond, unconfirmed);

        expect(list).toEqual([
            { ...first, id: 'stored-a', msg_index: 1 },
            { ...second, id: 'stored-b', msg_index: 2 },
        ]);
        expect(unconfirmed.size).toBe(0);
    });

    it('gives each bubble its own record when the second confirmation arrives first', () => {
        const unconfirmed = sent();
        let list = applyUserMessageConfirmed([first, second], confirmSecond, unconfirmed);
        list = applyUserMessageConfirmed(list, confirmFirst, unconfirmed);

        expect(list).toEqual([
            { ...first, id: 'stored-a', msg_index: 1 },
            { ...second, id: 'stored-b', msg_index: 2 },
        ]);
    });

    it('stamps the last unconfirmed bubble when the confirmation carries no command id', () => {
        // send_image sends none
        const unconfirmed = new Map<string, string | null>([['minted-b', null]]);
        const after = applyUserMessageConfirmed([first, second], { msg_index: 2, message_id: 'stored-b' }, unconfirmed);

        expect(after).toEqual([first, { ...second, id: 'stored-b', msg_index: 2 }]);
        expect(unconfirmed.has('minted-b')).toBe(false);
    });

    it('gives an image confirmation to the image bubble, not to a question typed after it', () => {
        const image = { id: 'minted-img', sender: 'user', text: '[Image]', timestamp: 1000 };
        const unconfirmed = new Map<string, string | null>([['minted-img', null], ['minted-b', 'cmd-b']]);
        let list = applyUserMessageConfirmed([image, second], { msg_index: 1, message_id: 'stored-img' }, unconfirmed);
        list = applyUserMessageConfirmed(list, confirmSecond, unconfirmed);

        expect(list).toEqual([
            { ...image, id: 'stored-img', msg_index: 1 },
            { ...second, id: 'stored-b', msg_index: 2 },
        ]);
        expect(unconfirmed.size).toBe(0);
    });

    it('stamps the last unconfirmed bubble when the command id is not on record', () => {
        const after = applyUserMessageConfirmed([first, second], { ...confirmSecond, command_id: 'cmd-unknown' }, new Map());
        expect(after).toEqual([first, { ...second, id: 'stored-b', msg_index: 2 }]);
    });

    it('still drops the bubble when a reload already brought its record', () => {
        const unconfirmed = sent();
        const storedA = { id: 'stored-a', sender: 'user', text: 'one', timestamp: 1003, msg_index: 1 };
        const after = applyUserMessageConfirmed([first, storedA, second], confirmFirst, unconfirmed);

        expect(after).toEqual([storedA, second]);
        expect(unconfirmed.has('minted-a')).toBe(false);
        expect(unconfirmed.has('minted-b')).toBe(true);
    });

    it('keeps a bubble awaiting confirmation off the older-page cursor until it is confirmed', () => {
        const unconfirmed = sent();
        const list = [first, second];
        expect(oldestRecordId(list, unconfirmed)).toBeNull();

        const confirmedFirst = applyUserMessageConfirmed(list, confirmFirst, unconfirmed);
        expect(oldestRecordId(confirmedFirst, unconfirmed)).toBe('stored-a');
    });
});
