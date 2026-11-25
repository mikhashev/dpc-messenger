// src/lib/services/musicJobs.ts
// The Music Player's Create panel state: jobs, takes, the engine and the capabilities
// (SPEC flow "song from own text", §5, §12 decisions 32-37; ADR-051 D30-D33).
//
// A page singleton, like services/music.ts, so a running job keeps being followed while
// the window is closed or minimised and the form keeps its draft.
//
// **How the state stays live.** The core broadcasts `music_job_updated {job}` on every
// change the runner or the person makes, and `music_engine_status {engine}` on every
// engine state change. coreService's message loop hands both to `noteMusicEvent`, which
// runs them through the pure reducer (utils/musicCreate.ts `reduceMusicEvent`). Command
// answers go through the same merge, so an answer and an event in either order end in
// the newest copy. While the panel is open (`startJobWatch`), an active job that has not
// been heard from for JOB_QUIET_MS is asked for with `get_music_job`: a fallback for a
// lost event (a reconnect, a second window), never the main path.

import { get, writable } from 'svelte/store';
import {
    backfillMusicQa,
    cancelMusicJob,
    discardMusicTake,
    getMusicCapabilities,
    getMusicEngineStatus,
    getMusicJob,
    getMusicJobQc,
    judgeMusicTake,
    keepMusicTake,
    listMusicDiscardReasons,
    listMusicJobs,
    listMusicTakes,
    measureMusicTakeLoudness,
    proposeMusicEdit,
    proposeMusicJob,
    rejectAllMusicTakes,
    startMusicEngine,
    stopLocalEngine,
    stopMusicEngine,
} from '$lib/coreService';
import { MUSIC_UNAVAILABLE_TEXT, type MusicAnswer } from '$lib/utils/musicPlayer';
import { backfillProblem, type JobQc } from '$lib/utils/musicQc';
import {
    JOB_POLL_TICK_MS,
    emptyFromTextForm,
    engineKeyOf,
    jobsToPoll,
    mergeJobs,
    reduceMusicEvent,
    upsertJob,
    type DiscardReason,
    type FromTextForm,
    type MusicCapabilities,
    type MusicEngine,
    type MusicJob,
    type MusicTake,
} from '$lib/utils/musicCreate';
import { loadMusicLibrary, noteTracksHidden } from '$lib/services/music';

export const FROM_TEXT = 'generate_from_text';
export const EDIT = 'edit';
/** The job types the Create panel lists: songs from text and region edits. */
const LISTED_TYPES = new Set([FROM_TEXT, EDIT]);

export type JobsStatus = 'idle' | 'loading' | 'ready' | 'unavailable' | 'error';

export interface MusicJobsState {
    status: JobsStatus;
    message: string;
    jobs: MusicJob[];
    /** The last engine status heard of (any engine). */
    engine: MusicEngine | null;
    /** Each engine's status by key ('yue2', 'acestep'); the panel shows the selected one. */
    engines: Record<string, MusicEngine>;
    capabilities: MusicCapabilities | null;
}

export const musicJobs = writable<MusicJobsState>({ status: 'idle', message: '', jobs: [], engine: null, engines: {}, capabilities: null });

/** The takes of the jobs the panel has opened, by job id. */
export const musicTakes = writable<Record<string, MusicTake[]>>({});

/** Each reviewed job's QC view (`get_music_job_qc`): ranking state, judge alias, display order. */
export const musicJobQc = writable<Record<string, JobQc>>({});

/** The Create form, kept while the panel or the window is closed. */
export const musicCreateDraft = writable<FromTextForm>(emptyFromTextForm());

/** The job whose takes are under review in the panel, or null. */
export const musicReviewJobId = writable<string | null>(null);

/** When each job was last heard from (an event or an answer), for the fallback poll. */
const lastSeen: Record<string, number> = {};

function seen(id: string): void {
    lastSeen[id] = Date.now();
}

/** What a job's change means beyond the list: a job that reached review registered its
 *  takes in the library; a job whose takes are open gets them read again. */
