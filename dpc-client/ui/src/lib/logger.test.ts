import { describe, it, expect, afterEach } from 'vitest';
import { setLogSender, clearLogSender, log, clipForRelay, RELAY_MAX_CHARS } from './logger';

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
        // Deliberately not "Error parsing message" — that phrase is on the
        // never-relay list, and using it here would test the breaker instead
        // of the serialization this case is about.
        log.error('test', 'Command failed:', new Error('unexpected token'));
        expect(seen[0]).toContain('Command failed:');
        expect(seen[0]).toContain('unexpected token');
    });

    it('still JSON-stringifies plain objects', () => {
        const seen = capture();
        log.info('test', { a: 1 });
        expect(seen[0].trim()).toBe('{"a":1}');
    });

    it('relays the head of a huge logged object, not all of it', () => {
        // A logged group history once went past the backend's 10 MiB read
        // limit and the socket closed with 1009 in a reconnect loop.
        const seen = capture();
        const history = { messages: Array.from({ length: 200 }, () => ({ tool_calls: 'x'.repeat(60000) })) };
        log.info('ChatHistory', 'Backend response:', history);
        expect(seen).toHaveLength(1);
        expect(seen[0].length).toBeLessThan(RELAY_MAX_CHARS + 100);
        expect(seen[0]).toContain('more characters not relayed');
        expect(seen[0].trimStart().startsWith('Backend response:')).toBe(true);
    });

    it('relays an ordinary line whole', () => {
        expect(clipForRelay('short line')).toBe('short line');
    });
});

describe('relay feedback breaker', () => {
    afterEach(() => clearLogSender());

    it('never relays a report about the socket it would relay over', () => {
        const seen = capture();

        log.error('coreService', 'Error parsing message:', new Error('Unexpected token s'));

        expect(seen).toHaveLength(0);
    });

    it('caps the relay so no feedback path can saturate the link', () => {
        const seen = capture();

        for (let i = 0; i < 500; i++) log.info('flood', `line ${i}`);

        // 166,000 lines in twelve seconds is what the uncapped relay produced.
        expect(seen.length).toBeLessThanOrEqual(60);
        expect(seen.length).toBeGreaterThan(0);
    });

    it('says how much it swallowed instead of going quiet', async () => {
        const seen = capture();
        for (let i = 0; i < 500; i++) log.info('flood', `line ${i}`);
        const duringFlood = seen.length;

        await new Promise((r) => setTimeout(r, 1100));
        log.info('after', 'next window');

        const note = seen.slice(duringFlood).join(' ');
        expect(note).toContain('suppressed');
        expect(note).toContain('previous second');
    });
});
