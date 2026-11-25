import { describe, expect, it } from 'vitest';
import {
    MISSING_LYRICS,
    RUACH_ATTRIBUTION,
    captionFromKeep,
    defaultCoverEngine,
    emptySimilarForm,
    keptValues,
    offersAcestepCover,
    similarFieldOf,
    songSupport,
    sourceAudioProblem,
    useAcestepCover,
    validateSimilarForm,
    type SongSource,
} from './musicSimilar';

const local: SongSource = { id: 'trk_1', title: 'Night Drive', kind: 'local', durationS: 212.4, hashState: 'done' };

describe('the generate-similar dialog', () => {
    it('starts with the SPEC defaults and the source length as the duration', () => {
        const f = emptySimilarForm(local);
        expect(f.mode).toBe('same_style');
        expect(f.keep).toEqual({ genre: true, bpm: true, key: false, timesig: false, mood: true, structure: false, vocals: true });
        expect(f.durationS).toBe('212');
        expect(f.reference).toBe('description');
    });

    it('builds the caption from the kept values that have one', () => {
        const f = emptySimilarForm(local);
        f.keepValues = { ...f.keepValues, genre: 'synthwave', bpm: '96', key: 'E minor', mood: 'nostalgic', vocals: 'instrumental' };
        expect(keptValues(f)).toEqual({ genre: 'synthwave', bpm: 96, mood: 'nostalgic', vocals: 'instrumental' });
        expect(captionFromKeep(f)).toBe('synthwave, nostalgic, 96 BPM, instrumental');
        f.keep.key = true;
        expect(captionFromKeep(f)).toContain('E minor');
    });

    it('sends a same-style request on acestep with the kept values, lyrics mode and reference', () => {
        const f = emptySimilarForm(local);
        f.keepValues.genre = 'lo-fi';
        f.lyricsMode = 'new';
        f.lyricsTheme = ' rainy mornings ';
        f.reference = 'audio';
        f.engine = 'yue2';                          // ignored: a same-style song runs on acestep only
        const v = validateSimilarForm(f);
        expect(v.ok).toBe(true);
        if (!v.ok) return;
        expect(v.type).toBe('generate_similar');
        expect(v.trackId).toBe('trk_1');
        expect(v.params).toEqual({ caption: 'lo-fi', variants: 2, engine: 'acestep', duration_s: 212, keep: { genre: 'lo-fi' },
                                   lyrics_mode: 'new', lyrics_theme: 'rainy mornings', reference: 'audio' });
    });

    it('sends a cover on yue2 by default with the lead melody and no duration; acestep with the strength', () => {
        const f = emptySimilarForm(local, 'cover');
        expect(f.engine).toBe('yue2');
        expect(validateSimilarForm(f).ok).toBe(false);               // the target style is required
        f.caption = 'bossa nova';
        f.captionEdited = true;
        f.durationS = '5000';                                        // a cover is as long as its source: not sent
        expect(validateSimilarForm(f)).toMatchObject({ ok: true, params: { caption: 'bossa nova', variants: 2,
                                                       engine: 'yue2', lyrics_mode: 'reuse', cot: 'melody' } });
        f.coverScore = 'vocal';
        expect(validateSimilarForm(f)).toMatchObject({ params: { cot: 'melody', vocal_only: true } });
        f.coverScore = 'full';
        const full = validateSimilarForm(f);
        expect(full.ok && full.params.cot).toBe('full');
        expect(full.ok && 'vocal_only' in full.params).toBe(false);
        const a = useAcestepCover({ ...f, coverStrength: 0.6 });
        expect(a.engineChosen).toBe(true);
        expect(validateSimilarForm(a)).toEqual({ ok: true, type: 'cover', trackId: 'trk_1', params: {
            caption: 'bossa nova', variants: 2, engine: 'acestep', lyrics_mode: 'reuse', cover_strength: 0.6 } });
    });

    it('offers an acestep cover when a yue2 cover has no lyrics, by the form or by the core', () => {
        const f = { ...emptySimilarForm(local, 'cover'), caption: 'punk', captionEdited: true, coverLyrics: 'none' as const };
        const y = validateSimilarForm(f);
        expect(!y.ok && y.errors.form).toBe(MISSING_LYRICS);
        expect(offersAcestepCover(f, MISSING_LYRICS)).toBe(true);
        expect(offersAcestepCover({ ...f, coverLyrics: 'reuse' }, 'the track has none stored', 'missing_lyrics')).toBe(true);
        expect(offersAcestepCover(useAcestepCover(f), MISSING_LYRICS)).toBe(false);
        expect(validateSimilarForm(useAcestepCover(f)).ok).toBe(true);
        expect(RUACH_ATTRIBUTION).toContain('Based on Ruach Studio by Viktor Zhuromskyy');
    });

    it('starts the cover on the engine the report says runs: yue2, or acestep when yue2 cannot', () => {
        const ok = { features: { cover: { default_engine: 'yue2', fallback_engine: null,
                                          cover_engines: { yue2: { status: 'supported' }, acestep: { status: 'supported' } } } } };
        expect(defaultCoverEngine(ok)).toBe('yue2');
        const missing = { features: { cover: { default_engine: 'yue2', fallback_engine: 'acestep',
                                               cover_engines: { yue2: { status: 'unsupported' }, acestep: { status: 'supported' } } } } };
        expect(defaultCoverEngine(missing)).toBe('acestep');
        expect(defaultCoverEngine(null)).toBe('yue2');
    });

    it("says a web track's audio waits for the streaming integration, but its description works", () => {
        const f = emptySimilarForm({ ...local, kind: 'web' }, 'cover');
        expect(sourceAudioProblem(f)).toMatch(/Yandex Music \/ SoundCloud integration/);
        const caps = { features: { web_source_audio: { status: 'planned', reason: 'listening comes with the integration' } } };
        expect(sourceAudioProblem(f, caps)).toBe('Listening comes with the integration.');
        expect(songSupport(caps, f)).toEqual({ ok: false, reason: 'Listening comes with the integration.' });
        const s = emptySimilarForm({ ...local, kind: 'web' });
        expect(sourceAudioProblem(s)).toBe('');
        s.reference = 'audio';
        expect(sourceAudioProblem(s)).toMatch(/integration/);
        expect(sourceAudioProblem(emptySimilarForm({ ...local, hashState: 'pending' }, 'cover'))).toMatch(/hashed/);
    });

    it('reads support from the capability report per mode and engine', () => {
        const caps = {
            job_types: { generate_similar: { status: 'supported', reason: 'acestep.cpp text2music' } },
            features: {
                audio_reference: { status: 'unsupported', reason: 'ffmpeg is not on PATH' },
                cover: { cover_engines: { acestep: { status: 'supported', reason: 'task cover' },
                                          yue2: { status: 'unsupported', reason: 'yue2.cpp cover: transcriber missing' } } },
            },
        };
        const f = emptySimilarForm(local);
        expect(songSupport(caps, f).ok).toBe(true);
        f.reference = 'audio';
        expect(songSupport(caps, f)).toEqual({ ok: false, reason: 'ffmpeg is not on PATH' });
        const c = emptySimilarForm(local, 'cover');
        expect(songSupport(caps, c)).toEqual({ ok: false, reason: 'yue2.cpp cover: transcriber missing' });
        c.engine = 'acestep';
        expect(songSupport(caps, c).ok).toBe(true);
        expect(songSupport(null, c).ok).toBe(false);
    });

    it('maps a core refusal to its field', () => {
        expect(similarFieldOf('duration_s')).toBe('duration');
        expect(similarFieldOf('track_id')).toBe('source');
        expect(similarFieldOf('lyrics')).toBe('form');
    });
});
