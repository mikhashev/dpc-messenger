/**
 * The sidebar's AI chats and Telegram chats come from the backend, and this is
 * the one-time move of what the old UI kept for them in localStorage.
 *
 * Until 2026-10-19 the WebView held the chat list, the Telegram chats and the
 * full text of AI chat histories under the three keys below, outside ~/.dpc,
 * and put them back at start without asking the backend — so a reset of ~/.dpc
 * left chats behind (THE-UI-KEEPS-CHATS-AND-AI-CHAT-HISTORIES-IN-THE-WEBVIEW-
 * OUTSIDE-DOT-DPC). Mike's call that day: the backend owns chats and their
 * histories; localStorage keeps window preferences only.
 *
 * The keys are read here and shrunk or removed here, and written nowhere else —
 * the pin test beside this file holds that.
 */

import { writable } from 'svelte/store';

export const LEGACY_AI_CHATS_KEY = 'dpc-ai-chats';
export const LEGACY_AI_CHAT_HISTORIES_KEY = 'dpc-ai-chat-histories';
export const LEGACY_TELEGRAM_CHATS_KEY = 'dpc-telegram-chats';
export const LEGACY_CHAT_KEYS = [
    LEGACY_AI_CHATS_KEY,
    LEGACY_AI_CHAT_HISTORIES_KEY,
    LEGACY_TELEGRAM_CHATS_KEY,
] as const;

export type AIChatMeta = {
    name: string;
    provider: string;
    instruction_set_name?: string;
    profile_name?: string;
    llm_provider?: string;
    compute_host?: string;
};

/** The default chat's sidebar name. It is never stored: the backend keeps no `local_ai` record. */
export const DEFAULT_AI_CHAT_NAME = 'AI Chat';

/**
 * The value of `instruction_set_name` that sends no instruction set
 * (PromptManager._build_system_instruction answers "" for it). An unset value is
 * not the same: the backend turns a missing one into the global default set.
 */
export const NO_INSTRUCTION_SET = 'none';

/** What the default chat and a new AI chat start with; the person may pick a set. */
export const DEFAULT_CHAT_INSTRUCTION_SET = NO_INSTRUCTION_SET;

/** The instruction set a chat sends, so that an unset one is sent as "none" and not left to the backend's default. */
export function chatInstructionSet(meta: Pick<AIChatMeta, 'instruction_set_name'> | undefined | null): string {
    return meta?.instruction_set_name || DEFAULT_CHAT_INSTRUCTION_SET;
}

type StoredObject = Record<string, any>;

export interface LegacyChatStore {
    chats: StoredObject | null;
    histories: StoredObject | null;
    telegram: StoredObject | null;
    /** Keys present but not a JSON object: never imported, never removed. */
    unreadable: string[];
}

export interface ImportUnit {
    /** Which key the chat came from: AI chats share two keys, Telegram has one. */
    kind: 'ai' | 'telegram';
    conversation_id: string;
    record: unknown;
    messages?: unknown;
}

function parseObject(raw: string | null): StoredObject | null | 'unreadable' {
    if (raw === null) return null;
    try {
        const value = JSON.parse(raw);
        return value && typeof value === 'object' && !Array.isArray(value) ? value : 'unreadable';
    } catch {
        return 'unreadable';
    }
}

/** What the old UI left behind, or null when nothing is left. */
export function readLegacyChatStore(storage: Pick<Storage, 'getItem'>): LegacyChatStore | null {
    const store: LegacyChatStore = { chats: null, histories: null, telegram: null, unreadable: [] };
    const slots: Array<[string, 'chats' | 'histories' | 'telegram']> = [
        [LEGACY_AI_CHATS_KEY, 'chats'],
        [LEGACY_AI_CHAT_HISTORIES_KEY, 'histories'],
        [LEGACY_TELEGRAM_CHATS_KEY, 'telegram'],
    ];
    let found = false;
    for (const [key, slot] of slots) {
        let raw: string | null = null;
        try {
            raw = storage.getItem(key);
        } catch {
            raw = null;
        }
        const parsed = parseObject(raw);
        if (parsed === 'unreadable') {
            store.unreadable.push(key);
            found = true;
        } else if (parsed !== null) {
            store[slot] = parsed;
            found = true;
        }
    }
    return found ? store : null;
}

