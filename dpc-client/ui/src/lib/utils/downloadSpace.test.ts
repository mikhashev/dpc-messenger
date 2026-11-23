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
        // Windows, v0.2.19: Control + the largest engine vs Control + the smallest.
        const size = 67_721_651 + 743_821_368, installed = 219_537_243 + 1_155_646_715;
        const sizeMin = 67_721_651 + 166_003_861, installedMin = 219_537_243 + 496_744_538;
        const least = (sizeMin + installedMin) * 1.1, most = (size + installed) * 1.1;
        expect(spaceVerdict(least - 1, size, installed, sizeMin, installedMin)).toBe('short');
        expect(spaceVerdict(least + 1, size, installed, sizeMin, installedMin)).toBe('may_not_fit');
        expect(spaceVerdict(most - 1, size, installed, sizeMin, installedMin)).toBe('may_not_fit');
        expect(spaceVerdict(most + 1, size, installed, sizeMin, installedMin)).toBe('enough');
    });
});
