/**
 * The packaged app enforces tauri.conf.json's CSP (`tauri dev` serves through Vite and hides it).
 * Every URL the UI hands to an <audio> or <img> must be allowed there, per platform:
 * - Windows `convertFileSrc` gives http://asset.localhost/... (https:// only with useHttpsScheme,
 *   which is NOT set: flipping it moves the webview origin and strands localStorage);
 * - macOS and Linux give asset://localhost/...;
 * - the track stream is http://127.0.0.1:9996 (musicPlayer.ts), the voice player's file server
 *   is http://localhost:9998 (VoicePlayer.svelte).
 * The backend WebSocket is ws://127.0.0.1:9999 (coreService.ts), and Telegram pasted
 * screenshots in ChatPanel.svelte convert their data URL via fetch(dataUrl), so
 * connect-src must explicitly allow both (the host source ws://localhost:* does not
 * cover 127.0.0.1, and data: must never be placed in default-src).
 */

import { describe, it, expect } from 'vitest';
import confText from '../../../src-tauri/tauri.conf.json?raw';

const conf = JSON.parse(confText);
const csp: string = conf.app.security.csp;

function directive(name: string): string[] {
    const part = csp.split(';').map((s) => s.trim()).find((s) => s.startsWith(`${name} `));
    return part ? part.split(/\s+/).slice(1) : [];
}

describe('tauri.conf.json CSP', () => {
    const media = directive('media-src');
    const img = directive('img-src');
    const connect = directive('connect-src');
    const def = directive('default-src');

    it.each([
        'asset:',
        'http://asset.localhost',
        'https://asset.localhost',
        'http://127.0.0.1:9996',
        'http://localhost:9998',
        'blob:',
    ])('media-src allows %s', (origin) => {
        expect(media).toContain(origin);
    });

    it.each(['asset:', 'http://asset.localhost', 'https://asset.localhost', 'data:', 'blob:'])(
        'img-src allows %s',
        (origin) => {
            expect(img).toContain(origin);
        },
    );

    it('img-src does not allow http://localhost:* (blocks markdown image probing of local HTTP ports)', () => {
        expect(img).not.toContain('http://localhost:*');
    });

    it.each([
        "'self'",
        'data:',
        'http://localhost:*',
        'ws://localhost:*',
        'ws://127.0.0.1:9999',
    ])('connect-src allows %s', (origin) => {
        // data: is required for fetch(imageData.dataUrl) on Telegram pasted screenshots;
        // ws://127.0.0.1:9999 is coreService.ts API_URL (localhost does not match 127.0.0.1).
        expect(connect).toContain(origin);
    });

    it('default-src does not allow data: (data: scripts must remain blocked)', () => {
        expect(def).not.toContain('data:');
    });

    it('keeps the webview origin: no useHttpsScheme on the main window', () => {
        const main = conf.app.windows.find((w: { label: string }) => w.label === 'main');
        expect(main.useHttpsScheme).toBeUndefined();
    });
});
