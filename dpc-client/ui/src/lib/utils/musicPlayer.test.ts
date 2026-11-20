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
    streamTrackUrl,
    isLoopbackStreamBase,
    trackMime,
    isConcreteAudioMime,
    formatRanges,
    resolveDuration,
    type LibraryEntry,
    type PlaybackEnv,
    type Playlist,
    type QueueState,
    MINI_PLAYER_MIN_WIDTH,
    MINI_PLAYER_MODES_WIDTH,
    MINI_PLAYER_POSITION_WIDTH,
    fitMusicPlayer,
    minimiseMusicPlayer,
    miniPlayerRect,
    miniPlayerShows,
    miniPlayerWidth,
    moveMiniPlayer,
    resizeMiniWidth,
    restoreMusicPlayer,
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

describe('the mini-player bar keeps its own width', () => {
    const VP = { width: 1600, height: 900 };
    function memory() {
        const m = new Map<string, string>();
        return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v), m };
    }

    it('without a mini width the bar is as wide as the window, as before', () => {
        expect(miniPlayerWidth(null, 700, VP)).toBe(700);
        expect(miniPlayerWidth(undefined, 700, VP)).toBe(700);
        expect(miniPlayerWidth(Number.NaN, 700, VP)).toBe(700);
    });

    it('is clamped to its minimum and to the viewport', () => {
        expect(miniPlayerWidth(120, 700, VP)).toBe(MINI_PLAYER_MIN_WIDTH);
        expect(miniPlayerWidth(5000, 700, VP)).toBe(VP.width);
        expect(miniPlayerWidth(360, 700, VP)).toBe(360);
        // A viewport under the minimum wins.
        expect(miniPlayerWidth(400, 700, { width: 250, height: 600 })).toBe(250);
    });

    it('the bar box is the window corner at the bar width, pulled back into view', () => {
        const rect = { x: 1400, y: 2000, width: 700, height: 500 };
        expect(miniPlayerRect(rect, 320, VP)).toEqual({ x: 1600 - 320, y: 900 - HEADER_HEIGHT, width: 320, height: HEADER_HEIGHT });
        // A narrow bar may sit further right than the whole window could.
        expect(miniPlayerRect({ ...rect, x: 1200, y: 100 }, 320, VP).x).toBe(1200);
    });

    it('the right-edge handle changes the width only, within min and the viewport edge', () => {
        const bar = { x: 1000, y: 300, width: 400, height: HEADER_HEIGHT };
        expect(resizeMiniWidth(bar, 50, VP)).toBe(450);
        expect(resizeMiniWidth(bar, -60, VP)).toBe(340);
        expect(resizeMiniWidth(bar, -500, VP)).toBe(MINI_PLAYER_MIN_WIDTH);
        expect(resizeMiniWidth(bar, 900, VP)).toBe(VP.width - bar.x);
        // The left edge does not move: the bar box is not an output of the resize.
        expect(bar).toEqual({ x: 1000, y: 300, width: 400, height: HEADER_HEIGHT });
    });

    it('a narrow bar drops shuffle and repeat, then k / n', () => {
        expect(miniPlayerShows(MINI_PLAYER_MODES_WIDTH)).toEqual({ position: true, modes: true });
        expect(miniPlayerShows(MINI_PLAYER_MODES_WIDTH - 1)).toEqual({ position: true, modes: false });
        expect(miniPlayerShows(MINI_PLAYER_POSITION_WIDTH - 1)).toEqual({ position: false, modes: false });
        expect(miniPlayerShows(MINI_PLAYER_MIN_WIDTH)).toEqual({ position: false, modes: false });
    });

    it('the mini width survives a save and load under the same key', () => {
        const s = memory();
        saveWindowState(s, { rect: { x: 100, y: 80, width: 900, height: 600 }, minimised: true, miniWidth: 340 }, MUSIC_PLAYER_WINDOW);
        expect([...s.m.keys()]).toEqual(['dpc.musicPlayer.window']);
        expect(loadWindowState(s, VP, MUSIC_PLAYER_WINDOW)).toEqual({
            rect: { x: 100, y: 80, width: 900, height: 600 }, minimised: true, miniWidth: 340,
        });
    });

    it('a state stored before the mini width loads, with no mini width', () => {
        const s = memory();
        s.setItem('dpc.musicPlayer.window', JSON.stringify({ rect: { x: 100, y: 80, width: 900, height: 600 }, minimised: true }));
        const w = loadWindowState(s, VP, MUSIC_PLAYER_WINDOW);
        expect(w.rect).toEqual({ x: 100, y: 80, width: 900, height: 600 });
        expect('miniWidth' in w).toBe(false);
        expect(miniPlayerWidth(w.miniWidth, w.rect.width, VP)).toBe(900);
    });
});

