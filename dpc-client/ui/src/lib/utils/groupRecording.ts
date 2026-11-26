// Where a microphone recording goes in a group
// (A-GROUP-WITH-AGENTS-BUT-ONE-NODE-CANNOT-TAKE-A-RECORDING-AS-AUDIO-SO-AGENTS-NEVER-GET-THE-SOUND,
// ADR-032 Part B as amended by Mike 2026-11-26).
//
// Part B routes by node count: with more than one node the recorder offers a voice
// message and dictation; a group whose only node is this one dictated only, so its
// agents (node-less: the header says "1 members") never got the sound itself, e.g. a
// sung sample for a voice clone. Now such a group, when it has agents, offers "Send as
// audio" beside dictation, as a 1:1 agent chat does: the recording is kept in the
// group's history as an audio file (`send_group_file`), with the words typed as its
// text, so the group's agents read its marker and the bridge prints where it is kept.
// A group of one with no agent keeps dictation only.

import type { GroupChat } from '$lib/types.js';

type GroupLike = Pick<GroupChat, 'members' | 'agents'> | null | undefined;

/**
 * Whether the group has an agent in the room: an embedded or an external agent
 * registered for a node that is a member. A node that left keeps its entries in
 * the record (a re-add needs them), so `members` decides, as core's
 * `_group_participants` does; a record without members counts every entry.
 */
export function groupHasAgents(group: GroupLike): boolean {
  const agents = group?.agents;
  if (!agents) return false;
  const members = group?.members ?? [];
  return Object.entries(agents).some(([nodeId, ids]) =>
    (members.length === 0 || members.includes(nodeId)) && Array.isArray(ids) && ids.length > 0);
}

/**
 * The sink a recording in this group goes to.
 * - `voice_message`: more than one node, a voice message to the members, as before;
 * - `audio_file`: this node alone with its agents, the recording kept as the group's
 *   audio file for them (Mike 2026-11-26);
 * - `dictation`: this node alone and no agent, the words into the input box only
 *   (ADR-032 Part B, unchanged).
 * An unknown group counts as one node, as Part B always did.
 */
export type GroupRecordingRoute = 'voice_message' | 'audio_file' | 'dictation';

export function groupRecordingRoute(group: GroupLike): GroupRecordingRoute {
  if ((group?.members?.length ?? 1) > 1) return 'voice_message';
  return groupHasAgents(group) ? 'audio_file' : 'dictation';
}

/** The recorder's send button: its label and tooltip. */
export function voiceSendButton(s: { isAgentChat: boolean; sendsAudioToGroup: boolean }): { label: string; title: string } {
  if (s.isAgentChat) {
    return { label: '🎵 Send as audio', title: 'Send the recording to the agent as an audio file, with the words typed' };
  }
  if (s.sendsAudioToGroup) {
    return {
      label: '🎵 Send as audio',
      title: "Keep the recording in the group as an audio file for its agents, with the words typed; an @mention in them wakes that agent",
    };
  }
  return { label: 'Send', title: 'Send voice message' };
}

/**
 * The toast after a recording was kept as the group's audio file. Only an
 * @mention wakes a group's agent (core's `mentions_in`, code spans aside), so
 * words without one say that nobody was woken.
 */
export function groupRecordingSentNotice(text: string): string {
  const plain = (text || '').replace(/```[\s\S]*?```|`[^`\n]+`/g, '');
  return /@\w/.test(plain)
    ? 'Recording saved in the group as an audio file, for the agents you mentioned.'
    : 'Recording saved in the group as an audio file. No agent was mentioned, so none was woken; @mention one to have it listen.';
}
