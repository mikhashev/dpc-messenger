/**
 * «в локальном чате нет static это же не агент» — Mike, 2026-08-24, on a header
 * reading `STATIC ≈42` in a chat with no agent behind it.
 *
 * He was right about the label and the number turned out to be stranger than
 * either of us assumed: it is a remainder of a measurement and an estimate, with
 * a floor at zero that can only ever hide the real quantity, never inflate it
 * beyond the estimator's error.
 */

import { describe, it, expect } from 'vitest';
import { nonDialogTokens, hasComponentBreakdown, nonDialogLabel } from './nonDialogTokens';

describe('the remainder', () => {
    it('is the measured prompt minus the estimated dialogue', () => {
        expect(nonDialogTokens(225, 183)).toBe(42);
    });

    it('reads zero when the estimate overshoots — which is not «nothing»', () => {
        // The bias that never reaches the screen: the instruction really is in
        // the prompt, and this says there is nothing there.
        expect(nonDialogTokens(225, 400)).toBe(0);
    });

    it('is zero before anything has been measured', () => {
        expect(nonDialogTokens(0, 183)).toBe(0);
        expect(nonDialogTokens(-1, 183)).toBe(0);
    });

    it('is the whole prompt when the dialogue is empty', () => {
        expect(nonDialogTokens(225, 0)).toBe(225);
    });
});

describe('whether the parts can be named', () => {
    it('needs a breakdown from the backend', () => {
        expect(hasComponentBreakdown([{ name: 'system', tokens: 40 }])).toBe(true);
    });

    it('and an empty or missing one names nothing', () => {
        expect(hasComponentBreakdown([])).toBe(false);
        expect(hasComponentBreakdown(null)).toBe(false);
        expect(hasComponentBreakdown(undefined)).toBe(false);
    });
});

describe('the label tells the truth about the surface', () => {
    it('an agent chat keeps its own name', () => {
        expect(nonDialogLabel('Ark', null)).toBe('Agent ctx');
    });

    it('«Static» is earned by a breakdown that can name the parts', () => {
        expect(nonDialogLabel('', [{ name: 'system prompt', tokens: 40 }])).toBe('Static');
    });

    it('and without one the row says it is a remainder', () => {
        // The case on Mike's screen: no agent, no breakdown, `≈42` under a word
        // that promises a system prompt, contexts and tool schemas.
        expect(nonDialogLabel('', null)).toBe('Non-dialog');
        expect(nonDialogLabel(undefined, [])).toBe('Non-dialog');
    });
});
