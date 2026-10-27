// The agent's turn and the chats waiting for it (ADR-046 point 8), as the
// `agent_turn_updated` event and the `list_agents` rows carry them, and the
// words the Agent map and the sidebar's state line say them with.
//
// Pure: the store in services/agents.ts holds the state, the components read
// the clock.

/** A turn with no event from its agent for this long is shown unconfirmed, not working. */
export const TURN_UNCONFIRMED_AFTER_S = 120;

export type TurnWaitReason = 'model_call' | 'approval';
export type WaitingWaitReason = 'other_chat' | 'room_busy';

export interface AgentTurn {
    conversation_id: string | null;
    conversation_title: string;
    started_at: string;
    round: number | null;
    provider_alias: string | null;
    tool: string | null;
    wait_reason: TurnWaitReason | null;
}

export interface HeldBy {
    conversation_id: string | null;
    wait_reason: TurnWaitReason | WaitingWaitReason | null;
    agent_id?: string;
}

export interface WaitingChat {
    conversation_id: string;
    conversation_title: string;
    waiting_since: string;
    wait_reason: WaitingWaitReason;
    held_by: HeldBy | null;
}

export interface AgentTurnSnapshot {
    agent_id: string;
    turn: AgentTurn | null;
    waiting: WaitingChat[];
    ts?: string;
}

/** One agent's state on screen: the last snapshot, and what this screen saw when. */
export interface AgentTurnState {
    agent_id: string;
    turn: AgentTurn | null;
    waiting: WaitingChat[];
    ts?: string;
    /** Local ms of the last event from this agent (any kind). */
    lastEventAt: number;
    /** Local ms the turn's current wait_reason was first seen. */
    waitSeenAt: number;
    /** True once an event, not only a list_agents row, has set this state. */
    fromEvent: boolean;
}

export type AgentTurnStates = Record<string, AgentTurnState>;

function sameWait(a: AgentTurn | null | undefined, b: AgentTurn | null | undefined): boolean {
    return !!a && !!b && a.conversation_id === b.conversation_id && a.wait_reason === b.wait_reason;
}

/** The state after an `agent_turn_updated` event; an older `ts` is ignored. */
export function applyTurnSnapshot(states: AgentTurnStates, snap: AgentTurnSnapshot, nowMs: number): AgentTurnStates {
    if (!snap?.agent_id) return states;
    const prev = states[snap.agent_id];
    if (prev?.fromEvent && prev.ts && snap.ts && snap.ts < prev.ts) return states;
    const turn = snap.turn ?? null;
    return {
        ...states,
        [snap.agent_id]: {
            agent_id: snap.agent_id,
            turn,
            waiting: Array.isArray(snap.waiting) ? snap.waiting : [],
            ts: snap.ts,
            lastEventAt: nowMs,
            waitSeenAt: sameWait(prev?.turn, turn) ? prev!.waitSeenAt : nowMs,
            fromEvent: true,
        },
    };
}

/** Seed from `list_agents` rows. An agent an event has already described is left alone:
 *  the events are full snapshots sent on every change, so the newest one is the truth. */
export function seedTurnsFromAgents(states: AgentTurnStates, agents: any[], nowMs: number): AgentTurnStates {
    let next = states;
    for (const row of agents ?? []) {
        const id = row?.agent_id;
        if (!id || !('turn' in row) || next[id]?.fromEvent) continue;
        const turn: AgentTurn | null = row.turn ?? null;
        const started = turn?.started_at ? Date.parse(turn.started_at) : NaN;
        next = {
            ...next,
            [id]: {
                agent_id: id,
                turn,
                waiting: Array.isArray(row.waiting) ? row.waiting : [],
                lastEventAt: nowMs,
                waitSeenAt: Number.isFinite(started) ? started : nowMs,
                fromEvent: false,
            },
        };
    }
    return next;
}

/** Any other event from an agent (progress, a text chunk) confirms its turn is alive. */
export function noteAgentActivity(states: AgentTurnStates, agentId: string, nowMs: number): AgentTurnStates {
    const prev = states[agentId];
    if (!prev) return states;
    return { ...states, [agentId]: { ...prev, lastEventAt: nowMs } };
}

/** A text chunk names only its chat: it confirms the agent whose turn is in that chat. */
export function noteConversationActivity(states: AgentTurnStates, conversationId: string, nowMs: number): AgentTurnStates {
    for (const s of Object.values(states)) {
        if (s.turn && s.turn.conversation_id === conversationId) {
            return noteAgentActivity(states, s.agent_id, nowMs);
        }
    }
    return states;
}

export function formatWait(seconds: number): string {
    const s = Math.max(0, Math.floor(seconds));
    if (s < 60) return `${s} s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m} min ${s % 60} s`;
    return `${Math.floor(m / 60)} h ${m % 60} min`;
}

export function ordinal(n: number): string {
    const tens = n % 100;
    if (tens >= 11 && tens <= 13) return `${n}th`;
    switch (n % 10) {
        case 1: return `${n}st`;
        case 2: return `${n}nd`;
        case 3: return `${n}rd`;
        default: return `${n}th`;
    }
}

/** How long since an ISO moment, as formatWait words; an unreadable moment reads 0 s. */
export function since(iso: string | null | undefined, nowMs: number): string {
    const t = iso ? Date.parse(iso) : NaN;
    return formatWait(Number.isFinite(t) ? (nowMs - t) / 1000 : 0);
}

// The phrases the waiting chat's line and the Agent map share, so the two never
// say the same state in two wordings.

/** What a turn's wait_reason says: `waiting for the model`, `waiting for an approval`, or null. */
export function turnWaitPhrase(reason: TurnWaitReason | null | undefined): string | null {
    if (reason === 'model_call') return 'waiting for the model';
    if (reason === 'approval') return 'waiting for an approval';
    return null;
}

/** Why a waiting chat is not moving: `busy in another chat`, or who works in the room. */
export function waitingReasonPhrase(w: WaitingChat, nameOf: (agentId: string) => string): string {
    if (w.wait_reason === 'room_busy') {
        const other = w.held_by?.agent_id ? nameOf(w.held_by.agent_id) : 'another agent';
        return `${other} is working in this room`;
    }
    return 'busy in another chat';
}

/** Said after the reason when the turn holding the lock itself waits for an approval. */
export function heldByApprovalSuffix(w: WaitingChat): string {
    return w.held_by?.wait_reason === 'approval' ? ', which waits for an approval' : '';
}

/** The silence that makes a turn unconfirmed, in words. */
export function silencePhrase(state: AgentTurnState, nowMs: number): string {
    return `nothing from it for ${formatWait((nowMs - state.lastEventAt) / 1000)}`;
}

/** True when the turn is non-null and nothing has come from its agent for TURN_UNCONFIRMED_AFTER_S. */
export function turnIsUnconfirmed(state: AgentTurnState, nowMs: number): boolean {
    return !!state.turn && nowMs - state.lastEventAt > TURN_UNCONFIRMED_AFTER_S * 1000;
}
