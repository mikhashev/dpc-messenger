// The pure parts of a take's QC in the review panel (core music/qc.py, `mp.qc/1`; SPEC §8.1):
// the compact judge line, whether the Judge and Measure QA buttons are offered, and the order the
// takes are shown in. QC is advice: nothing here accepts, rejects or blocks a take.

export interface TakeQcScores {
    style_match: number;
    mood: number;
    vocal_clarity: number;
    artifacts: number;
    structure: number;
    overall: number;
}

export interface TakeQcIssue {
    t_start: number;
    t_end: number;
    kind: string;
    note: string;
}

/** The part of `mp.qc/1` the review reads; the core validates the whole record. */
export interface TakeQc {
    schema?: string;
    take_id?: string;
    flow?: string;
    judge?: {
        status: 'absent' | 'done' | 'failed' | string;
        model?: string | null;
        alias?: string | null;
        error?: string | null;
        judged_at?: string | null;
    };
    /** A later judge attempt that failed, beside the earlier `done` verdict it left in place
     *  (core `qc.py`; the next good verdict clears it). */
    last_attempt?: { status?: string; error?: string | null; at?: string | null; alias?: string | null } | null;
    scores?: TakeQcScores | null;
    issues?: TakeQcIssue[];
    reasons?: string;
    confidence?: number | null;
}

/** `get_music_job_qc`: the ranking state, the configured judge alias (null: none) and the display order. */
export interface JobQc {
    ranking: { status: 'on' | 'off' | string; reason?: string };
    judge_alias: string | null;
    order: string[];
    caveat?: string;
}

export interface QcLine {
    kind: 'done' | 'failed';
    /** One short line: `Judge 7.5 / 10 (confidence 60%)` or `Judge failed: the reason`. */
    text: string;
    /** The six scores, named, for the compact row; empty unless the judge is done. */
    scores: { name: string; value: string }[];
    /** The judge's reasons and the issues with times, for a tooltip. */
    details: string[];
}

const SCORE_NAMES: [keyof TakeQcScores, string][] = [
    ['style_match', 'style'],
    ['mood', 'mood'],
    ['vocal_clarity', 'vocals'],
    ['artifacts', 'artifacts'],
    ['structure', 'structure'],
];

function num(v: unknown): v is number {
    return typeof v === 'number' && Number.isFinite(v);
}

/** A score as the review shows it: `7.5`, `8` (no trailing zero). */
export function scoreText(v: number): string {
    return String(Math.round(v * 10) / 10);
}

function issueTime(s: number): string {
    const m = Math.floor(s / 60);
    const r = s - m * 60;
    return `${m}:${r.toFixed(1).padStart(4, '0')}`;
}

/** The judge part of a take's QC as a compact line, or null while no judge has run (`absent`, or no QC). */
export function qcLine(qc: TakeQc | null | undefined): QcLine | null {
    const status = qc?.judge?.status;
    if (status === 'failed') {
        const reason = (qc?.judge?.error ?? '').trim() || 'no reason given';
        return { kind: 'failed', text: `Judge failed: ${reason}`, scores: [], details: [reason] };
    }
    if (status !== 'done' || !qc?.scores) return null;
    const s = qc.scores;
    const conf = num(qc.confidence) ? ` (confidence ${Math.round(qc.confidence * 100)}%)` : '';
    const details: string[] = [];
    if (qc.reasons) details.push(qc.reasons);
    for (const i of qc.issues ?? []) {
        details.push(`${issueTime(i.t_start)}-${issueTime(i.t_end)} ${i.kind}${i.note ? `: ${i.note}` : ''}`);
    }
    if (qc.judge?.model) details.push(`Judged by ${qc.judge.model}`);
    return {
        kind: 'done',
        text: `Judge ${scoreText(s.overall)} / 10${conf}`,
        scores: SCORE_NAMES.filter(([k]) => num(s[k])).map(([k, name]) => ({ name, value: scoreText(s[k]) })),
        details,
    };
}

/** The muted line under a done verdict when the judge's latest attempt failed (`qc.last_attempt`), or null:
 *  `Latest judge attempt failed (2026-11-26T10:00:00Z, music_flamingo): the reason`. */
export function lastAttemptLine(qc: TakeQc | null | undefined): string | null {
    const att = qc?.last_attempt;
    if (!att || qc?.judge?.status !== 'done') return null;
    const reason = (att.error ?? '').trim() || 'no reason given';
    const when = (att.at ?? '').trim();
    const who = [when, (att.alias ?? '').trim()].filter(Boolean).join(', ');
    return `Latest judge attempt failed${who ? ` (${who})` : ''}: ${reason}`;
}

/** The small error line when `get_music_job_qc` failed: the Judge button and the ranked order depend on it. */
export function qcUnavailableText(message: string | null | undefined): string {
    const reason = (message ?? '').trim() || 'no reason given';
    return `QC info unavailable: ${reason}`;
}

/** How many issues a done judge heard (a failed or absent judge has none to show). */
export function qcIssueCount(qc: TakeQc | null | undefined): number {
    return qc?.judge?.status === 'done' ? (qc.issues ?? []).length : 0;
}

/** The Judge button is offered only when the core has a judge alias configured. */
export function judgeOffered(info: Pick<JobQc, 'judge_alias'> | null | undefined): boolean {
    return !!info?.judge_alias;
}

/** The Measure QA button is offered for a take whose stage-1 QA badge says "not measured". */
export function measureQaOffered(badgeKind: string): boolean {
    return badgeKind === 'unknown';
}

/**
 * The takes in the order they are shown: the core's display order (`get_music_job_qc.order`) only
 * when ranking is on, otherwise variant order. A take the order does not name follows, in variant
 * order, so a take that arrived after the order was read is never lost.
 */
export function orderTakes<T extends { id: string; variant?: number }>(takes: T[], info: Pick<JobQc, 'ranking' | 'order'> | null | undefined): T[] {
    const byVariant = [...takes].sort((a, b) => (a.variant ?? 0) - (b.variant ?? 0));
    if (info?.ranking?.status !== 'on' || !Array.isArray(info.order)) return byVariant;
    const rank = new Map<string, number>(info.order.map((id, i) => [id, i]));
    const named = byVariant.filter(t => rank.has(t.id)).sort((a, b) => (rank.get(a.id) as number) - (rank.get(b.id) as number));
    return [...named, ...byVariant.filter(t => !rank.has(t.id))];
}

/** What a finished Measure QA call says when it did not measure: the person's line, or '' when it did. */
export function backfillProblem(result: { outcome?: string; error?: string } | undefined): string {
    if (!result) return 'The core measured nothing.';
    if (result.outcome === 'measured') return '';
    if (result.outcome === 'no_file') return 'The take has no file to measure.';
    if (result.outcome === 'skipped') return '';
    return result.error ? `QA could not be measured: ${result.error}` : 'QA could not be measured.';
}
