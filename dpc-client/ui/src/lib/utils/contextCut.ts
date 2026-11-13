/**
 * The context-cut part of the agent progress strip: why the context number fell.
 *
 * The backend carries the run's last cut as `last_context_cut` in the round's
 * speed payload (loop.py: round_progress_payload, from context.py
 * CompactionState): the round it happened in, the reason ("rounds" and
 * "pressure" shorten old tool results to notes, "summary" has a model summarize
 * them), the tokens it freed (an estimate) and the rounds kept as they are.
 * Absent before the first cut, and then the strip shows nothing.
 */

export interface ContextCut {
    round: number;
    reason: string;
    freed: number;
    kept: number;
}

function wholeCount(value: unknown): number | null {
    if (typeof value !== 'number' || !Number.isInteger(value) || value < 0) return null;
    return value;
}

/** Read the last cut out of a speed payload, or null when it carries none. */
export function cutFromSpeed(speed: Record<string, any> | null | undefined): ContextCut | null {
    const cut = speed?.last_context_cut;
    if (!cut || typeof cut !== 'object') return null;
    const round = wholeCount(cut.round);
    const freed = wholeCount(cut.freed_tokens);
    const kept = wholeCount(cut.kept_rounds);
    if (round === null || freed === null || kept === null) return null;
    return { round, reason: String(cut.reason ?? ''), freed, kept };
}

/** Tokens in thousands with one decimal: 10850 -> "10.9K". */
function kilo(tokens: number): string {
    return `${(tokens / 1000).toFixed(1)}K`;
}

/** The part as the header renders it: `cut r46 −10.9K`. */
export function cutLabel(cut: ContextCut): string {
    return `cut r${cut.round} −${kilo(cut.freed)}`;
}

/** One sentence: what the cut did, how much it freed, what it kept. */
export function cutTitle(cut: ContextCut): string {
    const how = cut.reason === 'summary' ? 'summarized by the model' : 'shortened';
    const rounds = cut.kept === 1 ? 'round is' : 'rounds are';
    return `Round ${cut.round}: old tool results ${how} to free ${kilo(cut.freed)} tokens; `
        + `the last ${cut.kept} ${rounds} kept as they are.`;
}
