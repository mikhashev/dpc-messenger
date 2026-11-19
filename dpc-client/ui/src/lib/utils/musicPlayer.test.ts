import { describe, it, expect, vi } from 'vitest';
import {
    EMPTY_QUEUE,
    MISSING_TEXT,
    MUSIC_PLAYER_WINDOW,
    MUSIC_PREFERENCES_KEY,
    MUSIC_UNAVAILABLE_TEXT,
    NOT_CONNECTED_TEXT,
    NOTHING_PLAYABLE_TEXT,
    OUTSIDE_DPC_TEXT,
    PREV_RESTART_S,
    QUEUE_EMPTY_TEXT,
    WEB_TEXT,
    alignQueue,
    callMusic,
    currentId,
    cycleRepeat,
    dpcRelativePath,
    dropIndex,
    enqueue,
    formatTime,
    isUnknownCommandError,
    loadMusicPreferences,
    matchesFilter,
    moveItem,
    nextPlaylistName,
    onTrackEnded,
    playbackProblem,
    positionLabel,
    queueClear,
    queueFromList,
    queueRemoveAt,
    saveMusicPreferences,
    shuffleOff,
    shuffleOn,
    stepQueue,
    throttle,
    trackPlaybackUrl,
    type LibraryEntry,
    type PlaybackEnv,
    type Playlist,
    type QueueState,
} from './musicPlayer';
import { HEADER_HEIGHT, loadWindowState, saveWindowState } from './windowGeometry';

const all = () => true;
const q = (ids: string[], index = 0, context: string | null = null): QueueState => ({ ids, index, context });
/** A random source that walks a fixed list, so a shuffle is repeatable. */
const seq = (...values: number[]) => {
    let i = 0;
    return () => values[i++ % values.length];
};

describe('Next and Prev', () => {
    it('Next moves one track on', () => {
        expect(stepQueue(q(['a', 'b', 'c'], 0), 1, { repeat: 'off', playable: all })).toEqual({ kind: 'play', index: 1, skipped: [] });
    });

    it('Next at the end stops with repeat off, and wraps with repeat all', () => {
        expect(stepQueue(q(['a', 'b'], 1), 1, { repeat: 'off', playable: all })).toMatchObject({ kind: 'end' });
        expect(stepQueue(q(['a', 'b'], 1), 1, { repeat: 'all', playable: all })).toEqual({ kind: 'play', index: 0, skipped: [] });
    });

    it('Next still moves with repeat one (only the end of a track loops)', () => {
        expect(stepQueue(q(['a', 'b'], 0), 1, { repeat: 'one', playable: all })).toMatchObject({ kind: 'play', index: 1 });
    });

    it('Prev goes back within the first 3 s, and restarts the track after them', () => {
        expect(PREV_RESTART_S).toBe(3);
        expect(stepQueue(q(['a', 'b'], 1), -1, { repeat: 'off', playable: all, currentTime: 2.9 })).toMatchObject({ kind: 'play', index: 0 });
        expect(stepQueue(q(['a', 'b'], 1), -1, { repeat: 'off', playable: all, currentTime: 3.1 })).toEqual({ kind: 'restart' });
    });

    it('Prev on the first track restarts it with repeat off, and wraps to the last with repeat all', () => {
        expect(stepQueue(q(['a', 'b'], 0), -1, { repeat: 'off', playable: all })).toEqual({ kind: 'restart' });
        expect(stepQueue(q(['a', 'b'], 0), -1, { repeat: 'all', playable: all })).toMatchObject({ kind: 'play', index: 1 });
    });

    it('skips a track that cannot play and says which', () => {
        const r = stepQueue(q(['a', 'b', 'c'], 0), 1, { repeat: 'off', playable: id => id !== 'b' });
        expect(r).toEqual({ kind: 'play', index: 2, skipped: ['b'] });
    });

    it('an empty queue says so; a queue with nothing playable says so', () => {
        expect(stepQueue(EMPTY_QUEUE, 1, { repeat: 'off', playable: all })).toEqual({ kind: 'empty', message: QUEUE_EMPTY_TEXT });
        expect(stepQueue(q(['a', 'b'], 0), 1, { repeat: 'all', playable: () => false }))
            .toEqual({ kind: 'nothing-playable', message: NOTHING_PLAYABLE_TEXT });
    });
});

