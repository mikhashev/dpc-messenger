import { describe, it, expect } from 'vitest';
import {
    DEFAULT_EXPORT_PRESET,
    EXPORT_PRESETS,
    exportChip,
    exportCopyDetail,
    exportDateText,
    exportFolderWarning,
    exportSizeText,
    DEFAULT_LIMIT_TRUE_PEAK,
    deleteCopyConfirm,
    exportFormatName,
    exportToggleLabel,
    fileNameOf,
    limiterNote,
    playlistCopiesLine,
    playlistCopyText,
    playlistExportLine,
    presetShortLabel,
    trackExportLine,
    type MusicExportInfo,
    type MusicExportRecord,
    type PlaylistCopy,
} from './musicExport';
import player from '../components/music/MusicPlayer.svelte?raw';
import core from '../coreService.ts?raw';
import store from '../services/musicExports.ts?raw';

const v0: MusicExportRecord = { export_id: 'e0', preset: 'mp3-v0', format: 'mp3', path: 'C:\\Users\\m\\.dpc\\exports\\music\\Band - Song.mp3', name: 'Band - Song.mp3', limited_db: -1 };
const cbr: MusicExportRecord = { export_id: 'e1', preset: 'mp3-320', format: 'mp3', path: '/home/m/.dpc/exports/music/Band - Song (320 kbps).mp3', name: 'Band - Song (320 kbps).mp3', limited_db: null };

describe('names and presets', () => {
    it('takes the last part of a Windows or POSIX path', () => {
        expect(fileNameOf(v0.path)).toBe('Band - Song.mp3');
        expect(fileNameOf(cbr.path)).toBe('Band - Song (320 kbps).mp3');
        expect(fileNameOf('/music/Mix/')).toBe('Mix');
        expect(fileNameOf(null)).toBe('');
    });

    it('defaults to V0 and offers 320 and Opus 160 too', () => {
        expect(DEFAULT_EXPORT_PRESET).toBe('mp3-v0');
        expect(EXPORT_PRESETS.map(p => p.id)).toEqual(['mp3-v0', 'mp3-320', 'opus-160']);
        expect(presetShortLabel('mp3-v0')).toBe('MP3 V0');
        expect(presetShortLabel('mp3-320')).toBe('MP3 320');
        expect(presetShortLabel('opus-160')).toBe('Opus 160');
        expect(presetShortLabel('opus-128')).toBe('opus-128');
    });

    it('names the format a preset writes, for the buttons and status lines', () => {
        expect(exportFormatName('mp3-v0')).toBe('MP3');
        expect(exportFormatName('mp3-320')).toBe('MP3');
        expect(exportFormatName('opus-160')).toBe('Opus');
        expect(exportFormatName(undefined)).toBe('MP3');
    });

    it('keeps the true-peak limit on by default, the way Mike decided', () => {
        expect(DEFAULT_LIMIT_TRUE_PEAK).toBe(true);
    });

    it('names the limiter only when it ran', () => {
        expect(limiterNote(-1)).toBe('limited to −1 dBTP');
        expect(limiterNote(null)).toBe('');
        expect(limiterNote(undefined)).toBe('');
    });
});

describe('the mp3 chip', () => {
    it('shows for a track with an export and reveals the default preset file first', () => {
        const chip = exportChip([cbr, v0]);
        expect(chip?.label).toBe('mp3');
        expect(chip?.path).toBe(v0.path);
        expect(chip?.title).toContain('Band - Song.mp3');
        expect(chip?.title).toContain('(+1 more)');
        expect(chip?.title).toContain('limited to −1 dBTP');
    });

    it('falls back to another preset and is absent without exports', () => {
        expect(exportChip([cbr])?.path).toBe(cbr.path);
        expect(exportChip([])).toBeNull();
        expect(exportChip(undefined)).toBeNull();
    });
});

