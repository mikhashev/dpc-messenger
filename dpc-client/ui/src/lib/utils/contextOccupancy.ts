/**
 * The occupancy half of the agent progress strip.
 *
 * The backend sends it beside the per-round speed (loop.py:
 * round_progress_payload): how many tokens the round that just finished
 * actually sent, against the model's window, plus the headroom the round
 * guard refuses a call below.
 *
 * Two decisions live here, and they are the reason this is a module and not
 * three lines in a template:
 *
 * 1. The denominator is the raw window of the agent's own model — the number
 *    its provider config carries — so the strip and the configuration cannot
 *    disagree. The reserve is what actually blocks a round, and it is shown by
 *    turning the number amber and then red, not by being subtracted.
 * 2. The pair is measured, not estimated: it is the input the provider counted
 *    for that round. It therefore describes the moment that round was sent,
 *    which is what the tooltip says rather than letting it read as "now".
 */

export interface Occupancy {
    used: number;
    window: number;
    free: number;
    reserve: number | null;
    /** The next round would be refused: free headroom is below the reserve. */
    blocked: boolean;
    /** The backend's own >80% warning line, and not yet blocked. */
    warn: boolean;
    pct: number;
}

export function formatTokens(value: number): string {
    return value.toLocaleString('en-US');
}

/**
 * Read the occupancy out of a speed payload, or null when the payload does not
 * carry one. Absent is absent: a missing window is not a window of zero, and a
 * round that reported no input size gets no bar rather than a "0 / N" that
 * would read as an empty context.
 */
export function occupancyFromSpeed(speed: Record<string, any> | null | undefined): Occupancy | null {
    const used = Number(speed?.context_used);
    const window = Number(speed?.context_window);
    if (!Number.isFinite(used) || !Number.isFinite(window) || used <= 0 || window <= 0) {
        return null;
    }
    const rawReserve = Number(speed?.context_reserve);
    const reserve = Number.isFinite(rawReserve) && rawReserve > 0 ? rawReserve : null;
    const free = window - used;
    const blocked = reserve !== null && free < reserve;
    return {
        used,
        window,
        free,
        reserve,
        blocked,
        warn: !blocked && used > window * 0.8,
        pct: Math.round((used / window) * 100),
    };
}

/** The strip itself, in the shape the header renders. */
export function occupancyLabel(occ: Occupancy): string {
    return `context: ${formatTokens(occ.used)} / ${formatTokens(occ.window)} (${occ.pct}%)`;
}

/**
 * What the number does not say on its own: which round it belongs to, how much
 * room is left, and where the next round stops being allowed.
 */
export function occupancyTitle(
    occ: Occupancy,
    opts: { round?: number | null; median?: boolean } = {},
): string {
    const round = opts.round ?? '?';
    const limit =
        occ.reserve !== null
            ? ` The next round is refused below ${formatTokens(occ.reserve)} free.`
            : '';
    const moment = opts.median
        ? ' Last round of the turn — a peak, not an average.'
        : ' Measured when that round was sent, not now.';
    return (
        `context at round ${round}: ${formatTokens(occ.used)} of ${formatTokens(occ.window)} tokens ` +
        `(${occ.pct}%), ${formatTokens(occ.free)} free.${limit}${moment}`
    );
}
