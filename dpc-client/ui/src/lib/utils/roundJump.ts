/**
 * The "Round N start" button in the live run's sticky Stop bar: the newest round
 * header is where it scrolls back to. A round's Thinking text can run for
 * screens, and the run grows downward under autoscroll.
 *
 * "Newest" is the newest round header the block draws, i.e. the highest round
 * among the tool calls. A round that has made no tool call yet has no header.
 */

/** The newest round among the tool calls, or null while there is none. */
export function newestRound(calls: ReadonlyArray<{ round?: number }>): number | null {
    let newest: number | null = null;
    for (const tc of calls) {
        const r = tc.round ?? 0;
        if (newest === null || r > newest) newest = r;
    }
    return newest;
}

export function roundJumpLabel(round: number): string {
    return `↑ Round ${round} start`;
}

export function roundJumpTitle(round: number): string {
    return `Scroll to the start of round ${round}`;
}

/**
 * The header of `round` inside one block. Looked up from the block's own root,
 * never from the document: several runs can be on screen.
 */
export function findRoundHeader(
    root: { querySelector(selector: string): Element | null } | null | undefined,
    round: number,
): Element | null {
    if (!root) return null;
    return root.querySelector(`.round-header[data-round="${round}"]`);
}
