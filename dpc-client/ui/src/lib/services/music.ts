// src/lib/services/music.ts
// The Music Player's page-level state and its one audio element (ADR-051 D1, D2).
//
// This module is a singleton of the page: the audio element is created here, not in
// a component, so playback goes on while the person switches chats and while the
// Music Player window is minimised to its mini-player or closed. The window
// (components/music/MusicPlayer.svelte) only reads these stores and calls these
// functions. The rules (queue, shuffle, repeat, the playback source) are pure, in
// utils/musicPlayer.ts.
//
// The library comes from the core music service (dpc_client_core/music/commands.py)
// through the coreService wrappers. Until those commands are wired into local_api,
// every call answers "unavailable" and the window says MUSIC_UNAVAILABLE_TEXT.

import { derived, get, writable } from 'svelte/store';
import {
    addMusicFolder,
    createMusicPlaylist,
    deleteMusicPlaylist,
    listMusicFolders,
    listMusicLibrary,
    listMusicPlaylists,
    removeMusicFolder,
    rescanMusicFolder,
    updateMusicPlaylist,
} from '$lib/coreService';
import { detectWindows } from '$lib/utils/attachmentSrc';
import {
    EMPTY_QUEUE,
    MUSIC_UNAVAILABLE_TEXT,
    QUEUE_EMPTY_TEXT,
    currentId,
    cycleRepeat,
    enqueue,
    loadMusicPreferences,
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
    VOLUME_SAVE_MS,
    type LibraryEntry,
    type MusicAnswer,
    type MusicFolder,
    type PlaybackEnv,
    type Playlist,
    type QueueState,
    type RepeatMode,
    type StepResult,
} from '$lib/utils/musicPlayer';

// -- the library -----------------------------------------------------------------

export type LibraryStatus = 'idle' | 'loading' | 'ready' | 'unavailable' | 'error';

export interface MusicLibraryState {
    status: LibraryStatus;
    message: string;
    entries: LibraryEntry[];
    playlists: Playlist[];
    folders: MusicFolder[];
}

const EMPTY_LIBRARY: MusicLibraryState = { status: 'idle', message: '', entries: [], playlists: [], folders: [] };

export const musicLibrary = writable<MusicLibraryState>(EMPTY_LIBRARY);
export const musicEntriesById = derived(musicLibrary, ($l) => new Map($l.entries.map(e => [e.id, e])));

let loading = false;

/** Read the library, the playlists and the folders. `quiet` keeps what is shown
 *  while reading (the window's poll); otherwise the window shows "Loading". */
export async function loadMusicLibrary(quiet = false): Promise<void> {
    if (loading) return;
    loading = true;
    if (!quiet) musicLibrary.update(l => ({ ...l, status: l.status === 'ready' ? 'ready' : 'loading', message: '' }));
    try {
        const [lib, pls, fol] = await Promise.all([
            listMusicLibrary(), listMusicPlaylists(), listMusicFolders(),
        ]);
        const failed = [lib, pls, fol].find(a => !a.ok) as Extract<MusicAnswer, { ok: false }> | undefined;
        if (failed) {
            musicLibrary.update(l => ({
                ...l,
                status: failed.unavailable ? 'unavailable' : 'error',
                message: failed.unavailable ? MUSIC_UNAVAILABLE_TEXT : failed.message,
            }));
            return;
        }
        musicLibrary.set({
            status: 'ready',
            message: '',
            entries: ((lib as any).data.entries ?? []) as LibraryEntry[],
            playlists: ((pls as any).data.playlists ?? []) as Playlist[],
            folders: ((fol as any).data.folders ?? []) as MusicFolder[],
        });
    } finally {
        loading = false;
    }
}

/** Note an answer that says the backend lacks the commands; returns the answer. */
function noteAnswer<T>(a: MusicAnswer<T>): MusicAnswer<T> {
    if (!a.ok && a.unavailable) musicLibrary.update(l => ({ ...l, status: 'unavailable', message: MUSIC_UNAVAILABLE_TEXT }));
    return a;
}

function putPlaylist(p: Playlist) {
    musicLibrary.update(l => {
        const i = l.playlists.findIndex(x => x.id === p.id);
        const playlists = [...l.playlists];
        if (i >= 0) playlists[i] = p; else playlists.push(p);
        return { ...l, playlists };
    });
}

export async function createPlaylist(name: string, trackIds: string[] = []): Promise<MusicAnswer<{ playlist: Playlist }>> {
    const a = noteAnswer(await createMusicPlaylist(name, trackIds));
    if (a.ok) putPlaylist(a.data.playlist);
    return a;
}

