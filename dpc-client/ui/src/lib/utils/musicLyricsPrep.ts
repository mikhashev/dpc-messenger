// src/lib/utils/musicLyricsPrep.ts
// The Create panel's "Prepare lyrics" (core: music/lyrics_prep.py, command prepare_music_lyrics).
// The core does the work (syllables, meter, stress marks); this module only reads its report
// for the panel: the text of each line split into words, the labels, what "Use marked lyrics"
// may do and the `lyrics_prep` record a job carries. Pure, no I/O.

export type PrepStatus =
    | 'resolved' | 'ambiguous' | 'unknown' | 'author_marked' | 'invalid_mark' | 'not_needed' | 'not_checked';

export interface PrepWord {
    key: string;
    text: string;
    start: number;
    end: number;
    script: string;
    syllables: number;
    status: PrepStatus;
    reason?: string;
    options?: string[];
    mark?: string;
    source?: string;
    audit?: string[];
}

export interface PrepLine {
    n: number;
    kind: 'lyric' | 'section';
    text: string;
    section: number;
    syllables?: number;
    words?: PrepWord[];
    notes?: string[];
    meter?: { flag: 'long' | 'short' | null; delta: number };
}

export interface PrepSection {
    index: number;
    name: string | null;
    line_count: number;
    checked: boolean;
    typical: number | null;
    tolerance: number | null;
    reason: string | null;
}

export interface PrepIssue {
    kind: string;
    line: number;
    column: number;
    word: string;
    key: string;
    message: string;
    options?: string[];
}

export interface PrepDictionary {
    state: 'ok' | 'missing' | 'error';
    message?: string | null;
    source?: string | null;
    licence?: string | null;
    built?: string | null;
    sha256?: string;
}

export interface PrepReport {
    schema: string;
    language: { requested: string; detected: string | null; effective: string | null };
    dictionary: PrepDictionary;
    engine: { key: string | null; accepts_marks: boolean; why: string };
    lines: PrepLine[];
    sections: PrepSection[];
    issues: PrepIssue[];
    summary: {
        lines: number;
        syllables: number;
        sections_checked: number;
        lines_flagged: number;
        counts: Record<string, number>;
        audit_issues: Record<string, number>;
        marks_insertable: number;
        marks_chosen: number;
        pronunciation: 'checked' | 'open_spots' | 'dictionary_missing' | 'not_applicable';
    };
    ignored_choices: string[];
    marked_text: string;
    records: { plain: Record<string, any>; marked: Record<string, any> | null };
}

/** What a job carries: the record, the exact text it describes, and the engine it was made for. */
export interface LyricsPrepState {
    sentText: string;
    engine: string;
    record: Record<string, any>;
}

export const STATUS_LABEL: Record<PrepStatus, string> = {
    resolved: 'stress found',
    ambiguous: 'ambiguous',
    unknown: 'unknown',
    author_marked: 'marked by you',
    invalid_mark: 'mark not usable',
    not_needed: 'no mark needed',
    not_checked: 'not checked',
};

/** A word the person has to look at: it ends without a stress the engine could use. */
export function needsAttention(w: PrepWord): boolean {
    return w.status === 'ambiguous' || w.status === 'unknown' || w.status === 'invalid_mark';
}

export function statusLabel(w: PrepWord): string {
    if (w.status === 'unknown' && w.reason === 'dictionary_missing') return 'unknown (no dictionary)';
    if (w.status === 'unknown' && w.reason === 'too_many_combinations') return 'unknown (too many readings)';
    if (w.status === 'resolved' && w.source === 'person') return 'your pick';
    if (w.status === 'author_marked' && w.source === 'yo') return 'marked by ё';
    if (w.status === 'not_checked' && w.reason === 'number') return 'number: reading not checked';
    return STATUS_LABEL[w.status] ?? w.status;
}

export interface Segment {
    text: string;
    word?: PrepWord;
}

/** A lyric line cut into plain stretches and words, in order. A resolved word shows its mark. */
export function lineSegments(line: PrepLine): Segment[] {
    const out: Segment[] = [];
    let at = 0;
    for (const w of line.words ?? []) {
        if (w.start > at) out.push({ text: line.text.slice(at, w.start) });
        out.push({ text: w.mark ?? w.text, word: w });
        at = w.end;
    }
    if (at < line.text.length) out.push({ text: line.text.slice(at) });
    return out;
}

/** "9 syllables" / "12 syllables, 3 over the section's 9 (long)". */
export function meterText(line: PrepLine, sections: readonly PrepSection[]): string {
    const n = line.syllables ?? 0;
    const base = `${n} ${n === 1 ? 'syllable' : 'syllables'}`;
    const flag = line.meter?.flag;
    if (!flag) return base;
    const sec = sections.find(s => s.index === line.section);
    const d = Math.abs(line.meter?.delta ?? 0);
    return `${base}, ${d} ${flag === 'long' ? 'over' : 'under'} the section's ${sec?.typical ?? '?'} (${flag})`;
}

export function dictionaryLine(d: PrepDictionary): string {
    if (d.state === 'ok') {
        const bits = [d.source ? d.source.split(',')[0] : 'Russian stress dictionary', d.licence ? d.licence.split(' (')[0] : null,
            d.built ? `built ${d.built}` : null].filter(Boolean);
        return `Russian stress dictionary: ${bits.join(', ')}`;
    }
    if (d.state === 'error') return `Russian stress dictionary unreadable: ${d.message ?? 'unknown error'}. Meter and mark checks still work.`;
    return 'Russian stress dictionary not installed: stress is not looked up. Meter and mark checks still work.';
}

function plural(n: number, one: string, many = `${one}s`): string {
    return `${n} ${n === 1 ? one : many}`;
}

