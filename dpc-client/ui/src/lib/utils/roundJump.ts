/**
 * The "Round N start" button in the live run's sticky Stop bar: it scrolls back
 * to the very start of the run, the first round, above all of its Thinking text
 * (Mike's call, 2026-11-15: back to round 1, the start of the loop, not to the
 * newest round). The run grows downward under autoscroll.
 *
 * The number on the button is the first round the block draws, i.e. the lowest
 * round among the tool calls; a call with no round counts as round 0, as the
 * round grouping does.
 */

/** The first round among the tool calls, or null while there is none. */
export function firstRound(calls: ReadonlyArray<{ round?: number }>): number | null {
    let first: number | null = null;
    for (const tc of calls) {
        const r = tc.round ?? 0;
        if (first === null || r < first) first = r;
    }
    return first;
}

export function roundJumpLabel(round: number): string {
    return `↑ Round ${round} start`;
}

export function roundJumpTitle(round: number): string {
    return `Scroll to the start of the run (round ${round})`;
}
