import { describe, expect, it } from 'vitest';
import {
    canCutWithShift,
    editActionState,
    editJobLine,
    editLimits,
    editProposal,
    isEditAction,
    regionWords,
} from './musicEdit';

const caps = {
    actions: {
        note: { status: 'supported', reason: 'comments' },
        regenerate_region: { status: 'unsupported', reason: 'needs acestep.cpp: model file(s) missing' },
        fix_seam: { status: 'unsupported', reason: 'needs acestep.cpp: model file(s) missing' },
        remove_region: { status: 'supported', reason: 'no model, no card' },
        extend_ending: { status: 'planned', reason: 'not built' },
    },
    limits: { edit: { max_region_s: 30, cut_with_shift_near_end_s: 10, takes: { regenerate_region: 2, remove_region: 1 } } },
};
const limits = editLimits(caps);

describe('edit actions follow the capability report', () => {
    it('supports what the core says and names the reason otherwise', () => {
        expect(editActionState(caps, 'remove_region')).toEqual({ supported: true, reason: 'no model, no card' });
        const r = editActionState(caps, 'regenerate_region');
        expect(r.supported).toBe(false);
        expect(r.reason).toContain('acestep.cpp');
        expect(editActionState(null, 'remove_region').supported).toBe(false);
        expect(editActionState({ actions: {} }, 'fix_seam').reason).toContain('does not report');
    });
    it('knows the built actions only', () => {
        expect(isEditAction('fix_seam')).toBe(true);
        expect(isEditAction('extend_ending')).toBe(false);
        expect(isEditAction('note')).toBe(false);
    });
    it('falls back to the SPEC limits when the report has none', () => {
        expect(editLimits(null)).toEqual({ max_region_s: 30, cut_with_shift_near_end_s: 10,
            takes: { regenerate_region: 2, fix_seam: 2, remove_region: 1 } });
    });
});

describe('the region is named as the core names it', () => {
    it('writes minutes, seconds and a tenth', () => {
        expect(regionWords('regenerate_region', { t_start: 2, t_end: 4 })).toBe('regenerate 0:02.0-0:04.0');
        expect(regionWords('remove_region', { t_start: 61.34, t_end: 75 })).toBe('remove 1:01.3-1:15.0');
        expect(regionWords('fix_seam', null)).toBe('fix seam');
    });
    it('gives an edit job row its line', () => {
        expect(editJobLine({ action: 'regenerate_region', region: { t_start: 2, t_end: 4 }, params: { takes: 2, seed: 7 } }))
            .toBe('Region edit: regenerate 0:02.0-0:04.0 · 2 takes · seed 7');
        expect(editJobLine({ action: 'remove_region', region: { t_start: 50, t_end: 58 }, params: { takes: 1, cut_with_shift: true } }))
            .toBe('Region edit: remove 0:50.0-0:58.0 · 1 take · cut with shift');
    });
});

describe('the edit form', () => {
    const base = { action: 'regenerate_region', start_s: 10, end_s: 14, text: 'the snare flams' };
    it('makes the proposal with the region and the caption for a repaint', () => {
        const r = editProposal({ ...base, caption: '  synthwave  ' }, 120, limits);
        expect(r.error).toBeNull();
        expect(r.value).toEqual({ action: 'regenerate_region', region: { t_start: 10, t_end: 14 }, prompt: 'the snare flams',
            params: { caption: 'synthwave' } });
    });
    it('needs a region of at most 30 s and a built action', () => {
        expect(editProposal({ ...base, end_s: null }, 120, limits).error).toContain('needs a region');
        expect(editProposal({ ...base, end_s: 45 }, 120, limits).error).toContain('at most 30 s');
        expect(editProposal({ ...base, action: 'note' }, 120, limits).error).toContain('Pick an edit action');
    });
    it('keeps the caption off a removal and allows cut with shift only near the end', () => {
        const rm = { ...base, action: 'remove_region', caption: 'x' };
        expect(editProposal(rm, 120, limits).value?.params).toEqual({});
        expect(editProposal({ ...rm, start_s: 108, end_s: 115, cut_with_shift: true }, 120, limits).value?.params)
            .toEqual({ cut_with_shift: true });
        expect(editProposal({ ...rm, cut_with_shift: true }, 120, limits).error).toContain('within 10 s of the end');
        expect(editProposal({ ...base, start_s: 108, end_s: 115, cut_with_shift: true }, 120, limits).error)
            .toContain('Remove region only');
    });
    it('tells when a cut with shift is possible', () => {
        expect(canCutWithShift(112, 120, 10)).toBe(true);
        expect(canCutWithShift(105, 120, 10)).toBe(false);
        expect(canCutWithShift(112, null, 10)).toBe(false);
    });
});
