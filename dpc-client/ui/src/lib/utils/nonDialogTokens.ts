/**
 * The counter row that is not a measurement of anything.
 *
 * `Static` reads as «the system prompt, the memory and the tool schemas» — a sum
 * of named parts. It is not one. It is what is left when the estimated size of
 * the dialogue is subtracted from the measured size of the whole prompt:
 *
 *     max(0, measuredTotal − min(estimatedDialogue, measuredTotal))
 *
 * Two numbers of different provenance. `measuredTotal` is what the API counted;
 * `estimatedDialogue` is `chars / 4` from `tokenEstimator.ts`. In an agent chat
 * the remainder is dominated by a genuinely large block and the estimator's error
 * is noise inside it. In a chat without an agent there is no such block — only a
 * one-line instruction — and at a dialogue of a couple of hundred tokens the
 * error is the same size as the thing the row claims to show.
 *
 * The clamp adds a bias worth naming, because it is invisible on screen: when the
 * estimator **overshoots** the measured total the difference is floored at zero,
 * so the row reads `≈0` and the instruction that really is in the prompt
 * disappears. The number can therefore only ever be too large or absent, never
 * too small — and «0» there does not mean «nothing», it means «the estimate was
 * bigger than the measurement».
 *
 * None of this is fixed here. What is fixed is the claim: the row says what it is
 * when there is nothing to substantiate it, and keeps its old name when the
 * backend sends a real breakdown of components.
 */

/** The remainder, with the floor the display has always had. */
export function nonDialogTokens(
    measuredTotal: number,
    estimatedDialogue: number,
): number {
    if (!(measuredTotal > 0)) return 0;
    return Math.max(0, measuredTotal - Math.min(estimatedDialogue, measuredTotal));
}

/**
 * Whether the remainder can be named as components rather than described as a
 * remainder — true only when the backend sent a breakdown to name them with.
 */
export function hasComponentBreakdown(
    breakdown: Array<{ name: string; tokens: number }> | null | undefined,
): breakdown is Array<{ name: string; tokens: number }> {
    // A type predicate rather than a plain boolean, so the caller that goes on to
    // map over the list keeps the narrowing this function just established.
    return Array.isArray(breakdown) && breakdown.length > 0;
}

/** The row's label: what this number honestly is on this surface. */
export function nonDialogLabel(
    contextAgent: string | null | undefined,
    breakdown: Array<{ name: string; tokens: number }> | null | undefined,
): string {
    if (contextAgent) return 'Agent ctx';
    return hasComponentBreakdown(breakdown) ? 'Static' : 'Non-dialog';
}
