import { describe, it, expect } from 'vitest';
import {
    TURN_COMMAND_AFTER_TURN_MS,
    TURN_COMMAND_TIMEOUT_MS,
    chatHasTurnOrWaits,
    isTurnCommand,
    noteTurnCommandLook,
    turnCommandTimedOut,
} from './turnCommandWait';
import type { AgentTurnStates } from './agentTurnNotice';

function states(turnChat: string | null, waitingChats: string[] = []): AgentTurnStates {
    return {
        agent_001: {
            agent_id: 'agent_001',
            turn: turnChat === null ? null : {
                conversation_id: turnChat, conversation_title: 'Johnny', started_at: '2026-10-29T14:58:23Z',
                round: 12, provider_alias: 'ollama', tool: null, wait_reason: 'model_call',
            },
            waiting: waitingChats.map((c) => ({
                conversation_id: c, conversation_title: c, waiting_since: '2026-10-29T14:58:23Z',
                wait_reason: 'other_chat' as const, held_by: null,
            })),
            lastEventAt: 0, waitSeenAt: 0, fromEvent: true,
        },
    };
}

describe('isTurnCommand', () => {
    it('is send_image routed to the agent', () => {
        expect(isTurnCommand('send_image', { chat_provider: 'dpc_agent', conversation_id: 'agent_001' })).toBe(true);
    });

    it('is not the plain AI chat nor another command', () => {
        expect(isTurnCommand('send_image', { chat_provider: null, conversation_id: 'ai_chat_1' })).toBe(false);
        expect(isTurnCommand('send_image', { chat_provider: 'dpc_agent' })).toBe(false);
        expect(isTurnCommand('transcribe_audio', { chat_provider: 'dpc_agent', conversation_id: 'agent_001' })).toBe(false);
    });
});

describe('chatHasTurnOrWaits', () => {
    it('sees a turn in the chat, also one in a long model call', () => {
        expect(chatHasTurnOrWaits(states('agent_001'), 'agent_001')).toBe(true);
    });

    it('sees the chat waiting while the agent works in another one', () => {
        expect(chatHasTurnOrWaits(states('telegram-5', ['agent_001']), 'agent_001')).toBe(true);
    });

    it('sees nothing when the agent is idle, busy elsewhere, or not known', () => {
        expect(chatHasTurnOrWaits(states(null), 'agent_001')).toBe(false);
        expect(chatHasTurnOrWaits(states('telegram-5'), 'agent_001')).toBe(false);
        expect(chatHasTurnOrWaits({}, 'agent_001')).toBe(false);
    });
});

describe('turnCommandTimedOut', () => {
    const sentAt = 1_000_000;

    it('waits the first 240 s for the turn to appear', () => {
        const w = { sentAt, lastTurnSeenAt: null };
        expect(turnCommandTimedOut(w, sentAt + TURN_COMMAND_TIMEOUT_MS)).toBeNull();
        expect(turnCommandTimedOut(w, sentAt + TURN_COMMAND_TIMEOUT_MS + 1)).toMatch(/no agent turn appeared/);
    });

    it('never gives up while the turn is still seen, however long it runs', () => {
        let w = { sentAt, lastTurnSeenAt: null as number | null };
        for (let t = sentAt; t <= sentAt + 3_600_000; t += 5_000) {
            w = noteTurnCommandLook(w, true, t);
            expect(turnCommandTimedOut(w, t)).toBeNull();
        }
    });

    it('gives up a minute after the turn it saw is gone', () => {
        const seen = sentAt + 249_000;
        const w = noteTurnCommandLook({ sentAt, lastTurnSeenAt: null }, true, seen);
        expect(noteTurnCommandLook(w, false, seen + 5_000)).toEqual(w);
        expect(turnCommandTimedOut(w, seen + TURN_COMMAND_AFTER_TURN_MS)).toBeNull();
        expect(turnCommandTimedOut(w, seen + TURN_COMMAND_AFTER_TURN_MS + 1)).toMatch(/ended 60 s ago/);
    });
});
