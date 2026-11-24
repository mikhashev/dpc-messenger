import { describe, it, expect } from 'vitest';
import { approvalView, musicJobSentence, formatEta } from './scheduleApprovalText';
import dialog from '../components/ScheduleApprovalDialog.svelte?raw';

// What the backend sends for a music_job (tools/music.py, _await_schedule_approval):
// task_type "music_job", when "now (a music job on the GPU)", about a sentence.
const backendMusic = {
  task_type: 'music_job',
  when: 'now (a music job on the GPU)',
  about: 'Generate music on the GPU: 2 variant(s), instrumental, the default engine, about 120 s on the GPU. Style: slow piano',
};

describe('the schedule card for a check_back', () => {
  const v = approvalView({ task_type: 'check_back', when: 'in 1200s', about: 'look at the job' });
  it('keeps its own words', () => {
    expect(v.isMusicJob).toBe(false);
    expect(v.headText).toBe('wants to come back to this later');
    expect(v.approveLabel).toBe('Schedule it');
    expect(v.refuseLabel).toBe('Not now');
    expect(v.rows.map(r => r.label)).toEqual(['When', 'What it will do']);
    expect(v.rows[0].value).toBe('in 1200s');
  });
  it('says "(no description given)" for an empty about', () => {
    expect(approvalView({ task_type: 'check_back', when: 'immediately', about: '' }).rows[1].value).toBe('(no description given)');
  });
});

describe('the card for a music_job', () => {
  const v = approvalView(backendMusic);
  it('does not say "come back later" or "Schedule it"', () => {
    expect(v.isMusicJob).toBe(true);
    expect(v.headText).not.toMatch(/come back|later/i);
    expect(v.approveLabel).toBe('Approve');
    expect(v.refuseLabel).toBe('Refuse');
    expect(JSON.stringify(v)).not.toContain('Schedule it');
  });
  it('shows the request, and no When row', () => {
    expect(v.rows.map(r => r.label)).toEqual(['Request']);
    expect(v.rows[0].value).toBe(backendMusic.about);
  });
  it('builds the sentence from the structured fields when the backend sends them', () => {
    expect(musicJobSentence({
      about: backendMusic.about,
      music: { type: 'generate_from_text', style: 'slow piano', variants: 2, eta_seconds: 120 },
    })).toBe('Run a music job on the GPU: generate from text, slow piano, 2 takes, about 2 min');
  });
  it('uses the singular for one take and seconds under two minutes', () => {
    expect(musicJobSentence({ about: '', music: { style: 'drums', variants: 1, eta_seconds: 45 } }))
      .toBe('Run a music job on the GPU: drums, 1 take, about 45 s');
  });
  it('falls back to the about sentence, then to a plain line', () => {
    expect(musicJobSentence({ about: backendMusic.about })).toBe(backendMusic.about);
    expect(musicJobSentence({ about: '  ' })).toBe('Run a music job on the GPU');
    expect(musicJobSentence({ about: 'x', music: {} })).toBe('x');
  });
  it('ignores a variants or eta that is not a usable number', () => {
    expect(musicJobSentence({ about: '', music: { style: 's', variants: 0, eta_seconds: Number.NaN } }))
      .toBe('Run a music job on the GPU: s');
  });
  it('formats the ETA', () => {
    expect(formatEta(0)).toBe('');
    expect(formatEta(undefined)).toBe('');
    expect(formatEta(119)).toBe('119 s');
    expect(formatEta(300)).toBe('5 min');
  });
});

describe('the dialog reads the view, not fixed words', () => {
  it('draws its head, rows and buttons from approvalView', () => {
    expect(dialog).toContain("import { approvalView } from '$lib/utils/scheduleApprovalText';");
    expect(dialog).toContain('{view.headText}');
    expect(dialog).toContain('{#each view.rows as row}');
    expect(dialog).toContain('{view.approveLabel}');
    expect(dialog).toContain('{view.refuseLabel}');
    expect(dialog).not.toContain('Schedule it');
  });
});
