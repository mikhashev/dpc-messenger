/**
 * Loading a chat's history a page at a time (A-CHAT-LOADS-ITS-WHOLE-LIVE-HISTORY-IN-ONE-ANSWER).
 *
 * The backend answers `get_conversation_history` with the newest page, or with
 * the page before `before_message_id` (a record id), and says in `has_older`
 * whether anything older is left (core/dpc_client_core/history_page.py). The
 * UI asks for the newest page when a chat opens and for the next older one
 * only when the reader scrolls to the top. An error stops the pager: the
 * backend refuses an unknown cursor rather than answering the newest page.
 * The stop lasts until the chat is next opened, which starts it again from the
 * newest page (stopPagerUntilReopen, forgetPagerOnReopen).
 *
 * The scroll position is kept by hand — the top visible message stays where it
 * was — because CSS `overflow-anchor` cannot be relied on in WebKit (macOS
 * WKWebView, Linux WebKitGTK). Only getBoundingClientRect and scrollTop are used.
 */

import { writable } from 'svelte/store';

/** Messages per page; the backend's default, named so every caller asks the same. */
export const HISTORY_PAGE_LIMIT = 100;
/** How close to the top (px) counts as "reached the top". */
export const TOP_THRESHOLD_PX = 200;
/** How close to the bottom (px) counts as "reading the newest messages". */
export const BOTTOM_THRESHOLD_PX = 150;

export interface HistoryPager {
    /** The backend said older live messages exist before the oldest one loaded. */
    hasOlder: boolean;
    /** The chat's live_history_boundary (ISO moment), null when it was never reset. */
    boundary: string | null;
    /** An error answered a page request; no further requests for this chat. */
    stopped: boolean;
    /** A page request is in flight — the per-chat lock. */
    loading: boolean;
}

/** Per chat id. Written by the history loader, read by the chat window. */
export const historyPagers = writable<Map<string, HistoryPager>>(new Map());

export function setPager(chatId: string, patch: Partial<HistoryPager>): void {
    historyPagers.update((map) => {
        const next = new Map(map);
        const current = next.get(chatId) ?? { hasOlder: false, boundary: null, stopped: false, loading: false };
        next.set(chatId, { ...current, ...patch });
        return next;
    });
}

/**
 * A page request failed — most often an unknown cursor, because the chat was
 * reset or archived under the reader. The pager stays stopped while the chat
 * stays open, so a scroll at the top cannot turn into a request loop, and the
 * chat is noted for a fresh start the next time it is opened.
 */
export function stopPagerUntilReopen(chatId: string, reloadOnOpen: Set<string>): void {
    setPager(chatId, { loading: false, stopped: true });
    reloadOnOpen.add(chatId);
}

/**
 * The chat has just been opened. If a failed page request noted it, its pager
 * and its "fetched once" mark are dropped so the loader asks for the newest
 * page again; returns whether that happened. A chat not noted is left alone.
 */
export function forgetPagerOnReopen(chatId: string, loaded: Set<string>, reloadOnOpen: Set<string>): boolean {
    if (!reloadOnOpen.has(chatId)) return false;
    reloadOnOpen.delete(chatId);
    loaded.delete(chatId);
    historyPagers.update((map) => {
        if (!map.has(chatId)) return map;
        const next = new Map(map);
        next.delete(chatId);
        return next;
    });
    return true;
}

/**
 * What survives the fresh start of a chat whose loaded pages no longer match
 * the backend: only the bubbles that are not stored records yet (an answer in
 * flight). Everything stored comes back with the newest page, or by scrolling;
 * keeping the old pages would put them after the new page, out of order.
 */
export function placeholdersOnly<T extends PagedMessage>(messages: T[]): T[] {
    return messages.filter((m) => !!m.commandId || !!m.pending);
}

/** What a page answer says about what is left; unknown or missing means nothing. */
export function pageFacts(result: any): { hasOlder: boolean; boundary: string | null } {
    const boundary = result?.live_history_boundary;
    return {
        hasOlder: result?.has_older === true,
        boundary: typeof boundary === 'string' && boundary ? boundary : null,
    };
}

interface PagedMessage {
    id?: string | null;
    timestamp?: number;
    commandId?: string;
    pending?: boolean;
}

/**
 * The cursor for the next older page: the first message in the list that is a
 * stored record. `unconfirmed` holds the ids of user bubbles still awaiting
 * user_message_confirmed (unconfirmedUserBubbles): the backend never had those
 * ids and refuses them as a cursor.
 */
