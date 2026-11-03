// The card queue's lines for the Local engines block, from local_engine_status
// (ADR-044, amendment 2026-11-02, the card queue): who is on the card, who waits for it
// in order, until when, and which background call was last skipped.

export interface CardHolderRow {
    alias: string;
    engine: string;
    state: string;
    port: number | null;
}

/** One request in line. The position is the order; there is no position field. */
export interface CardQueueRow {
    alias: string;
    agent_id: string | null;
    conversation_id: string | null;
    queued_at: number;
    give_up_at: number;
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

/** The holders beside the one the card holder line names; only llama-server aliases share. */
export function otherCardHolderLines(holders: CardHolderRow[] | null | undefined): string[] {
    return (holders ?? []).slice(1).map((h) => `Also on the card: ${h.alias} (${h.engine}, ${h.state})`);
}

/** One line per request, first in line first. */
export function cardQueueLines(rows: CardQueueRow[] | null | undefined): string[] {
    return (rows ?? []).map((row) => {
        const who = [
            row.agent_id ? `agent ${row.agent_id}` : '',
            row.conversation_id ? `chat ${row.conversation_id}` : '',
        ].filter(Boolean).join(', ');
        return `${row.alias}${who ? ` (${who})` : ''}: in line since ${clock(row.queued_at)}, `
            + `gives up at ${clock(row.give_up_at)}`;
    });
}

/** One line per background call that has been skipped, the latest first. */
export function backgroundSkipLines(skips: Record<string, BackgroundSkipRow> | null | undefined): string[] {
    return Object.entries(skips ?? {})
        .sort(([, a], [, b]) => b.at - a.at)
        .map(([what, row]) => `Background call ${what} skipped at ${dateAndClock(row.at)} on ${row.alias}: `
            + `${row.reason}. ${row.what_follows}`);
}
