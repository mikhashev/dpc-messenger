// The card queue's lines for the Local engines block, from local_engine_status
// (ADR-044, amendment 2026-11-02, the card queue): who is on the card, who waits for it
// in order, until when, and which background call was last skipped.

export interface CardHolderRow {
    alias: string;
    engine: string;
    state: string;
    port: number | null;
    /** A supervised llama-server or Strata child is `inference` (an older backend sends no kind);
     *  any other word is a card lease (ADR-052 L7): Whisper, a music engine, the embedder. */
    kind?: string;
    /** A lease's name for people, "Whisper (turbo)"; `alias` is its holder key. */
    display?: string;
    /** What a lease is busy with right now; empty when it only holds the card. */
    uses?: { what: string; since: number }[];
}

/** One request in line. The position is the order; there is no position field. */
export interface CardQueueRow {
    alias: string;
    /** A lease waiter has neither: it names its holder and `what` it asks for. */
    agent_id?: string | null;
    conversation_id?: string | null;
    queued_at: number;
    /** null on a card-borrow's way back until its next round asks, when its own bound starts; on a
     *  lease row, null means no bound at all (`until_cancelled`). */
    give_up_at: number | null;
    /** Lease rows only (ADR-052 L7): the holder's kind, what the request is for, and whether a null
     *  `give_up_at` means "until cancelled" (a lease) and not "coming back from a description". */
    kind?: string;
    what?: string;
    until_cancelled?: boolean;
}

export interface BackgroundSkipRow {
    alias: string;
    reason: string;
    what_follows: string;
    at: number;
}

const clock = (seconds: number) => new Date(seconds * 1000).toLocaleTimeString();
// A skip's row stays until the backend restarts, so a clock alone could be any day's.
const dateAndClock = (seconds: number) => new Date(seconds * 1000).toLocaleString();

const isLeaseKind = (kind: string | undefined) => kind !== undefined && kind !== 'inference';

/** The holders beside the one the card holder line names. That line names the first supervisor
 *  row (the core's `card_holder`, which skips leases), so leave that one out; a lease is always
 *  listed, whichever place it stands in. */
export function otherCardHolderLines(holders: CardHolderRow[] | null | undefined): string[] {
    const rows = holders ?? [];
    const named = rows.findIndex((h) => !isLeaseKind(h.kind));
    return rows.filter((_, i) => i !== named).map((h) => {
        if (!isLeaseKind(h.kind)) return `Also on the card: ${h.alias} (${h.engine}, ${h.state})`;
        const busy = (h.uses ?? []).map((u) => u.what).join(', ');
        return `Also on the card: ${h.display || h.alias} (lease, ${h.state}${busy ? `, busy with ${busy}` : ''})`;
    });
}

/** What the Card holder line says when no supervised engine holds the card: free, unless a lease does. */
export function noLocalEngineText(holders: CardHolderRow[] | null | undefined): string {
    return (holders ?? []).some((h) => isLeaseKind(h.kind))
        ? 'no local engine — a lease holds the card, below'
        : 'none — the card is free';
}

/** One line per request, first in line first. */
export function cardQueueLines(rows: CardQueueRow[] | null | undefined): string[] {
    return (rows ?? []).map((row) => {
        const lease = row.until_cancelled !== undefined || isLeaseKind(row.kind);
        const who = lease
            ? ['lease', row.what ?? ''].filter(Boolean).join(': ')
            : [
                row.agent_id ? `agent ${row.agent_id}` : '',
                row.conversation_id ? `chat ${row.conversation_id}` : '',
            ].filter(Boolean).join(', ');
        const until = row.give_up_at !== null ? `gives up at ${clock(row.give_up_at)}`
            : lease ? 'until cancelled' : 'coming back from a description';
        return `${row.alias}${who ? ` (${who})` : ''}: in line since ${clock(row.queued_at)}, ${until}`;
    });
}

/** One line per background call that has been skipped, the latest first. */
export function backgroundSkipLines(skips: Record<string, BackgroundSkipRow> | null | undefined): string[] {
    return Object.entries(skips ?? {})
        .sort(([, a], [, b]) => b.at - a.at)
        .map(([what, row]) => `Background call ${what} skipped at ${dateAndClock(row.at)} on ${row.alias}: `
            + `${row.reason}. ${row.what_follows}`);
}
