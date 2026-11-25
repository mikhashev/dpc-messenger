// The Music Player's Create panel: pure rules (SPEC flow "song from own text", §5, §8,
// §12 decisions 29, 32-37; ADR-051 D30-D33).
//
// The panel (components/music/MusicCreate.svelte, MusicReview.svelte) and its service
// (services/musicJobs.ts) call these; nothing here touches the DOM, the audio element or
// the socket, so every rule is tested in musicCreate.test.ts.
//
// - the form mirrors the core's `normalize_params_from_text` limits (job_schemas.py), so
//   a bad value is named before anything is sent; the core stays the authority and its
//   `field` error is shown on the same input;
// - jobs arrive by `music_job_updated` events and by command answers, in any order: a job
//   never returns to an earlier state, so the copy with more history (then the later
//   `updated_at`) wins;
// - level matching (decision 37: a plain A/B at matched loudness, display gain only): the
//   louder takes are turned down to the quietest one's integrated loudness; nothing is
//   turned up, since `HTMLAudioElement.volume` stops at 1;
// - the A/B switch keeps the position, clamped to the other take's length;
// - a Discard or "None of these" needs a reason: text, a class, or both (decision 35);
// - two engines (decision 40, Mike #198): yue2 (the default) and acestep, each with its own
//   duration limits; for yue2 the duration is a cap, and the form warns when the cap looks
//   short for the lyrics (an estimate: ~10 s per sung line plus a ~20 s intro).

import { lyricsPrepFor, type LyricsPrepState } from './musicLyricsPrep';

// -- records from the core (music/job_schemas.py, capabilities.py) -----------------

export type JobState = 'queued' | 'running' | 'review' | 'accepted' | 'rejected' | 'failed' | 'cancelled' | 'unsupported';

export interface CardHolder {
    alias: string;
    engine: string | null;
}

export interface MusicJob {
    id: string;
    type: string;
    state: JobState | string;
    state_reason?: string | null;
    progress?: number;
    take_ids?: string[];
    params: Record<string, any>;
    prompt?: string | null;
    created_at: string;
    updated_at?: string;
    history?: { state: string; at: string; reason: string | null }[];
    card_wait?: { holders: CardHolder[]; since: string } | null;
    engine_choice?: EngineChoice | null;
    /** An edit job's (ADR-051 row B4): the action, the intent region, the library track it edits. */
    action?: string | null;
    region?: { t_start: number; t_end: number } | null;
    track_id?: string | null;
}

/** What a rule chose for a job from what the job is (core `job_schemas`, `engine_choice`):
 *  the engine, the adapters it added, the fallback, and the sentence the job card shows. */
export interface EngineChoice {
    rule: string;
    engine: string;
    adapters: string[];
    fallback: string | null;
    reason: string;
    at?: string;
    fell_back?: { from: string; adapters: string[]; why: string; at: string } | null;
}

/** A take's `loudness` record (core `loudness.py`). */
export interface TakeLoudness {
    lufs_i: number;
    lra: number | null;
    true_peak_dbtp: number | null;
    measured_by: string;
    measured_at: string;
}

/** One stage-1 QA gate of a take's `qa_report` (core `qa.py`). */
export interface TakeQaGate {
    status: 'pass' | 'fail' | 'not_measured' | string;
    detail: string;
}

/** A take's stage-1 QA report: four gates and a verdict. Seams and lyrics WER are advisory and never gate. */
export interface TakeQaReport {
    stage: number;
    verdict: 'passed' | 'failed' | 'not_measured' | string;
    gates: Record<string, TakeQaGate>;
    advisory?: Record<string, { status: string; reason?: string }>;
    measurements?: Record<string, unknown>;
    note?: string | null;
}

export interface MusicTake {
    id: string;
    job_id: string;
    variant: number;
    title: string;
    file: { uri: string; content_hash: string | null; sample_rate?: number | null; channels?: number | null; duration_s?: number | null };
    decision: 'pending' | 'accepted' | 'rejected' | string;
    seed?: number;
    synth_batch_size?: number;
    synth_batch_index?: number;
    duration_requested_s?: number;
    library_track_id?: string | null;
    reason_text?: string | null;
    reason_class?: string | null;
    decided_at?: string | null;
    loudness?: TakeLoudness | null;
    /** The stage-1 `mp.qa/1`, or null when it could not be filled (the report says why). */
    qa?: Record<string, unknown> | null;
    qa_report?: TakeQaReport | null;
    engine?: string | null;
    lm_seed?: number | null;
}

export type EngineState = 'not_built' | 'not_installed' | 'stopped' | 'starting' | 'running' | 'error';

