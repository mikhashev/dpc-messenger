// MP3 and Opus export of library tracks (Mike, #210 and #171; core: dpc_client_core/music/export.py).
// Pure helpers for the Music Player: the presets, the "mp3" / "opus" chip of a track that has an
// export, and the one line the window shows after an export. The core decides names,
// folders and duplicates; the UI only names a track id or a playlist id, a preset and whether
// the -1 dBTP true-peak limit stays on (it does by default; Mike's switch turns it off).

export const DEFAULT_EXPORT_PRESET = 'mp3-v0';

export interface MusicExportPreset {
    id: string;
    label: string;
    format?: string;
}

/** The presets the core offers; used until `get_music_export_info` has answered. */
export const EXPORT_PRESETS: MusicExportPreset[] = [
    { id: 'mp3-v0', label: 'MP3 V0 (VBR, about 245 kbps)', format: 'mp3' },
    { id: 'mp3-320', label: 'MP3 320 kbps (CBR)', format: 'mp3' },
    { id: 'opus-160', label: 'Opus 160 kbps (VBR, for messengers)', format: 'opus' },
];

export const DEFAULT_LIMIT_TRUE_PEAK = true;

/** "MP3" or "Opus": the format a preset writes, for the button and the status lines. */
export function exportFormatName(presetId: string | null | undefined): string {
    return presetId && presetId.startsWith('opus') ? 'Opus' : 'MP3';
}

/** The short name in a select. */
export function presetShortLabel(id: string): string {
    if (id === 'mp3-v0') return 'MP3 V0';
    if (id === 'mp3-320') return 'MP3 320';
    if (id === 'opus-160') return 'Opus 160';
    return id;
}

/** A file in a playlist folder that holds the same song as a single export: a hard link of it (the
 *  same bytes on disk) or a copy. Deleting the single export leaves it where it is. */
export interface PlaylistCopy {
    playlist_id: string;
    playlist_name: string;
    /** The playlist's folder name under the export root. */
    folder: string;
    path: string;
    name: string;
    how: 'hardlink' | 'copy';
}

/** One existing export of a track (`get_music_export_info`). */
export interface MusicExportRecord {
    export_id: string;
    preset: string;
    format: string;
    path: string;
    name: string;
    size_bytes?: number | null;
    exported_at?: string | null;
    limited_db?: number | null;
    true_peak_out_dbtp?: number | null;
    /** False for an export made with the true-peak limit switched off; absent from an older core (limited). */
    limit_true_peak?: boolean;
    /** The playlist-folder files of the same song; absent from an older core. */
    playlists?: PlaylistCopy[];
}

/** `delete_music_export`'s answer. */
export interface DeleteExportAnswer {
    export_id: string;
    path: string;
    name: string;
    preset: string;
    format: string;
    deleted: boolean;
    file_missing: boolean;
    /** The playlist-folder files of the same song, which stay. */
    playlist_copies?: PlaylistCopy[];
}

export interface MusicExportInfo {
    export_root: string;
    default_root: string;
    inside_watched_folder: boolean;
    watched_folder: string | null;
    presets: MusicExportPreset[];
    default_preset: string;
    exports: Record<string, MusicExportRecord[]>;
}

/** `export_music_track`'s answer. */
export interface TrackExportAnswer {
    track_id: string;
    path: string;
    name: string;
    already_exported: boolean;
    relinked?: boolean;
    preset: string;
    limit_true_peak?: boolean;
    limited_db?: number | null;
    true_peak_out_dbtp?: number | null;
}

export interface PlaylistTrackResult {
    track_id: string;
    title?: string | null;
    status: 'ok' | 'skipped' | 'error';
    path?: string;
    name?: string;
    already_exported?: boolean;
    placed?: string;
    code?: string;
    reason?: string;
}

/** `export_music_playlist`'s answer. */
export interface PlaylistExportAnswer {
    playlist_id: string;
    folder: string;
    folder_name?: string;
    preset: string;
    tracks: PlaylistTrackResult[];
    removed?: string[];
    kept?: string[];
}

/** The last part of a path, on any OS. */
export function fileNameOf(path: string | null | undefined): string {
    if (!path) return '';
    const parts = path.split(/[\\/]+/).filter(Boolean);
    return parts.length ? parts[parts.length - 1] : path;
}

/** "limited to −1 dBTP" when the limiter ran, else ''. */
export function limiterNote(limitedDb: number | null | undefined): string {
    if (typeof limitedDb !== 'number') return '';
    return `limited to ${limitedDb < 0 ? '−' : ''}${Math.abs(limitedDb)} dBTP`;
}