export async function renamePlaylist(id: string, name: string): Promise<MusicAnswer<{ playlist: Playlist }>> {
    const a = noteAnswer(await updateMusicPlaylist(id, { name }));
    if (a.ok) putPlaylist(a.data.playlist);
    return a;
}

/** Set a playlist's whole order (add, remove and reorder are each this one call).
 *  Shown at once; put back if the core refuses. */
export async function setPlaylistTracks(id: string, trackIds: string[]): Promise<MusicAnswer<{ playlist: Playlist }>> {
    const before = get(musicLibrary).playlists.find(p => p.id === id);
    if (before) putPlaylist({ ...before, track_ids: trackIds });
    const a = noteAnswer(await updateMusicPlaylist(id, { track_ids: trackIds }));
    if (a.ok) putPlaylist(a.data.playlist);
    else if (before) putPlaylist(before);
    return a;
}

export async function deletePlaylist(id: string): Promise<MusicAnswer> {
    const a = noteAnswer(await deleteMusicPlaylist(id));
    if (a.ok) {
        musicLibrary.update(l => ({ ...l, playlists: l.playlists.filter(p => p.id !== id) }));
        // The queue keeps playing; it only forgets the list it came from.
        musicPlayback.update(s => (s.queue.context === id ? { ...s, queue: { ...s.queue, context: null } } : s));
    }
    return a;
}

export async function addFolder(path: string): Promise<MusicAnswer<{ folder: MusicFolder }>> {
    const a = noteAnswer(await addMusicFolder(path));
    if (a.ok) await loadMusicLibrary(true);
    return a;
}

export async function removeFolder(id: string): Promise<MusicAnswer<{ removed_tracks: number }>> {
    const a = noteAnswer(await removeMusicFolder(id));
    if (a.ok) await loadMusicLibrary(true);
    return a;
}

export async function rescanFolder(id?: string): Promise<MusicAnswer> {
    const a = noteAnswer(await rescanMusicFolder(id));
    if (a.ok) await loadMusicLibrary(true);
    return a;
}

// -- the playback source ---------------------------------------------------------

let envPromise: Promise<PlaybackEnv> | null = null;

function isTauriWindow(): boolean {
    if (typeof window === 'undefined') return false;
    const w = window as any;
    return w.isTauri === true || !!w.__TAURI__ || !!w.__TAURI_INTERNALS__;
}

function isLinuxWebview(): boolean {
    if (typeof navigator === 'undefined') return false;
    // The same reading as VoicePlayer.svelte; Android is not a DPC target.
    return /Linux|X11/.test(navigator.userAgent || '') || /Linux|X11/.test(navigator.platform || '');
}

async function readEnv(): Promise<PlaybackEnv> {
    const tauri = isTauriWindow();
    const env: PlaybackEnv = {
        tauri,
        home: null,
        isWindows: detectWindows(),
        isLinux: isLinuxWebview() && !detectWindows(),
        createObjectURL: (b) => URL.createObjectURL(b),
        revokeObjectURL: (u) => URL.revokeObjectURL(u),
    };
    if (!tauri) return env;
    try {
        const core = await import('@tauri-apps/api/core');
        env.convertFileSrc = (p: string) => core.convertFileSrc(p);
        // The home Tauri itself resolves `$HOME` to in the asset and fs scopes.
        try {
            const { homeDir } = await import('@tauri-apps/api/path');
            env.home = await homeDir();
        } catch {
            env.home = await core.invoke<string>('get_home_directory');
        }
        env.readHomeFile = async (relative: string) => {
            const { readFile, BaseDirectory } = await import('@tauri-apps/plugin-fs');
            return readFile(relative, { baseDir: BaseDirectory.Home });
        };
    } catch (e) {
        console.warn('[music] the Tauri API did not load; nothing can play', e);
    }
    return env;
}

/** The playback environment, read once. */
export function getPlaybackEnv(): Promise<PlaybackEnv> {
    envPromise ??= readEnv();
    return envPromise;
}

/** The environment once read, so the window can mark rows without waiting. */
export const playbackEnv = writable<PlaybackEnv | null>(null);
if (typeof window !== 'undefined') getPlaybackEnv().then(e => playbackEnv.set(e)).catch(() => {});

// -- playback --------------------------------------------------------------------

