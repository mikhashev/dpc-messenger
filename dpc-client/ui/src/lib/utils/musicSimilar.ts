// The pure parts of the "Generate similar" dialog (SPEC flows 10 and 17; ADR-051 row B5; core
// music/similar.py and job_schemas.normalize_params_song): one dialog with a mode switch, Same style
// (`generate_similar`: kept attributes, a style description, lyrics none / new / reuse, a description or
// the source's audio as a reference) or Cover (`cover`: the melody and, when the track has them, the
// lyrics kept, a target style required, acestep.cpp or yue2.cpp). The core checks everything again and
// fills the source's facts and the reused lyrics; this module only shapes the request and says what the
// capability report already says.

export type SongMode = 'same_style' | 'cover';
export type SongEngine = 'acestep' | 'yue2';
export type LyricsMode = 'none' | 'new' | 'reuse';
export type KeepKey = 'genre' | 'bpm' | 'key' | 'timesig' | 'mood' | 'structure' | 'vocals';

export const KEEP_KEYS: KeepKey[] = ['genre', 'bpm', 'key', 'timesig', 'mood', 'structure', 'vocals'];
export const KEEP_LABELS: Record<KeepKey, string> = {
    genre: 'Genre / style tags', bpm: 'Tempo (BPM)', key: 'Key', timesig: 'Time signature', mood: 'Mood',
    structure: 'Structure', vocals: 'Vocals',
};
/** SPEC flow 10: genre, tempo, mood and vocals kept by default; key, time signature and structure free. */
export const KEEP_DEFAULT: Record<KeepKey, boolean> = {
    genre: true, bpm: true, key: false, timesig: false, mood: true, structure: false, vocals: true,
};
export const SONG_TYPE: Record<SongMode, string> = { same_style: 'generate_similar', cover: 'cover' };
export const VARIANTS_MIN = 1;
export const VARIANTS_MAX = 4;
export const YUE2_COVER_CAVEATS =
    'Score-based: SheetSage2 transcribes the melody to an ABC score and YuE2 sings it in the new style; the lyrics ' +
    'must match the original syllables; transcription errors carry into the cover; MIT code with CC BY-NC weights.';
export const REFERENCE_NOTE =
    "The source's audio goes to acestep.cpp as a timbre reference; whether it steers the style beyond timbre is not verified.";

export interface SongSource {
    id: string;
    title: string;
    kind: string;                // local | web | generated
    durationS: number | null;
    hashState?: string | null;   // the library's hash_state: pending while the file is being hashed
}

export interface SimilarForm {
    mode: SongMode;
    source: SongSource;
    keep: Record<KeepKey, boolean>;
    keepValues: Record<KeepKey, string>;
    caption: string;
    captionEdited: boolean;
    lyricsMode: LyricsMode;
    lyricsTheme: string;
    reference: 'description' | 'audio';
    engine: SongEngine;
    coverLyrics: 'reuse' | 'none';
    coverStrength: number;
    variants: number;
    durationS: string;
}

export type SimilarErrors = Partial<Record<'caption' | 'variants' | 'duration' | 'keep' | 'source' | 'form', string>>;

const DURATION = { acestep: { min: 10, max: 600 }, yue2: { min: 30, max: 360 } } as const;

export function emptySimilarForm(source: SongSource, mode: SongMode = 'same_style'): SimilarForm {
    const keepValues = Object.fromEntries(KEEP_KEYS.map(k => [k, ''])) as Record<KeepKey, string>;
    const d = source.durationS ? Math.round(source.durationS) : 120;
    return {
        mode, source, keep: { ...KEEP_DEFAULT }, keepValues, caption: '', captionEdited: false, lyricsMode: 'none',
        lyricsTheme: '', reference: 'description', engine: 'acestep', coverLyrics: 'reuse', coverStrength: 1,
        variants: 2, durationS: String(Math.min(Math.max(d, DURATION.acestep.min), DURATION.acestep.max)),
    };
}

/** The kept attributes that have a value, as the core's `keep` object (bpm a whole number). */
export function keptValues(form: SimilarForm): Record<string, string | number> {
    const out: Record<string, string | number> = {};
    for (const k of KEEP_KEYS) {
        const v = form.keepValues[k].trim();
        if (!form.keep[k] || !v) continue;
        if (k === 'bpm') {
            const n = Number(v);
            if (Number.isInteger(n)) out.bpm = n;
        } else if (k === 'vocals') {
            out.vocals = /^instr/i.test(v) ? 'instrumental' : 'vocals';
        } else {
            out[k] = v;
        }
    }
    return out;
}

/** The style description built from the kept values (SPEC flow 10): until the person edits it, toggling
 *  Keep rewrites it. */
