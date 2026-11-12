/**
 * The finished header strip of an agent turn is stored by the backend on the
 * message (`speed_summary`, beside `tool_calls`). Every path that turns a
 * backend record into a chat row goes through the mapper, so the strip comes
 * back after a chat switch, a refresh and a restart only if the mapper keeps it.
 */

import { describe, it, expect } from 'vitest';
import { mapBackendMessage, speedSummaryOf } from './messageMapper';

const SUMMARY = {
    rounds: 3,
    prefill_tok_s: 812.5,
    decode_tok_s: 41.2,
    total_tok_s: null,
    context_used: 53960,
    context_window: 262144,
    context_reserve: 8192,
    cached_tokens: 47700,
    prompt_tokens: 60000,
    round: 3,
    model: 'qwen3',
    median: true,
};

describe('the stored strip', () => {
    it('is carried from the record onto the row', () => {
        const row = mapBackendMessage({
            id: 'a1', role: 'assistant', content: 'done',
            tool_calls: [{ tool: 'read_file', input: '', output: '', is_error: false, duration_ms: 1, round: 1 }],
            speed_summary: SUMMARY,
        });
        expect(row.speed_summary).toEqual(SUMMARY);
        expect(row.tool_calls).toHaveLength(1);
    });

    it('is absent on an old record, which renders as before', () => {
        const row = mapBackendMessage({ id: 'a1', role: 'assistant', content: 'done' });
        expect(row.speed_summary).toBeUndefined();
    });

    it('wins over a copy the open chat held', () => {
        const local = { ...SUMMARY, decode_tok_s: 1 };
        const row = mapBackendMessage({ id: 'a1', content: 'x', speed_summary: SUMMARY }, { local: { speed_summary: local } });
        expect(row.speed_summary).toEqual(SUMMARY);
        const old = mapBackendMessage({ id: 'a1', content: 'x' }, { local: { speed_summary: local } });
        expect(old.speed_summary).toEqual(local);
    });

    it('is only an object', () => {
        expect(speedSummaryOf(null)).toBeUndefined();
        expect(speedSummaryOf('fast')).toBeUndefined();
        expect(speedSummaryOf([1, 2])).toBeUndefined();
        expect(speedSummaryOf(SUMMARY)).toBe(SUMMARY);
    });
});
