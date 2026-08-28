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