export function captionFromKeep(form: SimilarForm): string {
    const k = keptValues(form);
    const parts: string[] = [];
    if (k.genre) parts.push(String(k.genre));
    if (k.mood) parts.push(String(k.mood));
    if (k.bpm) parts.push(`${k.bpm} BPM`);
    if (k.key) parts.push(String(k.key));
    if (k.timesig) parts.push(`${k.timesig} time`);
    if (k.structure) parts.push(String(k.structure));
    if (k.vocals) parts.push(k.vocals === 'instrumental' ? 'instrumental' : 'with vocals');
    return parts.join(', ');
}

/** Why the source's audio cannot go to the engine now (a cover, a reference), or ''. */
export function sourceAudioProblem(form: SimilarForm): string {
    const needs = form.mode === 'cover' || form.reference === 'audio';
    if (!needs) return '';
    if (form.source.kind === 'web') return 'A web track is listen-only here: DPC does not download it, so its audio cannot go to the engine.';
    if (form.source.hashState === 'pending') return 'The track is still being hashed; try again in a moment.';
    return '';
}

/** Whether the capability report lets this request run: the job type on its engine (a cover on yue2
 *  needs the transcriber), from `features.cover.cover_engines` and `job_types`. */
export function songSupport(caps: any, form: SimilarForm): { ok: boolean; reason: string } {
    if (!caps) return { ok: false, reason: 'The music service has not answered yet.' };
    if (form.mode === 'cover') {
        if (sourceAudioProblem(form)) return { ok: false, reason: sourceAudioProblem(form) };
        const st = caps.features?.cover?.cover_engines?.[form.engine];
        if (!st) return { ok: false, reason: 'This core does not report the cover engines.' };
        return { ok: st.status === 'supported', reason: st.reason ?? '' };
    }
    const st = caps.job_types?.generate_similar;
    if (!st) return { ok: false, reason: 'This core does not report same-style songs.' };
    if (form.reference === 'audio') {
        const ref = caps.features?.audio_reference;
        if (ref && ref.status !== 'supported') return { ok: false, reason: ref.reason ?? '' };
    }
    return { ok: st.status === 'supported', reason: st.reason ?? '' };
}

export type SimilarValidation =
    | { ok: true; type: string; trackId: string; params: Record<string, unknown> }
    | { ok: false; errors: SimilarErrors };

/** The `propose_music_job` request of the form, or the errors to show on its fields. */
export function validateSimilarForm(form: SimilarForm): SimilarValidation {
    const errors: SimilarErrors = {};
    const caption = (form.captionEdited ? form.caption : form.caption || captionFromKeep(form)).trim();
    if (!caption) errors.caption = form.mode === 'cover' ? 'A target style is required.' : 'Describe the style, or keep an attribute with a value.';
    if (!Number.isInteger(form.variants) || form.variants < VARIANTS_MIN || form.variants > VARIANTS_MAX)
        errors.variants = `Variants: ${VARIANTS_MIN} to ${VARIANTS_MAX}.`;
    const bpm = form.keepValues.bpm.trim();
    if (form.mode === 'same_style' && form.keep.bpm && bpm && !(Number.isInteger(Number(bpm)) && Number(bpm) >= 1 && Number(bpm) <= 400))
        errors.keep = 'Tempo must be a whole number of BPM.';
    const problem = sourceAudioProblem(form);
    if (problem) errors.source = problem;
    // A same-style song runs on acestep.cpp only (the core's SONG_ENGINES).
    const engine: SongEngine = form.mode === 'same_style' ? 'acestep' : form.engine;
    const params: Record<string, unknown> = { caption, variants: form.variants, engine };
    const coverOnAce = form.mode === 'cover' && engine === 'acestep';
    if (!coverOnAce) {
        const lim = DURATION[engine];
        const d = Number(form.durationS);
        if (!Number.isFinite(d) || d < lim.min || d > lim.max) errors.duration = `Duration: ${lim.min} to ${lim.max} s on ${engine}.`;
        else params.duration_s = d;
    }
    if (form.mode === 'cover') {
        params.lyrics_mode = form.coverLyrics;
        if (form.engine === 'acestep') params.cover_strength = form.coverStrength;
        if (form.engine === 'yue2' && form.coverLyrics === 'none')
            errors.form = 'YuE2 needs the original lyrics for a cover (a technical limit; ACE-Step cover works without lyrics).';
    } else {
        params.keep = keptValues(form);
        params.lyrics_mode = form.lyricsMode;
        if (form.lyricsMode === 'new' && form.lyricsTheme.trim()) params.lyrics_theme = form.lyricsTheme.trim();
        params.reference = form.reference;
    }
    if (Object.keys(errors).length) return { ok: false, errors };
    return { ok: true, type: SONG_TYPE[form.mode], trackId: form.source.id, params };
}

/** The form field a core refusal names (`field` of the answer). */
export function similarFieldOf(field: string | undefined): keyof SimilarErrors {
    if (field === 'caption') return 'caption';
    if (field === 'variants') return 'variants';
    if (field === 'duration_s') return 'duration';
    if (field === 'keep') return 'keep';
    if (field === 'track_id' || field === 'source_track_id') return 'source';
    return 'form';
}
