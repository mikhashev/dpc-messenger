import { describe, it, expect } from 'vitest';
import { parseSwapWait } from './swapWait';

describe('parseSwapWait', () => {
    it('takes a whole number of seconds, 0 included', () => {
        expect(parseSwapWait('300')).toBe(300);
        expect(parseSwapWait(' 45 ')).toBe(45);
        expect(parseSwapWait('0')).toBe(0);
        expect(parseSwapWait(12)).toBe(12);
    });
    it('refuses what the backend would refuse', () => {
        for (const bad of ['', '  ', '-1', '1.5', '1e3', 'soon', '5 s', '+5']) {
            expect(parseSwapWait(bad), JSON.stringify(bad)).toBeNull();
        }
        expect(parseSwapWait(-1)).toBeNull();
        expect(parseSwapWait(2.5)).toBeNull();
        expect(parseSwapWait(NaN)).toBeNull();
        expect(parseSwapWait(undefined)).toBeNull();
        expect(parseSwapWait(null)).toBeNull();
    });
});
