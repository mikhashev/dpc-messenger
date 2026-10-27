import { describe, it, expect } from 'vitest';
import {
    TURN_UNCONFIRMED_AFTER_S,
    applyTurnSnapshot,
    seedTurnsFromAgents,
    noteAgentActivity,
    noteConversationActivity,
    turnNoticeLines,
    ordinal,
    formatWait,
    type AgentTurn,
    type AgentTurnStates,
} from './agentTurnNotice';

const NOW = Date.parse('2026-10-27T10:00:00Z');
const iso = (secondsAgo: number) => new Date(NOW - secondsAgo * 1000).toISOString();
const names: Record<string, string> = { agent_ark: 'Ark', agent_johnny: 'Johnny' };
const nameOf = (id: string) => names[id] ?? id;

function turn(over: Partial<AgentTurn> = {}): AgentTurn {
    return {
        conversation_id: 'group-a', conversation_title: 'DPC project', started_at: iso(30),
        round: 2, provider_alias: null, tool: null, wait_reason: null, ...over,
    };
}

function states(snaps: any[], at = NOW): AgentTurnStates {
    return snaps.reduce((m, s) => applyTurnSnapshot(m, s, at), {} as AgentTurnStates);
}

describe('the line in a waiting chat', () => {
    it('says the agent is busy in another chat, the place in line and the wait', () => {
        const st = states([{
            agent_id: 'agent_ark', ts: 't1', turn: turn(),
            waiting: [
                { conversation_id: 'c1', conversation_title: 'Mike (1:1)', waiting_since: iso(50),
                  wait_reason: 'other_chat', held_by: { conversation_id: 'group-a', wait_reason: null } },
                { conversation_id: 'c2', conversation_title: 'Linus', waiting_since: iso(12),
                  wait_reason: 'other_chat', held_by: { conversation_id: 'group-a', wait_reason: null } },
            ],
        }]);
        expect(turnNoticeLines('c2', st, nameOf, NOW)).toEqual([
            'Ark is busy in another chat (DPC project) · this chat is 2nd in line · 12 s',
        ]);
        expect(turnNoticeLines('c1', st, nameOf, NOW)[0]).toContain('1st in line · 50 s');
    });

    it('adds that the holding turn waits for an approval', () => {
        const st = states([{
            agent_id: 'agent_ark', ts: 't1', turn: turn({ wait_reason: 'approval', tool: 'run_shell' }),
            waiting: [{ conversation_id: 'c1', conversation_title: 'Mike', waiting_since: iso(5),
                        wait_reason: 'other_chat', held_by: { conversation_id: 'group-a', wait_reason: 'approval' } }],
        }]);
        expect(turnNoticeLines('c1', st, nameOf, NOW)).toEqual([
            'Ark is busy in another chat (DPC project), which waits for an approval · this chat is 1st in line · 5 s',
        ]);
    });

    it('names the other agent working in the room for room_busy', () => {
        const st = states([{
            agent_id: 'agent_johnny', ts: 't1', turn: null,
            waiting: [{ conversation_id: 'group-a', conversation_title: 'DPC project', waiting_since: iso(75),
                        wait_reason: 'room_busy',
                        held_by: { conversation_id: 'group-a', wait_reason: 'approval', agent_id: 'agent_ark' } }],
        }]);
        expect(turnNoticeLines('group-a', st, nameOf, NOW)).toEqual([
            'Johnny waits: Ark is working in this room, which waits for an approval · 1 min 15 s',
        ]);
    });

    it('in the turn\'s own chat, says it waits for the model and which one', () => {
        const st = states([{ agent_id: 'agent_ark', ts: 't1', waiting: [],
                             turn: turn({ wait_reason: 'model_call', provider_alias: 'deepseek_flash' }) }],
                          NOW - 8000);
        expect(turnNoticeLines('group-a', st, nameOf, NOW)).toEqual([
            'Ark is waiting for the model (deepseek_flash) · 8 s',
        ]);
    });

    it('in the turn\'s own chat, says it waits for an approval of which tool', () => {
        const st = states([{ agent_id: 'agent_ark', ts: 't1', waiting: [],
                             turn: turn({ wait_reason: 'approval', tool: 'schedule_task' }) }], NOW - 3000);
        expect(turnNoticeLines('group-a', st, nameOf, NOW)).toEqual([
            'Ark is waiting for an approval (schedule_task) · 3 s',
        ]);
    });

    it('shows nothing while the turn simply works, and nothing for an unrelated chat', () => {
        const st = states([{ agent_id: 'agent_ark', ts: 't1', waiting: [], turn: turn({ tool: 'read_file' }) }]);
        expect(turnNoticeLines('group-a', st, nameOf, NOW)).toEqual([]);
        expect(turnNoticeLines('elsewhere', st, nameOf, NOW)).toEqual([]);
    });

    it('the wait clock keeps running across snapshots that keep the same wait', () => {
        const snap = { agent_id: 'agent_ark', waiting: [],
                       turn: turn({ wait_reason: 'model_call', provider_alias: 'x' }) };
        let st = applyTurnSnapshot({}, { ...snap, ts: 't1' }, NOW - 20000);
        st = applyTurnSnapshot(st, { ...snap, ts: 't2', turn: { ...snap.turn, round: 3 } }, NOW - 5000);
        expect(turnNoticeLines('group-a', st, nameOf, NOW)[0]).toMatch(/· 20 s$/);
    });
});

describe('the 120 s rule', () => {
    it('is 120 seconds', () => {
        expect(TURN_UNCONFIRMED_AFTER_S).toBe(120);
    });

    it('shows a turn with no event for longer than that as unconfirmed, not working', () => {
        const st = states([{ agent_id: 'agent_ark', ts: 't1', waiting: [],
                             turn: turn({ wait_reason: 'model_call', provider_alias: 'x' }) }],
                          NOW - 121_000);
        expect(turnNoticeLines('group-a', st, nameOf, NOW)).toEqual([
            "Ark's turn here is unconfirmed: nothing from it for 2 min 1 s",
        ]);
    });

    it('any later event from the agent confirms the turn again', () => {
        let st = states([{ agent_id: 'agent_ark', ts: 't1', waiting: [], turn: turn() }], NOW - 200_000);
        st = noteAgentActivity(st, 'agent_ark', NOW - 1000);
        expect(turnNoticeLines('group-a', st, nameOf, NOW)).toEqual([]);
        st = states([{ agent_id: 'agent_ark', ts: 't1', waiting: [], turn: turn() }], NOW - 200_000);
        st = noteConversationActivity(st, 'group-a', NOW - 1000);
        expect(turnNoticeLines('group-a', st, nameOf, NOW)).toEqual([]);
    });

    it('an idle agent is never unconfirmed', () => {
        const st = states([{ agent_id: 'agent_ark', ts: 't1', waiting: [], turn: null }], NOW - 999_000);
        expect(turnNoticeLines('group-a', st, nameOf, NOW)).toEqual([]);
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
