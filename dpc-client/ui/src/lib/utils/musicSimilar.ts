// The pure parts of the "Generate similar" dialog (SPEC flows 10 and 17; ADR-051 row B5; core
// music/similar.py and job_schemas.normalize_params_song): one dialog with a mode switch, Same style
// (`generate_similar`: kept attributes, a style description, lyrics none / new / reuse; on yue2.cpp, the default
// since Mike 2026-11-26, a section of the source's own codes opens the song and is cut off each take, and the
// describer fills an empty style and each checked Keep box left empty; on acestep.cpp a description or the source's
// audio as a timbre reference) or Cover (`cover`: the melody and, when the track has them, the
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
/** Same style on yue2.cpp (core similar.py `_same_style_on_yue2`): what goes in and what is not known. */
export const YUE2_SAME_STYLE_NOTE =
    "YuE2 opens on the chosen section of this track's own audio codes and writes on from it; the section is cut off " +
    "each take, so Duration is the new part. Whether the track's sound carries into the new part is not verified " +
    "(unverified).";
export const DESCRIBE_NOTE =
    'Leave the style empty, or a checked Keep value empty, and the describer fills it from the track; what you type wins.';
/** The Keep attributes the describer can fill (core job_schemas.KEEP_FROM_SOURCE_KEYS); it does not hear structure. */
export const KEEP_FROM_SOURCE: KeepKey[] = ['genre', 'bpm', 'key', 'timesig', 'mood', 'vocals'];
/** The opening section's bounds (core job_schemas.SIMILAR_SECTION_*) and the cap of section + new part. */
export const SECTION = { min: 10, max: 60, default: 30 } as const;
export const SAME_STYLE_MAX_TOTAL_S = 360;

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
    /** The person picked the engine of this mode: the capability report no longer moves it. */
    engineChosen: boolean;
    /** Same style on yue2: the opening section of the source, in seconds (text fields). */
    sectionStart: string;
    sectionEnd: string;
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
    const end = source.durationS ? Math.min(SECTION.default, Math.floor(source.durationS * 25) / 25) : SECTION.default;
    return {
        mode, source, keep: { ...KEEP_DEFAULT }, keepValues, caption: '', captionEdited: false, lyricsMode: 'none',
        lyricsTheme: '', reference: 'description', engine: 'yue2', engineChosen: false, coverScore: 'melody',
        coverLyrics: 'reuse', coverStrength: 1, sectionStart: '0', sectionEnd: String(end),
        variants: 2, durationS: String(Math.min(Math.max(d, DURATION.acestep.min), DURATION.acestep.max)),
    };
}

/** The checked Keep boxes left empty that the describer fills (same style on yue2): `keep_from_source`. */
export function keepFromSource(form: SimilarForm): KeepKey[] {
    return KEEP_FROM_SOURCE.filter(k => form.keep[k] && !form.keepValues[k].trim());
}

/** The section the waveform selection gives, when it is long enough to open a same-style song: its bounds rounded
 *  to frames (25 a second) and held to `SECTION.max`, or null. */
export function sectionFromRegion(region: { start: number; end: number } | null | undefined): { start: string; end: string } | null {
    if (!region || !(region.end > region.start)) return null;
    const start = Math.round(region.start * 25) / 25;
    const end = Math.round(Math.min(region.end, region.start + SECTION.max) * 25) / 25;
    if (end - start < SECTION.min) return null;
    return { start: String(start), end: String(end) };
}

/** A description (core `describe_music_track` `suggestion`) put into the form for the person to edit: every Keep
 *  value left empty takes the description's, and an unedited style becomes its style line. Typed values stay. */
