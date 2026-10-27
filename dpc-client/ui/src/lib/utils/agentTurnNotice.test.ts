import { describe, it, expect } from 'vitest';
import {
    TURN_UNCONFIRMED_AFTER_S,
    applyTurnSnapshot,
    seedTurnsFromAgents,
    noteAgentActivity,
    noteConversationActivity,
    turnIsUnconfirmed,
    ordinal,
    formatWait,
    type AgentTurn,
    type AgentTurnStates,
} from './agentTurnNotice';

const NOW = Date.parse('2026-10-27T10:00:00Z');
const iso = (secondsAgo: number) => new Date(NOW - secondsAgo * 1000).toISOString();

function turn(over: Partial<AgentTurn> = {}): AgentTurn {
    return {
        conversation_id: 'group-a', conversation_title: 'DPC project', started_at: iso(30),
        round: 2, provider_alias: null, tool: null, wait_reason: null, ...over,
    };
}

function states(snaps: any[], at = NOW): AgentTurnStates {
    return snaps.reduce((m, s) => applyTurnSnapshot(m, s, at), {} as AgentTurnStates);
}

describe('the 120 s rule', () => {
    it('is 120 seconds', () => {
        expect(TURN_UNCONFIRMED_AFTER_S).toBe(120);
    });

    it('a turn with no event for longer than that is unconfirmed', () => {
        const st = states([{ agent_id: 'agent_ark', ts: 't1', waiting: [],
                             turn: turn({ wait_reason: 'model_call', provider_alias: 'x' }) }],
                          NOW - 121_000);
        expect(turnIsUnconfirmed(st.agent_ark, NOW)).toBe(true);
        expect(turnIsUnconfirmed(st.agent_ark, NOW - 2_000)).toBe(false);
    });

    it('any later event from the agent confirms the turn again', () => {
        let st = states([{ agent_id: 'agent_ark', ts: 't1', waiting: [], turn: turn() }], NOW - 200_000);
        st = noteAgentActivity(st, 'agent_ark', NOW - 1000);
        expect(turnIsUnconfirmed(st.agent_ark, NOW)).toBe(false);
        st = states([{ agent_id: 'agent_ark', ts: 't1', waiting: [], turn: turn() }], NOW - 200_000);
        st = noteConversationActivity(st, 'group-a', NOW - 1000);
        expect(turnIsUnconfirmed(st.agent_ark, NOW)).toBe(false);
    });

    it('an idle agent is never unconfirmed', () => {
        const st = states([{ agent_id: 'agent_ark', ts: 't1', waiting: [], turn: null }], NOW - 999_000);
        expect(turnIsUnconfirmed(st.agent_ark, NOW)).toBe(false);
    });
});

describe('the store feeding the line', () => {
    it('is seeded from list_agents rows and then replaced whole by events', () => {
        let st = seedTurnsFromAgents({}, [
            { agent_id: 'agent_ark', name: 'Ark', turn: turn(), waiting: [] },
            { agent_id: 'agent_old', name: 'Old' },
        ], NOW);
        expect(st.agent_ark.turn?.conversation_id).toBe('group-a');
        expect(st.agent_old).toBeUndefined();
        st = applyTurnSnapshot(st, { agent_id: 'agent_ark', turn: null, waiting: [], ts: 't2' }, NOW);
        expect(st.agent_ark.turn).toBeNull();
        // A later list_agents row does not undo what an event said.
        st = seedTurnsFromAgents(st, [{ agent_id: 'agent_ark', turn: turn(), waiting: [] }], NOW);
        expect(st.agent_ark.turn).toBeNull();
    });

    it('an older snapshot arriving late is ignored', () => {
        let st = applyTurnSnapshot({}, { agent_id: 'agent_ark', turn: null, waiting: [],
                                         ts: '2026-10-27T10:00:02+00:00' }, NOW);
        st = applyTurnSnapshot(st, { agent_id: 'agent_ark', turn: turn(), waiting: [],
                                     ts: '2026-10-27T10:00:01+00:00' }, NOW);
        expect(st.agent_ark.turn).toBeNull();
    });
});

describe('words', () => {
    it('ordinals', () => {
        expect([1, 2, 3, 4, 11, 12, 13, 21, 22, 23].map(ordinal))
            .toEqual(['1st', '2nd', '3rd', '4th', '11th', '12th', '13th', '21st', '22nd', '23rd']);
    });
    it('durations', () => {
        expect(formatWait(5)).toBe('5 s');
        expect(formatWait(65)).toBe('1 min 5 s');
        expect(formatWait(3720)).toBe('1 h 2 min');
    });
});
