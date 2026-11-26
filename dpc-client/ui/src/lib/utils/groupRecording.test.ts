import { describe, it, expect } from 'vitest';
import {
  groupHasAgents, groupRecordingRoute, groupRecordingSentNotice, voiceSendButton,
} from './groupRecording';
import chatPanel from '../panels/ChatPanel.svelte?raw';
import fileTransferUI from '../components/FileTransferUI.svelte?raw';

// A-GROUP-WITH-AGENTS-BUT-ONE-NODE-CANNOT-TAKE-A-RECORDING-AS-AUDIO-SO-AGENTS-NEVER-GET-THE-SOUND
// (Mike 2026-11-26): the AI Studio group is this node alone with its agents, and a sung
// sample recorded there could only be dictated. ADR-032 Part B, as amended.

const ME = 'dpc-node-' + 'a'.repeat(32);
const PEER = 'dpc-node-' + 'b'.repeat(32);
const GONE = 'dpc-node-' + 'c'.repeat(32);

describe('groupRecordingRoute: where a recording in a group goes', () => {
  it('a group of this node alone with its agents keeps it as an audio file for them', () => {
    expect(groupRecordingRoute({ members: [ME], agents: { [ME]: ['agent_001'] } })).toBe('audio_file');
  });

  it('an external agent (the bridge) counts as an agent of the group', () => {
    expect(groupRecordingRoute({ members: [ME], agents: { [ME]: ['ext:CC_windows'] } })).toBe('audio_file');
  });

  it('a group of one with no agent still dictates only (ADR-032 Part B holds there)', () => {
    expect(groupRecordingRoute({ members: [ME] })).toBe('dictation');
    expect(groupRecordingRoute({ members: [ME], agents: {} })).toBe('dictation');
    expect(groupRecordingRoute({ members: [ME], agents: { [ME]: [] } })).toBe('dictation');
  });

  it('the agents of a node that left the group do not count', () => {
    expect(groupHasAgents({ members: [ME], agents: { [GONE]: ['agent_009'] } })).toBe(false);
    expect(groupRecordingRoute({ members: [ME], agents: { [GONE]: ['agent_009'] } })).toBe('dictation');
  });

  it('a group with more than one node sends a voice message, agents or not, as before', () => {
    expect(groupRecordingRoute({ members: [ME, PEER] })).toBe('voice_message');
    expect(groupRecordingRoute({ members: [ME, PEER], agents: { [ME]: ['agent_001'] } })).toBe('voice_message');
  });

  it('an unknown group counts as one node with no agent', () => {
    expect(groupRecordingRoute(undefined)).toBe('dictation');
    expect(groupRecordingRoute(null)).toBe('dictation');
  });
});

describe('voiceSendButton: the recorder offers Send as audio in such a group', () => {
  it('a group of one with agents says Send as audio and that an @mention wakes the agent', () => {
    const b = voiceSendButton({ isAgentChat: false, sendsAudioToGroup: true });
    expect(b.label).toBe('🎵 Send as audio');
    expect(b.title).toContain('@mention');
  });

  it('a 1:1 agent chat and a voice message keep their words', () => {
    expect(voiceSendButton({ isAgentChat: true, sendsAudioToGroup: false })).toEqual({
      label: '🎵 Send as audio', title: 'Send the recording to the agent as an audio file, with the words typed',
    });
    expect(voiceSendButton({ isAgentChat: false, sendsAudioToGroup: false })).toEqual({
      label: 'Send', title: 'Send voice message',
    });
  });
});

describe('groupRecordingSentNotice', () => {
  it('names the mentioned agents when the words carry an @mention', () => {
    expect(groupRecordingSentNotice('@Ark my voice sample')).toContain('for the agents you mentioned');
  });

  it('says nobody was woken when they carry none, or only one inside code', () => {
    for (const text of ['', 'my voice sample', 'see `@Ark`']) {
      expect(groupRecordingSentNotice(text)).toContain('none was woken');
    }
  });
});

describe('the panel routes by it', () => {
  it('the send button shows for a group of one with agents', () => {
    expect(chatPanel).toContain("return groupRecordingRoute($groupChats.get(chatId)) !== 'dictation';");
    expect(chatPanel).not.toContain('(get(groupChats).get(chatId)?.members?.length ?? 1) > 1');
  });

  it('Send keeps the recorder file as the group file, with the words typed, and dictation stays a button', () => {
    expect(chatPanel).toContain("if (route === 'audio_file' && voicePreview.filePath) {");
    expect(chatPanel).toContain('result = await sendGroupFile(chatId, recorded, text);');
    expect(chatPanel).toContain("if (route === 'dictation') {");
    expect(chatPanel).toContain('{sendsAudioToGroup}');
    expect(fileTransferUI).toContain('voiceSendButton({ isAgentChat, sendsAudioToGroup }).label');
  });
});