export interface MusicPlaybackState {
    queue: QueueState;
    shuffle: boolean;
    repeat: RepeatMode;
    volume: number;
    /** The track the audio element holds, or null. */
    loadedId: string | null;
    playing: boolean;
    loadingTrack: boolean;
    currentTime: number;
    duration: number;
    /** The last thing the player has to say (end of queue, a track that cannot play). */
    notice: string;
}

function storage(): Storage | null {
    try {
        return typeof window !== 'undefined' ? window.localStorage : null;
    } catch {
        return null;
    }
}

const prefs = loadMusicPreferences(storage());

export const musicPlayback = writable<MusicPlaybackState>({
    queue: EMPTY_QUEUE,
    shuffle: prefs.shuffle,
    repeat: prefs.repeat,
    volume: prefs.volume,
    loadedId: null,
    playing: false,
    loadingTrack: false,
    currentTime: 0,
    duration: 0,
    notice: '',
});

/** The current track's entry, or null. */
export const musicCurrentEntry = derived([musicPlayback, musicEntriesById], ([$p, $byId]) => {
    const id = currentId($p.queue);
    return id ? $byId.get(id) ?? null : null;
});

/** "k / n" of the queue. */
export const musicPosition = derived(musicPlayback, ($p) => positionLabel($p.queue));

/** The line on the Music Player button: what is playing, or nothing. */
export const musicButtonSummary = derived([musicPlayback, musicCurrentEntry], ([$p, $e]) => {
    if (!$p.loadedId || !$e) return '';
    return `${$p.playing ? 'Playing' : 'Paused'}: ${$e.title}`;
});

function remember() {
    const s = get(musicPlayback);
    saveMusicPreferences(storage(), { shuffle: s.shuffle, repeat: s.repeat, volume: s.volume });
}

let noticeTimer: ReturnType<typeof setTimeout> | null = null;
function say(text: string) {
    musicPlayback.update(s => ({ ...s, notice: text }));
    if (noticeTimer) clearTimeout(noticeTimer);
    if (text) noticeTimer = setTimeout(() => musicPlayback.update(s => ({ ...s, notice: '' })), 8000);
}

function entryOf(id: string | null): LibraryEntry | null {
    return id ? get(musicEntriesById).get(id) ?? null : null;
}

function titleOf(id: string | null): string {
    return entryOf(id)?.title ?? 'this track';
}

let audio: HTMLAudioElement | null = null;
let releaseSource: () => void = () => {};
/** Bumped by every load; a load whose token is no longer this one is superseded. */
let loadToken = 0;
/** The token of the load whose source the audio element holds now. Its events
 *  (`ended`, `timeupdate`, duration, `error`) count only while it is `loadToken`:
 *  an `ended` that loses the race with a manual Next would otherwise advance the
 *  queue a second time, and a late `timeupdate` would show the old track's time. */
let audioToken = -1;
const current = () => audioToken === loadToken;

function ensureAudio(): HTMLAudioElement | null {
    if (audio || typeof Audio === 'undefined') return audio;
    const a = new Audio();
    a.preload = 'metadata';
    a.volume = get(musicPlayback).volume;
    a.addEventListener('play', () => musicPlayback.update(s => ({ ...s, playing: true })));
    a.addEventListener('pause', () => musicPlayback.update(s => ({ ...s, playing: false })));
    a.addEventListener('timeupdate', () => {
        if (current()) musicPlayback.update(s => ({ ...s, currentTime: a.currentTime }));
    });
    const takeDuration = () => {
        if (current() && Number.isFinite(a.duration)) musicPlayback.update(s => ({ ...s, duration: a.duration }));
    };
    a.addEventListener('loadedmetadata', takeDuration);
    a.addEventListener('durationchange', takeDuration);
    a.addEventListener('ended', () => {
        if (current()) void handleEnded(audioToken);
    });
    a.addEventListener('error', () => {
        if (!a.getAttribute('src') || !current()) return;
        const code = a.error?.code;
        musicPlayback.update(s => ({ ...s, playing: false, loadingTrack: false }));
        say(`Could not play "${titleOf(get(musicPlayback).loadedId)}": the webview could not open the file${code ? ` (media error ${code})` : ''}.`);
    });
    audio = a;
    return a;
}

/** Whether a track can be asked to play now (it exists, it is not missing, it is in reach). */
async function playablePredicate(): Promise<(id: string) => boolean> {
    const env = await getPlaybackEnv();
    const byId = get(musicEntriesById);
    return (id: string) => playbackProblem(byId.get(id), env) === null;
}

