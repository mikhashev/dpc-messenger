// A recording or a file sent into a 1:1 agent chat (ADR-051 Phase 0,
// IN-AN-AGENT-CHAT-THE-MICROPHONE-ONLY-DICTATES-SO-A-HUMMED-MELODY-NEVER-REACHES-THE-AGENT-AS-AUDIO).
// The microphone used to dictate only and the paperclip was disabled there, so a
// hummed melody never reached the agent as audio. Now, after recording, the
// person picks Dictate (the words go to the input box, as before) or Send as
// audio, and the paperclip attaches a file. Both travel as `execute_ai_query`'s
// `attachment`; core keeps the file in the chat's files/ folder and the agent
// reads where it is kept. A group is not one of these chats: with one node and
// its agents it still dictates (ADR-032 Part B), and that rule is Mike's to change.

import type { MessageAttachment } from '$lib/types.js';
import { fileNameOf, mimeTypeFromName } from './pendingAttachment';

/** What `execute_ai_query` carries as `attachment` (core: keep_agent_chat_attachment). */
export type AgentChatAttachment =
  | { kind: 'file'; file_path: string }
  | {
      kind: 'voice';
      mime_type: string;
      duration_seconds: number;
      file_path?: string;
      audio_base64?: string;
    };

/**
 * Whether this chat takes a recording as audio and a file from the paperclip
 * for its agent: a 1:1 agent chat (`isAgentChat`, the panel's mirror of core's
 * is_agent_conversation), never a group or a Telegram chat.
 */
export function takesAgentAttachments(chatId: string, isAgentChat: boolean): boolean {
  if (!chatId || !isAgentChat) return false;
  return !chatId.startsWith('group-') && !chatId.startsWith('telegram-');
}

export type FileButtonState = {
  chatId: string;
  isAgentChat: boolean;
  connected: boolean;
  isLoading: boolean;
  isPeerConnected: boolean;
  isTelegramChat: boolean;
  /** An agent asleep: its chat takes no message, so no attachment either. */
  isSleeping?: boolean;
};

function isAiChatWithoutAgent(chatId: string): boolean {
  return chatId === 'local_ai' || chatId.startsWith('ai_') || chatId.startsWith('agent_') || chatId === 'default';
}

/** Whether the paperclip can be pressed in this chat. */
export function fileButtonEnabled(s: FileButtonState): boolean {
  if (!s.connected || s.isLoading) return false;
  if (takesAgentAttachments(s.chatId, s.isAgentChat)) return !s.isSleeping;
  if (isAiChatWithoutAgent(s.chatId)) return false;
  return s.isPeerConnected || s.isTelegramChat;
}

/** The paperclip's tooltip in this chat. */
export function fileButtonTitle(s: FileButtonState): string {
  if (takesAgentAttachments(s.chatId, s.isAgentChat)) return 'Attach a file for the agent';
  if (s.isPeerConnected || s.isTelegramChat || s.chatId.startsWith('agent_') || s.chatId === 'default') return 'Send file';
  return 'Peer disconnected';
}

/** The attachment for a file picked with the paperclip. */
export function pickedFileAttachment(filePath: string): AgentChatAttachment {
  return { kind: 'file', file_path: filePath };
}

/**
 * The attachment for a recording sent as audio. The desktop recorder leaves a
 * WAV in ~/.dpc/temp and names it (`filePath`), and core moves it from there;
 * the browser recorder has only the bytes, sent as base64.
 */
export function recordingAttachment(
  mimeType: string, durationSeconds: number, source: { filePath?: string; audioBase64?: string },
): AgentChatAttachment {
  const base = {
    kind: 'voice' as const,
    mime_type: mimeType || 'audio/webm',
    duration_seconds: Number.isFinite(durationSeconds) && durationSeconds > 0 ? durationSeconds : 0,
  };
  return source.filePath ? { ...base, file_path: source.filePath } : { ...base, audio_base64: source.audioBase64 ?? '' };
}

