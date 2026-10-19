import { describe, it, expect } from 'vitest';
import { LEGACY_CHAT_KEYS } from './aiChatStore';

// Pin for THE-UI-KEEPS-CHATS-AND-AI-CHAT-HISTORIES-IN-THE-WEBVIEW-OUTSIDE-DOT-DPC
// (Mike's call 2026-10-19, Variant A): the backend in ~/.dpc owns chats and their
// histories, and the WebView's localStorage keeps window preferences only. A new
// key has to be added here on purpose, by someone who can say it is a preference.
const UI_PREFERENCE_KEYS = new Set([
    'chatPanelHeight',          // ChatPanel: height of the chat pane
    'enableMarkdown',           // +page: markdown rendering toggle
    'dpc-cmd-chat-map',         // +page: which chat an in-flight command answers
    'dpc.activeTransfersPanel', // FileTransferUI: panel collapsed / position
    'notificationPermission',   // notificationService
    'notificationPreference',
    'permissionRequestedAt',
]);

const sources = import.meta.glob('/src/**/*.{ts,svelte}', {
    query: '?raw', import: 'default', eager: true,
}) as Record<string, string>;
const files = Object.entries(sources).filter(([path]) => !path.endsWith('.test.ts'));

const CALL = /localStorage\s*\.\s*(setItem|getItem|removeItem)\s*\(\s*([^,)]*)/g;

/** The key a call names: a string literal, or a `const NAME = '...'` in the same file. */
function resolveKey(arg: string, source: string): string | null {
    const literal = arg.trim().match(/^(['"`])([^'"`]*)\1$/);
    if (literal) return literal[2];
    const name = arg.trim();
    if (!/^[A-Za-z_$][\w$]*$/.test(name)) return null;
    const decl = source.match(new RegExp(`const\\s+${name}\\s*=\\s*(['"\`])([^'"\`]*)\\1`));
    return decl ? decl[2] : null;
}

function calls(kind: string) {
    const found: Array<{ path: string; key: string | null; arg: string }> = [];
    for (const [path, source] of files) {
        for (const m of source.matchAll(CALL)) {
            if (m[1] === kind) found.push({ path, key: resolveKey(m[2], source), arg: m[2].trim() });
        }
    }
    return found;
}

describe('localStorage holds UI preferences only', () => {
    it('finds the writes it is checking (the scan itself works)', () => {
        expect(calls('setItem').length).toBeGreaterThanOrEqual(5);
    });

    it('writes no key outside the preference allow-list', () => {
        const offending = calls('setItem').filter(c => !c.key || !UI_PREFERENCE_KEYS.has(c.key));
        expect(offending.map(c => `${c.path}: setItem(${c.arg})`)).toEqual([]);
    });

    it('writes through no other storage handle, except the import shrinking its own keys', () => {
        // `storage.setItem(key, …)` in aiChatStore.ts is the remainder write;
        // its tests hold that it only ever writes a subset of what it read.
        const other = files.flatMap(([path, source]) =>
            [...source.matchAll(/([A-Za-z_$][\w$]*)\s*\.\s*setItem\s*\(/g)]
                .filter(m => m[1] !== 'localStorage')
                .map(m => `${path}: ${m[1]}.setItem`));
        expect(other).toEqual(['/src/lib/utils/aiChatStore.ts: storage.setItem']);
    });

    it('reads the old chat keys only in the one-time import', () => {
        const readers = files
            .filter(([, source]) => LEGACY_CHAT_KEYS.some(k => source.includes(`'${k}'`) || source.includes(`"${k}"`)))
            .map(([path]) => path);
        expect(readers).toEqual(['/src/lib/utils/aiChatStore.ts']);
    });

    it('reaches storage no other way than the three calls', () => {
        const other = files.filter(([, source]) =>
            /localStorage\s*\[/.test(source) || /sessionStorage|indexedDB/.test(source));
        expect(other.map(([path]) => path)).toEqual([]);
    });
});
