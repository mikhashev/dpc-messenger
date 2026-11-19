// The Music Player's pure logic (ADR-051 D1, D2; listener mode, phase 1).
//
// The window (components/music/MusicPlayer.svelte) and the playback service
// (services/music.ts) call these; nothing here touches the DOM, the audio element,
// Tauri or the WebSocket, so every rule is tested in musicPlayer.test.ts.
//
// The queue rules follow the agreed prototype (ai-studio experiments/music-player/
// prototype/index.html: stepTrack, onFinish, setShuffle, cycleRepeat, playList):
// - Prev restarts the track when it has played more than PREV_RESTART_S;
// - shuffle reorders only what comes after the current track, and turning it off
//   goes back to the list's order when the queue came from a list;
// - repeat off / all / one: "all" wraps the queue, "one" loops the current track on
//   its end, and Next / Prev still move;
// - a track that cannot play (missing, outside the DPC folder) is skipped.

import type { WindowSpec } from './windowGeometry';

// -- records from the core music service (dpc_client_core/music/schemas.py) ------

/** `mp.library_entry/1`, the keys the window reads. */
export interface LibraryEntry {
    schema?: string;
    id: string;
    title: string;
    source: { kind: 'local' | 'web' | 'generated' | string; name: string; uri: string; size_bytes?: number; mime?: string | null };
    duration_s: number | null;
    origin?: 'folder' | 'import' | 'web' | 'generated' | 'recording' | string;
    path?: string | null;
    folder_id?: string | null;
    missing?: boolean;
    hash_state?: 'pending' | 'done' | 'error' | string;
    artist?: string | null;
    album?: string | null;
}

/** `mp.music_folder/1`. */
export interface MusicFolder {
    schema?: string;
    id: string;
    path: string;
    added_at: string;
    last_scan_at: string | null;
    watch: boolean;
    file_count: number;
    hashed_count?: number;
    watch_state?: 'watching' | 'polling' | 'off' | 'unavailable' | string;
    state_reason?: string | null;
}

/** `mp.playlist/1`: a smart list (All tracks, Generated) is computed by the core. */
export interface Playlist {
    schema?: string;
    id: string;
    name: string;
    track_ids: string[];
    created_at: string;
    updated_at: string;
    kind: 'user' | 'smart';
    rule?: 'all' | 'generated';
}

/** The core's id of the smart list of every track (`library.py` SMART_ALL_ID). */
export const SMART_ALL_ID = 'pl_smart_all';

/** The open, expanded window re-reads the library this often, so a watched folder's
 *  changes show without a button; a closed or minimised window reads nothing. */
export const LIBRARY_POLL_MS = 10_000;

/** Rows a long list shows before "Show more". */
export const TRACK_ROWS_STEP = 300;

// -- answers of the music commands -----------------------------------------------

/** Shown while the backend does not know the music commands (not wired into local_api). */
export const MUSIC_UNAVAILABLE_TEXT = 'Music library not available on this backend yet';
export const NOT_CONNECTED_TEXT = 'Not connected to the backend.';

export type MusicAnswer<T = Record<string, any>> =
    | { ok: true; data: T }
    | { ok: false; unavailable: boolean; code: string; message: string };

/** What `sendCommand` is: a promise of the payload, or false when not connected. */
export type MusicSender = (command: string, payload?: any) => Promise<any> | boolean;

/** True when the backend refused the command because it does not have it
 *  (`local_api` raises "Unknown or non-async command: <name>"). */
export function isUnknownCommandError(error: unknown): boolean {
    const text = error instanceof Error ? error.message : String(error ?? '');
    return /unknown or non-async command|unknown command/i.test(text);
}

/** One music command, its answer made one shape: the core's `{status: "ok", ...}`
 *  becomes `{ok: true, data}`, its `{status: "error", code, message}` an error, an
 *  unknown command `unavailable`, and a lost socket `not_connected`. Never throws. */
