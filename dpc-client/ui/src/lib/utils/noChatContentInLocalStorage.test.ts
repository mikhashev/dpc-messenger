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
    'dpc.providersEditor.expanded', // ProvidersEditor: which provider cards are unfolded (aliases)
    'notificationPermission',   // notificationService
    'notificationPreference',
    'permissionRequestedAt',
    // Floating windows (utils/windowGeometry.ts, `saveWindowState`): each window's
    // box and minimised flag under its WindowSpec key. Written through a
    // `storage?.setItem(spec.key, ...)` handle the scan did not see until it learned `?.`.
    'dpc.agentMap.window',          // AgentMap
    'dpc.groupSettings.window',     // GroupSettingsDialog
    'dpc.musicPlayer.window',       // MusicPlayer
    'dpc.musicPlayer.preferences',  // MusicPlayer: shuffle, repeat, volume (never the library)
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
    'dpc.providersEditor.expanded': ['JSON.stringify([...storedExpanded])'],
    notificationPermission: ['permission'],
    notificationPreference: ["permissionGranted?'enabled':'disabled'"],
    permissionRequestedAt: ['newDate().toISOString()'],
    'dpc.agentMap.window': ['JSON.stringify(state)'],
    'dpc.groupSettings.window': ['JSON.stringify(state)'],
    'dpc.musicPlayer.window': ['JSON.stringify(state)'],
    'dpc.musicPlayer.preferences': ['JSON.stringify(prefs)'],
};

const sources = import.meta.glob('/src/**/*.{ts,js,svelte}', {
    query: '?raw', import: 'default', eager: true,
}) as Record<string, string>;
const files = Object.entries(sources).filter(([path]) => !path.endsWith('.test.ts'));