describe('the mini-player bar keeps its own corner', () => {
    // Mike's window: 2576 px wide.
    const VP = { width: 2576, height: 1400 };
    const window1 = { x: 300, y: 120, width: 1600, height: 900 };
    function memory() {
        const m = new Map<string, string>();
        return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v), m };
    }

    it('a minimised drag reaches the right edge at the bar width, not the window width', () => {
        const bar = { x: 300, y: 120, width: 400, height: HEADER_HEIGHT };
        expect(moveMiniPlayer(bar, 5000, 0, VP)).toEqual({ x: VP.width - 400, y: 120 });
        expect(moveMiniPlayer(bar, 5000, 5000, VP)).toEqual({ x: VP.width - 400, y: VP.height - HEADER_HEIGHT });
        expect(moveMiniPlayer(bar, -5000, -5000, VP)).toEqual({ x: 0, y: 0 });
        expect(moveMiniPlayer(bar, 100, 50, VP)).toEqual({ x: 400, y: 170 });
    });

    it('the first minimise puts the bar where the window header is and keeps the window box', () => {
        const w = minimiseMusicPlayer({ rect: window1, minimised: false, miniWidth: 400 }, VP);
        expect(w).toEqual({ rect: window1, minimised: true, miniWidth: 400, miniX: 300, miniY: 120 });
    });

    it('minimise / restore round trips keep both corners', () => {
        let w = minimiseMusicPlayer({ rect: window1, minimised: false, miniWidth: 400 }, VP);
        const moved = moveMiniPlayer(miniPlayerRect(w.rect, w.miniWidth, VP, { x: w.miniX!, y: w.miniY! }), 5000, 400, VP);
        w = { ...w, miniX: moved.x, miniY: moved.y };
        w = restoreMusicPlayer(w, VP);
        expect(w).toEqual({ rect: window1, minimised: false, miniWidth: 400, miniX: 2176, miniY: 520 });
        // The window moves while open; the bar's corner does not follow it.
        w = { ...w, rect: { ...window1, x: 50, y: 60 } };
        w = minimiseMusicPlayer(w, VP);
        expect(w).toEqual({ rect: { ...window1, x: 50, y: 60 }, minimised: true, miniWidth: 400, miniX: 2176, miniY: 520 });
        expect(restoreMusicPlayer(w, VP).rect).toEqual({ ...window1, x: 50, y: 60 });
    });

    it('a bar resize keeps its left edge, so the corner is unchanged', () => {
        const bar = { x: 2000, y: 400, width: 400, height: HEADER_HEIGHT };
        const width = resizeMiniWidth(bar, 300, VP);
        expect(width).toBe(VP.width - 2000);
        expect(miniPlayerRect(window1, width, VP, { x: bar.x, y: bar.y })).toMatchObject({ x: 2000, y: 400 });
    });

    it('a viewport resize fits both boxes', () => {
        const small = { width: 1200, height: 700 };
        const w = fitMusicPlayer({ rect: window1, minimised: true, miniWidth: 400, miniX: 2176, miniY: 1300 }, small);
        expect(w).toEqual({
            rect: { x: 0, y: 0, width: 1200, height: 700 }, minimised: true, miniWidth: 400, miniX: 800, miniY: 700 - HEADER_HEIGHT,
        });
    });

    it('a stored state with both corners survives a save and load under the same key', () => {
        const s = memory();
        const state = { rect: window1, minimised: true, miniWidth: 400, miniX: 2176, miniY: 520 };
        saveWindowState(s, state, MUSIC_PLAYER_WINDOW);
        expect([...s.m.keys()]).toEqual(['dpc.musicPlayer.window']);
        const loaded = loadWindowState(s, VP, MUSIC_PLAYER_WINDOW);
        expect(loaded).toEqual(state);
        expect(fitMusicPlayer(loaded, VP)).toEqual(state);
    });

    it('an old minimised state without a bar corner loads with the bar where it was drawn', () => {
        const s = memory();
        // Before: the stored rect corner was the bar's, here at the bottom edge.
        s.setItem('dpc.musicPlayer.window', JSON.stringify({ rect: { x: 900, y: 1400 - HEADER_HEIGHT, width: 1000, height: 600 }, minimised: true }));
        const w = fitMusicPlayer(loadWindowState(s, VP, MUSIC_PLAYER_WINDOW), VP);
        expect(w.miniX).toBe(900);
        expect(w.miniY).toBe(1400 - HEADER_HEIGHT);
        expect('miniWidth' in w).toBe(false);
        expect(miniPlayerRect(w.rect, w.miniWidth, VP, { x: w.miniX!, y: w.miniY! }))
            .toEqual({ x: 900, y: 1400 - HEADER_HEIGHT, width: 1000, height: HEADER_HEIGHT });
        // Restored, the window is whole in view.
        expect(restoreMusicPlayer(w, VP).rect).toEqual({ x: 900, y: 800, width: 1000, height: 600 });
    });

    it('an old open state loads as before, with no bar corner until the first minimise', () => {
        const s = memory();
        s.setItem('dpc.musicPlayer.window', JSON.stringify({ rect: window1, minimised: false }));
        expect(fitMusicPlayer(loadWindowState(s, VP, MUSIC_PLAYER_WINDOW), VP)).toEqual({ rect: window1, minimised: false });
    });
});

