import { describe, it, expect } from 'vitest';
import { boolValue, parseWholeNumber, tokenPlaceholder } from './telegramDiscord';

describe('parseWholeNumber (the Discord limits)', () => {
    it('takes whole numbers at or above the reader minimum, as text or number', () => {
        expect(parseWholeNumber('5', 1)).toBe(5);
        expect(parseWholeNumber(' 600 ', 1)).toBe(600);
        expect(parseWholeNumber(30, 1)).toBe(30);
        expect(parseWholeNumber('0', 0)).toBe(0);
    });
    it('answers null for what the backend would refuse, so the form says so first', () => {
        expect(parseWholeNumber('', 1)).toBeNull();
        expect(parseWholeNumber('2.5', 1)).toBeNull();
        expect(parseWholeNumber('ten', 1)).toBeNull();
        expect(parseWholeNumber('-1', 0)).toBeNull();
        expect(parseWholeNumber('0', 1)).toBeNull();
        expect(parseWholeNumber(true as unknown as string, 1)).toBeNull();
    });
});

describe('boolValue', () => {
    it('answers a real bool and the fallback for anything else', () => {
        expect(boolValue(true, false)).toBe(true);
        expect(boolValue(false, true)).toBe(false);
        expect(boolValue('true', false)).toBe(false);
        expect(boolValue(1, false)).toBe(false);
        expect(boolValue(null, true)).toBe(true);
    });
});

describe('tokenPlaceholder', () => {
    it('says a token is stored with dots, never a value, and that empty keeps it', () => {
        const set = tokenPlaceholder(true);
        expect(set).toContain('••••••••');
        expect(set).toContain('(set');
        expect(set).toContain('leave empty to keep it');
    });
    it('asks for the token when none is stored', () => {
        expect(tokenPlaceholder(false)).toBe('Paste the bot token');
    });
});
