/**
 * What a chat bubble shows for an audio file the agent posted (ADR-051 D40, D41),
 * as pure functions: the length, the caption, "Open in player", and whether a
 * record with no text still has something to draw.
 *
 * The attachment is the `file` shape `keep_agent_chat_attachment` writes
 * (`sha256`, `duration_seconds`), plus a `caption` that `chat_post_file` sets and
 * an optional `music` block that names the library track the file is.
 */
import type { MessageAttachment } from '$lib/types';
import { formatTime } from './musicPlayer';
import { knownDurationSeconds } from './pendingAttachment';

type AudioAttachment = Partial<Pick<MessageAttachment, 'type' | 'mime_type' | 'file_path' | 'duration_seconds' | 'voice_metadata' | 'caption' | 'music' | 'filename'>>;

/** The label for the bubble's length, "m:ss" or "h:mm:ss", or '' when the record knows
 *  none (0 is "unmeasured"): the player then reads it from the file. */
export function audioLengthLabel(attachment: AudioAttachment | null | undefined): string {
    if (!attachment) return '';
    const seconds = knownDurationSeconds(attachment as Pick<MessageAttachment, 'duration_seconds' | 'voice_metadata'>);
    return seconds > 0 ? formatTime(seconds) : '';
}

/** The caption line under the file, trimmed, or '' when there is none. */
export function attachmentCaption(attachment: AudioAttachment | null | undefined): string {
    const caption = attachment?.caption;
    return typeof caption === 'string' ? caption.trim() : '';
}

/** The library track an attachment names, or null. A blank id is none. */
export function musicTrackIdOf(attachment: AudioAttachment | null | undefined): string | null {
    const id = attachment?.music?.track_id;
    return typeof id === 'string' && id.trim() ? id.trim() : null;
}

/** Whether the bubble offers "Open in player": an audio file that plays inline here
 *  and names a library track. Without a copy on this node the file has nothing to
 *  play from, and the player streams the library's copy, so the test is the track. */
export function showsOpenInPlayer(attachment: AudioAttachment | null | undefined): boolean {
    if (!attachment || attachment.type !== 'file') return false;
    if (typeof attachment.mime_type !== 'string' || !attachment.mime_type.toLowerCase().startsWith('audio/')) return false;
    return musicTrackIdOf(attachment) !== null;
}

/** The detail parts of the file's meta line: length first when known. */
export function audioMetaParts(attachment: AudioAttachment | null | undefined): string[] {
    const length = audioLengthLabel(attachment);
    return length ? [length] : [];
}

/** The attachments of a record that can be drawn: objects with a name. An assistant
 *  record that is only a file has an empty text (`_record_answer`), and the bubble
 *  is made of this list alone, so a malformed entry must not take the rest with it. */
export function renderableAttachments<T extends { filename?: string } | null | undefined>(list: readonly T[] | null | undefined): NonNullable<T>[] {
    if (!Array.isArray(list)) return [];
    return list.filter((a): a is NonNullable<T> => !!a && typeof a === 'object' && typeof a.filename === 'string' && a.filename.length > 0);
}

/** Whether the record has anything to draw: words, or a drawable attachment. */
export function recordHasContent(record: { text?: string | null; attachments?: readonly ({ filename?: string } | null | undefined)[] | null }): boolean {
    return !!(record.text && record.text.trim()) || renderableAttachments(record.attachments).length > 0;
}

export type OpenTrackPlan = 'missing' | 'hidden' | 'already-playing' | 'play';

/** What "Open in player" does for a track: `entry` is the library entry (null when
 *  the library does not hold it), `loadedId` the track now loaded in the player. */
export function openTrackPlan(
    entry: { id: string; hidden?: boolean } | null | undefined,
    loadedId: string | null | undefined,
    playing: boolean,
): OpenTrackPlan {
    if (!entry) return 'missing';
    if (entry.hidden) return 'hidden';
    if (loadedId === entry.id && playing) return 'already-playing';
    return 'play';
}
