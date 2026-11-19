// An attachment whose file is not on this node, and the request for it.
//
// History sync carries records, never bytes: a record synced from another node
// can name a file on that node's disk. Core marks such an attachment
// `available: false` when it serves the history (managers/file_request.py), and
// the chat then shows "Not on this node" and a "Request file" button instead of
// a player, an image or "Show in folder" (Mike's call, 2026-11-19). An attachment
// whose file is here carries no such field and renders exactly as before: no
// button, no label.
//
// The button sends `request_attachment`; core asks the peers online, the author
// first (FILE_REQUEST), and reports through `file_request_status` events. When
// the file arrives, the ordinary file event completes the record in place.

import type { MessageAttachment } from '$lib/types.js';

/** What a request is doing now, as core's events and command answer say it. */
export type AttachmentRequestState =
    | 'requesting'   // the peers online are being asked
    | 'receiving'    // a peer offered the file; the transfer runs
    | 'received'     // the file is here; the record carries its path
    | 'not_held'     // every peer asked answered that it does not have the file
    | 'no_answer'    // nobody offered it, and a peer did not answer (an older version)
    | 'no_peers'     // nobody who may hold the file is online
    | 'failed'       // an offer came, and the transfer ended without the file
    | 'error';       // the command itself was refused

export type AttachmentRequest = {
    state: AttachmentRequestState;
    requestId?: string;
    transferId?: string;
    reason?: string;
};

/** Whether core said this attachment's file is not on this node. */
export function isMissingHere(attachment: Partial<MessageAttachment> | null | undefined): boolean {
    return attachment?.available === false;
}

const RETRYABLE: ReadonlyArray<AttachmentRequestState> = ['not_held', 'no_answer', 'no_peers', 'failed', 'error'];

/**
 * Whether the "Request file" button is drawn: only beside an attachment whose
 * file is missing here, and only while no request for it is under way. An
 * attachment whose file is here never gets one, whatever a request says.
 */
export function showsRequestButton(
    attachment: Partial<MessageAttachment> | null | undefined,
    request?: AttachmentRequest | null,
): boolean {
    if (!isMissingHere(attachment)) return false;
    return !request || RETRYABLE.includes(request.state);
}

/** The key a request is kept under: one chat, one record, one attachment. */
export function requestKey(conversationId: string, messageId: string, filename: string = ''): string {
    return `${conversationId}\u0000${messageId}\u0000${filename}`;
}

/** The line under a missing attachment's name. `percent` is the transfer's progress, when known. */
export function requestStatusText(request: AttachmentRequest | null | undefined, percent?: number): string {
    if (!request) return 'Not on this node';
    switch (request.state) {
        case 'requesting':
            return 'Requesting…';
        case 'receiving':
            return typeof percent === 'number' && Number.isFinite(percent)
                ? `Receiving… ${Math.max(0, Math.min(100, Math.round(percent)))}%`
                : 'Receiving…';
        case 'received':
            return 'Received';
        case 'not_held':
            return 'Nobody online has this file';
        case 'no_answer':
            return 'Nobody online answered (a peer may run an older version)';
        case 'no_peers':
            return 'No member of this chat is online';
        case 'failed':
            return request.reason ? `The transfer failed: ${request.reason}` : 'The transfer failed';
        case 'error':
            return request.reason ? `Could not request the file: ${request.reason}` : 'Could not request the file';
    }
}

/** The request as the answer of `request_attachment` leaves it. */
export function requestFromAnswer(answer: any): AttachmentRequest {
    const status = answer?.status;
    if (status === 'requesting' || status === 'receiving') {
        return { state: status, requestId: answer.request_id };
    }
    if (status === 'no_peers') return { state: 'no_peers' };
    if (status === 'available') return { state: 'received' };
    return { state: 'error', reason: typeof answer?.message === 'string' ? answer.message : undefined };
}

export type FileRequestStatusEvent = {
    request_id: string;
    conversation_id: string;
    message_id: string;
    filename?: string;
    state: AttachmentRequestState;
    transfer_id?: string;
    reason?: string;
};

/** The requests with one `file_request_status` event applied; a new Map when it changed anything. */
export function applyRequestStatus(
    requests: Map<string, AttachmentRequest>,
    event: FileRequestStatusEvent,
): Map<string, AttachmentRequest> {
    if (!event?.conversation_id || !event?.message_id || !event?.state) return requests;
    const key = requestKey(event.conversation_id, event.message_id, event.filename ?? '');
    const next = new Map(requests);
    next.set(key, {
        state: event.state,
        requestId: event.request_id,
        transferId: event.transfer_id ?? (event.state === 'receiving' ? requests.get(key)?.transferId : undefined),
        reason: event.reason,
    });
    return next;
}

/**
 * The history with the message `id` given the attachments a completed transfer
 * brought, matched by filename; null when no message has that id. A requested
 * file completes a record already on screen, so its event must replace that
 * bubble's attachments rather than append a second bubble of the same id.
 */
export function patchAttachmentsById<T extends { id?: string | null; attachments?: any[] }>(
    history: T[],
    id: string | null | undefined,
    attachments: any[] | null | undefined,
): T[] | null {
    if (!id || !Array.isArray(attachments) || attachments.length === 0) return null;
    const at = history.findIndex((m) => m.id === id);
    if (at < 0) return null;
    const current = history[at];
    const merged = (current.attachments ?? []).map((old) => {
        const arrived = attachments.find((a) => a?.filename === old?.filename);
        if (!arrived) return old;
        const { available: _gone, ...rest } = { ...old, ...arrived };
        return rest;
    });
    const next = history.slice();
    next[at] = { ...current, attachments: merged.length ? merged : attachments };
    return next;
}
