import { describe, it, expect } from 'vitest';
import { secondsLeft } from './retryCountdown';

describe('secondsLeft', () => {
    it('names the whole wait at the moment it is announced', () => {
        expect(secondsLeft(3, 1000, 1000)).toBe(3);
        expect(secondsLeft(192, 1000, 1000)).toBe(192);
    });

    it('counts down as the clock moves', () => {
        expect(secondsLeft(12, 0, 1000)).toBe(11);
        expect(secondsLeft(12, 0, 6000)).toBe(6);
    });

    it('stops at zero rather than going negative', () => {
        // The call after the sleep can take the rest of the budget; the row
        // reads "trying again" from here, and must not count into the minus.
        expect(secondsLeft(3, 0, 4000)).toBe(0);
        expect(secondsLeft(3, 0, 600_000)).toBe(0);
    });

    it('is right after a throttled timer, where a decrement per tick would drift', () => {
        // A backgrounded tab stops firing the interval; the next tick lands late.
        expect(secondsLeft(192, 0, 100_000)).toBe(92);
    });
});