/** `EngineStatus.as_dict()` (the event) or `SupervisedEngine.details()` (the command). */
export interface MusicEngine {
    id?: string;
    /** The job-param word: 'yue2' or 'acestep'. */
    key?: string | null;
    name?: string;
    state: EngineState | string;
    available?: boolean;
    reason: string;
    version?: string | null;
    busy?: boolean;
    /** A start by hand waiting for the card (the core's `card_wait`): what is letting go and for how
     *  long, `text` being the person's line; null when nothing waits. */
    card_wait?: MusicEngineCardWait | null;
}

export interface MusicEngineCardWait {
    text: string;
    behind?: string[];
    wait_s?: number | null;
    known?: boolean;
    /** The figure is the longest the wait can take, not an estimate. */
    upper_bound?: boolean;
}

export interface FailureClass {
    id: string;
    meaning: string;
}

/** A voice or style adapter yue2.cpp can stack (capabilities `engines.yue2.adapters`). */
export interface EngineAdapter {
    name: string;
    file?: string;
    /** 'instrumental' for the adapter an Instrumental job gets by itself (not a voice choice). */
    kind?: 'voice' | 'instrumental' | 'other' | string;
    layout?: 'split' | 'whole' | string | null;
    halves?: string[];
}

/** One engine in the capability report (`engines.<key>`). */
export interface EngineInfo extends MusicEngine {
    default?: boolean;
    limits?: Record<string, any>;
    backbones?: Record<string, { present: boolean; file?: string; reason?: string | null }>;
    backbone_default?: string;
    adapters?: EngineAdapter[];
    adapter_note?: string;
}

export interface MusicCapabilities {
    engine?: MusicEngine;
    engines?: Record<string, EngineInfo>;
    default_engine?: string;
    /** What each rule would choose now (core `MusicCapabilities.engine_choice_rules`). */
    engine_choice_rules?: Record<string, Omit<EngineChoice, 'at' | 'fell_back'> | null>;
    job_types?: Record<string, { status: string; reason: string }>;
    /** Each comment / edit action's `{status, reason}` (SPEC §7 `actions`). */
    actions?: Record<string, { status: string; reason: string }>;
    limits?: { generate_from_text?: Partial<FromTextLimits> & Record<string, any>; edit?: Record<string, any> };
    take_decisions?: { failure_classes?: FailureClass[]; max_reason_text?: number };
}

export interface DiscardReason {
    text: string;
    key: string;
    uses: number;
    reason_class?: string | null;
}

// -- states ------------------------------------------------------------------------

export const TERMINAL_STATES: readonly string[] = ['accepted', 'rejected', 'failed', 'cancelled', 'unsupported'];
export const ACTIVE_STATES: readonly string[] = ['queued', 'running'];

export function isTerminal(job: Pick<MusicJob, 'state'>): boolean {
    return TERMINAL_STATES.includes(job.state);
}

export function isActive(job: Pick<MusicJob, 'state'>): boolean {
    return ACTIVE_STATES.includes(job.state);
}

/** The GPU card holders a queued job waits for (decision 32), or []. A running job waits too
 *  when its engine stopped under it and its next start asks for the card again (ADR-052 S4). */
export function cardHolders(job: Pick<MusicJob, 'state' | 'card_wait'>): CardHolder[] {
    if ((job.state !== 'queued' && job.state !== 'running') || !job.card_wait) return [];
    return (job.card_wait.holders ?? []).filter(h => typeof h?.alias === 'string' && h.alias);
}

/** "Waiting for the GPU: X holds it" (one holder) or "... X, Y hold it". */
export function cardWaitText(holders: readonly CardHolder[]): string {
    if (!holders.length) return '';
    const names = holders.map(h => h.alias);
    return names.length === 1
        ? `Waiting for the GPU: ${names[0]} holds it`
        : `Waiting for the GPU: ${names.join(', ')} hold it`;
}

/** One line for a job's state, as the jobs list shows it. */
export function jobStateText(job: MusicJob): string {
    const reason = (job.state_reason ?? '').trim();
    switch (job.state) {
        case 'queued': {
            const holders = cardHolders(job);
            return holders.length ? cardWaitText(holders) : 'Queued';
        }
        case 'running': {
            const holders = cardHolders(job);
            return holders.length
                ? cardWaitText(holders)
                : `Running · ${Math.round(Math.max(0, Math.min(1, job.progress ?? 0)) * 100)}%`;
        }
        case 'review': return reason ? `Ready for review (${reason})` : 'Ready for review';
        case 'accepted': return 'Kept';
        case 'rejected': return 'None kept (every take discarded)';
        case 'failed': return `Failed: ${reason || 'no reason given'}`;
        case 'cancelled': return 'Cancelled';
        case 'unsupported': return `Unsupported: ${reason || 'no reason given'}`;
        default: return String(job.state);
    }
}

