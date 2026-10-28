import { describe, it, expect, vi } from 'vitest';
import { revealInFolder, revealFailureText } from './revealInFolder';

describe('revealInFolder', () => {
    it('passes the stored path to the reveal and reports nothing on success', async () => {
        const reveal = vi.fn().mockResolvedValue(undefined);
        const out = await revealInFolder('C:\\Users\\x\\.dpc\\conversations\\p\\files\\notes.md', reveal);
        expect(out).toBeNull();
        expect(reveal).toHaveBeenCalledWith('C:\\Users\\x\\.dpc\\conversations\\p\\files\\notes.md');
    });

    it('does not call the reveal without a path', async () => {
        const reveal = vi.fn();
        expect(await revealInFolder('', reveal)).toBe('The file has no saved location');
        expect(await revealInFolder(undefined, reveal)).toBe('The file has no saved location');
        expect(reveal).not.toHaveBeenCalled();
    });

    it('turns a rejection into one line instead of throwing', async () => {
        vi.spyOn(console, 'error').mockImplementation(() => {});
        const reveal = vi.fn().mockRejectedValue('path not found');
        expect(await revealInFolder('/x/y.md', reveal)).toBe('Could not show the file: path not found');
    });
});

describe('revealFailureText', () => {
    it('reads an Error message, a string, and falls back when there is none', () => {
        expect(revealFailureText(new Error('boom'))).toBe('Could not show the file: boom');
        expect(revealFailureText('gone')).toBe('Could not show the file: gone');
        expect(revealFailureText(undefined)).toBe('Could not show the file');
    });

    it('cuts a long OS message', () => {
        const text = revealFailureText('x'.repeat(500));
        expect(text.length).toBeLessThan(160);
        expect(text.endsWith('...')).toBe(true);
    });
});
