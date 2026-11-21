import { describe, it, expect } from 'vitest';
import {
    AB_END_GUARD_S,
    DEFAULT_FROM_TEXT_LIMITS,
    abKeyTarget,
    abSwitchPosition,
    cardHolders,
    cardWaitText,
    dbToGain,
    emptyFromTextForm,
    engineCanStart,
    engineCanStop,
    engineStatusText,
    formFieldOf,
    formatDb,
    fromTextSupport,
    jobIsNewer,
    jobStateText,
    jobsToPoll,
    levelMatch,
    limitsFrom,
    mergeJobs,
    reasonCheck,
    reasonPayload,
    reasonSuggestions,
    reduceMusicEvent,
    takeDecidable,
    takeDecisionText,
    takeInfo,
    upsertJob,
    validateFromTextForm,
    type MusicJob,
    type MusicTake,
    type TakeLoudness,
} from './musicCreate';
import { callMusic } from './musicPlayer';

function job(over: Partial<MusicJob> = {}): MusicJob {
    return {
        id: 'job_0000000000000001', type: 'generate_from_text', state: 'queued', params: { style: 'indie rock', variants: 2, duration_s: 30 },
        created_at: '2026-11-21T10:00:00.000Z', updated_at: '2026-11-21T10:00:00.000Z',
        history: [{ state: 'queued', at: '2026-11-21T10:00:00.000Z', reason: null }], ...over,
    };
}

function loud(lufs: number): TakeLoudness {
    return { lufs_i: lufs, lra: 3, true_peak_dbtp: -1, measured_by: 'ffmpeg ebur128', measured_at: '2026-11-21T10:00:00Z' };
}

const RUNNING_HISTORY = [
    { state: 'queued', at: '2026-11-21T10:00:00.000Z', reason: null },
    { state: 'running', at: '2026-11-21T10:00:05.000Z', reason: null },
];

// -- level matching ----------------------------------------------------------------

describe('level matching turns the louder take down to the quieter one', () => {
    it('A louder by 1.5 LU: A gets -1.5 dB, B stays at 0 dB', () => {
        const m = levelMatch([loud(-12.5), loud(-14)]);
        expect(m.matched).toBe(true);
        expect(m.db[0]).toBeCloseTo(-1.5, 6);
        expect(m.db[1]).toBe(0);
        expect(m.gain[0]).toBeCloseTo(Math.pow(10, -1.5 / 20), 9);
        expect(m.gain[1]).toBe(1);
        expect(m.label).toBe('matched: −1.5 dB on A');
    });

    it('B louder: the gain goes on B, never above 1', () => {
        const m = levelMatch([loud(-16.2), loud(-9.0)]);
        expect(m.label).toBe('matched: −7.2 dB on B');
        expect(m.gain.every(g => g <= 1)).toBe(true);
        expect(m.gain[0]).toBe(1);
    });

    it('equal loudness changes nothing and says so', () => {
        const m = levelMatch([loud(-14), loud(-14.02)]);
        expect(m.matched).toBe(true);
        expect(m.label).toBe('matched: equal loudness, no change');
        expect(m.gain[0]).toBeCloseTo(dbToGain(-0.02), 9);
    });

    it('more takes all meet the quietest', () => {
        const m = levelMatch([loud(-10), loud(-14), loud(-12)]);
        expect(m.db.map(d => Number(d.toFixed(2)))).toEqual([-4, 0, -2]);
        expect(m.label).toBe('matched: −4.0 dB on A, −2.0 dB on C');
    });

    it('a missing measurement leaves every gain at 1 and says "not level-matched"', () => {
        const m = levelMatch([loud(-12), null]);
        expect(m.matched).toBe(false);
        expect(m.gain).toEqual([1, 1]);
        expect(m.label).toBe('not level-matched: the loudness of B is not measured yet');
    });

    it('a take at the ebur128 floor is not matched against', () => {
        const m = levelMatch([loud(-70), loud(-12)]);
        expect(m.matched).toBe(false);
        expect(m.gain).toEqual([1, 1]);
        expect(m.label).toContain('A is silent or too short');
    });

    it('formats dB with a real minus sign', () => {
        expect(formatDb(-1.54)).toBe('−1.5');
        expect(formatDb(0.01)).toBe('0.0');
        expect(formatDb(2)).toBe('+2.0');
    });
});

// -- A/B position ----------------------------------------------------------------

