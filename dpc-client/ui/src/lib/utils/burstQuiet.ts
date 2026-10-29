/**
 * The "Join messages sent within (seconds)" field of Edit Telegram Configuration:
 * how long a Telegram chat must stay quiet before the messages gathered from it
 * reach the agent as one run. The backend owns the rule (a number from 0 to 300,
 * 20 when the agent's row has none); this keeps the screen from turning a 0 into
 * the default, which `value || 20` would.
 */

export const BURST_QUIET_DEFAULT_SECONDS = 20;
export const BURST_QUIET_MAX_SECONDS = 300;

/** What the field shows for an agent: its stored pause, or the default when the row has none. */
export function burstQuietFromAgent(stored: number | null | undefined): number {
    return typeof stored === 'number' && Number.isFinite(stored) ? stored : BURST_QUIET_DEFAULT_SECONDS;
}

/**
 * What the field holds, ready to send. An emptied field (a number input gives
 * null) means the default; a value outside 0..300 is an error the dialog can show.
 */
export function burstQuietToSend(
    typed: number | string | null | undefined
): { value: number } | { error: string } {
    if (typed === null || typed === undefined || typed === '') {
        return { value: BURST_QUIET_DEFAULT_SECONDS };
    }
    const n = typeof typed === 'number' ? typed : Number(typed);
    if (!Number.isFinite(n) || n < 0 || n > BURST_QUIET_MAX_SECONDS) {
        return { error: `Join messages sent within must be a number from 0 to ${BURST_QUIET_MAX_SECONDS} seconds` };
    }
    return { value: n };
}
