import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { get } from 'svelte/store';

// The playback service with a fake audio element: the races the review found
// (a superseded load's events, a queue edited while Next waits, the volume write
// per input event) are checked against the real service code.

const h = vi.hoisted(() => {
    const writes: Array<[string, string]> = [];
    const store = new Map<string, string>();
    const storage = {
        getItem: (k: string) => store.get(k) ?? null,
        setItem: (k: string, v: string) => { writes.push([k, v]); store.set(k, v); },
    };
    /** Per track id, a gate the next source read of that track waits on. */
    const gates = new Map<string, Promise<void>>();
    (globalThis as any).window = { localStorage: storage };
    const logLines: string[] = [];
    return { writes, store, storage, gates, logLines };
});

vi.mock('$lib/coreService', () => {
    const unavailable = async () => ({ ok: false, unavailable: true, message: '' });
    return {
        addMusicFolder: unavailable, createMusicPlaylist: unavailable, deleteMusicPlaylist: unavailable,
        listMusicFolders: unavailable, listMusicLibrary: unavailable, listMusicPlaylists: unavailable,
        removeMusicFolder: unavailable, rescanMusicFolder: unavailable, updateMusicPlaylist: unavailable,
    };
});

vi.mock('$lib/logger', () => ({
    log: { debug: (_ctx: string, msg: string) => { h.logLines.push(msg); }, info() {}, warn() {}, error() {} },
}));

vi.mock('$lib/utils/attachmentSrc', () => ({ detectWindows: () => true }));

vi.mock('$lib/utils/musicPlayer', async () => {
    const actual = await vi.importActual<typeof import('../utils/musicPlayer')>('../utils/musicPlayer');
    return {
        ...actual,
        playbackProblem: () => null,
        trackPlaybackUrl: async (entry: { id: string } | null) => {
            const gate = entry ? h.gates.get(entry.id) : undefined;
            if (gate) await gate;
            return { ok: true, url: `blob:${entry?.id}`, via: 'blob', release: () => {} };
        },
    };
});

class FakeAudio extends EventTarget {
    static last: FakeAudio | null = null;
    preload = '';
    volume = 1;
    loop = false;
    paused = true;
    currentTime = 0;
    duration = NaN;
    error: { code: number; message?: string } | null = null;
    readyState = 4;
    networkState = 1;
    buffered = { length: 1, start: () => 0, end: () => 12.34 };
    seekable = { length: 1, start: () => 0, end: () => 100 };
    private attrs = new Map<string, string>();
    constructor() {
        super();
        FakeAudio.last = this;
    }
    set src(v: string) { this.attrs.set('src', v); this.currentTime = 0; }
    get src() { return this.attrs.get('src') ?? ''; }
    getAttribute(n: string) { return this.attrs.get(n) ?? null; }
    removeAttribute(n: string) { this.attrs.delete(n); }
    load() {}
    async play() { this.paused = false; this.dispatchEvent(new Event('play')); }
    pause() { this.paused = true; this.dispatchEvent(new Event('pause')); }
    fire(type: string) { this.dispatchEvent(new Event(type)); }
}
(globalThis as any).Audio = FakeAudio;

const settle = async () => {
    for (let i = 0; i < 10; i++) await new Promise(r => setTimeout(r, 0));
};

function deferred() {
    let open!: () => void;
    const promise = new Promise<void>(r => { open = r; });
    return { promise, open };
}

const entry = (id: string) => ({
    id, title: id.toUpperCase(), duration_s: 100, path: `C:/Users/x/.dpc/music/${id}.mp3`,
    source: { kind: 'local', name: `${id}.mp3`, uri: '' },
});

async function freshService(ids: string[], index: number) {
    vi.resetModules();
    h.gates.clear();
    FakeAudio.last = null;
    const m = await import('./music');
    m.musicLibrary.set({ status: 'ready', message: '', entries: ids.map(entry) as any, playlists: [], folders: [] });
    m.musicPlayback.update(s => ({ ...s, queue: { ids, index, context: null } }));
    await m.playQueueAt(index);
    await settle();
    return m;
}