describe('trackMime, isConcreteAudioMime, formatRanges', () => {
    const e = (path: string | null, mime?: string | null, name = ''): LibraryEntry => ({
        id: 'trk_1', title: 't', duration_s: 1, origin: 'folder', path, missing: false,
        source: { kind: 'local', name, uri: '', mime },
    });
    it('derives the type from the extension without case, from the path or the source name', () => {
        const cases: Array<[string, string]> = [
            ['a.mp3', 'audio/mpeg'], ['a.MP3', 'audio/mpeg'], ['a.flac', 'audio/flac'], ['a.wav', 'audio/wav'],
            ['a.ogg', 'audio/ogg'], ['a.oga', 'audio/ogg'], ['a.opus', 'audio/ogg'], ['a.m4a', 'audio/mp4'],
            ['a.mp4', 'audio/mp4'], ['a.aac', 'audio/mp4'], ['a.webm', 'audio/webm'], ['a.xyz', ''], ['noext', ''],
        ];
        for (const [file, mime] of cases) {
            expect(trackMime(e('C:\\Users\\m\\.dpc\\music\\' + file, null))).toBe(mime);
            expect(trackMime(e(null, undefined, file))).toBe(mime);
        }
        expect(trackMime(null)).toBe('');
    });
    it('keeps a recorded concrete type and rejects wildcards and non-audio', () => {
        expect(trackMime(e('/x/a.mp3', 'audio/x-mpeg'))).toBe('audio/x-mpeg');
        expect(trackMime(e('/x/a.mp3', 'audio/*'))).toBe('audio/mpeg');
        expect(trackMime(e('/x/a.mp3', 'video/mp4'))).toBe('audio/mpeg');
        expect(isConcreteAudioMime('audio/ogg; codecs=opus')).toBe(true);
        expect(isConcreteAudioMime('audio/*')).toBe(false);
        expect(isConcreteAudioMime(null)).toBe(false);
    });
    it('formats ranges, and survives a throwing or missing TimeRanges', () => {
        const r = { length: 2, start: (i: number) => [0, 40][i], end: (i: number) => [12.34, 60.06][i] };
        expect(formatRanges(r)).toBe('0.0-12.3,40.0-60.1');
        expect(formatRanges({ length: 0, start: () => 0, end: () => 0 })).toBe('none');
        expect(formatRanges(null)).toBe('none');
        expect(formatRanges({ length: 1, start: () => { throw new Error('x'); }, end: () => 0 })).toBe('unreadable');
    });
});

