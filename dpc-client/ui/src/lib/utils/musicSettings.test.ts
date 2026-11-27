import { describe, it, expect } from 'vitest';
import {
    emptyMusicSettings,
    musicSettingsPayload,
    normaliseMusicSettings,
    numberOr,
    YUE2_BACKBONES,
    ENGINE_DEVICES,
} from './musicSettings';

describe('the option lists', () => {
    it('are the backend readers own sets — the backbones and the documented devices', () => {
        expect(YUE2_BACKBONES).toEqual(['stock', 'rs-tw']);
        // '' is the engine's own pick; the four spellings are what --device documents.
        expect(ENGINE_DEVICES).toEqual(['', 'CUDA0', 'Vulkan0', 'MTL0', 'CPU']);
    });
});

describe('numberOr', () => {
    it('takes a finite number of at least the minimum', () => {
        expect(numberOr(8500, 1, 9994)).toBe(8500);
        expect(numberOr('120', 0, 300)).toBe(120);
        expect(numberOr(0, 0, 300)).toBe(0);
    });
    it('answers the fallback for anything a number input must not hold', () => {
        expect(numberOr(0, 1, 9994)).toBe(9994);
        expect(numberOr(-5, 0, 300)).toBe(300);
        expect(numberOr('soon', 0, 300)).toBe(300);
        expect(numberOr(NaN, 0, 300)).toBe(300);
        expect(numberOr(undefined, 1, 2)).toBe(2);
    });
});

describe('normaliseMusicSettings', () => {
    it('takes the command answer as it is', () => {
        const s = normaliseMusicSettings({
            yue2_port: 8500, yue2_backbone: 'rs-tw', yue2_idle_stop_s: 120.5,
            agent_queue_max: 3, describe_alias: 'none', lrclib_enabled: false,
            export_dir: 'D:/exports', stream_ranges_linux: true, poll_interval_s: 30,
            yue2_instrumental_adapter: 'none',
        });
        expect(s.yue2_port).toBe(8500);
        expect(s.yue2_backbone).toBe('rs-tw');
        expect(s.yue2_idle_stop_s).toBe(120.5);
        expect(s.agent_queue_max).toBe(3);
        expect(s.describe_alias).toBe('none');
        expect(s.lrclib_enabled).toBe(false);
        expect(s.export_dir).toBe('D:/exports');
        expect(s.stream_ranges_linux).toBe(true);
        expect(s.poll_interval_s).toBe(30);
        expect(s.yue2_instrumental_adapter).toBe('none');
    });
    it('answers the defaults for whatever the command did not say', () => {
        expect(normaliseMusicSettings(null)).toEqual(emptyMusicSettings());
        expect(normaliseMusicSettings({})).toEqual(emptyMusicSettings());
        expect(normaliseMusicSettings({ status: 'error' })).toEqual(emptyMusicSettings());
    });
    it('keeps only the values a select may hold', () => {
        // A stored device or backbone the reader would silently replace must not sit in
        // a select whose options do not name it.
        const s = normaliseMusicSettings({ yue2_backbone: 'turbo', yue2_device: 'cuda', acestep_device: 'gpu' });
        expect(s.yue2_backbone).toBe('stock');
        expect(s.yue2_device).toBe('');
        expect(s.acestep_device).toBe('');
    });
    it('keeps free text as free text, trimmed', () => {
        const s = normaliseMusicSettings({ yue2_binary: '  C:/yue-server.exe  ', describe_alias: ' other ' });
        expect(s.yue2_binary).toBe('C:/yue-server.exe');
        expect(s.describe_alias).toBe('other');
    });
});

describe('musicSettingsPayload', () => {
    it('carries only the keys the draft changed — a key left alone keeps its stored value', () => {
        const loaded = emptyMusicSettings();
        const draft = { ...loaded, yue2_port: 8500, lrclib_enabled: false };
        expect(musicSettingsPayload(loaded, draft)).toEqual({ yue2_port: 8500, lrclib_enabled: false });
    });
    it('answers nothing when nothing changed', () => {
        const loaded = emptyMusicSettings();
        expect(musicSettingsPayload(loaded, { ...loaded })).toEqual({});
    });
    it('sees a change back to the loaded value as no change', () => {
        const loaded = { ...emptyMusicSettings(), yue2_port: 8500 };
        expect(musicSettingsPayload(loaded, { ...loaded, yue2_port: 8500 })).toEqual({});
    });
});