/** "4.2 MB", "812 KB", or '' when the size is not known. */
export function exportSizeText(bytes: number | null | undefined): string {
    if (typeof bytes !== 'number' || !isFinite(bytes) || bytes < 0) return '';
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** The date of an export as "2026-11-24", or '' when the core gave none. */
export function exportDateText(exportedAt: string | null | undefined): string {
    const m = /^(\d{4}-\d{2}-\d{2})/.exec(exportedAt ?? '');
    return m ? m[1] : '';
}

/** The facts under a file name in the exports tree: "MP3 V0 · 4.2 MB · 2026-11-24". */
export function exportCopyDetail(r: MusicExportRecord): string {
    const limit = r.limit_true_peak === false ? 'no limit' : limiterNote(r.limited_db);
    return [presetShortLabel(r.preset), exportSizeText(r.size_bytes), exportDateText(r.exported_at), limit]
        .filter(Boolean)
        .join(' · ');
}

/** One playlist file as a person reads it: "Road trip / 01 - Band - Song.mp3 (hard link)". */
export function playlistCopyText(c: PlaylistCopy): string {
    return `${c.folder} / ${c.name} (${c.how === 'hardlink' ? 'hard link' : 'copy'})`;
}

/** The line under an exported copy that names the playlist folders holding the same song, or ''. */
export function playlistCopiesLine(copies: PlaylistCopy[] | null | undefined): string {
    if (!copies || !copies.length) return '';
    const folders = [...new Set(copies.map(c => c.folder))];
    return `Also in ${folders.length === 1 ? 'playlist folder' : 'playlist folders'}: ${folders.join(', ')}`;
}

/** What the confirm for deleting an exported copy says. Playlist-folder files of the same song are
 *  not deleted, so it names the ones that stay (at most five, then a count). */
export function deleteCopyConfirm(copyName: string, trackTitle: string, playlists: PlaylistCopy[] | null | undefined): string {
    const base = `Delete the exported file "${copyName}" from disk? This cannot be undone. "${trackTitle}" stays in the library.`;
    if (!playlists || !playlists.length) return base;
    const shown = playlists.slice(0, 5).map(c => `  ${playlistCopyText(c)}`);
    if (playlists.length > shown.length) shown.push(`  and ${playlists.length - shown.length} more`);
    const linked = playlists.some(c => c.how === 'hardlink');
    const where = playlists.length === 1 ? 'this playlist folder' : 'these playlist folders';
    return [
        base,
        `The same song stays in ${where}, which this does not touch:\n${shown.join('\n')}`,
        ...(linked ? ['A hard link is the same file on disk, so its audio is not freed until it is deleted too.'] : []),
    ].join('\n\n');
}

/** The label of the toggle that opens a track's exports: "mp3" or "mp3 ×2". */
export function exportToggleLabel(records: MusicExportRecord[] | null | undefined): string {
    if (!records || !records.length) return '';
    const fmt = records[0].format || 'mp3';
    return records.length > 1 ? `${fmt} ×${records.length}` : fmt;
}

/** The chip of a track that has an export: the default preset's file first. */
export function exportChip(records: MusicExportRecord[] | null | undefined): { label: string; path: string; title: string } | null {
    if (!records || !records.length) return null;
    const pick = records.find(r => r.preset === DEFAULT_EXPORT_PRESET) ?? records[0];
    const others = records.length > 1 ? ` (+${records.length - 1} more)` : '';
    const limit = pick.limit_true_peak === false ? 'no limit' : limiterNote(pick.limited_db);
    return {
        label: pick.format || 'mp3',
        path: pick.path,
        title: `Exported as ${presetShortLabel(pick.preset)}: ${pick.name}${others}${limit ? ` · ${limit}` : ''}. Click to show it in its folder.`,
    };
}

/** The line after a track export. */
export function trackExportLine(a: TrackExportAnswer): string {
    const name = a.name || fileNameOf(a.path);
    if (a.already_exported) {
        return a.relinked ? `Already exported: ${name} (found under its new name)` : `Already exported: ${name}`;
    }
    const limit = a.limit_true_peak === false ? 'no limit' : limiterNote(a.limited_db);
    return `Exported: ${name}${limit ? ` · ${limit}` : ''}`;
}

/** The line after a playlist export. */
export function playlistExportLine(a: PlaylistExportAnswer): string {
    const ok = a.tracks.filter(t => t.status === 'ok');
    const skipped = a.tracks.filter(t => t.status === 'skipped').length;
    const failed = a.tracks.filter(t => t.status === 'error').length;
    const reused = ok.filter(t => t.already_exported).length;
    const folder = a.folder_name || fileNameOf(a.folder);
    const parts = [`Exported ${ok.length} of ${a.tracks.length} to ${folder}`];
    if (reused) parts.push(`${reused} already exported, not encoded again`);
    if (skipped) parts.push(`${skipped} skipped (hidden or missing)`);
    if (failed) parts.push(`${failed} failed`);
    if (a.removed?.length) parts.push(`${a.removed.length} old ${a.removed.length === 1 ? 'file' : 'files'} removed`);
    return parts.join(' · ');
}

/** The warning when the export folder is inside a watched folder, or ''. */
export function exportFolderWarning(info: MusicExportInfo | null | undefined): string {
    if (!info || !info.inside_watched_folder) return '';
    return `The export folder ${info.export_root} is inside the watched folder ${info.watched_folder ?? ''}. `
        + 'DPC recognises its own exports by their tag and does not add them to the library again, '
        + 'but other players will see both files.';
}