export async function callMusic<T = Record<string, any>>(
    send: MusicSender, command: string, payload: Record<string, any> = {},
): Promise<MusicAnswer<T>> {
    let pending: Promise<any> | boolean;
    try {
        pending = send(command, payload);
    } catch (e) {
        return { ok: false, unavailable: false, code: 'send_failed', message: e instanceof Error ? e.message : String(e) };
    }
    if (pending === false || pending === true) {
        return { ok: false, unavailable: false, code: 'not_connected', message: NOT_CONNECTED_TEXT };
    }
    let answer: any;
    try {
        answer = await pending;
    } catch (e) {
        if (isUnknownCommandError(e)) {
            return { ok: false, unavailable: true, code: 'unknown_command', message: MUSIC_UNAVAILABLE_TEXT };
        }
        return { ok: false, unavailable: false, code: 'failed', message: e instanceof Error ? e.message : String(e) };
    }
    if (answer && typeof answer === 'object' && answer.status === 'ok') {
        const { status: _status, ...data } = answer;
        return { ok: true, data: data as T };
    }
    if (answer && typeof answer === 'object' && answer.status === 'error') {
        return {
            ok: false, unavailable: false,
            code: typeof answer.code === 'string' ? answer.code : 'error',
            message: typeof answer.message === 'string' && answer.message ? answer.message : `${command} failed`,
        };
    }
    return { ok: false, unavailable: false, code: 'bad_answer', message: `${command} gave an answer the player does not understand` };
}

// -- the queue -------------------------------------------------------------------

export type RepeatMode = 'off' | 'all' | 'one';

/** Up next: the ordered track ids, the current one, and the list it was started
 *  from (so shuffle off can go back to that list's order). */
export interface QueueState {
    ids: string[];
    index: number;
    context: string | null;
}

export const EMPTY_QUEUE: QueueState = { ids: [], index: -1, context: null };

/** Prev within this many seconds of the start goes to the previous track; later, it restarts. */
export const PREV_RESTART_S = 3;

export function currentId(q: QueueState): string | null {
    return q.index >= 0 && q.index < q.ids.length ? q.ids[q.index] : null;
}

/** "k / n" for the mini-player, or '' with an empty queue. */
export function positionLabel(q: QueueState): string {
    if (!q.ids.length) return '';
    const k = q.index >= 0 && q.index < q.ids.length ? q.index + 1 : 0;
    return `${k} / ${q.ids.length}`;
}

export function cycleRepeat(mode: RepeatMode): RepeatMode {
    return mode === 'off' ? 'all' : mode === 'all' ? 'one' : 'off';
}

export function repeatLabel(mode: RepeatMode): string {
    return `Repeat: ${mode}`;
}

/** What a Next, a Prev or the end of a track asks the player to do. */
export type StepResult =
    | { kind: 'play'; index: number; skipped: string[] }
    | { kind: 'restart' }
    | { kind: 'end'; message: string }
    | { kind: 'empty'; message: string }
    | { kind: 'nothing-playable'; message: string };

export const QUEUE_EMPTY_TEXT = 'The queue is empty. Play a list, or press Queue on a track.';
export const NOTHING_PLAYABLE_TEXT = 'No playable track in the queue.';

export interface StepOptions {
    repeat: RepeatMode;
    /** True when the track ended by itself (auto-advance), false for a press. */
    auto?: boolean;
    /** Seconds played of the current track; Prev past PREV_RESTART_S restarts it. */
    currentTime?: number;
    /** Whether a track id can play now. */
    playable: (id: string) => boolean;
}

/** Next (dir 1) or Prev (dir -1) from the current track, skipping what cannot play. */
export function stepQueue(q: QueueState, dir: 1 | -1, opts: StepOptions): StepResult {
    const n = q.ids.length;
    if (!n) return { kind: 'empty', message: QUEUE_EMPTY_TEXT };
    if (dir < 0 && !opts.auto && (opts.currentTime ?? 0) > PREV_RESTART_S) return { kind: 'restart' };
    let i = q.index;
    const skipped: string[] = [];
    for (let s = 0; s < n; s++) {
        i += dir;
        if (i >= n || i < 0) {
            if (opts.repeat !== 'all') {
                if (dir < 0) return { kind: 'restart' };
                return { kind: 'end', message: opts.auto ? 'End of the queue (repeat is off).' : 'No next track (repeat is off).' };
            }
            i = (i + n) % n;
        }
        const id = q.ids[i];
        if (opts.playable(id)) return { kind: 'play', index: i, skipped };
        skipped.push(id);
    }
    return { kind: 'nothing-playable', message: NOTHING_PLAYABLE_TEXT };
}