/** The style the job was made with, cut for a row. */
export function jobTitle(job: MusicJob, limit = 70): string {
    const style = String((job.type === 'edit' ? job.prompt : null) ?? job.params?.style ?? job.prompt ?? '')
        .replace(/\s+/g, ' ').trim();
    if (!style) return job.id;
    return style.length > limit ? `${style.slice(0, limit - 1)}…` : style;
}

// -- the event -> jobs reducer ----------------------------------------------------

/** True when `incoming` is at least as new as `existing`: more history entries (a job
 *  never goes back), then a later or equal `updated_at`. */
export function jobIsNewer(incoming: MusicJob, existing: MusicJob | undefined | null): boolean {
    if (!existing) return true;
    const hi = incoming.history?.length ?? 0;
    const he = existing.history?.length ?? 0;
    if (hi !== he) return hi > he;
    return (incoming.updated_at ?? incoming.created_at ?? '') >= (existing.updated_at ?? existing.created_at ?? '');
}

/** Newest first. */
export function sortJobs(jobs: readonly MusicJob[]): MusicJob[] {
    return [...jobs].sort((a, b) => (a.created_at < b.created_at ? 1 : a.created_at > b.created_at ? -1 : a.id < b.id ? 1 : -1));
}

/** The list with `job` in it, unless the list already holds a newer copy (then the same array). */
export function upsertJob(jobs: readonly MusicJob[], job: MusicJob): MusicJob[] {
    const i = jobs.findIndex(j => j.id === job.id);
    if (i >= 0) {
        if (!jobIsNewer(job, jobs[i])) return jobs as MusicJob[];
        const out = [...jobs];
        out[i] = job;
        return out;
    }
    return sortJobs([...jobs, job]);
}

/** A whole list read from the core, merged with what events already brought. */
export function mergeJobs(jobs: readonly MusicJob[], incoming: readonly MusicJob[]): MusicJob[] {
    let out = [...jobs];
    for (const j of incoming) out = upsertJob(out, j);
    return sortJobs(out);
}

export interface MusicEventState {
    jobs: MusicJob[];
    engine: MusicEngine | null;
    /** Each engine's last status, by its key ('yue2', 'acestep'). */
    engines?: Record<string, MusicEngine>;
}

/** An engine status's key: its `key`, else read from its `id` (`yue2.cpp`, `acestep.cpp`). */
export function engineKeyOf(engine: Pick<MusicEngine, 'key' | 'id'> | null | undefined): string | null {
    if (!engine) return null;
    if (typeof engine.key === 'string' && engine.key) return engine.key;
    if (engine.id === 'yue2.cpp') return 'yue2';
    if (engine.id === 'acestep.cpp') return 'acestep';
    return null;
}

/** Apply one backend event. Unknown events and malformed payloads leave the state as it is. */
export function reduceMusicEvent(state: MusicEventState, event: string, payload: any): MusicEventState {
    if (event === 'music_job_updated') {
        const job = payload?.job;
        if (!job || typeof job.id !== 'string' || typeof job.state !== 'string') return state;
        const jobs = upsertJob(state.jobs, job as MusicJob);
        return jobs === state.jobs ? state : { ...state, jobs };
    }
    if (event === 'music_engine_status') {
        const engine = payload?.engine;
        if (!engine || typeof engine.state !== 'string') return state;
        const key = engineKeyOf(engine);
        const engines = { ...(state.engines ?? {}) };
        if (key) engines[key] = { ...(engines[key] ?? {}), ...engine } as MusicEngine;
        return { ...state, engine: { ...(state.engine ?? {}), ...engine } as MusicEngine, engines };
    }
    return state;
}

/** How long an active job may go without an event before the panel asks for it. */
export const JOB_QUIET_MS = 5000;
export const JOB_POLL_TICK_MS = 2500;

/** The active jobs not heard from (event or answer) for `quietMs`: the fallback poll. */
export function jobsToPoll(jobs: readonly MusicJob[], lastSeen: Readonly<Record<string, number>>, now: number, quietMs = JOB_QUIET_MS): string[] {
    return jobs.filter(j => isActive(j) && now - (lastSeen[j.id] ?? 0) >= quietMs).map(j => j.id);
}

// -- the engine ------------------------------------------------------------------

/** The engine status line. */
export function engineStatusText(engine: MusicEngine | null | undefined): string {
    if (!engine) return 'Engine status unknown';
    const reason = (engine.reason ?? '').trim();
    // A start by hand that waits for the card (before the child exists: the state is still `stopped`).
    const wait = (engine.card_wait?.text ?? '').trim();
    if (wait && (engine.state === 'stopped' || engine.state === 'error')) return wait;
    switch (engine.state) {
        case 'not_built': return `Engine not available: ${reason || 'not built in DPC'}`;
        case 'not_installed': return `Engine not installed: ${reason || 'binary or weights missing'}`;
        case 'stopped': return 'Engine stopped (DPC starts it when a job runs)';
        case 'starting': return 'Engine starting…';
        case 'running': return engine.busy ? 'Engine running (busy with a job)' : 'Engine running';
        case 'error': return `Engine error: ${reason || 'the engine failed'}`;
        default: return `Engine: ${engine.state}`;
    }
}

