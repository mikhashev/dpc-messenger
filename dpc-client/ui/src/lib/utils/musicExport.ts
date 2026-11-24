// MP3 export of library tracks (Mike, #210; core: dpc_client_core/music/export.py).
// Pure helpers for the Music Player: the presets, the "mp3" chip of a track that has an
// export, and the one line the window shows after an export. The core decides names,
// folders and duplicates; the UI only names a track id or a playlist id and a preset.

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
];

/** The short name in a select. */
export function presetShortLabel(id: string): string {
    if (id === 'mp3-v0') return 'MP3 V0';
    if (id === 'mp3-320') return 'MP3 320';
    return id;
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
    const limit = limiterNote(r.limited_db);
    return [presetShortLabel(r.preset), exportSizeText(r.size_bytes), exportDateText(r.exported_at), limit]
        .filter(Boolean)
        .join(' · ');
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
    const limit = limiterNote(pick.limited_db);
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
    const limit = limiterNote(a.limited_db);
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
    return `The MP3 export folder ${info.export_root} is inside the watched folder ${info.watched_folder ?? ''}. `
        + 'DPC recognises its own exports by their tag and does not add them to the library again, '
        + 'but other players will see both files.';
}
