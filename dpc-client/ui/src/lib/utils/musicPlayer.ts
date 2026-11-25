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

import { HEADER_HEIGHT, clampRect, type Viewport, type WindowRect, type WindowSpec, type WindowState } from './windowGeometry';

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
    hidden?: boolean;
    /** Computed by the core on every read: present, inside DPC's own generated/ folder, file there. */
    deletable?: boolean;
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

/** Rows a long list shows before "Show more". */
export const TRACK_ROWS_STEP = 300;

// -- answers of the music commands -----------------------------------------------

/** Shown while the backend does not know the music commands (not wired into local_api). */
export const MUSIC_UNAVAILABLE_TEXT = 'Music library not available on this backend yet';
export const NOT_CONNECTED_TEXT = 'Not connected to the backend.';

export type MusicAnswer<T = Record<string, any>> =
    | { ok: true; data: T }
    | { ok: false; unavailable: boolean; code: string; message: string;
        /** The param at fault, when the core names one (`MusicJobError.field`). */
        field?: string };

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
        const out: Extract<MusicAnswer<T>, { ok: false }> = {
            ok: false, unavailable: false,
            code: typeof answer.code === 'string' ? answer.code : 'error',
            message: typeof answer.message === 'string' && answer.message ? answer.message : `${command} failed`,
        };
        if (typeof answer.field === 'string' && answer.field) out.field = answer.field;
        return out;
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

/** Why the player moved (or tried to), for the debug log. */
export type StepReason = 'ended' | 'ended+repeat-one' | 'next' | 'prev' | 'play' | 'row' | 'list';

/** The state a debug line carries: repeat, shuffle, the queue's length and position, and whether
 *  the audio element loops by itself (repeat one: it then never fires `ended`). */
export function playerStateLine(s: { repeat: RepeatMode; shuffle: boolean; queue: QueueState; loop?: boolean | null }): string {
    const n = s.queue.ids.length;
    const pos = s.queue.index >= 0 && s.queue.index < n ? `${s.queue.index + 1}/${n}` : `-/${n}`;
    return `repeat=${s.repeat} shuffle=${s.shuffle ? 'on' : 'off'} queue=${n} pos=${pos} context=${s.queue.context ?? '-'}` +
        (s.loop == null ? '' : ` loop=${s.loop ? 'on' : 'off'}`);
}

/** The reason a track ended by itself moved: repeat one plays it again, anything else advances. */
export function endedReason(repeat: RepeatMode): StepReason {
    return repeat === 'one' ? 'ended+repeat-one' : 'ended';
}

/** One debug line for a step: its reason, what it did and the player's state before it. */
export function stepLogLine(
    reason: StepReason, r: StepResult, s: { repeat: RepeatMode; shuffle: boolean; queue: QueueState; loop?: boolean | null },
): string {
    const from = currentId(s.queue) ?? '-';
    const to = r.kind === 'play' ? `play ${s.queue.ids[r.index] ?? '-'} (index ${r.index + 1})` +
        (r.skipped.length ? ` skipped=${r.skipped.length}` : '') : r.kind === 'restart' ? `restart ${from}` : `${r.kind}: ${r.message}`;
    return `track-change reason=${reason} from=${from} -> ${to} ${playerStateLine(s)}`;
}

/** Whether the element's time jumped back far enough between two `timeupdate`s to be a loop (the
 *  element restarting the same track by itself, with no `ended`). */
export function loopWrapped(previous: number, now: number): boolean {
    return Number.isFinite(previous) && Number.isFinite(now) && previous - now > 1 && now < 1;
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

// -- the playback source (ADR-051 N8, option (i), D28) -----------------------------

/**
 * Where the webview reads a track's bytes from.
 *
 * - The core's track stream (`dpc_client_core/music/stream.py`): loopback HTTP with
 *   `Range` and a token, keyed by track id; `media-src` names `http://127.0.0.1:9996`.
 *   **Linux** always uses it, with `&ranges=0` (a plain whole-file 200, no Accept-Ranges;
 *   `[music] stream_ranges_linux` turns Range back on): WebKitGTK plays no audio from
 *   `asset:`, treats a `blob:` as a live stream (duration 0, no seeking forward), and on a
 *   Range source reports duration 0 and refuses forward seeks it has not buffered.
 *   Whether 36a4777a (wait for a duration before play) made the no-Range mode redundant is not
 *   proven; it stays the default until a Linux run with `stream_ranges_linux = true` says so.
 * - **Windows and macOS** keep `convertFileSrc` (`asset:`) for a file under `~/.dpc`, the
 *   asset scope; a file elsewhere uses the stream.
 * - Fallback when the stream info cannot be had: Linux reads a file under `~/.dpc` into
 *   a `blob:` URL; a file outside `~/.dpc` is "not playable yet (outside the DPC folder)".
 *   The source and any fallback reason are in the `[music] source ... via=` log line.
 * - A web track is "not playable yet" until N9 is decided.
 * Changing this is one place: `trackPlaybackUrl`.
 */
export interface StreamInfo {
    /** `http://127.0.0.1:<port>`, from the core. */
    base_url: string;
    /** The stream's per-process token. Never logged. */
    token: string;
    /** `[music] stream_ranges_linux`: true makes Linux ask for HTTP Range; absent or false, no Range. */
    ranges_linux?: boolean;
}

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
    /** The core's track stream (`get_music_stream_info`); rejects when it is unavailable. */
    streamInfo?: () => Promise<StreamInfo>;
}

