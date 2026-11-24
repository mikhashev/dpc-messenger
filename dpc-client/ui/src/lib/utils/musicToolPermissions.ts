/**
 * The "Music" category of the agent permissions panel (ADR-051 D42, D43): the nine
 * `music_*` tools and `chat_post_file`, with their names as the backend registers them
 * (`dpc_agent/tools/music.py`, `tools/chat_files.py`).
 *
 * Each is off by default (`default_enabled=False`) and owner-only: it is neither offered
 * nor run outside the owner's turn, so a peer, or a group with a foreign node in it,
 * never gets one even when the toggle is on. The panel draws that as a badge.
 */

export interface PermissionTool {
  key: string;
  label: string;
  desc: string;
  isDanger?: boolean;
}

export interface PermissionCategory {
  name: string;
  /** Offered and run only in the owner's turns (the registry's `owner_only`). */
  ownerOnly?: boolean;
  isDanger?: boolean;
  tools: PermissionTool[];
}

export const MUSIC_TOOL_CATEGORY: PermissionCategory = {
  name: 'Music Tools (generate and play music)',
  ownerOnly: true,
  tools: [
    { key: 'music_capabilities', label: 'Capabilities', desc: 'What the music service can do now: engines, limits, job params' },
    { key: 'music_propose_job', label: 'Propose Job', desc: 'Propose one music generation job on the GPU; you approve each one first' },
    { key: 'music_check', label: 'Check Job', desc: 'One look at a music job: its state and, when done, its takes' },
    { key: 'music_wait', label: 'Wait for Job', desc: 'Wait for a music job to finish (blocking, up to 15 minutes a turn)' },
    { key: 'music_cancel_job', label: 'Cancel Job', desc: 'Cancel a music job this agent proposed' },
    { key: 'music_list_takes', label: 'List Takes', desc: "A job's takes: id, length, loudness, file" },
    { key: 'music_list_library', label: 'List Library', desc: "The music library's tracks and your playlists (read-only)" },
    { key: 'music_get_lyrics', label: 'Get Lyrics', desc: "A library track's lyrics from what DPC stores locally (never asks the internet)" },
    { key: 'music_playlist_add', label: 'Add to Playlist', desc: 'Add library tracks to the end of one of your playlists' },
    { key: 'chat_post_file', label: 'Post File in Chat', desc: "Post a file (an audio take plays in the message) into the agent's own 1:1 chat, when you ask for it" },
  ],
};

/** The badge beside an owner-only category, and what it means. */
export const OWNER_ONLY_BADGE = 'owner-only';
export const OWNER_ONLY_HINT =
  'Offered and run only in your own turns. A peer, or a group with another node in it, never gets these tools, even when they are on.';

/** The tool keys of the Music category, in the order the panel lists them. */
export function musicToolKeys(): string[] {
  return MUSIC_TOOL_CATEGORY.tools.map((t) => t.key);
}
