/**
 * The strip showed one agent's tokens per second under another agent's answer.
 *
 * Observed to the second: the DPC Project footer under Ark's message read
 * `decode: 55 tok/s | context: 56 790 / 262 144 | qwen3.8 27b Mythos` while the
 * backend log for that same second recorded his turn on `alias=deepseek_pro`.
 * The numbers were the local engine's, left standing by "keep the last
 * non-empty value" — a rule that is correct inside one run and has no idea
 * whose value it is holding across two.
 *
 * These cover the three rules that pull against each other, because a later
 * tidy-up would reverse any one of them on its own:
 *
 * - update-only survives inside a run (the tool-argument burst carries no
 *   speed, and nulling on it made the counter flash and die);
 * - it does NOT survive a change of agent, so a provider that reports no speed
 *   shows none rather than inheriting one;
 * - and `agent_id: ''` is the singleton agent, not a missing field. The backend
 *   sends `self.agent_id or ''` on every event, so reading the empty string as
 *   silence would leave the singleton — the configuration most installs run —
 *   inheriting every named agent's numbers, with all the other tests still green.
 */

import { describe, it, expect } from 'vitest';
import { nextStrip, clearedStrip } from './speedStripOwner';

const local = { model: 'qwen3.8 27b Mythos', decode_tok_s: 55, context_window: 262144 };
const faster = { model: 'qwen3.8 27b Mythos', decode_tok_s: 61, context_window: 262144 };

describe('within one agent run', () => {
    it('takes the first speed and remembers whose it is', () => {
        const u = nextStrip(null, null, { agent_id: 'agent_local', speed: local });
        expect(u.ownerAgentId).toBe('agent_local');
        expect(u.speed).toEqual(local);
        expect(u.appendSample).toEqual(local);
    });

    it('keeps the last speed through events that carry none', () => {
        // The burst after a round: tool arguments and per-tool narration, no speed.
        const u = nextStrip('agent_local', local, { agent_id: 'agent_local' });
        expect(u.speed).toEqual(local);
        expect(u.appendSample).toBeNull();
        expect(u.resetSamples).toBe(false);
    });

    it('replaces it when the next round reports', () => {
        const u = nextStrip('agent_local', local, { agent_id: 'agent_local', speed: faster });
        expect(u.speed).toEqual(faster);
        expect(u.appendSample).toEqual(faster);
        expect(u.resetSamples).toBe(false);
    });
});

describe('when a different agent answers in the same chat', () => {
    it('does not lend the previous agent its tokens per second', () => {
        // The observed case: an agent on a paid API sends no speed at all, and
        // used to be painted with whatever the local engine last left behind.
        const u = nextStrip('agent_local', local, { agent_id: 'ark' });
        expect(u.ownerAgentId).toBe('ark');
        expect(u.speed).toBeNull();
        expect(u.resetSamples).toBe(true);
        expect(u.appendSample).toBeNull();
    });

    it('drops the previous agent samples before adding its own', () => {
        // Otherwise the finished header's medians are computed across two
        // models — and they are written onto the message, so the wrong number
        // outlives the run it came from.
        const u = nextStrip('agent_local', local, { agent_id: 'ark', speed: faster });
        expect(u.resetSamples).toBe(true);
        expect(u.appendSample).toEqual(faster);
        expect(u.speed).toEqual(faster);
    });

    it('and the same agent answering again is not a change', () => {
        const u = nextStrip('agent_local', local, { agent_id: 'agent_local', speed: faster });
        expect(u.resetSamples).toBe(false);
    });
});

describe('the singleton agent, whose id is the empty string', () => {
    it('takes the strip back from a named agent', () => {
        // `self.agent_id or ''` — the singleton manager has no id, and this is
        // the case a rule that treats '' as "unknown" leaves broken.
        const u = nextStrip('ark', local, { agent_id: '', speed: faster });
        expect(u.ownerAgentId).toBe('');
        expect(u.resetSamples).toBe(true);
        expect(u.speed).toEqual(faster);
    });

    it('and does not lose its own samples to its own next round', () => {
        const u = nextStrip('', local, { agent_id: '', speed: faster });
        expect(u.ownerAgentId).toBe('');
        expect(u.resetSamples).toBe(false);
    });

    it('while a named agent still takes it back from the singleton', () => {
        const u = nextStrip('', local, { agent_id: 'ark' });
        expect(u.ownerAgentId).toBe('ark');
        expect(u.resetSamples).toBe(true);
        expect(u.speed).toBeNull();
    });
});

describe('an event that says nothing about the owner', () => {
    it('changes nothing, because a missing field is not an identity', () => {
        for (const event of [{}, { speed: null }, undefined, null]) {
            const u = nextStrip('agent_local', local, event as any);
            expect(u.ownerAgentId).toBe('agent_local');
            expect(u.resetSamples).toBe(false);
            expect(u.speed).toEqual(local);
        }
    });
});

describe('leaving the chat', () => {
    it('describes nobody, so the next room starts empty', () => {
        // The half that was actually seen on screen: two chats showing one
        // identical `197,235 / 1,000,000` because the switch cleared the round
        // count and not the name, the speed or the samples.
        const u = clearedStrip();
        expect(u.ownerAgentId).toBeNull();
        expect(u.speed).toBeNull();
        expect(u.resetSamples).toBe(true);
        expect(u.appendSample).toBeNull();
    });
});