/**
 * What the bubble shows until core confirms the record: the name, type and size,
 * no path. The player needs the kept copy under ~/.dpc (the asset scope), and the
 * confirmation brings it.
 */
export function bubbleAttachmentFor(
  attachment: AgentChatAttachment, sizeBytes: number,
): MessageAttachment {
  if (attachment.kind === 'file') {
    const filename = fileNameOf(attachment.file_path);
    return {
      type: 'file', filename, size_bytes: Math.max(0, sizeBytes || 0),
      mime_type: mimeTypeFromName(filename) || undefined, status: 'sending',
    };
  }
  return {
    type: 'file', filename: 'Recording', size_bytes: Math.max(0, sizeBytes || 0),
    mime_type: attachment.mime_type.split(';')[0], status: 'sending',
    ...(attachment.duration_seconds > 0 ? { duration_seconds: attachment.duration_seconds } : {}),
  };
}

/** The kept attachments a confirmation brings, or null when it brings none. */
export function confirmedAttachments(confirmed: unknown): MessageAttachment[] | null {
  const list = (confirmed as { attachments?: unknown } | null | undefined)?.attachments;
  return Array.isArray(list) && list.length > 0 ? (list as MessageAttachment[]) : null;
}

/**
 * A message with an attachment sent into an agent chat, held until core answers:
 * core may refuse it (too long, too large, the wrong format, a missing file), and
 * a refusal must not lose what the person typed and attached. Cleared on
 * `user_message_confirmed` for its command, restored on an ERROR before that.
 */
export type HeldAgentSend<F, R> = {
  chatId: string;
  text: string;
  /** The optimistic bubble drawn at send, removed on a refusal. */
  bubbleId: string;
  file?: F | null;
  recording?: R | null;
};

export type ComposerState<F, R> = {
  activeChatId: string;
  currentInput: string;
  drafts: Map<string, string>;
  pendingFile: F | null;
  pendingImage: unknown | null;
  voicePreview: R | null;
};

function joinDraft(held: string, now: string): string {
  if (!held) return now;
  if (!now.trim()) return held;
  return `${held}\n${now}`;
}

/**
 * The composer after core refused a held send. The words go back first in the
 * chat they were typed in (before anything typed since); the file or recording
 * goes back when that chat is open and its attachment place is free. A chat
 * left meanwhile has dropped its attachment place, so there only the words return.
 */
export function composerAfterRefusal<F, R>(
  c: ComposerState<F, R>, held: HeldAgentSend<F, R>,
): ComposerState<F, R> & { attachmentRestored: boolean } {
  const drafts = new Map(c.drafts);
  if (held.chatId !== c.activeChatId) {
    drafts.set(held.chatId, joinDraft(held.text, drafts.get(held.chatId) ?? ''));
    return { ...c, drafts, attachmentRestored: false };
  }
  const currentInput = joinDraft(held.text, c.currentInput);
  drafts.set(held.chatId, currentInput);
  let { pendingFile, voicePreview } = c;
  let attachmentRestored = false;
  if (held.file && pendingFile === null && c.pendingImage === null) {
    pendingFile = held.file;
    attachmentRestored = true;
  } else if (held.recording && voicePreview === null) {
    voicePreview = held.recording;
    attachmentRestored = true;
  }
  return { ...c, currentInput, drafts, pendingFile, voicePreview, attachmentRestored };
}

/** The toast after a refusal: why, and where the message went. */
export function refusalNoticeFor(reason: string, attachmentRestored: boolean, hadAttachment: boolean): string {
  const why = reason || 'core refused it';
  if (attachmentRestored) return `Not sent: ${why}. Your words and the attachment are back in the composer.`;
  if (hadAttachment) return `Not sent: ${why}. Your words are back in the draft; attach the file again.`;
  return `Not sent: ${why}. Your words are back in the draft.`;
}