// A storage call through any handle, plain or optional (`localStorage.setItem(`,
// `storage?.setItem(`). The optional chain hid two writers until 2026-11.
const CALL = /([A-Za-z_$][\w$]*)\s*\??\.\s*(setItem|getItem|removeItem)\s*\(\s*([^,)]*)/g;
const SET_ITEM = /([A-Za-z_$][\w$]*)\s*\??\.\s*setItem\s*\(/g;

// `storage.setItem(key, ...)` in aiChatStore.ts is the remainder write of the
// one-time import; its tests hold that it only ever writes a subset of what it read.
const isImportRemainder = (path: string, handle: string) =>
    path === '/src/lib/utils/aiChatStore.ts' && handle === 'storage';

type Sources = Array<[string, string]>;

/** Every `key:` a `WindowSpec` is declared with, across the sources. */
function windowSpecKeys(all: Sources): string[] {
    const keys: string[] = [];
    for (const [, source] of all) {
        for (const m of source.matchAll(/:\s*WindowSpec\s*=\s*\{\s*key\s*:\s*([^,\n}]+)/g)) {
            keys.push(resolveKey(m[1], source) ?? `<unresolved ${m[1].trim()}>`);
        }
    }
    return keys;
}

/** The keys a call may name: one literal or same-file const, or, for `spec.key`
 *  where `spec: WindowSpec`, every declared WindowSpec key. Null when unknown. */
function resolveKeys(arg: string, source: string, all: Sources): string[] | null {
    const one = resolveKey(arg, source);
    if (one !== null) return [one];
    const member = arg.trim().match(/^([A-Za-z_$][\w$]*)\.key$/);
    if (member && new RegExp(`\\b${member[1]}\\s*:\\s*WindowSpec\\b`).test(source)) return windowSpecKeys(all);
    return null;
}

/** The key a call names: a string literal, or a `const NAME = '...'` in the same file. */
function resolveKey(arg: string, source: string): string | null {
    const literal = arg.trim().match(/^(['"`])([^'"`]*)\1$/);
    if (literal) return literal[2];
    const name = arg.trim();
    if (!/^[A-Za-z_$][\w$]*$/.test(name)) return null;
    const decl = source.match(new RegExp(`const\\s+${name}\\s*=\\s*(['"\`])([^'"\`]*)\\1`));
    return decl ? decl[2] : null;
}

/** The handle and the text of the two arguments of every `<handle>.setItem(` or
 *  `<handle>?.setItem(` in `source`, brackets balanced. */
function setItemArguments(source: string): Array<{ handle: string; key: string; value: string }> {
    const out: Array<{ handle: string; key: string; value: string }> = [];
    for (const m of source.matchAll(SET_ITEM)) {
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
        out.push({ handle: m[1], key: parts[0]?.trim() ?? '', value: (parts[1] ?? '').replace(/\s+/g, '') });
    }
    return out;
}

function calls(kind: string, all: Sources = files) {
    const found: Array<{ path: string; handle: string; keys: string[] | null; arg: string }> = [];
    for (const [path, source] of all) {
        for (const m of source.matchAll(CALL)) {
            if (m[2] !== kind || isImportRemainder(path, m[1])) continue;
            found.push({ path, handle: m[1], keys: resolveKeys(m[3], source, all), arg: m[3].trim() });
        }
    }
    return found;
}

/** The setItem calls that break the allow-list (key or value), as `path: handle.setItem(key, value)`. */
function offendingWrites(all: Sources): string[] {
    const offending: string[] = [];
    for (const [path, source] of all) {
        for (const { handle, key, value } of setItemArguments(source)) {
            if (isImportRemainder(path, handle)) continue;
            const names = resolveKeys(key, source, all);
            const ok = !!names && names.length > 0
                && names.every(n => UI_PREFERENCE_KEYS.has(n) && (UI_PREFERENCE_VALUES[n] ?? []).includes(value));
            if (!ok) offending.push(`${path}: ${handle}.setItem(${key}, ${value})`);
        }
    }
    return offending;
}

describe('localStorage holds UI preferences only', () => {
    it('finds the writes it is checking (the scan itself works)', () => {
        expect(calls('setItem').length).toBeGreaterThanOrEqual(5);
    });

    it('writes no key outside the preference allow-list', () => {
        const offending = calls('setItem').filter(c => !c.keys?.length || !c.keys.every(k => UI_PREFERENCE_KEYS.has(k)));
        expect(offending.map(c => `${c.path}: setItem(${c.arg})`)).toEqual([]);
    });

    it('gives each allow-listed key only the value expression listed for it', () => {
        expect(offendingWrites(files)).toEqual([]);
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

    it('writes through no other storage handle than these, each one listed on purpose', () => {
        // The import's remainder write (aiChatStore.ts), and the two helpers that
        // are handed `window.localStorage`: window geometry and the music
        // preferences. The last two are held to the allow-list above.
        const other = files.flatMap(([path, source]) =>
            [...source.matchAll(SET_ITEM)]
                .filter(m => m[1] !== 'localStorage')
                .map(m => `${path}: ${m[1]}.setItem`));
        expect(other.sort()).toEqual([
            '/src/lib/utils/aiChatStore.ts: storage.setItem',
            '/src/lib/utils/musicPlayer.ts: storage.setItem',
            '/src/lib/utils/windowGeometry.ts: storage.setItem',
        ]);
    });

    it('resolves every WindowSpec key it holds windows to', () => {
        expect(windowSpecKeys(files).sort()).toEqual(['dpc.agentMap.window', 'dpc.groupSettings.window', 'dpc.musicPlayer.window']);
    });

    it('catches an optional-chain write of an unknown key (the scan sees `?.`)', () => {
        const fixture: Sources = [
            ['/fixture/a.ts', `function f(storage) { storage?.setItem('dpc.chat.history', JSON.stringify(messages)); }`],
            ['/fixture/b.ts', `localStorage?.setItem('dpc.chat.drafts', text);`],
            ['/fixture/c.ts', `const K = 'enableMarkdown';\nwindow.localStorage ?. setItem(K, JSON.stringify(history));`],
        ];
        expect(calls('setItem', fixture).map(c => `${c.handle}:${c.arg}`))
            .toEqual(["storage:'dpc.chat.history'", "localStorage:'dpc.chat.drafts'", 'localStorage:K']);
        expect(offendingWrites(fixture)).toEqual([
            "/fixture/a.ts: storage.setItem('dpc.chat.history', JSON.stringify(messages))",
            "/fixture/b.ts: localStorage.setItem('dpc.chat.drafts', text)",
            '/fixture/c.ts: localStorage.setItem(K, JSON.stringify(history))',
        ]);
        expect(calls('getItem', [['/fixture/d.ts', `storage?.getItem('x')`]]).map(c => c.arg)).toEqual(["'x'"]);
        expect(calls('removeItem', [['/fixture/e.ts', `storage?.removeItem('x')`]]).map(c => c.arg)).toEqual(["'x'"]);
    });

    it('holds a WindowSpec write to the allow-list too (a new window key is caught)', () => {
        const fixture: Sources = [
            ['/fixture/geom.ts', `export function save(storage, state, spec: WindowSpec) { storage?.setItem(spec.key, JSON.stringify(state)); }`],
            ['/fixture/win.ts', `export const CHAT_WINDOW: WindowSpec = {\n    key: 'dpc.chatLog.window',\n    limits: {} };`],
        ];
        expect(offendingWrites(fixture)).toEqual(['/fixture/geom.ts: storage.setItem(spec.key, JSON.stringify(state))']);
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