describe('the playback source without the core track stream (only ~/.dpc plays)', () => {
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

    it('on Linux the blob gets a real MIME type: the recorded one, else by extension, never audio/*', async () => {
        const typeOf = async (e: LibraryEntry) => {
            let seen = '';
            const env = linux({ createObjectURL: (b: Blob) => { seen = b.type; return 'blob:t'; } });
            const r = await trackPlaybackUrl(e, env);
            expect(r).toMatchObject({ ok: true, bytes: 3 });
            return { seen, mime: r.ok ? r.mime : undefined };
        };
        const mp3 = (mime: string | null | undefined) => entry('/home/mike/.dpc/music/song.mp3', { source: { kind: 'local', name: 'song.mp3', uri: 'file:///x', mime } });
        expect(await typeOf(mp3('audio/mpeg'))).toEqual({ seen: 'audio/mpeg', mime: 'audio/mpeg' });
        expect(await typeOf(mp3(null))).toEqual({ seen: 'audio/mpeg', mime: 'audio/mpeg' });
        expect(await typeOf(mp3(undefined))).toEqual({ seen: 'audio/mpeg', mime: 'audio/mpeg' });
        expect(await typeOf(mp3('audio/*'))).toEqual({ seen: 'audio/mpeg', mime: 'audio/mpeg' });
        expect(await typeOf(mp3(''))).toEqual({ seen: 'audio/mpeg', mime: 'audio/mpeg' });
        expect(await typeOf(entry('/home/mike/.dpc/music/x.xyz', { source: { kind: 'local', name: 'x.xyz', uri: '', mime: 'audio/*' } })))
            .toEqual({ seen: '', mime: '' });
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

describe('the playback source with the core track stream (ADR-051 N8, D28)', () => {
    const INFO = { base_url: 'http://127.0.0.1:9996', token: 'tok/en+=' };
    const entry = (path: string | null, extra: Partial<LibraryEntry> = {}): LibraryEntry => ({
        id: 'trk_0000000000000001', title: 'Night bus', duration_s: 200, origin: 'folder', path, missing: false,
        source: { kind: 'local', name: 'night-bus.flac', uri: 'file:///x', mime: 'audio/flac' }, ...extra,
    });
    const URL_1 = 'http://127.0.0.1:9996/music/track/trk_0000000000000001?t=tok%2Fen%2B%3D';
    const URL_NO_RANGE = `${URL_1}&ranges=0`;
    const linux = (over: Partial<PlaybackEnv> = {}): PlaybackEnv => ({
        tauri: true, home: '/home/mike', isWindows: false, isLinux: true,
        readHomeFile: vi.fn(async () => new Uint8Array([1, 2, 3])),
        createObjectURL: () => 'blob:abc', revokeObjectURL: vi.fn(),
        streamInfo: vi.fn(async () => INFO), ...over,
    });
    const win = (over: Partial<PlaybackEnv> = {}): PlaybackEnv => ({
        tauri: true, home: 'C:\\Users\\mike', isWindows: true, isLinux: false,
        convertFileSrc: (p) => `http://asset.localhost/${encodeURIComponent(p)}`,
        streamInfo: vi.fn(async () => INFO), ...over,
    });
    const mac = (over: Partial<PlaybackEnv> = {}): PlaybackEnv => ({
        tauri: true, home: '/Users/mike', isWindows: false, isLinux: false,
        convertFileSrc: (p) => `asset://localhost/${encodeURIComponent(p)}`,
        streamInfo: vi.fn(async () => INFO), ...over,
    });

    it('the stream URL is keyed by the track id with the token, both encoded, and no path', () => {
        expect(streamTrackUrl(INFO, 'trk_0000000000000001')).toBe(URL_1);
        expect(streamTrackUrl(INFO, 'a/../b')).toBe('http://127.0.0.1:9996/music/track/a%2F..%2Fb?t=tok%2Fen%2B%3D');
    });

    it('ranges=false adds the one extra query key; the default URL is unchanged', () => {
        expect(streamTrackUrl(INFO, 'trk_0000000000000001', false)).toBe(URL_NO_RANGE);
        expect(streamTrackUrl(INFO, 'trk_0000000000000001', true)).toBe(URL_1);
    });

    it('Linux asks for ranges=0 unless the core says [music] stream_ranges_linux is on; Windows and macOS never do', async () => {
        const e = entry('/home/mike/Music/night-bus.flac');
        expect(await trackPlaybackUrl(e, linux())).toMatchObject({ ok: true, url: URL_NO_RANGE, ranges: false });
        expect(await trackPlaybackUrl(e, linux({ streamInfo: async () => ({ ...INFO, ranges_linux: false }) })))
            .toMatchObject({ url: URL_NO_RANGE, ranges: false });
        expect(await trackPlaybackUrl(e, linux({ streamInfo: async () => ({ ...INFO, ranges_linux: true }) })))
            .toMatchObject({ url: URL_1, ranges: true });
        const w = await trackPlaybackUrl(entry('D:\Music\a.flac'), win({ streamInfo: async () => ({ ...INFO, ranges_linux: false }) }));
        const m = await trackPlaybackUrl(entry('/Users/mike/Music/a.flac'), mac({ streamInfo: async () => ({ ...INFO, ranges_linux: false }) }));
        for (const r of [w, m]) {
            expect(r).toMatchObject({ ok: true, via: 'http', ranges: true });
            expect(r.ok && r.url).not.toContain('ranges');
        }
    });

    it('only a loopback http base is a stream', () => {
        expect(isLoopbackStreamBase('http://127.0.0.1:9996')).toBe(true);
        expect(isLoopbackStreamBase('http://[::1]:9996')).toBe(true);
        for (const bad of ['http://evil.example:9996', 'https://127.0.0.1:9996', 'http://127.0.0.1:9996/x', 'http://127.0.0.1.evil:1', '', null]) {
            expect(isLoopbackStreamBase(bad)).toBe(false);
        }
    });

    it('on Linux a track under ~/.dpc plays from the stream, with nothing read into a blob', async () => {
        const env = linux();
        const r = await trackPlaybackUrl(entry('/home/mike/.dpc/music/take.flac'), env);
        expect(r).toMatchObject({ ok: true, via: 'http', url: URL_NO_RANGE, mime: 'audio/flac', ranges: false });
        expect(r.ok && r.fallback).toBeFalsy();
        expect(env.readHomeFile).not.toHaveBeenCalled();
    });

    it('on Linux a track in a watched folder outside ~/.dpc plays from the stream too', async () => {
        const e = entry('/home/mike/Music/night-bus.flac');
        const env = linux();
        expect(playbackProblem(e, env)).toBeNull();
        expect(await trackPlaybackUrl(e, env)).toMatchObject({ ok: true, via: 'http', url: URL_NO_RANGE });
    });

    it('on Windows and macOS a track under ~/.dpc stays on asset:, and the stream is not asked', async () => {
        const w = win();
        expect(await trackPlaybackUrl(entry('C:\\Users\\mike\\.dpc\\music\\take.wav'), w))
            .toMatchObject({ ok: true, via: 'asset', url: `http://asset.localhost/${encodeURIComponent('C:\\Users\\mike\\.dpc\\music\\take.wav')}` });
        expect(w.streamInfo).not.toHaveBeenCalled();
        const m = mac();
        expect(await trackPlaybackUrl(entry('/Users/mike/.dpc/music/take.wav'), m)).toMatchObject({ ok: true, via: 'asset' });
        expect(m.streamInfo).not.toHaveBeenCalled();
    });

    it('on Windows and macOS a track outside ~/.dpc, which asset: cannot reach, plays from the stream', async () => {
        expect(await trackPlaybackUrl(entry('D:\\Music\\night-bus.flac'), win())).toMatchObject({ ok: true, via: 'http', url: URL_1, ranges: true });
        expect(await trackPlaybackUrl(entry('/Users/mike/Music/night-bus.flac'), mac())).toMatchObject({ ok: true, via: 'http', url: URL_1 });
    });

    it('on Linux, when the stream info fails, a track under ~/.dpc falls back to a blob: and says why', async () => {
        const env = linux({ streamInfo: vi.fn(async () => { throw new Error('the port was taken'); }) });
        const r = await trackPlaybackUrl(entry('/home/mike/.dpc/music/take.flac'), env);
        expect(r).toMatchObject({ ok: true, via: 'blob', url: 'blob:abc' });
        expect(r.ok && r.fallback).toBe('stream unavailable: the port was taken');
        expect(env.readHomeFile).toHaveBeenCalledWith('.dpc/music/take.flac');
    });

    it('a stream address that is not loopback is not used: the fallback applies', async () => {
        const env = linux({ streamInfo: async () => ({ base_url: 'http://evil.example:9996', token: 'x' }) });
        const r = await trackPlaybackUrl(entry('/home/mike/.dpc/music/take.flac'), env);
        expect(r).toMatchObject({ ok: true, via: 'blob' });
        expect(r.ok && r.fallback).toContain('no loopback stream address');
    });

    it('when the stream info fails, a track outside ~/.dpc is "not playable yet" with the reason', async () => {
        const failing = vi.fn(async () => { throw new Error('stream_unavailable'); });
        for (const [env, path] of [
            [linux({ streamInfo: failing }), '/home/mike/Music/a.flac'],
            [win({ streamInfo: failing }), 'D:\\Music\\a.flac'],
        ] as const) {
            const r = await trackPlaybackUrl(entry(path), env);
            expect(r).toMatchObject({ ok: false, problem: 'outside-dpc' });
            expect(!r.ok && r.reason).toBe(`${OUTSIDE_DPC_TEXT}: the track stream unavailable: stream_unavailable`);
        }
    });
});

describe('resolveDuration', () => {
    it('takes the element duration when it is plausible', () => {
        expect(resolveDuration(204.88, 204.9, 10)).toBe(204.88);
        expect(resolveDuration(180, null, 10)).toBe(180);
    });
    it('falls back to the library duration when the element reports 0, NaN or Infinity', () => {
        expect(resolveDuration(0, 204.9, 0)).toBe(204.9);
        expect(resolveDuration(NaN, 204.9, 5)).toBe(204.9);
        expect(resolveDuration(Infinity, 204.9, 5)).toBe(204.9);
        expect(resolveDuration(undefined, 204.9, 5)).toBe(204.9);
    });
    it('rejects an element duration below the position or under half the library figure', () => {
        expect(resolveDuration(3.2, 204.9, 13)).toBe(204.9);
        expect(resolveDuration(3.2, 204.9, 0)).toBe(204.9);
    });
    it('is 0 when nothing is known, and never below the current time', () => {
        expect(resolveDuration(0, null, 0)).toBe(0);
        expect(resolveDuration(0, 0, NaN)).toBe(0);
        expect(resolveDuration(5, null, 13)).toBe(13);
        expect(resolveDuration(0, 10, 13)).toBe(13);
    });
});
