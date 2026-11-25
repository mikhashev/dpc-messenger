// Follow-up 13, item 9 and the YuE2 card wording (item 4): what the player's debug log says, and
// how the music card reads a cap-based estimate. The helpers are pure; the service only prints them.
import { describe, expect, it } from 'vitest';
import {
    endedReason,
    loopWrapped,
    onTrackEnded,
    playerStateLine,
    stepLogLine,
    stepQueue,
    type QueueState,
} from './musicPlayer';
import { musicJobSentence } from './scheduleApprovalText';

const queue = (index = 2, ids = ['a', 'b', 'c', 'd', 'e', 'f', 'g']): QueueState => ({ ids, index, context: 'pl_1' });
const playable = () => true;

describe('the player state line', () => {
    it('names repeat, shuffle, the queue length and position, the list and the element loop', () => {
        const line = playerStateLine({ repeat: 'one', shuffle: true, queue: queue(), loop: true });
        expect(line).toBe('repeat=one shuffle=on queue=7 pos=3/7 context=pl_1 loop=on');
    });

    it('says "-" for no position and leaves loop out when there is no element yet', () => {
        const line = playerStateLine({ repeat: 'off', shuffle: false, queue: { ids: [], index: -1, context: null } });
        expect(line).toBe('repeat=off shuffle=off queue=0 pos=-/0 context=-');
    });
});

describe('the reason of a track change', () => {
    it('is ended+repeat-one when a track ends under repeat one, and plain ended otherwise', () => {
        expect(endedReason('one')).toBe('ended+repeat-one');
        expect(endedReason('all')).toBe('ended');
        expect(endedReason('off')).toBe('ended');
    });

    it('reads the 2026-11-25 loop: repeat one restarts the same track, with the reason and the state', () => {
        const q = queue();
        const state = { repeat: 'one' as const, shuffle: false, queue: q, loop: false };
        const line = stepLogLine(endedReason('one'), onTrackEnded(q, 'one', playable), state);
        expect(line).toBe('track-change reason=ended+repeat-one from=c -> restart c repeat=one shuffle=off queue=7 pos=3/7 context=pl_1 loop=off');
    });

    it('reads an auto-advance, a user Next and a Prev with the track it moved to', () => {
        const q = queue();
        const state = { repeat: 'all' as const, shuffle: true, queue: q };
        expect(stepLogLine('ended', onTrackEnded(q, 'all', playable), state))
            .toBe('track-change reason=ended from=c -> play d (index 4) repeat=all shuffle=on queue=7 pos=3/7 context=pl_1');
        expect(stepLogLine('next', stepQueue(q, 1, { repeat: 'all', playable }), state)).toContain('reason=next from=c -> play d');
        expect(stepLogLine('prev', stepQueue(q, -1, { repeat: 'all', playable }), state)).toContain('reason=prev from=c -> play b');
    });

    it('names a skipped track and the end of the queue', () => {
        const q = queue(2);
        const state = { repeat: 'off' as const, shuffle: false, queue: q };
        const skipping = stepQueue(q, 1, { repeat: 'off', playable: (id) => id !== 'd' });
        expect(stepLogLine('next', skipping, state)).toContain('play e (index 5) skipped=1');
        const last = queue(6);
        const end = stepQueue(last, 1, { repeat: 'off', auto: true, playable });
        expect(stepLogLine('ended', end, { ...state, queue: last })).toContain('end: End of the queue (repeat is off).');
    });
});

describe('a loop the element makes by itself', () => {
    it('is a time that jumped back by more than a second to the start', () => {
        expect(loopWrapped(183.9, 0.1)).toBe(true);
        expect(loopWrapped(12, 0)).toBe(true);
    });

    it('is not a seek back into the middle, a small step or an unusable time', () => {
        expect(loopWrapped(60, 30)).toBe(false);
        expect(loopWrapped(5, 4.5)).toBe(false);
        expect(loopWrapped(0, 0.3)).toBe(false);
        expect(loopWrapped(Number.NaN, 0)).toBe(false);
    });
});

describe('the music card reads the time of an estimate that is a cap', () => {
    it('says "up to" for the flagged estimate and for yue2', () => {
        expect(musicJobSentence({ about: '', music: { style: 's', eta_seconds: 90, eta_is_upper_bound: true } })).toContain('up to 90 s');
        expect(musicJobSentence({ about: '', music: { style: 's', engine: 'yue2', eta_seconds: 300 } })).toContain('up to 5 min');
    });

    it('keeps "about" for an estimate that is not a cap (ACE-Step wording as it was)', () => {
        const text = musicJobSentence({ about: '', music: { style: 's', engine: 'acestep', eta_seconds: 45 } });
        expect(text).toContain('about 45 s');
        expect(text).not.toContain('up to');
        expect(musicJobSentence({ about: '', music: { style: 's', eta_is_upper_bound: false, eta_seconds: 45 } })).toContain('about 45 s');
    });

    it('shows no time at all when there is no estimate', () => {
        const text = musicJobSentence({ about: '', music: { style: 's', engine: 'acestep' } });
        expect(text).not.toMatch(/about|up to/);
    });
});
