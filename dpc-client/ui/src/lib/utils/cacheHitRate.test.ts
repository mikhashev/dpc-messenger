/**
 * The header shows `cache hit rate: N%`, and the two ways it can lie are the ones held
 * here: a made-up number where nothing was reported, and a median where the
 * turn needs a weighted sum.
 */

import { describe, it, expect } from 'vitest';
import { cacheFromSpeed, cacheLabel, cacheTitle } from './cacheHitRate';

describe('one round', () => {
    it('is cached over prompt, in integer percent', () => {
        const rate = cacheFromSpeed({ cached_tokens: 111593, prompt_tokens: 113343 })!;
        expect(rate.pct).toBe(98);
        expect(cacheLabel(rate)).toBe('cache hit rate: 98%');
    });

    it('shows a real zero as 0%', () => {
        const rate = cacheFromSpeed({ cached_tokens: 0, prompt_tokens: 800 })!;
        expect(cacheLabel(rate)).toBe('cache hit rate: 0%');
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
        expect(rate.pct).toBe(80);
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
