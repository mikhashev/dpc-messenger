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
//
// **Review playback** (the Create panel's A/B of a job's takes) uses the same audio
// element, the same `trackPlaybackUrl` and the same wait for the duration before a seek
// or a play (WebKitGTK, 36a4777a): there is one playback path. The review takes the
// element over for as long as it plays: the queue itself is never changed, its track and
// position are remembered, and when the review ends (`endReview`: the review view is
// left, the window closed or minimised) the queue's track is loaded back, paused, at that
// position. Pressing Play, Next, Prev or a row of the queue during a review hands the
// element back to the queue at once (Play resumes at the remembered position). The
// review's level-match gain multiplies the person's volume; it never raises it.

import { derived, get, writable } from 'svelte/store';
import {
    addMusicFolder,
    alignMusicLyrics as coreAlignMusicLyrics,
    createMusicPlaylist,
    deleteGeneratedMusicFile,
    deleteMusicPlaylist,
    hideMusicTrack,
    listMusicFolders,
    listMusicLibrary,
    listMusicPlaylists,
    removeMissingMusic,
    removeMusicFolder,
    rescanMusicFolder,
    sendCommand,
    unhideMusicTrack,
    updateMusicPlaylist,
} from '$lib/coreService';
import { detectWindows } from '$lib/utils/attachmentSrc';
import { log } from '$lib/logger';
import {
    EMPTY_QUEUE,
    MUSIC_UNAVAILABLE_TEXT,
    QUEUE_EMPTY_TEXT,
    currentId,
    cycleRepeat,
    endedReason,
    enqueue,
    formatRanges,
    resolveDuration,
    loadMusicPreferences,
    loopWrapped,
    onTrackEnded,
    playbackProblem,
    playerStateLine,
    playableIds,
    positionLabel,
    queueClear,
    queueDrop,
    queueFromList,
    queueRemoveAt,
    saveMusicPreferences,
    shuffleOff,
    shuffleOn,
    stepLogLine,
    stepQueue,
    throttle,
    type StepReason,
    trackPlaybackUrl,
    VOLUME_SAVE_MS,
    callMusic,
    lrclibLookupPayload,
    transcribeLyricsPayload,
    type LibraryEntry,
    type LyricsAnswer,
    type MusicAnswer,
    type MusicFolder,
    type PlaybackEnv,
    type Playlist,
    type StreamInfo,
    type QueueState,
    type RepeatMode,
    type StepResult,
    type PlaybackSource,
} from '$lib/utils/musicPlayer';
import { abSwitchPosition } from '../utils/musicCreate';
import { openTrackPlan } from '../utils/musicAttachment';

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
/** Whether the window is open and showing the library: only then does an event re-read it. */
let watching = false;
/** A read was asked for while one was under way: it may have started before the change
 *  the asker wants to see (a take hidden by Discard), so one more quiet read follows. */
let readAgain = false;

/** Read the library, the playlists and the folders. `quiet` keeps what is shown
 *  while reading (the window's poll); otherwise the window shows "Loading". */
export async function loadMusicLibrary(quiet = false): Promise<void> {
    if (loading) {
        readAgain = true;
        return;
    }
    loading = true;
    if (!quiet) musicLibrary.update(l => ({ ...l, status: l.status === 'ready' ? 'ready' : 'loading', message: '' }));
    const heldId = get(musicPlayback).loadedId;
    const heldBefore = entryOf(heldId);
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
        noteRefresh(heldId, heldBefore);
    } finally {
        loading = false;
        if (readAgain) {
            readAgain = false;
            void loadMusicLibrary(true);
        }
    }
}

/** The window tells whether it is open and showing the library. It reads once when it opens
 *  (its own call); after that the core's `music_library_changed` is the only trigger. */
export function setMusicLibraryWatch(on: boolean): void {
    watching = on;
    if (!on && eventTimer !== null) {
        clearTimeout(eventTimer);
        eventTimer = null;
    }
}

/** The least time between two reads the core's events start (hashing sends up to two events a second,
 *  and each read is the whole library, the playlists and the folders): an event inside it waits for one
 *  trailing read at its end, so the last change is always read and the burst costs one read a second. */
export const EVENT_REREAD_MIN_MS = 1000;
let lastEventReadAt = 0;
let eventTimer: ReturnType<typeof setTimeout> | null = null;

function readForAnEvent(): void {
    lastEventReadAt = Date.now();
    void loadMusicLibrary(true);
}

/** A `music_library_changed` event from the core: read the library again, once, when the
 *  window shows a library that has been read. A closed window reads nothing; it reads on open.
 *  An event that comes while a read is under way (the first read on opening included, whose status is
 *  `loading`) is remembered and read once more when that read ends: the read may have started before
 *  the change. A burst of events is coalesced (`EVENT_REREAD_MIN_MS`). */