describe('auto-advance at the end of a track', () => {
    it('moves to the next track', () => {
        expect(onTrackEnded(q(['a', 'b'], 0), 'off', all)).toMatchObject({ kind: 'play', index: 1 });
    });

    it('stops at the end of the queue with repeat off, saying it ended by itself', () => {
        expect(onTrackEnded(q(['a', 'b'], 1), 'off', all)).toEqual({ kind: 'end', message: 'End of the queue (repeat is off).' });
    });

    it('wraps with repeat all', () => {
        expect(onTrackEnded(q(['a', 'b'], 1), 'all', all)).toMatchObject({ kind: 'play', index: 0 });
    });

    it('plays the same track again with repeat one', () => {
        expect(onTrackEnded(q(['a', 'b'], 0), 'one', all)).toEqual({ kind: 'restart' });
    });

    it('is never a restart of the current track past PREV_RESTART_S: that rule is for a press only', () => {
        expect(stepQueue(q(['a', 'b'], 1), -1, { repeat: 'off', auto: true, currentTime: 100, playable: all })).toMatchObject({ kind: 'play', index: 0 });
    });
});

describe('repeat', () => {
    it('cycles off → all → one → off', () => {
        expect(cycleRepeat('off')).toBe('all');
        expect(cycleRepeat('all')).toBe('one');
        expect(cycleRepeat('one')).toBe('off');
    });
});

describe('shuffle', () => {
    it('on: shuffles only what comes after the current track', () => {
        const s = shuffleOn(q(['a', 'b', 'c', 'd', 'e'], 1), seq(0, 0, 0));
        expect(s.ids.slice(0, 2)).toEqual(['a', 'b']);
        expect([...s.ids.slice(2)].sort()).toEqual(['c', 'd', 'e']);
        expect(s.ids.slice(2)).not.toEqual(['c', 'd', 'e']);
        expect(s.index).toBe(1);
    });

    it('off: back to the list order at the current track when the queue came from that list', () => {
        const r = shuffleOff(q(['c', 'a', 'd', 'b'], 1, 'pl_1'), ['a', 'b', 'c', 'd']);
        expect(r.restored).toBe(true);
        expect(r.queue.ids).toEqual(['a', 'b', 'c', 'd']);
        expect(currentId(r.queue)).toBe('a');
    });

    it('off: the queue stays when there is no list or the current track left it', () => {
        const queue = q(['c', 'a'], 0);
        expect(shuffleOff(queue, null)).toEqual({ queue, restored: false });
        expect(shuffleOff(queue, ['a', 'b'])).toEqual({ queue, restored: false });
    });
});

describe('a queue made from a list', () => {
    it('starts at the first playable track, in order', () => {
        const r = queueFromList('pl', ['a', 'b', 'c'], { shuffle: false, playable: id => id !== 'a' });
        expect(r).toEqual({ ids: ['a', 'b', 'c'], index: 1, context: 'pl' });
    });

    it('starts at the track asked for', () => {
        expect(queueFromList('pl', ['a', 'b', 'c'], { startId: 'c', shuffle: false, playable: all })).toMatchObject({ index: 2 });
    });

    it('with shuffle the start comes first and the rest follows shuffled', () => {
        const r = queueFromList('pl', ['a', 'b', 'c', 'd'], { startId: 'c', shuffle: true, playable: all, random: seq(0) })!;
        expect(r.ids[0]).toBe('c');
        expect(r.index).toBe(0);
        expect([...r.ids].sort()).toEqual(['a', 'b', 'c', 'd']);
    });

    it('is null when nothing in the list can play', () => {
        expect(queueFromList('pl', ['a'], { shuffle: false, playable: () => false })).toBeNull();
        expect(queueFromList('pl', [], { shuffle: false, playable: all })).toBeNull();
    });
});

