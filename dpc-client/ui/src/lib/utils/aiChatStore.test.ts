import { describe, it, expect } from 'vitest';
import {
    LEGACY_AI_CHATS_KEY,
    LEGACY_AI_CHAT_HISTORIES_KEY,
    LEGACY_TELEGRAM_CHATS_KEY,
    readLegacyChatStore,
    importUnits,
    writeImportRemainder,
    importPayloads,
    sidebarEntry,
    isAIChatId,
    answersFreshInstall,
    eraseLegacyChatStore,
    FINAL_IMPORT_STATUSES,
} from './aiChatStore';

function storage(items: Record<string, string>) {
    return { getItem: (key: string) => (key in items ? items[key] : null) };
}

describe('readLegacyChatStore', () => {
    it('is null when the old UI left nothing behind', () => {
        expect(readLegacyChatStore(storage({}))).toBeNull();
    });

    it('reads the three keys and sets aside one that is not a JSON object', () => {
        const store = readLegacyChatStore(storage({
            [LEGACY_AI_CHATS_KEY]: JSON.stringify({ ai_chat_1: { name: 'A', provider: 'p' } }),
            [LEGACY_AI_CHAT_HISTORIES_KEY]: '[1, 2]',
            [LEGACY_TELEGRAM_CHATS_KEY]: '{not json',
        }))!;
        expect(store.chats).toEqual({ ai_chat_1: { name: 'A', provider: 'p' } });
        expect(store.histories).toBeNull();
        expect(store.unreadable).toEqual([LEGACY_AI_CHAT_HISTORIES_KEY, LEGACY_TELEGRAM_CHATS_KEY]);
    });
});

describe('importUnits', () => {
    it('makes one unit per chat, joining a record and a history of the same id', () => {
        const units = importUnits({
            chats: { ai_chat_1: { name: 'A' }, agent_001: { name: 'Ark' } },
            histories: { ai_chat_1: [{ id: 'm1' }], ai_chat_2: [{ id: 'm2' }] },
            telegram: { 'telegram-42': { name: 'T' } },
            unreadable: [],
        });
        expect(units.map(u => [u.kind, u.conversation_id])).toEqual([
            ['ai', 'ai_chat_1'], ['ai', 'agent_001'], ['ai', 'ai_chat_2'], ['telegram', 'telegram-42'],
        ]);
        expect(units[0].record).toEqual({ name: 'A' });
        expect(units[0].messages).toEqual([{ id: 'm1' }]);
        expect(units[2].record).toBeNull();
    });
});

function memoryStorage(items: Record<string, string>) {
    const writes: Array<[string, string | null]> = [];
    return {
        items,
        writes,
        setItem: (k: string, v: string) => { items[k] = v; writes.push([k, v]); },
        removeItem: (k: string) => { delete items[k]; writes.push([k, null]); },
    };
}

describe('writeImportRemainder', () => {
    const store = {
        chats: { ai_chat_1: { name: 'A' }, ai_chat_2: { name: 'B' } },
        histories: { ai_chat_1: [{ id: 'm1' }], ai_chat_2: [{ id: 'm2' }] },
        telegram: { 'telegram-42': { name: 'T' } },
        unreadable: [],
    };
    const unit = (kind: 'ai' | 'telegram', id: string) => ({ kind, conversation_id: id, record: {} });

    it('drops each chat with a final answer and keeps the rest, chat by chat', () => {
        const storage = memoryStorage({});
        const removed = writeImportRemainder(storage, store, [
            { unit: unit('ai', 'ai_chat_1'), status: 'imported' },
            { unit: unit('ai', 'ai_chat_2'), status: 'failed' },
            { unit: unit('telegram', 'telegram-42'), status: 'refused' },
        ]);
        expect(removed).toEqual([LEGACY_TELEGRAM_CHATS_KEY]);
        expect(JSON.parse(storage.items[LEGACY_AI_CHATS_KEY])).toEqual({ ai_chat_2: { name: 'B' } });
        expect(JSON.parse(storage.items[LEGACY_AI_CHAT_HISTORIES_KEY])).toEqual({ ai_chat_2: [{ id: 'm2' }] });
    });

    it('keeps a chat whose history would not be kept, or that never got an answer', () => {
        const storage = memoryStorage({});
        writeImportRemainder(storage, store, [
            { unit: unit('ai', 'ai_chat_1'), status: 'deferred' },
            { unit: unit('ai', 'ai_chat_2'), status: undefined },
            { unit: unit('telegram', 'telegram-42'), status: 'kept' },
        ]);
        expect(storage.writes).toEqual([[LEGACY_TELEGRAM_CHATS_KEY, null]]);
    });

    it('only ever writes a subset of what it read', () => {
        const storage = memoryStorage({});
        writeImportRemainder(storage, store, [{ unit: unit('ai', 'ai_chat_2'), status: 'skipped' }]);
        for (const [key, value] of storage.writes) {
            const before = key === LEGACY_AI_CHATS_KEY ? store.chats : store.histories;
            if (value !== null) {
                for (const id of Object.keys(JSON.parse(value))) expect(Object.keys(before)).toContain(id);
            }
        }
        expect(storage.writes.length).toBe(2);
    });
});

