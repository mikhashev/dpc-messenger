import { describe, it, expect } from 'vitest';
import {
    AB_END_GUARD_S,
    DEFAULT_ENGINE,
    DEFAULT_FROM_TEXT_LIMITS,
    ENGINE_DURATION_LIMITS,
    abKeyTarget,
    abSwitchPosition,
    cardHolders,
    cardWaitText,
    dbToGain,
    emptyFromTextForm,
    engineKeyOf,
    estimateYue2Seconds,
    formForEngine,
    shortCapWarning,
    yue2Adapters,
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
    LEVEL_MATCH_EPSILON_DB,
    askAgain,
    limitsFrom,
    mergeJobs,
    reasonCheck,
    reasonPayload,
    reasonSuggestions,
    reduceMusicEvent,
    takeDecidable,
    takeDecisionText,
    takeInfo,
    takeQaBadge,
    upsertJob,
    validateFromTextForm,
    instrumentalChoiceText,
    jobVoiceName,
    type MusicCapabilities,
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
        // the label is true of the gain applied: a difference under the threshold is not turned down
        expect(m.gain).toEqual([1, 1]);
        expect(m.db).toEqual([0, 0]);
    });

    it('the label threshold and the applied gain agree on both sides of it', () => {
        const under = levelMatch([loud(-14), loud(-14 + LEVEL_MATCH_EPSILON_DB * 0.9)]);
        expect(under.label).toBe('matched: equal loudness, no change');
        expect(under.gain).toEqual([1, 1]);
        const over = levelMatch([loud(-14), loud(-14 + LEVEL_MATCH_EPSILON_DB * 1.5)]);
        expect(over.label).toMatch(/^matched: −0\.1 dB on B$/);
        expect(over.gain[1]).toBeLessThan(1);
        // whatever the label says, "no change" only when every gain is exactly 1
        for (const gap of [0, 0.01, 0.049, 0.05, 0.051, 0.2, 1]) {
            const m = levelMatch([loud(-14), loud(-14 + gap)]);
            expect(m.label.includes('no change')).toBe(m.gain.every(g => g === 1));
        }
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
        if (v.ok) expect(v.params).toEqual({ lyrics, style: 'indie rock', variants: 2, duration_s: 180, seed: null,
            engine: 'yue2', backbone: 'stock' });
    });

    it('defaults are the core defaults: YuE2, stock backbone, no voice, 2 variants, a 180 s cap, no seed', () => {
        expect(DEFAULT_ENGINE).toBe('yue2');
        expect(emptyFromTextForm()).toMatchObject({ engine: 'yue2', backbone: 'stock', voice: '', variants: 2, duration: 180,
            seed: '', voiceMusic: 0.45, voiceSound: 0.35 });
        const ace = emptyFromTextForm(limitsFrom(null, 'acestep'), 'acestep');
        expect(ace).toMatchObject({ engine: 'acestep', duration: 120 });
        const v = validateFromTextForm({ ...ace, lyrics: 'x', style: 'y' }, limitsFrom(null, 'acestep'));
        expect(v.ok && v.params).toEqual({ lyrics: 'x', style: 'y', variants: 2, duration_s: 120, seed: null, engine: 'acestep' });
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

    it('duration: a YuE2 cap of 30-360 s, an ACE-Step length of 10-600 s', () => {
        for (const bad of [29.9, 361, '', null]) {
            const v = validateFromTextForm({ ...base, duration: bad as any });
            expect(!v.ok && v.errors.duration).toBe('Duration must be from 30 to 360 seconds.');
        }
        expect(validateFromTextForm({ ...base, duration: 30 }).ok).toBe(true);
        const ace = limitsFrom(null, 'acestep');
        for (const bad of [9.9, 601]) {
            const v = validateFromTextForm({ ...base, engine: 'acestep', duration: bad }, ace);
            expect(!v.ok && v.errors.duration).toBe('Duration must be from 10 to 600 seconds.');
        }
        const v = validateFromTextForm({ ...base, engine: 'acestep', duration: 10 }, ace);
        expect(v.ok && v.params.duration_s).toBe(10);
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

    it('the limits follow the engine, from the report or the core defaults', () => {
        expect(limitsFrom(null).duration_s).toEqual(ENGINE_DURATION_LIMITS.yue2);
        expect(limitsFrom(null, 'acestep').duration_s).toEqual({ min: 10, max: 600, default: 120 });
        const caps = { default_engine: 'yue2', limits: { generate_from_text: { engines: { yue2: { duration_s: { min: 30, max: 300, default: 150 } } } } } };
        expect(limitsFrom(caps, 'yue2').duration_s.max).toBe(300);
        expect(limitsFrom(caps, 'acestep').duration_s.max).toBe(600);
    });

    it('a voice adds one adapter with its music and sound strengths; none sends none', () => {
        expect(validateFromTextForm(base).ok && (validateFromTextForm(base) as any).params.adapters).toBeUndefined();
        const v = validateFromTextForm({ ...base, voice: 'voice-ru-m-bass-82' });
        expect(v.ok && v.params.adapters).toEqual([{ name: 'voice-ru-m-bass-82', music: 0.45, sound: 0.35 }]);
        const bad = validateFromTextForm({ ...base, voice: 'voice-ru-m-bass-82', voiceMusic: '3' });
        expect(!bad.ok && bad.errors.voice).toMatch(/0 to 2/);
        const ace = validateFromTextForm({ ...base, engine: 'acestep', voice: 'voice-ru-m-bass-82', duration: 60 }, limitsFrom(null, 'acestep'));
        expect(ace.ok && ace.params).not.toHaveProperty('adapters');
        expect(ace.ok && ace.params).not.toHaveProperty('backbone');
        expect(formFieldOf('adapters')).toBe('voice');
        expect(formFieldOf('backbone')).toBe('backbone');
    });

    it('switching the engine moves a default duration to the new default and keeps a typed one', () => {
        const f = { ...emptyFromTextForm(), lyrics: 'x', style: 'y' };
        expect(formForEngine(f, 'acestep')).toMatchObject({ engine: 'acestep', duration: 120 });
        expect(formForEngine({ ...f, duration: 200 }, 'acestep')).toMatchObject({ engine: 'acestep', duration: 200 });
        expect(formForEngine(formForEngine(f, 'acestep'), 'yue2')).toMatchObject({ engine: 'yue2', duration: 180 });
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

describe('the YuE2 short-cap warning is an estimate from the sung lines', () => {
    const lyrics = '[verse]\none\ntwo\n\n[chorus]\nthree\nfour\nfive\nsix\n';

    it('about 10 s per sung line plus a 20 s intro; tags and blank lines do not count', () => {
        expect(estimateYue2Seconds(lyrics)).toBe(80);
        expect(estimateYue2Seconds('')).toBe(20);
        expect(estimateYue2Seconds('a\r\nb', 12, 15)).toBe(39);
    });

    it('warns when the cap is under the estimate, and says it is an estimate', () => {
        const w = shortCapWarning({ engine: 'yue2', lyrics, instrumental: false, duration: 30 });
        expect(w).toMatch(/estimate: about 80 s/);
        expect(w).toMatch(/30 s looks short/);
        expect(shortCapWarning({ engine: 'yue2', lyrics, instrumental: false, duration: 180 })).toBe('');
        expect(shortCapWarning({ engine: 'acestep', lyrics, instrumental: false, duration: 30 })).toBe('');
        expect(shortCapWarning({ engine: 'yue2', lyrics, instrumental: true, duration: 30 })).toBe('');
        expect(shortCapWarning({ engine: 'yue2', lyrics, instrumental: false, duration: '' })).toBe('');
    });

    it('the report can set the estimate', () => {
        const caps = { limits: { generate_from_text: { engines: { yue2: { short_cap_estimate: { seconds_per_line: 5, intro_s: 10 } } } } } };
        expect(shortCapWarning({ engine: 'yue2', lyrics, instrumental: false, duration: 45 }, caps)).toBe('');
        expect(shortCapWarning({ engine: 'yue2', lyrics, instrumental: false, duration: 30 }, caps)).toMatch(/about 40 s/);
    });
});

describe('two engines in the report and the events', () => {
    it('an engine status is keyed by key, else by id', () => {
        expect(engineKeyOf({ key: 'yue2' })).toBe('yue2');
        expect(engineKeyOf({ id: 'acestep.cpp' })).toBe('acestep');
        expect(engineKeyOf({ id: 'other' })).toBeNull();
    });

    it('engine events keep each engine apart', () => {
        let s = reduceMusicEvent({ jobs: [], engine: null }, 'music_engine_status', { engine: { key: 'yue2', state: 'running', reason: '' } });
        s = reduceMusicEvent(s, 'music_engine_status', { engine: { id: 'acestep.cpp', state: 'stopped', reason: 'swap' } });
        expect(s.engines?.yue2?.state).toBe('running');
        expect(s.engines?.acestep?.state).toBe('stopped');
    });

    it('Generate follows the selected engine, and the voice list puts voices first', () => {
        const caps = {
            job_types: { generate_from_text: { status: 'supported', reason: '' } },
            engines: {
                yue2: { state: 'not_installed', available: false, reason: 'no yue-server', adapters: [
                    { name: 'style-x', kind: 'other' }, { name: 'voice-b', kind: 'voice' }, { name: 'voice-a', kind: 'voice' }] },
                acestep: { state: 'stopped', available: true, reason: 'installed' },
            },
        };
        expect(fromTextSupport(caps, 'yue2')).toEqual({ supported: false, reason: 'no yue-server' });
        expect(fromTextSupport(caps, 'acestep').supported).toBe(true);
        expect(yue2Adapters(caps).map(a => a.name)).toEqual(['voice-a', 'voice-b', 'style-x']);
    });
});

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
        // ADR-052 S4: a running job whose engine stopped under it waits for the card again, named the same way.
        const runningWait = job({ state: 'running', progress: 0.05, state_reason: 'waiting for the card: whisper:turbo holds it', card_wait: { holders: [{ alias: 'whisper:turbo', engine: 'whisper' }], since: 'x' } });
        expect(cardHolders(runningWait).map(h => h.alias)).toEqual(['whisper:turbo']);
        expect(jobStateText(runningWait)).toBe('Waiting for the GPU: whisper:turbo holds it');
        expect(cardHolders({ ...runningWait, card_wait: null })).toEqual([]);
        expect(jobStateText({ ...runningWait, card_wait: null })).toBe('Running · 5%');
        expect(cardHolders({ ...waiting, state: 'review' })).toEqual([]);
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

    it('a start by hand that waits for the card shows what is letting go and an upper bound', () => {
        const wait = {
            text: 'yue2.cpp is waiting for the card: Whisper (turbo) is letting go, at most about 30 s',
            behind: ['Whisper (turbo)'], wait_s: 30, known: true, upper_bound: true,
        };
        // Still `stopped` while it waits: the wait row replaces the "DPC starts it when a job runs" line.
        expect(engineStatusText({ state: 'stopped', reason: '', card_wait: wait })).toBe(wait.text);
        expect(engineStatusText({ state: 'error', reason: 'x', card_wait: wait })).toBe(wait.text);
        expect(engineStatusText({ state: 'stopped', reason: '', card_wait: null })).toBe('Engine stopped (DPC starts it when a job runs)');
        expect(engineStatusText({ state: 'starting', reason: '', card_wait: wait })).toBe('Engine starting…');
        expect(engineCanStart({ state: 'stopped', reason: '', card_wait: wait })).toBe(true);

        // The event that ends the wait carries `card_wait: null`, and the merge clears the row.
        const waiting = reduceMusicEvent({ jobs: [], engine: null, engines: {} } as any, 'music_engine_status',
            { engine: { key: 'yue2', state: 'stopped', reason: 'r', card_wait: wait } });
        expect(engineStatusText(waiting.engine)).toBe(wait.text);
        const done = reduceMusicEvent(waiting, 'music_engine_status',
            { engine: { key: 'yue2', state: 'starting', reason: 'r', card_wait: null } });
        expect(engineStatusText(done.engine)).toBe('Engine starting…');
        expect(done.engine?.card_wait).toBeNull();
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
        expect(takeInfo({ ...take, engine: 'yue2.cpp', seed: 43, lm_seed: 43, synth_batch_size: 1, synth_batch_index: 0 }))
            .toBe('yue2.cpp · seed 43 · variant 1 · batch 1, index 0 · 0:30 · −14.0 LUFS / −1.0 dBTP');
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

describe('an Instrumental job on YuE2 says which engine and adapter it will use', () => {
    const INST = 'YuE2-instrumental-cot-full-loras';
    const withAdapter: MusicCapabilities = {
        engine_choice_rules: { instrumental: { rule: 'instrumental', engine: 'yue2', adapters: [INST], fallback: 'acestep',
            reason: `Instrumental: YuE2 with the instrumental adapter ${INST}; acestep.cpp if the adapter does not load` } },
        engines: { yue2: { state: 'stopped', reason: 'installed', adapters: [
            { name: INST, kind: 'instrumental', layout: 'whole' }, { name: 'voice-a', kind: 'voice' }] } },
    };
    const withoutAdapter: MusicCapabilities = {
        engine_choice_rules: { instrumental: { rule: 'instrumental', engine: 'acestep', adapters: [], fallback: null,
            reason: `Instrumental: acestep.cpp, because the YuE2 instrumental adapter ${INST} is not installed` } },
    };

    it('names the adapter and the fallback when the adapter is installed', () => {
        const text = instrumentalChoiceText({ engine: 'yue2', instrumental: true }, withAdapter);
        expect(text).toContain(INST);
        expect(text).toContain('acestep.cpp makes it instead');
    });

    it('says the job goes to acestep.cpp, and why, when the adapter is missing', () => {
        expect(instrumentalChoiceText({ engine: 'yue2', instrumental: true }, withoutAdapter))
            .toBe(`Runs on acestep.cpp instead of YuE2: the YuE2 instrumental adapter ${INST} is not installed.`);
    });

    it('says nothing for a song, for acestep, or when the core names no rule', () => {
        expect(instrumentalChoiceText({ engine: 'yue2', instrumental: false }, withAdapter)).toBe('');
        expect(instrumentalChoiceText({ engine: 'acestep', instrumental: true }, withAdapter)).toBe('');
        expect(instrumentalChoiceText({ engine: 'yue2', instrumental: true }, {})).toBe('');
    });

    it('keeps the instrumental adapter out of the Voice select and out of the job row voice', () => {
        expect(yue2Adapters(withAdapter).map(a => a.name)).toEqual(['voice-a']);
        const j = job({ params: { engine: 'yue2', adapters: [{ name: INST }, { name: 'voice-a' }] },
            engine_choice: { rule: 'instrumental', engine: 'yue2', adapters: [INST], fallback: 'acestep', reason: 'x' } });
        expect(jobVoiceName(j)).toBe('voice-a');
        expect(jobVoiceName(job({ params: { adapters: [{ name: INST }] },
            engine_choice: { rule: 'instrumental', engine: 'yue2', adapters: [INST], fallback: 'acestep', reason: 'x' } }))).toBeNull();
    });
});

describe('the QA badge beside a take', () => {
    const gate = (status: string, detail = `${status} detail`) => ({ status, detail });
    const report = (gates: Record<string, { status: string; detail: string }>, verdict: string, note?: string) => ({
        stage: 1, verdict, gates, note: note ?? null,
    });
    const base: MusicTake = { id: 'tak_1', job_id: 'job_a', variant: 1, title: 't', decision: 'pending', file: { uri: 'file:///x.flac', content_hash: null } };
    const allPass = { length: gate('pass'), clipping: gate('pass'), true_peak: gate('pass'), silence: gate('pass') };

    it('says passed only when every gate passed', () => {
        const b = takeQaBadge({ ...base, qa_report: report(allPass, 'passed') });
        expect(b).toMatchObject({ kind: 'passed', label: 'QA passed' });
        expect(b.details).toHaveLength(4);
    });

    it('names the failed gates, in the gates order, with their details', () => {
        const b = takeQaBadge({ ...base, qa_report: report({ ...allPass, silence: gate('fail', '4.0 s of silence at the start'), clipping: gate('fail', '12 sample(s)') }, 'failed') });
        expect(b.kind).toBe('failed');
        expect(b.label).toBe('QA failed: clipping, silence');
        expect(b.details).toEqual(['clipping: 12 sample(s)', 'silence: 4.0 s of silence at the start']);
    });

    it('shows a failed gate even when another gate was not measured', () => {
        const b = takeQaBadge({ ...base, qa_report: report({ ...allPass, length: gate('fail', 'too short'), true_peak: gate('not_measured', 'no peak') }, 'failed') });
        expect(b.label).toBe('QA failed: length');
        expect(b.details).toEqual(['length: too short', 'true peak: no peak']);
    });

    it('never shows a take as passed when a gate was not measured or there is no report', () => {
        const unmeasured = takeQaBadge({ ...base, qa_report: report({ ...allPass, clipping: gate('not_measured', 'astats printed no peak level') }, 'not_measured') });
        expect(unmeasured).toMatchObject({ kind: 'unknown', label: 'QA not measured' });
        expect(unmeasured.details).toEqual(['clipping: astats printed no peak level']);
        const none = takeQaBadge(base);
        expect(none).toMatchObject({ kind: 'unknown', label: 'QA not measured' });
        expect(takeQaBadge({ ...base, qa_report: null }).kind).toBe('unknown');
        // a report whose verdict is not "passed" is not a pass, whatever its gates say
        expect(takeQaBadge({ ...base, qa_report: report(allPass, 'not_measured') }).kind).toBe('unknown');
    });
});


// -- a failed measurement is asked again once --------------------------------------------

describe('askAgain', () => {
    const ok = { ok: true as const, data: 1 };
    const bad = { ok: false as const, unavailable: false, code: 'measure_failed', message: 'busy' };
    const noSleep = async () => {};

    it('does not ask twice when the first answer is ok', async () => {
        let calls = 0;
        const a = await askAgain(async () => { calls++; return ok; }, 10, noSleep);
        expect(a).toBe(ok);
        expect(calls).toBe(1);
    });

    it('asks once more after an error answer, waits the delay, and returns the second answer', async () => {
        const answers = [bad, ok];
        const waits: number[] = [];
        const a = await askAgain(async () => answers.shift()!, 2000, async ms => { waits.push(ms); });
        expect(a).toBe(ok);
        expect(waits).toEqual([2000]);
    });

    it('asks once more after a throw, and stops after the second failure', async () => {
        let calls = 0;
        const a = await askAgain(async () => { calls++; if (calls === 1) throw new Error('boom'); return bad; }, 5, noSleep);
        expect(a).toBe(bad);
        expect(calls).toBe(2);
        let again = 0;
        const b = await askAgain(async () => { again++; return bad; }, 5, noSleep);
        expect(b).toBe(bad);
        expect(again).toBe(2);
    });

    it('does not retry a call already in flight (null) or an unavailable core', async () => {
        let calls = 0;
        expect(await askAgain(async () => { calls++; return null; }, 5, noSleep)).toBeNull();
        const gone = { ok: false as const, unavailable: true, code: 'not_connected', message: 'x' };
        expect(await askAgain(async () => { calls++; return gone; }, 5, noSleep)).toBe(gone);
        expect(calls).toBe(2);
    });
});
