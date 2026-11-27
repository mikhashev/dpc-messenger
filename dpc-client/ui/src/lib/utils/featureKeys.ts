/**
 * The small pure helpers of the feature-key controls that live inside their feature
 * views (config-parity card, order 7): the HuggingFace offline mode's three states
 * and the image-cap figure the composer's image chip shows.
 */

/** The three states of `[hf] offline_mode`, as run_service reads it: `auto` = the key is
 *  absent and the per-start cache detection decides, `on` = written `true`, `off` =
 *  written `false`. An explicit HF_HUB_OFFLINE in the environment wins over all three. */
export const HF_STATES: readonly string[] = ['auto', 'on', 'off'];

/** `value` normalised into one of the three states — a select must always hold one of
 *  its options, whatever the command answered. */
export function hfStateOf(value: unknown): string {
    const text = typeof value === 'string' ? value.trim().toLowerCase() : '';
    return HF_STATES.includes(text) ? text : 'auto';
}

/** `value` as a whole number of MB above 0, else `fallback` (the reader's default, 5) —
 *  the image-cap input must never hold a figure the writer would refuse (a boolean is
 *  not a figure, as in the backend writer). */
export function imageMbOr(value: unknown, fallback: number): number {
    if (typeof value === 'boolean') return fallback;
    const n = typeof value === 'number' ? value : Number(value);
    return Number.isInteger(n) && n > 0 ? n : fallback;
}