describe('Up next', () => {
    it('Queue adds at the end; "next" inserts after the current track; a waiting track moves', () => {
        expect(enqueue(q(['a', 'b', 'c'], 0), ['d']).ids).toEqual(['a', 'b', 'c', 'd']);
        expect(enqueue(q(['a', 'b', 'c'], 0), ['d'], true).ids).toEqual(['a', 'd', 'b', 'c']);
        expect(enqueue(q(['a', 'b', 'c'], 0), ['b']).ids).toEqual(['a', 'c', 'b']);
    });

    it('never adds the current track again', () => {
        expect(enqueue(q(['a', 'b'], 0), ['a']).ids).toEqual(['a', 'b']);
    });

    it('into an empty queue: the track becomes the current one', () => {
        expect(enqueue(EMPTY_QUEUE, ['x'])).toEqual({ ids: ['x'], index: 0, context: null });
    });

    it('removing a row keeps the current track current; the current row cannot be removed', () => {
        expect(queueRemoveAt(q(['a', 'b', 'c'], 2), 0)).toEqual(q(['b', 'c'], 1));
        expect(queueRemoveAt(q(['a', 'b', 'c'], 1), 1)).toEqual(q(['a', 'b', 'c'], 1));
    });

    it('Clear keeps only the current track', () => {
        expect(queueClear(q(['a', 'b', 'c'], 1, 'pl'))).toEqual(q(['b'], 0, 'pl'));
        expect(queueClear(EMPTY_QUEUE)).toEqual(EMPTY_QUEUE);
    });

    it('align points at a later copy first, else inserts after the current track', () => {
        expect(alignQueue(q(['a', 'b', 'a'], 1), 'a').index).toBe(2);
        expect(alignQueue(q(['a', 'b'], 0), 'z')).toEqual(q(['a', 'z', 'b'], 1));
    });

    it('"k / n" for the mini-player', () => {
        expect(positionLabel(q(['a', 'b', 'c'], 1))).toBe('2 / 3');
        expect(positionLabel(EMPTY_QUEUE)).toBe('');
    });
});

describe('playlist reorder', () => {
    it('moves one item, clamped to the ends', () => {
        expect(moveItem(['a', 'b', 'c'], 0, 2)).toEqual(['b', 'c', 'a']);
        expect(moveItem(['a', 'b', 'c'], 2, 0)).toEqual(['c', 'a', 'b']);
        expect(moveItem(['a', 'b', 'c'], 0, -1)).toEqual(['a', 'b', 'c']);
        expect(moveItem(['a', 'b', 'c'], 2, 9)).toEqual(['a', 'b', 'c']);
    });

    it('a drag lands by the rows\' midpoints', () => {
        const mids = [10, 30, 50, 70];
        expect(dropIndex(mids, 25, 0)).toBe(0); // above row 1's middle: stays
        expect(dropIndex(mids, 35, 0)).toBe(1);
        expect(dropIndex(mids, 99, 0)).toBe(3);
        expect(dropIndex(mids, 0, 3)).toBe(0);
        expect(dropIndex(mids, 60, 3)).toBe(3);
        expect(dropIndex(mids, 45, 3)).toBe(2);
    });

    it('a new playlist gets a free "Playlist N" name', () => {
        const p = (name: string, kind: 'user' | 'smart' = 'user'): Playlist =>
            ({ id: name, name, kind, track_ids: [], created_at: '', updated_at: '' });
        expect(nextPlaylistName([p('All tracks', 'smart')])).toBe('Playlist 1');
        expect(nextPlaylistName([p('Playlist 2'), p('Evening')])).toBe('Playlist 3');
    });
});

describe('the window geometry is remembered under its own key', () => {
    function memory() {
        const m = new Map<string, string>();
        return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v), m };
    }

    it('restores the saved box and minimised flag, fitted to the viewport', () => {
        const s = memory();
        saveWindowState(s, { rect: { x: 100, y: 80, width: 700, height: 500 }, minimised: true }, MUSIC_PLAYER_WINDOW);
        expect([...s.m.keys()]).toEqual(['dpc.musicPlayer.window']);
        const w = loadWindowState(s, { width: 1600, height: 900 }, MUSIC_PLAYER_WINDOW);
        expect(w).toEqual({ rect: { x: 100, y: 80, width: 700, height: 500 }, minimised: true });
    });

    it('a minimised mini-player is pulled back so its bar stays in view', () => {
        const s = memory();
        saveWindowState(s, { rect: { x: 100, y: 2000, width: 700, height: 500 }, minimised: true }, MUSIC_PLAYER_WINDOW);
        const w = loadWindowState(s, { width: 1600, height: 900 }, MUSIC_PLAYER_WINDOW);
        expect(w.rect.y).toBe(900 - HEADER_HEIGHT);
    });

    it('nothing saved: the default box, within its own limits', () => {
        const w = loadWindowState(memory(), { width: 1600, height: 900 }, MUSIC_PLAYER_WINDOW);
        expect(w.minimised).toBe(false);
        expect(w.rect.width).toBe(MUSIC_PLAYER_WINDOW.limits.defaultWidth);
        expect(w.rect.height).toBe(MUSIC_PLAYER_WINDOW.limits.defaultHeight);
    });

    it('shuffle, repeat and volume are kept; bad or missing text gives the defaults', () => {
        const s = memory();
        saveMusicPreferences(s, { shuffle: true, repeat: 'one', volume: 0.4 });
        expect(s.m.has(MUSIC_PREFERENCES_KEY)).toBe(true);
        expect(loadMusicPreferences(s)).toEqual({ shuffle: true, repeat: 'one', volume: 0.4 });
        s.setItem(MUSIC_PREFERENCES_KEY, '{nope');
        expect(loadMusicPreferences(s)).toEqual({ shuffle: false, repeat: 'off', volume: 1 });
        expect(loadMusicPreferences(null)).toEqual({ shuffle: false, repeat: 'off', volume: 1 });
        const throwing = { getItem: () => { throw new Error('blocked'); }, setItem: () => { throw new Error('blocked'); } };
        expect(loadMusicPreferences(throwing)).toEqual({ shuffle: false, repeat: 'off', volume: 1 });
        expect(() => saveMusicPreferences(throwing, { shuffle: false, repeat: 'off', volume: 1 })).not.toThrow();
    });
});