describe('the A/B switch keeps the position, clamped to the other take', () => {
    it('the same time when the other take is long enough', () => {
        expect(abSwitchPosition(15, 30)).toBe(15);
    });

    it('a take shorter by a frame: clamped before its end, never onto `ended`', () => {
        expect(abSwitchPosition(29.999, 29.98)).toBeCloseTo(29.98 - AB_END_GUARD_S, 9);
        expect(abSwitchPosition(40, 30)).toBeCloseTo(30 - AB_END_GUARD_S, 9);
    });

    it('unknown length keeps the time; a bad time starts at 0', () => {
        expect(abSwitchPosition(12, null)).toBe(12);
        expect(abSwitchPosition(12, 0)).toBe(12);
        expect(abSwitchPosition(NaN, 30)).toBe(0);
        expect(abSwitchPosition(-3, 30)).toBe(0);
    });

    it('keys pick a take by letter; the letter playing flips back (B toggles A and B)', () => {
        expect(abKeyTarget('b', 2, 0, null)).toBe(1);
        expect(abKeyTarget('B', 2, 1, 0)).toBe(0);
        expect(abKeyTarget('b', 2, 1, null)).toBe(0);
        expect(abKeyTarget('a', 2, 0, 1)).toBe(1);
        expect(abKeyTarget('c', 2, 0, null)).toBeNull();
        expect(abKeyTarget('c', 3, 0, null)).toBe(2);
        expect(abKeyTarget('c', 3, 2, null)).toBeNull();
        expect(abKeyTarget('Enter', 2, 0, null)).toBeNull();
    });
});

// -- the form ----------------------------------------------------------------------

describe('the form mirrors the core limits', () => {
    const base = { ...emptyFromTextForm(), lyrics: '[verse]\nline one\n', style: 'indie rock' };

    it('a good form gives the params with the lyrics verbatim and a null seed', () => {
        const lyrics = '[verse]\nТы́сячи умов\n\n[chorus]\n  spaced  \n';
        const v = validateFromTextForm({ ...base, lyrics });
        expect(v.ok).toBe(true);
        if (v.ok) expect(v.params).toEqual({ lyrics, style: 'indie rock', variants: 2, duration_s: 120, seed: null });
    });

    it('defaults are the core defaults: 2 variants, 120 s, no seed', () => {
        expect(emptyFromTextForm()).toMatchObject({ variants: 2, duration: 120, seed: '' });
    });

    it('instrumental sends null lyrics; empty lyrics without it is refused', () => {
        const v = validateFromTextForm({ ...base, lyrics: '', instrumental: true });
        expect(v.ok && v.params.lyrics).toBeNull();
        const bad = validateFromTextForm({ ...base, lyrics: '  \n ' });
        expect(!bad.ok && bad.errors.lyrics).toMatch(/empty/);
    });

    it('style is required', () => {
        const v = validateFromTextForm({ ...base, style: '   ' });
        expect(!v.ok && v.errors.style).toMatch(/required/);
    });

    it('variants 1-4 whole numbers', () => {
        for (const bad of [0, 5, 2.5, '', null, 'two']) {
            const v = validateFromTextForm({ ...base, variants: bad as any });
            expect(!v.ok && v.errors.variants).toBe('Variants must be a whole number from 1 to 4.');
        }
        expect(validateFromTextForm({ ...base, variants: '4' }).ok).toBe(true);
    });

    it('duration 10-600 s', () => {
        for (const bad of [9.9, 601, '', null]) {
            const v = validateFromTextForm({ ...base, duration: bad as any });
            expect(!v.ok && v.errors.duration).toBe('Duration must be from 10 to 600 seconds.');
        }
        const v = validateFromTextForm({ ...base, duration: 30 });
        expect(v.ok && v.params.duration_s).toBe(30);
    });

    it('seed 0..2^32-1 or empty', () => {
        expect(validateFromTextForm({ ...base, seed: '42' }).ok && true).toBe(true);
        const max = validateFromTextForm({ ...base, seed: String(2 ** 32 - 1) });
        expect(max.ok && max.params.seed).toBe(2 ** 32 - 1);
        for (const bad of ['-1', '1.5', String(2 ** 32), 'abc']) {
            const v = validateFromTextForm({ ...base, seed: bad });
            expect(!v.ok && v.errors.seed).toMatch(/^Seed must be a whole number from 0 to 4294967295/);
        }
    });

    it('every bad field is named at once', () => {
        const v = validateFromTextForm({ lyrics: '', instrumental: false, style: '', variants: 9, duration: 1, seed: 'x' });
        expect(!v.ok && Object.keys(v.errors).sort()).toEqual(['duration', 'lyrics', 'seed', 'style', 'variants']);
    });

    it('the capability report can narrow the limits', () => {
        const limits = limitsFrom({ limits: { generate_from_text: { variants: { min: 1, max: 2, default: 1 } } } });
        expect(limits.variants.max).toBe(2);
        expect(limits.duration_s).toEqual(DEFAULT_FROM_TEXT_LIMITS.duration_s);
        const v = validateFromTextForm({ ...base, variants: 3 }, limits);
        expect(!v.ok && v.errors.variants).toBe('Variants must be a whole number from 1 to 2.');
    });

    it('a core refusal lands on the field it names', () => {
        expect(formFieldOf('duration_s')).toBe('duration');
        expect(formFieldOf('lyrics')).toBe('lyrics');
        expect(formFieldOf('lyrics_prep')).toBe('form');
        expect(formFieldOf(undefined)).toBe('form');
    });

    it('callMusic keeps the core field of an error', async () => {
        const a = await callMusic(() => Promise.resolve({ status: 'error', code: 'invalid_params', message: 'bad', field: 'seed' }), 'propose_music_job');
        expect(a).toEqual({ ok: false, unavailable: false, code: 'invalid_params', message: 'bad', field: 'seed' });
        const b = await callMusic(() => Promise.resolve({ status: 'error', code: 'x', message: 'y' }), 'c');
        expect('field' in b).toBe(false);
    });
});

