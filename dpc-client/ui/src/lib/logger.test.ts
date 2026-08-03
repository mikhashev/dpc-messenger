import { describe, it, expect, afterEach } from 'vitest';
import { setLogSender, clearLogSender, log } from './logger';

function capture(): string[] {
    const seen: string[] = [];
    setLogSender((_level, _context, message) => seen.push(message));
    return seen;
}

describe('logger relay serialization', () => {
    afterEach(() => clearLogSender());

    it('relays an Error by its stack or message, never as "{}"', () => {
        const seen = capture();
        log.error('test', new Error('boom'));
        expect(seen).toHaveLength(1);
        expect(seen[0]).toContain('boom');
        expect(seen[0]).not.toBe('{}');
    });

    it('keeps the leading text argument alongside the Error', () => {
        const seen = capture();
        log.error('test', 'Error parsing message:', new Error('unexpected token'));
        expect(seen[0]).toContain('Error parsing message:');
        expect(seen[0]).toContain('unexpected token');
    });

    it('still JSON-stringifies plain objects', () => {
        const seen = capture();
        log.info('test', { a: 1 });
        expect(seen[0].trim()).toBe('{"a":1}');
    });
});
