// The pure parts of the "Generate similar" dialog (SPEC flows 10 and 17; ADR-051 row B5; core
// music/similar.py and job_schemas.normalize_params_song): one dialog with a mode switch, Same style
// (`generate_similar`: kept attributes, a style description, lyrics none / new / reuse, a description or
// the source's audio as a reference) or Cover (`cover`: the melody and, when the track has them, the
// lyrics kept, a target style required, yue2.cpp by default (Mike 2026-11-26) or acestep.cpp, as long as
// the source on both). The core checks everything again and
// fills the source's facts and the reused lyrics; this module only shapes the request and says what the
// capability report already says.

export type SongMode = 'same_style' | 'cover';
export type SongEngine = 'acestep' | 'yue2';
/** Ruach Studio's three ways to write a cover's score (index.html:1250-1254): the lead melody (the default),
 *  the vocal line alone, or the melody with its chords. */
export type CoverScore = 'melody' | 'vocal' | 'full';
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
    'Score-based: SheetSage2 transcribes the melody to an ABC score and YuE2 sings it in the new style, as long as ' +
    "the original; the lyrics' blocks follow the score's sections when the stored lyrics are synced; transcription " +
    'errors carry into the cover; MIT code with CC BY-NC weights.';
/** Ruach Studio's attribution (its LICENSE-ADDITIONAL-TERMS.md, term 1): the cover chain is ported from it. */
export const RUACH_ATTRIBUTION =
    'Based on Ruach Studio by Viktor Zhuromskyy · https://github.com/igrbible/Ruach_Studio · GNU AGPL v3 or later';
export const ACESTEP_COVER_OFFER = 'Use acestep cover (no lyrics needed)';
export const MISSING_LYRICS =
    'YuE2 needs the original lyrics for a cover (a technical limit; ACE-Step cover works without lyrics).';
/** The core's words when it does not send them (`web_sources.PENDING_MESSAGE`). */
export const WEB_PENDING =
    'Listening to and downloading web tracks will come with the Yandex Music / SoundCloud integration; until then ' +
    "a web track's audio cannot go to the engine. A same-style song from its description works now.";
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
    /** The person picked the cover engine: the capability report no longer moves it. */
    engineChosen: boolean;
    coverScore: CoverScore;
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
        lyricsTheme: '', reference: 'description', engine: 'yue2', engineChosen: false, coverScore: 'melody',
        coverLyrics: 'reuse', coverStrength: 1,
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

/** The cover engine to start with: the report's default when it runs, else its fallback (acestep when yue2's
 *  transcriber is missing), else yue2. */
export function defaultCoverEngine(caps: any): SongEngine {
    const cover = caps?.features?.cover;
    const d: SongEngine = cover?.default_engine === 'acestep' ? 'acestep' : 'yue2';
    if (cover?.cover_engines?.[d]?.status === 'supported') return d;
    return cover?.fallback_engine === 'acestep' || cover?.fallback_engine === 'yue2' ? cover.fallback_engine : d;
}

/** Why the source's audio cannot go to the engine now (a cover, a reference), or ''. A web track's waits for
 *  the streaming integration, in the capability report's words when it has them. */
export function sourceAudioProblem(form: SimilarForm, caps?: any): string {
    const needs = form.mode === 'cover' || form.reference === 'audio';
    if (!needs) return '';
    if (form.source.kind === 'web') {
        const reason = caps?.features?.web_source_audio?.reason;
        return typeof reason === 'string' && reason ? reason.charAt(0).toUpperCase() + reason.slice(1) + '.' : WEB_PENDING;
    }
    if (form.source.hashState === 'pending') return 'The track is still being hashed; try again in a moment.';
    return '';
}

/** Whether the capability report lets this request run: the job type on its engine (a cover on yue2
 *  needs the transcriber), from `features.cover.cover_engines` and `job_types`. */
export function songSupport(caps: any, form: SimilarForm): { ok: boolean; reason: string } {
    if (!caps) return { ok: false, reason: 'The music service has not answered yet.' };
    if (form.mode === 'cover') {
        const problem = sourceAudioProblem(form, caps);
        if (problem) return { ok: false, reason: problem };
        const st = caps.features?.cover?.cover_engines?.[form.engine];
        if (!st) return { ok: false, reason: 'This core does not report the cover engines.' };
        return { ok: st.status === 'supported', reason: st.reason ?? '' };
    }
    const st = caps.job_types?.generate_similar;
    if (!st) return { ok: false, reason: 'This core does not report same-style songs.' };
    if (form.reference === 'audio') {
        const problem = sourceAudioProblem(form, caps);
        if (problem) return { ok: false, reason: problem };
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
    // A cover is as long as its source on both engines: it sends no duration.
    if (form.mode !== 'cover') {
        const lim = DURATION[engine];
        const d = Number(form.durationS);
        if (!Number.isFinite(d) || d < lim.min || d > lim.max) errors.duration = `Duration: ${lim.min} to ${lim.max} s on ${engine}.`;
        else params.duration_s = d;
    }
    if (form.mode === 'cover') {
        params.lyrics_mode = form.coverLyrics;
        if (form.engine === 'acestep') params.cover_strength = form.coverStrength;
        if (form.engine === 'yue2') {
            params.cot = form.coverScore === 'full' ? 'full' : 'melody';
            if (form.coverScore === 'vocal') params.vocal_only = true;
            if (form.coverLyrics === 'none') errors.form = MISSING_LYRICS;
        }
    } else {
        params.keep = keptValues(form);
        params.lyrics_mode = form.lyricsMode;
        if (form.lyricsMode === 'new' && form.lyricsTheme.trim()) params.lyrics_theme = form.lyricsTheme.trim();
        params.reference = form.reference;
    }
    if (Object.keys(errors).length) return { ok: false, errors };
    return { ok: true, type: SONG_TYPE[form.mode], trackId: form.source.id, params };
}

/** Whether a refusal (the form's, or the core's `missing_lyrics`) should offer an acestep cover instead. */
export function offersAcestepCover(form: SimilarForm, message: string | undefined, code?: string): boolean {
    return form.mode === 'cover' && form.engine === 'yue2' && (code === 'missing_lyrics' || message === MISSING_LYRICS);
}

/** The form switched to an acestep cover (the offer of a yue2 cover that has no lyrics). */
export function useAcestepCover(form: SimilarForm): SimilarForm {
    return { ...form, engine: 'acestep', engineChosen: true };
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