// -- the reason box ---------------------------------------------------------------

describe('a Discard reason is text, a class, or both, never neither', () => {
    it('refuses empty and blank', () => {
        expect(reasonCheck('', null).ok).toBe(false);
        expect(reasonCheck('   ', '').ok).toBe(false);
        expect(reasonCheck('', null).message).toMatch(/type a reason, pick a class, or both/);
    });

    it('takes text alone, class alone, or both', () => {
        expect(reasonCheck('muddy mix', null).ok).toBe(true);
        expect(reasonCheck('', 'struct').ok).toBe(true);
        expect(reasonCheck('cut-off ending', 'struct').ok).toBe(true);
    });

    it('refuses text past the core limit', () => {
        expect(reasonCheck('x'.repeat(2001), null).ok).toBe(false);
        expect(reasonCheck('x'.repeat(2000), null).ok).toBe(true);
    });

    it('sends only what was given, the text trimmed', () => {
        expect(reasonPayload('  muddy  ', null)).toEqual({ reason_text: 'muddy' });
        expect(reasonPayload('', 'sel')).toEqual({ reason_class: 'sel' });
        expect(reasonPayload('a', 'opt')).toEqual({ reason_text: 'a', reason_class: 'opt' });
    });

    it('suggestions drop the entry already typed in full', () => {
        const reasons = [
            { text: 'Muddy mix', key: 'muddy mix', uses: 3 },
            { text: 'muddy vocals', key: 'muddy vocals', uses: 1 },
        ];
        expect(reasonSuggestions(reasons, 'muddy  MIX').map(r => r.key)).toEqual(['muddy vocals']);
        expect(reasonSuggestions(reasons, 'mu')).toHaveLength(2);
    });
});

// -- the event -> job reducer -------------------------------------------------------

describe('events and answers merge into the newest job', () => {
    it('an event adds an unknown job and updates a known one', () => {
        let s = reduceMusicEvent({ jobs: [], engine: null }, 'music_job_updated', { job: job() });
        expect(s.jobs.map(j => j.state)).toEqual(['queued']);
        s = reduceMusicEvent(s, 'music_job_updated', { job: job({ state: 'running', history: RUNNING_HISTORY, progress: 0.4, updated_at: '2026-11-21T10:00:09.000Z' }) });
        expect(s.jobs).toHaveLength(1);
        expect(jobStateText(s.jobs[0])).toBe('Running · 40%');
    });

    it('a late, older copy never overwrites a newer one', () => {
        const running = job({ state: 'running', history: RUNNING_HISTORY, updated_at: '2026-11-21T10:00:05.000Z' });
        const s = reduceMusicEvent({ jobs: [running], engine: null }, 'music_job_updated', { job: job() });
        expect(s.jobs[0].state).toBe('running');
        expect(jobIsNewer(job(), running)).toBe(false);
    });

    it('progress within one state: the later updated_at wins', () => {
        const a = job({ state: 'running', history: RUNNING_HISTORY, progress: 0.4, updated_at: '2026-11-21T10:00:09.000Z' });
        const b = job({ state: 'running', history: RUNNING_HISTORY, progress: 0.1, updated_at: '2026-11-21T10:00:06.000Z' });
        expect(upsertJob([a], b)[0].progress).toBe(0.4);
        expect(upsertJob([b], a)[0].progress).toBe(0.4);
    });

    it('the list is newest first, and a full read merges with what events brought', () => {
        const older = job({ id: 'job_0000000000000001', created_at: '2026-11-21T09:00:00.000Z' });
        const newer = job({ id: 'job_0000000000000002', created_at: '2026-11-21T11:00:00.000Z' });
        expect(mergeJobs([older], [newer]).map(j => j.id)).toEqual([newer.id, older.id]);
    });

    it('engine events replace the engine state; junk changes nothing', () => {
        const s0 = { jobs: [], engine: null };
        const s1 = reduceMusicEvent(s0, 'music_engine_status', { engine: { state: 'starting', reason: 'starting on 127.0.0.1' } });
        expect(s1.engine?.state).toBe('starting');
        expect(reduceMusicEvent(s1, 'music_engine_status', {})).toBe(s1);
        expect(reduceMusicEvent(s1, 'music_job_updated', { job: { id: 1 } })).toBe(s1);
        expect(reduceMusicEvent(s1, 'something_else', {})).toBe(s1);
    });

    it('only active jobs quiet for long enough are polled', () => {
        const jobs = [job({ id: 'job_a' }), job({ id: 'job_b', state: 'running' }), job({ id: 'job_c', state: 'review' })];
        expect(jobsToPoll(jobs, { job_a: 1000, job_b: 9000 }, 10000, 5000)).toEqual(['job_a']);
        expect(jobsToPoll(jobs, {}, 10000, 5000)).toEqual(['job_a', 'job_b']);
    });
});