export function noteMusicLibraryChanged(_payload?: unknown): void {
    if (!watching) return;
    if (loading) {
        readAgain = true;
        return;
    }
    if (get(musicLibrary).status !== 'ready') return;
    const wait = lastEventReadAt + EVENT_REREAD_MIN_MS - Date.now();
    if (wait <= 0) {
        readForAnEvent();
        return;
    }
    if (eventTimer !== null) return;
    eventTimer = setTimeout(() => {
        eventTimer = null;
        if (watching && get(musicLibrary).status === 'ready') readForAnEvent();
    }, wait);
}

/** The library refresh never touches the audio element, `currentTime`, the queue or
 *  `loadedId` (it only replaces the store). When it changes the loaded track's entry in a way
 *  that matters for playback, say so in the log, so a playback glitch is not blamed on it. */
function noteRefresh(heldId: string | null, before: LibraryEntry | null): void {
    if (!heldId) return;
    const after = entryOf(heldId);
    if (!after) {
        dlog(`refresh: the loaded track ${heldId} is no longer in the library; playback is not touched`);
    } else if (before && (before.path !== after.path || !!before.missing !== !!after.missing || before.source?.mime !== after.source?.mime)) {
        dlog(`refresh: the loaded track ${heldId} changed (path ${before.path === after.path ? 'same' : 'moved'}, missing ${!!before.missing}->${!!after.missing}, mime ${before.source?.mime ?? 'none'}->${after.source?.mime ?? 'none'}); playback is not touched`);
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
        env.streamInfo = fetchStreamInfo;
    } catch (e) {
        console.warn('[music] the Tauri API did not load; nothing can play', e);
    }
    return env;
}

/** The core's track stream address and token (ADR-051 N8), asked at every track start:
 *  the token is the core process's, so a restarted core is picked up. */