export function applyDescription(form: SimilarForm, suggestion: { caption?: string; keep?: Record<string, unknown> }): SimilarForm {
    const keepValues = { ...form.keepValues };
    for (const k of KEEP_FROM_SOURCE) {
        const v = suggestion.keep?.[k];
        if (v === undefined || v === null || keepValues[k].trim()) continue;
        keepValues[k] = String(v);
    }
    const out: SimilarForm = { ...form, keepValues };
    const line = (suggestion.caption ?? '').trim();
    if (line && !form.captionEdited) {
        out.caption = line;
        out.captionEdited = true;
    }
    return out;
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

/** The same-style engine to start with, read like the cover's (`features.same_style`): yue2 when it runs, else
 *  the fallback (acestep when the audio tokenizer is missing). A core that does not report it runs acestep only. */
export function defaultSameStyleEngine(caps: any): SongEngine {
    const same = caps?.features?.same_style;
    if (!same) return caps ? 'acestep' : 'yue2';
    const d: SongEngine = same.default_engine === 'acestep' ? 'acestep' : 'yue2';
    if (same.same_style_engines?.[d]?.status === 'supported') return d;
    return same.fallback_engine === 'acestep' || same.fallback_engine === 'yue2' ? same.fallback_engine : d;
}

/** The engine a mode starts with (`defaultCoverEngine`, `defaultSameStyleEngine`). */
export function defaultEngine(caps: any, mode: SongMode): SongEngine {
    return mode === 'cover' ? defaultCoverEngine(caps) : defaultSameStyleEngine(caps);
}

/** Whether the report says a describer is set up (`features.describe`), and its alias. */
export function describerOf(caps: any): { ok: boolean; alias: string | null; reason: string } {
    const d = caps?.features?.describe;
    return { ok: d?.status === 'supported', alias: d?.alias ?? null, reason: d?.reason ?? 'This core has no describer.' };
}

/** Why the source's audio cannot go to the engine now (a cover, a reference), or ''. A web track's waits for
 *  the streaming integration, in the capability report's words when it has them. */
export function sourceAudioProblem(form: SimilarForm, caps?: any): string {
    const needs = form.mode === 'cover' || form.reference === 'audio' || form.engine === 'yue2';
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
    const per = caps.features?.same_style?.same_style_engines?.[form.engine];
    if (form.engine === 'yue2') {
        const problem = sourceAudioProblem(form, caps);
        if (problem) return { ok: false, reason: problem };
        if (!per) return { ok: false, reason: 'This core runs same style on acestep only.' };
        return { ok: per.status === 'supported', reason: per.reason ?? '' };
    }
    if (per && per.status !== 'supported') return { ok: false, reason: per.reason ?? '' };
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
    // Same style on yue2 may leave the style to the describer (core describe.py): an empty caption is its request.
    const describerStyle = form.mode === 'same_style' && form.engine === 'yue2';
    if (!caption && !describerStyle)
        errors.caption = form.mode === 'cover' ? 'A target style is required.' : 'Describe the style, or keep an attribute with a value.';
    if (!Number.isInteger(form.variants) || form.variants < VARIANTS_MIN || form.variants > VARIANTS_MAX)
        errors.variants = `Variants: ${VARIANTS_MIN} to ${VARIANTS_MAX}.`;
    const bpm = form.keepValues.bpm.trim();
    if (form.mode === 'same_style' && form.keep.bpm && bpm && !(Number.isInteger(Number(bpm)) && Number(bpm) >= 1 && Number(bpm) <= 400))
        errors.keep = 'Tempo must be a whole number of BPM.';
    const problem = sourceAudioProblem(form);
    if (problem) errors.source = problem;
    // Both modes run on yue2.cpp (the default) or acestep.cpp (Mike 2026-11-26; the core's SONG_ENGINES).
    const engine: SongEngine = form.engine;
    const params: Record<string, unknown> = { caption, variants: form.variants, engine };
    let sectionS = 0;
    if (form.mode === 'same_style' && engine === 'yue2') {
        const a = Number(form.sectionStart), b = Number(form.sectionEnd);
        const max = form.source.durationS ?? Infinity;
        if (!Number.isFinite(a) || !Number.isFinite(b) || a < 0 || b > max + 0.04 || b - a < SECTION.min - 1e-9 || b - a > SECTION.max + 1e-9)
            errors.form = `Opening section: ${SECTION.min} to ${SECTION.max} s of the track.`;
        else {
            params.source_section = { start_s: a, end_s: b };
            sectionS = b - a;
        }
    }
    // A cover is as long as its source on both engines: it sends no duration. Same style on yue2: the new part.
    if (form.mode !== 'cover') {
        const lim: { min: number; max: number } = { ...DURATION[engine] };
        if (form.mode === 'same_style' && engine === 'yue2') lim.max = Math.floor((SAME_STYLE_MAX_TOTAL_S - sectionS) * 1000) / 1000;
        const d = Number(form.durationS);
        if (!Number.isFinite(d) || d < lim.min || d > lim.max)
            errors.duration = sectionS ? `Duration (the new part): ${lim.min} to ${lim.max} s after the ${sectionS} s section.`
                                       : `Duration: ${lim.min} to ${lim.max} s on ${engine}.`;
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
        if (engine === 'yue2') {
            const asked = keepFromSource(form);
            if (asked.length) params.keep_from_source = asked;
        } else {
            params.reference = form.reference;
        }
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
