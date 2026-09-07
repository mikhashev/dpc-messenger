/**
 * Whether a vote status belongs to the proposal currently on screen.
 *
 * The status store clears correctly; the dialog's error text does not live in
 * it, so the identity has to be checked where the text is written. Without the
 * check a refusal raised for one proposal stayed up over an unrelated vote in
 * another group — and told its only member he could not vote there.
 */

export interface Identified {
    proposal_id?: string | null;
}

/**
 * True when the status may be applied to the open proposal.
 *
 * Unknown on either side means yes: a status with no id is the old shape and
 * refusing it would drop a message that has nowhere else to appear, and a
 * dialog with no proposal has nothing to contradict.
 */
export function voteStatusAppliesTo(
    held: Identified | null | undefined,
    open: Identified | null | undefined,
): boolean {
    const a = held?.proposal_id;
    const b = open?.proposal_id;
    if (!a || !b) return true;
    return a === b;
}
