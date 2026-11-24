import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { get } from 'svelte/store';
import player from '../components/music/MusicPlayer.svelte?raw';
import playerUtils from '../utils/musicPlayer.ts?raw';

// The library no longer re-reads on a timer: the core's `music_library_changed` event is the
// only trigger while the window is open, and a closed window reads nothing.

const h = vi.hoisted(() => {
    (globalThis as any).window = { localStorage: { getItem: () => null, setItem: () => {} } };
    return { reads: 0, exportReads: 0, deletes: [] as string[], deleteAnswer: { ok: true } as any };
});

vi.mock('$lib/coreService', () => {
    const ok = (data: unknown) => async () => ({ ok: true, data });
    const unavailable = async () => ({ ok: false, unavailable: true, message: '' });
    return {
        addMusicFolder: unavailable, createMusicPlaylist: unavailable, deleteMusicPlaylist: unavailable,
        removeMusicFolder: unavailable, rescanMusicFolder: unavailable, updateMusicPlaylist: unavailable,
        listMusicLibrary: async () => { h.reads += 1; return { ok: true, data: { entries: [] } }; },
        listMusicPlaylists: ok({ playlists: [] }),
        listMusicFolders: ok({ folders: [] }),
        getMusicExportInfo: async () => { h.exportReads += 1; return { ok: true, data: { exports: {} } }; },
        exportMusicTrack: unavailable, exportMusicPlaylist: unavailable,
        deleteMusicExport: async (id: string) => { h.deletes.push(id); return h.deleteAnswer; },
    };
});
vi.mock('$lib/logger', () => ({ log: { debug() {}, info() {}, warn() {}, error() {} } }));
vi.mock('$lib/utils/attachmentSrc', () => ({ detectWindows: () => true }));
vi.mock('$lib/utils/musicPlayer', async () => vi.importActual('../utils/musicPlayer'));
vi.mock('$lib/utils/musicExport', async () => vi.importActual('../utils/musicExport'));

const settle = async () => {
    for (let i = 0; i < 10; i++) await new Promise(r => setTimeout(r, 0));
};

async function fresh(status: 'ready' | 'idle' = 'ready') {
    vi.resetModules();
    h.reads = 0;
    h.exportReads = 0;
    h.deletes = [];
    h.deleteAnswer = { ok: true };
    const m = await import('./music');
    m.musicLibrary.set({ status, message: '', entries: [], playlists: [], folders: [] });
    return m;
}

describe('the library re-reads on the core event, not on a timer', () => {
    beforeEach(() => vi.useFakeTimers());
    afterEach(() => vi.useRealTimers());

    it('reads once per event while the window watches, and sets no timer', async () => {
        const m = await fresh();
        m.setMusicLibraryWatch(true);
        expect(vi.getTimerCount()).toBe(0);
        m.noteMusicLibraryChanged({ version: 1, reasons: ['scan'], track_ids: [] });
        await vi.advanceTimersByTimeAsync(0);
        expect(h.reads).toBe(1);
        await vi.advanceTimersByTimeAsync(10 * 60_000);
        expect(h.reads).toBe(1);
        expect(vi.getTimerCount()).toBe(0);
        m.noteMusicLibraryChanged({ version: 2, reasons: ['track'], track_ids: ['a'] });
        await vi.advanceTimersByTimeAsync(0);
        expect(h.reads).toBe(2);
    });

    it('reads nothing when the window is closed, minimised or not showing a read library', async () => {
        const m = await fresh();
        m.noteMusicLibraryChanged({ version: 1, reasons: ['scan'] });
        m.setMusicLibraryWatch(true);
        m.setMusicLibraryWatch(false);
        m.noteMusicLibraryChanged({ version: 2, reasons: ['scan'] });
        await vi.advanceTimersByTimeAsync(0);
        expect(h.reads).toBe(0);

        const unread = await fresh('idle');
        unread.setMusicLibraryWatch(true);
        unread.noteMusicLibraryChanged({ version: 3, reasons: ['scan'] });
        await vi.advanceTimersByTimeAsync(0);
        expect(h.reads).toBe(0);
        expect(get(unread.musicLibrary).status).toBe('idle');
    });
});

describe('the window has no poll left', () => {
    it('the component sets no interval for the library and tells the service when it watches', () => {
        expect(player).not.toContain('setInterval');
        expect(player).not.toContain('LIBRARY_POLL_MS');
        expect(playerUtils).not.toContain('LIBRARY_POLL_MS');
        expect(player).toContain('setMusicLibraryWatch(true)');
        expect(player).toContain('setMusicLibraryWatch(false)');
    });

    it('still reads on open and on a reconnect of the backend', () => {
        expect(player).toContain("untrack(() => loadMusicLibrary($musicLibrary.status === 'ready'))");
        expect(player).toContain("c === 'connected' && lastConnection && lastConnection !== 'connected') loadMusicLibrary()");
    });
});

describe('deleting an exported copy', () => {
    it('asks the core for the id and reads the exports again either way', async () => {
        vi.resetModules();
        await fresh();
        const e = await import('./musicExports');
        expect(await e.deleteExportedCopy('abc')).toBeNull();
        expect(h.deletes).toEqual(['abc']);
        expect(h.exportReads).toBe(1);

        h.deleteAnswer = { ok: false, unavailable: false, message: 'could not delete a.mp3: Windows refused' };
        expect(await e.deleteExportedCopy('abc')).toBe('could not delete a.mp3: Windows refused');
        expect(h.exportReads).toBe(2);

        h.deleteAnswer = { ok: false, unavailable: true, message: '' };
        expect(await e.deleteExportedCopy('abc')).toMatch(/not available on this backend/);
    });

    it('reads again after a read that was already under way, which may predate the delete', async () => {
        await fresh();
        const e = await import('./musicExports');
        const first = e.loadMusicExports();
        const second = e.loadMusicExports();
        await first;
        await second;
        await settle();
        expect(h.exportReads).toBe(2);
    });

    it('re-reads the exports on an event that names an export change and on no other', async () => {
        await fresh();
        const e = await import('./musicExports');
        e.noteMusicExportsChanged({ reasons: ['scan', 'hash'] });
        e.noteMusicExportsChanged(null);
        await settle();
        expect(h.exportReads).toBe(0);
        e.noteMusicExportsChanged({ reasons: ['export'] });
        await settle();
        expect(h.exportReads).toBe(1);
    });
});