describe('the playback source (ADR-051 N8 open: only ~/.dpc plays today)', () => {
    const entry = (path: string | null, extra: Partial<LibraryEntry> = {}): LibraryEntry => ({
        id: 'trk_0000000000000001', title: 'Night bus', duration_s: 200, origin: 'folder', path, missing: false,
        source: { kind: 'local', name: 'night-bus.flac', uri: 'file:///x', mime: 'audio/flac' }, ...extra,
    });
    const win = (over: Partial<PlaybackEnv> = {}): PlaybackEnv => ({
        tauri: true, home: 'C:\\Users\\mike', isWindows: true, isLinux: false,
        convertFileSrc: (p) => `http://asset.localhost/${encodeURIComponent(p)}`, ...over,
    });
    const linux = (over: Partial<PlaybackEnv> = {}): PlaybackEnv => ({
        tauri: true, home: '/home/mike', isWindows: false, isLinux: true,
        readHomeFile: async () => new Uint8Array([1, 2, 3]),
        createObjectURL: () => 'blob:abc', revokeObjectURL: vi.fn(), ...over,
    });

    it('in scope: a path under home/.dpc, relative to home; Windows without case or slash style', () => {
        expect(dpcRelativePath('C:\\Users\\mike\\.dpc\\music\\a.flac', 'C:\\Users\\mike', true)).toBe('.dpc/music/a.flac');
        expect(dpcRelativePath('c:/users/MIKE/.DPC/music/a.flac', 'C:\\Users\\mike', true)).toBe('.DPC/music/a.flac'); // the path keeps its own case
        expect(dpcRelativePath('/home/mike/.dpc/music/a.flac', '/home/mike/', false)).toBe('.dpc/music/a.flac');
    });

    it('out of scope: another folder, a climb out of .dpc, a look-alike, the .dpc folder itself, case on POSIX', () => {
        expect(dpcRelativePath('D:\\Music\\a.flac', 'C:\\Users\\mike', true)).toBeNull();
        expect(dpcRelativePath('C:\\Users\\mike\\.dpc\\..\\Music\\a.flac', 'C:\\Users\\mike', true)).toBeNull();
        expect(dpcRelativePath('C:\\Users\\mike\\.dpc-old\\a.flac', 'C:\\Users\\mike', true)).toBeNull();
        expect(dpcRelativePath('C:\\Users\\mike\\.dpc', 'C:\\Users\\mike', true)).toBeNull();
        expect(dpcRelativePath('/home/Mike/.dpc/a.flac', '/home/mike', false)).toBeNull();
        expect(dpcRelativePath('/home/mike/.dpc/a.flac', null, false)).toBeNull();
    });

    it('a track under ~/.dpc plays through convertFileSrc on Windows and macOS', async () => {
        const r = await trackPlaybackUrl(entry('C:\\Users\\mike\\.dpc\\music\\take.wav'), win());
        expect(r).toMatchObject({ ok: true, via: 'asset' });
        expect(r.ok && r.url).toBe(`http://asset.localhost/${encodeURIComponent('C:\\Users\\mike\\.dpc\\music\\take.wav')}`);
    });

    it('on Linux a track under ~/.dpc is read through the fs plugin into a blob: URL, freed on release', async () => {
        const readHomeFile = vi.fn(async (_relative: string) => new Uint8Array([1, 2, 3]));
        const env = linux({ readHomeFile });
        const r = await trackPlaybackUrl(entry('/home/mike/.dpc/music/take.wav'), env);
        expect(r).toMatchObject({ ok: true, via: 'blob', url: 'blob:abc' });
        expect(readHomeFile).toHaveBeenCalledWith('.dpc/music/take.wav');
        if (r.ok) r.release();
        expect(env.revokeObjectURL).toHaveBeenCalledWith('blob:abc');
    });

    it('on Linux a read that fails is a clear "cannot", not a throw', async () => {
        const r = await trackPlaybackUrl(entry('/home/mike/.dpc/a.wav'), linux({ readHomeFile: async () => { throw new Error('forbidden path'); } }));
        expect(r).toMatchObject({ ok: false, problem: 'read-failed' });
        expect(!r.ok && r.reason).toContain('forbidden path');
    });

    it('a track outside ~/.dpc is "not playable yet (outside the DPC folder)", with nothing read or converted', async () => {
        const convert = vi.fn(() => 'asset://x');
        const r = await trackPlaybackUrl(entry('D:\\Music\\demos\\night-bus.flac'), win({ convertFileSrc: convert }));
        expect(r).toEqual({ ok: false, problem: 'outside-dpc', reason: OUTSIDE_DPC_TEXT });
        expect(convert).not.toHaveBeenCalled();
        const readHomeFile = vi.fn(async () => new Uint8Array());
        expect(await trackPlaybackUrl(entry('/home/mike/Music/a.flac'), linux({ readHomeFile })))
            .toMatchObject({ ok: false, problem: 'outside-dpc' });
        expect(readHomeFile).not.toHaveBeenCalled();
    });

    it('a missing track, a web track, no file and no desktop app each say why', async () => {
        expect(await trackPlaybackUrl(entry('C:\\Users\\mike\\.dpc\\a.wav', { missing: true }), win()))
            .toEqual({ ok: false, problem: 'missing', reason: MISSING_TEXT });
        expect(await trackPlaybackUrl(entry(null, { origin: 'web', source: { kind: 'web', name: 'x', uri: 'https://x' } }), win()))
            .toEqual({ ok: false, problem: 'web', reason: WEB_TEXT });
        expect(await trackPlaybackUrl(entry(null), win())).toMatchObject({ ok: false, problem: 'no-file' });
        expect(await trackPlaybackUrl(entry('C:\\Users\\mike\\.dpc\\a.wav'), win({ tauri: false }))).toMatchObject({ ok: false, problem: 'no-desktop' });
        expect(await trackPlaybackUrl(null, win())).toMatchObject({ ok: false, problem: 'missing' });
    });

    it('the row check agrees with the source', () => {
        expect(playbackProblem(entry('C:\\Users\\mike\\.dpc\\a.wav'), win())).toBeNull();
        expect(playbackProblem(entry('D:\\a.wav'), win())?.problem).toBe('outside-dpc');
        expect(playbackProblem(entry('C:\\Users\\mike\\.dpc\\a.wav'), win({ home: null }))?.problem).toBe('outside-dpc');
    });
});