/** One unit per chat, so each import call stays small and answers for one chat. */
export function importUnits(store: LegacyChatStore): ImportUnit[] {
    const units: ImportUnit[] = [];
    const aiIds = new Set([...Object.keys(store.chats ?? {}), ...Object.keys(store.histories ?? {})]);
    for (const id of aiIds) {
        units.push({
            kind: 'ai',
            conversation_id: id,
            record: store.chats?.[id] ?? null,
            messages: store.histories?.[id],
        });
    }
    for (const id of Object.keys(store.telegram ?? {})) {
        units.push({ kind: 'telegram', conversation_id: id, record: store.telegram![id] });
    }
    return units;
}

/**
 * The answers after which a chat leaves the old store. "discarded" is the
 * backend's answer on a fresh install (~/.dpc did not exist at its start): the
 * chat is not imported and goes. "deferred" (its history would not be kept) and
 * "failed" are not final, and neither is no answer at all — the call never came
 * back, or the chat was too large to send.
 */
export const FINAL_IMPORT_STATUSES = new Set(['imported', 'kept', 'skipped', 'refused', 'discarded']);

/**
 * Whether an `import_ai_chats` answer says ~/.dpc is a fresh install. The
 * top-level flag is there on every answer, including one to a call that sent no
 * chat; a "discarded" row says the same for a backend that answers per chat.
 */
export function answersFreshInstall(result: any): boolean {
    return result?.fresh_install === true
        || (Array.isArray(result?.results) && result.results.some((r: any) => r?.status === 'discarded'));
}

/**
 * Erase all three keys, readable or not: a fresh install is a clean slate, and
 * nothing of the old UI may stay outside ~/.dpc. Returns the keys that held
 * something. Like `writeImportRemainder`, one of the only places that writes
 * these keys.
 */
export function eraseLegacyChatStore(storage: Pick<Storage, 'getItem' | 'removeItem'>): string[] {
    const erased: string[] = [];
    for (const key of LEGACY_CHAT_KEYS) {
        let present = false;
        try {
            present = storage.getItem(key) !== null;
        } catch {
            present = false;
        }
        try {
            storage.removeItem(key);
            if (present) erased.push(key);
        } catch {
            // the store refuses; the next start asks again
        }
    }
    return erased;
}

export type ImportOutcome = { unit: ImportUnit; status: string | undefined };

/**
 * Write back what is still to be imported, chat by chat: a chat with a final
 * answer leaves its key, a key left empty is removed, an unreadable key is not
 * touched. Only ever shrinks a key — it writes a subset of what it read, and
 * this is the one place in the UI that writes these keys at all.
 */
export function writeImportRemainder(
    storage: Pick<Storage, 'setItem' | 'removeItem'>,
    store: LegacyChatStore,
    outcomes: ImportOutcome[],
): string[] {
    const settled = { ai: new Set<string>(), telegram: new Set<string>() };
    for (const o of outcomes) {
        if (o.status && FINAL_IMPORT_STATUSES.has(o.status)) settled[o.unit.kind].add(o.unit.conversation_id);
    }
    const slots: Array<[string, StoredObject | null, Set<string>]> = [
        [LEGACY_AI_CHATS_KEY, store.chats, settled.ai],
        [LEGACY_AI_CHAT_HISTORIES_KEY, store.histories, settled.ai],
        [LEGACY_TELEGRAM_CHATS_KEY, store.telegram, settled.telegram],
    ];
    const removed: string[] = [];
    for (const [key, value, done] of slots) {
        if (!value || store.unreadable.includes(key)) continue;
        const rest = Object.fromEntries(Object.entries(value).filter(([id]) => !done.has(id)));
        const restCount = Object.keys(rest).length;
        if (restCount === Object.keys(value).length) continue;
        if (restCount === 0) {
            storage.removeItem(key);
            removed.push(key);
        } else {
            storage.setItem(key, JSON.stringify(rest));
        }
    }
    return removed;
}

