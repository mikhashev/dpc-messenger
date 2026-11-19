import { describe, it, expect } from 'vitest';
import {
  bubbleAttachmentFor, confirmedAttachments, fileButtonEnabled, fileButtonTitle, pickedFileAttachment,
  recordingAttachment, takesAgentAttachments, type FileButtonState,
} from './agentChatAttachment';

// IN-AN-AGENT-CHAT-THE-MICROPHONE-ONLY-DICTATES-SO-A-HUMMED-MELODY-NEVER-REACHES-THE-AGENT-AS-AUDIO
// (ADR-051 Phase 0): which chats offer "Send as audio" and an enabled paperclip.

describe('takesAgentAttachments: which chats offer Send as audio', () => {
  it('a 1:1 agent chat does', () => {
    expect(takesAgentAttachments('agent_001', true)).toBe(true);
    // a chat mapped to an agent, or an AI chat whose provider is dpc_agent
    expect(takesAgentAttachments('ai_chat_7', true)).toBe(true);
  });

  it('a plain AI chat and local_ai do not: no agent there to open a file', () => {
    expect(takesAgentAttachments('local_ai', false)).toBe(false);
    expect(takesAgentAttachments('ai_chat_7', false)).toBe(false);
  });

  it('a group never does, whatever its agents (ADR-032 Part B is unchanged)', () => {
    expect(takesAgentAttachments('group-60aff6110f02', true)).toBe(false);
    expect(takesAgentAttachments('group-60aff6110f02', false)).toBe(false);
  });

  it('a P2P chat, a Telegram chat and no chat do not', () => {
    expect(takesAgentAttachments('dpc-node-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', false)).toBe(false);
    expect(takesAgentAttachments('telegram-123', true)).toBe(false);
    expect(takesAgentAttachments('', true)).toBe(false);
  });
});

const state = (over: Partial<FileButtonState>): FileButtonState => ({
  chatId: 'agent_001', isAgentChat: true, connected: true, isLoading: false,
  isPeerConnected: false, isTelegramChat: false, isSleeping: false, ...over,
});

describe('fileButtonEnabled: the paperclip per chat kind', () => {
  it('is enabled in an agent chat, which has no peer to be connected to', () => {
    expect(fileButtonEnabled(state({}))).toBe(true);
    expect(fileButtonTitle(state({}))).toBe('Attach a file for the agent');
  });

  it('is disabled in an agent chat while the agent sleeps, or while loading, or offline', () => {
    expect(fileButtonEnabled(state({ isSleeping: true }))).toBe(false);
    expect(fileButtonEnabled(state({ isLoading: true }))).toBe(false);
    expect(fileButtonEnabled(state({ connected: false }))).toBe(false);
  });

  it('stays disabled in a plain AI chat, local_ai and the default chat', () => {
    for (const chatId of ['local_ai', 'ai_chat_7', 'default']) {
      expect(fileButtonEnabled(state({ chatId, isAgentChat: false, isPeerConnected: true }))).toBe(false);
    }
  });

  it('in a P2P chat it follows the peer connection, as before', () => {
    const peer = 'dpc-node-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
    expect(fileButtonEnabled(state({ chatId: peer, isAgentChat: false, isPeerConnected: true }))).toBe(true);
    expect(fileButtonEnabled(state({ chatId: peer, isAgentChat: false, isPeerConnected: false }))).toBe(false);
    expect(fileButtonTitle(state({ chatId: peer, isAgentChat: false, isPeerConnected: false }))).toBe('Peer disconnected');
    expect(fileButtonTitle(state({ chatId: peer, isAgentChat: false, isPeerConnected: true }))).toBe('Send file');
  });

  it('in a group it is enabled and says Send file, as before', () => {
    const g = state({ chatId: 'group-60aff6110f02', isAgentChat: false, isPeerConnected: true });
    expect(fileButtonEnabled(g)).toBe(true);
    expect(fileButtonTitle(g)).toBe('Send file');
  });

  it('a Telegram chat keeps its paperclip', () => {
    expect(fileButtonEnabled(state({ chatId: 'telegram-1', isAgentChat: false, isTelegramChat: true }))).toBe(true);
  });
});

describe('the attachment execute_ai_query carries', () => {
  it('a picked file is its path', () => {
    expect(pickedFileAttachment('C:\\Users\\x\\song.mp3')).toEqual({ kind: 'file', file_path: 'C:\\Users\\x\\song.mp3' });
  });

  it('a desktop recording is its temp path, not its bytes', () => {
    const a = recordingAttachment('audio/wav', 20, { filePath: '/home/x/.dpc/temp/voice_1.wav' });
    expect(a).toEqual({ kind: 'voice', mime_type: 'audio/wav', duration_seconds: 20, file_path: '/home/x/.dpc/temp/voice_1.wav' });
  });

  it('a browser recording is its bytes, with the format the recorder chose', () => {
    const a = recordingAttachment('audio/mp4', 3, { audioBase64: 'AAAA' });
    expect(a).toEqual({ kind: 'voice', mime_type: 'audio/mp4', duration_seconds: 3, audio_base64: 'AAAA' });
  });

  it('an unknown length is 0, not NaN', () => {
    const a = recordingAttachment('', Number.NaN, { audioBase64: 'AAAA' });
    expect(a.kind === 'voice' && a.duration_seconds).toBe(0);
    expect(a.kind === 'voice' && a.mime_type).toBe('audio/webm');
  });
});

describe('the bubble before core confirms', () => {
  it('a picked file shows its name and guessed type, and no path to play from yet', () => {
    const b = bubbleAttachmentFor(pickedFileAttachment('/home/x/song.mp3'), 4096);
    expect(b).toMatchObject({ type: 'file', filename: 'song.mp3', mime_type: 'audio/mpeg', size_bytes: 4096 });
    expect(b.file_path).toBeUndefined();
  });

  it('a recording shows its type without codec parameters and its length', () => {
    const b = bubbleAttachmentFor(recordingAttachment('audio/webm;codecs=opus', 12, { audioBase64: 'AAAA' }), 100);
    expect(b).toMatchObject({ type: 'file', mime_type: 'audio/webm', duration_seconds: 12 });
    expect(b.file_path).toBeUndefined();
  });

  it('the confirmation brings the kept copy, or nothing', () => {
    const kept = [{ type: 'file', filename: 'voice_1.wav', size_bytes: 9, file_path: '/h/.dpc/conversations/agent_001/files/voice_1.wav' }];
    expect(confirmedAttachments({ msg_index: 3, attachments: kept })).toEqual(kept);
    expect(confirmedAttachments({ msg_index: 3 })).toBeNull();
    expect(confirmedAttachments({ msg_index: 3, attachments: [] })).toBeNull();
    expect(confirmedAttachments(null)).toBeNull();
  });
});