const state = (m: typeof import('./music')) => {
    const s = get(m.musicPlayback);
    return { current: s.queue.ids[s.queue.index], loadedId: s.loadedId, ids: s.queue.ids, currentTime: s.currentTime };
};

describe('a superseded load is not heard', () => {
    it("an 'ended' of the old track that loses the race with Next does not advance the queue again", async () => {
        const m = await freshService(['a', 'b', 'c', 'd'], 0);
        const audio = FakeAudio.last!;
        expect(state(m).loadedId).toBe('a');

        const gate = deferred();
        h.gates.set('b', gate.promise);
        const next = m.nextTrack();
        await settle();
        // Next has begun loading B; the element still holds A, and A ends now.
        expect(state(m).current).toBe('b');
        audio.fire('ended');
        await settle();
        gate.open();
        await next;
        await settle();

        expect(state(m)).toMatchObject({ current: 'b', loadedId: 'b' });
    });

    it("a late 'timeupdate' of the old track does not write its time over the new one", async () => {
        const m = await freshService(['a', 'b'], 0);
        const audio = FakeAudio.last!;
        const gate = deferred();
        h.gates.set('b', gate.promise);
        const next = m.nextTrack();
        await settle();
        audio.currentTime = 87;
        audio.fire('timeupdate');
        expect(state(m).currentTime).toBe(0);
        gate.open();
        await next;
        audio.currentTime = 3;
        audio.fire('timeupdate');
        expect(state(m).currentTime).toBe(3);
    });

    it("an 'ended' of the loaded track still advances (the guard does not mute it)", async () => {
        const m = await freshService(['a', 'b', 'c'], 0);
        FakeAudio.last!.fire('ended');
        await settle();
        expect(state(m)).toMatchObject({ current: 'b', loadedId: 'b' });
    });

    it('a Next that started before an ended track moved on is dropped, not stacked', async () => {
        const m = await freshService(['a', 'b', 'c'], 0);
        FakeAudio.last!.fire('ended');
        const next = m.nextTrack();
        await next;
        await settle();
        expect(state(m)).toMatchObject({ current: 'b', loadedId: 'b' });
    });
});

