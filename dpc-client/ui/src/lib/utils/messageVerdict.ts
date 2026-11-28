/**
 * A group record's signature verdict (`verification`, set by the backend when
 * it checked the record: group_handler._authenticate_author) as the words a
 * message row shows for it. The classes are the owner's split (DPC #111/#112):
 * the three non-clean verdicts mean different things, and one badge for all
 * three would be noise.
 *
 * - 'unverified' — the signature is valid but this machine does not yet hold
 *   the author's certificate (the code already asks for it). Not danger: a
 *   quiet, neutral "awaiting certificate".
 * - 'legacy' — a record from before signatures existed (ADR-036). Age, not a
 *   threat: on the owner's machine 1432 of 1439 records are like this.
 * - 'verified', no verdict at all, or a verdict this UI does not know — nothing.
 *   A note is drawn only for a verdict whose meaning is known to be "not
 *   checked". A word this build has not heard of (a later step stores one for
 *   this node's own records) must not be shown as unsigned: that would draw
 *   this node's own words as suspicious. Absence is never drawn as an alarm.
 *
 * 'rejected' records never reach the UI (the handler drops them), so they have
 * no class here.
 */

export type VerdictNoteKind = 'none' | 'awaiting-certificate' | 'older-format';

export interface VerdictNote {
    kind: VerdictNoteKind;
    text: string;
}

/** The class and the words for one record's verdict. Shared by the chat row and
 *  (a later step) the agent prompt, so both say the same thing. */
export function verdictNote(verification: string | undefined): VerdictNote {
    switch (verification) {
        case 'unverified':
            return { kind: 'awaiting-certificate', text: 'awaiting certificate' };
        case 'legacy':
            return { kind: 'older-format', text: 'unsigned, older format' };
        default:
            return { kind: 'none', text: '' };
    }
}
