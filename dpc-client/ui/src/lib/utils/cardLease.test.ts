import { describe, it, expect } from 'vitest';
import { get } from 'svelte/store';
import {
    cardLeaseView,
    cardWaitNote,
    clockOfSeconds,
    estimateWords,
    newerSnapshot,
    type CardHolder,
    type CardLeaseSnapshot,
    type CardWaiter,
} from './cardLease';
import { buildAgentMap } from './agentMap';
import { applyTurnSnapshot, type AgentTurnStates } from './agentTurnNotice';
import { cardLease, onCardLeaseUpdated } from '../services/cardLease';
import mapSrc from '../components/AgentMap.svelte?raw';
import coreSrc from '../coreService.ts?raw';

const NOW_S = 1_800_000_000;
const NOW = NOW_S * 1000;

function holder(over: Partial<CardHolder> = {}): CardHolder {
    return {
        client: 'whisper:turbo', display: 'Whisper (turbo)', kind: 'transcription', engine: 'whisper',
        state: 'running', busy: true, purpose: 'dictation', agent_id: null, conversation_id: null,
        since: NOW_S - 68, eta_s: null, beside: null, ...over,
    };
}

function waiter(over: Partial<CardWaiter> = {}): CardWaiter {
    return {
        position: 1, client: 'music:yue2', display: 'yue2.cpp', kind: 'music', purpose: 'music job J1',
        agent_id: null, conversation_id: null, since: NOW_S - 41, give_up_at: null, until_cancelled: false,
        wait_s: null, wait_known: false, wait_upper_bound: false, waits_for: ['Whisper (turbo) (dictation)'],
        where: 'busy_holder', for_a_peer: false, ...over,
    };
}

function snap(over: Partial<CardLeaseSnapshot> = {}): CardLeaseSnapshot {
    const h = holder();
    return {
        schema: 'card_lease/1', updated_at: NOW_S, backend_pid: 1, free: false, closed: null, holder: h,
        holders: [h], waiters: [], recent: [], ...over,
    };
}

describe('estimateWords', () => {
    it('is unknown unless the lease knew, and never 0', () => {
        expect(estimateWords({ wait_s: null, wait_known: false, wait_upper_bound: false })).toBe('unknown');
        expect(estimateWords({ wait_s: 0, wait_known: false, wait_upper_bound: false })).toBe('unknown');
        expect(estimateWords({ wait_s: null, wait_known: true, wait_upper_bound: false })).toBe('unknown');
    });
    it('says about for a figure and at most for a declared bound', () => {
        expect(estimateWords({ wait_s: 20, wait_known: true, wait_upper_bound: false })).toBe('about 20 s');
        expect(estimateWords({ wait_s: 125, wait_known: true, wait_upper_bound: true })).toBe('at most 2 min 5 s');
    });
});

describe('cardLeaseView', () => {
    it('is null until a snapshot has come', () => {
        expect(cardLeaseView(null, NOW)).toBeNull();
    });

    it('says Free when nobody holds the card', () => {
        const view = cardLeaseView(snap({ free: true, holder: null, holders: [] }), NOW)!;
        expect(view.headline).toBe('Free');
        expect(view.free).toBe(true);
        expect(view.holders).toEqual([]);
    });

    it('names the holder, what it does, for which agent and since when', () => {
        const view = cardLeaseView(snap({ holders: [holder({ agent_id: 'agent_ark' })] }), NOW, (id) => (id === 'agent_ark' ? 'Ark' : id))!;
        expect(view.headline).toBe('Held by Whisper (turbo)');
        expect(view.holders[0]).toMatchObject({
            name: 'Whisper (turbo)', purpose: 'dictation · for Ark', state: 'running', busy: true,
            since: `since ${clockOfSeconds(NOW_S - 68)} · 1 min 8 s`,
        });
    });

    it('calls an idle holder idle and a supervised engine (no grant time) without a since', () => {
        const engine = holder({ client: 'qwen27', display: "llama-server 'qwen27'", kind: 'inference',
            busy: false, purpose: 'idle', since: null });
        const view = cardLeaseView(snap({ holders: [engine] }), NOW)!;
        expect(view.holders[0].purpose).toBe('idle');
        expect(view.holders[0].since).toBe('');
        expect(cardLeaseView(snap({ holders: [holder({ busy: false, purpose: null })] }), NOW)!.holders[0].purpose).toBe('idle');
    });

    it('lists the waiters in the order sent, with position, purpose, estimate and what each waits for', () => {
        const view = cardLeaseView(snap({
            waiters: [
                waiter({ wait_s: 20, wait_known: true }),
                waiter({ position: 2, client: 'qwen27', display: 'qwen27', kind: 'inference', purpose: 'a model call',
                    agent_id: 'agent_x', until_cancelled: true, since: NOW_S - 5,
                    waits_for: ['Whisper (turbo)', 'yue2.cpp'] }),
            ],
        }), NOW, (id) => (id === 'agent_x' ? 'Iris' : id))!;
        expect(view.headline).toBe('Held by Whisper (turbo) · 2 waiting');
        expect(view.waiters.map((w) => [w.position, w.name, w.purpose, w.waited, w.estimate, w.waitsFor, w.note])).toEqual([
            [1, 'yue2.cpp', 'music job J1', 'waiting 41 s', 'about 20 s', 'waits for Whisper (turbo) (dictation)', ''],
            [2, 'qwen27', 'a model call · for Iris', 'waiting 5 s', 'unknown', 'waits for Whisper (turbo), yue2.cpp', 'until cancelled'],
        ]);
    });

    it("marks a peer's request", () => {
        const view = cardLeaseView(snap({ waiters: [waiter({ for_a_peer: true })] }), NOW)!;
        expect(view.waiters[0].note).toBe("a peer's request");
    });

    it('lists the record newest first, in words', () => {
        const view = cardLeaseView(snap({
            recent: [
                { at: NOW_S - 200, event: 'granted', client: 'whisper:turbo', display: 'Whisper (turbo)', purpose: 'dictation', waited_s: 3 },
                { at: NOW_S - 120, event: 'released', client: 'whisper:turbo', display: 'Whisper (turbo)', purpose: 'dictation',
                    why: 'yield for music:yue2', held_s: 80 },
                { at: NOW_S - 90, event: 'left_the_line', client: 'music:yue2', display: 'yue2.cpp', purpose: 'music job J0', why: 'cancelled', waited_s: 30 },
            ],
        }), NOW)!;
        expect(view.recent.map((r) => r.text)).toEqual([
            'yue2.cpp (music job J0) left the line · waited 30 s · cancelled',
            'Whisper (turbo) (dictation) let go · held 1 min 20 s · yield for music:yue2',
            'Whisper (turbo) (dictation) got the card · waited 3 s',
        ]);
    });

    it('carries the backend stopping', () => {
        expect(cardLeaseView(snap({ closed: 'the backend is stopping' }), NOW)!.closed).toBe('the backend is stopping');
    });
});

