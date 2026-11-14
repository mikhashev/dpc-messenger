// KV snapshots in the providers editor (ADR-044, amendment 2026-11-13, points 1, 6 and 10):
// the alias fields' defaults, the disk hint, the UNC-path check, and the lines the Local
// engines block shows from local_engine_status()["kv_snapshots"]. Pure functions: the
// component only renders what these return.

/** What one save or restore did last; null when it has not happened since the backend started. */
export interface KvSnapshotEvent {
    /** saved, skipped, engine_refuses, transport, engine_died, failed, not_loaded,
     *  restored, refused, no_room, missing; an unknown word is shown as it is. */
    outcome: string;
    bytes: number | null;
    ms: number | null;
    reason: string | null;
}

/** One alias's row of local_engine_status()["kv_snapshots"]. */
export interface KvSnapshotsRow {
    folder: string;
    files: number;
    bytes: number;
    cap_bytes: number;
    /** Unix seconds; null when the folder holds no snapshot. */
    oldest_expiry: number | null;
    last_save: KvSnapshotEvent | null;
    last_restore: KvSnapshotEvent | null;
    /** Why saving is off for this launch ("folder missing", "the engine refuses saves"); null when on. */
    off_reason: string | null;
}

export interface StatusLine {
    text: string;
    warn: boolean;
}

/** What an empty alias field resolves to (the backend's own defaults). */
export const KV_SNAPSHOT_DEFAULT_TTL_S = 86400;
export const KV_SNAPSHOT_DEFAULT_MAX_GIB = 32;

export const KV_SNAPSHOTS_FOLDER_LABEL = 'KV snapshots folder (slot_save_path)';

export const KV_SNAPSHOTS_HELP =
    "A snapshot of the conversation's engine state (KV cache, checkpoints, draft state) is kept here "
    + 'when the engine stops, and brought back when that conversation returns. Empty: off.';

/** The dated disk hint of ADR-044 point 6; shown as a tooltip, not as a paragraph. */
export const KV_SNAPSHOTS_DISK_HINT =
    'As measured 2026-11-13. SSD: a 250K conversation saves in ~2 s (Strata) / ~6 s (llama-server) '
    + 'and returns in ~3 s / ~5 s. HDD: on Strata a save takes ~38 s, which the agent waiting for the '
    + 'card waits through, and a restore ~43 s, slower in total than reading it again (~67 s), so Strata '
    + 'does not save to an HDD at a stop. On llama-server a 250K cold read is ~7 min, so a deep '
    + 'conversation pays on either disk. An encrypted volume is your option.';

export const KV_SNAPSHOTS_UNC_WARNING =
    'A network share: a save over it can be slow, and the folder may be missing when the engine starts.';

/** A Windows UNC path (\\server\share). On Windows a share draws a warning (point 6). */
export function isUncPath(path: string | null | undefined): boolean {
    return typeof path === 'string' && path.trim().startsWith('\\\\');
}

/** 86400 -> "24 h", 90 -> "90 s": the TTL field is in seconds, a person thinks in hours. */
export function ttlHuman(seconds: number): string {
    if (!Number.isFinite(seconds) || seconds < 0) return '';
    if (seconds >= 2 * 86400 && seconds % 86400 === 0) return `${seconds / 86400} d`;
    if (seconds >= 3600 && seconds % 3600 === 0) return `${seconds / 3600} h`;
    if (seconds >= 60 && seconds % 60 === 0) return `${seconds / 60} min`;
    return `${seconds} s`;
}

/** The TTL field's placeholder: the default, in seconds and in words. */
export function ttlPlaceholder(): string {
    return `${KV_SNAPSHOT_DEFAULT_TTL_S} (${ttlHuman(KV_SNAPSHOT_DEFAULT_TTL_S)})`;
}

const trim1 = (n: number) => (Math.round(n * 10) / 10).toString();

/** Binary steps, named as such, so the 32 GiB cap reads "32 GiB". */
export function formatBytes(bytes: number): string {
    if (!Number.isFinite(bytes) || bytes < 0) return '?';
    const KIB = 1024, MIB = KIB * 1024, GIB = MIB * 1024;
    if (bytes >= GIB) return `${trim1(bytes / GIB)} GiB`;
    if (bytes >= MIB) return `${Math.round(bytes / MIB)} MiB`;
    if (bytes >= KIB) return `${Math.round(bytes / KIB)} KiB`;
    return `${Math.round(bytes)} B`;
}

