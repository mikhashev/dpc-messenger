/**
 * The strip says why the context number fell: a short part after the context
 * one, with the whole reason in its tooltip, and nothing before the first cut.
 */

import { describe, it, expect } from 'vitest';
import { cutFromSpeed, cutLabel, cutTitle } from './contextCut';

const speed = (cut: Record<string, any> | undefined) => ({ round: 47, last_context_cut: cut });

describe('the last context cut', () => {
    it('reads as round and freed thousands', () => {
        const cut = cutFromSpeed(speed({ round: 46, reason: 'rounds', freed_tokens: 10856, kept_rounds: 6 }))!;
        expect(cutLabel(cut)).toBe('cut r46 −10.9K');
    });

    it('says a deterministic cut shortened the old results', () => {
        for (const reason of ['rounds', 'pressure']) {
            const cut = cutFromSpeed(speed({ round: 46, reason, freed_tokens: 10856, kept_rounds: 6 }))!;
            expect(cutTitle(cut)).toBe(
                'Round 46: old tool results shortened to free 10.9K tokens; the last 6 rounds are kept as they are.',
            );
        }
    });

    it('says a summary was made by the model', () => {
        const cut = cutFromSpeed(speed({ round: 46, reason: 'summary', freed_tokens: 10856, kept_rounds: 6 }))!;
        expect(cutTitle(cut)).toBe(
            'Round 46: old tool results summarized by the model to free 10.9K tokens; the last 6 rounds are kept as they are.',
        );
    });

    it('shows nothing before the first cut or for a malformed one', () => {
        expect(cutFromSpeed(speed(undefined))).toBeNull();
        expect(cutFromSpeed(speed({ round: 46, reason: 'rounds' }))).toBeNull();
        expect(cutFromSpeed(null)).toBeNull();
    });
});