describe('the result lines', () => {
    it('says Exported with the limiter, or Already exported', () => {
        expect(trackExportLine({ track_id: 't', path: v0.path, name: 'Band - Song.mp3', already_exported: false, preset: 'mp3-v0', limited_db: -1 }))
            .toBe('Exported: Band - Song.mp3 · limited to −1 dBTP');
        expect(trackExportLine({ track_id: 't', path: v0.path, name: 'Band - Song.mp3', already_exported: false, preset: 'mp3-v0', limited_db: null }))
            .toBe('Exported: Band - Song.mp3');
        expect(trackExportLine({ track_id: 't', path: v0.path, name: '', already_exported: true, preset: 'mp3-v0' }))
            .toBe('Already exported: Band - Song.mp3');
        expect(trackExportLine({ track_id: 't', path: '/x/mine.mp3', name: 'mine.mp3', already_exported: true, relinked: true, preset: 'mp3-v0' }))
            .toBe('Already exported: mine.mp3 (found under its new name)');
    });

    it('says no limit for an export made with the limit off, on the line, the copy and the chip', () => {
        expect(trackExportLine({ track_id: 't', path: '/x/a (no limit).opus', name: 'a (no limit).opus', already_exported: false, preset: 'opus-160', limit_true_peak: false, limited_db: null }))
            .toBe('Exported: a (no limit).opus · no limit');
        const off: MusicExportRecord = { ...v0, preset: 'opus-160', format: 'opus', name: 'a (no limit).opus', limited_db: null, limit_true_peak: false, size_bytes: 1_048_576 };
        expect(exportCopyDetail(off)).toBe('Opus 160 · 1.0 MB · no limit');
        expect(exportChip([off])?.label).toBe('opus');
        expect(exportChip([off])?.title).toContain('no limit');
        expect(exportCopyDetail({ ...off, limit_true_peak: true, limited_db: -1 })).toBe('Opus 160 · 1.0 MB · limited to −1 dBTP');
    });

    it('counts a playlist export', () => {
        const line = playlistExportLine({
            playlist_id: 'pl_1', folder: '/x/exports/music/Road trip', preset: 'mp3-v0', removed: ['01 - old.mp3'],
            tracks: [
                { track_id: 'a', status: 'ok', already_exported: true },
                { track_id: 'b', status: 'ok', already_exported: false },
                { track_id: 'c', status: 'skipped', reason: 'hidden' },
                { track_id: 'd', status: 'error', reason: 'no ffmpeg' },
            ],
        });
        expect(line).toBe('Exported 2 of 4 to Road trip · 1 already exported, not encoded again · 1 skipped (hidden or missing) · 1 failed · 1 old file removed');
    });

    it('warns only when the export folder is inside a watched folder', () => {
        const info: MusicExportInfo = {
            export_root: '/home/m/Music/exports', default_root: '/home/m/.dpc/exports/music', inside_watched_folder: true,
            watched_folder: '/home/m/Music', presets: EXPORT_PRESETS, default_preset: 'mp3-v0', exports: {},
        };
        expect(exportFolderWarning(info)).toContain('inside the watched folder /home/m/Music');
        expect(exportFolderWarning({ ...info, inside_watched_folder: false })).toBe('');
        expect(exportFolderWarning(null)).toBe('');
    });
});

describe('the window wiring', () => {
    it('offers Export MP3 in the row menu, on the now-playing card and on a playlist', () => {
        expect(player).toContain('onclick={() => exportRow(row.entry!)}');
        expect(player).toContain('>Export…</button>');
        expect(player).toContain('onclick={() => cur && exportTrackFile(cur.id, cur.title)}');
        expect(player).toContain('>Export playlist</button>');
    });

    it('puts the true-peak switch beside every preset select, on by default', () => {
        const snippet = player.slice(player.indexOf('{#snippet presetSelect'), player.indexOf('{/snippet}', player.indexOf('{#snippet presetSelect')));
        expect(snippet).toContain('bind:checked={$musicExportLimit}');
        expect(snippet).toContain('Limit peaks (−1 dBTP)');
        expect(player).toContain("{@render presetSelect('Export preset for this track')}");
        expect(player).toContain("{@render presetSelect('Export preset for the playlist export')}");
        expect(player).toContain('{@render presetSelect(`Export preset for ${row.entry.title}`)}');
    });

    it('sends limit_true_peak: false only when the switch is off, and never a preset the person did not pick', () => {
        expect(core).toContain('...(limitTruePeak ? {} : { limit_true_peak: false })');
        expect(core).toContain('exportMusicTrack(trackId: string, preset?: string, limitTruePeak = true)');
        expect(core).toContain('exportMusicPlaylist(playlistId: string, preset?: string, limitTruePeak = true)');
        expect(store).toContain('limitTruePeak ?? get(musicExportLimit)');
        expect(store).toContain('export const musicExportLimit = writable<boolean>(DEFAULT_LIMIT_TRUE_PEAK);');
    });

    it('reveals the file through the existing helper; a row has the copies toggle and the card the chip', () => {
        expect(player).toContain('await revealInFolder(path, revealItemInDir)');
        expect(player).toContain('{@render originBadge(row.entry)}{@render exportToggle(row.entry)}');
        expect(player).toContain('{@render exportChipTag(cur)}');
    });

    it('the core door sends ids, a preset and the switch only when it is off, and the export done event reaches the store', () => {
        expect(core).toContain("callMusic(sendCommand, 'export_music_track', {");
        expect(core).toContain("callMusic(sendCommand, 'export_music_playlist'");
        expect(core).toContain('message.event === "music_export_done"');
        expect(core).toContain("command === 'export_music_track'");
    });
});

