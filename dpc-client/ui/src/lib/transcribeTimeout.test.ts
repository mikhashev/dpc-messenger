import { describe, it, expect } from 'vitest';
import core from './coreService.ts?raw';

// Source guard (ADR-052 L4, S3e): the UI's transcribe_audio timeout is the dictation bound (the
// backend's `[local_transcription] queue_wait_s` seed, 600 s) plus the transcription's own margin
// (240 s). The 600 is a literal copy of the backend's seed; the backend's
// test_a_queued_transcription_outlives_no_caller.py pins it to Settings, this pins the sum here.
describe('the transcribe_audio command waits for the dictation bound plus the margin', () => {
  const number = (name: string): number => {
    const m = core.match(new RegExp(`const ${name} = ([\\d_]+);`));
    expect(m).not.toBeNull();
    return Number(m![1].replace(/_/g, ''));
  };

  it('is 600 000 + 240 000 ms', () => {
    expect(number('TRANSCRIBE_QUEUE_WAIT_MS')).toBe(600_000);
    expect(number('TRANSCRIBE_WORK_MARGIN_MS')).toBe(240_000);
    expect(number('TRANSCRIBE_QUEUE_WAIT_MS') + number('TRANSCRIBE_WORK_MARGIN_MS')).toBe(840_000);
  });

  it('the transcribe_audio branch uses the sum', () => {
    expect(core).toMatch(
      /command === 'transcribe_audio'\) \{\s*timeout = TRANSCRIBE_QUEUE_WAIT_MS \+ TRANSCRIBE_WORK_MARGIN_MS;/,
    );
  });
});
