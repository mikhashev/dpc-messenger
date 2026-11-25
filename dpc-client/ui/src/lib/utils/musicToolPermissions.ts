/**
 * The "Music" category of the agent permissions panel (ADR-051 D42, D43): the nine
 * `music_*` tools and `chat_post_file`, with their names as the backend registers them
 * (`dpc_agent/tools/music.py`, `tools/chat_files.py`).
 *
 * Each is off by default (`default_enabled=False`). Since ADR-053 each asks the owner when
 * someone else asked for the turn (`owner_approval_for_others`); they were owner-only before
 * (ADR-051 D43). The panel draws that as a badge.
 */

export interface PermissionTool {
  key: string;
  label: string;
  desc: string;
  isDanger?: boolean;
}

export interface PermissionCategory {
  name: string;
  /** Asks the owner on someone else's request (the registry's `owner_approval_for_others`;
   *  before ADR-053, `owner_only`). */
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

/** The badge beside such a category, and what it means (ADR-053). */
export const OWNER_ONLY_BADGE = 'asks you for others';
export const OWNER_ONLY_HINT =
  'Runs as switched on when you ask. When anyone else asks (a peer, a friend in a group, a Telegram stranger), every call waits for your approval card first; no answer in 60 s is a refusal.';

/** The tool keys of the Music category, in the order the panel lists them. */
export function musicToolKeys(): string[] {
  return MUSIC_TOOL_CATEGORY.tools.map((t) => t.key);
}

/**
 * Whether a category's badge shows. The backend's `list_all_tools` carries
 * `owner_approval_for_others` per tool (ADR-053; `owner_only` on an older backend): when it
 * does for any of the category's tools, the badge shows if every tool the backend reports
 * for it carries it. When neither field is there or none of the tools is listed, the
 * category's own hardcoded `ownerOnly` stands.
 */
export function categoryOwnerOnly(
  category: PermissionCategory,
  registered: ReadonlyArray<{ name: string; owner_only?: boolean; owner_approval_for_others?: boolean }>,
): boolean {
  const keys = new Set(category.tools.map((t) => t.key));
  const flagOf = (t: { owner_only?: boolean; owner_approval_for_others?: boolean }) =>
    typeof t.owner_approval_for_others === 'boolean' ? t.owner_approval_for_others : t.owner_only;
  const flags = registered
    .filter((t) => keys.has(t.name) && typeof flagOf(t) === 'boolean')
    .map((t) => flagOf(t) as boolean);
  if (flags.length === 0) return !!category.ownerOnly;
  return flags.every(Boolean);
}