/** The end of the current track: repeat one plays it again, otherwise auto-advance. */
export function onTrackEnded(q: QueueState, repeat: RepeatMode, playable: (id: string) => boolean): StepResult {
    if (repeat === 'one' && currentId(q) !== null) return { kind: 'restart' };
    return stepQueue(q, 1, { repeat, auto: true, playable });
}

export type Random = () => number;

/** Fisher-Yates on a copy. */
export function shuffled<T>(items: readonly T[], random: Random = Math.random): T[] {
    const a = [...items];
    for (let i = a.length - 1; i > 0; i--) {
        const j = Math.floor(random() * (i + 1));
        [a[i], a[j]] = [a[j], a[i]];
    }
    return a;
}

/** Shuffle on: what comes after the current track is shuffled; what was played stays. */
export function shuffleOn(q: QueueState, random: Random = Math.random): QueueState {
    const head = q.ids.slice(0, q.index + 1);
    return { ...q, ids: [...head, ...shuffled(q.ids.slice(q.index + 1), random)] };
}

/** Shuffle off: back to the list's order when the queue came from that list and the
 *  current track is in it; otherwise the queue stays as it is. */
export function shuffleOff(q: QueueState, listIds: readonly string[] | null): { queue: QueueState; restored: boolean } {
    const cur = currentId(q);
    if (listIds && cur && listIds.includes(cur)) {
        const ids = [...listIds];
        return { queue: { ...q, ids, index: ids.indexOf(cur) }, restored: true };
    }
    return { queue: q, restored: false };
}

/** A queue made from a list: from `startId`, or the first playable track (a random
 *  one with shuffle on); with shuffle the rest follows in a random order. Null when
 *  nothing in the list can play. */
export function queueFromList(
    listId: string, trackIds: readonly string[], opts: { startId?: string | null; shuffle: boolean; playable: (id: string) => boolean; random?: Random },
): QueueState | null {
    const random = opts.random ?? Math.random;
    const playable = trackIds.filter(opts.playable);
    if (!playable.length) return null;
    const start = opts.startId && trackIds.includes(opts.startId)
        ? opts.startId
        : opts.shuffle ? playable[Math.floor(random() * playable.length)] : playable[0];
    let ids = [...trackIds];
    if (opts.shuffle) {
        const rest = [...ids];
        rest.splice(rest.indexOf(start), 1);
        ids = [start, ...shuffled(rest, random)];
    }
    return { ids, index: ids.indexOf(start), context: listId };
}

/** Add to Up next: at the end, or (`next`) right after the current track. A track
 *  already waiting moves rather than repeats; the current track is never added again. */
export function enqueue(q: QueueState, ids: readonly string[], next = false): QueueState {
    const cur = currentId(q);
    const add = ids.filter((id, i) => id !== cur && ids.indexOf(id) === i);
    const before = q.ids.slice(0, q.index + 1);
    const after = q.ids.slice(q.index + 1).filter(id => !add.includes(id));
    if (q.index < 0) {
        // Nothing is current: the first added track becomes current.
        const ids2 = next ? [...add, ...after] : [...after, ...add];
        return { ...q, ids: ids2, index: ids2.length ? 0 : -1 };
    }
    return { ...q, ids: next ? [...before, ...add, ...after] : [...before, ...after, ...add] };
}

/** Remove one row of Up next; the current track cannot be removed. */
export function queueRemoveAt(q: QueueState, i: number): QueueState {
    if (i === q.index || i < 0 || i >= q.ids.length) return q;
    const ids = [...q.ids];
    ids.splice(i, 1);
    return { ...q, ids, index: i < q.index ? q.index - 1 : q.index };
}

/** Clear Up next: only the current track stays. */
export function queueClear(q: QueueState): QueueState {
    const cur = currentId(q);
    return cur ? { ids: [cur], index: 0, context: q.context } : EMPTY_QUEUE;
}

/** Point the queue at `id`: the next occurrence after the current one, else any, else
 *  inserted right after the current track. */
