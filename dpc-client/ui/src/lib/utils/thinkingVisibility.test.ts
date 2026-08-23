/**
 * «Локальный чат просто попал под ту же метку и это ошибка» — Mike, 2026-08-13.
 *
 * The two directions are what matter, because a fix that only checks the new
 * case would quietly turn every agent answer into a double display of its own
 * reasoning, and a fix that only checks the old one changes nothing.
 */

import { describe, it, expect } from 'vitest';
import { showsThinkingBlock, SURFACES_WITHOUT_THEIR_OWN_REASONING_VIEW } from './thinkingVisibility';

describe('the chat that has nowhere else to show reasoning', () => {
    it('draws the block for its own AI answers', () => {
        // The defect exactly: the local chat marks every answer `sender: 'ai'`,
        // so the old rule — «hide it for AI senders» — hid all of them.
        expect(showsThinkingBlock('local_ai', true)).toBe(true);
    });

    it('and for anything else in that chat too', () => {
        expect(showsThinkingBlock('local_ai', false)).toBe(true);
    });
});

describe('surfaces that already render rounds and tool calls', () => {
    it('keep hiding it for agent answers, which is what the rule was for', () => {
        expect(showsThinkingBlock('agent_001', true)).toBe(false);
        expect(showsThinkingBlock('group-b88b65076b85', true)).toBe(false);
    });

    it('and keep showing it for everyone else there', () => {
        expect(showsThinkingBlock('agent_001', false)).toBe(true);
        expect(showsThinkingBlock('group-b88b65076b85', false)).toBe(true);
    });

    it('including the surfaces this change did not examine', () => {
        // Telegram bridges and 1:1 peer chats are untouched on purpose: nothing
        // was measured about them, so they keep the behaviour they have.
        expect(showsThinkingBlock('telegram-123', true)).toBe(false);
        expect(showsThinkingBlock('dpc-node-abc', true)).toBe(false);
    });
});

describe('the set is the whole of the exception', () => {
    it('names one surface today', () => {
        expect([...SURFACES_WITHOUT_THEIR_OWN_REASONING_VIEW]).toEqual(['local_ai']);
    });
});