async function fetchStreamInfo(): Promise<StreamInfo> {
    const answer = await callMusic<StreamInfo>(sendCommand, 'get_music_stream_info');
    if (!answer.ok) throw new Error(answer.message);
    return answer.data;
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
/** The load whose source was already reloaded once after a media error (a read failure
 *  while the file changed under the player). One reload per load: when the reloaded
 *  source fails too, the error falls through to the notice and nothing is reloaded
 *  again until a new load (a track change, a row picked) takes the element. */
let errorReloadToken = -1;
const DURATION_WAIT_MS = 4000;

/** Who holds the audio element: the queue, or a review of a music job's takes. */
let owner: 'queue' | 'review' = 'queue';

// -- diagnostics -----------------------------------------------------------------
// One compact debug line per media event, to ui.log through the console relay
// (grep for "[music]" in ~/.dpc/logs/ui.log). `timeupdate` is never logged. Events that can
// repeat (waiting, stalled, suspend, ...) are cut to one line per second per kind with a
// count of what was folded; seeks and errors are always logged (the relay caps at 50/s).

function dlog(message: string): void {
    try {
        log.debug('music', message);
    } catch { /* a log line never breaks playback */ }
}

const NEVER_FOLDED = new Set(['seek', 'seeking', 'seeked', 'error', 'ended', 'loadedmetadata', 'source', 'track', 'state', 'loop']);
const FOLD_MS = 1000;
const lastLogged = new Map<string, { at: number; folded: number }>();

function dlogEvent(kind: string, message: string): void {
    if (!NEVER_FOLDED.has(kind)) {
        const now = Date.now();
        const last = lastLogged.get(kind);
        if (last && now - last.at < FOLD_MS) {
            last.folded++;
            return;
        }
        const folded = last?.folded ?? 0;
        lastLogged.set(kind, { at: now, folded: 0 });
        if (folded) message += ` (+${folded} folded)`;
    }
    dlog(message);
}

/** The player's state for a log line: repeat, shuffle, the queue's length and position, the element's loop. */
function playerState(): string {
    const s = get(musicPlayback);
    return playerStateLine({ repeat: s.repeat, shuffle: s.shuffle, queue: s.queue, loop: audio ? audio.loop : null });
}

/** The element's state at this moment, for a log line. */
function snapshot(a: HTMLAudioElement): string {
    return `t=${(a.currentTime ?? 0).toFixed(2)} readyState=${a.readyState} networkState=${a.networkState} ` +
        `buffered=${formatRanges(a.buffered)} seekable=${formatRanges(a.seekable)} paused=${a.paused}`;
}

/** The last seek asked for (by the slider or a key), to compare with where the element lands. */
let seekRequested: number | null = null;
let uaLogged = false;

function attachDiagnostics(a: HTMLAudioElement): void {
    const tag = () => `#${audioToken} ${get(musicPlayback).loadedId ?? '-'}`;
    for (const type of ['waiting', 'stalled', 'suspend', 'playing', 'pause', 'play', 'emptied', 'abort']) {
        a.addEventListener(type, () => dlogEvent(type, `${type} ${tag()} ${snapshot(a)}`));
    }
    a.addEventListener('loadedmetadata', () => {
        dlogEvent('loadedmetadata', `loadedmetadata ${tag()} duration=${Number.isFinite(a.duration) ? a.duration.toFixed(2) : a.duration} ${snapshot(a)}`);
    });
    a.addEventListener('seeking', () => {
        dlogEvent('seeking', `seeking ${tag()} requested=${seekRequested?.toFixed(2) ?? 'none'} actual=${a.currentTime.toFixed(2)} ${snapshot(a)}`);
    });
    a.addEventListener('seeked', () => {
        const delta = seekRequested == null ? 'n/a' : (a.currentTime - seekRequested).toFixed(2);
        dlogEvent('seeked', `seeked ${tag()} requested=${seekRequested?.toFixed(2) ?? 'none'} actual=${a.currentTime.toFixed(2)} delta=${delta} ${snapshot(a)}`);
    });
    a.addEventListener('ended', () => dlogEvent('ended', `ended ${tag()} ${snapshot(a)} ${playerState()}`));
    a.addEventListener('error', () => {
        if (!a.getAttribute('src')) return;
        dlogEvent('error', `error ${tag()} code=${a.error?.code ?? 'none'} message=${JSON.stringify(a.error?.message ?? '')} ${snapshot(a)}`);
    });
}

function ensureAudio(): HTMLAudioElement | null {
    if (audio || typeof Audio === 'undefined') return audio;
    const a = new Audio();
    // WebKitGTK with preload='metadata' fetches only the header and then feeds the
    // element on demand: duration stays 0 and forward seeks are refused (Linux node,
    // WebKitGTK 2.50.4, 2026-11-20). 'auto' downloads the whole track first, as the
    // chat VoicePlayer does, and gives full duration and seekable. Tracks are a few MB.
    a.preload = isLinuxWebview() && !detectWindows() ? 'auto' : 'metadata';
    a.volume = get(musicPlayback).volume;
    attachDiagnostics(a);
    a.addEventListener('play', () => {
        if (owner === 'review') musicReview.update(r => ({ ...r, playing: true }));
        else musicPlayback.update(s => ({ ...s, playing: true }));
    });
    a.addEventListener('pause', () => {
        if (owner === 'review') musicReview.update(r => ({ ...r, playing: false }));
        else musicPlayback.update(s => ({ ...s, playing: false }));
    });
    let lastUpdate = 0;
    a.addEventListener('timeupdate', () => {
        if (!current()) return;
        // Repeat one makes the element loop by itself, with no `ended`: the wrap is the only trace.
        if (owner === 'queue' && a.loop && loopWrapped(lastUpdate, a.currentTime)) {
            dlogEvent('loop', `loop-restart #${audioToken} ${get(musicPlayback).loadedId ?? '-'} from=${lastUpdate.toFixed(2)} to=${a.currentTime.toFixed(2)} ${playerState()}`);
        }
        lastUpdate = a.currentTime;
        if (owner === 'review') musicReview.update(r => ({ ...r, currentTime: a.currentTime }));
        else musicPlayback.update(s => ({ ...s, currentTime: a.currentTime }));
    });
    const takeDuration = () => {
        if (!current()) return;
        if (owner === 'review') {
            const d = resolveDuration(a.duration, entryOf(get(musicReview).trackId)?.duration_s, a.currentTime);
            if (d > 0) musicReview.update(r => (r.duration === d ? r : { ...r, duration: d }));
            return;
        }
        const d = resolveDuration(a.duration, entryOf(get(musicPlayback).loadedId)?.duration_s, a.currentTime);
        if (d > 0) musicPlayback.update(s => (s.duration === d ? s : { ...s, duration: d }));
    };
    a.addEventListener('loadedmetadata', takeDuration);
    a.addEventListener('durationchange', takeDuration);
    a.addEventListener('ended', () => {
        if (!current()) return;
        // A take ends where it ends: the review never moves the queue on.
        if (owner === 'review') musicReview.update(r => ({ ...r, playing: false }));
        else void handleEnded(audioToken);
    });
    a.addEventListener('error', () => {
        if (!a.getAttribute('src') || !current()) return;
        if (owner === 'review') {
            const code = a.error?.code;
            musicReview.update(r => ({ ...r, playing: false, loading: false, notice: `The webview could not open the take${code ? ` (media error ${code})` : ''}.` }));
            return;
        }
        const code = a.error?.code;
        const s = get(musicPlayback);
        // Code 2 (a read/source failure) on the loaded track is most often the file being
        // rewritten under the player (a tag upsert moves the audio data; the lyrics align
        // pipeline stopped playback this way, 2026-11-27). Whoever rewrote it: the player
        // only sees its source fail, so it loads the same source once more and goes on
        // from where it stood (a short audible gap). When the reloaded source fails too,
        // the notice below stands and nothing is reloaded until a new load begins.
        if (code === 2 && s.loadedId && audioToken !== errorReloadToken) {
            const at = Number.isFinite(a.currentTime) ? a.currentTime : s.currentTime;
            const q = freshQueueAt(s.loadedId);
            if (currentId(q) === s.loadedId) {
                dlogEvent('error', `error reload #${audioToken} ${s.loadedId} at=${at.toFixed(2)}: the source is loaded again`);
                void startAt(q, q.index, s.playing, at);
                errorReloadToken = loadToken; // the token startAt has just taken
                return;
            }
        }
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

/** Resolve once `el` reports a finite duration > 0 (checked now, then on `loadedmetadata` /
 *  `durationchange`), after `timeoutMs`, or when `isStale()` turns true (a newer track
 *  request; polled every 100 ms). Never rejects. */
export function waitForDuration(
    el: Pick<HTMLAudioElement, 'duration' | 'addEventListener' | 'removeEventListener'>,
    timeoutMs: number,
    isStale: () => boolean = () => false,
): Promise<'ok' | 'timeout' | 'stale'> {
    const ready = () => Number.isFinite(el.duration) && el.duration > 0;
    if (ready()) return Promise.resolve('ok');
    return new Promise(resolve => {
        const finish = (r: 'ok' | 'timeout' | 'stale') => {
            clearTimeout(timer);
            clearInterval(poll);
            el.removeEventListener('loadedmetadata', check);
            el.removeEventListener('durationchange', check);
            resolve(r);
        };
        const check = () => {
            if (isStale()) finish('stale');
            else if (ready()) finish('ok');
        };
        const timer = setTimeout(() => finish('timeout'), timeoutMs);
        const poll = setInterval(() => { if (isStale()) finish('stale'); }, 100);
        el.addEventListener('loadedmetadata', check);
        el.addEventListener('durationchange', check);
    });
}

/** Make `queue` the queue and load its track at `index`; play it when `autoplay`. From
 *  `resumeAt` seconds when given (a queue track loaded back after a review). */
async function startAt(queue: QueueState, index: number, autoplay: boolean, resumeAt = 0): Promise<boolean> {
    const q = { ...queue, index };
    const id = currentId(q);
    if (owner === 'review') {
        // The queue takes the element back from a review (Play, Next, a row, or endReview).
        if (!resumeAt && queueHeld && queueHeld.id === id) resumeAt = queueHeld.time;
        leaveReview();
    }
    musicPlayback.update(s => ({ ...s, queue: q, loadingTrack: true, currentTime: resumeAt, duration: entryOf(id)?.duration_s ?? 0 }));
    const token = ++loadToken;
    const source = await trackPlaybackUrl(entryOf(id), await getPlaybackEnv());
    if (token !== loadToken) {
        if (source.ok) source.release();
        return false;
    }
    if (!source.ok) {
        dlogEvent('source', `source #${token} ${id} failed: ${source.problem}: ${source.reason}`);
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
    if (!uaLogged) {
        uaLogged = true;
        dlogEvent('source', `webview ${typeof navigator !== 'undefined' ? navigator.userAgent : 'unknown'}`);
    }
    let platformIsLinux = false;
    {
        const env = await getPlaybackEnv();
        platformIsLinux = env.isLinux;
        const platform = env.isLinux ? 'linux' : env.isWindows ? 'windows' : 'macos-or-other';
        dlogEvent('source', `source #${token} ${id} ${entryOf(id)?.source?.name ?? ''} via=${source.via} mime=${source.mime || 'none'} bytes=${source.bytes ?? 'n/a'}${source.via === 'http' ? ` ranges=${source.ranges === false ? 0 : 1}` : ''} platform=${platform}${source.fallback ? ` fallback=${source.fallback}` : ''}`);
    }
    seekRequested = null;
    releaseSource();
    releaseSource = source.release;
    a.loop = get(musicPlayback).repeat === 'one';
    a.volume = get(musicPlayback).volume;
    a.src = source.url;
    audioToken = token;
    musicPlayback.update(s => ({ ...s, loadedId: id, loadingTrack: false }));
    if (resumeAt > 0) {
        // A seek needs the duration first (and on WebKitGTK it is refused before it).
        await waitForDuration(a, DURATION_WAIT_MS, () => token !== loadToken);
        if (token !== loadToken) return true;
        seekRequested = resumeAt;
        a.currentTime = resumeAt;
    }
    if (autoplay) {
        try {
            if (platformIsLinux) {
                // WebKitGTK reports duration=0 and refuses forward seeks when play() follows
                // src at once; waiting for the duration first fixes both (Zcode, 2026-11-20).
                const t0 = Date.now();
                const r = await waitForDuration(a, DURATION_WAIT_MS, () => token !== loadToken);
                dlogEvent('source', `waited ${Date.now() - t0}ms for duration before play (${r === 'ok' ? 'ok' : 'timeout'})`);
                if (token !== loadToken) return true;
            }
            await a.play();
        } catch (e) {
            if (token === loadToken) say(`Could not start "${titleOf(id)}": ${e instanceof Error ? e.message : String(e)}`);
        }
    }
    return true;
}

/** Act on `r`, a step computed from `q`: its index is an index into `q`, never
 *  into a queue read later. */
async function applyStep(r: StepResult, q: QueueState, autoplay: boolean, reason: StepReason): Promise<void> {
    const s = get(musicPlayback);
    dlogEvent('track', stepLogLine(reason, r, { repeat: s.repeat, shuffle: s.shuffle, queue: q, loop: audio ? audio.loop : null }));
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
    await applyStep(onTrackEnded(q, s.repeat, playable), q, true, endedReason(s.repeat));
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
    await applyStep(stepQueue(q, 1, { repeat: s.repeat, playable }), q, s.playing, 'next');
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
    await applyStep(stepQueue(q, -1, { repeat: s.repeat, playable, currentTime }), q, s.playing, 'prev');
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
    await applyStep(stepQueue(q, 1, { repeat: get(musicPlayback).repeat, playable }), q, true, 'play');
}

/** Play a list from `startId`, or from its start (a random track with shuffle on). */
export async function playList(list: Playlist, startId: string | null = null): Promise<void> {
    const s = get(musicPlayback);
    const q = queueFromList(list.id, playableIds(list.track_ids, get(musicEntriesById)), { startId, shuffle: s.shuffle, playable: await playablePredicate() });
    dlogEvent('track', `track-change reason=list list=${list.id} ${q ? `-> play ${currentId(q)}` : '-> nothing playable'} ${playerState()}`);
    if (!q) {
        say(list.track_ids.length ? `Nothing in "${list.name}" can play yet.` : `"${list.name}" is empty.`);
        return;
    }
    await startAt(q, q.index, true);
}

/** Queue: add to the end of Up next, or (`next`) right after the current track. */
export function queueTrack(id: string, next = false): void {
    if (entryOf(id)?.hidden) {
        say(`"${titleOf(id)}" is hidden: unhide it to queue it.`);
        return;
    }
    musicPlayback.update(s => ({ ...s, queue: enqueue(s.queue, [id], next) }));
    say(`Added "${titleOf(id)}" to Up next.`);
}

/** Play the queue's row `index` now. */
export async function playQueueAt(index: number): Promise<void> {
    const q = get(musicPlayback).queue;
    if (index < 0 || index >= q.ids.length) return;
    dlogEvent('track', `track-change reason=row from=${currentId(q) ?? '-'} -> play ${q.ids[index]} (index ${index + 1}) ${playerState()}`);
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
    dlogEvent('state', `shuffle ${on ? 'on' : 'off'} ${playerState()}`);
    remember();
    say(text);
}

/** Repeat off → all → one (Shift+R). */
export function cycleRepeatMode(): void {
    const repeat = cycleRepeat(get(musicPlayback).repeat);
    musicPlayback.update(s => ({ ...s, repeat }));
    if (audio && owner === 'queue') audio.loop = repeat === 'one';
    dlogEvent('state', `repeat ${repeat} ${playerState()}`);
    remember();
    say(repeat === 'one' ? 'Repeat: one (the track loops; Next and Prev still move).'
        : repeat === 'all' ? 'Repeat: all (the queue wraps around).' : 'Repeat: off.');
}

export function seekTo(seconds: number, origin: 'slider' | 'key' | 'ui' = 'ui'): void {
    if (!audio || !get(musicPlayback).loadedId) return;
    // The element's duration may be 0 or a bogus partial one (WebKitGTK); the library's
    // figure stands then. A target past `seekable` is still attempted, and logged.
    const d = resolveDuration(audio.duration, entryOf(get(musicPlayback).loadedId)?.duration_s, audio.currentTime) || Infinity;
    const target = Math.max(0, Math.min(d, seconds));
    seekRequested = target;
    const sk = audio.seekable;
    let seekableEnd = 0;
    try { seekableEnd = sk && sk.length ? sk.end(sk.length - 1) : 0; } catch { /* unreadable */ }
    const beyond = target > seekableEnd + 0.5 ? ' beyond-seekable' : '';
    dlogEvent('seek', `seek request (${origin}) target=${target.toFixed(2)} from=${audio.currentTime.toFixed(2)}${beyond} ${snapshot(audio)}`);
    audio.currentTime = target;
}

export function seekBy(delta: number): void {
    if (!audio) return;
    seekTo(audio.currentTime + delta, 'key');
}

const rememberVolume = throttle(remember, VOLUME_SAVE_MS);

/** Set the volume at once; it is written to storage at most every VOLUME_SAVE_MS
 *  while the slider moves, and once more when it is let go (`final`). */
export function setVolume(volume: number, final = false): void {
    const v = Math.max(0, Math.min(1, volume));
    if (audio) audio.volume = owner === 'review' ? v * reviewGain : v;
    musicPlayback.update(s => ({ ...s, volume: v }));
    rememberVolume.call();
    if (final) rememberVolume.flush();
}

// -- lyrics ----------------------------------------------------------------------

/** "Find on LRCLIB" (the only call that sends anything to lrclib.net; the core makes
 *  it, never the webview). `replace` applies a found result over stored lyrics; over
 *  the person's own (manual or edited) the core also needs `overwriteManual`, which the
 *  Lyrics area passes only after the person confirms. */
export function lookupLyricsOnLrclib(
    trackId: string,
    query: { artist?: string; title?: string; album?: string } = {},
    opts: { replace?: boolean; overwriteManual?: boolean } = {},
): Promise<MusicAnswer<LyricsAnswer>> {
    return callMusic<LyricsAnswer>(sendCommand, 'lookup_music_lyrics_lrclib', lrclibLookupPayload(trackId, query, opts));
}

/** "Transcribe" (the lyrics chain's explicit step 4): the core runs DPC's transcription
 *  on the track's file and stores the lines as `source: transcribe`. Over the person's
 *  own lyrics the core also needs `overwriteManual`, which the Lyrics area passes only
 *  after the person confirms. A long run can outlast this call's timeout; the core goes
 *  on, and the Lyrics area follows it through get_music_lyrics (`runTranscribe`). */
export function transcribeMusicLyrics(
    trackId: string,
    opts: { overwriteManual?: boolean } = {},
): Promise<MusicAnswer<LyricsAnswer>> {
    return callMusic<LyricsAnswer>(sendCommand, 'transcribe_music_lyrics', transcribeLyricsPayload(trackId, opts));
}

/** "Align to audio": aligns existing unsynced lyrics to Whisper word timestamps while keeping their text verbatim. */
export function alignMusicLyrics(
    trackId: string,
    opts: { overwriteManual?: boolean } = {},
): Promise<MusicAnswer<LyricsAnswer>> {
    return coreAlignMusicLyrics(trackId, opts);
}

export function clearNotice(): void {
    say('');
}

export { say as musicNotice };

// -- open a track from elsewhere (a chat bubble's "Open in player") -----------------

/** Bumped by `openTrackInPlayer`; the page watches it and opens the window. The window
 *  is page state (`showMusicPlayer`), so a bubble far below it asks through a store. */
export const musicWindowRequests = writable(0);

/** Open the Music Player on a library track and play it, as a chat bubble asks.
 *  The track plays next in the current queue (what is queued stays), and a track that
 *  is already playing is left playing. Resolves true when the track is playing or
 *  loading; false when the window opened with a notice saying why not. */
/** Open a track in the player and play it. `allowHidden` plays a hidden track without
 *  unhiding it (the Evolution card's discarded takes); otherwise a hidden track is refused. */
export async function openTrackInPlayer(trackId: string, opts: { allowHidden?: boolean } = {}): Promise<boolean> {
    if (!entryOf(trackId)) await loadMusicLibrary(true);
    // The window opens whatever follows: the notice is shown there.
    musicWindowRequests.update(n => n + 1);
    const entry = entryOf(trackId);
    const s0 = get(musicPlayback);
    const plan = openTrackPlan(entry, s0.loadedId, s0.playing, !!opts.allowHidden);
    if (plan === 'missing') {
        say('This track is not in the library (yet). If it is a take still in review, keep it first.');
        return false;
    }
    if (plan === 'hidden') {
        say(`"${titleOf(trackId)}" is hidden: unhide it to play it.`);
        return false;
    }
    if (plan === 'already-playing') return true;
    const problem = playbackProblem(entry, await getPlaybackEnv());
    if (problem) {
        say(`"${titleOf(trackId)}": ${problem.reason}.`);
        return false;
    }
    const q = enqueue(get(musicPlayback).queue, [trackId], true);
    const index = q.ids.indexOf(trackId);
    if (index < 0) return false;
    return startAt(q, index, true);
}

// -- hide, remove, delete --------------------------------------------------------

/** Hide a track. A hidden track leaves the queue (the one playing now plays on). */
export async function hideTrack(id: string): Promise<MusicAnswer<{ entry: LibraryEntry }>> {
    const a = noteAnswer(await hideMusicTrack(id));
    if (a.ok) {
        const playing = currentId(get(musicPlayback).queue);
        if (playing !== id) musicPlayback.update(s => ({ ...s, queue: queueDrop(s.queue, [id]) }));
        await loadMusicLibrary(true);
    }
    return a;
}

export async function unhideTrack(id: string): Promise<MusicAnswer<{ entry: LibraryEntry }>> {
    const a = noteAnswer(await unhideMusicTrack(id));
    if (a.ok) await loadMusicLibrary(true);
    return a;
}

/** Stop the audio if it holds one of `ids`, and take them out of the queue. */
function forgetInPlayback(ids: readonly string[]): void {
    const r = get(musicReview);
    if (owner === 'review' && r.trackId && ids.includes(r.trackId)) {
        loadToken++;
        if (audio) {
            audio.pause();
            audio.removeAttribute('src');
            audio.load();
        }
        musicReview.update(x => ({ ...x, trackId: null, playing: false, loading: false, currentTime: 0 }));
    }
    for (const id of ids) {
        reviewSources.get(id)?.release();
        reviewSources.delete(id);
    }
    const s = get(musicPlayback);
    if (s.loadedId && ids.includes(s.loadedId)) {
        loadToken++;
        stopAudio();
        musicPlayback.update(x => ({ ...x, loadedId: null, playing: false, loadingTrack: false, currentTime: 0 }));
    }
    musicPlayback.update(x => ({ ...x, queue: queueDrop(x.queue, ids) }));
}

/** Remove a missing entry, or (no id) every missing one, from the library for good. */
export async function removeMissingTracks(id?: string): Promise<MusicAnswer<{ removed: string[] }>> {
    const a = noteAnswer(await removeMissingMusic(id));
    if (a.ok) {
        forgetInPlayback(a.data.removed);
        await loadMusicLibrary(true);
    }
    return a;
}

/** Delete a DPC-made file. The audio element lets go of the file first (Windows will not
 *  delete an open file), so deleting the track that is playing stops playback and the
 *  queue moves to the next track, not started. */
export async function deleteGeneratedFile(id: string): Promise<MusicAnswer<{ id: string; title: string; deleted: string[] }>> {
    forgetInPlayback([id]);
    const a = noteAnswer(await deleteGeneratedMusicFile(id));
    await loadMusicLibrary(true);
    return a;
}

// -- review playback (the Create panel's A/B; see the header) --------------------

export interface ReviewPlaybackState {
    /** The review holds the audio element. */
    active: boolean;
    /** The library entry of the take loaded, or null. */
    trackId: string | null;
    playing: boolean;
    loading: boolean;
    currentTime: number;
    duration: number;
    /** The level-match gain applied on top of the person's volume (<= 1). */
    gain: number;
    notice: string;
}

const EMPTY_REVIEW: ReviewPlaybackState = {
    active: false, trackId: null, playing: false, loading: false, currentTime: 0, duration: 0, gain: 1, notice: '',
};

export const musicReview = writable<ReviewPlaybackState>(EMPTY_REVIEW);

let reviewGain = 1;
/** The resolved sources of the takes heard in this review, kept so an A/B switch does not
 *  read the file again; released when the review ends. */
const reviewSources = new Map<string, Extract<PlaybackSource, { ok: true }>>();
/** The queue's track and position when the review took the element over. */
let queueHeld: { id: string; time: number } | null = null;

function releaseReviewSources(): void {
    for (const s of reviewSources.values()) s.release();
    reviewSources.clear();
}

/** The review lets go of the element (the caller loads or stops it next). */
function leaveReview(): void {
    owner = 'queue';
    queueHeld = null;
    reviewGain = 1;
    releaseReviewSources();
    musicReview.set(EMPTY_REVIEW);
}

function clampGain(g: number | undefined): number {
    return typeof g === 'number' && Number.isFinite(g) ? Math.max(0, Math.min(1, g)) : 1;
}

/** Load a take (its library entry `trackId`) into the element at `at` seconds (clamped to
 *  the take's length), with the level-match `gain`, and play it when `autoplay`. The
 *  first call takes the element from the queue (see the header). False when it could not
 *  load, or a newer load overtook it; `musicReview.notice` says why. */
export async function reviewPlay(trackId: string, opts: { at?: number; autoplay?: boolean; gain?: number } = {}): Promise<boolean> {
    const token = ++loadToken;
    const a0 = audio;
    if (owner === 'queue') {
        const s = get(musicPlayback);
        queueHeld = s.loadedId && a0 ? { id: s.loadedId, time: a0.currentTime } : null;
        owner = 'review';
        a0?.pause();
        // The queue's own source (a `blob:` on the Linux fallback) is let go; the queue
        // resolves it again when it takes the element back.
        releaseSource();
        releaseSource = () => {};
        musicPlayback.update(x => ({ ...x, loadedId: null, playing: false, loadingTrack: false }));
    }
    reviewGain = clampGain(opts.gain ?? reviewGain);
    musicReview.update(r => ({ ...r, active: true, trackId, loading: true, gain: reviewGain, notice: '' }));
    let source = reviewSources.get(trackId);
    if (!source) {
        let entry = entryOf(trackId);
        if (!entry) {
            await loadMusicLibrary(true);
            entry = entryOf(trackId);
        }
        const s = await trackPlaybackUrl(entry, await getPlaybackEnv());
        if (token !== loadToken || owner !== 'review') {
            if (s.ok) s.release();
            return false;
        }
        if (!s.ok) {
            dlogEvent('source', `review source #${token} ${trackId} failed: ${s.problem}: ${s.reason}`);
            musicReview.update(r => ({ ...r, loading: false, playing: false, notice: `This take cannot play: ${s.reason}.` }));
            return false;
        }
        reviewSources.set(trackId, s);
        source = s;
    }
    const a = ensureAudio();
    if (!a) {
        musicReview.update(r => ({ ...r, loading: false, notice: 'This webview has no audio element.' }));
        return false;
    }
    dlogEvent('source', `review source #${token} ${trackId} via=${source.via} mime=${source.mime || 'none'} at=${(opts.at ?? 0).toFixed(2)} gain=${reviewGain.toFixed(3)}`);
    seekRequested = null;
    a.loop = false;
    a.volume = get(musicPlayback).volume * reviewGain;
    a.src = source.url;
    audioToken = token;
    // The seek needs the duration (WebKitGTK refuses it before), and so does the clamp.
    await waitForDuration(a, DURATION_WAIT_MS, () => token !== loadToken);
    if (token !== loadToken) return false;
    const duration = resolveDuration(a.duration, entryOf(trackId)?.duration_s, 0);
    const at = abSwitchPosition(opts.at ?? 0, duration);
    if (at > 0) {
        seekRequested = at;
        a.currentTime = at;
    }
    musicReview.update(r => ({ ...r, loading: false, duration, currentTime: at }));
    if (opts.autoplay) {
        try {
            await a.play();
        } catch (e) {
            if (token === loadToken) musicReview.update(r => ({ ...r, notice: `Could not start the take: ${e instanceof Error ? e.message : String(e)}` }));
        }
    }
    return true;
}

/** Play / pause the take loaded in the review. False when the review holds nothing. */
export async function reviewTogglePlay(): Promise<boolean> {
    if (owner !== 'review' || !audio || !get(musicReview).trackId || !audio.getAttribute('src')) return false;
    if (audio.paused) await audio.play().catch(e => musicReview.update(r => ({ ...r, notice: `Could not start: ${e instanceof Error ? e.message : String(e)}` })));
    else audio.pause();
    return true;
}

/** Seek the take loaded in the review (clamped to its length). */
export function reviewSeek(seconds: number): void {
    if (owner !== 'review' || !audio || !get(musicReview).trackId) return;
    const target = abSwitchPosition(seconds, get(musicReview).duration);
    seekRequested = target;
    dlogEvent('seek', `seek request (review) target=${target.toFixed(2)} from=${audio.currentTime.toFixed(2)} ${snapshot(audio)}`);
    audio.currentTime = target;
}

/** The review's position now (the element's; `timeupdate` reports only every ~250 ms). */
export function reviewPosition(): number {
    return owner === 'review' && audio ? audio.currentTime : get(musicReview).currentTime;
}

/** Change the level-match gain of the take playing (the person's volume stays on top). */
export function reviewSetGain(gain: number): void {
    reviewGain = clampGain(gain);
    musicReview.update(r => ({ ...r, gain: reviewGain }));
    if (owner === 'review' && audio) audio.volume = get(musicPlayback).volume * reviewGain;
}

/** End the review: the element goes back to the queue, whose track is loaded again,
 *  paused, where it was. Nothing happens when the queue already took it back. */
export async function endReview(): Promise<void> {
    if (owner !== 'review') {
        musicReview.set(EMPTY_REVIEW);
        return;
    }
    const held = queueHeld;
    loadToken++;
    if (audio) {
        audio.pause();
        audio.removeAttribute('src');
        audio.load();
    }
    leaveReview();
    if (audio) audio.volume = get(musicPlayback).volume;
    if (!held) return;
    const q = freshQueueAt(held.id);
    if (currentId(q) === held.id) await startAt(q, q.index, false, held.time);
}

/** Library entries the core hid (a discarded take): they leave the queue (the one
 *  playing plays on), and the library is read again so every list drops them. */
export async function noteTracksHidden(ids: readonly string[]): Promise<void> {
    if (!ids.length) return;
    const playing = currentId(get(musicPlayback).queue);
    const drop = ids.filter(id => id !== playing);
    if (drop.length) musicPlayback.update(s => ({ ...s, queue: queueDrop(s.queue, drop) }));
    await loadMusicLibrary(true);
}
