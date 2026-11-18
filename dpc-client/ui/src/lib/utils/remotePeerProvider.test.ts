import { describe, it, expect } from 'vitest';
import {
  peerChoices, peerDisplayName, peerModelChoices, remotePeerFields, remotePeerModelState,
  NO_PEERS_TEXT, PEER_OFFLINE_TEXT, PEER_OFFERS_NOTHING_TEXT,
  REMOTE_PEER_TYPE, REMOTE_PEER_TYPE_LABEL,
} from './remotePeerProvider';
import { suggestProviderAlias, validateProviderDraftForAdd } from './providerDraft';
import { NEVER_OFFERED_TYPES, classifyProviderType, offeredProviders } from '../components/inferenceSharing';

const ALICE = 'dpc-node-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
const BOB = 'dpc-node-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';

const ROWS = [
  { alias: 'qwen_vision', model: 'qwen3.8-27b', type: 'ollama', supports_vision: true },
  { alias: 'deepseek_flash', model: 'deepseek-v4-flash', type: 'deepseek' },
];

describe('the Type list', () => {
  it('names the type the way providers.json and llm_manager spell it', () => {
    expect(REMOTE_PEER_TYPE).toBe('remote_peer');
    expect(REMOTE_PEER_TYPE_LABEL).toContain('Remote Peer');
  });
});

describe('peerChoices — the picker lists the connected peers', () => {
  it('shows the name and a short node id for each', () => {
    const out = peerChoices([
      { node_id: ALICE, name: 'Alice', is_connected: true },
      { node_id: BOB, is_connected: true },
    ]);
    expect(out.map((c) => c.node_id)).toEqual([ALICE, BOB]);
    expect(out[0].label).toBe(`Alice | ${ALICE.slice(0, 20)}...`);
    expect(out[1].label).toBe(`${BOB.slice(0, 20)}...`);
    expect(out.every((c) => c.connected)).toBe(true);
  });

  it('lists nobody when no peer is connected, and a peer marked not connected is not listed', () => {
    expect(peerChoices([])).toEqual([]);
    expect(peerChoices(null)).toEqual([]);
    expect(peerChoices([{ node_id: ALICE, is_connected: false }])).toEqual([]);
  });

  it('keeps the peer stored on an alias when it is gone, marked, so opening the alias loses nothing', () => {
    const out = peerChoices([{ node_id: BOB, is_connected: true }], ALICE);
    expect(out.map((c) => [c.node_id, c.connected])).toEqual([[BOB, true], [ALICE, false]]);
    expect(out[1].label).toContain('(not connected)');
  });

  it('does not list a stored peer twice when it is connected', () => {
    expect(peerChoices([{ node_id: ALICE, is_connected: true }], ALICE)).toHaveLength(1);
  });
});

describe('peerModelChoices — the model picker lists what the peer offers', () => {
  it('turns each row into an option carrying the peer alias and the model', () => {
    const out = peerModelChoices(ROWS);
    expect(out.map((c) => [c.alias, c.model])).toEqual([
      ['qwen_vision', 'qwen3.8-27b'],
      ['deepseek_flash', 'deepseek-v4-flash'],
    ]);
    expect(out[0].label).toBe('qwen3.8-27b (qwen_vision)');
  });

  it('is empty for a peer that offers nothing, or one never asked', () => {
    expect(peerModelChoices([])).toEqual([]);
    expect(peerModelChoices(undefined)).toEqual([]);
  });

  it('never offers a row that is itself somebody else\'s model, nor a transcription row', () => {
    const out = peerModelChoices([
      ...ROWS,
      { alias: 'borrowed', model: 'x', type: 'remote_peer' },
      { alias: 'agent', type: 'dpc_agent' },
      { alias: 'whisper', model: 'openai/whisper-large-v3', type: 'local_whisper' },
    ]);
    expect(out.map((c) => c.alias)).toEqual(['qwen_vision', 'deepseek_flash']);
  });

  it('keeps a row of a type this build does not know, and one that names no model', () => {
    const out = peerModelChoices([{ alias: 'mystery', type: 'brand_new' }, { alias: 'bare' }]);
    expect(out.map((c) => c.alias)).toEqual(['mystery', 'bare']);
    expect(out[1].model).toBe('');
    expect(out[1].label).toBe('bare');
  });
});

