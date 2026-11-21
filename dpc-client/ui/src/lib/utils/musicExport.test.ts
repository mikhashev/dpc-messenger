import { describe, it, expect } from 'vitest';
import {
    DEFAULT_EXPORT_PRESET,
    EXPORT_PRESETS,
    exportChip,
    exportFolderWarning,
    fileNameOf,
    limiterNote,
    playlistExportLine,
    presetShortLabel,
    trackExportLine,
    type MusicExportInfo,
    type MusicExportRecord,
} from './musicExport';
import player from '../components/music/MusicPlayer.svelte?raw';
import core from '../coreService.ts?raw';

const v0: MusicExportRecord = { preset: 'mp3-v0', format: 'mp3', path: 'C:\\Users\\m\\.dpc\\exports\\music\\Band - Song.mp3', name: 'Band - Song.mp3', limited_db: -1 };
const cbr: MusicExportRecord = { preset: 'mp3-320', format: 'mp3', path: '/home/m/.dpc/exports/music/Band - Song (320 kbps).mp3', name: 'Band - Song (320 kbps).mp3', limited_db: null };

describe('names and presets', () => {
    it('takes the last part of a Windows or POSIX path', () => {
        expect(fileNameOf(v0.path)).toBe('Band - Song.mp3');
        expect(fileNameOf(cbr.path)).toBe('Band - Song (320 kbps).mp3');
        expect(fileNameOf('/music/Mix/')).toBe('Mix');
        expect(fileNameOf(null)).toBe('');
    });

    it('defaults to V0 and offers 320 too', () => {
        expect(DEFAULT_EXPORT_PRESET).toBe('mp3-v0');
        expect(EXPORT_PRESETS.map(p => p.id)).toEqual(['mp3-v0', 'mp3-320']);
        expect(presetShortLabel('mp3-v0')).toBe('MP3 V0');
        expect(presetShortLabel('mp3-320')).toBe('MP3 320');
        expect(presetShortLabel('opus-128')).toBe('opus-128');
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
        expect(player).toContain('>Export MP3…</button>');
        expect(player).toContain('onclick={() => cur && exportTrackMp3(cur.id, cur.title)}');
        expect(player).toContain('>Export playlist as MP3</button>');
    });

    it('reveals the file through the existing helper and shows the chip beside the origin badge', () => {
        expect(player).toContain('await revealInFolder(path, revealItemInDir)');
        expect(player).toContain('{@render originBadge(row.entry)}{@render exportChipTag(row.entry)}');
        expect(player).toContain('{@render exportChipTag(cur)}');
    });

    it('the core door sends ids only and the export done event reaches the store', () => {
        expect(core).toContain("callMusic(sendCommand, 'export_music_track', preset ? { track_id: trackId, preset } : { track_id: trackId })");
        expect(core).toContain("callMusic(sendCommand, 'export_music_playlist'");
        expect(core).toContain('message.event === "music_export_done"');
        expect(core).toContain("command === 'export_music_track'");
    });
});
