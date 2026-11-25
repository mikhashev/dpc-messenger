// MP3 and Opus exports of the Music Player (Mike #210, #171; core: dpc_client_core/music/export.py).
// One store for what exists (the "mp3" chips, the export folder, its warning), and one for
// the export that runs or has just finished, keyed by the track or playlist id it was asked
// for, so the row, the now-playing card and the playlist bar each show their own line. The
// core broadcasts `music_export_done` when a file is written, from any window, and the
// list is read again then.

import { writable, get } from 'svelte/store';
import { deleteMusicExport, exportMusicPlaylist, exportMusicTrack, getMusicExportInfo } from '$lib/coreService';
import {
    DEFAULT_EXPORT_PRESET,
    DEFAULT_LIMIT_TRUE_PEAK,
    exportFormatName,
    playlistExportLine,
    trackExportLine,
    type MusicExportInfo,
} from '$lib/utils/musicExport';

export const musicExportInfo = writable<MusicExportInfo | null>(null);

/** What the window shows for one id: running, or the result line and the file to reveal. */
export interface ExportStatus {
    running: boolean;
    text: string;
    path: string | null;
    error: boolean;
}

export const musicExportStatus = writable<Record<string, ExportStatus>>({});

/** The preset the person picked last in this session. */
export const musicExportPreset = writable<string>(DEFAULT_EXPORT_PRESET);

/** The export switch: keep the -1 dBTP true-peak limit (Mike's default, #171) or export the audio as it comes. */
export const musicExportLimit = writable<boolean>(DEFAULT_LIMIT_TRUE_PEAK);

let loading: Promise<void> | null = null;
/** A read was asked for while one was under way: that one may predate the change the asker
 *  wants to see (a copy just deleted), so one more read follows it. */
let readAgain = false;

function setStatus(id: string, status: ExportStatus): void {
    musicExportStatus.update(m => ({ ...m, [id]: status }));
}

/** Forget the line of an id (its × button). */
export function clearExportStatus(id: string): void {
    musicExportStatus.update(m => {
        if (!(id in m)) return m;
        const { [id]: _drop, ...rest } = m;
        return rest;
    });
}

/** Read the export folder and the existing exports of every track. A backend without the
 *  export commands leaves the store empty: the chips and the warning do not show. */
export function loadMusicExports(): Promise<void> {
    if (loading) {
        readAgain = true;
        return loading;
    }
    loading = (async () => {
        try {
            const a = await getMusicExportInfo();
            if (a.ok) musicExportInfo.set(a.data);
        } finally {
            loading = null;
            if (readAgain) {
                readAgain = false;
                void loadMusicExports();
            }
        }
    })();
    return loading;
}

/** Delete one exported copy (the core deletes the file and its entry); null on success, else
 *  the line to show. The list is read again either way. */
export async function deleteExportedCopy(exportId: string): Promise<string | null> {
    const a = await deleteMusicExport(exportId);
    await loadMusicExports();
    if (a.ok) return null;
    return a.unavailable ? 'Deleting an export is not available on this backend yet.' : a.message;
}

/** A `music_library_changed` event: only an export change moves the copies list. */
export function noteMusicExportsChanged(payload: { reasons?: string[] } | null | undefined): void {
    if (payload?.reasons?.includes('export')) void loadMusicExports();
}

/** Export one track; the line says "Exported", "Already exported" or the core's error. */
export async function exportTrackFile(trackId: string, title: string, preset?: string, limitTruePeak?: boolean): Promise<void> {
    const current = get(musicExportStatus)[trackId];
    if (current?.running) return;
    const chosen = preset ?? get(musicExportPreset);
    setStatus(trackId, { running: true, text: `Exporting "${title}" as ${exportFormatName(chosen)}…`, path: null, error: false });
    const a = await exportMusicTrack(trackId, chosen, limitTruePeak ?? get(musicExportLimit));
    if (a.ok) {
        setStatus(trackId, { running: false, text: trackExportLine(a.data), path: a.data.path, error: false });
        await loadMusicExports();
    } else {
        const text = a.unavailable ? 'Export is not available on this backend yet.' : a.message;
        setStatus(trackId, { running: false, text, path: null, error: true });
    }
}

/** Export a playlist into its own folder; the line counts what was done. */
export async function exportPlaylistFiles(playlistId: string, name: string, preset?: string, limitTruePeak?: boolean): Promise<void> {
    const current = get(musicExportStatus)[playlistId];
    if (current?.running) return;
    const chosen = preset ?? get(musicExportPreset);
    setStatus(playlistId, { running: true, text: `Exporting the playlist "${name}" as ${exportFormatName(chosen)}…`, path: null, error: false });
    const a = await exportMusicPlaylist(playlistId, chosen, limitTruePeak ?? get(musicExportLimit));
    if (a.ok) {
        const failed = a.data.tracks.some(t => t.status === 'error');
        setStatus(playlistId, { running: false, text: playlistExportLine(a.data), path: a.data.folder, error: failed });
        await loadMusicExports();
    } else {
        const text = a.unavailable ? 'Export is not available on this backend yet.' : a.message;
        setStatus(playlistId, { running: false, text, path: null, error: true });
    }
}

/** A `music_export_done` event from the core: read the exports again. */
export function noteMusicExportDone(_payload: { track_id?: string; playlist_id?: string; path?: string } | null | undefined): void {
    void loadMusicExports();
}