function afterJobChange(before: MusicJob | undefined, after: MusicJob): void {
    if (after.state === 'review' && before?.state !== 'review') void loadMusicLibrary(true);
    const open = get(musicTakes)[after.id];
    if (open && (before?.state !== after.state || (before?.take_ids?.length ?? 0) !== (after.take_ids?.length ?? 0))) {
        void fetchTakes(after.id);
    }
}

/** Merge a job (an answer or an event) into the list. */
export function putJob(job: MusicJob): void {
    seen(job.id);
    const s = get(musicJobs);
    const before = s.jobs.find(j => j.id === job.id);
    const jobs = upsertJob(s.jobs, job);
    if (jobs === s.jobs) return;
    musicJobs.update(x => ({ ...x, jobs }));
    afterJobChange(before, job);
}

function putTake(take: MusicTake): void {
    musicTakes.update(m => {
        const list = m[take.job_id];
        if (!list) return m;
        return { ...m, [take.job_id]: list.map(t => (t.id === take.id ? take : t)) };
    });
}

let capsTimer: ReturnType<typeof setTimeout> | null = null;

/** Read the capabilities again soon (they follow the engine's state). */
function refreshCapabilitiesSoon(): void {
    if (capsTimer) clearTimeout(capsTimer);
    capsTimer = setTimeout(() => {
        capsTimer = null;
        void refreshCapabilities();
    }, 300);
}

/** A `music_job_updated` or `music_engine_status` event from the core. */
export function noteMusicEvent(event: string, payload: any): void {
    const s = get(musicJobs);
    if (event === 'music_job_updated' && payload?.job?.id) {
        putJob(payload.job as MusicJob);
        return;
    }
    const next = reduceMusicEvent({ jobs: s.jobs, engine: s.engine, engines: s.engines }, event, payload);
    if (next.engine !== s.engine) {
        musicJobs.update(x => ({ ...x, engine: next.engine, engines: next.engines ?? x.engines }));
        refreshCapabilitiesSoon();
    }
}

export async function refreshCapabilities(): Promise<void> {
    const a = await getMusicCapabilities();
    if (a.ok) musicJobs.update(x => ({ ...x, capabilities: a.data.capabilities }));
}

function failure(a: MusicAnswer<any>): { status: JobsStatus; message: string } {
    if (a.ok) return { status: 'ready', message: '' };
    return a.unavailable ? { status: 'unavailable', message: MUSIC_UNAVAILABLE_TEXT } : { status: 'error', message: a.message };
}

/** Read the jobs, the capabilities and the engine (the panel opens, or the backend came back). */
export async function loadMusicJobs(): Promise<void> {
    musicJobs.update(x => ({ ...x, status: x.status === 'ready' ? 'ready' : 'loading', message: '' }));
    const [jobs, caps, engine] = await Promise.all([listMusicJobs(), getMusicCapabilities(), getMusicEngineStatus()]);
    const bad = [jobs, caps, engine].find(a => !a.ok);
    if (bad) {
        musicJobs.update(x => ({ ...x, ...failure(bad) }));
        return;
    }
    const list = ((jobs as any).data.jobs as MusicJob[]).filter(j => LISTED_TYPES.has(j.type));
    for (const j of list) seen(j.id);
    musicJobs.update(x => ({
        ...x,
        status: 'ready',
        message: '',
        jobs: mergeJobs(x.jobs, list),
        capabilities: (caps as any).data.capabilities ?? null,
        engine: (engine as any).data.engine ?? x.engine,
        engines: { ...x.engines, ...((engine as any).data.engines ?? {}) },
    }));
}

let watchTimer: ReturnType<typeof setInterval> | null = null;
let watchers = 0;
let polling = false;

async function pollQuietJobs(): Promise<void> {
    if (polling) return;
    polling = true;
    try {
        const ids = jobsToPoll(get(musicJobs).jobs, lastSeen, Date.now());
        for (const id of ids) {
            seen(id); // asked now: not again before the next quiet spell, answer or not
            const a = await getMusicJob(id);
            if (a.ok) putJob(a.data.job);
        }
    } finally {
        polling = false;
    }
}