function stopAudio() {
    const a = audio;
    if (a) {
        a.pause();
        a.removeAttribute('src');
        a.load();
    }
    releaseSource();
    releaseSource = () => {};
}

/** Make `queue` the queue and load its track at `index`; play it when `autoplay`. */
async function startAt(queue: QueueState, index: number, autoplay: boolean): Promise<boolean> {
    const q = { ...queue, index };
    const id = currentId(q);
    musicPlayback.update(s => ({ ...s, queue: q, loadingTrack: true, currentTime: 0, duration: entryOf(id)?.duration_s ?? 0 }));
    const token = ++loadToken;
    const source = await trackPlaybackUrl(entryOf(id), await getPlaybackEnv());
    if (token !== loadToken) {
        if (source.ok) source.release();
        return false;
    }
    if (!source.ok) {
        stopAudio();
        musicPlayback.update(s => ({ ...s, loadedId: null, playing: false, loadingTrack: false }));
        say(`"${titleOf(id)}": ${source.reason}.`);
        return false;
    }
    const a = ensureAudio();
    if (!a) {
        source.release();
        musicPlayback.update(s => ({ ...s, loadingTrack: false }));
        say('This webview has no audio element.');
        return false;
    }
    releaseSource();
    releaseSource = source.release;
    a.loop = get(musicPlayback).repeat === 'one';
    a.src = source.url;
    audioToken = token;
    musicPlayback.update(s => ({ ...s, loadedId: id, loadingTrack: false }));
    if (autoplay) {
        try {
            await a.play();
        } catch (e) {
            if (token === loadToken) say(`Could not start "${titleOf(id)}": ${e instanceof Error ? e.message : String(e)}`);
        }
    }
    return true;
}

/** Act on `r`, a step computed from `q`: its index is an index into `q`, never
 *  into a queue read later. */
async function applyStep(r: StepResult, q: QueueState, autoplay: boolean): Promise<void> {
    switch (r.kind) {
        case 'restart':
            if (audio && get(musicPlayback).loadedId === currentId(q)) {
                audio.currentTime = 0;
                if (autoplay) await audio.play().catch(() => {});
            } else if (currentId(q)) {
                await startAt(q, q.index, autoplay);
            }
            return;
        case 'play':
            if (r.skipped.length) say(`Skipped ${r.skipped.length} track(s) that cannot play.`);
            if (r.index === q.index && audio && get(musicPlayback).loadedId === currentId(q)) {
                audio.currentTime = 0;
                if (autoplay) await audio.play().catch(() => {});
                return;
            }
            await startAt(q, r.index, autoplay);
            return;
        default:
            say(r.message);
    }
}

/** The queue as it is now, pointed at `id` when `id` is still in it: the step that
 *  follows an `await` is taken from the fresh queue and the track that was current
 *  before the await, so a queue edit in between cannot shift it onto another row. */
function freshQueueAt(id: string | null): QueueState {
    const q = get(musicPlayback).queue;
    if (!id || currentId(q) === id) return q;
    const i = q.ids.indexOf(id);
    return i >= 0 ? { ...q, index: i } : q;
}

async function handleEnded(token: number): Promise<void> {
    const endedId = get(musicPlayback).loadedId;
    const playable = await playablePredicate();
    // A load began while the predicate was read: that load owns the queue now.
    if (token !== loadToken) return;
    const s = get(musicPlayback);
    const q = freshQueueAt(endedId);
    await applyStep(onTrackEnded(q, s.repeat, playable), q, true);
}

/** Next track (N). A step asked for before another load began (the track ended
 *  and moved on, or a row was picked) is dropped: the track it meant to leave is
 *  already left. */
export async function nextTrack(): Promise<void> {
    const token = loadToken;
    const from = currentId(get(musicPlayback).queue);
    const playable = await playablePredicate();
    if (token !== loadToken) return;
    const s = get(musicPlayback);
    const q = freshQueueAt(from);
    await applyStep(stepQueue(q, 1, { repeat: s.repeat, playable }), q, s.playing);
}

/** Previous track (P); past the first PREV_RESTART_S seconds it restarts the track. */
export async function prevTrack(): Promise<void> {
    const token = loadToken;
    const from = currentId(get(musicPlayback).queue);
    const playable = await playablePredicate();
    if (token !== loadToken) return;
    const s = get(musicPlayback);
    const q = freshQueueAt(from);
    const currentTime = s.loadedId ? (audio?.currentTime ?? s.currentTime) : 0;
    await applyStep(stepQueue(q, -1, { repeat: s.repeat, playable, currentTime }), q, s.playing);
}