/**
 * The backend's socket takes at most 10 MB a message (local_api.py), and a
 * message over it closes the connection — which reconnects and imports again.
 * Each call stays well under it, measured in UTF-8 bytes.
 */
export const IMPORT_PAYLOAD_LIMIT = 4 * 1024 * 1024;

const utf8 = new TextEncoder();
const byteSize = (value: unknown) => utf8.encode(JSON.stringify(value)).length;

/**
 * One chat as import calls: one call, or for a long history several, each but
 * the last marked `partial` (the backend merges them by message id and marks
 * the chat imported on the last). Null when one message alone is over the
 * limit: that chat is not sent and stays in the store.
 */
export function importPayloads(unit: ImportUnit, limit = IMPORT_PAYLOAD_LIMIT): Array<Record<string, unknown>> | null {
    const id = unit.conversation_id;
    const chats = unit.record !== null && unit.record !== undefined ? { [id]: unit.record } : {};
    if (unit.kind === 'telegram' || !Array.isArray(unit.messages)) {
        const payload = {
            chats,
            histories: unit.kind === 'ai' && unit.messages !== undefined ? { [id]: unit.messages } : {},
        };
        return byteSize(payload) <= limit ? [payload] : null;
    }
    const overhead = byteSize({ chats, histories: { [id]: [] }, partial: true });
    const parts: unknown[][] = [[]];
    let size = overhead;
    for (const message of unit.messages) {
        const bytes = byteSize(message) + 1;
        if (overhead + bytes > limit) return null;
        if (size + bytes > limit) {
            parts.push([]);
            size = overhead;
        }
        parts[parts.length - 1].push(message);
        size += bytes;
    }
    return parts.map((messages, i) => ({
        chats: i === 0 ? chats : {},
        histories: { [id]: messages },
        ...(i < parts.length - 1 ? { partial: true } : {}),
    }));
}

function legacyStoreAtLoad(): boolean {
    try {
        return typeof localStorage !== 'undefined' && readLegacyChatStore(localStorage) !== null;
    } catch {
        return false;
    }
}

/**
 * True from page load until the import has run. An AI chat's history is not
 * fetched meanwhile: answered before its import, it would show the chat empty
 * and stay so (ChatHistorySyncPanel fetches a history once).
 */
export const legacyImportPending = writable<boolean>(legacyStoreAtLoad());

/** A `list_ai_chats` entry as the sidebar's `aiChats` map holds it. */
export function sidebarEntry(chat: any): [string, AIChatMeta] | null {
    const id = chat?.conversation_id;
    if (typeof id !== 'string' || !id) return null;
    if (chat.provider === 'telegram' || id.startsWith('telegram-')) {
        const telegramId = chat.telegram_chat_id ?? id.slice('telegram-'.length);
        return [id, {
            name: chat.name || `📱 Telegram (${telegramId})`,
            provider: 'telegram',
            instruction_set_name: 'general',
        }];
    }
    const meta: AIChatMeta = { name: chat.name || DEFAULT_AI_CHAT_NAME, provider: chat.provider ?? '' };
    for (const key of ['instruction_set_name', 'profile_name', 'llm_provider', 'compute_host'] as const) {
        if (typeof chat[key] === 'string' && chat[key]) meta[key] = chat[key];
    }
    return [id, meta];
}

/** Whether a chat's history is restored from the backend as an AI chat's. */
export function isAIChatId(chatId: string): boolean {
    return chatId === 'local_ai' || chatId.startsWith('ai_');
}