export function engineCanStart(engine: MusicEngine | null | undefined): boolean {
    return !!engine && (engine.state === 'stopped' || engine.state === 'error');
}

export function engineCanStop(engine: MusicEngine | null | undefined): boolean {
    return !!engine && (engine.state === 'starting' || engine.state === 'running');
}

/** `{supported, reason}` of generate_from_text from the capability report, on `engine`
 *  when the report lists engines. Without a report it is not supported (the core is the
 *  authority; the panel waits for it). */
export function fromTextSupport(caps: MusicCapabilities | null | undefined, engine?: string): { supported: boolean; reason: string } {
    const st = caps?.job_types?.generate_from_text;
    if (!st) return { supported: false, reason: 'the capabilities have not been read yet' };
    const info = engine ? caps?.engines?.[engine] : undefined;
    if (info) return { supported: !!info.available, reason: info.available ? '' : (info.reason ?? `${engine} is not available`) };
    return { supported: st.status === 'supported', reason: st.reason ?? '' };
}

// -- engines -------------------------------------------------------------------------

export type EngineKey = 'yue2' | 'acestep';
/** Mike #198, 2026-11-21: YuE2 is the default engine. */
export const DEFAULT_ENGINE: EngineKey = 'yue2';
export const ENGINE_CHOICES: readonly { key: EngineKey; label: string; detail: string }[] = [
    { key: 'yue2', label: 'YuE2 (yue2.cpp)', detail: 'yue2.cpp · YuE2 3B Q8 · one run per variant · FLAC' },
    { key: 'acestep', label: 'ACE-Step 1.5 (acestep.cpp)', detail: 'acestep.cpp · turbo Q8 · 8 steps · FLAC' },
];
export const BACKBONE_CHOICES: readonly { key: string; label: string }[] = [
    { key: 'stock', label: 'Stock (m-a-p YuE2 3B)' },
    { key: 'rs-tw', label: 'RS-TW finetune' },
];
export const ADAPTER_MUSIC_DEFAULT = 0.45;
export const ADAPTER_SOUND_DEFAULT = 0.35;
export const VOICE_NOTE = "Voice adapters are trained on real readers' voices without stated consent: for local listening only.";

/** The engine the panel starts on: the report's default, else yue2. */
export function defaultEngineOf(caps: MusicCapabilities | null | undefined): EngineKey {
    const d = caps?.default_engine;
    return d === 'acestep' || d === 'yue2' ? d : DEFAULT_ENGINE;
}

/** The adapters the person can pick in the Voice select, voices first: what yue2.cpp offers
 *  minus the instrumental adapter, which an Instrumental job gets by itself. */
export function yue2Adapters(caps: MusicCapabilities | null | undefined): EngineAdapter[] {
    const list = (caps?.engines?.yue2?.adapters ?? []).filter(a => a.kind !== 'instrumental');
    return [...list].sort((a, b) => (a.kind === 'voice' ? 0 : 1) - (b.kind === 'voice' ? 0 : 1) || a.name.localeCompare(b.name));
}

/** The line under Instrumental: which engine and adapter the job will use, from the core's
 *  `engine_choice_rules` (it decides; the form only shows it). '' when the box is not ticked,
 *  the engine is not YuE2, or the core names no rule (then nothing is claimed). */
export function instrumentalChoiceText(form: Pick<FromTextForm, 'engine' | 'instrumental'>,
    caps: MusicCapabilities | null | undefined): string {
    if (!form.instrumental || (form.engine ?? DEFAULT_ENGINE) !== 'yue2') return '';
    const rule = caps?.engine_choice_rules?.instrumental;
    if (!rule) return '';
    if (rule.engine === 'yue2') {
        const name = rule.adapters?.[0] ?? 'the instrumental adapter';
        return `Runs on YuE2 with the instrumental adapter ${name}${rule.fallback === 'acestep' ? '; if it does not load, acestep.cpp makes it instead' : ''}.`;
    }
    return `Runs on acestep.cpp instead of YuE2: ${rule.reason.replace(/^[^:]*:\s*acestep\.cpp,\s*because\s*/i, '')}.`;
}

/** The voice adapter a job names, leaving out the adapters its engine choice added. */
export function jobVoiceName(job: Pick<MusicJob, 'params' | 'engine_choice'>): string | null {
    const added = new Set(job.engine_choice?.adapters ?? []);
    const voice = (job.params?.adapters ?? []).find((a: any) => a && typeof a.name === 'string' && !added.has(a.name));
    return voice ? voice.name : null;
}

