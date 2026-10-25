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

/**
 * A group send's answer when some connected members were not offered the file.
 *
 * `send_group_file`, `send_group_image` and `send_group_voice_message` answer
 * success with the offers they made in `transfer_ids`. A connected member the
 * backend could not offer the file to is a row in `not_offered` (`reason` is
 * `max_concurrent_transfers` for the upload limit, `send_error` with its own
 * `detail` for anything else); when the limit was hit, `upload_limit.sentence`
 * says what holds the slots. The sentence is the backend's, the same one a
 * one-to-one send at the limit raises. A send that offered everyone carries
 * none of these.
 */
export interface NotOfferedRow {
    node_id: string;
    name?: string | null;
    reason: string;
    detail?: string;
}

export interface GroupSendResult {
    transfer_ids?: string[];
    not_offered?: NotOfferedRow[];
    upload_limit?: { limit: number; sentence: string };
}

const UPLOAD_LIMIT_REASON = 'max_concurrent_transfers';
const TOAST_NAMES = 3;

/**
 * The warning for a group send that left members out, or null when it offered
 * everyone connected. `toast` is short; `text` is the whole account and belongs
 * in the chat, where the 5-second toast cannot lose it.
 */
export function notOfferedNotice(
    result: GroupSendResult | null | undefined,
    nameOf: (nodeId: string) => string | undefined,
): { toast: string; text: string } | null {
    const rows = result?.not_offered;
    if (!result || !Array.isArray(rows) || rows.length === 0) return null;

    const who = (row: NotOfferedRow) => nameOf(row.node_id) || row.name || row.node_id;
    const offered = Array.isArray(result.transfer_ids) ? result.transfer_ids.length : 0;
    const names = rows.map(who);
    const limit = result.upload_limit;
    const hitLimit = rows.some((r) => r.reason === UPLOAD_LIMIT_REASON);

    const parts = [`Not offered to ${rows.length} of ${offered + rows.length} connected members: ${names.join(', ')}.`];
    if (hitLimit) {
        parts.push(limit?.sentence || 'Upload limit reached.');
    }
    for (const row of rows) {
        if (row.reason !== UPLOAD_LIMIT_REASON) {
            parts.push(`${who(row)}: ${row.detail || row.reason}.`);
        }
    }

    const shown = names.slice(0, TOAST_NAMES).join(', ') + (names.length > TOAST_NAMES ? ` and ${names.length - TOAST_NAMES} more` : '');
    const reasons = new Set<string>();
    for (const row of rows) {
        reasons.add(row.reason === UPLOAD_LIMIT_REASON
            ? (limit ? `upload limit of ${limit.limit} reached` : 'upload limit reached')
            : (row.detail || row.reason));
    }
    return {
        toast: `Not sent to ${shown}: ${[...reasons].join('; ')}. Details in the chat.`,
        text: parts.join(' '),
    };
}
