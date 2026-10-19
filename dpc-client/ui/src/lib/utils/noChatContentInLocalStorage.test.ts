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

// What each allow-listed key may be given, call site by call site. Pinning the
// key name alone left a hole: `setItem('enableMarkdown', JSON.stringify(history))`
// passes a key check and puts message text back in the WebView. The value
// expression is pinned too (whitespace ignored), so a new or changed value has
// to be added here on purpose, by someone who can say it is a small preference
// and not chat content. Each of these is a number, a flag, a timestamp, a
// panel position or a map of ids.
const UI_PREFERENCE_VALUES: Record<string, string[]> = {
    chatPanelHeight: ['chatPanelHeight.toString()'],
    enableMarkdown: ['enableMarkdown.toString()'],
    'dpc-cmd-chat-map': ['JSON.stringify([...commandToChatMap.entries()])'],
    'dpc.activeTransfersPanel': ['JSON.stringify({collapsed:panelCollapsed,pos:panelPos})'],
    notificationPermission: ['permission'],
    notificationPreference: ["permissionGranted?'enabled':'disabled'"],
    permissionRequestedAt: ['newDate().toISOString()'],
};

const sources = import.meta.glob('/src/**/*.{ts,js,svelte}', {
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

/** The text of the two arguments of every `localStorage.setItem(` in `source`, brackets balanced. */
function setItemArguments(source: string): Array<{ key: string; value: string }> {
    const out: Array<{ key: string; value: string }> = [];
    for (const m of source.matchAll(/localStorage\s*\.\s*setItem\s*\(/g)) {
        const parts: string[] = [];
        let depth = 0;
        let start = m.index! + m[0].length;
        let quote: string | null = null;
        for (let i = start; i < source.length; i++) {
            const c = source[i];
            if (quote) {
                if (c === '\\') i++;
                else if (c === quote) quote = null;
            } else if (c === "'" || c === '"' || c === '`') {
                quote = c;
            } else if ('([{'.includes(c)) {
                depth++;
            } else if (')]}'.includes(c)) {
                if (depth === 0) {
                    parts.push(source.slice(start, i));
                    break;
                }
                depth--;
            } else if (c === ',' && depth === 0) {
                parts.push(source.slice(start, i));
                start = i + 1;
            }
        }
        out.push({ key: parts[0]?.trim() ?? '', value: (parts[1] ?? '').replace(/\s+/g, '') });
    }
    return out;
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

    it('gives each allow-listed key only the value expression listed for it', () => {
        const offending: string[] = [];
        for (const [path, source] of files) {
            for (const { key, value } of setItemArguments(source)) {
                const name = resolveKey(key, source);
                const allowed = name ? UI_PREFERENCE_VALUES[name] : undefined;
                if (!allowed || !allowed.includes(value)) offending.push(`${path}: setItem(${key}, ${value})`);
            }
        }
        expect(offending).toEqual([]);
    });

    it('lists a value expression for every allow-listed key and no other', () => {
        expect(Object.keys(UI_PREFERENCE_VALUES).sort()).toEqual([...UI_PREFERENCE_KEYS].sort());
    });

    it('catches message text stored under an allow-listed key (the check itself works)', () => {
        const bad = `localStorage.setItem('enableMarkdown', JSON.stringify(messages));`;
        const [call] = setItemArguments(bad);
        expect(UI_PREFERENCE_VALUES[resolveKey(call.key, bad)!]).not.toContain(call.value);
        const ok = `localStorage.setItem(
 'chatPanelHeight',
 chatPanelHeight.toString());`;
        const [good] = setItemArguments(ok);
        expect(UI_PREFERENCE_VALUES[resolveKey(good.key, ok)!]).toContain(good.value);
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
