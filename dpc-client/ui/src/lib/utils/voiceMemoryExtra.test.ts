import { describe, it, expect } from 'vitest';
import { VOICE_MEMORY_COMMAND, voiceMemoryNote } from './voiceMemoryExtra';

describe('voiceMemoryNote', () => {
  it('names the extra and the command when the backend says it is missing', () => {
    const note = voiceMemoryNote({ voice_memory_installed: false });
    expect(note).toContain('voice-memory extra');
    expect(note).toContain('uv sync --extra voice-memory --inexact');
    expect(VOICE_MEMORY_COMMAND).toBe('uv sync --extra voice-memory --inexact');
  });

  it('says nothing when the extra is installed', () => {
    expect(voiceMemoryNote({ voice_memory_installed: true })).toBe('');
  });

  it('says nothing for an older backend that omits the field', () => {
    expect(voiceMemoryNote({})).toBe('');
  });

  it('says nothing before any status has arrived', () => {
    expect(voiceMemoryNote(null)).toBe('');
    expect(voiceMemoryNote(undefined)).toBe('');
  });
});