/** Play / pause (Space). With nothing loaded it starts the current track of the queue,
 *  or the next one that can play. */
export async function togglePlay(): Promise<void> {
    const s = get(musicPlayback);
    const cur = currentId(s.queue);
    if (audio && s.loadedId && s.loadedId === cur) {
        if (audio.paused) await audio.play().catch(e => say(`Could not start: ${e instanceof Error ? e.message : String(e)}`));
        else audio.pause();
        return;
    }
    if (!s.queue.ids.length) {
        say(QUEUE_EMPTY_TEXT);
        return;
    }
    const playable = await playablePredicate();
    const q = freshQueueAt(cur);
    if (cur && currentId(q) === cur && playable(cur)) {
        await startAt(q, q.index, true);
        return;
    }
    // The current track cannot play: the next one that can, from here.
    await applyStep(stepQueue(q, 1, { repeat: get(musicPlayback).repeat, playable }), q, true);
}

/** Play a list from `startId`, or from its start (a random track with shuffle on). */
export async function playList(list: Playlist, startId: string | null = null): Promise<void> {
    const s = get(musicPlayback);
    const q = queueFromList(list.id, list.track_ids, { startId, shuffle: s.shuffle, playable: await playablePredicate() });
    if (!q) {
        say(list.track_ids.length ? `Nothing in "${list.name}" can play yet.` : `"${list.name}" is empty.`);
        return;
    }
    await startAt(q, q.index, true);
}

/** Queue: add to the end of Up next, or (`next`) right after the current track. */
export function queueTrack(id: string, next = false): void {
    musicPlayback.update(s => ({ ...s, queue: enqueue(s.queue, [id], next) }));
    say(`Added "${titleOf(id)}" to Up next.`);
}

/** Play the queue's row `index` now. */
export async function playQueueAt(index: number): Promise<void> {
    const q = get(musicPlayback).queue;
    if (index < 0 || index >= q.ids.length) return;
    await startAt(q, index, true);
}

export function removeFromQueue(index: number): void {
    musicPlayback.update(s => ({ ...s, queue: queueRemoveAt(s.queue, index) }));
}

export function clearQueue(): void {
    musicPlayback.update(s => ({ ...s, queue: queueClear(s.queue) }));
}

/** Shuffle (Shift+S). */
export function setShuffle(on: boolean): void {
    const s = get(musicPlayback);
    let queue = s.queue;
    let text: string;
    if (on) {
        queue = shuffleOn(queue);
        text = 'Shuffle on: the rest of the queue is shuffled.';
    } else {
        const list = get(musicLibrary).playlists.find(p => p.id === queue.context);
        const r = shuffleOff(queue, list?.track_ids ?? null);
        queue = r.queue;
        text = r.restored ? 'Shuffle off: back to the list order.' : 'Shuffle off.';
    }
    musicPlayback.update(x => ({ ...x, shuffle: on, queue }));
    remember();
    say(text);
}

/** Repeat off → all → one (Shift+R). */
export function cycleRepeatMode(): void {
    const repeat = cycleRepeat(get(musicPlayback).repeat);
    musicPlayback.update(s => ({ ...s, repeat }));
    if (audio) audio.loop = repeat === 'one';
    remember();
    say(repeat === 'one' ? 'Repeat: one (the track loops; Next and Prev still move).'
        : repeat === 'all' ? 'Repeat: all (the queue wraps around).' : 'Repeat: off.');
}

export function seekTo(seconds: number): void {
    if (!audio || !get(musicPlayback).loadedId) return;
    const d = Number.isFinite(audio.duration) ? audio.duration : Infinity;
    audio.currentTime = Math.max(0, Math.min(d, seconds));
}

export function seekBy(delta: number): void {
    if (!audio) return;
    seekTo(audio.currentTime + delta);
}

const rememberVolume = throttle(remember, VOLUME_SAVE_MS);

/** Set the volume at once; it is written to storage at most every VOLUME_SAVE_MS
 *  while the slider moves, and once more when it is let go (`final`). */
export function setVolume(volume: number, final = false): void {
    const v = Math.max(0, Math.min(1, volume));
    if (audio) audio.volume = v;
    musicPlayback.update(s => ({ ...s, volume: v }));
    rememberVolume.call();
    if (final) rememberVolume.flush();
}

export function clearNotice(): void {
    say('');
}

export { say as musicNotice };
