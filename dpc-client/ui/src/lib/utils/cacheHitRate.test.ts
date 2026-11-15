/**
 * The header shows `avg cache hit rate: N.N%` (the turn's average), and the ways it can
 * lie are the ones held here: a made-up number where nothing was reported, a median
 * where the turn needs a weighted sum, and an "avg" where only one round was sent.
 */

import { describe, it, expect } from 'vitest';
import { cacheFromSpeed, cacheLabel, cacheTitle, cachePart } from './cacheHitRate';

describe('one round', () => {
    it('is cached over prompt, with one decimal', () => {
        const rate = cacheFromSpeed({ cached_tokens: 111593, prompt_tokens: 113343 })!;
        expect(rate.pct).toBe(98.4);
        expect(cacheLabel(rate)).toBe('cache hit rate: 98.4%');
    });

    it('says 99.5% where a whole-percent strip said 100% (the round of 2026-11-15)', () => {
        const rate = cacheFromSpeed({ cached_tokens: 217786 - 989, prompt_tokens: 217786 })!;
        expect(cacheLabel(rate)).toBe('cache hit rate: 99.5%');
    });

    it('says 100.0% only when nothing was read again', () => {
        expect(cacheLabel(cacheFromSpeed({ cached_tokens: 99999, prompt_tokens: 100000 })!))
            .toBe('cache hit rate: 99.9%');
        expect(cacheLabel(cacheFromSpeed({ cached_tokens: 5000, prompt_tokens: 5000 })!))
            .toBe('cache hit rate: 100.0%');
    });

    it('shows a real zero as 0.0%', () => {
        const rate = cacheFromSpeed({ cached_tokens: 0, prompt_tokens: 800 })!;
        expect(cacheLabel(rate)).toBe('cache hit rate: 0.0%');
    });

    it('shows nothing when either number is missing', () => {
        expect(cacheFromSpeed({ prompt_tokens: 800 })).toBeNull();
        expect(cacheFromSpeed({ cached_tokens: 100 })).toBeNull();
        expect(cacheFromSpeed({ cached_tokens: null, prompt_tokens: 800 })).toBeNull();
        expect(cacheFromSpeed(null)).toBeNull();
        expect(cacheFromSpeed(undefined)).toBeNull();
    });

    it('shows nothing for a prompt of 0, rather than a made-up 0% or NaN', () => {
        expect(cacheFromSpeed({ cached_tokens: 0, prompt_tokens: 0 })).toBeNull();
    });

    it('does not believe a count larger than its prompt', () => {
        expect(cacheFromSpeed({ cached_tokens: 1001, prompt_tokens: 1000 })).toBeNull();
    });

    it('says the raw numbers in the tooltip', () => {
        const rate = cacheFromSpeed({ cached_tokens: 111593, prompt_tokens: 113343 })!;
        expect(cacheTitle(rate)).toBe('111,593 of 113,343 prompt tokens reused from the cache');
    });
});

describe('a finished turn', () => {
    // The sums are made by the backend (loop.py speed_summary_of, tested in
    // test_the_finished_strip_is_stored_with_the_message.py); the header divides.
    it('reads the stored sums as the token-weighted rate', () => {
        const rate = cacheFromSpeed({ rounds: 3, cached_tokens: 47700, prompt_tokens: 60000, median: true })!;
        expect(rate.pct).toBe(79.5);
    });

    it('says the rounds in the tooltip', () => {
        const rate = cacheFromSpeed({ cached_tokens: 900, prompt_tokens: 1000 })!;
        expect(cacheTitle(rate, { rounds: 12 })).toBe(
            '900 of 1,000 prompt tokens reused from the cache over 12 rounds',
        );
        expect(cacheTitle(rate, { rounds: 1 })).toContain('over 1 round');
        expect(cacheTitle(rate, { rounds: 1 })).not.toContain('rounds');
    });
});

describe('the header part, the turn average', () => {
    // Round 3 read 989 of 217,786 again; the turn so far read less than that share.
    const live = {
        round: 3, cached_tokens: 217786 - 989, prompt_tokens: 217786,
        speed_summary: { rounds: 3, cached_tokens: 300000, prompt_tokens: 400000, median: true },
    };

    it('live: the label is the running average, not the round', () => {
        const part = cachePart(live)!;
        expect(part.label).toBe('avg cache hit rate: 75.0%');
    });

    it('live: the tooltip has the turn raw numbers and the last round', () => {
        expect(cachePart(live)!.title).toBe(
            '300,000 of 400,000 prompt tokens reused from the cache over 3 rounds; '
            + 'last round 99.5%: 989 of 217,786 read again',
        );
    });

    it('live: a round with no pair still shows the turn average, no last-round clause', () => {
        const part = cachePart({ round: 4, speed_summary: live.speed_summary })!;
        expect(part.label).toBe('avg cache hit rate: 75.0%');
        expect(part.title).toBe('300,000 of 400,000 prompt tokens reused from the cache over 3 rounds');
    });

    it('finished: the stored summary is the average, rounds in the tooltip, no last round', () => {
        const part = cachePart({ rounds: 12, cached_tokens: 900, prompt_tokens: 1000, median: true })!;
        expect(part.label).toBe('avg cache hit rate: 90.0%');
        expect(part.title).toBe('900 of 1,000 prompt tokens reused from the cache over 12 rounds');
    });

    it('live, older backend: the round numbers under the old label, never "avg"', () => {
        const part = cachePart({ round: 2, cached_tokens: 111593, prompt_tokens: 113343 })!;
        expect(part.label).toBe('cache hit rate: 98.4%');
        expect(part.label).not.toContain('avg');
        expect(part.title).toBe('111,593 of 113,343 prompt tokens reused from the cache');
    });

    it('live: a summary with no cache counts does not become an average', () => {
        const part = cachePart({
            cached_tokens: 10, prompt_tokens: 100,
            speed_summary: { rounds: 2, cached_tokens: null, prompt_tokens: null, median: true },
        })!;
        expect(part.label).toBe('cache hit rate: 10.0%');
    });

    it('shows nothing when nothing was stated', () => {
        expect(cachePart(null)).toBeNull();
        expect(cachePart({ round: 1 })).toBeNull();
        expect(cachePart({ rounds: 2, cached_tokens: null, prompt_tokens: null, median: true })).toBeNull();
    });
});
