/**
 * The idle swap of local engines (ADR-044, amendment 2026-11-02): the Providers
 * editor's "Wait for a busy holder, s" field holds a whole number of seconds, 0 or
 * more (0 = do not wait). The backend refuses anything else; this says so before
 * the command is sent.
 */

export const SWAP_WAIT_REFUSAL = 'Wait for a busy holder must be a whole number of seconds, 0 or more.';

/** The seconds the field holds, or null when it is not a whole number >= 0. */
export function parseSwapWait(text: string | number | undefined | null): number | null {
    if (typeof text === 'number') {
        return Number.isSafeInteger(text) && text >= 0 ? text : null;
    }
    const v = (text ?? '').trim();
    if (!/^\d+$/.test(v)) return null;
    const n = Number(v);
    return Number.isSafeInteger(n) ? n : null;
}
