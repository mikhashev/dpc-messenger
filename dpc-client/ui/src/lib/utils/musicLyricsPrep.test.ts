import { describe, it, expect } from 'vitest';
import {
    canUseMarked,
    dictionaryLine,
    lineSegments,
    lyricsPrepFor,
    markedState,
    meterText,
    needsAttention,
    plainState,
    statusLabel,
    stressMarkNote,
    summaryText,
    useMarkedLabel,
    withChoice,
    type PrepLine,
    type PrepReport,
    type PrepWord,
} from './musicLyricsPrep';
import { emptyFromTextForm, validateFromTextForm } from './musicCreate';

const A = '́';

function word(over: Partial<PrepWord>): PrepWord {
    return { key: '1:0', text: 'слово', start: 0, end: 5, script: 'cyrillic', syllables: 2, status: 'not_needed', ...over };
}

function report(over: Partial<PrepReport> = {}): PrepReport {
    return {
        schema: 'dpc.lyrics_prep.report/1',
        language: { requested: 'auto', detected: 'ru', effective: 'ru' },
        dictionary: { state: 'ok', source: 'A. A. Zaliznyak, Grammatical Dictionary of Russian, 6th ed.', licence: 'CC BY-NC 4.0 (rights: x)', built: '2026-11-12', sha256: 'a'.repeat(64) },
        engine: { key: 'yue2', accepts_marks: true, why: 'YuE2 reads U+0301' },
        lines: [],
        sections: [],
        issues: [],
        summary: {
            lines: 4, syllables: 31, sections_checked: 1, lines_flagged: 1,
            counts: { resolved: 5, ambiguous: 2, unknown: 1, author_marked: 0, invalid_mark: 0, not_needed: 3, not_checked: 0 },
            audit_issues: {}, marks_insertable: 5, marks_chosen: 0, pronunciation: 'open_spots',
        },
        ignored_choices: [],
        marked_text: `Ты${A}сячи`,
        records: { plain: { engine_text: 'plain' }, marked: { engine_text: 'marked', marks_inserted: 5 } },
        ...over,
    };
}

describe('lineSegments', () => {
    it('cuts a line into plain stretches and words, showing the mark of a resolved word', () => {
        const line: PrepLine = {
            n: 2, kind: 'lyric', text: 'и дома, стекло!', section: 1, syllables: 5,
            words: [
                word({ key: '2:2', text: 'дома', start: 2, end: 6, status: 'ambiguous', options: [`до${A}ма`, `дома${A}`] }),
                word({ key: '2:8', text: 'стекло', start: 8, end: 14, status: 'resolved', mark: `стекло${A}` }),
            ],
        };
        const segs = lineSegments(line);
        expect(segs.map(s => s.text)).toEqual(['и ', 'дома', ', ', `стекло${A}`, '!']);
        expect(segs.map(s => s.word?.status ?? null)).toEqual([null, 'ambiguous', null, 'resolved', null]);
        expect(segs.map(s => s.text).join('').replace(A, '')).toBe(line.text);
    });

    it('a line with no words is its text', () => {
        expect(lineSegments({ n: 1, kind: 'lyric', text: '...', section: 0 })).toEqual([{ text: '...' }]);
    });
});

describe('labels', () => {
    it('names a status the way a person reads it', () => {
        expect(statusLabel(word({ status: 'ambiguous' }))).toBe('ambiguous');
        expect(statusLabel(word({ status: 'unknown', reason: 'dictionary_missing' }))).toBe('unknown (no dictionary)');
        expect(statusLabel(word({ status: 'resolved', source: 'person' }))).toBe('your pick');
        expect(statusLabel(word({ status: 'resolved', source: 'dictionary' }))).toBe('stress found');
        expect(statusLabel(word({ status: 'author_marked', source: 'yo' }))).toBe('marked by ё');
        expect(statusLabel(word({ status: 'not_checked', reason: 'number' }))).toBe('number: reading not checked');
    });

    it('a word needs attention when it ends without a usable stress', () => {
        expect(['ambiguous', 'unknown', 'invalid_mark'].every(s => needsAttention(word({ status: s as any })))).toBe(true);
        expect(['resolved', 'author_marked', 'not_needed', 'not_checked'].some(s => needsAttention(word({ status: s as any })))).toBe(false);
    });

    it('says how far a line is off its section', () => {
        const sections = [{ index: 1, name: 'verse', line_count: 4, checked: true, typical: 8, tolerance: 2, reason: null }];
        const ok: PrepLine = { n: 2, kind: 'lyric', text: 'a', section: 1, syllables: 8, meter: { flag: null, delta: 0 } };
        const long: PrepLine = { n: 3, kind: 'lyric', text: 'a', section: 1, syllables: 11, meter: { flag: 'long', delta: 3 } };
        const short: PrepLine = { n: 4, kind: 'lyric', text: 'a', section: 1, syllables: 4, meter: { flag: 'short', delta: -4 } };
        expect(meterText(ok, sections)).toBe('8 syllables');
        expect(meterText(long, sections)).toBe("11 syllables, 3 over the section's 8 (long)");
        expect(meterText(short, sections)).toBe("4 syllables, 4 under the section's 8 (short)");
        expect(meterText({ ...ok, syllables: 1, meter: undefined }, sections)).toBe('1 syllable');
    });

    it('describes the dictionary and its absence', () => {
        expect(dictionaryLine(report().dictionary)).toBe('Russian stress dictionary: A. A. Zaliznyak, CC BY-NC 4.0, built 2026-11-12');
        expect(dictionaryLine({ state: 'missing', message: 'x' })).toContain('not installed');
        expect(dictionaryLine({ state: 'missing' })).toContain('Meter and mark checks still work');
        expect(dictionaryLine({ state: 'error', message: 'bad file' })).toContain('unreadable: bad file');
    });

    it('sums the report in one line', () => {
        expect(summaryText(report())).toBe(
            "4 lines, 31 syllables; 1 line off the section's meter; 5 stress marks can be added; open: 2 ambiguous, 1 unknown");
        const clean = report({ summary: { ...report().summary, lines_flagged: 0, counts: { ...report().summary.counts, ambiguous: 0, unknown: 0 }, pronunciation: 'checked', marks_insertable: 1 } });
        expect(summaryText(clean)).toBe('4 lines, 31 syllables; meter even; 1 stress mark can be added; no open spots');
        const none = report({ summary: { ...report().summary, pronunciation: 'not_applicable' } });
        expect(summaryText(none)).toContain('no Russian words to check');
    });
});

