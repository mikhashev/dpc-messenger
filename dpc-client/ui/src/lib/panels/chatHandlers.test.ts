// src/lib/panels/chatHandlers.test.ts
// Baseline characterization tests for chat routing and streaming logic.
// These capture current +page.svelte behaviour BEFORE panel extraction (Step 3b).
// Tests run in Vitest node environment — no DOM, no Svelte, no stores.
//
// Pattern: pure helper functions are defined inline here, matching the logic
// currently in +page.svelte. When Step 4 extracts ChatPanel, these functions
// move to ChatPanel.svelte and the tests travel with them unchanged.

import { describe, it, expect } from 'vitest';

// ---------------------------------------------------------------------------
// Pure helpers (mirror of +page.svelte logic, to be moved in Step 4)
// ---------------------------------------------------------------------------

/**
 * Resolve the chat type for a given activeChatId + aiChats Map.
 *
 * Mirrors the if/else routing in handleSendMessage (lines ~2353-2700):
 *   aiChats.has(id) && !id.startsWith('telegram-') → 'ai'
 *   id.startsWith('telegram-')                     → 'telegram'
 *   id.startsWith('group-')                        → 'group'
 *   otherwise (dpc-node-*)                         → 'p2p'
 */
function resolveChatRoute(
    activeChatId: string,
    aiChats: Map<string, unknown>,
): 'ai' | 'telegram' | 'group' | 'p2p' {
    if (activeChatId.startsWith('telegram-')) return 'telegram';
    if (activeChatId.startsWith('group-')) return 'group';
    if (aiChats.has(activeChatId)) return 'ai';
    return 'p2p';
}

/**
 * Persist a draft message for a chat ID.
 * Returns a NEW Map — does not mutate the input.
 */
function persistDraft(
    drafts: Map<string, string>,
    chatId: string,
    text: string,
): Map<string, string> {
    return new Map(drafts).set(chatId, text);
}

/**
 * Restore a draft for a chat ID (empty string if none).
 */
function restoreDraft(drafts: Map<string, string>, chatId: string): string {
    return drafts.get(chatId) ?? '';
}

/**
 * Accumulate a streaming text chunk onto the buffer.
 */
function accumulateChunk(buffer: string, chunk: string): string {
    return buffer + chunk;
}

/**
 * Clear the streaming buffer (returns empty string).
 */
function clearStreaming(): string {
    return '';
}

/**
 * Determine whether a conversation_id matches the active chat.
 * Mirrors isActiveChatConv() used in agent progress effects.
 * Agent chats use id like "agent_001"; backend sends conversation_id "agent_001".
 */
function isActiveChatConv(activeChatId: string, conversationId: string): boolean {
    return activeChatId === conversationId;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('resolveChatRoute (Baseline)', () => {
    const aiChats = new Map([
        ['local_ai', { provider: 'ollama' }],
        ['ai_abc123', { provider: 'anthropic' }],
        ['agent_001', { provider: 'dpc_agent' }],
    ]);

    it('routes local_ai to ai', () => {
        expect(resolveChatRoute('local_ai', aiChats)).toBe('ai');
    });

    it('routes ai_ prefix to ai', () => {
        expect(resolveChatRoute('ai_abc123', aiChats)).toBe('ai');
    });

    it('routes agent_ prefix to ai (agent chats are in aiChats map)', () => {
        expect(resolveChatRoute('agent_001', aiChats)).toBe('ai');
    });

    it('routes telegram- prefix to telegram (takes precedence over aiChats lookup)', () => {
        // Even if somehow a telegram id ended up in aiChats, telegram prefix wins
        const withTelegram = new Map(aiChats).set('telegram-12345', { provider: 'telegram' });
        expect(resolveChatRoute('telegram-12345', withTelegram)).toBe('telegram');
    });

    it('routes group- prefix to group', () => {
        expect(resolveChatRoute('group-abc', aiChats)).toBe('group');
    });

    it('routes dpc-node-* to p2p', () => {
        expect(resolveChatRoute('dpc-node-abc123def456789a', aiChats)).toBe('p2p');
    });

    it('routes unknown id not in aiChats to p2p', () => {
        expect(resolveChatRoute('unknown-id', aiChats)).toBe('p2p');
    });
});

describe('Draft persistence (Baseline)', () => {
    it('persists draft for a chat', () => {
        const drafts = new Map<string, string>();
        const updated = persistDraft(drafts, 'ai_abc', 'hello world');
        expect(updated.get('ai_abc')).toBe('hello world');
    });

    it('does not mutate the original map', () => {
        const drafts = new Map<string, string>();
        persistDraft(drafts, 'ai_abc', 'hello');
        expect(drafts.has('ai_abc')).toBe(false);
    });

    it('overwrites existing draft for same chat', () => {
        const drafts = new Map([['ai_abc', 'old text']]);
        const updated = persistDraft(drafts, 'ai_abc', 'new text');
        expect(updated.get('ai_abc')).toBe('new text');
    });

    it('preserves drafts for other chats', () => {
        const drafts = new Map([['ai_xyz', 'keep this']]);
        const updated = persistDraft(drafts, 'ai_abc', 'new draft');
        expect(updated.get('ai_xyz')).toBe('keep this');
        expect(updated.get('ai_abc')).toBe('new draft');
    });

    it('restores draft for known chat', () => {
        const drafts = new Map([['ai_abc', 'saved text']]);
        expect(restoreDraft(drafts, 'ai_abc')).toBe('saved text');
    });

    it('restores empty string for unknown chat', () => {
        const drafts = new Map<string, string>();
        expect(restoreDraft(drafts, 'ai_abc')).toBe('');
    });
});

describe('Agent streaming (Baseline)', () => {
    it('accumulates streaming text chunks', () => {
        const b0 = '';
        const b1 = accumulateChunk(b0, 'Hello ');
        const b2 = accumulateChunk(b1, 'world');
        expect(b2).toBe('Hello world');
    });

    it('accumulates to non-empty buffer', () => {
        expect(accumulateChunk('existing ', 'chunk')).toBe('existing chunk');
    });

    it('accumulates empty chunk (no-op)', () => {
        expect(accumulateChunk('abc', '')).toBe('abc');
    });

    it('clearStreaming returns empty string', () => {
        expect(clearStreaming()).toBe('');
    });
});

describe('isActiveChatConv (Baseline)', () => {
    it('matches exact conversation_id', () => {
        expect(isActiveChatConv('agent_001', 'agent_001')).toBe(true);
    });

    it('does not match different conversation_id', () => {
        expect(isActiveChatConv('agent_001', 'agent_002')).toBe(false);
    });

    it('does not match partial prefix', () => {
        expect(isActiveChatConv('agent_001', 'agent_')).toBe(false);
    });

    it('matches local_ai conversation', () => {
        expect(isActiveChatConv('local_ai', 'local_ai')).toBe(true);
    });
});
