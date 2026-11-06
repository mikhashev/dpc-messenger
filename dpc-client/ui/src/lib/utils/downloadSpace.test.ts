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
        // Windows, v0.2.11: Control + the largest engine vs Control + the smallest.
        const size = 73_411_113 + 915_324_388, installed = 229_788_151 + 1_479_343_085;
        const sizeMin = 73_411_113 + 174_926_996, installedMin = 229_788_151 + 584_050_448;
        const least = (sizeMin + installedMin) * 1.1, most = (size + installed) * 1.1;
        expect(spaceVerdict(least - 1, size, installed, sizeMin, installedMin)).toBe('short');
        expect(spaceVerdict(least + 1, size, installed, sizeMin, installedMin)).toBe('may_not_fit');
        expect(spaceVerdict(most - 1, size, installed, sizeMin, installedMin)).toBe('may_not_fit');
        expect(spaceVerdict(most + 1, size, installed, sizeMin, installedMin)).toBe('enough');
    });
});
