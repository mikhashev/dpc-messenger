// The line that sits beside a voice or memory toggle when the backend says the
// optional `voice-memory` extra (torch, transformers, sentence-transformers, faiss,
// librosa) is not installed. The text lives here once; every toggle reads it.

/** What adds the extra, run from `dpc-client/core`; the same command the backend logs. */
export const VOICE_MEMORY_COMMAND = 'uv sync --extra voice-memory --inexact';

/**
 * The note for a toggle, or '' when there is nothing to say. Only an explicit
 * `false` speaks: a backend older than the field omits it and must show nothing,
 * and so must a status that has not arrived yet.
 */
export function voiceMemoryNote(
  status: { voice_memory_installed?: boolean } | null | undefined,
): string {
  if (status?.voice_memory_installed !== false) return '';
  return `Needs the voice-memory extra (not installed). In dpc-client/core run: ${VOICE_MEMORY_COMMAND}`;
}
