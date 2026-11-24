// The note under the lyrics on what the transcript filter did (core: music/lyrics_filter.py).
//
// Two sources: the Transcribe answer's `filter` report, which lists the dropped lines, and the stored
// lyrics' `transcription.filter`, which keeps only the counts (the unfiltered transcript itself stays
// beside the lyrics on disk, last run only). Nothing here reads or changes the lyrics.

import { formatTime } from './musicPlayer';

export interface DroppedLine {
    start: number | null;
    text: string;
    reason: string;
}

export interface LyricsFilterReport {
    version?: number;
    dropped: number;
    kept_raw: boolean;
    split_by_words?: number;
    split_proportional?: number;
    words?: boolean;
    words_unavailable?: boolean;
    dropped_reasons?: Record<string, number>;
    dropped_lines?: DroppedLine[];
}

export interface FilterNote {
    /** The collapsed line, e.g. "Filtered 3 lines". */
    summary: string;
    /** One sentence per fact worth showing under it. */
    facts: string[];
    /** The dropped lines the report lists (a run's own answer only), time-stamped. */
    lines: { time: string; text: string; reason: string }[];
    /** Dropped lines the report counted but did not list. */
    more: number;
}

const REASON_TEXT: Record<string, string> = {
    phrase: 'credit or sign-off phrase',
    sound_tag: 'sound tag',
    silence: 'no speech reported',
    loop: 'looping text',
    repeat: 'repeat with impossible times',
    empty: 'empty',
};

function count(n: number, one: string, many: string): string {
    return `${n} ${n === 1 ? one : many}`;
}

function asReport(value: unknown): LyricsFilterReport | null {
    if (!value || typeof value !== 'object') return null;
    const r = value as Record<string, unknown>;
    if (typeof r.dropped !== 'number' || typeof r.kept_raw !== 'boolean') return null;
    return r as unknown as LyricsFilterReport;
}

/** The report to show: the run's own (it has the dropped lines) when the panel just ran Transcribe,
 *  else the one stored on the lyrics. Null when neither exists (lyrics from another source, or
 *  written before the filter). */
export function filterReportOf(
    info: object | null | undefined,
    lyrics: { source?: string } | null | undefined,
): LyricsFilterReport | null {
    const stored = (lyrics as { transcription?: { filter?: unknown } } | null | undefined)?.transcription;
    return asReport((info as { filter?: unknown } | null | undefined)?.filter)
        ?? (lyrics?.source === 'transcribe' ? asReport(stored?.filter) : null);
}

/** The note for a report, or null when the filter changed nothing worth saying. */
export function filterNote(report: LyricsFilterReport | null | undefined): FilterNote | null {
    if (!report) return null;
    const splits = (report.split_by_words ?? 0) + (report.split_proportional ?? 0);
    if (report.dropped <= 0 && !report.kept_raw && !report.words_unavailable && splits <= 0) return null;

    const facts: string[] = [];
    const reasons = Object.entries(report.dropped_reasons ?? {}).filter(([, n]) => n > 0);
    if (report.dropped > 0 && reasons.length) {
        facts.push(reasons.map(([why, n]) => `${n} ${REASON_TEXT[why] ?? why}`).join(', ') + '.');
    }
    if (report.kept_raw) {
        facts.push('Every line looked like noise, so the raw transcript was kept as it came.');
    }
    if (report.words_unavailable) {
        facts.push('The model gave no word timestamps, so the line times come from its segments.');
    }
    if (report.split_by_words) {
        facts.push(`${count(report.split_by_words, 'long segment was', 'long segments were')} split into lines at word times.`);
    }
    if (report.split_proportional) {
        facts.push(
            `${count(report.split_proportional, 'long segment was', 'long segments were')} split in proportion to its text: those times are approximate.`,
        );
    }
    facts.push('The unfiltered transcript is kept beside the lyrics file (the last run only).');

    const lines = (report.dropped_lines ?? []).map((l) => ({
        time: formatTime(l.start),
        text: l.text,
        reason: REASON_TEXT[l.reason] ?? l.reason,
    }));
    const summary = report.dropped > 0
        ? `Filtered ${count(report.dropped, 'line', 'lines')}`
        : report.kept_raw
            ? 'Nothing filtered: raw transcript kept'
            : 'How these lines were made';
    return { summary, facts, lines, more: Math.max(0, report.dropped - lines.length) };
}