export function alignQueue(q: QueueState, id: string): QueueState {
    if (currentId(q) === id) return q;
    let i = q.ids.indexOf(id, q.index + 1);
    if (i < 0) i = q.ids.indexOf(id);
    if (i >= 0) return { ...q, index: i };
    const ids = [...q.ids];
    ids.splice(q.index + 1, 0, id);
    return { ...q, ids, index: q.index + 1 };
}

// -- playlist order --------------------------------------------------------------

/** `items` with the one at `from` moved to `to` (clamped); the same array when nothing moves. */
export function moveItem<T>(items: readonly T[], from: number, to: number): T[] {
    if (from < 0 || from >= items.length) return [...items];
    const target = Math.max(0, Math.min(items.length - 1, to));
    if (target === from) return [...items];
    const out = [...items];
    const [x] = out.splice(from, 1);
    out.splice(target, 0, x);
    return out;
}

/** Where a dragged row lands: the index among the rows whose vertical midpoints are
 *  `midpoints` (top to bottom), for a pointer at `y`, with the dragged row at `from`. */
export function dropIndex(midpoints: readonly number[], y: number, from: number): number {
    let slot = 0;
    while (slot < midpoints.length && y > midpoints[slot]) slot++;
    // `slot` is the gap before row `slot`; removing the dragged row shifts later gaps.
    const to = slot > from ? slot - 1 : slot;
    return Math.max(0, Math.min(midpoints.length - 1, to));
}

/** A name for a new playlist that is not taken: "Playlist N". */
export function nextPlaylistName(playlists: readonly Playlist[]): string {
    const users = playlists.filter(p => p.kind === 'user');
    let n = users.length + 1;
    while (playlists.some(p => p.name === `Playlist ${n}`)) n++;
    return `Playlist ${n}`;
}

// -- the playback source (ADR-051 N8, open) --------------------------------------

/**
 * Where the webview may read a track's bytes from today.
 *
 * ADR-051 N8 (how core serves bytes to the webview) is Mike's open decision:
 * - the Tauri asset scope is `$HOME/.dpc/**` only (`tauri.conf.json`), so a file in
 *   `~/Music` cannot go through `convertFileSrc`;
 * - the 9998 file server serves only `~/.dpc/conversations/<peer>/files/`;
 * - `media-src` allows `blob:` and `asset:` but not `http://localhost:*` nor web `https:`.
 *
 * So, without widening the scope or the CSP:
 * - a file under `~/.dpc` plays through `convertFileSrc` (an `asset:` URL);
 * - on Linux the `asset:` protocol does not play audio in WebKitGTK (the note in
 *   VoicePlayer.svelte), so the file is read through the fs plugin, whose scope is the
 *   same `$HOME/.dpc/**`, into a `blob:` URL (the whole file in webview memory);
 * - any other path is "not playable yet (outside the DPC folder)", and a web track is
 *   "not playable yet" until N9 is decided.
 * Changing this is one place: `trackPlaybackUrl`.
 */
export interface PlaybackEnv {
    /** Running in the Tauri desktop app. */
    tauri: boolean;
    /** The home folder Tauri resolves `$HOME` to, or null when unknown. */
    home: string | null;
    isWindows: boolean;
    isLinux: boolean;
    /** `@tauri-apps/api/core` convertFileSrc. */
    convertFileSrc?: (path: string) => string;
    /** Read a file by its path relative to the home folder (fs plugin, BaseDirectory.Home). */
    readHomeFile?: (relativePath: string) => Promise<Uint8Array>;
    /** `URL.createObjectURL`. */
    createObjectURL?: (blob: Blob) => string;
    revokeObjectURL?: (url: string) => void;
}

export type PlaybackProblem = 'missing' | 'no-file' | 'outside-dpc' | 'web' | 'no-desktop' | 'read-failed';

export type PlaybackSource =
    | { ok: true; url: string; via: 'asset' | 'blob'; release: () => void }
    | { ok: false; problem: PlaybackProblem; reason: string };

export const OUTSIDE_DPC_TEXT = 'Not playable yet (outside the DPC folder)';
export const MISSING_TEXT = 'Missing: the file is no longer where the library saw it';
export const WEB_TEXT = 'Not playable yet (web tracks wait on a decision)';
export const NO_DESKTOP_TEXT = 'Not playable here: playback needs the desktop app';

const DPC_DIR = '.dpc';