describe('importPayloads', () => {
    const messages = Array.from({ length: 10 }, (_, i) => ({ id: `m${i}`, sender: 'user', text: 'x'.repeat(100) }));
    const unit = { kind: 'ai' as const, conversation_id: 'ai_chat_1', record: { name: 'A' }, messages };

    it('sends a small chat in one call with no partial flag', () => {
        const payloads = importPayloads(unit)!;
        expect(payloads).toHaveLength(1);
        expect(payloads[0]).toEqual({ chats: { ai_chat_1: { name: 'A' } }, histories: { ai_chat_1: messages } });
    });

    it('splits a large history into calls under the limit, all but the last partial', () => {
        const payloads = importPayloads(unit, 600)!;
        expect(payloads.length).toBeGreaterThan(1);
        for (const p of payloads) expect(new TextEncoder().encode(JSON.stringify(p)).length).toBeLessThanOrEqual(600);
        expect(payloads.slice(0, -1).every(p => p.partial === true)).toBe(true);
        expect(payloads[payloads.length - 1].partial).toBeUndefined();
        expect(payloads.flatMap(p => (p.histories as any).ai_chat_1)).toEqual(messages);
        expect(payloads[0].chats).toEqual({ ai_chat_1: { name: 'A' } });
    });

    it('refuses a chat with one message over the limit rather than send it', () => {
        expect(importPayloads({ ...unit, messages: [{ id: 'big', text: 'x'.repeat(2000) }] }, 600)).toBeNull();
    });
});

describe('sidebarEntry', () => {
    it('names a Telegram chat by its record, or by its chat id when it has none', () => {
        expect(sidebarEntry({ conversation_id: 'telegram-42', provider: 'telegram', name: '📱 Telegram (Mike)', telegram_chat_id: '42' }))
            .toEqual(['telegram-42', { name: '📱 Telegram (Mike)', provider: 'telegram', instruction_set_name: 'general' }]);
        expect(sidebarEntry({ conversation_id: 'telegram-42', provider: 'telegram', name: null, telegram_chat_id: '42' })![1].name)
            .toBe('📱 Telegram (42)');
    });

    it('keeps the record fields of an AI chat and drops empty ones', () => {
        expect(sidebarEntry({ conversation_id: 'ai_chat_1', name: 'qwen (q3)', provider: 'qwen', instruction_set_name: 'general', compute_host: '', created_at: 'x' }))
            .toEqual(['ai_chat_1', { name: 'qwen (q3)', provider: 'qwen', instruction_set_name: 'general' }]);
        expect(sidebarEntry({})).toBeNull();
    });
});

describe('isAIChatId', () => {
    it('covers the built-in chat and the created ones, not agents', () => {
        expect(isAIChatId('local_ai')).toBe(true);
        expect(isAIChatId('ai_chat_1a2b3c4d')).toBe(true);
        expect(isAIChatId('agent_001')).toBe(false);
    });
});

describe('a fresh install (~/.dpc did not exist at the backend start)', () => {
    const unit = (kind: 'ai' | 'telegram', id: string) => ({ kind, conversation_id: id, record: {} });

    it('counts a discarded chat as settled, and removes all three keys when everything was discarded', () => {
        expect(FINAL_IMPORT_STATUSES.has('discarded')).toBe(true);
        const store = {
            chats: { ai_chat_1: { name: 'A' } },
            histories: { ai_chat_1: [{ id: 'm1' }] },
            telegram: { 'telegram-42': { name: 'T' } },
            unreadable: [],
        };
        const storage = memoryStorage({});
        const removed = writeImportRemainder(storage, store, [
            { unit: unit('ai', 'ai_chat_1'), status: 'discarded' },
            { unit: unit('telegram', 'telegram-42'), status: 'discarded' },
        ]);
        expect(removed.sort()).toEqual([LEGACY_AI_CHATS_KEY, LEGACY_AI_CHAT_HISTORIES_KEY, LEGACY_TELEGRAM_CHATS_KEY].sort());
        expect(storage.items).toEqual({});
    });

    it('reads the flag on any answer, including one to a call that sent no chat, and a discarded row', () => {
        expect(answersFreshInstall({ status: 'success', results: [], fresh_install: true })).toBe(true);
        expect(answersFreshInstall({ status: 'success', results: [{ status: 'discarded' }] })).toBe(true);
        expect(answersFreshInstall({ status: 'success', results: [{ status: 'imported' }], fresh_install: false })).toBe(false);
        expect(answersFreshInstall({ status: 'error', message: 'x' })).toBe(false);
        expect(answersFreshInstall(undefined)).toBe(false);
    });

    it('erases the three keys even when one cannot be read, and names the ones that held something', () => {
        const items: Record<string, string> = {
            [LEGACY_AI_CHATS_KEY]: JSON.stringify({ ai_chat_1: { name: 'A' } }),
            [LEGACY_TELEGRAM_CHATS_KEY]: '{not json',
            unrelated: 'stays',
        };
        const storage = {
            getItem: (k: string) => (k in items ? items[k] : null),
            removeItem: (k: string) => { delete items[k]; },
        };
        expect(eraseLegacyChatStore(storage)).toEqual([LEGACY_AI_CHATS_KEY, LEGACY_TELEGRAM_CHATS_KEY]);
        expect(items).toEqual({ unrelated: 'stays' });
        expect(readLegacyChatStore(storage)).toBeNull();
    });

    it('survives a store that refuses to answer', () => {
        const refusing = {
            getItem: () => { throw new Error('denied'); },
            removeItem: () => { throw new Error('denied'); },
        };
        expect(eraseLegacyChatStore(refusing)).toEqual([]);
    });
});
