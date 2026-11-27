import { describe, it, expect } from 'vitest';
import { hfStateOf, HF_STATES, imageMbOr } from './featureKeys';

describe('hfStateOf', () => {
    it('takes the three states run_service knows', () => {
        expect(HF_STATES).toEqual(['auto', 'on', 'off']);
        expect(hfStateOf('on')).toBe('on');
        expect(hfStateOf('off')).toBe('off');
        expect(hfStateOf('auto')).toBe('auto');
        expect(hfStateOf(' ON ')).toBe('on');
    });
    it('answers auto for anything else — a select must always hold one of its options', () => {
        expect(hfStateOf('maybe')).toBe('auto');
        expect(hfStateOf('')).toBe('auto');
        expect(hfStateOf(1)).toBe('auto');
        expect(hfStateOf(null)).toBe('auto');
        expect(hfStateOf(undefined)).toBe('auto');
    });
});

describe('imageMbOr', () => {
    it('takes a whole number of MB above 0', () => {
        expect(imageMbOr(12, 5)).toBe(12);
        expect(imageMbOr('8', 5)).toBe(8);
        expect(imageMbOr(1, 5)).toBe(1);
    });
    it('answers the fallback for anything the writer would refuse', () => {
        expect(imageMbOr(0, 5)).toBe(5);
        expect(imageMbOr(-3, 5)).toBe(5);
        expect(imageMbOr(2.5, 5)).toBe(5);
        expect(imageMbOr('big', 5)).toBe(5);
        expect(imageMbOr(NaN, 5)).toBe(5);
        expect(imageMbOr(true, 5)).toBe(5);
    });
});
