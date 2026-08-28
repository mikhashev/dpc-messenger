/**
 * How long a queue-approval card may stay on screen.
 *
 * The backend gate stops waiting after its own TTL and answers nobody
 * afterwards. Until 2026-08-29 nothing took the card away, so a press that came
 * later reached a request id the backend no longer held — and the front end
 * dropped the card before reading the refusal, which is why it looked as though
 * nothing had happened at all. Mike raised it on 2026-08-16 and hit the same
 * card again twelve days later.
 *
 * The rule and its wiring live here rather than in the store so both can be
 * tested without a websocket: `services/scheduleApproval.ts` is the adapter
 * that hands them the real store and the real clock.
 */

export interface CardWithDeadline {
    request_id: string;
    /** Seconds the agent will wait. Absent on a backend older than the change
     *  that started sending it, and then the card keeps its old behaviour
     *  rather than vanishing on a number nobody sent. */
    timeout_seconds?: number | null;
}

/** Milliseconds the card may live, or null when the backend named no deadline. */
export function cardLifetimeMs(request: CardWithDeadline | null | undefined): number | null {
    const seconds = Number(request?.timeout_seconds ?? 0);
    if (!Number.isFinite(seconds) || seconds <= 0) return null;
    return seconds * 1000;
}

/**
 * Arrange for this card to be taken away when the agent stops waiting.
 *
 * Returns whether a retirement was scheduled, so a caller can tell "no deadline
 * was given" from "the timer is set" instead of guessing.
 */
export function scheduleCardRetirement(
    request: CardWithDeadline | null | undefined,
    retire: (requestId: string) => void,
    schedule: (fn: () => void, ms: number) => unknown,
): boolean {
    const lifetime = cardLifetimeMs(request);
    if (lifetime === null || !request) return false;
    schedule(() => retire(request.request_id), lifetime);
    return true;
}