export type PlaybackProblem = 'missing' | 'no-file' | 'outside-dpc' | 'web' | 'no-desktop' | 'read-failed';

export type PlaybackSource =
    | { ok: true; url: string; via: 'asset' | 'blob' | 'http'; release: () => void; mime?: string; bytes?: number;
        /** For `via: 'http'`: whether the stream URL allows HTTP Range (false: `ranges=0`, a plain whole-file 200). */
        ranges?: boolean;
        /** Why the stream was not used when it would have been (Linux fell back to `blob:`). */
        fallback?: string }
    | { ok: false; problem: PlaybackProblem; reason: string };

export const OUTSIDE_DPC_TEXT = 'Not playable yet (outside the DPC folder)';
export const MISSING_TEXT = 'Missing: the file is no longer where the library saw it';
export const WEB_TEXT = 'Not playable yet (web tracks wait on a decision)';
export const NO_DESKTOP_TEXT = 'Not playable here: playback needs the desktop app';

const DPC_DIR = '.dpc';

/** The MIME type of an audio file by its extension (what the core's library also
 *  records for a folder track). Never a wildcard. */
const AUDIO_MIME_BY_EXTENSION: Record<string, string> = {
    mp3: 'audio/mpeg',
    flac: 'audio/flac',
    wav: 'audio/wav',
    ogg: 'audio/ogg',
    oga: 'audio/ogg',
    opus: 'audio/ogg',
    m4a: 'audio/mp4',
    mp4: 'audio/mp4',
    aac: 'audio/mp4',
    webm: 'audio/webm',
};

/** A concrete `audio/<subtype>` (no `*`; parameters after `;` are allowed). */
export function isConcreteAudioMime(mime: unknown): mime is string {
    return typeof mime === 'string' && /^audio\/[a-z0-9][a-z0-9.+-]*(\s*;.*)?$/i.test(mime.trim());
}

/** The MIME type to give a `Blob` of this track: the one the library recorded when it is a
 *  real audio type, else one derived from the file extension, else '' (no type). A wildcard
 *  such as `audio/*` is not a type, and WebKitGTK's GStreamer pipeline types the `blob:` by it. */
export function trackMime(entry: LibraryEntry | null | undefined): string {
    const recorded = entry?.source?.mime;
    if (isConcreteAudioMime(recorded)) return recorded.trim();
    const name = entry?.path || entry?.source?.name || '';
    const base = name.split(/[\\/]/).pop() ?? '';
    const dot = base.lastIndexOf('.');
    const ext = dot >= 0 ? base.slice(dot + 1).toLowerCase() : '';
    return AUDIO_MIME_BY_EXTENSION[ext] ?? '';
}

/** `TimeRanges` as "0.0-12.3,40.0-60.1" for a log line; "none" when empty, "unreadable" on a throw. */
export function formatRanges(r: { length: number; start(i: number): number; end(i: number): number } | null | undefined): string {
    try {
        if (!r || !r.length) return 'none';
        const parts: string[] = [];
        for (let i = 0; i < Math.min(r.length, 8); i++) parts.push(`${r.start(i).toFixed(1)}-${r.end(i).toFixed(1)}`);
        if (r.length > 8) parts.push(`+${r.length - 8} more`);
        return parts.join(',');
    } catch {
        return 'unreadable';
    }
}

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

/** Only a loopback `http://127.0.0.1:<port>` (or `[::1]`) base is used as a stream. */
export function isLoopbackStreamBase(base: unknown): base is string {
    return typeof base === 'string' && /^http:\/\/(127\.0\.0\.1|\[::1\]):\d{1,5}$/.test(base);
}

/** The `<audio>` URL of a track on the core's stream: keyed by its library id, never a path. */
export function streamTrackUrl(info: StreamInfo, trackId: string, ranges = true): string {
    const url = `${info.base_url}/music/track/${encodeURIComponent(trackId)}?t=${encodeURIComponent(info.token)}`;
    return ranges ? url : `${url}&ranges=0`;
}

/** Whether the track can be asked to play at all, without reading anything. With the
 *  core's stream on offer (`env.streamInfo`), a file outside `~/.dpc` is in reach too. */
