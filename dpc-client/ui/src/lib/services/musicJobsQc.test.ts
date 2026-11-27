import { beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';

// `fetchJobQc` (follow-up 16, Zcode #188 finding 7): a failed `get_music_job_qc` is recorded per job so the
// review can say "QC info unavailable" and offer a retry; a good read clears it.

const h = vi.hoisted(() => ({ answers: [] as any[] }));

vi.mock('$lib/coreService', () => {
    const unavailable = async () => ({ ok: false, unavailable: true, message: '' });
    // Only the QC read is scripted; every other call the service imports is unavailable.
    return {
        backfillMusicQa: unavailable, cancelMusicJob: unavailable, discardMusicTake: unavailable, getMusicCapabilities: unavailable, getMusicEngineStatus: unavailable, getMusicJob: unavailable, judgeMusicTake: unavailable, keepMusicTake: unavailable, listMusicDiscardReasons: unavailable, listMusicJobs: unavailable, listMusicTakes: unavailable, measureMusicTakeLoudness: unavailable, proposeMusicEdit: unavailable, proposeMusicJob: unavailable, proposeMusicSong: unavailable, rejectAllMusicTakes: unavailable, startMusicEngine: unavailable, stopLocalEngine: unavailable, stopMusicEngine: unavailable,
        getMusicSettings: unavailable, saveMusicSettings: unavailable,
        getMusicJobQc: async () => { const a = h.answers.shift(); return typeof a === 'function' ? a() : a; },
    };
});

// There is no `$lib` alias under vitest: each `$lib` module the service imports is mocked to its real file.
vi.mock('$lib/utils/musicPlayer', () => vi.importActual('../utils/musicPlayer'));
vi.mock('$lib/utils/musicQc', () => vi.importActual('../utils/musicQc'));
vi.mock('$lib/utils/musicCreate', () => vi.importActual('../utils/musicCreate'));
vi.mock('$lib/utils/musicSettings', () => vi.importActual('../utils/musicSettings'));
vi.mock('$lib/services/music', () => ({ loadMusicLibrary: async () => {}, noteTracksHidden: () => {} }));

import { fetchJobQc, musicJobQc, musicJobQcError } from './musicJobs';

const good = { ok: true, data: { job_id: 'job_1', takes: [], ranking: { status: 'off' }, judge_alias: 'flamingo', order: [], caveat: 'c' } };

describe('fetchJobQc', () => {
    beforeEach(() => {
        h.answers.length = 0;
        musicJobQc.set({});
        musicJobQcError.set({});
    });
    it('records the reason of a failed read and keeps no QC view for the job', async () => {
        h.answers.push({ ok: false, message: 'timed out' });
        expect(await fetchJobQc('job_1')).toEqual({ ok: false, message: 'timed out' });
        expect(get(musicJobQcError)).toEqual({ job_1: 'timed out' });
        expect(get(musicJobQc).job_1).toBeUndefined();
    });
    it('a throw is a failed read too, and a later good read clears the reason', async () => {
        h.answers.push(() => { throw new Error('socket closed'); });
        expect(await fetchJobQc('job_1')).toEqual({ ok: false, message: 'socket closed' });
        expect(get(musicJobQcError).job_1).toBe('socket closed');
        h.answers.push(good);
        expect(await fetchJobQc('job_1')).toEqual({ ok: true });
        expect(get(musicJobQcError)).toEqual({});
        expect(get(musicJobQc).job_1.judge_alias).toBe('flamingo');
    });
    it('a failure keeps the view an earlier read gave, and names the reason', async () => {
        h.answers.push(good, { ok: false, message: '' });
        await fetchJobQc('job_1');
        const second = await fetchJobQc('job_1');
        expect(second.ok).toBe(false);
        expect(get(musicJobQc).job_1.judge_alias).toBe('flamingo');
        expect(get(musicJobQcError).job_1).toBe('the core did not answer');
    });
});
