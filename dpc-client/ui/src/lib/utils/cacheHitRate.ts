/**
 * The cache half of the agent progress strip: how much of the prompt the
 * provider did not have to read again.
 *
 * The backend sends two counts of the round that just finished, `cached_tokens`
 * and `prompt_tokens` (loop.py: round_progress_payload), and never a percentage.
 * That is deliberate. The live header shows one round's rate, but the finished
 * header has to describe the turn, and the turn's rate is the token-weighted
 * one — sum(cached) / sum(prompt), the sums made by the backend and stored
 * with the message (`speed_summary`, loop.py speed_summary_of). A median of
 * per-round percentages is the wrong instrument here: the first call of a turn
 * often reuses nothing, and a median hides exactly that.
 *
 * Absent is absent. A round that did not state the reused count, a prompt of 0
 * tokens, or a count larger than its prompt gets no number at all — a percentage
 * nobody reported would be made up. A real 0 (the provider said it reused
 * nothing) is a number and reads `cache hit rate: 0%`.
 */

import { formatTokens } from './contextOccupancy';

export interface CacheRate {
    cached: number;
    prompt: number;
    /** Integer percent, cached / prompt. */
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
    return { cached, prompt, pct: Math.round((cached / prompt) * 100) };
}

/** The part as the header renders it. */
export function cacheLabel(rate: CacheRate): string {
    return `cache hit rate: ${rate.pct}%`;
}

/** The raw numbers behind the percent; `rounds` is given for a finished turn. */
export function cacheTitle(rate: CacheRate, opts: { rounds?: number | null } = {}): string {
    const over = opts.rounds ? ` over ${opts.rounds} ${opts.rounds === 1 ? 'round' : 'rounds'}` : '';
    return `${formatTokens(rate.cached)} of ${formatTokens(rate.prompt)} prompt tokens reused from the cache${over}`;
}
