import { describe, expect, it } from 'vitest';
import {
    captionFromKeep,
    emptySimilarForm,
    keptValues,
    similarFieldOf,
    songSupport,
    sourceAudioProblem,
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

    it('sends a cover without a duration on acestep and with the strength; yue2 without lyrics is refused', () => {
        const f = emptySimilarForm(local, 'cover');
        expect(validateSimilarForm(f).ok).toBe(false);               // the target style is required
        f.caption = 'bossa nova';
        f.captionEdited = true;
        f.coverStrength = 0.6;
        const v = validateSimilarForm(f);
        expect(v.ok && v.params).toEqual({ caption: 'bossa nova', variants: 2, engine: 'acestep', lyrics_mode: 'reuse',
                                           cover_strength: 0.6 });
        f.engine = 'yue2';
        f.coverLyrics = 'none';
        const y = validateSimilarForm(f);
        expect(y.ok).toBe(false);
        if (!y.ok) expect(y.errors.form).toMatch(/YuE2 needs the original lyrics/);
        f.coverLyrics = 'reuse';
        f.durationS = '500';
        const d = validateSimilarForm(f);
        expect(!d.ok && d.errors.duration).toMatch(/30 to 360/);
    });

    it("says a web track's audio cannot go to the engine, but its description can", () => {
        const f = emptySimilarForm({ ...local, kind: 'web' }, 'cover');
        expect(sourceAudioProblem(f)).toMatch(/listen-only/);
        const s = emptySimilarForm({ ...local, kind: 'web' });
        expect(sourceAudioProblem(s)).toBe('');
        s.reference = 'audio';
        expect(sourceAudioProblem(s)).toMatch(/listen-only/);
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
        expect(songSupport(caps, c).ok).toBe(true);
        c.engine = 'yue2';
        expect(songSupport(caps, c)).toEqual({ ok: false, reason: 'yue2.cpp cover: transcriber missing' });
        expect(songSupport(null, c).ok).toBe(false);
    });

    it('maps a core refusal to its field', () => {
        expect(similarFieldOf('duration_s')).toBe('duration');
        expect(similarFieldOf('track_id')).toBe('source');
        expect(similarFieldOf('lyrics')).toBe('form');
    });
});
