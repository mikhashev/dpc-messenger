import { describe, it, expect } from 'vitest';
import src from '../components/ProvidersEditor.svelte?raw';
import { backgroundSkipLines, cardQueueLines, otherCardHolderLines } from './cardQueue';

const time = (seconds: number) => new Date(seconds * 1000).toLocaleTimeString();
const dateAndTime = (seconds: number) => new Date(seconds * 1000).toLocaleString();
const QUEUED = 1793000000;
const GIVE_UP = QUEUED + 300;

describe('cardQueueLines', () => {
    it('says who waits, in the order given, since when and until when', () => {
        const lines = cardQueueLines([
            { alias: 'flash', agent_id: 'agent_1', conversation_id: 'agent_1', queued_at: QUEUED, give_up_at: GIVE_UP },
            { alias: 'qwen', agent_id: null, conversation_id: 'local_ai', queued_at: QUEUED, give_up_at: GIVE_UP },
            { alias: 'third', agent_id: null, conversation_id: null, queued_at: QUEUED, give_up_at: GIVE_UP },
        ]);
        const tail = `in line since ${time(QUEUED)}, gives up at ${time(GIVE_UP)}`;
        expect(lines).toEqual([
            `flash (agent agent_1, chat agent_1): ${tail}`,
            `qwen (chat local_ai): ${tail}`,
            `third: ${tail}`,
        ]);
    });

    it('names no time for a turn on its way back to its own alias, until its bound starts', () => {
        expect(cardQueueLines([
            { alias: 'qwen', agent_id: 'agent_1', conversation_id: null, queued_at: QUEUED, give_up_at: null },
        ])).toEqual([
            `qwen (agent agent_1): in line since ${time(QUEUED)}, coming back from a description`,
        ]);
    });

    it('is empty with nobody in line and on a backend that has no such key', () => {
        expect(cardQueueLines([])).toEqual([]);
        expect(cardQueueLines(undefined)).toEqual([]);
    });
});

describe('otherCardHolderLines', () => {
    it('leaves out the first holder, which the card holder line names', () => {
        expect(otherCardHolderLines([
            { alias: 'a', engine: 'llama.cpp', state: 'running', port: 1 },
            { alias: 'b', engine: 'llama.cpp', state: 'starting', port: 2 },
        ])).toEqual(['Also on the card: b (llama.cpp, starting)']);
        expect(otherCardHolderLines([{ alias: 'a', engine: 'strata', state: 'running', port: 1 }])).toEqual([]);
        expect(otherCardHolderLines(null)).toEqual([]);
    });
});

describe('backgroundSkipLines', () => {
    it('names the call, the alias, the reason and what follows, the latest first', () => {
        const lines = backgroundSkipLines({
            skill_reflection: {
                alias: 'flash', reason: "'flash' is not on the card", what_follows: 'This reflection ends.', at: QUEUED,
            },
            self_reflection_confirmation: {
                alias: 'qwen', reason: "a request is waiting in the queue for the card for 'flash'",
                what_follows: 'It is treated as general.', at: GIVE_UP,
            },
        });
        expect(lines).toEqual([
            `Background call self_reflection_confirmation skipped at ${dateAndTime(GIVE_UP)} on qwen: `
                + "a request is waiting in the queue for the card for 'flash'. It is treated as general.",
            `Background call skill_reflection skipped at ${dateAndTime(QUEUED)} on flash: `
                + "'flash' is not on the card. This reflection ends.",
        ]);
    });

    it('carries the date, because a row stays until the backend restarts', () => {
        const [line] = backgroundSkipLines({
            skill_reflection: { alias: 'a', reason: 'r', what_follows: 'f', at: QUEUED },
        });
        expect(line).toContain(new Date(QUEUED * 1000).toLocaleDateString());
    });

    it('is empty before any skip and on a backend that has no such key', () => {
        expect(backgroundSkipLines({})).toEqual([]);
        expect(backgroundSkipLines(undefined)).toEqual([]);
    });
});

// No mount harness: the wiring is checked in the source, the way the other ProvidersEditor guards do.
describe('the Local engines block shows them', () => {
    const start = src.indexOf('{#snippet cardStatus(');
    const block = src.slice(start, src.indexOf('{/snippet}', start));

    it('renders the queue after the card holder, and the other two lines beside it', () => {
        const holder = block.indexOf('<strong>Card holder:</strong>');
        expect(holder).toBeGreaterThan(0);
        for (const line of ['otherCardHolderLines(engineStatus?.card_holders)',
            'cardQueueLines(engineStatus?.card_queue)',
            'backgroundSkipLines(engineStatus?.last_background_skip)']) {
            expect(block.indexOf(line), line).toBeGreaterThan(holder);
        }
    });

    it('says nobody waits, so an empty line does not read as a missing feature', () => {
        const empty = block.indexOf('{:else if engineStatus?.card_queue}');
        expect(empty).toBeGreaterThan(block.indexOf('cardQueueLines(engineStatus?.card_queue)'));
        expect(block.slice(empty)).toContain('<strong>Waiting for the card:</strong> nobody');
    });

    it('feeds the three keys from local_engine_status', () => {
        expect(src).toContain('card_holders: r.card_holders ?? []');
        expect(src).toContain('card_queue: r.card_queue ?? []');
        expect(src).toContain('last_background_skip: r.last_background_skip ?? {}');
    });
});