describe('a backend without the music commands', () => {
    it('recognises local_api\'s refusal of an unknown command', () => {
        expect(isUnknownCommandError(new Error('Unknown or non-async command: list_music_library'))).toBe(true);
        expect(isUnknownCommandError(new Error('Command \'x\' timed out after 60 seconds'))).toBe(false);
    });

    it('answers "unavailable" with the sentence the window shows, and never throws', async () => {
        const send = () => Promise.reject(new Error('Unknown or non-async command: list_music_library'));
        expect(await callMusic(send, 'list_music_library')).toEqual({
            ok: false, unavailable: true, code: 'unknown_command', message: MUSIC_UNAVAILABLE_TEXT,
        });
        expect(MUSIC_UNAVAILABLE_TEXT).toBe('Music library not available on this backend yet');
    });

    it('a lost socket is "not connected", not "unavailable"', async () => {
        expect(await callMusic(() => false, 'list_music_library')).toEqual({
            ok: false, unavailable: false, code: 'not_connected', message: NOT_CONNECTED_TEXT,
        });
    });

    it('the core\'s ok and error answers keep their data, code and message', async () => {
        expect(await callMusic(async () => ({ status: 'ok', entries: [1] }), 'list_music_library'))
            .toEqual({ ok: true, data: { entries: [1] } });
        expect(await callMusic(async () => ({ status: 'error', code: 'already_added', message: 'already a watched folder: D:\\Music' }), 'add_music_folder'))
            .toEqual({ ok: false, unavailable: false, code: 'already_added', message: 'already a watched folder: D:\\Music' });
        expect(await callMusic(async () => 'nonsense', 'list_music_library')).toMatchObject({ ok: false, code: 'bad_answer' });
        expect(await callMusic(() => Promise.reject(new Error('timed out')), 'x')).toMatchObject({ ok: false, unavailable: false, code: 'failed' });
        expect(await callMusic(() => { throw new Error('boom'); }, 'x')).toMatchObject({ ok: false, code: 'send_failed' });
    });

    it('passes the payload through', async () => {
        const send = vi.fn(async () => ({ status: 'ok' }));
        await callMusic(send, 'update_music_playlist', { playlist_id: 'pl_1', track_ids: ['a'] });
        expect(send).toHaveBeenCalledWith('update_music_playlist', { playlist_id: 'pl_1', track_ids: ['a'] });
    });
});

