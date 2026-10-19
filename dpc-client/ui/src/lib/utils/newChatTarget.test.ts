/**
 * Mike, 2026-10-19: in "Create New AI Agent", an AI Host that is a peer and the
 * peer's model (strata) ended in the alert "Provider 'dpc_agent' not found." and
 * nothing was created. The dialog looked the agent runtime up in the peer's list;
 * a peer never advertises it.
 */

import { describe, it, expect } from 'vitest';
import { resolveNewChatTarget, defaultAgentLlmAlias, type ProviderRow } from './newChatTarget';

const LOCAL: ProviderRow[] = [
    { alias: 'dpc_agent', model: 'agent', context_window: 1000 },
    { alias: 'deepseek_flash', model: 'deepseek-v4-flash', context_window: 128000 },
];
const PEER_ID = 'dpc-node-0123456789abcdef0123456789abcdef';
const PEER: ProviderRow[] = [{ alias: 'strata', model: 'strata-35b', context_window: 262144 }];

/** The dialog's old lookup, kept to show the bug the new function removes. */
function oldLookup(chatProvider: string, host: string): ProviderRow | undefined {
    const list = host === 'local' ? LOCAL : PEER;
    return list.find((p) => p.alias === chatProvider);
}

describe('the old lookup', () => {
    it('could not find the agent runtime in a peer list', () => {
        expect(oldLookup('dpc_agent', PEER_ID)).toBeUndefined();
    });
});

describe('an agent on a peer host', () => {
    it('is created with the peer as compute host and the peer model as its LLM', () => {
        const r = resolveNewChatTarget({
            chatProvider: 'dpc_agent', llmAlias: 'strata', host: PEER_ID, hostLabel: 'Linux',
            localProviders: LOCAL, hostProviders: PEER, defaultProvider: 'deepseek_flash',
        });
        expect(r).toMatchObject({
            ok: true, computeHost: PEER_ID, agentLlmAlias: 'strata', contextWindow: 262144,
        });
    });

    it('with no model asks for one and names the peer', () => {
        const r = resolveNewChatTarget({
            chatProvider: 'dpc_agent', llmAlias: '', host: PEER_ID, hostLabel: 'Linux',
            localProviders: LOCAL, hostProviders: PEER, defaultProvider: 'deepseek_flash',
        });
        expect(r).toEqual({ ok: false, message: 'Pick a model offered by Linux.' });
    });

    it('does not fall back to the local default when the model is empty', () => {
        const r = resolveNewChatTarget({
            chatProvider: 'dpc_agent', llmAlias: '', host: PEER_ID,
            localProviders: LOCAL, hostProviders: PEER, defaultProvider: 'deepseek_flash',
        });
        expect(r.ok).toBe(false);
    });

    it('refuses a model the peer does not offer', () => {
        const r = resolveNewChatTarget({
            chatProvider: 'dpc_agent', llmAlias: 'deepseek_flash', host: PEER_ID, hostLabel: 'Linux',
            localProviders: LOCAL, hostProviders: PEER,
        });
        expect(r).toEqual({ ok: false, message: "Linux does not offer the model 'deepseek_flash'." });
    });

    it('still needs the runtime on this machine', () => {
        const r = resolveNewChatTarget({
            chatProvider: 'dpc_agent', llmAlias: 'strata', host: PEER_ID,
            localProviders: [LOCAL[1]], hostProviders: PEER,
        });
        expect(r).toEqual({ ok: false, message: "Provider 'dpc_agent' not found." });
    });
});

describe('an agent on the local host', () => {
    it('keeps its chosen LLM and has no compute host', () => {
        const r = resolveNewChatTarget({
            chatProvider: 'dpc_agent', llmAlias: 'deepseek_flash', host: 'local',
            localProviders: LOCAL, hostProviders: LOCAL, defaultProvider: 'other',
        });
        expect(r).toMatchObject({ ok: true, agentLlmAlias: 'deepseek_flash', contextWindow: 128000 });
        expect(r.ok && r.computeHost).toBeUndefined();
    });

    it('falls back to the default provider when none is chosen', () => {
        const r = resolveNewChatTarget({
            chatProvider: 'dpc_agent', llmAlias: '', host: 'local',
            localProviders: LOCAL, hostProviders: LOCAL, defaultProvider: 'deepseek_flash',
        });
        expect(r).toMatchObject({ ok: true, agentLlmAlias: 'deepseek_flash' });
    });
});

describe('a plain chat', () => {
    it('on a peer is looked up in the peer list and goes to that host', () => {
        const r = resolveNewChatTarget({
            chatProvider: 'strata', llmAlias: '', host: PEER_ID,
            localProviders: LOCAL, hostProviders: PEER,
        });
        expect(r).toMatchObject({ ok: true, computeHost: PEER_ID, provider: { alias: 'strata' } });
        expect(r.ok && r.agentLlmAlias).toBeUndefined();
    });

    it('on a peer that does not offer the alias keeps the old message', () => {
        const r = resolveNewChatTarget({
            chatProvider: 'deepseek_flash', llmAlias: '', host: PEER_ID,
            localProviders: LOCAL, hostProviders: PEER,
        });
        expect(r).toEqual({ ok: false, message: "Provider 'deepseek_flash' not found." });
    });

    it('on the local host is looked up locally', () => {
        const r = resolveNewChatTarget({
            chatProvider: 'deepseek_flash', llmAlias: '', host: 'local',
            localProviders: LOCAL, hostProviders: LOCAL,
        });
        expect(r).toMatchObject({ ok: true, provider: { alias: 'deepseek_flash' } });
        expect(r.ok && r.computeHost).toBeUndefined();
    });
});

describe('the default LLM for a host', () => {
    it('is the first alias that is not the runtime', () => {
        expect(defaultAgentLlmAlias(LOCAL)).toBe('deepseek_flash');
        expect(defaultAgentLlmAlias(PEER)).toBe('strata');
    });
    it('is empty when the host has no list yet', () => {
        expect(defaultAgentLlmAlias(undefined)).toBe('');
        expect(defaultAgentLlmAlias([])).toBe('');
    });
});
