import { describe, expect, it } from 'vitest';
import { backfillProblem, judgeOffered, lastAttemptLine, measureQaOffered, orderTakes, qcIssueCount, qcLine, qcUnavailableText, type TakeQc } from './musicQc';

const done = (over: Partial<TakeQc> = {}): TakeQc => ({
    judge: { status: 'done', model: 'flamingo', alias: 'music_flamingo', error: null },
    scores: { style_match: 8, mood: 7.5, vocal_clarity: 6, artifacts: 9, structure: 7, overall: 7.5 },
    issues: [{ t_start: 41.2, t_end: 43, kind: 'artifact', note: 'smear on the snare' }],
    reasons: 'Warm guitars as asked.',
    confidence: 0.6,
    ...over,
});

describe('qcLine', () => {
    it('is null while no judge has run', () => {
        expect(qcLine(null)).toBeNull();
        expect(qcLine(undefined)).toBeNull();
        expect(qcLine({ judge: { status: 'absent' }, scores: null })).toBeNull();
    });
    it('shows the overall score, the confidence and the named scores of a done judge', () => {
        const line = qcLine(done())!;
        expect(line.kind).toBe('done');
        expect(line.text).toBe('Judge 7.5 / 10 (confidence 60%)');
        expect(line.scores.map(s => `${s.name} ${s.value}`)).toEqual(['style 8', 'mood 7.5', 'vocals 6', 'artifacts 9', 'structure 7']);
        expect(line.details[0]).toBe('Warm guitars as asked.');
        expect(line.details).toContain('0:41.2-0:43.0 artifact: smear on the snare');
        expect(line.details).toContain('Judged by flamingo');
    });
    it('leaves the confidence out when the judge gave none', () => {
        expect(qcLine(done({ confidence: null }))!.text).toBe('Judge 7.5 / 10');
    });
    it('shows a failed judge with its reason, and never a score', () => {
        const line = qcLine({ judge: { status: 'failed', error: 'the answer was not the rubric JSON' }, scores: null })!;
        expect(line).toMatchObject({ kind: 'failed', text: 'Judge failed: the answer was not the rubric JSON', scores: [] });
        expect(qcLine({ judge: { status: 'failed', error: ' ' } })!.text).toBe('Judge failed: no reason given');
    });
});

describe('qcIssueCount', () => {
    it('counts the issues of a done judge only', () => {
        expect(qcIssueCount(done())).toBe(1);
        expect(qcIssueCount({ judge: { status: 'failed', error: 'x' }, issues: [] })).toBe(0);
        expect(qcIssueCount(null)).toBe(0);
    });
});

describe('the buttons', () => {
    it('offers Judge only with a configured alias', () => {
        expect(judgeOffered({ judge_alias: 'music_flamingo' })).toBe(true);
        expect(judgeOffered({ judge_alias: null })).toBe(false);
        expect(judgeOffered(null)).toBe(false);
    });
    it('offers Measure QA only for a take that is not measured', () => {
        expect(measureQaOffered('unknown')).toBe(true);
        expect(measureQaOffered('passed')).toBe(false);
        expect(measureQaOffered('failed')).toBe(false);
    });
});

describe('orderTakes', () => {
    const takes = [{ id: 'a', variant: 2 }, { id: 'b', variant: 1 }, { id: 'c', variant: 3 }];
    it('keeps variant order when ranking is off or unknown', () => {
        const off = { ranking: { status: 'off' }, order: ['c', 'a', 'b'] };
        expect(orderTakes(takes, off).map(t => t.id)).toEqual(['b', 'a', 'c']);
        expect(orderTakes(takes, null).map(t => t.id)).toEqual(['b', 'a', 'c']);
    });
    it('follows the core order when ranking is on, with unnamed takes after it in variant order', () => {
        const on = { ranking: { status: 'on' }, order: ['c', 'b'] };
        expect(orderTakes(takes, on).map(t => t.id)).toEqual(['c', 'b', 'a']);
    });
    it('does not change the list it is given', () => {
        const copy = takes.map(t => t.id);
        orderTakes(takes, { ranking: { status: 'on' }, order: ['c', 'a', 'b'] });
        expect(takes.map(t => t.id)).toEqual(copy);
    });
});

describe('backfillProblem', () => {
    it('is empty when the take was measured', () => {
        expect(backfillProblem({ outcome: 'measured' })).toBe('');
    });
    it('names a missing file, an error and an empty answer', () => {
        expect(backfillProblem({ outcome: 'no_file' })).toBe('The take has no file to measure.');
        expect(backfillProblem({ outcome: 'failed', error: 'QaError: no ffmpeg' })).toBe('QA could not be measured: QaError: no ffmpeg');
        expect(backfillProblem(undefined)).toBe('The core measured nothing.');
    });
});

describe('lastAttemptLine and qcUnavailableText (follow-up 16, Zcode #188 finding 7)', () => {
    it('shows a failed later attempt beside a done verdict, with its reason, time and alias', () => {
        const qc = done({ last_attempt: { status: 'failed', error: 'the card stayed busy', at: '2026-11-26T10:00:00Z', alias: 'music_flamingo' } });
        expect(lastAttemptLine(qc)).toBe('Latest judge attempt failed (2026-11-26T10:00:00Z, music_flamingo): the card stayed busy');
        expect(qcLine(qc)!.kind).toBe('done');          // the verdict itself is unchanged
    });
    it('has no line without an attempt, and none unless a verdict is done', () => {
        expect(lastAttemptLine(done())).toBeNull();
        expect(lastAttemptLine(null)).toBeNull();
        expect(lastAttemptLine({ judge: { status: 'failed', error: 'x' }, last_attempt: { status: 'failed', error: 'x' } })).toBeNull();
    });
    it('copes with a missing reason, time and alias', () => {
        expect(lastAttemptLine(done({ last_attempt: { status: 'failed' } }))).toBe('Latest judge attempt failed: no reason given');
    });
    it('words the unavailable QC info with its reason', () => {
        expect(qcUnavailableText('not_found: no job')).toBe('QC info unavailable: not_found: no job');
        expect(qcUnavailableText('  ')).toBe('QC info unavailable: no reason given');
        expect(qcUnavailableText(null)).toBe('QC info unavailable: no reason given');
    });
});