export function formatMs(ms: number): string {
    if (!Number.isFinite(ms) || ms < 0) return '?';
    if (ms < 1000) return `${Math.round(ms)} ms`;
    const s = ms / 1000;
    return s >= 100 ? `${Math.round(s)} s` : `${trim1(s)} s`;
}

/** How far ahead a moment is: "5 min", "3 h 20 min", "23 h", "4 d". */
export function formatSpan(seconds: number): string {
    const s = Math.max(0, seconds);
    if (s < 3600) return `${Math.max(1, Math.ceil(s / 60))} min`;
    if (s < 10 * 3600) {
        const h = Math.floor(s / 3600);
        const m = Math.round((s - h * 3600) / 60);
        return m === 0 || m === 60 ? `${m === 60 ? h + 1 : h} h` : `${h} h ${m} min`;
    }
    if (s < 48 * 3600) return `${Math.round(s / 3600)} h`;
    return `${Math.round(s / 86400)} d`;
}

const OUTCOME_WORDS: Record<string, string> = {
    saved: 'saved',
    skipped: 'skipped',
    engine_refuses: 'the engine refuses saves',
    transport: 'could not reach the engine',
    engine_died: 'the engine died',
    failed: 'failed',
    not_loaded: 'no model loaded',
    restored: 'restored',
    refused: 'the engine refused it',
    no_room: 'no room',
    missing: 'file missing',
};

/** "saved 4.0 GiB in 2.1 s", "skipped (below the size threshold)", "no room". */
export function describeEvent(ev: KvSnapshotEvent): string {
    const word = OUTCOME_WORDS[ev.outcome] ?? ev.outcome;
    const done = ev.outcome === 'saved' || ev.outcome === 'restored';
    const parts: string[] = [];
    if (done && ev.bytes != null) parts.push(formatBytes(ev.bytes));
    if (done && ev.ms != null) parts.push(`in ${formatMs(ev.ms)}`);
    let text = parts.length ? `${word} ${parts.join(' ')}` : word;
    if (!done && ev.reason) text += ` (${ev.reason})`;
    return text;
}

const BAD_OUTCOMES = new Set(['engine_refuses', 'transport', 'engine_died', 'failed', 'refused']);

/** The "KV snapshots" line of one alias: files, bytes against the cap, the oldest expiry. */
export function summaryLine(row: KvSnapshotsRow, nowS: number): string {
    const cap = `of ${formatBytes(row.cap_bytes)}`;
    if (!row.files) return `KV snapshots (agent conversations): none held (cap ${formatBytes(row.cap_bytes)})`;
    let text = `KV snapshots (agent conversations): ${row.files} ${row.files === 1 ? 'file' : 'files'}, ${formatBytes(row.bytes)} ${cap}`;
    if (row.oldest_expiry != null) {
        text += row.oldest_expiry <= nowS
            ? ' · oldest is past its expiry'
            : ` · oldest expires in ${formatSpan(row.oldest_expiry - nowS)}`;
    }
    return text;
}

/** All the lines for one alias's row, in the order the Local engines block shows them. */
export function kvSnapshotLines(row: KvSnapshotsRow, nowS: number): StatusLine[] {
    const lines: StatusLine[] = [{ text: summaryLine(row, nowS), warn: false }];
    const events: [string, KvSnapshotEvent | null][] = [['Last save', row.last_save], ['Last restore', row.last_restore]];
    for (const [label, ev] of events) {
        if (ev) lines.push({ text: `${label}: ${describeEvent(ev)}`, warn: BAD_OUTCOMES.has(ev.outcome) });
    }
    if (row.off_reason) lines.push({ text: `Off this launch: ${row.off_reason}`, warn: true });
    return lines;
}

/** One block per alias that has a row, in alias order; none when the backend sent none. */
export function kvSnapshotBlocks(
    rows: Record<string, KvSnapshotsRow> | null | undefined,
    nowS: number,
): { alias: string; lines: StatusLine[] }[] {
    return Object.keys(rows ?? {}).sort().map((alias) => ({ alias, lines: kvSnapshotLines(rows![alias], nowS) }));
}
