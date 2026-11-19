// The composer's attachment: a pasted screenshot or a file picked with the
// paperclip, held above the input until Send. A file and the words typed with
// it are one message (Mike's call, 2026-11-19), so the paperclip no longer
// opens a confirm box of its own. A message carries one attachment: the newer
// one replaces the older, and the composer says so.

import type { MessageAttachment } from '$lib/types.js';

/** A file picked with the paperclip, waiting in the composer for Send. */
export type PendingFile = {
  filePath: string;
  fileName: string;
  sizeBytes: number;
  /** A guess from the extension, for the chip only; core decides the record's own. */
  mimeType: string;
};

// The chip's guess only: the record's mime_type is core's (Python mimetypes).
const MIME_BY_EXTENSION: Record<string, string> = {
  mp3: 'audio/mpeg', wav: 'audio/wav', ogg: 'audio/ogg', oga: 'audio/ogg', opus: 'audio/opus',
  flac: 'audio/flac', m4a: 'audio/mp4', aac: 'audio/aac', weba: 'audio/webm', mid: 'audio/midi',
  midi: 'audio/midi',
  mp4: 'video/mp4', webm: 'video/webm', mov: 'video/quicktime', mkv: 'video/x-matroska',
  png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', gif: 'image/gif', webp: 'image/webp',
  svg: 'image/svg+xml',
  pdf: 'application/pdf', zip: 'application/zip', json: 'application/json',
  txt: 'text/plain', md: 'text/markdown', csv: 'text/csv', html: 'text/html',
};

export function fileNameOf(filePath: string): string {
  return filePath.split(/[\\/]/).pop() || filePath;
}

/** The MIME type a file's extension suggests, or '' when the table does not know it. */
export function mimeTypeFromName(fileName: string): string {
  const dot = fileName.lastIndexOf('.');
  if (dot <= 0 || dot === fileName.length - 1) return '';
  return MIME_BY_EXTENSION[fileName.slice(dot + 1).toLowerCase()] ?? '';
}

export function pendingFileFrom(filePath: string, sizeBytes: number): PendingFile {
  const fileName = fileNameOf(filePath);
  return { filePath, fileName, sizeBytes: Math.max(0, sizeBytes || 0), mimeType: mimeTypeFromName(fileName) };
}

/** What the chip shows for the file's type: the guessed MIME type, else its extension. */
export function pendingFileTypeLabel(file: PendingFile): string {
  if (file.mimeType) return file.mimeType;
  const dot = file.fileName.lastIndexOf('.');
  return dot > 0 && dot < file.fileName.length - 1 ? file.fileName.slice(dot + 1).toUpperCase() : 'file';
}

export function formatMegabytes(sizeBytes: number): string {
  return `${(Math.max(0, sizeBytes || 0) / (1024 * 1024)).toFixed(2)} MB`;
}

/** The toast when a newer attachment pushes out the one already waiting in the composer. */
export function replacedAttachmentNotice(replaced: { kind: 'image' | 'file'; name: string }, byName: string): string {
  const what = replaced.kind === 'image' ? 'pasted image' : 'file';
  return `The ${what} ${replaced.name} was replaced by ${byName}: a message carries one attachment.`;
}

/** Whether Send has something to send: words, an attachment, or both. */
export function composerHasContent(text: string, hasAttachment: boolean): boolean {
  return hasAttachment || text.trim().length > 0;
}

/**
 * The fields of `send_file` / `send_group_file` after the target: the file, the
 * size that scales the UI's command timeout, and the words sent with the file,
 * left out when there are none (core then records the file alone).
 */
export function fileSendFields(filePath: string, fileSizeBytes: number, text: string = ''): {
  file_path: string; file_size_bytes: number; text?: string;
} {
  const words = text.trim();
  return { file_path: filePath, file_size_bytes: fileSizeBytes, ...(words ? { text: words } : {}) };
}

/**
 * A `file` attachment that plays in the chat (ADR-051 D19: no new attachment
 * type, a file whose mime_type is audio/* renders as a player). It needs a copy
 * on this node to play from.
 */
export function playsInline(attachment: Pick<MessageAttachment, 'type' | 'mime_type' | 'file_path'>): boolean {
  return attachment.type === 'file'
    && typeof attachment.mime_type === 'string'
    && attachment.mime_type.toLowerCase().startsWith('audio/')
    && !!attachment.file_path;
}

const PLACEHOLDER_VERBS = ['Sent', 'Received'];
const PLACEHOLDER_KINDS = ['file', 'screenshot', 'voice message'];

/**
 * Whether a message's text is only core's line for an attachment sent with no
 * words, "Sent file: <name> (<MB> MB)" or "Received file: ..." (also screenshot
 * and voice message), naming one of the message's own attachments. The bubble
 * hides it: the attachment card already says all of it. The record keeps the
 * line, because the agents' history (history_body) and older nodes read it, and
 * records written before 2026-11-19 carry it too.
 */
export function isAttachmentPlaceholderText(
  text: string | undefined,
  attachments: ReadonlyArray<Pick<MessageAttachment, 'filename'>> | undefined,
): boolean {
  if (!text || !attachments?.length) return false;
  const line = text.trim();
  if (!line.endsWith(' MB)')) return false;
  return attachments.some(({ filename }) => {
    if (!filename) return false;
    return PLACEHOLDER_VERBS.some((verb) => PLACEHOLDER_KINDS.some((kind) => {
      const head = `${verb} ${kind}: ${filename} (`;
      if (!line.startsWith(head)) return false;
      const size = line.slice(head.length, -' MB)'.length);
      return /^\d+(\.\d+)?$/.test(size);
    }));
  });
}

/**
 * The length the record knows, in seconds, or 0 when it knows none: the player
 * then takes it from the audio element's loadedmetadata. Core reads no length
 * from a sent file yet (ADR-051 N12).
 */
export function knownDurationSeconds(attachment: Pick<MessageAttachment, 'duration_seconds' | 'voice_metadata'>): number {
  for (const value of [attachment.duration_seconds, attachment.voice_metadata?.duration_seconds]) {
    if (typeof value === 'number' && Number.isFinite(value) && value > 0) return value;
  }
  return 0;
}