/** Follow active jobs while the panel is open: the fallback poll. Returns the stop. */
export function startJobWatch(): () => void {
    watchers++;
    if (!watchTimer) watchTimer = setInterval(() => void pollQuietJobs(), JOB_POLL_TICK_MS);
    let stopped = false;
    return () => {
        if (stopped) return;
        stopped = true;
        watchers--;
        if (watchers <= 0 && watchTimer) {
            clearInterval(watchTimer);
            watchTimer = null;
            watchers = 0;
        }
    };
}

/** A region edit from the player's comment form (`propose_music_job {type: "edit"}`): the job comes
 *  back `queued` (or `unsupported` with the reason) and joins the list; its takes are reviewed in the
 *  Create panel like any other job's. */
export async function proposeEdit(args: Parameters<typeof proposeMusicEdit>[0]): Promise<MusicAnswer<{ job: MusicJob }>> {
    const a = await proposeMusicEdit(args);
    if (a.ok) putJob(a.data.job);
    return a;
}

/** Generate: `propose_music_job {type: generate_from_text}`. The job comes back `queued`,
 *  or `unsupported` with the reason; either way it is in the list. */
export async function proposeFromText(params: Record<string, any>, idempotencyKey: string): Promise<MusicAnswer<{ job: MusicJob }>> {
    const a = await proposeMusicJob(FROM_TEXT, params, idempotencyKey);
    if (a.ok) putJob(a.data.job);
    return a;
}

export async function cancelJob(jobId: string): Promise<MusicAnswer<{ job: MusicJob }>> {
    const a = await cancelMusicJob(jobId);
    if (a.ok) putJob(a.data.job);
    return a;
}

/** "Stop <holder>" on a job waiting for the GPU card: the person's press, through the
 *  existing stop_local_engine with the holder's alias. Nothing calls it by itself. The
 *  waiting job is asked for again at the next tick, since the runner sees the free card
 *  within a few seconds. A card lease (ADR-052) is not an engine child: since S2 item 8,
 *  `stop_local_engine` lets an idle lease go by its holder name and answers a busy one with what it
 *  is busy with; the job's `card_wait` holds only alias and engine, and the button needs no more
 *  (S4: the stop reaches a lease by its holder name). A running job waits too when its engine
 *  stopped under it (S4), so it is asked for again as well. */
export async function stopCardHolder(alias: string): Promise<MusicAnswer<{ alias: string }>> {
    const a = await stopLocalEngine(alias);
    for (const j of get(musicJobs).jobs) if (j.state === 'queued' || j.card_wait) lastSeen[j.id] = 0;
    return a;
}

export async function fetchTakes(jobId: string): Promise<MusicAnswer<{ takes: MusicTake[] }>> {
    const a = await listMusicTakes(jobId);
    if (a.ok) musicTakes.update(m => ({ ...m, [jobId]: a.data.takes }));
    return a;
}

export async function keepTake(takeId: string): Promise<MusicAnswer<{ take: MusicTake; job: MusicJob }>> {
    const a = await keepMusicTake(takeId);
    if (a.ok) {
        putTake(a.data.take);
        putJob(a.data.job);
    }
    return a;
}

/** Discard with a reason. The core hides the take's library entry (the file stays); the
 *  library is read again so every list drops it. */
export async function discardTake(take: MusicTake, reason: { reason_text?: string; reason_class?: string }):
    Promise<MusicAnswer<{ take: MusicTake; job: MusicJob; library_hidden: boolean; library_reason?: string }>> {
    const a = await discardMusicTake(take.id, reason);
    if (a.ok) {
        putTake(a.data.take);
        putJob(a.data.job);
        if (a.data.library_hidden && take.library_track_id) await noteTracksHidden([take.library_track_id]);
        else await loadMusicLibrary(true);
    }
    return a;
}