describe('newerSnapshot', () => {
    it('keeps the newest by its own clock and ignores a payload that is not a snapshot', () => {
        const a = snap({ updated_at: 10 });
        const b = snap({ updated_at: 20 });
        expect(newerSnapshot(a, b)).toBe(b);
        expect(newerSnapshot(b, a)).toBe(b);
        expect(newerSnapshot(a, null)).toBe(a);
        expect(newerSnapshot(a, { nonsense: true })).toBe(a);
        expect(newerSnapshot(null, b)).toBe(b);
    });

    it('the store takes a pushed snapshot and not an older one after it', () => {
        cardLease.set(null);
        onCardLeaseUpdated(snap({ updated_at: 20 }));
        onCardLeaseUpdated(snap({ updated_at: 10, free: true }));
        expect(get(cardLease)!.updated_at).toBe(20);
        cardLease.set(null);
    });
});

describe('cardWaitNote and the agent NOW line', () => {
    const waiting = snap({
        waiters: [waiter({ position: 2, agent_id: 'agent_ark', waits_for: ['Whisper (turbo) (dictation)'] })],
    });

    it('gives an agent its place and what it stands behind', () => {
        expect(cardWaitNote(waiting, 'agent_ark')).toBe('2nd in line for the card, behind Whisper (turbo) (dictation)');
        expect(cardWaitNote(waiting, 'agent_other')).toBe('');
        expect(cardWaitNote(null, 'agent_ark')).toBe('');
    });

    it('names the holder when the waiter lists nothing it waits for', () => {
        const s = snap({ waiters: [waiter({ agent_id: 'agent_ark', waits_for: [] })] });
        expect(cardWaitNote(s, 'agent_ark')).toBe('1st in line for the card, behind Whisper (turbo)');
    });

    it('is on the NOW detail of a turn that waits for the card, and only then', () => {
        const turn = { conversation_id: 'group-a', conversation_title: 'DPC Project', started_at: new Date(NOW - 60000).toISOString(),
            round: 2, provider_alias: 'qwen27', tool: null };
        const states = (reason: string | null): AgentTurnStates => applyTurnSnapshot({}, {
            agent_id: 'agent_ark', turn: { ...turn, wait_reason: reason } as any, waiting: [], ts: NOW,
        } as any, NOW);
        const build = (reason: string | null, card: CardLeaseSnapshot | null) => buildAgentMap({
            agents: [{ agent_id: 'agent_ark', name: 'Ark' }], turns: states(reason), listing: null,
            names: { groups: new Map(), peers: [] }, nowMs: NOW, card,
        }).agents[0];

        const waitingForCard = build('card_queue', waiting);
        expect(waitingForCard.nowLine).toBe('Waiting for the card in DPC Project');
        expect(waitingForCard.nowDetail).toContain('qwen27 waits in the queue');
        expect(waitingForCard.nowDetail).toContain('2nd in line for the card, behind Whisper (turbo) (dictation)');
        expect(build('card_queue', null).nowDetail).not.toContain('in line for the card');
        expect(build('model_call', waiting).nowDetail).not.toContain('in line for the card');
    });
});

describe('the wiring', () => {
    it('the map reads the command, shows the block under the memory bars and passes the queue to the NOW line', () => {
        expect(mapSrc).toContain("sendCommand('get_card_lease_status'");
        expect(mapSrc).toContain('card: $cardLease');
        expect(mapSrc).toContain('cardLeaseView(');
        expect(mapSrc.indexOf('class="card-block"')).toBeGreaterThan(mapSrc.indexOf('class="memory"'));
        expect(mapSrc.indexOf('class="card-block"')).toBeLessThan(mapSrc.indexOf('class="map-columns"'));
    });

    it('the core service stores the pushed event', () => {
        expect(coreSrc).toContain('message.event === "card_lease_updated"');
        expect(coreSrc).toContain('onCardLeaseUpdated(message.payload)');
    });
});
