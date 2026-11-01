import { describe, it, expect } from 'vitest';
import { neededBytes, spaceVerdict } from './downloadSpace';

describe('spaceVerdict', () => {
    it('an exact size is short below (size + installed) x 1.1, enough at it', () => {
        expect(spaceVerdict(1649, 1000, 500)).toBe('short');
        expect(spaceVerdict(1651, 1000, 500)).toBe('enough');
    });

    it('an unstated installed size is not a known zero', () => {
        expect(neededBytes(1000, null)).toBeCloseTo(1100);
        expect(spaceVerdict(1099, 1000, null)).toBe('short');
    });

    it('unknown free space is never enough and never refused', () => {
        expect(spaceVerdict(null, 1000, 500)).toBe('unknown');
    });

    it('a range refuses only below its smallest variant and warns below its largest', () => {
        // Windows, v0.2.9: Control + the largest engine vs Control + the smallest.
        const size = 73_121_142 + 904_164_080, installed = 228_912_696 + 1_444_237_804;
        const sizeMin = 73_121_142 + 172_708_267, installedMin = 228_912_696 + 577_773_839;
        const least = (sizeMin + installedMin) * 1.1, most = (size + installed) * 1.1;
        expect(spaceVerdict(least - 1, size, installed, sizeMin, installedMin)).toBe('short');
        expect(spaceVerdict(least + 1, size, installed, sizeMin, installedMin)).toBe('may_not_fit');
        expect(spaceVerdict(most - 1, size, installed, sizeMin, installedMin)).toBe('may_not_fit');
        expect(spaceVerdict(most + 1, size, installed, sizeMin, installedMin)).toBe('enough');
    });
});
