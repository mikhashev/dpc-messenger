// The Stop button's state machine, kept out of the component so it can be tested.
//
// idle --press--> stopping --press--> killing
// A first press asks the loop to stop at the next round boundary. While that is
// pending (a local model can spend minutes prefilling) the button becomes Kill,
// and a second press sends `force`: the model call in flight is cancelled, and
// every process of the turn and the agent's browser are ended (Kill).

export type StopState = 'idle' | 'stopping' | 'killing' | 'miss' | 'error';

export const KILL_TITLE =
    'Stop is pending. Kill ends it now, with no summary: the model call, every process '
    + "this turn started and the agent's browser.";

/** What a press does in this state: nothing, a stop, or a kill. */
export function pressAction(state: StopState): 'ignore' | 'stop' | 'kill' {
    if (state === 'killing') return 'ignore';
    if (state === 'stopping') return 'kill';
    return 'stop';
}

export function stopButtonLabel(state: StopState): string {
    switch (state) {
        case 'stopping': return 'Kill';
        case 'killing': return 'Killing…';
        case 'miss': return 'No active loop';
        default: return 'Stop';
    }
}

export function stopButtonTitle(state: StopState, note: string): string {
    if (state === 'stopping') return KILL_TITLE;
    return note || 'Stop agent';
}

export function stopButtonDisabled(state: StopState): boolean {
    return state === 'killing';
}