/** Whether a backbone's file is present (true when the report does not say). */
export function backbonePresent(caps: MusicCapabilities | null | undefined, backbone: string): boolean {
    const b = caps?.engines?.yue2?.backbones?.[backbone];
    return b ? !!b.present : true;
}

// -- the short-cap warning (yue2) ---------------------------------------------------

export const YUE2_SECONDS_PER_LINE = 10;
export const YUE2_INTRO_S = 20;

/** About how long YuE2 needs to sing these lyrics: an intro plus a few seconds per sung
 *  line (section tags such as [verse] and blank lines do not count). The core's
 *  `estimate_yue2_seconds`; an estimate, never a limit. */
export function estimateYue2Seconds(lyrics: string | null | undefined, perLine = YUE2_SECONDS_PER_LINE, intro = YUE2_INTRO_S): number {
    if (!lyrics) return intro;
    const sung = lyrics.split(/\r?\n/).map(l => l.trim()).filter(l => l && !(l.startsWith('[') && l.endsWith(']')));
    return intro + perLine * sung.length;
}

/** The warning under the duration when a YuE2 cap looks too short for the lyrics, or ''. */
export function shortCapWarning(form: Pick<FromTextForm, 'engine' | 'lyrics' | 'instrumental' | 'duration'>,
    caps?: MusicCapabilities | null): string {
    if ((form.engine ?? DEFAULT_ENGINE) !== 'yue2' || form.instrumental) return '';
    const cap = asNumber(form.duration);
    if (!Number.isFinite(cap)) return '';
    const est = caps?.limits?.generate_from_text?.engines?.yue2?.short_cap_estimate ?? {};
    const need = estimateYue2Seconds(form.lyrics, est.seconds_per_line ?? YUE2_SECONDS_PER_LINE, est.intro_s ?? YUE2_INTRO_S);
    if (cap >= need) return '';
    return `The cap of ${Math.round(cap)} s looks short for these lyrics (estimate: about ${Math.round(need)} s). `
        + 'YuE2 stops at the cap, so the song may end before the vocals do.';
}

// -- the form ----------------------------------------------------------------------

export interface FromTextLimits {
    variants: { min: number; max: number; default: number };
    duration_s: { min: number; max: number; default: number };
    seed: { min: number; max: number };
}

/** The core's `job_schemas.py` duration limits per engine (yue2: a cap, decision 40;
 *  acestep: SPEC §12 decision 34). */
export const ENGINE_DURATION_LIMITS: Record<EngineKey, { min: number; max: number; default: number }> = {
    yue2: { min: 30, max: 360, default: 180 },
    acestep: { min: 10, max: 600, default: 120 },
};

/** The default engine's limits. */
export const DEFAULT_FROM_TEXT_LIMITS: FromTextLimits = {
    variants: { min: 1, max: 4, default: 2 },
    duration_s: ENGINE_DURATION_LIMITS[DEFAULT_ENGINE],
    seed: { min: 0, max: 2 ** 32 - 1 },
};

/** The limits the capability report gives for `engine` (the default engine when omitted),
 *  with the defaults for what it leaves out. */
export function limitsFrom(caps: MusicCapabilities | null | undefined, engine?: string): FromTextLimits {
    const l = caps?.limits?.generate_from_text ?? {};
    const key: EngineKey = engine === 'acestep' || engine === 'yue2' ? engine : defaultEngineOf(caps);
    const pick = <T extends object>(given: any, fallback: T): T =>
        given && typeof given === 'object' ? { ...fallback, ...given } : fallback;
    const perEngine = l.engines?.[key]?.duration_s;
    const fallbackDuration = ENGINE_DURATION_LIMITS[key];
    return {
        variants: pick(l.variants, DEFAULT_FROM_TEXT_LIMITS.variants),
        duration_s: perEngine ? pick(perEngine, fallbackDuration)
            : (engine ? fallbackDuration : pick(l.duration_s, fallbackDuration)),
        seed: pick(l.seed, DEFAULT_FROM_TEXT_LIMITS.seed),
    };
}

export interface FromTextForm {
    lyrics: string;
    instrumental: boolean;
    style: string;
    variants: string | number | null;
    duration: string | number | null;
    /** Empty: the runner draws one. */
    seed: string | number | null;
    /** 'yue2' (the default) or 'acestep'; a form without it is yue2. */
    engine?: EngineKey;
    /** yue2 only: 'stock' or 'rs-tw'. */
    backbone?: string;
    /** yue2 only: an adapter name, or '' for none (the default). */
    voice?: string;
    voiceMusic?: string | number | null;
    voiceSound?: string | number | null;
    /** The Prepare lyrics check (music/lyrics_prep.py) and the exact text and engine it describes;
     *  it becomes the job's `lyrics_prep` only while those still match (`lyricsPrepFor`). */
    lyricsPrep?: LyricsPrepState | null;
}