export function playbackProblem(entry: LibraryEntry | null | undefined, env: Pick<PlaybackEnv, 'tauri' | 'home' | 'isWindows'> & Partial<Pick<PlaybackEnv, 'streamInfo'>>):
    { problem: PlaybackProblem; reason: string } | null {
    if (!entry) return { problem: 'missing', reason: 'Not in the library any more' };
    if (entry.missing) return { problem: 'missing', reason: MISSING_TEXT };
    if (entry.source?.kind === 'web' || entry.origin === 'web') return { problem: 'web', reason: WEB_TEXT };
    if (!entry.path) return { problem: 'no-file', reason: 'This entry has no file' };
    if (!env.tauri) return { problem: 'no-desktop', reason: NO_DESKTOP_TEXT };
    // An unknown home is treated as outside: nothing proves the path is in the scope.
    if (!env.streamInfo && dpcRelativePath(entry.path, env.home, env.isWindows) === null) {
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
    const relative = dpcRelativePath(path, env.home, env.isWindows);
    let fallback: string | undefined;
    // Linux always, elsewhere only what the asset scope cannot reach (see PlaybackEnv).
    if (env.isLinux || relative === null) {
        if (env.streamInfo) {
            try {
                const info = await env.streamInfo();
                if (!isLoopbackStreamBase(info?.base_url) || typeof info?.token !== 'string' || !info.token) {
                    throw new Error('the core gave no loopback stream address');
                }
                // Linux without Range (unless `[music] stream_ranges_linux`): WebKitGTK reports
                // duration 0 and refuses forward seeks on a Range source, and handles a plain 200.
                const ranges = !env.isLinux || info.ranges_linux === true;
                return { ok: true, url: streamTrackUrl(info, entry!.id, ranges), via: 'http', release: () => {}, mime: trackMime(entry), ranges };
            } catch (e) {
                fallback = `stream unavailable: ${e instanceof Error ? e.message : String(e)}`;
            }
        }
        if (relative === null) {
            return { ok: false, problem: 'outside-dpc', reason: fallback ? `${OUTSIDE_DPC_TEXT}: the track ${fallback}` : OUTSIDE_DPC_TEXT };
        }
    }
    if (env.isLinux) {
        if (!env.readHomeFile || !env.createObjectURL) {
            return { ok: false, problem: 'read-failed', reason: 'Cannot read the file in this webview' };
        }
        try {
            const bytes = await env.readHomeFile(relative!); // null returned above
            const mime = trackMime(entry);
            const blob = new Blob([bytes as BlobPart], mime ? { type: mime } : {});
            const url = env.createObjectURL(blob);
            return { ok: true, url, via: 'blob', release: () => env.revokeObjectURL?.(url), mime, bytes: bytes.byteLength, fallback };
        } catch (e) {
            return { ok: false, problem: 'read-failed', reason: `Could not read the file: ${e instanceof Error ? e.message : String(e)}` };
        }
    }
    if (!env.convertFileSrc) return { ok: false, problem: 'no-desktop', reason: NO_DESKTOP_TEXT };
    return { ok: true, url: env.convertFileSrc(path), via: 'asset', release: () => {}, mime: trackMime(entry) };
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

/** The duration the player shows and seeks against. The media element's own duration
 *  wins when it is plausible; it is not when it is 0, NaN or Infinity (WebKitGTK reports
 *  0 for a stream it cannot size), smaller than the position already played, or under
 *  half the library's figure (a pipeline that reports only what it has read so far). Then
 *  the library's `duration_s` (ffprobe) stands. The result is never below `currentTime`
 *  and is 0 only when nothing is known. */
export function resolveDuration(
    elementDuration: number | null | undefined,
    libraryDuration: number | null | undefined,
    currentTime = 0,
): number {
    const valid = (n: number | null | undefined): n is number => typeof n === 'number' && Number.isFinite(n) && n > 0;
    const lib = valid(libraryDuration) ? libraryDuration : 0;
    const pos = valid(currentTime) ? currentTime : 0;
    let d = 0;
    if (valid(elementDuration) && elementDuration >= pos && !(lib > 0 && elementDuration < lib / 2)) d = elementDuration;
    else if (lib > 0) d = lib;
    else if (valid(elementDuration)) d = elementDuration;
    return Math.max(d, pos);
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

/** Whether the origin badge can open the file's folder: the track is a file on this
 *  machine (not a web track), it is not missing, and the library knows its path. */
export function canRevealFile(entry: LibraryEntry | null | undefined): boolean {
    if (!entry || entry.missing) return false;
    if (entry.origin === 'web' || entry.source?.kind === 'web') return false;
    return typeof entry.path === 'string' && entry.path.length > 0;
}

/** The badge's hover text: "Show in folder" when it opens one, else just the origin. */
export function originBadgeTitle(entry: LibraryEntry): string {
    return canRevealFile(entry) ? 'Show in folder' : originTag(entry);
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

// -- the mini-player bar: its own width, apart from the window's -----------------
//
// Measured from the bar's CSS: the head pads 8 + 8 px and puts 12 px between the
// mini part and the restore / close pair (2 x 40 px + 2); the small transport is
// 32 + 36 + 32 px with 6 px gaps; the mini part's gaps are 8 px; the window's
// border is 1 + 1 px. That is 224 px before the title, so 300 px leaves the title
// about 70 px (a handful of characters at 14 px) with k / n, shuffle and repeat hidden.

/** The narrowest the mini-player bar may be dragged (a smaller viewport wins). */
export const MINI_PLAYER_MIN_WIDTH = 300;
/** At or above this width the bar shows "k / n" (about 50 px more, plus a gap). */
export const MINI_PLAYER_POSITION_WIDTH = 370;
/** At or above this width the bar shows shuffle and repeat too (2 x 30 px plus gaps). */
export const MINI_PLAYER_MODES_WIDTH = 440;

function within(value: number, low: number, high: number): number {
    return Math.min(Math.max(value, low), Math.max(low, high));
}

/** The bar's width: the remembered mini width, or the window's width when there is
 *  none, kept within [MINI_PLAYER_MIN_WIDTH, viewport]. */
export function miniPlayerWidth(miniWidth: number | null | undefined, fullWidth: number, vp: Viewport): number {
    const w = typeof miniWidth === 'number' && Number.isFinite(miniWidth) && miniWidth > 0 ? miniWidth : fullWidth;
    return within(w, Math.min(MINI_PLAYER_MIN_WIDTH, vp.width), vp.width);
}

/** The bar's own top-left corner, kept apart from the window's. */
export interface MiniPosition {
    x: number;
    y: number;
}

/** The bar's box: its own corner (`at`), or the window's top-left corner when it has
 *  none, at the bar's width and the header's height, pulled back so the whole bar
 *  stays in view. */
export function miniPlayerRect(
    rect: WindowRect, miniWidth: number | null | undefined, vp: Viewport, at?: MiniPosition | null,
): WindowRect {
    const width = miniPlayerWidth(miniWidth, rect.width, vp);
    const limits = { ...MUSIC_PLAYER_WINDOW.limits, minWidth: MINI_PLAYER_MIN_WIDTH, minHeight: 0 };
    const corner = at ?? rect;
    const bar = clampRect({ x: corner.x, y: corner.y, width, height: HEADER_HEIGHT }, vp, true, limits);
    return { x: bar.x, y: bar.y, width, height: HEADER_HEIGHT };
}

/** The bar's corner after a drag of (dx, dy) from `start` (the bar's box at the
 *  press): only the bar moves, and the whole bar, at its own width, stays in view,
 *  so a narrow bar reaches the right edge. */
export function moveMiniPlayer(start: WindowRect, dx: number, dy: number, vp: Viewport): MiniPosition {
    const bar = miniPlayerRect({ ...start, x: start.x + dx, y: start.y + dy }, start.width, vp);
    return { x: bar.x, y: bar.y };
}

/** The bar's remembered corner in a stored state, or null when it has none. */
export function miniPosition(state: WindowState): MiniPosition | null {
    return state.miniX !== undefined && state.miniY !== undefined ? { x: state.miniX, y: state.miniY } : null;
}

/** The state with the bar at `at` (null: no corner of its own). Only the fields
 *  that are set are written, so a state that never had them stores as before. */
function withMini(state: WindowState, miniWidth: number | undefined, at: MiniPosition | null): WindowState {
    const out: WindowState = { rect: state.rect, minimised: state.minimised };
    if (miniWidth !== undefined) out.miniWidth = miniWidth;
    if (at) {
        out.miniX = at.x;
        out.miniY = at.y;
    }
    return out;
}

/** What is drawn for a saved state in this viewport: the whole window and the whole bar at its own
 *  width and corner, each pulled back into view. A view only: the saved state is not changed and
 *  must stay as the person left it, so a temporary small window or screen never moves the
 *  remembered place or width for good (backlog THE-MINI-PLAYER-FIT-TO-A-SMALLER-SCREEN-...).
 *  A state without a bar corner of its own draws the bar at the window's corner. */
export function musicPlayerView(saved: WindowState, vp: Viewport): { window: WindowRect; bar: WindowRect } {
    return {
        window: clampRect(saved.rect, vp, false, MUSIC_PLAYER_WINDOW.limits),
        bar: miniPlayerRect(saved.rect, saved.miniWidth, vp, miniPosition(saved)),
    };
}

/** Both boxes fitted to the viewport: the whole window, and the whole bar at its own
 *  width. A minimised state without a corner of its own (stored before the bar had
 *  one) takes the window's corner as the bar's, where the bar was drawn then.
 *  A clamped copy for drawing or a pure check: the component does not save it
 *  (`musicPlayerView` is what it draws), because saving it would keep the clamp for good. */
export function fitMusicPlayer(state: WindowState, vp: Viewport): WindowState {
    const miniWidth = state.miniWidth !== undefined ? miniPlayerWidth(state.miniWidth, state.rect.width, vp) : undefined;
    let at = miniPosition(state);
    if (!at && state.minimised) at = { x: state.rect.x, y: state.rect.y };
    if (at) {
        const bar = miniPlayerRect(state.rect, miniWidth, vp, at);
        at = { x: bar.x, y: bar.y };
    }
    const rect = clampRect(state.rect, vp, false, MUSIC_PLAYER_WINDOW.limits);
    return withMini({ ...state, rect }, miniWidth, at);
}

/** Minimised: the bar goes to its own remembered corner; the first time, where the
 *  window's header is. The window's box is kept as it was, for the restore. */
export function minimiseMusicPlayer(state: WindowState, vp: Viewport): WindowState {
    return fitMusicPlayer({ ...state, minimised: true }, vp);
}

/** Restored: the window at its own corner and size; the bar keeps its corner for the
 *  next minimise. */
export function restoreMusicPlayer(state: WindowState, vp: Viewport): WindowState {
    return fitMusicPlayer({ ...state, minimised: false }, vp);
}

/** The bar's width after its right-edge handle moved by dx from `start` (the bar's
 *  box at the press): the left edge stays, the bar cannot pass the viewport's edge. */
export function resizeMiniWidth(start: WindowRect, dx: number, vp: Viewport): number {
    return within(start.width + dx, Math.min(MINI_PLAYER_MIN_WIDTH, vp.width), vp.width - start.x);
}

/** What a bar this wide has room for besides prev / play / next, the title and the
 *  restore / close pair, which are always shown. */
export function miniPlayerShows(width: number): { position: boolean; modes: boolean } {
    return { position: width >= MINI_PLAYER_POSITION_WIDTH, modes: width >= MINI_PLAYER_MODES_WIDTH };
}

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

// -- lyrics (ADR-051 D6; SPEC §3 flows 11-13, 12a; dpc_client_core/music/lyrics.py) --

/** One `mp.lyrics/1` line: `t_start` is null only in unsynced lyrics. */
export interface LyricsLine {
    t_start: number | null;
    t_end?: number;
    text: string;
}

/** `mp.lyrics/1`. */
export interface Lyrics {
    schema?: string;
    track_hash: string;
    source: 'lrc' | 'tags' | 'lrclib' | 'transcribe' | 'manual' | string;
    language?: string | null;
    /** Absent means synced; false only for plain (untimed) lines. */
    synced?: boolean;
    lines: LyricsLine[];
    edited?: boolean;
    lrclib?: { id: number; artist_name: string; track_name: string; album_name: string | null; duration: number | null; synced: boolean };
    updated_at?: string;
}

/** What one LRCLIB lookup came to (core: `MusicLyrics._lrclib_answer`). */
export interface LrclibOutcome {
    result: 'synced' | 'plain' | 'instrumental' | 'not_found' | 'mismatch' | 'empty' | 'rate_limited' | 'error'
        | 'disabled' | 'no_query' | 'no_duration' | string;
    cached: boolean;
    query: { artist: string; title: string; album: string | null; duration: number | null } | null;
    record: { id: number; artist_name: string; track_name: string; album_name: string | null; duration: number | null } | null;
    retry_after?: number;
    error?: string;
}

export interface LyricsChainStep {
    step: 'lrc' | 'tags' | 'lrclib' | 'transcribe' | 'manual' | string;
    state: string;
    detail: string;
}

/** What one Transcribe run reports (core: `transcript_lyrics`): the provider and model the
 *  result named (null when it named none), and whether the lines carry times. */
export interface TranscriptionInfo {
    provider: string | null;
    model: string | null;
    language: string | null;
    duration_s: number | null;
    segments: number;
    synced: boolean;
    fallback_reason?: string;
}

/** The track's last Transcribe run in the core process (`get_music_lyrics` → `transcribe`). */
export interface TranscribeRun {
    state: 'running' | 'done' | 'error' | string;
    code?: string;
    message?: string;
    applied?: boolean;
    transcription?: TranscriptionInfo;
    started_at?: string;
    finished_at?: string;
}

/** The answer of get_music_lyrics, lookup_music_lyrics_lrclib, save_music_lyrics and
 *  transcribe_music_lyrics. */
export interface LyricsAnswer {
    lyrics: Lyrics | null;
    from_store?: boolean;
    chain?: LyricsChainStep[];
    lrclib?: LrclibOutcome | null;
    lrclib_enabled?: boolean;
    /** Lyrics found but not applied, because others are stored: the person chooses. */
    candidate?: Lyrics | null;
    applied?: boolean;
    kept?: string;
    /** get_music_lyrics: the track's last Transcribe run, null when there was none. */
    transcribe?: TranscribeRun | null;
    /** transcribe_music_lyrics: what the run reports, and its lines as LRC text. */
    transcription?: TranscriptionInfo;
    lrc?: string;
}

/** The note shown next to "Find on LRCLIB" (SPEC §6 "Egress/privacy"). Only that click
 *  sends anything: opening a track reads the sidecar, the tags and the local cache. */
export const LRCLIB_EGRESS_TEXT = 'Sends artist, title, album and duration to lrclib.net';

/** The question asked before LRCLIB lyrics replace the person's own (manual or edited). */
export const LYRICS_OVERWRITE_CONFIRM_TEXT =
    'Replace your own lyrics with the LRCLIB lyrics? Your edited lyrics will be lost.';

/** True for lyrics the person wrote or edited: LRCLIB never replaces them without a
 *  confirmation (the core refuses with `overwrite_manual_required`). */
export function lyricsIsOwnEdit(lyrics: Lyrics | null | undefined): boolean {
    return !!lyrics && (lyrics.source === 'manual' || lyrics.edited === true);
}

/** The payload of `lookup_music_lyrics_lrclib`. `overwrite_manual` goes only with
 *  `replace` and only when the person confirmed replacing their own lyrics. */
export function lrclibLookupPayload(
    trackId: string,
    query: { artist?: string; title?: string; album?: string } = {},
    opts: { replace?: boolean; overwriteManual?: boolean } = {},
): Record<string, any> {
    const payload: Record<string, any> = { track_id: trackId, ...query, replace: opts.replace === true };
    if (opts.replace === true && opts.overwriteManual === true) payload.overwrite_manual = true;
    return payload;
}

// -- Transcribe (the lyrics chain's explicit step 4) ------------------------------

/** The question asked before a transcription replaces the person's own lyrics. */
export const TRANSCRIBE_OVERWRITE_CONFIRM_TEXT =
    'Replace your own lyrics with a machine transcription of this track? Your edited lyrics will be lost.';

/** The note on the Transcribe button: what runs, where the audio goes, and that sung
 *  words come out imperfect. */
export const TRANSCRIBE_NOTE_TEXT =
    "Runs DPC's speech-to-text (your voice transcription provider) on this track's file. "
    + 'With local Whisper the audio stays on this machine; a cloud or peer provider receives it. '
    + 'Sung words over music are often misheard: check the lines.';

/** The payload of `transcribe_music_lyrics`: `overwrite_manual` only when the person
 *  confirmed replacing their own lyrics. */
export function transcribeLyricsPayload(trackId: string, opts: { overwriteManual?: boolean } = {}): Record<string, any> {
    const payload: Record<string, any> = { track_id: trackId };
    if (opts.overwriteManual === true) payload.overwrite_manual = true;
    return payload;
}

/** One sentence on a finished Transcribe: who transcribed it and whether the lines
 *  are timed. It claims nothing about how well the times fit the words. */
export function transcriptionText(info: TranscriptionInfo | null | undefined): string {
    if (!info) return '';
    const who = info.provider
        ? `${info.provider}${info.model ? ` (${info.model})` : ''}`
        : 'an unnamed provider';
    const what = info.synced
        ? `${info.segments} line${info.segments === 1 ? '' : 's'} timed from its segments`
        : 'text only, no times (unsynced)';
    return `Transcribed by ${who}: ${what}. Machine transcription: words and times may be wrong.`;
}

/** True when a music command's answer is the webview giving up waiting (sendCommand's
 *  timeout); the core goes on with the command. */
export function isCommandTimeout(a: MusicAnswer<unknown>): boolean {
    return !a.ok && a.code === 'failed' && /timed out/i.test(a.message);
}

export type TranscribeOutcome =
    | { ok: true; lyrics: Lyrics | null; candidate: Lyrics | null; applied: boolean; info: TranscriptionInfo | null; kept?: string }
    | { ok: false; unavailable: boolean; code: string; message: string };

export const TRANSCRIBE_POLL_MS = 3000;
export const TRANSCRIBE_MAX_WAIT_MS = 30 * 60 * 1000;

/** One Transcribe click: `call` is transcribe_music_lyrics. A transcription can outlast
 *  the webview's command timeout; the core keeps going, so on a timeout this follows the
 *  run through `poll` (get_music_lyrics, whose `transcribe` says running/done/error)
 *  until it ends. Null when `cancelled()` turns true (another track was opened). */
export async function runTranscribe(
    call: () => Promise<MusicAnswer<LyricsAnswer>>,
    poll: () => Promise<MusicAnswer<LyricsAnswer>>,
    opts: {
        intervalMs?: number;
        maxWaitMs?: number;
        cancelled?: () => boolean;
        sleep?: (ms: number) => Promise<void>;
        now?: () => number;
    } = {},
): Promise<TranscribeOutcome | null> {
    const a = await call();
    if (a.ok) {
        return {
            ok: true,
            lyrics: a.data.lyrics ?? null,
            candidate: a.data.candidate ?? null,
            applied: a.data.applied === true,
            info: a.data.transcription ?? null,
            kept: a.data.kept,
        };
    }
    if (!isCommandTimeout(a)) return a;
    const interval = opts.intervalMs ?? TRANSCRIBE_POLL_MS;
    const maxWait = opts.maxWaitMs ?? TRANSCRIBE_MAX_WAIT_MS;
    const sleep = opts.sleep ?? ((ms: number) => new Promise<void>(r => setTimeout(r, ms)));
    const now = opts.now ?? (() => Date.now());
    const cancelled = opts.cancelled ?? (() => false);
    const start = now();
    for (;;) {
        await sleep(interval);
        if (cancelled()) return null;
        const p = await poll();
        if (cancelled()) return null;
        if (!p.ok) return p;
        const run = p.data.transcribe ?? null;
        if (run?.state === 'done') {
            return {
                ok: true,
                lyrics: p.data.lyrics ?? null,
                candidate: null,
                applied: run.applied === true,
                info: run.transcription ?? null,
                kept: run.applied === false ? 'your own lyrics were saved while the transcription ran, so they are kept' : undefined,
            };
        }
        if (run?.state === 'error') {
            return { ok: false, unavailable: false, code: run.code || 'transcription_failed', message: run.message || 'the transcription failed' };
        }
        if (run?.state !== 'running') {
            return { ok: false, unavailable: false, code: 'lost', message: 'the core no longer reports this transcription (was it restarted?); Look again to check' };
        }
        if (now() - start >= maxWait) {
            return { ok: false, unavailable: false, code: 'still_running', message: 'still running; its lyrics will show when it ends (Look again)' };
        }
    }
}

export function lyricsSynced(lyrics: Lyrics | null | undefined): boolean {
    return !!lyrics && lyrics.synced !== false && lyrics.lines.some(l => l.t_start != null);
}

/** Where line `i` ends: its `t_end`, else the next line's start, else the track's end. */
export function lyricsLineEnd(lines: readonly LyricsLine[], i: number, duration: number | null | undefined): number {
    const l = lines[i];
    if (l.t_end != null) return l.t_end;
    const next = lines[i + 1];
    if (next && next.t_start != null) return next.t_start;
    return duration != null && Number.isFinite(duration) && duration > 0 ? duration : Infinity;
}

/** The line current at `t` (SPEC flow 11): from its start until its end, else until
 *  the next line, else to the end of the track; in a gap after an explicit end, and
 *  before the first line, none (-1). Untimed lines are never current. */
export function currentLyricIndex(lines: readonly LyricsLine[], t: number, duration?: number | null): number {
    let i = -1;
    for (let k = 0; k < lines.length; k++) {
        const s = lines[k].t_start;
        if (s == null) return -1;
        if (s <= t + 0.02) i = k;
        else break;
    }
    return i >= 0 && t >= lyricsLineEnd(lines, i, duration) ? -1 : i;
}

/** The compact view's three lines: the previous, the current and the next. Between
 *  lines (or before the first) `current` is -1, `prev` is the last line that started
 *  and `next` the one coming up. */
export function lyricsWindow(lines: readonly LyricsLine[], t: number, duration?: number | null): { prev: number; current: number; next: number } {
    if (!lines.length || lines[0].t_start == null) return { prev: -1, current: -1, next: lines.length ? 0 : -1 };
    const current = currentLyricIndex(lines, t, duration);
    let started = current;
    if (current < 0) {
        started = -1;
        for (let k = 0; k < lines.length && (lines[k].t_start ?? Infinity) <= t + 0.02; k++) started = k;
    }
    const next = started + 1 < lines.length ? started + 1 : -1;
    return { prev: current >= 0 ? current - 1 : started, current, next };
}

/** `mm:ss.xx` (the core's `lrc_stamp`). */
export function lrcStamp(t: number | null | undefined): string {
    const v = Math.max(0, typeof t === 'number' && Number.isFinite(t) ? t : 0);
    let m = Math.floor(v / 60);
    let s = Math.round((v - m * 60) * 100) / 100;
    if (s >= 60) { m++; s -= 60; }
    return `${String(m).padStart(2, '0')}:${s.toFixed(2).padStart(5, '0')}`;
}

const LRC_LEAD = /^\s*(?:\[\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?\]\s*)+/;
const LRC_META = /^\s*\[[A-Za-z#][A-Za-z0-9_#-]*:.*\]\s*$/;

/** LRC text for lyrics (the core's `format_lrc`): untimed lines without a stamp. */
export function formatLrc(lyrics: Lyrics | null | undefined): string {
    if (!lyrics) return '';
    const out: string[] = [];
    if (lyrics.language) out.push(`[la:${lyrics.language}]`);
    for (const l of lyrics.lines) {
        if (l.t_start == null) out.push(l.text);
        else out.push(`[${lrcStamp(l.t_start)}] ${l.text}${l.t_end != null ? ` <${lrcStamp(l.t_end)}>` : ''}`);
    }
    return out.join('\n');
}

/** How many lines of an LRC text have text but no leading stamp (Save refuses them). */
export function countUnstampedLines(text: string): number {
    let n = 0;
    for (const line of text.split(/\r?\n/)) {
        if (!line.trim() || LRC_META.test(line) || LRC_LEAD.test(line)) continue;
        n++;
    }
    return n;
}

/** "Stamp current time" (SPEC flow 13): the line at `cursor` gets `t` as its leading
 *  stamp (replacing any it had), and the cursor moves to the start of the next line,
 *  so lyrics are timed by tapping along. A metadata line (`[la:en]`) is skipped. */
export function stampLrcLine(text: string, cursor: number, t: number): { text: string; cursor: number } {
    const at = Math.max(0, Math.min(text.length, cursor));
    const start = text.lastIndexOf('\n', at - 1) + 1;
    let end = text.indexOf('\n', at);
    if (end < 0) end = text.length;
    const line = text.slice(start, end);
    if (LRC_META.test(line)) return { text, cursor: end < text.length ? end + 1 : end };
    const body = line.replace(LRC_LEAD, '').replace(/\r$/, '').trim();
    const stamped = body ? `[${lrcStamp(t)}] ${body}` : `[${lrcStamp(t)}]`;
    const out = text.slice(0, start) + stamped + text.slice(end);
    const lineEnd = start + stamped.length;
    return { text: out, cursor: lineEnd < out.length ? lineEnd + 1 : lineEnd };
}

/** How a stored source reads on the badge: the core stores SPEC §4's `transcribe`, the
 *  badge says what happened to the lyrics. */
const LYRICS_SOURCE_LABELS: Record<string, string> = { transcribe: 'transcribed' };

/** The source badge: "lrclib · unsynced", "manual · edited", "transcribed", or "none". */
export function lyricsBadge(lyrics: Lyrics | null | undefined): string {
    if (!lyrics) return 'none';
    const parts = [LYRICS_SOURCE_LABELS[lyrics.source] ?? lyrics.source];
    if (lyrics.synced === false) parts.push('unsynced');
    if (lyrics.edited) parts.push('edited');
    return parts.join(' · ');
}

/** One honest sentence for an LRCLIB outcome. */
export function lrclibOutcomeText(o: LrclibOutcome | null | undefined): string {
    if (!o) return '';
    const q = o.query;
    const what = q ? `"${q.artist} - ${q.title}"${q.duration != null ? `, ${q.duration} s` : ''}` : 'this track';
    const cached = o.cached ? ' (from the local cache)' : '';
    switch (o.result) {
        case 'synced': return `LRCLIB: found synced lyrics${cached}.`;
        case 'plain': return `LRCLIB: found plain lyrics only, without times${cached}.`;
        case 'instrumental': return `LRCLIB marks this track instrumental: no lyrics${cached}.`;
        case 'not_found': return `LRCLIB: not found for ${what}${cached}.`;
        case 'mismatch': return `LRCLIB: a record came back${o.record?.duration != null ? ` (${o.record.duration} s)` : ''}, but its duration is more than ±2 s from this track, so it is not used${cached}.`;
        case 'empty': return `LRCLIB: the record has no lyrics${cached}.`;
        case 'rate_limited': return `LRCLIB is rate limiting this machine${o.retry_after ? `; try again in ${o.retry_after} s` : ''}.`;
        case 'error': return `LRCLIB: ${o.error || 'the lookup failed'}.`;
        case 'disabled': return 'Online lookup is off ([music] lrclib_enabled = false).';
        case 'no_query': return 'LRCLIB needs an artist and a title: type them below.';
        case 'no_duration': return 'LRCLIB needs the track\'s duration to check a match, and it is not known yet.';
        default: return `LRCLIB: ${o.result}.`;
    }
}

/** Artist and title for the LRCLIB fields: the entry's tags, else its file name
 *  "Artist - Title" (the core's `build_query`). */
export function guessArtistTitle(entry: LibraryEntry | null | undefined): { artist: string; title: string } {
    if (!entry) return { artist: '', title: '' };
    const artist = (entry.artist ?? '').trim();
    if (artist) return { artist, title: (entry.title ?? '').trim() };
    const base = (entry.source?.name || entry.title || '').replace(/\.[^./\\]+$/, '').trim();
    const m = base.match(/^(.+?)\s+[-–—]\s+(.+)$/);
    return m ? { artist: m[1].trim(), title: m[2].trim() } : { artist: '', title: (entry.title ?? base).trim() };
}

// -- hide, remove, delete (Mike, 2026-11-21) ---------------------------------------

/** The ids that may be played or queued: a hidden track is left out, an unknown id stays
 *  (the row says "Not in the library"). The playlist itself keeps hidden ids, so an
 *  unhide puts the track back where it was. */
export function playableIds(ids: readonly string[], byId: ReadonlyMap<string, LibraryEntry>): string[] {
    return ids.filter(id => !byId.get(id)?.hidden);
}

/** The hidden entries, in library order. */
export function hiddenEntries(entries: readonly LibraryEntry[]): LibraryEntry[] {
    return entries.filter(e => !!e.hidden);
}

/** The entries whose file is gone. */
export function missingEntries(entries: readonly LibraryEntry[]): LibraryEntry[] {
    return entries.filter(e => !!e.missing);
}

/** Whether Delete file is offered: only the core's `deletable` flag (ownership is decided
 *  there, by path). The core checks again when the command arrives. */
export function canDeleteFile(entry: LibraryEntry | null | undefined): boolean {
    return !!entry && entry.deletable === true && !entry.missing;
}

/** Take every occurrence of `ids` out of the queue. When the current track is one of them
 *  the index points at the track that followed it (not loaded), or at -1 when none is left. */
export function queueDrop(q: QueueState, ids: readonly string[]): QueueState {
    const gone = new Set(ids);
    if (!q.ids.some(id => gone.has(id))) return q;
    const kept: string[] = [];
    let index = -1;
    q.ids.forEach((id, i) => {
        if (gone.has(id)) return;
        if (i >= q.index && index < 0) index = kept.length;
        kept.push(id);
    });
    if (!kept.length) return { ...q, ids: [], index: -1 };
    return { ...q, ids: kept, index: index < 0 ? kept.length - 1 : index };
}
