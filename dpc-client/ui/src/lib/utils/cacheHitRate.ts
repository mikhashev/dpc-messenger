/**
 * The cache half of the agent progress strip: how much of the prompt the
 * provider did not have to read again.
 *
 * The backend sends two counts of the round that just finished, `cached_tokens`
 * and `prompt_tokens` (loop.py: round_progress_payload), and never a percentage.
 * That is deliberate. The header shows the turn's rate, live and finished, and
 * the turn's rate is the token-weighted one — sum(cached) / sum(prompt), the
 * sums made by the backend (`speed_summary`, loop.py speed_summary_of: running
 * on the live payload, stored with the finished message). A median of
 * per-round percentages is the wrong instrument here: the first call of a turn
 * often reuses nothing, and a median hides exactly that.
 *
 * Absent is absent. A round that did not state the reused count, a prompt of 0
 * tokens, or a count larger than its prompt gets no number at all — a percentage
 * nobody reported would be made up. A real 0 (the provider said it reused
 * nothing) is a number and reads `cache hit rate: 0.0%`.
 *
 * One decimal, cut down rather than rounded (Mike's call, 2026-11-15): 989 tokens
 * read again out of 217,786 is 99.5%, and only a round that read nothing again
 * may say 100.0%.
 */

import { formatTokens } from './contextOccupancy';

export interface CacheRate {
    cached: number;
    prompt: number;
    /** Percent with one decimal, cached / prompt, cut down: 100 only when all was reused. */
    pct: number;
}

function wholeCount(value: unknown): number | null {
    if (typeof value !== 'number' || !Number.isInteger(value) || value < 0) return null;
    return value;
}

/**
 * Read the cache rate out of a speed payload (one round) or a finished-turn
 * summary (the sums of the turn), or null when it does not carry a usable pair.
 */
export function cacheFromSpeed(speed: Record<string, any> | null | undefined): CacheRate | null {
    const cached = wholeCount(speed?.cached_tokens);
    const prompt = wholeCount(speed?.prompt_tokens);
    if (cached === null || prompt === null || prompt <= 0 || cached > prompt) return null;
    return { cached, prompt, pct: Math.floor((cached * 1000) / prompt) / 10 };
}

/** One round's rate as the header renders it (the label of a backend that sends no turn summary). */
export function cacheLabel(rate: CacheRate): string {
    return `cache hit rate: ${rate.pct.toFixed(1)}%`;
}

/** The turn's token-weighted rate as the header renders it. */
export function avgCacheLabel(rate: CacheRate): string {
    return `avg cache hit rate: ${rate.pct.toFixed(1)}%`;
}

/** The raw numbers behind the percent; `rounds` is given for a finished turn. */
export function cacheTitle(rate: CacheRate, opts: { rounds?: number | null } = {}): string {
    const over = opts.rounds ? ` over ${opts.rounds} ${opts.rounds === 1 ? 'round' : 'rounds'}` : '';
    return `${formatTokens(rate.cached)} of ${formatTokens(rate.prompt)} prompt tokens reused from the cache${over}`;
}

/** The last round's own figure, for the live tooltip: the share and what was read again. */
export function lastRoundTitle(rate: CacheRate): string {
    return `last round ${rate.pct.toFixed(1)}%: ${formatTokens(rate.prompt - rate.cached)} of ${formatTokens(rate.prompt)} read again`;
}

export interface CachePart {
    label: string;
    title: string;
}

/**
 * The cache part of the header: the turn's average. Live it is the backend's
 * running `speed_summary` on the round's payload; finished it is the stored
 * summary itself (`median: true`). A live payload without `speed_summary` (an
 * older backend) falls back to the round's own numbers under the old label, so
 * the header never claims an average it was not given.
 */
export function cachePart(speed: Record<string, any> | null | undefined): CachePart | null {
    if (!speed) return null;
    const finished = !!speed.median;
    const turnSource = finished ? speed : speed.speed_summary;
    const turn = cacheFromSpeed(turnSource);
    const round = finished ? null : cacheFromSpeed(speed);
    if (turn) {
        const base = cacheTitle(turn, { rounds: turnSource.rounds });
        return {
            label: avgCacheLabel(turn),
            title: round ? `${base}; ${lastRoundTitle(round)}` : base,
        };
    }
    if (round) return { label: cacheLabel(round), title: cacheTitle(round) };
    return null;
}