/** A path as forward-slash segments: no `\\?\` prefix, no empty or `.` segments.
 *  Null when it climbs (`..`), since the scope check would not see where it lands. */
function segments(path: string): string[] | null {
    const p = path.replace(/^\\\\\?\\/, '').replace(/\\/g, '/');
    const out: string[] = [];
    for (const s of p.split('/')) {
        if (s === '' || s === '.') continue;
        if (s === '..') return null;
        out.push(s);
    }
    return out;
}

/** The path relative to `home` when it lies inside `home/.dpc` (the asset and fs
 *  scope), joined with '/', else null. Windows compares without case. */
export function dpcRelativePath(path: string | null | undefined, home: string | null, isWindows: boolean): string | null {
    if (!path || !home) return null;
    const p = segments(path);
    const h = segments(home);
    if (!p || !h || !h.length) return null;
    const norm = (s: string) => (isWindows ? s.toLowerCase() : s);
    const base = [...h, DPC_DIR];
    if (p.length <= base.length) return null;
    for (let i = 0; i < base.length; i++) if (norm(p[i]) !== norm(base[i])) return null;
    // An absolute POSIX path must also start at the root the home does.
    if (!isWindows && path.startsWith('/') !== home.startsWith('/')) return null;
    return p.slice(h.length).join('/');
}

/** Whether the track can be asked to play at all, without reading anything. */
export function playbackProblem(entry: LibraryEntry | null | undefined, env: Pick<PlaybackEnv, 'tauri' | 'home' | 'isWindows'>):
    { problem: PlaybackProblem; reason: string } | null {
    if (!entry) return { problem: 'missing', reason: 'Not in the library any more' };
    if (entry.missing) return { problem: 'missing', reason: MISSING_TEXT };
    if (entry.source?.kind === 'web' || entry.origin === 'web') return { problem: 'web', reason: WEB_TEXT };
    if (!entry.path) return { problem: 'no-file', reason: 'This entry has no file' };
    if (!env.tauri) return { problem: 'no-desktop', reason: NO_DESKTOP_TEXT };
    // An unknown home is treated as outside: nothing proves the path is in the scope.
    if (dpcRelativePath(entry.path, env.home, env.isWindows) === null) {
        return { problem: 'outside-dpc', reason: OUTSIDE_DPC_TEXT };
    }
    return null;
}

/** The URL an `<audio>` gets for a track, or why it cannot get one. The single place
 *  the playback source is decided (see PlaybackEnv). `release` frees a `blob:` URL. */
export async function trackPlaybackUrl(entry: LibraryEntry | null | undefined, env: PlaybackEnv): Promise<PlaybackSource> {
    const blocked = playbackProblem(entry, env);
    if (blocked) return { ok: false, ...blocked };
    const path = entry!.path!;
    const relative = dpcRelativePath(path, env.home, env.isWindows)!;
    if (env.isLinux) {
        if (!env.readHomeFile || !env.createObjectURL) {
            return { ok: false, problem: 'read-failed', reason: 'Cannot read the file in this webview' };
        }
        try {
            const bytes = await env.readHomeFile(relative);
            const blob = new Blob([bytes as BlobPart], { type: entry!.source?.mime || 'audio/*' });
            const url = env.createObjectURL(blob);
            return { ok: true, url, via: 'blob', release: () => env.revokeObjectURL?.(url) };
        } catch (e) {
            return { ok: false, problem: 'read-failed', reason: `Could not read the file: ${e instanceof Error ? e.message : String(e)}` };
        }
    }
    if (!env.convertFileSrc) return { ok: false, problem: 'no-desktop', reason: NO_DESKTOP_TEXT };
    return { ok: true, url: env.convertFileSrc(path), via: 'asset', release: () => {} };
}

// -- display ---------------------------------------------------------------------

