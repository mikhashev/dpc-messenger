import { describe, it, expect, vi, afterEach } from 'vitest';
import {
  AGENT_SEND_CONFIRM_TIMEOUT_MS, HeldSendTimers, bubbleAttachmentFor, composerAfterLateConfirmation,
  composerAfterRefusal, composerAfterTimeout, confirmedAttachments, fileButtonEnabled, fileButtonTitle,
  lateConfirmationNoticeFor, pickedFileAttachment, recordingAttachment, refusalNoticeFor,
  takesAgentAttachments, unconfirmedNoticeFor,
  type ComposerState, type FileButtonState,
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

// Zcode's review, 2026-11-20: a refusal from core (too long, too large, the wrong
// format) used to lose the words and the attachment, cleared at send.
describe('composerAfterRefusal: a refused send gives back what the person typed and attached', () => {
  type F = { filePath: string };
  type R = { filePath?: string; duration: number };
  const file: F = { filePath: '/home/x/song.mp3' };
  const recording: R = { filePath: '/home/x/.dpc/temp/voice_rec.wav', duration: 12 };
  const empty = (over: Partial<ComposerState<F, R>> = {}): ComposerState<F, R> => ({
    activeChatId: 'agent_001', currentInput: '', drafts: new Map(), pendingFile: null,
    pendingImage: null, voicePreview: null, ...over,
  });

  it('the words and the picked file come back to the open chat', () => {
    const next = composerAfterRefusal(empty(), { chatId: 'agent_001', text: 'listen', bubbleId: 'b1', file });
    expect(next.currentInput).toBe('listen');
    expect(next.pendingFile).toBe(file);
    expect(next.attachmentRestored).toBe(true);
  });

  it('the recording comes back as the preview', () => {
    const next = composerAfterRefusal(empty(), { chatId: 'agent_001', text: '', bubbleId: 'b1', recording });
    expect(next.voicePreview).toBe(recording);
    expect(next.attachmentRestored).toBe(true);
    expect(next.currentInput).toBe('');
  });

  it('words typed since stay, after the returned ones', () => {
    const next = composerAfterRefusal(empty({ currentInput: 'and also' }),
      { chatId: 'agent_001', text: 'listen', bubbleId: 'b1', file });
    expect(next.currentInput).toBe('listen\nand also');
  });

  it('a file or image attached since keeps its place; the refused file does not push it out', () => {
    const other: F = { filePath: '/home/x/other.pdf' };
    const withFile = composerAfterRefusal(empty({ pendingFile: other }), { chatId: 'agent_001', text: 'x', bubbleId: 'b1', file });
    expect(withFile.pendingFile).toBe(other);
    expect(withFile.attachmentRestored).toBe(false);
    const withImage = composerAfterRefusal(empty({ pendingImage: { dataUrl: 'data:' } }), { chatId: 'agent_001', text: 'x', bubbleId: 'b1', file });
    expect(withImage.pendingFile).toBeNull();
    expect(withImage.attachmentRestored).toBe(false);
  });

  it('a chat left meanwhile gets the words in its draft, and the open chat is untouched', () => {
    const c = empty({ activeChatId: 'agent_002', currentInput: 'other chat', drafts: new Map([['agent_001', 'later']]) });
    const next = composerAfterRefusal(c, { chatId: 'agent_001', text: 'listen', bubbleId: 'b1', recording });
    expect(next.drafts.get('agent_001')).toBe('listen\nlater');
    expect(next.currentInput).toBe('other chat');
    expect(next.voicePreview).toBeNull();
    expect(next.attachmentRestored).toBe(false);
    expect(c.drafts.get('agent_001')).toBe('later'); // the state given is not mutated
  });

  it('the notice says why and where the message went', () => {
    expect(refusalNoticeFor('Recording too long (400 s > 300 s limit)', true, true))
      .toBe('Not sent: Recording too long (400 s > 300 s limit). Your words and the attachment are back in the composer.');
    expect(refusalNoticeFor('File too large', false, true)).toContain('attach the file again');
    expect(refusalNoticeFor('', false, false)).toBe('Not sent: core refused it. Your words are back in the draft.');
  });
});

// Zcode's review of 3f2c6600: core accepted the send and died, so neither the
// confirmation nor an ERROR came, and the held send and its bubble waited for ever.
describe('HeldSendTimers: a held send not confirmed in time is given back', () => {
  afterEach(() => { vi.useRealTimers(); });

  it('the bound covers a cold agent start, not just the record write', () => {
    expect(AGENT_SEND_CONFIRM_TIMEOUT_MS).toBe(180_000);
  });

  it('fires once after the bound, and forgets the send before calling back', () => {
    vi.useFakeTimers();
    const timers = new HeldSendTimers(1000);
    const expired: string[] = [];
    timers.arm('cmd-1', (id) => { expired.push(id); expect(timers.size).toBe(0); });
    vi.advanceTimersByTime(999);
    expect(expired).toEqual([]);
    vi.advanceTimersByTime(1);
    expect(expired).toEqual(['cmd-1']);
    vi.advanceTimersByTime(10_000);
    expect(expired).toEqual(['cmd-1']);
  });

  it('a confirmation or an answer in time settles it: nothing fires', () => {
    vi.useFakeTimers();
    const timers = new HeldSendTimers(1000);
    const onExpire = vi.fn();
    timers.arm('cmd-1', onExpire);
    timers.arm('cmd-2', onExpire);
    vi.advanceTimersByTime(500);
    timers.settle('cmd-1');
    timers.settle('unknown'); // harmless
    vi.advanceTimersByTime(1000);
    expect(onExpire).toHaveBeenCalledTimes(1);
    expect(onExpire).toHaveBeenCalledWith('cmd-2');
  });

  it('the panel going away stops every timer', () => {
    vi.useFakeTimers();
    const timers = new HeldSendTimers(1000);
    const onExpire = vi.fn();
    timers.arm('a', onExpire);
    timers.arm('b', onExpire);
    timers.clear();
    expect(timers.size).toBe(0);
    vi.advanceTimersByTime(5000);
    expect(onExpire).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('arming the same id again restarts it rather than firing twice', () => {
    vi.useFakeTimers();
    const timers = new HeldSendTimers(1000);
    const onExpire = vi.fn();
    timers.arm('a', onExpire);
    vi.advanceTimersByTime(800);
    timers.arm('a', onExpire);
    vi.advanceTimersByTime(800);
    expect(onExpire).not.toHaveBeenCalled();
    vi.advanceTimersByTime(200);
    expect(onExpire).toHaveBeenCalledTimes(1);
  });
});

describe('a timed-out send, and a confirmation that comes after it', () => {
  type F = { filePath: string };
  type R = { filePath?: string; duration: number };
  const file: F = { filePath: '/home/x/song.mp3' };
  const recording: R = { filePath: '/home/x/.dpc/temp/voice_rec.wav', duration: 12 };
  const empty = (over: Partial<ComposerState<F, R>> = {}): ComposerState<F, R> => ({
    activeChatId: 'agent_001', currentInput: '', drafts: new Map(), pendingFile: null,
    pendingImage: null, voicePreview: null, ...over,
  });

  it('the timeout gives back what a refusal gives back, and remembers it', () => {
    const { next, restore } = composerAfterTimeout(empty({ currentInput: 'p.s.' }),
      { chatId: 'agent_001', text: 'listen', bubbleId: 'b1', file });
    expect(next.currentInput).toBe('listen\np.s.');
    expect(next.pendingFile).toBe(file);
    expect(restore).toMatchObject({ textBefore: 'p.s.', textAfter: 'listen\np.s.', file, recording: null });
  });

  it('a late confirmation takes the untouched draft back out: nothing is sent twice', () => {
    const { next, restore } = composerAfterTimeout(empty({ currentInput: 'p.s.' }),
      { chatId: 'agent_001', text: 'listen', bubbleId: 'b1', file });
    const after = composerAfterLateConfirmation(next, restore);
    expect(after.draftCleared).toBe(true);
    expect(after.currentInput).toBe('p.s.');
    expect(after.drafts.get('agent_001')).toBe('p.s.');
    expect(after.pendingFile).toBeNull();
  });

  it('the recording given back goes too (core has already moved its file)', () => {
    const { next, restore } = composerAfterTimeout(empty(), { chatId: 'agent_001', text: '', bubbleId: 'b1', recording });
    expect(restore.recording).toBe(recording);
    const after = composerAfterLateConfirmation(next, restore);
    expect(after.draftCleared).toBe(true);
    expect(after.voicePreview).toBeNull();
  });

  it("an edited draft stays the person's, words or attachment", () => {
    const { next, restore } = composerAfterTimeout(empty(), { chatId: 'agent_001', text: 'listen', bubbleId: 'b1', file });
    const edited = composerAfterLateConfirmation({ ...next, currentInput: 'listen carefully' }, restore);
    expect(edited.draftCleared).toBe(false);
    expect(edited.currentInput).toBe('listen carefully');
    expect(edited.pendingFile).toBe(file);
    const fileRemoved = composerAfterLateConfirmation({ ...next, pendingFile: null }, restore);
    expect(fileRemoved.draftCleared).toBe(false);
    expect(fileRemoved.currentInput).toBe('listen');
  });

  it('an attachment that could not go back is not touched by the late confirmation', () => {
    const other: F = { filePath: '/home/x/other.pdf' };
    const { next, restore } = composerAfterTimeout(empty({ pendingFile: other }),
      { chatId: 'agent_001', text: 'listen', bubbleId: 'b1', file });
    expect(restore.file).toBeNull();
    const after = composerAfterLateConfirmation(next, restore);
    expect(after.draftCleared).toBe(true);
    expect(after.currentInput).toBe('');
    expect(after.pendingFile).toBe(other);
  });

  it('a chat left meanwhile: its draft is put back as it was, the open chat untouched', () => {
    const c = empty({ activeChatId: 'agent_002', currentInput: 'here', drafts: new Map([['agent_001', 'later']]) });
    const { next, restore } = composerAfterTimeout(c, { chatId: 'agent_001', text: 'listen', bubbleId: 'b1', recording });
    expect(next.drafts.get('agent_001')).toBe('listen\nlater');
    const after = composerAfterLateConfirmation(next, restore);
    expect(after.draftCleared).toBe(true);
    expect(after.drafts.get('agent_001')).toBe('later');
    expect(after.currentInput).toBe('here');
  });

  it('the person went back to the chat and left the draft as it was: still cleared', () => {
    const c = empty({ activeChatId: 'agent_002' });
    const { next, restore } = composerAfterTimeout(c, { chatId: 'agent_001', text: 'listen', bubbleId: 'b1', file });
    // switching chats loads the draft into the input
    const back = { ...next, activeChatId: 'agent_001', currentInput: next.drafts.get('agent_001') ?? '' };
    const after = composerAfterLateConfirmation(back, restore);
    expect(after.draftCleared).toBe(true);
    expect(after.currentInput).toBe('');
  });

  it('the notices say what happened', () => {
    expect(unconfirmedNoticeFor(180_000, true, true))
      .toBe('Core did not confirm the message within 180 s; your draft is back, with the attachment.');
    expect(unconfirmedNoticeFor(180_000, false, true)).toContain('attach the file again');
    expect(unconfirmedNoticeFor(30_000, false, false)).toBe('Core did not confirm the message within 30 s; your draft is back.');
    expect(lateConfirmationNoticeFor(true)).toContain('cleared');
    expect(lateConfirmationNoticeFor(false)).toContain('your edited draft stays');
  });
});
