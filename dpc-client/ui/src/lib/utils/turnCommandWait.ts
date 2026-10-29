// How long the UI waits for a command whose answer is an agent's turn.
//
// `send_image` in an agent chat runs a whole turn before it answers, and a turn
// has no fixed length: the vision turn of 2026-10-29 was at 249 s when it was
// stopped, past the 240 s the command used to get. A fixed timeout there reports
// a failure while the agent is still working, and its answer then lands anyway
// through `ai_response_with_image`. So the wait is measured from the last moment
// the screen saw a turn in that chat (the `agent_turn_updated` snapshots and the
// `list_agents` rows, held in `agentTurns`), not from the send. The snapshot is
// sent on every change and a turn in one long model call is a turn with
// `wait_reason: model_call`, so a model call that emits nothing does not look idle.
//
// Pure: coreService holds the pending command and reads the clock.

import type { AgentTurnStates } from './agentTurnNotice';

/** Until a turn is seen in the chat, how long after the send the command may go unanswered. */
export const TURN_COMMAND_TIMEOUT_MS = 240_000;
/** Once a turn was seen there and is gone, how long its answer may still take. */
export const TURN_COMMAND_AFTER_TURN_MS = 60_000;
/** How often a pending turn command looks at the chat. */
export const TURN_COMMAND_CHECK_MS = 5_000;

/** A command whose answer is an agent's turn: `send_image` routed to the agent. */
export function isTurnCommand(command: string, payload: any): boolean {
    return command === 'send_image'
        && payload?.chat_provider === 'dpc_agent'
        && typeof payload?.conversation_id === 'string'
        && payload.conversation_id !== '';
}

/** True when an agent's turn is in this chat, or the chat is waiting for one. */
export function chatHasTurnOrWaits(states: AgentTurnStates, conversationId: string): boolean {
    for (const s of Object.values(states)) {
        if (s.turn?.conversation_id === conversationId) return true;
        if ((s.waiting ?? []).some((w) => w.conversation_id === conversationId)) return true;
    }
    return false;
}

export interface TurnCommandWatch {
    sentAt: number;
    /** Local ms of the last look that saw a turn in the chat; null until one did. */
    lastTurnSeenAt: number | null;
}

/** The watch after one look at the chat. */
export function noteTurnCommandLook(watch: TurnCommandWatch, turnShown: boolean, nowMs: number): TurnCommandWatch {
    return turnShown ? { ...watch, lastTurnSeenAt: nowMs } : watch;
}

/** Null while the answer may still come; otherwise why the command is given up. */
export function turnCommandTimedOut(watch: TurnCommandWatch, nowMs: number): string | null {
    if (watch.lastTurnSeenAt === null) {
        const s = Math.round((nowMs - watch.sentAt) / 1000);
        return nowMs - watch.sentAt > TURN_COMMAND_TIMEOUT_MS
            ? `no agent turn appeared in this chat in the ${s} s since it was sent`
            : null;
    }
    const s = Math.round((nowMs - watch.lastTurnSeenAt) / 1000);
    return nowMs - watch.lastTurnSeenAt > TURN_COMMAND_AFTER_TURN_MS
        ? `the agent's turn in this chat ended ${s} s ago and its answer did not arrive`
        : null;
}
