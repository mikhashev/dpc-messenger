/**
 * What the sender is told, and where, when a receiver refuses a file.
 *
 * The receiver's firewall puts its sentence in FILE_CANCEL as `denial_reason`
 * (which rule, which size limit, which type). The backend passes it on in the
 * `file_transfer_cancelled` event; a 5-second toast alone loses it, so the same
 * sentence also lands in the chat as a system line.
 */

export interface TransferCancelledEvent {
    transfer_id: string;
    reason: string;
    filename?: string;
    node_id?: string;
    direction?: string;
    group_id?: string;
    denial_reason?: string;
}

function sentenceOf(event: TransferCancelledEvent): string {
    return typeof event.denial_reason === 'string' ? event.denial_reason.trim() : '';
}

/** `reason`, with the receiver's sentence behind it when it sent one. */
export function cancelReasonText(event: TransferCancelledEvent): string {
    const detail = sentenceOf(event);
    return detail ? `${event.reason}: ${detail}` : event.reason;
}

/**
 * The chat line for a refusal of an upload, or null when there is nothing to add:
 * only an upload the peer refused with a stated reason gets a line (a local
 * cancel, or a refusal from an older peer that sent no sentence, keeps the toast
 * alone). The line goes to the group chat when the transfer was part of a group
 * send, otherwise to the peer's chat.
 */
export function refusalNotice(
    event: TransferCancelledEvent,
    nameOf: (nodeId: string) => string | undefined,
): { chatId: string; text: string } | null {
    const detail = sentenceOf(event);
    if (event.direction !== 'upload' || !detail) return null;
    const chatId = event.group_id || event.node_id;
    if (!chatId) return null;
    const who = (event.node_id && nameOf(event.node_id)) || event.node_id || 'the peer';
    const file = event.filename && event.filename !== 'unknown' ? event.filename : 'the file';
    return { chatId, text: `Not sent: ${who} refused ${file} (${detail})` };
}