// -- states, the card, the engine --------------------------------------------------

describe('what the jobs list and the engine line say', () => {
    it('a job waiting for the card names its holder', () => {
        const waiting = job({ state_reason: 'waiting for the card: strata holds it', card_wait: { holders: [{ alias: 'strata', engine: 'strata' }], since: 'x' } });
        expect(cardHolders(waiting).map(h => h.alias)).toEqual(['strata']);
        expect(jobStateText(waiting)).toBe('Waiting for the GPU: strata holds it');
        expect(cardWaitText([{ alias: 'a', engine: null }, { alias: 'b', engine: null }])).toBe('Waiting for the GPU: a, b hold it');
        expect(cardHolders({ ...waiting, state: 'running' })).toEqual([]);
    });

    it('failures and unsupported carry their reason', () => {
        expect(jobStateText(job({ state: 'failed', state_reason: 'ffmpeg is not on PATH' }))).toBe('Failed: ffmpeg is not on PATH');
        expect(jobStateText(job({ state: 'unsupported', state_reason: 'binary missing' }))).toBe('Unsupported: binary missing');
        expect(jobStateText(job({ state: 'accepted' }))).toBe('Kept');
    });

    it('the engine line, and when Start / Stop apply', () => {
        expect(engineStatusText({ state: 'not_installed', reason: 'model file(s) missing' })).toBe('Engine not installed: model file(s) missing');
        expect(engineStatusText({ state: 'error', reason: 'exited with 1' })).toBe('Engine error: exited with 1');
        expect(engineStatusText({ state: 'running', reason: '' })).toBe('Engine running');
        expect(engineStatusText(null)).toBe('Engine status unknown');
        expect(engineCanStart({ state: 'stopped', reason: '' })).toBe(true);
        expect(engineCanStart({ state: 'error', reason: '' })).toBe(true);
        expect(engineCanStart({ state: 'not_installed', reason: '' })).toBe(false);
        expect(engineCanStop({ state: 'running', reason: '' })).toBe(true);
        expect(engineCanStop({ state: 'stopped', reason: '' })).toBe(false);
    });

    it('Generate follows the capability report', () => {
        expect(fromTextSupport(null).supported).toBe(false);
        expect(fromTextSupport({ job_types: { generate_from_text: { status: 'unsupported', reason: 'binary missing' } } }))
            .toEqual({ supported: false, reason: 'binary missing' });
        expect(fromTextSupport({ job_types: { generate_from_text: { status: 'supported', reason: 'ok' } } }).supported).toBe(true);
    });
});

describe('take info and decisions', () => {
    const take: MusicTake = {
        id: 'tak_1', job_id: 'job_a', variant: 1, title: 'indie rock (v1)', decision: 'pending', seed: 42,
        synth_batch_size: 2, synth_batch_index: 0, file: { uri: 'file:///x.flac', content_hash: null, duration_s: 30.02 },
        loudness: loud(-14),
    };

    it('shows seed, variant, batch, duration and LUFS / TP', () => {
        expect(takeInfo(take)).toBe('seed 42 · variant 1 · batch 2, index 0 · 0:30 · −14.0 LUFS / −1.0 dBTP');
        expect(takeInfo({ ...take, loudness: null })).toContain('loudness not measured');
    });

    it('says the decision and the reason', () => {
        expect(takeDecisionText(take)).toBe('');
        expect(takeDecisionText({ ...take, decision: 'accepted' })).toBe('Kept');
        expect(takeDecisionText({ ...take, decision: 'rejected', reason_text: 'muddy', reason_class: 'struct' })).toBe('Discarded: muddy (struct)');
    });

    it('a pending take is decidable in review and after a Keep, never before review', () => {
        expect(takeDecidable({ state: 'review' }, take)).toBe(true);
        expect(takeDecidable({ state: 'accepted' }, take)).toBe(true);
        expect(takeDecidable({ state: 'running' }, take)).toBe(false);
        expect(takeDecidable({ state: 'review' }, { decision: 'rejected' })).toBe(false);
    });
});
