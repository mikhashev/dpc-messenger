import { describe, it, expect } from 'vitest';
import { clampDetail, formatToolInput, EXPANDED_MAX } from './toolDetail';

describe('formatToolInput — the row exists so a person can read the command', () => {
    it('shows a single string argument as itself, not as JSON', () => {
        const input = JSON.stringify({
            command: 'python -c "from grafeo import GrafeoDB; print(GrafeoDB(\'x\').count())"',
        });
        expect(formatToolInput(input)).toBe(
            'python -c "from grafeo import GrafeoDB; print(GrafeoDB(\'x\').count())"',
        );
    });

    it('keeps a long command whole — the clipping was the defect', () => {
        const command = 'echo ' + 'x'.repeat(300);
        expect(formatToolInput(JSON.stringify({ command }))).toBe(command);
    });

    it('lists several arguments one per line', () => {
        const input = JSON.stringify({ path: 'notes/a.md', content: 'hello' });
        expect(formatToolInput(input)).toBe('path: notes/a.md\ncontent: hello');
    });

    it('renders a non-string argument as JSON rather than [object Object]', () => {
        const input = JSON.stringify({ path: 'a.md', lines: [1, 2] });
        expect(formatToolInput(input)).toBe('path: a.md\nlines: [1,2]');
    });

    it('shows a raw non-JSON argument as it came', () => {
        expect(formatToolInput('git status --short')).toBe('git status --short');
    });

    it('is empty for an empty input and for an argument-less call', () => {
        expect(formatToolInput('')).toBe('');
        expect(formatToolInput('{}')).toBe('');
    });
});

describe('clampDetail — a cap that says what it hid', () => {
    it('returns short text untouched', () => {
        expect(clampDetail('ok')).toBe('ok');
    });

    it('names the number of hidden characters instead of trailing off', () => {
        const text = 'y'.repeat(EXPANDED_MAX + 120);
        const out = clampDetail(text);
        expect(out.startsWith('y'.repeat(EXPANDED_MAX))).toBe(true);
        expect(out).toContain('120 more characters');
        expect(out.endsWith('...')).toBe(false);
    });

    it('keeps a 3 500-character output whole — the old cap was 500', () => {
        const text = 'z'.repeat(3500);
        expect(clampDetail(text)).toBe(text);
    });

    it('handles an empty output', () => {
        expect(clampDetail('')).toBe('');
    });

    it('shows an output the backend already clipped exactly as it would have clipped it', () => {
        // tool_detail.py sends the first EXPANDED_MAX characters and this notice.
        const original = 'q'.repeat(EXPANDED_MAX + 9000);
        const fromBackend = 'q'.repeat(EXPANDED_MAX) + '\n… 9000 more characters (full text in the agent log)';
        expect(clampDetail(fromBackend)).toBe(clampDetail(original));
        expect(clampDetail(fromBackend)).toContain('9000 more characters');
    });
});
