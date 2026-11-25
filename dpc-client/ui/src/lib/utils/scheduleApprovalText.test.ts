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
  it('says instrumental or with lyrics, and the engine, from the music block', () => {
    expect(musicJobSentence({
      about: '',
      music: { type: 'generate_from_text', style: 'slow piano', variants: 2, instrumental: true, engine: 'yue2', eta_seconds: 120 },
    })).toBe('Run a music job on the GPU: generate from text, slow piano, 2 takes, instrumental, yue2 engine, up to 2 min');
    expect(musicJobSentence({ about: '', music: { style: 's', instrumental: false, engine: null } }))
      .toBe('Run a music job on the GPU: s, with lyrics, default engine');
  });
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

describe('duration and lyrics on the music card', () => {
  it('says the length in the sentence', () => {
    expect(musicJobSentence({ about: '', music: { style: 's', duration_s: 90 } }))
      .toBe('Run a music job on the GPU: s, 90 s long');
  });
  it('adds a Lyrics row only when core sends an excerpt', () => {
    const base = { task_type: 'music_job', when: 'now', about: '' };
    const sung = approvalView({ ...base, music: { style: 's', has_lyrics: true, lyrics_excerpt: 'la la la…' } });
    expect(sung.rows.map(r => r.label)).toEqual(['Request', 'Lyrics']);
    expect(sung.rows[1].value).toBe('la la la…');
    expect(approvalView({ ...base, music: { style: 's', instrumental: true } }).rows.map(r => r.label)).toEqual(['Request']);
  });
});

describe('the card for a region edit (ADR-051 row B4)', () => {
  it('names the region, the takes and whether it holds the GPU', () => {
    expect(musicJobSentence({ about: 'x', music: { type: 'edit', action: 'regenerate_region', region_text: 'regenerate 0:02.0-0:04.0',
      track_id: 'trk_1', variants: 2, uses_gpu: true, text: 'the snare flams' } }))
      .toBe('Edit a region of track trk_1: regenerate 0:02.0-0:04.0 ("the snare flams"), 2 takes, on the GPU; '
        + 'each take is a new file, the track stays as it is');
    expect(musicJobSentence({ about: 'x', music: { type: 'edit', action: 'remove_region', region_text: 'remove 0:50.0-0:58.0',
      variants: 1, uses_gpu: false } }))
      .toBe('Edit a region: remove 0:50.0-0:58.0, 1 take, no GPU; each take is a new file, the track stays as it is');
  });
});