describe('remotePeerModelState — the states the picker area is in', () => {
  const connected = peerChoices([{ node_id: ALICE, name: 'Alice', is_connected: true }]);
  const choices = peerModelChoices(ROWS);

  it('no peers connected says so', () => {
    const s = remotePeerModelState([], '', undefined, []);
    expect(s.kind).toBe('no-peers');
    expect(s.text).toBe(NO_PEERS_TEXT);
  });

  it('peers connected but none picked asks for a pick', () => {
    expect(remotePeerModelState(connected, '', undefined, []).kind).toBe('no-peer-picked');
  });

  it('a stored peer that is offline says so and does not wait on a fetch', () => {
    const gone = peerChoices([], ALICE);
    const s = remotePeerModelState(gone, ALICE, { loading: true, error: '' }, []);
    expect(s.kind).toBe('peer-offline');
    expect(s.text).toBe(PEER_OFFLINE_TEXT);
  });

  it('is loading while the question is out, and before the first answer', () => {
    expect(remotePeerModelState(connected, ALICE, { loading: true, error: '' }, []).kind).toBe('loading');
    expect(remotePeerModelState(connected, ALICE, undefined, []).kind).toBe('loading');
  });

  it('a failed answer carries the backend message', () => {
    const s = remotePeerModelState(connected, ALICE, { loading: false, error: 'Timeout waiting for providers' }, []);
    expect(s).toEqual({ kind: 'error', text: 'Timeout waiting for providers' });
  });

  it('a peer that answered with nothing is told apart from a failure', () => {
    const s = remotePeerModelState(connected, ALICE, { loading: false, error: '' }, []);
    expect(s.kind).toBe('empty');
    expect(s.text).toBe(PEER_OFFERS_NOTHING_TEXT);
  });

  it('a list is ready, and a refresh in flight keeps showing it as loading', () => {
    expect(remotePeerModelState(connected, ALICE, { loading: false, error: '' }, choices).kind).toBe('ready');
    expect(remotePeerModelState(connected, ALICE, { loading: true, error: '' }, choices).kind).toBe('loading');
  });
});

describe('what is saved', () => {
  it('peer_id from the peer, provider and model from the picked row', () => {
    const [qwen] = peerModelChoices(ROWS);
    expect(remotePeerFields(ALICE, qwen)).toEqual({
      peer_id: ALICE,
      provider: 'qwen_vision',
      model: 'qwen3.8-27b',
    });
  });

  it('leaves model out when the row names none', () => {
    const [bare] = peerModelChoices([{ alias: 'bare' }]);
    expect(remotePeerFields(ALICE, bare)).toEqual({ peer_id: ALICE, provider: 'bare' });
  });
});

describe('the draft rules for a remote_peer alias', () => {
  it('refuses until a peer is picked, then until one of its models is', () => {
    const base = { alias: 'x', type: 'remote_peer' };
    expect(validateProviderDraftForAdd(base, []).valid).toBe(false);
    expect(validateProviderDraftForAdd({ ...base, peer_id: ALICE }, []).valid).toBe(false);
    expect(validateProviderDraftForAdd({ ...base, peer_id: ALICE, provider: 'qwen_vision', model: 'qwen3.8-27b' }, []).valid).toBe(true);
  });

  it('does not accept a model typed without a peer', () => {
    expect(validateProviderDraftForAdd({ alias: 'x', type: 'remote_peer', model: 'm' }, []).valid).toBe(false);
  });

  it('the duplicate-alias check still applies', () => {
    const draft = { alias: 'qwen3.8-27b Remote Peer', type: 'remote_peer', peer_id: ALICE, provider: 'q', model: 'qwen3.8-27b' };
    const v = validateProviderDraftForAdd(draft, ['qwen3.8-27b remote peer']);
    expect(v.valid).toBe(false);
    expect(v.reason).toContain('already exists');
  });

  it('the alias is <model> <peer name>, and a taken one gets a number', () => {
    const peers = [{ node_id: ALICE, name: 'Alice', is_connected: true }];
    const label = peerDisplayName(peers, ALICE);
    expect(label).toBe('Alice');
    expect(suggestProviderAlias('qwen3.8-27b', 'remote_peer', [], label)).toBe('qwen3.8-27b Alice');
    expect(suggestProviderAlias('qwen3.8-27b', 'remote_peer', ['qwen3.8-27b Alice'], label)).toBe('qwen3.8-27b Alice 2');
  });

  it('a peer without a name falls back to the first 20 characters of its id', () => {
    const peers = [{ node_id: BOB, is_connected: true }];
    expect(peerDisplayName(peers, BOB)).toBe(BOB.slice(0, 20));
    expect(peerDisplayName([], BOB)).toBe(BOB.slice(0, 20));
    expect(peerDisplayName(peers, '')).toBe('');
    expect(suggestProviderAlias('m', 'remote_peer', [], peerDisplayName(peers, BOB))).toBe(`m ${BOB.slice(0, 20)}`);
  });

  it('without an override the type label is still used', () => {
    expect(suggestProviderAlias('qwen3.8-27b', 'remote_peer', [])).toBe('qwen3.8-27b Remote Peer');
    expect(suggestProviderAlias('m', 'ollama', [], '')).toBe('m Ollama');
  });
});

describe('a remote_peer alias is never offered onward', () => {
  it('stays in the never-offered set and classifies as never', () => {
    expect(NEVER_OFFERED_TYPES.has('remote_peer')).toBe(true);
    expect(classifyProviderType('remote_peer')).toBe('never');
  });

  it('is in neither Inference Sharing list', () => {
    const { local, vendor } = offeredProviders([
      { alias: 'borrowed', type: 'remote_peer' } as any,
      { alias: 'own', type: 'ollama' } as any,
    ]);
    expect([...local, ...vendor].map((p) => p.alias)).toEqual(['own']);
  });
});
