/**
 * An agent turn that ended without an answer is kept in history.json with a
 * `turn_end` saying why (stopped, killed, failed, refused, guard — Mike's call,
 * 2026-10-27). The reload maps it to `turnEnd`, and the live `execute_ai_query`
 * response reads it through the same `turnEndOf`, so the bubble drawn live is
 * the bubble a reload draws. A record without the field is an answer, and so
 * is a value this UI does not know.
 */

import { describe, it, expect } from 'vitest';
import { mapBackendMessage, turnEndOf } from './messageMapper';

const IDENTITY = { agentSelfId: 'agent_001', agentSelfName: 'Ark', selfNodeId: 'dpc-node-me' };

function record(extra: Record<string, unknown> = {}) {
    return {
        id: 'm2', role: 'assistant', content: '⚠️ Stopped by user after 2 rounds.',
        timestamp: '2026-10-27T10:00:02Z', sender_name: 'Ark', msg_index: 2, ...extra,
    };
}

describe('turnEnd on the history reload', () => {
    it.each(['stopped', 'killed', 'failed', 'refused', 'guard'] as const)(
        'carries %s from the record', (value) => {
            const mapped = mapBackendMessage(record({ turn_end: value }), { identity: IDENTITY });
            expect(mapped.turnEnd).toBe(value);
            expect(mapped.isAgent).toBe(true);
            expect(mapped.text).toBe('⚠️ Stopped by user after 2 rounds.');
        });

    it('leaves an answer unmarked, even one that opens with a warning sign', () => {
        const mapped = mapBackendMessage(record({ content: '⚠️ Not done: the agent wrote…' }),
            { identity: IDENTITY });
        expect(mapped.turnEnd).toBeUndefined();
    });

    it('reads a value it does not know as an answer', () => {
        expect(mapBackendMessage(record({ turn_end: 'cancelled' })).turnEnd).toBeUndefined();
    });
});

describe('turnEndOf, shared by the live response', () => {
    it('agrees with the reload for every value', () => {
        for (const value of ['stopped', 'killed', 'failed', 'refused', 'guard', undefined, null, 'x', 3]) {
            expect(turnEndOf(value)).toBe(mapBackendMessage(record({ turn_end: value })).turnEnd);
        }
    });
});