describe('the audio diagnostics in the log', () => {
    it('logs the source, the seek request, seeking/seeked and an error, and never a timeupdate', async () => {
        const m = await freshService(['a'], 0);
        const audio = FakeAudio.last!;
        expect(h.logLines.some(l => /^source #\d+ a a\.mp3 via=blob mime=none bytes=n\/a platform=windows/.test(l))).toBe(true);

        h.logLines.length = 0;
        audio.fire('timeupdate');
        audio.fire('timeupdate');
        expect(h.logLines).toEqual([]);

        m.seekTo(42.5, 'slider');
        expect(h.logLines[0]).toMatch(/^seek request \(slider\) target=42\.50 from=\d/);
        expect(h.logLines[0]).toContain('readyState=4 networkState=1 buffered=0.0-12.3 seekable=0.0-100.0');
        audio.fire('seeking');
        audio.fire('seeked');
        expect(h.logLines[1]).toMatch(/^seeking #\d+ a requested=42\.50 actual=42\.50/);
        expect(h.logLines[2]).toMatch(/^seeked #\d+ a requested=42\.50 actual=42\.50 delta=0\.00/);

        audio.error = { code: 4, message: 'Failed to open' };
        audio.fire('error');
        expect(h.logLines.at(-1)).toMatch(/^error #\d+ a code=4 message="Failed to open"/);
    });

    it('folds a stalled burst into one line per second, with a count', async () => {
        await freshService(['a'], 0);
        const audio = FakeAudio.last!;
        h.logLines.length = 0;
        for (let i = 0; i < 5; i++) audio.fire('stalled');
        expect(h.logLines.filter(l => l.startsWith('stalled'))).toHaveLength(1);
    });
});

describe('Next steps from the queue as it is after its await', () => {
    it('a row removed above the current track while Next waits does not make it skip one', async () => {
        const m = await freshService(['a', 'b', 'c', 'd'], 1);
        const next = m.nextTrack();
        m.removeFromQueue(0); // [b, c, d], index 0: the snapshot's index 1 + 1 would be d
        await next;
        await settle();
        expect(state(m)).toMatchObject({ ids: ['b', 'c', 'd'], current: 'c', loadedId: 'c' });
    });

    it('a track queued next while Next waits is the one it plays', async () => {
        const m = await freshService(['a', 'b', 'c'], 0);
        m.musicLibrary.update(l => ({ ...l, entries: [...l.entries, entry('x') as any] }));
        const next = m.nextTrack();
        m.queueTrack('x', true); // [a, x, b, c]
        await next;
        await settle();
        expect(state(m)).toMatchObject({ current: 'x', loadedId: 'x' });
    });

    it('Prev after a removal below the current track still goes to the one before it', async () => {
        const m = await freshService(['a', 'b', 'c', 'd'], 2);
        const prev = m.prevTrack();
        m.removeFromQueue(0); // [b, c, d], index 1
        await prev;
        await settle();
        expect(state(m)).toMatchObject({ current: 'b', loadedId: 'b' });
    });
});

describe('the volume is written at most every VOLUME_SAVE_MS, and once on release', () => {
    beforeEach(() => vi.useFakeTimers());
    afterEach(() => vi.useRealTimers());

    it('folds a drag into a leading and a trailing write, and writes the release value at once', async () => {
        vi.resetModules();
        const m = await import('./music');
        const { MUSIC_PREFERENCES_KEY, VOLUME_SAVE_MS } = await import('../utils/musicPlayer');
        const volumes = () => h.writes.filter(([k]) => k === MUSIC_PREFERENCES_KEY).map(([, v]) => JSON.parse(v).volume);
        h.writes.length = 0;

        for (const v of [0.9, 0.8, 0.7, 0.6, 0.5]) m.setVolume(v);
        expect(get(m.musicPlayback).volume).toBe(0.5); // the store and the element follow at once
        expect(volumes()).toEqual([0.9]);

        vi.advanceTimersByTime(VOLUME_SAVE_MS);
        expect(volumes()).toEqual([0.9, 0.5]);

        m.setVolume(0.4);
        m.setVolume(0.3);
        m.setVolume(0.25, true); // the slider's change event
        expect(volumes()).toEqual([0.9, 0.5, 0.25]);

        vi.advanceTimersByTime(VOLUME_SAVE_MS * 4);
        expect(volumes()).toEqual([0.9, 0.5, 0.25]);
    });
});


describe('waitForDuration', () => {
    const fake = (duration: number) => {
        const et = new EventTarget() as EventTarget & { duration: number };
        et.duration = duration;
        return et;
    };

    it('resolves ok at once when the duration is already known', async () => {
        const m = await import('./music');
        expect(await m.waitForDuration(fake(12) as any, 50)).toBe('ok');
    });

    it('waits for loadedmetadata / durationchange to bring a finite duration', async () => {
        const m = await import('./music');
        const el = fake(NaN);
        const p = m.waitForDuration(el as any, 1000);
        el.duration = 0;
        el.dispatchEvent(new Event('durationchange'));
        el.duration = 7.5;
        el.dispatchEvent(new Event('loadedmetadata'));
        expect(await p).toBe('ok');
    });

    it('times out when no duration arrives', async () => {
        const m = await import('./music');
        expect(await m.waitForDuration(fake(0) as any, 20)).toBe('timeout');
    });

    it('stops waiting when the request went stale', async () => {
        const m = await import('./music');
        let stale = false;
        const p = m.waitForDuration(fake(0) as any, 5000, () => stale);
        stale = true;
        expect(await p).toBe('stale');
    });
});