/** None of these: every pending take rejected with one reason; their entries hidden. */
export async function rejectAllTakes(jobId: string, reason: { reason_text?: string; reason_class?: string }):
    Promise<MusicAnswer<{ job: MusicJob; takes: (MusicTake & { library_hidden?: boolean })[] }>> {
    const a = await rejectAllMusicTakes(jobId, reason);
    if (a.ok) {
        musicTakes.update(m => ({ ...m, [jobId]: a.data.takes }));
        putJob(a.data.job);
        const hidden = a.data.takes.filter(t => t.library_hidden && t.library_track_id).map(t => t.library_track_id as string);
        if (hidden.length) await noteTracksHidden(hidden);
        else await loadMusicLibrary(true);
    }
    return a;
}

/** The Discard-reason dictionary for what is typed (most used first). */
export async function discardReasons(prefix: string): Promise<DiscardReason[]> {
    const a = await listMusicDiscardReasons(prefix.trim() || undefined, 8);
    return a.ok ? a.data.reasons : [];
}

const measuring = new Set<string>();

/** Measure a take that has no loudness yet (once at a time per take). */
export async function measureTakeLoudness(takeId: string): Promise<MusicAnswer<{ take: MusicTake; measured: boolean }> | null> {
    if (measuring.has(takeId)) return null;
    measuring.add(takeId);
    try {
        const a = await measureMusicTakeLoudness(takeId);
        if (a.ok) putTake(a.data.take);
        return a;
    } finally {
        measuring.delete(takeId);
    }
}

function putEngine(engine: MusicEngine): void {
    const key = engineKeyOf(engine);
    musicJobs.update(x => ({ ...x, engine, engines: key ? { ...x.engines, [key]: engine } : x.engines }));
}

/** Start `engine` ('yue2' or 'acestep'); the core stops the other one when it is idle. */
export async function startEngine(engine?: string, backbone?: string): Promise<MusicAnswer<{ engine: MusicEngine }>> {
    const a = await startMusicEngine(engine, backbone);
    if (a.ok) {
        putEngine(a.data.engine);
        // The other engine may have been stopped for this one: read every engine again.
        const all = await getMusicEngineStatus();
        if (all.ok) musicJobs.update(x => ({ ...x, engines: { ...x.engines, ...(all.data.engines ?? {}) } }));
    }
    return a;
}

export async function stopEngine(engine?: string): Promise<MusicAnswer<{ engine: MusicEngine; stopped: boolean }>> {
    const a = await stopMusicEngine(engine);
    if (a.ok) putEngine(a.data.engine);
    return a;
}

// -- QC of a take (core music/qc_commands.py): the judge and the stage-1 measurement -------------

/** Read a job's QC view (ranking state, judge alias, display order); the takes keep their own `qc`. */
export async function fetchJobQc(jobId: string): Promise<void> {
    const a = await getMusicJobQc(jobId);
    if (a.ok) {
        const { ranking, judge_alias, order, caveat } = a.data;
        musicJobQc.update(m => ({ ...m, [jobId]: { ranking, judge_alias: judge_alias ?? null, order, caveat } }));
    }
}

/** Judge one take with the configured audio judge. A judge that ran and failed is an ok answer whose
 *  `qc.judge.status` is `failed` (the take shows the reason); a refusal (no alias, no file) is not ok. */
export async function judgeTake(take: MusicTake): Promise<MusicAnswer<{ take_id: string; qc: any }>> {
    const a = await judgeMusicTake(take.id);
    if (a.ok) {
        putTake({ ...take, qc: a.data.qc });
        await fetchJobQc(take.job_id);
    }
    return a;
}

/** Measure a take's stage-1 QA (the backfill of a take made before QA, or whose measurement could not
 *  run). `ok: false` with the line to show when the core did not measure it. */
export async function measureTakeQa(take: MusicTake): Promise<{ ok: true } | { ok: false; message: string }> {
    const a = await backfillMusicQa({ take_id: take.id });
    if (!a.ok) return { ok: false, message: a.message };
    const problem = backfillProblem(a.data.takes.find(t => t.take_id === take.id));
    if (problem) return { ok: false, message: problem };
    await fetchTakes(take.job_id);
    await fetchJobQc(take.job_id);
    return { ok: true };
}