/** The one-line result: lines, syllables, flags, marks, open spots. */
export function summaryText(r: PrepReport): string {
    const s = r.summary;
    const c = s.counts;
    const parts = [`${plural(s.lines, 'line')}, ${plural(s.syllables, 'syllable')}`];
    parts.push(s.lines_flagged ? `${plural(s.lines_flagged, 'line')} off the section's meter` : 'meter even');
    if (s.pronunciation === 'not_applicable') parts.push('no Russian words to check');
    else {
        parts.push(`${plural(s.marks_insertable, 'stress mark')} can be added`);
        const open = (c.ambiguous ?? 0) + (c.unknown ?? 0) + (c.invalid_mark ?? 0);
        if (open) {
            const bits = [] as string[];
            if (c.ambiguous) bits.push(`${c.ambiguous} ambiguous`);
            if (c.unknown) bits.push(`${c.unknown} unknown`);
            if (c.invalid_mark) bits.push(`${c.invalid_mark} with a mark that is not usable`);
            parts.push(`open: ${bits.join(', ')}`);
        } else if (s.pronunciation === 'checked') parts.push('no open spots');
    }
    return parts.join('; ');
}

/** Whether "Use marked lyrics" may be pressed, and the reason when not. */
export function canUseMarked(r: PrepReport | null, stale: boolean): { ok: boolean; reason: string } {
    if (!r) return { ok: false, reason: 'Prepare the lyrics first.' };
    if (stale) return { ok: false, reason: 'The lyrics changed after this check. Prepare them again.' };
    if (!r.engine.accepts_marks) return { ok: false, reason: r.engine.why };
    if (!r.records.marked || r.summary.marks_insertable === 0) return { ok: false, reason: 'No stress marks to add.' };
    return { ok: true, reason: '' };
}

export function plainState(r: PrepReport, authorText: string, engine: string): LyricsPrepState {
    return { sentText: authorText, engine, record: r.records.plain };
}

export function markedState(r: PrepReport, engine: string): LyricsPrepState | null {
    return r.records.marked ? { sentText: r.marked_text, engine, record: r.records.marked } : null;
}

/** The `lyrics_prep` a job gets: the record when it describes exactly the lyrics being sent
 *  to the engine selected now, else `{}` (an old check says nothing about changed text). */
export function lyricsPrepFor(state: LyricsPrepState | null | undefined, lyrics: string | null, engine: string): Record<string, any> {
    if (!state || lyrics == null) return {};
    return state.sentText === lyrics && state.engine === engine ? state.record : {};
}

/** `choices` after the person picked `option` for the word `key` (null clears the pick). */
export function withChoice(choices: Record<string, string>, key: string, option: string | null): Record<string, string> {
    const next = { ...choices };
    if (option === null) delete next[key];
    else next[key] = option;
    return next;
}

/** The marked text's mark count a person sees on the button: "Use marked lyrics (13 marks)". */
export function useMarkedLabel(r: PrepReport | null): string {
    const n = r?.summary.marks_insertable ?? 0;
    return n > 0 ? `Use marked lyrics (${plural(n, 'mark')})` : 'Use marked lyrics';
}

/** What "Use marked lyrics" changed in `original` to make `marked`: the U+0301 marks it inserted
 *  (indexes into `marked`) and the letters it turned into ё (indexes into `marked`, with the letter that
 *  was there). Null when `marked` is not `original` plus such changes. */
export function insertedMarks(original: string, marked: string): { marks: number[]; yo: Array<[number, string]> } | null {
    const marks: number[] = [];
    const yo: Array<[number, string]> = [];
    let i = 0;
    for (let j = 0; j < marked.length; j++) {
        const m = marked[j];
        const o = original[i];
        if (o === m) { i++; continue; }
        if (m === '\u0301') { marks.push(j); continue; }
        if ((m === 'ё' && o === 'е') || (m === 'Ё' && o === 'Е')) { yo.push([j, o]); i++; continue; }
        return null;
    }
    return i === original.length ? { marks, yo } : null;
}

/** Undo of "Use marked lyrics" that survives the person's later typing: the text as it is now with the
 *  marks and ё letters the marking added taken back out, wherever the person has not edited since.
 *  The edited span is the part of `current` between its common prefix and suffix with `marked`; a mark
 *  inside it is the person's now and stays. Unchanged text gives back `original` exactly. */
export function undoInsertedMarks(original: string, marked: string, current: string): string {
    if (current === marked) return original;
    const changes = insertedMarks(original, marked);
    if (!changes) return current;
    let pre = 0;
    while (pre < marked.length && pre < current.length && marked[pre] === current[pre]) pre++;
    let suf = 0;
    while (suf < marked.length - pre && suf < current.length - pre
        && marked[marked.length - 1 - suf] === current[current.length - 1 - suf]) suf++;
    const shift = current.length - marked.length;   // where a suffix index of `marked` sits in `current`
    const out = current.split('');
    const toCurrent = (j: number) => (j < pre ? j : j >= marked.length - suf ? j + shift : -1);
    for (const [j, letter] of changes.yo) {
        const k = toCurrent(j);
        if (k >= 0) out[k] = letter;
    }
    for (const j of [...changes.marks].sort((a, b) => b - a)) {
        const k = toCurrent(j);
        if (k >= 0) out.splice(k, 1);
    }
    return out.join('');
}

/** A line under the lyrics box when the text holds U+0301 marks and the engine is not one that reads them. */
export function stressMarkNote(lyrics: string, engine: string): string {
    if (engine === 'yue2' || !lyrics.includes('́')) return '';
    return 'The lyrics hold stress marks (U+0301). ACE-Step is not documented to read them; they are sent as written.';
}