export type FormField = 'lyrics' | 'style' | 'variants' | 'duration' | 'seed' | 'engine' | 'backbone' | 'voice' | 'form';
export type FormErrors = Partial<Record<FormField, string>>;

export function emptyFromTextForm(limits: FromTextLimits = DEFAULT_FROM_TEXT_LIMITS, engine: EngineKey = DEFAULT_ENGINE): FromTextForm {
    return {
        lyrics: '', instrumental: false, style: '', variants: limits.variants.default, duration: limits.duration_s.default, seed: '',
        engine, backbone: 'stock', voice: '', voiceMusic: ADAPTER_MUSIC_DEFAULT, voiceSound: ADAPTER_SOUND_DEFAULT,
    };
}

/** The form after the engine select changed: a duration still at the old engine's default
 *  takes the new engine's default; anything the person typed stays. */
export function formForEngine(form: FromTextForm, engine: EngineKey, caps?: MusicCapabilities | null): FromTextForm {
    const before = limitsFrom(caps, form.engine ?? DEFAULT_ENGINE).duration_s.default;
    const after = limitsFrom(caps, engine).duration_s.default;
    const d = asNumber(form.duration);
    return { ...form, engine, duration: !Number.isFinite(d) || d === before ? after : form.duration };
}

function asNumber(v: string | number | null | undefined): number {
    if (v == null) return NaN;
    if (typeof v === 'number') return v;
    const t = v.trim();
    return t === '' ? NaN : Number(t);
}

/** The `params` of `propose_music_job {type: generate_from_text}`, or the errors by
 *  field. The rules are the core's (`normalize_params_from_text`); the lyrics are sent
 *  verbatim (line breaks, section tags and U+0301 stress marks stay). */
export function validateFromTextForm(form: FromTextForm, limits: FromTextLimits = DEFAULT_FROM_TEXT_LIMITS):
    { ok: true; params: Record<string, any> } | { ok: false; errors: FormErrors } {
    const errors: FormErrors = {};
    let lyrics: string | null = null;
    if (!form.instrumental) {
        if (!form.lyrics.trim()) errors.lyrics = 'The lyrics are empty: write the text, or tick Instrumental.';
        else lyrics = form.lyrics;
    }
    if (!form.style.trim()) errors.style = 'Style is required: describe the sound (genre, instruments, voice).';

    const v = asNumber(form.variants);
    const { min: vmin, max: vmax } = limits.variants;
    if (!Number.isInteger(v) || v < vmin || v > vmax) errors.variants = `Variants must be a whole number from ${vmin} to ${vmax}.`;

    const d = asNumber(form.duration);
    const { min: dmin, max: dmax } = limits.duration_s;
    if (!Number.isFinite(d) || d < dmin || d > dmax) errors.duration = `Duration must be from ${dmin} to ${dmax} seconds.`;

    let seed: number | null = null;
    const seedText = form.seed == null ? '' : typeof form.seed === 'number' ? String(form.seed) : form.seed.trim();
    if (seedText !== '') {
        const s = Number(seedText);
        const { min: smin, max: smax } = limits.seed;
        if (!/^\d+$/.test(seedText) || !Number.isSafeInteger(s) || s < smin || s > smax) {
            errors.seed = `Seed must be a whole number from ${smin} to ${smax}, or empty to draw one.`;
        } else seed = s;
    }
    const engine: EngineKey = form.engine ?? DEFAULT_ENGINE;
    const extra: Record<string, any> = {};
    if (engine === 'yue2') {
        extra.backbone = form.backbone || 'stock';
        if (form.voice) {
            const music = asNumber(form.voiceMusic);
            const sound = asNumber(form.voiceSound);
            if (![music, sound].every(x => Number.isFinite(x) && x >= 0 && x <= 2)) {
                errors.voice = 'The voice strengths must be numbers from 0 to 2.';
            } else extra.adapters = [{ name: form.voice, music, sound }];
        }
    }
    if (Object.keys(errors).length) return { ok: false, errors };
    const prep = lyricsPrepFor(form.lyricsPrep, lyrics, engine);
    if (Object.keys(prep).length) extra.lyrics_prep = prep;
    return { ok: true, params: { lyrics, style: form.style, variants: v, duration_s: d, seed, engine, ...extra } };
}

/** The form field a core `field` names (`ParamsError.field`), else 'form'. */
export function formFieldOf(coreField: string | null | undefined): FormField {
    switch (coreField) {
        case 'lyrics': return 'lyrics';
        case 'style': return 'style';
        case 'variants': return 'variants';
        case 'duration_s': return 'duration';
        case 'seed': return 'seed';
        case 'engine': return 'engine';
        case 'backbone': return 'backbone';
        case 'adapters': return 'voice';
        default: return 'form';
    }
}