describe('Use marked lyrics', () => {
    it('is allowed for YuE2 with marks to add and a fresh check', () => {
        expect(canUseMarked(report(), false)).toEqual({ ok: true, reason: '' });
        expect(useMarkedLabel(report())).toBe('Use marked lyrics (5 marks)');
    });

    it('says why it is not: no check, a stale check, ACE-Step, nothing to add', () => {
        expect(canUseMarked(null, false).reason).toMatch(/Prepare the lyrics first/);
        expect(canUseMarked(report(), true).reason).toMatch(/changed after this check/);
        const ace = report({ engine: { key: 'acestep', accepts_marks: false, why: 'acestep is not documented to read stress marks' } });
        expect(canUseMarked(ace, false)).toEqual({ ok: false, reason: 'acestep is not documented to read stress marks' });
        const none = report({ summary: { ...report().summary, marks_insertable: 0 }, records: { plain: {}, marked: null } });
        expect(canUseMarked(none, false).reason).toBe('No stress marks to add.');
        expect(useMarkedLabel(none)).toBe('Use marked lyrics');
        expect(useMarkedLabel(null)).toBe('Use marked lyrics');
    });

    it('a pick is added to the choices and cleared with null', () => {
        const one = withChoice({}, '1:2', `дома${A}`);
        expect(one).toEqual({ '1:2': `дома${A}` });
        expect(withChoice(one, '1:9', 'x')).toEqual({ '1:2': `дома${A}`, '1:9': 'x' });
        expect(withChoice(one, '1:2', null)).toEqual({});
        expect(one).toEqual({ '1:2': `дома${A}` }); // not changed in place
    });

    it('warns about marks only where the engine is not documented to read them', () => {
        expect(stressMarkNote(`ко${A}шка`, 'yue2')).toBe('');
        expect(stressMarkNote('кошка', 'acestep')).toBe('');
        expect(stressMarkNote(`ко${A}шка`, 'acestep')).toMatch(/ACE-Step is not documented/);
    });
});

describe('the job params', () => {
    const base = { ...emptyFromTextForm(), style: 'folk', lyrics: 'Тысячи' };

    it('carries the record only while it describes the text and engine being sent', () => {
        const r = report();
        const marked = markedState(r, 'yue2')!;
        expect(marked).toEqual({ sentText: `Ты${A}сячи`, engine: 'yue2', record: r.records.marked });
        const sent = validateFromTextForm({ ...base, lyrics: `Ты${A}сячи`, lyricsPrep: marked });
        expect(sent.ok && sent.params.lyrics_prep).toEqual({ engine_text: 'marked', marks_inserted: 5 });
        // the text was edited after the check: nothing is claimed
        const edited = validateFromTextForm({ ...base, lyrics: `Ты${A}сячи!`, lyricsPrep: marked });
        expect(edited.ok && 'lyrics_prep' in edited.params).toBe(false);
        // another engine selected since: nothing is claimed
        const ace = validateFromTextForm({ ...base, engine: 'acestep', lyrics: `Ты${A}сячи`, lyricsPrep: marked });
        expect(ace.ok && 'lyrics_prep' in ace.params).toBe(false);
    });

    it('a check without Use records the plain text', () => {
        const plain = plainState(report(), 'Тысячи', 'yue2');
        const v = validateFromTextForm({ ...base, lyricsPrep: plain });
        expect(v.ok && v.params.lyrics_prep).toEqual({ engine_text: 'plain' });
        expect(lyricsPrepFor(plain, 'Тысячи', 'yue2')).toEqual({ engine_text: 'plain' });
    });

    it('an instrumental or a form with no check sends no lyrics_prep', () => {
        const plain = plainState(report(), 'Тысячи', 'yue2');
        const inst = validateFromTextForm({ ...base, instrumental: true, lyricsPrep: plain });
        expect(inst.ok && 'lyrics_prep' in inst.params).toBe(false);
        const none = validateFromTextForm(base);
        expect(none.ok && 'lyrics_prep' in none.params).toBe(false);
        expect(lyricsPrepFor(null, 'x', 'yue2')).toEqual({});
        expect(markedState(report({ records: { plain: {}, marked: null } }), 'yue2')).toBeNull();
    });
});