describe('the exported copies under a track row', () => {
    const copy = { ...v0, size_bytes: 4_404_019, exported_at: '2026-11-24T10:00:00Z' };

    it('writes sizes and dates the way the rows do', () => {
        expect(exportSizeText(512)).toBe('512 B');
        expect(exportSizeText(812 * 1024)).toBe('812 KB');
        expect(exportSizeText(4_404_019)).toBe('4.2 MB');
        expect(exportSizeText(null)).toBe('');
        expect(exportSizeText(-1)).toBe('');
        expect(exportDateText('2026-11-24T10:00:00Z')).toBe('2026-11-24');
        expect(exportDateText(undefined)).toBe('');
        expect(exportDateText('yesterday')).toBe('');
    });

    it('shows preset, size, date and the limiter on one line, leaving out what is not known', () => {
        expect(exportCopyDetail(copy)).toBe('MP3 V0 · 4.2 MB · 2026-11-24 · limited to −1 dBTP');
        expect(exportCopyDetail(cbr)).toBe('MP3 320');
    });

    it('labels the toggle with the count when a track has more than one copy', () => {
        expect(exportToggleLabel([v0])).toBe('mp3');
        expect(exportToggleLabel([v0, cbr])).toBe('mp3 ×2');
        expect(exportToggleLabel([])).toBe('');
        expect(exportToggleLabel(undefined)).toBe('');
    });

    it('lists each copy under its row with Show in folder and a confirmed Delete', () => {
        expect(player).toContain('{@render exportTree(row.entry)}');
        expect(player).toContain('>Show in folder</button>');
        expect(player).toContain('onclick={() => revealPath(copy.path)}');
        expect(player).toContain("title: 'Delete exported copy'");
        expect(player).toContain('deleteExportedCopy(copy.export_id)');
    });

    const linked: PlaylistCopy = { playlist_id: 'pl_1', playlist_name: 'Road trip', folder: 'Road trip', path: '/x/Road trip/01 - Band - Song.mp3', name: '01 - Band - Song.mp3', how: 'hardlink' };
    const copied: PlaylistCopy = { playlist_id: 'pl_2', playlist_name: 'Night', folder: 'Night', path: '/x/Night/03 - Band - Song.mp3', name: '03 - Band - Song.mp3', how: 'copy' };

    it('names the playlist folders that hold the same song, once each', () => {
        expect(playlistCopyText(linked)).toBe('Road trip / 01 - Band - Song.mp3 (hard link)');
        expect(playlistCopyText(copied)).toBe('Night / 03 - Band - Song.mp3 (copy)');
        expect(playlistCopiesLine([linked])).toBe('Also in playlist folder: Road trip');
        expect(playlistCopiesLine([linked, copied, { ...linked, name: '09 - again.mp3' }])).toBe('Also in playlist folders: Road trip, Night');
        expect(playlistCopiesLine([])).toBe('');
        expect(playlistCopiesLine(undefined)).toBe('');
    });

    it('a Delete confirm without playlist copies is the plain one', () => {
        const plain = deleteCopyConfirm('Band - Song.mp3', 'Song', []);
        expect(plain).toBe('Delete the exported file "Band - Song.mp3" from disk? This cannot be undone. "Song" stays in the library.');
        expect(deleteCopyConfirm('Band - Song.mp3', 'Song', undefined)).toBe(plain);
    });

    it('a Delete confirm says which playlist copies stay, and that a hard link keeps the audio', () => {
        const text = deleteCopyConfirm('Band - Song.mp3', 'Song', [linked, copied]);
        expect(text).toContain('these playlist folders');
        expect(text).toContain('Road trip / 01 - Band - Song.mp3 (hard link)');
        expect(text).toContain('Night / 03 - Band - Song.mp3 (copy)');
        expect(text).toContain('does not touch');
        expect(text).toContain('same file on disk');
        const onlyCopy = deleteCopyConfirm('Band - Song.mp3', 'Song', [copied]);
        expect(onlyCopy).toContain('this playlist folder');
        expect(onlyCopy).not.toContain('same file on disk');
    });

    it('a Delete confirm lists at most five playlist files and counts the rest', () => {
        const many = Array.from({ length: 8 }, (_, i) => ({ ...copied, folder: `P${i}`, name: `0${i}.mp3` }));
        const text = deleteCopyConfirm('a.mp3', 'S', many);
        expect(text).toContain('P4 / 04.mp3');
        expect(text).not.toContain('P5 / 05.mp3');
        expect(text).toContain('and 3 more');
    });

    it('the tree shows the playlist line under a copy and Delete uses the confirm text', () => {
        expect(player).toContain('playlistCopiesLine(copy.playlists)');
        expect(player).toContain('deleteCopyConfirm(copy.name, entry.title, copy.playlists)');
    });

    it('the core door sends the export id, and the library change event reaches both services', () => {
        expect(core).toContain("callMusic(sendCommand, 'delete_music_export', { export_id: exportId })");
        expect(core).toContain('message.event === "music_library_changed"');
        expect(core).toContain('noteMusicLibraryChanged(message.payload)');
        expect(core).toContain('noteMusicExportsChanged(message.payload)');
    });
});