// -- level matching (decision 37) ------------------------------------------------

/** ffmpeg's ebur128 never reads below this; a take at the floor is silent or shorter
 *  than one gating block, and is not matched against. */
export const LUFS_FLOOR = -70;

export function dbToGain(db: number): number {
    return Math.pow(10, db / 20);
}

/** "−1.5" with a real minus sign; "0.0" for zero. */
export function formatDb(db: number, digits = 1): string {
    const r = Number(db.toFixed(digits));
    if (r === 0) return (0).toFixed(digits);
    return `${r < 0 ? '−' : '+'}${Math.abs(r).toFixed(digits)}`;
}

export function takeLetter(index: number): string {
    return String.fromCharCode(65 + index);
}

export interface LevelMatch {
    /** True only when every take has a usable measurement. */
    matched: boolean;
    /** Per take, in order: the dB applied (0 or negative) and its linear gain (<= 1). */
    db: number[];
    gain: number[];
    /** "matched: −1.5 dB on A", or "not level-matched: ...". */
    label: string;
}

/** The display gains that play every take at the quietest one's integrated loudness:
 *  the louder ones are turned down, nothing is turned up (`volume` stops at 1). Without
 *  a usable measurement for every take nothing is changed and the label says so. */
export function levelMatch(loudness: readonly (TakeLoudness | null | undefined)[]): LevelMatch {
    const n = loudness.length;
    const flat = (label: string): LevelMatch => ({ matched: false, db: Array(n).fill(0), gain: Array(n).fill(1), label });
    if (!n) return flat('not level-matched: no takes');
    const missing = loudness.map((l, i) => (l && Number.isFinite(l.lufs_i) ? null : takeLetter(i))).filter(Boolean) as string[];
    if (missing.length) {
        return flat(`not level-matched: the loudness of ${missing.join(', ')} is not measured yet`);
    }
    const lufs = loudness.map(l => l!.lufs_i);
    const floor = lufs.map((v, i) => (v <= LUFS_FLOOR + 0.05 ? takeLetter(i) : null)).filter(Boolean) as string[];
    if (floor.length) return flat(`not level-matched: ${floor.join(', ')} is silent or too short to measure`);
    const target = Math.min(...lufs);
    const db = lufs.map(v => Math.min(0, target - v));
    const gain = db.map(dbToGain);
    const turned = db.map((d, i) => (d <= -0.05 ? `${formatDb(d)} dB on ${takeLetter(i)}` : null)).filter(Boolean);
    const label = turned.length ? `matched: ${turned.join(', ')}` : 'matched: equal loudness, no change';
    return { matched: true, db, gain, label };
}

// -- the A/B position --------------------------------------------------------------

/** Stay this far before the end, so a switch at the very end does not land on `ended`. */
export const AB_END_GUARD_S = 0.05;

/** Where the other take starts when A/B switches at `t`: the same time, clamped to the
 *  other take's length (takes can differ by a frame or more). */
export function abSwitchPosition(t: number, otherDuration: number | null | undefined): number {
    const pos = Number.isFinite(t) && t > 0 ? t : 0;
    if (typeof otherDuration !== 'number' || !Number.isFinite(otherDuration) || otherDuration <= 0) return pos;
    return Math.min(pos, Math.max(0, otherDuration - AB_END_GUARD_S));
}

/** The take a key selects: 'a'..'d' by letter; pressing the letter of the take already
 *  playing toggles back to the one before (so `B`, SPEC §9's A/B key, flips A and B). */
export function abKeyTarget(key: string, count: number, current: number, previous: number | null): number | null {
    const k = key.toLowerCase();
    if (k.length !== 1 || k < 'a' || k > 'z') return null;
    const i = k.charCodeAt(0) - 97;
    if (i >= count) return null;
    if (i === current) {
        if (previous !== null && previous !== current && previous < count) return previous;
        if (count === 2) return 1 - current;
        return null;
    }
    return i;
}

// -- the reason box (decision 35) -------------------------------------------------

export const MAX_REASON_TEXT = 2000;

/** Whether a Discard / None of these reason can be sent: text, a class, or both, the
 *  text within the core's limit. */
export function reasonCheck(text: string, reasonClass: string | null | undefined, max = MAX_REASON_TEXT): { ok: boolean; message: string } {
    const t = (text ?? '').trim();
    if (!t && !reasonClass) return { ok: false, message: 'Say why: type a reason, pick a class, or both.' };
    if (t.length > max) return { ok: false, message: `The reason is longer than ${max} characters.` };
    return { ok: true, message: '' };
}

