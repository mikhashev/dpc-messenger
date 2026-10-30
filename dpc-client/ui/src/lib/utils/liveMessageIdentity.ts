/**
 * The id a live message keeps, and the merge that depends on it.
 *
 * Every message reaches the screen twice over: once as the websocket event that
 * announced it, and once as the record the backend stored, handed back when the
 * chat is opened. The backfill joins the two by `id` and keeps only the live
 * bubbles the snapshot does not carry.
 *
 * That join is the whole reason the id matters. The router computed the
 * backend's `message_id` for its own dedup set and then stamped the bubble with
 * a fresh `crypto.randomUUID()`, so no live bubble could ever match a stored
 * record: opening a 1:1 chat drew the conversation twice, the stored copy first
 * and the live copy after it. The group-text effect in the same file had it
 * right, which is what makes this an omission rather than a design.
 */

export interface HasMessageId {
    message_id?: string | null;
}

export interface Identified {
    id?: string | null;
}

/**
 * The id to stamp on a bubble built from a websocket payload.
 *
 * `newId` is a parameter so a test can watch the fallback without mocking the
 * global, and so the fallback stays lazy: an event that carries an id must not
 * spend one.
 */
export function liveBubbleId(
    msg: HasMessageId | null | undefined,
    newId: () => string = () => crypto.randomUUID(),
): string {
    const carried = msg?.message_id;
    if (typeof carried === 'string' && carried.length > 0) return carried;
    return newId();
}

/**
 * Fold a backend snapshot into what is already on screen.
 *
 * Anything on screen the snapshot does not carry is newer than the snapshot — a
 * message that landed while the fetch was in flight, or before the chat was
 * opened — so it is kept after it. Replacing outright would drop it.
 */
export function mergeBackfillWithLive<T extends Identified>(loaded: T[], live: T[]): T[] {
    const loadedIds = new Set(loaded.map((m) => m.id).filter(Boolean));
    const kept = live.filter((m) => !m.id || !loadedIds.has(m.id));
    return kept.length ? [...loaded, ...kept] : loaded;
}

export interface UserMessageConfirmed {
    msg_index?: number | null;
    message_id?: string | null;
    command_id?: string | null;
}

/**
 * User bubbles drawn in an agent or AI chat whose record the backend has not
 * confirmed yet: bubble id -> the command id the question was sent under, null
 * for a pasted image (send_image carries none).
 *
 * The command id is what tells two quick sends apart: confirmations may arrive
 * after the second bubble is drawn, and "the last unconfirmed bubble" would
 * then take the first record. The same entries keep the older-page cursor off
 * an id the backend never had (oldestRecordId). UI-local, never on the message.
 */
export const unconfirmedUserBubbles = new Map<string, string | null>();

interface ConfirmableMessage extends Identified {
    sender?: string;
    msg_index?: number | null;
}

/**
 * A chat's messages after `user_message_confirmed` reached them.
 *
 * A question typed into an agent chat is drawn at once under an id the UI
 * mints; the backend stores it and confirms with the record's index and id.
 * The bubble takes both, so a reload of the newest page finds it in the page
 * by id and the older-page cursor is an id the backend knows. Taking the index
 * alone left the bubble beside the record it stood for: the question drawn
 * twice, both numbered, whenever it was the page's first record.
 *
 * The bubble is the one sent under the confirmation's `command_id`, looked up
 * in `unconfirmed`. Without a command id, or with one not on record, it is the
 * last message the person sent, while it has no index. A confirmation that
 * finds the last one already numbered (one applied before, as on a remount)
 * changes nothing, rather than numbering an older bubble.
 *
 * When the record is already in the list — a reload landed first and kept the
 * bubble right in front of it — the bubble is dropped, not re-stamped: two
 * entries with one id reach the keyed `{#each ... (msg.id)}` and Svelte 5
 * throws each_key_duplicate.
 *
 * Returns the same array when nothing changes.
 */
export function applyUserMessageConfirmed<T extends ConfirmableMessage>(
    messages: T[],
    confirmed: UserMessageConfirmed | null | undefined,
    unconfirmed: Map<string, string | null> = unconfirmedUserBubbles,
): T[] {
    const msgIndex = confirmed?.msg_index;
    if (!msgIndex) return messages;
    const carried = confirmed?.message_id;
    const messageId = typeof carried === 'string' && carried.length > 0 ? carried : null;
    const isBubble = (m: T | undefined) => !!m && m.sender === 'user' && !m.msg_index;
    const stamp = (at: number) => {
        unconfirmed.delete(messages[at].id ?? '');
        const updated = [...messages];
        updated[at] = { ...messages[at], msg_index: msgIndex, ...(messageId ? { id: messageId } : {}) };
        return updated;
    };

    const commandId = confirmed?.command_id;
    let bubbleId: string | null = null;
    if (commandId) {
        for (const [id, sentUnder] of unconfirmed) {
            if (sentUnder === commandId) { bubbleId = id; break; }
        }
    }
    if (bubbleId !== null) {
        const bubble = messages.findIndex((m) => m.id === bubbleId);
        // Sent from another chat's list, or already gone: not this list's to number.
        if (bubble < 0) return messages;
        if (messageId && messages.some((m) => m.id === messageId)) {
            unconfirmed.delete(bubbleId);
            return messages.filter((_, i) => i !== bubble);
        }
        return stamp(bubble);
    }

    if (messageId) {
        const at = messages.findIndex((m) => m.id === messageId);
        if (at >= 0) {
            if (at > 0 && isBubble(messages[at - 1])) {
                unconfirmed.delete(messages[at - 1].id ?? '');
                return messages.filter((_, i) => i !== at - 1);
            }
            return messages;
        }
    }

    // Only send_image confirms without a command id, and its bubble is the one
    // registered with none: a question typed after the image is not it.
    if (!commandId) {
        const image = messages.findLastIndex((m) => isBubble(m) && unconfirmed.get(m.id ?? '') === null);
        if (image >= 0) return stamp(image);
    }

    const last = messages.findLastIndex((m) => m.sender === 'user');
    if (last < 0 || !isBubble(messages[last])) return messages;
    return stamp(last);
}
