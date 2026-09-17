import { describe, it, expect } from 'vitest';
import { voteStatusAppliesTo } from './voteStatusIdentity';

const OPEN = { proposal_id: 'proposal-02972996' };
const OTHER = { proposal_id: 'proposal-35786746' };

describe('voteStatusAppliesTo', () => {
    it('lets a status about the open proposal through', () => {
        expect(voteStatusAppliesTo(OPEN, OPEN)).toBe(true);
    });

    it('refuses one raised for a different proposal', () => {
        expect(voteStatusAppliesTo(OTHER, OPEN)).toBe(false);
    });

    it('lets an unidentified status through rather than losing it', () => {
        expect(voteStatusAppliesTo({}, OPEN)).toBe(true);
        expect(voteStatusAppliesTo({ proposal_id: null }, OPEN)).toBe(true);
    });

    it('has nothing to contradict when no dialog is open', () => {
        expect(voteStatusAppliesTo(OTHER, null)).toBe(true);
        expect(voteStatusAppliesTo(OTHER, {})).toBe(true);
    });
});