/** The reason part of `discard_music_take` / `reject_all_music_takes`. */
export function reasonPayload(text: string, reasonClass: string | null | undefined): { reason_text?: string; reason_class?: string } {
    const out: { reason_text?: string; reason_class?: string } = {};
    const t = (text ?? '').trim();
    if (t) out.reason_text = t;
    if (reasonClass) out.reason_class = reasonClass;
    return out;
}

/** The dictionary entries offered under the input: the core's answer for the prefix,
 *  without the one already typed in full. */
export function reasonSuggestions(reasons: readonly DiscardReason[], typed: string, limit = 8): DiscardReason[] {
    const key = typed.split(/\s+/).filter(Boolean).join(' ').toLowerCase();
    return reasons.filter(r => r.key !== key).slice(0, limit);
}

// -- take info ---------------------------------------------------------------------

function clock(seconds: number): string {
    const s = Math.round(seconds);
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

/** "yue2.cpp · seed 42 · lm_seed 42 · variant 1 · batch 1, index 0 · 0:30 · −14.0 LUFS / −1.0 dBTP". */
export function takeInfo(take: MusicTake): string {
    const parts: string[] = [];
    if (take.engine) parts.push(take.engine);
    if (typeof take.seed === 'number') parts.push(`seed ${take.seed}`);
    if (typeof take.lm_seed === 'number' && take.lm_seed !== take.seed) parts.push(`lm_seed ${take.lm_seed}`);
    parts.push(`variant ${take.variant}`);
    if (typeof take.synth_batch_size === 'number') parts.push(`batch ${take.synth_batch_size}, index ${take.synth_batch_index ?? '?'}`);
    const d = take.file?.duration_s;
    if (typeof d === 'number' && Number.isFinite(d)) parts.push(clock(d));
    const l = take.loudness;
    if (l && Number.isFinite(l.lufs_i)) {
        const tp = typeof l.true_peak_dbtp === 'number' ? ` / ${formatDb(l.true_peak_dbtp)} dBTP` : '';
        parts.push(`${formatDb(l.lufs_i)} LUFS${tp}`);
    } else parts.push('loudness not measured');
    return parts.join(' · ');
}

/** The gate names in the order the review shows them, with their plain words. */
const QA_GATE_WORDS: Record<string, string> = {
    length: 'length',
    clipping: 'clipping',
    true_peak: 'true peak',
    silence: 'silence',
};

export interface TakeQaBadge {
    kind: 'passed' | 'failed' | 'unknown';
    /** "QA passed", "QA failed: clipping, silence" or "QA not measured". */
    label: string;
    /** One line per gate that failed or was not measured, for the hover text. */
    details: string[];
}

/**
 * The badge beside a take: passed, the failed gates by name, or not measured. A take with no
 * report (made before stage-1 QA) and a gate that could not be measured are never shown as a pass.
 * QA flags a take; it never rejects it, so a failure is amber text, not a block.
 */
export function takeQaBadge(take: MusicTake): TakeQaBadge {
    const report = take.qa_report;
    if (!report || typeof report !== 'object' || !report.gates) {
        return { kind: 'unknown', label: 'QA not measured', details: ['No stage-1 QA record on this take.'] };
    }
    const names = Object.keys(QA_GATE_WORDS);
    const failed = names.filter(n => report.gates[n]?.status === 'fail');
    const unknown = names.filter(n => report.gates[n]?.status !== 'pass' && report.gates[n]?.status !== 'fail');
    const line = (n: string) => `${QA_GATE_WORDS[n]}: ${report.gates[n]?.detail ?? 'not measured'}`;
    if (failed.length) {
        return { kind: 'failed', label: `QA failed: ${failed.map(n => QA_GATE_WORDS[n]).join(', ')}`, details: [...failed, ...unknown].map(line) };
    }
    if (unknown.length || report.verdict !== 'passed') {
        return { kind: 'unknown', label: 'QA not measured', details: unknown.map(line).concat(report.note ? [report.note] : []) };
    }
    return { kind: 'passed', label: 'QA passed', details: names.map(line) };
}

/** "Kept", "Discarded: muddy mix (struct)", or '' while pending. */
export function takeDecisionText(take: MusicTake): string {
    if (take.decision === 'accepted') return 'Kept';
    if (take.decision === 'rejected') {
        const why = [take.reason_text, take.reason_class ? `(${take.reason_class})` : ''].filter(Boolean).join(' ');
        return why ? `Discarded: ${why}` : 'Discarded';
    }
    return '';
}

/** Keep and Discard are offered for a pending take of a job in review (or already
 *  accepted: another take may still be kept or discarded); None of these only in review. */
export function takeDecidable(job: Pick<MusicJob, 'state'>, take: Pick<MusicTake, 'decision'>): boolean {
    return (job.state === 'review' || job.state === 'accepted') && take.decision === 'pending';
}