export function oldestRecordId<T extends PagedMessage>(
    messages: T[],
    unconfirmed: { has(id: string): boolean } = new Set<string>(),
): string | null {
    for (const m of messages) {
        if (m.commandId || m.pending) continue;
        if (m.id && unconfirmed.has(m.id)) continue;
        if (m.id) return m.id;
    }
    return null;
}

/** Older page in front of what is loaded; a message already on screen is not drawn twice. */
export function prependOlderPage<T extends PagedMessage>(existing: T[], older: T[]): { merged: T[]; added: number } {
    const have = new Set(existing.map((m) => m.id).filter(Boolean));
    const fresh = older.filter((m) => !m.id || !have.has(m.id));
    return { merged: fresh.length ? [...fresh, ...existing] : existing, added: fresh.length };
}

/**
 * The loaded messages a reload of the newest page does not cover: older than
 * its first message and not in it. A site that used to replace the whole list
 * with the snapshot keeps these in front, or a reload would throw away the
 * pages the reader scrolled up through.
 */
export function olderThanPage<T extends PagedMessage>(existing: T[], page: T[]): T[] {
    if (page.length === 0) return [];
    const first = page[0].timestamp;
    if (typeof first !== 'number' || !Number.isFinite(first)) return [];
    const inPage = new Set(page.map((m) => m.id).filter(Boolean));
    return existing.filter((m) =>
        !m.commandId && !m.pending && (!m.id || !inPage.has(m.id))
        && typeof m.timestamp === 'number' && m.timestamp < first);
}

/** Whether a scroll should ask for the next older page. */
export function shouldLoadOlder(args: {
    pager: HistoryPager | undefined;
    scrollTop: number;
    /** The reader moved the view up (a scroll that decreased scrollTop, or a wheel up). */
    movedUp: boolean;
    /** The reader has touched this chat's scroll since it opened. */
    userScrolled: boolean;
    thresholdPx?: number;
}): boolean {
    const { pager, scrollTop, movedUp, userScrolled } = args;
    if (!pager || !pager.hasOlder || pager.stopped || pager.loading) return false;
    if (!userScrolled || !movedUp) return false;
    return scrollTop <= (args.thresholdPx ?? TOP_THRESHOLD_PX);
}

/** Index of the first message whose bottom is below the container's top edge; -1 if none. */
export function pickAnchorIndex(rects: Array<{ top: number; bottom: number }>, containerTop: number): number {
    return rects.findIndex((r) => r.bottom > containerTop);
}

/** The scrollTop that puts the anchor back at the offset it had before the insert. */
export function scrollTopKeepingAnchor(scrollTop: number, offsetBefore: number, offsetAfter: number): number {
    return scrollTop + (offsetAfter - offsetBefore);
}

/** The reader is at (or near) the bottom: new messages may scroll the view. */
export function readerIsAtBottom(
    el: { scrollHeight: number; scrollTop: number; clientHeight: number } | null | undefined,
    thresholdPx: number = BOTTOM_THRESHOLD_PX,
): boolean {
    if (!el) return true;
    return el.scrollHeight - el.scrollTop - el.clientHeight < thresholdPx;
}

/** The line at the start of a chat whose live history begins at a reset. */
export function boundaryLineText(
    boundary: string | null | undefined,
    format: (d: Date) => string = (d) => d.toLocaleString(),
): string | null {
    if (!boundary) return null;
    const when = new Date(boundary);
    if (Number.isNaN(when.getTime())) return null;
    return `Session started ${format(when)} — earlier messages are in the archive`;
}

/**
 * The one line at the top of a chat whose pager has nothing older to load:
 * the backend said has_older false. A refused page says nothing — older pages
 * may well exist, and the chat loads again on its next open. A chat reset at some moment gets the
 * boundary line; any other chat says its loaded messages are all this node
 * keeps, so scrolling up does not end in silence. Nothing while older pages
 * may still come, and nothing above an empty list unless there is a boundary.
 */
export function startLineText(
    pager: HistoryPager | undefined,
    hasMessages: boolean,
    format?: (d: Date) => string,
): string | null {
    if (!pager || pager.hasOlder) return null;
    const boundary = boundaryLineText(pager.boundary, format);
    if (boundary) return boundary;
    return hasMessages ? 'No older messages on this node' : null;
}
