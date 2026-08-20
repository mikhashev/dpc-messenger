/**
 * The agent header used to answer "how fast" and never "how full".
 *
 * The number that decides whether the next round is refused — the round's real
 * input against the model window — existed only as a DEBUG line in the backend
 * log, where nobody watching an agent work can see it. These cover the two
 * decisions that shape how it is now shown, because both are the kind that a
 * later tidy-up would silently reverse:
 *
 * - the denominator is the RAW window, not the window minus the reserve, so
 *   the strip matches the provider config; the reserve is a colour instead;
 * - the pair is absent, not zero, when the round reported no size — a "0 / N"
 *   would read as an empty context, which is the opposite of unknown.
 */

import { describe, it, expect } from 'vitest';
import {
    occupancyFromSpeed,
    occupancyLabel,
    occupancyTitle,
    formatTokens,
} from './contextOccupancy';

// A window of 262 144 with the agent's 16 384-token round reserve.
const WINDOW = 262144;
const RESERVE = 16384;

const speed = (over: Record<string, any> = {}) => ({
    alias: 'local',
    model: 'qwen 3.8 27b',
    round: 7,
    prefill_tok_s: 845,
    decode_tok_s: 54,
    context_used: 107431,
    context_window: WINDOW,
    context_reserve: RESERVE,
    ...over,
});

describe('what the strip says', () => {
    it('carries the round input against the raw window', () => {
        const occ = occupancyFromSpeed(speed())!;
        expect(occ.used).toBe(107431);
        expect(occ.window).toBe(WINDOW);
        expect(occ.pct).toBe(41);
        expect(occ.free).toBe(WINDOW - 107431);
    });

    it('renders the pair the way the request asked for it', () => {
        expect(occupancyLabel(occupancyFromSpeed(speed())!)).toBe(
            'context: 107,431 / 262,144 (41%)',
        );
    });

    it('separates thousands so a six-digit count is readable at a glance', () => {
        expect(formatTokens(949386)).toBe('949,386');
        expect(formatTokens(3030)).toBe('3,030');
    });
});

describe('the denominator is the window, and the reserve is a colour', () => {
    it('does not subtract the reserve from the window', () => {
        const occ = occupancyFromSpeed(speed({ context_used: WINDOW - RESERVE }))!;
        // Were the reserve folded into the denominator this would read 100%.
        expect(occ.window).toBe(WINDOW);
        expect(occ.pct).toBe(94);
    });

    it('warns above the backend own >80% line', () => {
        const occ = occupancyFromSpeed(speed({ context_used: Math.round(WINDOW * 0.85) }))!;
        expect(occ.warn).toBe(true);
        expect(occ.blocked).toBe(false);
    });

    it('stays quiet below it', () => {
        const occ = occupancyFromSpeed(speed({ context_used: Math.round(WINDOW * 0.5) }))!;
        expect(occ.warn).toBe(false);
        expect(occ.blocked).toBe(false);
    });

    it('goes blocked exactly where the round guard refuses the call', () => {
        const justInside = occupancyFromSpeed(speed({ context_used: WINDOW - RESERVE }))!;
        const justOutside = occupancyFromSpeed(speed({ context_used: WINDOW - RESERVE + 1 }))!;
        expect(justInside.blocked).toBe(false);
        expect(justOutside.blocked).toBe(true);
        // Blocked outranks the warning: one state, not two colours at once.
        expect(justOutside.warn).toBe(false);
    });

    it('cannot claim a block when no reserve travelled with the pair', () => {
        const occ = occupancyFromSpeed(speed({ context_used: WINDOW - 1, context_reserve: undefined }))!;
        expect(occ.reserve).toBeNull();
        expect(occ.blocked).toBe(false);
    });
});

describe('absent is absent', () => {
    it('shows nothing when the payload carries no window', () => {
        expect(occupancyFromSpeed(speed({ context_window: undefined }))).toBeNull();
    });

    it('shows nothing when the round reported no input size', () => {
        // The silent-drop case: prompt_tokens 0. "0 / 262,144" would read as an
        // empty context, which is the opposite of what happened.
        expect(occupancyFromSpeed(speed({ context_used: 0 }))).toBeNull();
    });

    it('shows nothing for a speed-only payload, and nothing for no payload', () => {
        expect(occupancyFromSpeed({ prefill_tok_s: 845, decode_tok_s: 54 })).toBeNull();
        expect(occupancyFromSpeed(null)).toBeNull();
        expect(occupancyFromSpeed(undefined)).toBeNull();
    });
});

describe('the tooltip says which moment the number describes', () => {
    it('names the round and the headroom left', () => {
        const title = occupancyTitle(occupancyFromSpeed(speed())!, { round: 7 });
        expect(title).toContain('at round 7');
        expect(title).toContain('154,713 free');
        expect(title).toContain('refused below 16,384 free');
    });

    it('says the live number is the moment that round was sent, not now', () => {
        const title = occupancyTitle(occupancyFromSpeed(speed())!, { round: 7 });
        expect(title).toContain('not now');
    });

    it('says the finished number is a peak rather than an average', () => {
        // The finished header carries medians for speed; occupancy grows through
        // the turn, so its summary is the last round, and must not read as one.
        const title = occupancyTitle(occupancyFromSpeed(speed())!, { round: 11, median: true });
        expect(title).toContain('peak, not an average');
        expect(title).not.toContain('median');
    });

    it('omits the limit sentence when no reserve is known', () => {
        const occ = occupancyFromSpeed(speed({ context_reserve: undefined }))!;
        expect(occupancyTitle(occ, { round: 2 })).not.toContain('refused');
    });
});