describe('display', () => {
    it('formats times', () => {
        expect(formatTime(0)).toBe('0:00');
        expect(formatTime(65.9)).toBe('1:05');
        expect(formatTime(3725)).toBe('1:02:05');
        expect(formatTime(null)).toBe('–:––');
        expect(formatTime(NaN)).toBe('–:––');
    });

    it('filters by title, artist, album and file name', () => {
        const e: LibraryEntry = { id: 't', title: 'Night bus', artist: 'Mike', album: null, duration_s: null,
            source: { kind: 'local', name: 'demo-01.flac', uri: '' } };
        expect(matchesFilter(e, 'NIGHT')).toBe(true);
        expect(matchesFilter(e, 'mike')).toBe(true);
        expect(matchesFilter(e, 'demo-01')).toBe(true);
        expect(matchesFilter(e, 'rain')).toBe(false);
        expect(matchesFilter(e, '  ')).toBe(true);
        expect(matchesFilter(null, 'x')).toBe(false);
    });
});

describe('throttle (the volume write)', () => {
    function clock() {
        let t = 0;
        const due: Array<{ at: number; fn: () => void; id: number }> = [];
        let next = 1;
        return {
            timers: {
                now: () => t,
                setTimeout: (fn: () => void, ms: number) => { due.push({ at: t + ms, fn, id: next }); return next++; },
                clearTimeout: (id: number) => { const i = due.findIndex(d => d.id === id); if (i >= 0) due.splice(i, 1); },
            },
            advance(ms: number) {
                t += ms;
                for (const d of [...due].sort((a, b) => a.at - b.at)) {
                    if (d.at <= t) { due.splice(due.indexOf(d), 1); d.fn(); }
                }
            },
            waiting: () => due.length,
        };
    }

    it('runs the first call at once and folds the calls inside the window into one trailing run', () => {
        const c = clock();
        const fn = vi.fn();
        const t = throttle(fn, 250, c.timers);
        t.call(); t.call(); t.call();
        expect(fn).toHaveBeenCalledTimes(1);
        expect(c.waiting()).toBe(1);
        c.advance(249);
        expect(fn).toHaveBeenCalledTimes(1);
        c.advance(1);
        expect(fn).toHaveBeenCalledTimes(2);
        c.advance(1000);
        expect(fn).toHaveBeenCalledTimes(2);
        t.call(); // the window has passed: at once again
        expect(fn).toHaveBeenCalledTimes(3);
    });

    it('flush runs a waiting call now and only then; cancel drops it', () => {
        const c = clock();
        const fn = vi.fn();
        const t = throttle(fn, 250, c.timers);
        t.flush();
        expect(fn).not.toHaveBeenCalled();
        t.call(); t.call();
        t.flush();
        expect(fn).toHaveBeenCalledTimes(2);
        expect(c.waiting()).toBe(0);
        t.call();
        t.cancel();
        c.advance(1000);
        expect(fn).toHaveBeenCalledTimes(2);
    });
});
