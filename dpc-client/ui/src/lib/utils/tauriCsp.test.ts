/**
 * The packaged app enforces tauri.conf.json's CSP (`tauri dev` serves through Vite and hides it).
 * Every URL the UI hands to an <audio> or <img> must be allowed there, per platform:
 * - Windows `convertFileSrc` gives http://asset.localhost/... (https:// only with useHttpsScheme,
 *   which is NOT set: flipping it moves the webview origin and strands localStorage);
 * - macOS and Linux give asset://localhost/...;
 * - the track stream is http://127.0.0.1:9996 (musicPlayer.ts), the voice player's file server
 *   is http://localhost:9998 (VoicePlayer.svelte).
 * The backend WebSocket is ws://127.0.0.1:9999 (coreService.ts), and there is no connect-src, so
 * default-src must name it: the host source ws://localhost:* does not cover 127.0.0.1.
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

    it('default-src (the fallback for connect-src) allows the backend socket ws://127.0.0.1:9999', () => {
        // coreService.ts API_URL; a CSP host source "localhost" does not match 127.0.0.1.
        expect(directive('default-src')).toContain('ws://127.0.0.1:9999');
    });

    it('keeps the webview origin: no useHttpsScheme on the main window', () => {
        const main = conf.app.windows.find((w: { label: string }) => w.label === 'main');
        expect(main.useHttpsScheme).toBeUndefined();
    });
});