/** m:ss, or h:mm:ss past an hour; '–:––' when unknown. */
export function formatTime(seconds: number | null | undefined): string {
    if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return '–:––';
    const s = Math.floor(seconds);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const ss = String(s % 60).padStart(2, '0');
    return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`;
}

/** The short tag of a track's origin: folder, gen, rec, web, import. */
export function originTag(entry: LibraryEntry): string {
    switch (entry.origin) {
        case 'generated': return 'gen';
        case 'recording': return 'rec';
        case 'web': return 'web';
        case 'import': return 'import';
        default: return 'folder';
    }
}

/** Whether a track matches the library filter (title, artist, album, file name), without case. */
export function matchesFilter(entry: LibraryEntry | null | undefined, text: string): boolean {
    const t = text.trim().toLowerCase();
    if (!t) return true;
    if (!entry) return false;
    return [entry.title, entry.artist, entry.album, entry.source?.name]
        .some(v => typeof v === 'string' && v.toLowerCase().includes(t));
}

/** "Artist · Album" when known. */
export function trackSubtitle(entry: LibraryEntry): string {
    return [entry.artist, entry.album].filter(Boolean).join(' · ');
}

// -- the window and the remembered preferences -----------------------------------

/** The Music Player window: remembered under its own key, through windowGeometry. */
export const MUSIC_PLAYER_WINDOW: WindowSpec = {
    key: 'dpc.musicPlayer.window',
    limits: { minWidth: 520, minHeight: 360, defaultWidth: 1040, defaultHeight: 720 },
};

/** What the player remembers between sessions: preferences, never the library. */
export interface MusicPreferences {
    shuffle: boolean;
    repeat: RepeatMode;
    volume: number;
}

export const DEFAULT_MUSIC_PREFERENCES: MusicPreferences = { shuffle: false, repeat: 'off', volume: 1 };
export const MUSIC_PREFERENCES_KEY = 'dpc.musicPlayer.preferences';

type StorageLike = Pick<Storage, 'getItem' | 'setItem'>;

export function parseMusicPreferences(raw: string | null | undefined): MusicPreferences {
    if (!raw) return { ...DEFAULT_MUSIC_PREFERENCES };
    try {
        const v = JSON.parse(raw);
        return {
            shuffle: v?.shuffle === true,
            repeat: v?.repeat === 'all' || v?.repeat === 'one' ? v.repeat : 'off',
            volume: typeof v?.volume === 'number' && Number.isFinite(v.volume) ? Math.min(1, Math.max(0, v.volume)) : 1,
        };
    } catch {
        return { ...DEFAULT_MUSIC_PREFERENCES };
    }
}

export function loadMusicPreferences(storage: StorageLike | null | undefined): MusicPreferences {
    try {
        return parseMusicPreferences(storage?.getItem(MUSIC_PREFERENCES_KEY));
    } catch {
        return { ...DEFAULT_MUSIC_PREFERENCES };
    }
}

export function saveMusicPreferences(storage: StorageLike | null | undefined, prefs: MusicPreferences): void {
    try {
        storage?.setItem(MUSIC_PREFERENCES_KEY, JSON.stringify(prefs));
    } catch {
        /* the player works without it */
    }
}

/** How often a volume drag may write the preferences: the slider fires an input
 *  event per step, and each write is a synchronous localStorage call. */
export const VOLUME_SAVE_MS = 250;

export interface Throttled {
    /** Run now if the last run is `ms` old, else once when it is. */
    call(): void;
    /** Run now if a run is waiting (the end of a drag); nothing otherwise. */
    flush(): void;
    /** Drop a waiting run. */
    cancel(): void;
}

type Timers = {
    now: () => number;
    setTimeout: (fn: () => void, ms: number) => unknown;
    clearTimeout: (handle: any) => void;
};

const REAL_TIMERS: Timers = {
    now: () => Date.now(),
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (h) => clearTimeout(h),
};

/** `fn` at most once per `ms`: the first call runs at once, the calls inside the
 *  window fold into one trailing run, so the last value is always written. */
export function throttle(fn: () => void, ms: number, timers: Timers = REAL_TIMERS): Throttled {
    let last = -Infinity;
    let pending: unknown = null;
    const run = () => {
        pending = null;
        last = timers.now();
        fn();
    };
    return {
        call() {
            if (pending !== null) return;
            const wait = last + ms - timers.now();
            if (wait <= 0) run();
            else pending = timers.setTimeout(run, wait);
        },
        flush() {
            if (pending === null) return;
            timers.clearTimeout(pending);
            run();
        },
        cancel() {
            if (pending !== null) timers.clearTimeout(pending);
            pending = null;
        },
    };
}
