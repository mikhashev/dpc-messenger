/**
 * The agent configuration dialog says where a chosen model's calls go.
 *
 * The backend names the route and the kind of each provider row from its type
 * and base_url; these tests hold the words the screen builds from them, and that
 * an agent pinned to another node outranks the row.
 */
import { describe, it, expect } from 'vitest';
import { compactionOffLine, computeHostNote, modelRoute, optionWithRoute, routeForAlias, UNKNOWN_ROUTE_LINE } from './agentModelRoute';

const PEER = 'dpc-node-' + 'b'.repeat(32);
const PEERS = [{ node_id: PEER, display_name: 'Bob', is_connected: true }];

describe('modelRoute', () => {
  it('says a self-hosted model on this node stays on this machine', () => {
    const r = modelRoute({ alias: 'qwen', type: 'llamacpp_server', route: 'local', provider_kind: 'self_hosted' }, '', PEERS);
    expect(r?.where).toBe('local');
    expect(r?.tag).toBe('local');
    expect(r?.line).toContain('runs on this machine');
  });

  it('names the vendor a cloud model sends the prompt to', () => {
    const r = modelRoute({ alias: 'c', type: 'anthropic', route: 'local', provider_kind: 'vendor' }, undefined, PEERS);
    expect(r?.where).toBe('vendor');
    expect(r?.tag).toBe('cloud');
    expect(r?.line).toBe('Cloud: vendor model, your prompt goes to Anthropic and is not kept on this machine.');
  });

  it('reads an OpenAI-compatible vendor by its kind, not its alias', () => {
    const r = modelRoute({ alias: 'my local box', type: 'openai_compatible', route: 'local', provider_kind: 'vendor' }, '', PEERS);
    expect(r?.where).toBe('vendor');
    expect(r?.line).toContain('an OpenAI-compatible API');
  });

  it("says another node's model sends the prompt to that node, by name", () => {
    const r = modelRoute({ alias: 'llama70', type: 'ollama', is_remote: true, peer_id: PEER, route: 'peer', provider_kind: 'self_hosted' }, '', PEERS);
    expect(r?.where).toBe('peer');
    expect(r?.tag).toBe('peer node');
    expect(r?.line).toBe("Peer node Bob: your prompt is sent to that node, not kept on this machine. It runs on that node's own hardware.");
  });

  it("says a peer's vendor model passes the prompt on to the vendor", () => {
    const r = modelRoute({ alias: 'nd', type: 'neuraldeep', is_remote: true, peer_id: PEER, route: 'peer', provider_kind: 'vendor' }, '', PEERS);
    expect(r?.line).toContain('It is a vendor model there, so the prompt goes on to NeuralDeep.');
  });

  it('does not invent a name for a local alias that points at an unnamed node', () => {
    const r = modelRoute({ alias: 'alice_llama', type: 'remote_peer', route: 'peer', provider_kind: 'unknown' }, '', PEERS);
    expect(r?.line.startsWith('Another node: your prompt is sent to that node')).toBe(true);
  });

  it('says a model on a private network address is on your network, not cloud', () => {
    for (const kind of ['vendor', 'self_hosted']) {
      const r = modelRoute({ alias: 'nas', type: 'openai_compatible', route: 'local', provider_kind: kind, host_scope: 'lan' }, '', PEERS);
      expect(r?.where).toBe('lan');
      expect(r?.tag).toBe('your network');
      expect(r?.line).toBe('Your network: runs on a machine in your local network; this node cannot see what that machine does with the prompt.');
    }
  });

  it('says local for a server on this machine even when its kind reads vendor', () => {
    const r = modelRoute({ alias: 'a', type: 'openai_compatible', route: 'local', provider_kind: 'vendor', host_scope: 'this_machine' }, '', PEERS);
    expect(r?.where).toBe('local');
    expect(r?.line).toContain('runs on this machine');
  });

  it('says unknown for a host scope unknown whatever the kind', () => {
    for (const kind of ['vendor', 'self_hosted']) {
      const r = modelRoute({ alias: 'nas', type: 'openai_compatible', route: 'local', provider_kind: kind, host_scope: 'unknown' }, '', PEERS);
      expect(r?.where).toBe('unknown');
      expect(r?.line).toBe(UNKNOWN_ROUTE_LINE);
    }
  });

  it('says unknown for a row whose kind is unknown', () => {
    const r = modelRoute({ alias: 'nas', type: 'openai_compatible', route: 'local', provider_kind: 'unknown' }, '', PEERS);
    expect(r?.where).toBe('unknown');
    expect(r?.line).toBe(UNKNOWN_ROUTE_LINE);
  });

  it('does not call a generic-type host a vendor when it names none', () => {
    const r = modelRoute({ alias: 'o', type: 'openai_compatible', route: 'local', provider_kind: 'vendor', base_url_host: 'ollama.mycompany.com' }, '', PEERS);
    expect(r?.where).toBe('unknown');
    expect(r?.tag).toBe('cloud?');
    expect(r?.line).toBe('Cloud?: your prompt is sent to ollama.mycompany.com; this node cannot tell whether a vendor or a machine of yours answers there.');
  });

  it('keeps a link-local or public address as the backend read them', () => {
    expect(modelRoute({ alias: 'b', type: 'openai_compatible', route: 'local', provider_kind: 'vendor' }, '', PEERS)?.where).toBe('vendor');
  });

  it('puts a lan row of an agent pinned to a node on that node', () => {
    const r = modelRoute({ alias: 'nas', type: 'openai_compatible', route: 'local', provider_kind: 'vendor', host_scope: 'lan' }, PEER, PEERS);
    expect(r?.where).toBe('peer');
  });

  it('does not guess when the backend sent no route', () => {
    const r = modelRoute({ alias: 'old', type: 'ollama' }, '', PEERS);
    expect(r?.where).toBe('unknown');
    expect(r?.tag).toBe('');
    expect(r?.line).toBe(UNKNOWN_ROUTE_LINE);
  });

  it('says unknown for a kind it cannot tell', () => {
    const r = modelRoute({ alias: 'x', type: 'something_new', route: 'local', provider_kind: 'unknown' }, '', PEERS);
    expect(r?.where).toBe('unknown');
  });

  it('says nothing about an alias no list carries on an agent that runs here', () => {
    expect(modelRoute(undefined, '', PEERS)).toBeNull();
    expect(modelRoute(null, 'local', PEERS)).toBeNull();
  });

  it('puts a local row of an agent pinned to a node on that node', () => {
    const r = modelRoute({ alias: 'qwen', type: 'llamacpp_server', route: 'local', provider_kind: 'self_hosted' }, PEER, PEERS);
    expect(r?.where).toBe('peer');
    expect(r?.line).toBe('Peer node Bob: your prompt is sent to that node, not kept on this machine.');
  });

  it('keeps the host kind for a row of the node the agent is pinned to', () => {
    const r = modelRoute({ alias: 'nd', type: 'neuraldeep', is_remote: true, peer_id: PEER, route: 'peer', provider_kind: 'vendor' }, PEER, PEERS);
    expect(r?.line).toContain('goes on to NeuralDeep');
  });

  it('still says peer for a held alias of a pinned agent that no list carries', () => {
    const r = modelRoute(undefined, PEER, []);
    expect(r?.where).toBe('peer');
    expect(r?.line).toContain('dpc-node-bbbbbbbbbbb...');
  });
});

describe('routeForAlias', () => {
  const rows = [{ alias: 'qwen', type: 'llamacpp_server', route: 'local', provider_kind: 'self_hosted' }];

  it('finds the row of the alias', () => {
    expect(routeForAlias('qwen', rows, '', PEERS)?.where).toBe('local');
  });

  it('says unknown for an alias no row carries (a deleted provider)', () => {
    const r = routeForAlias('gone', rows, '', PEERS);
    expect(r?.where).toBe('unknown');
    expect(r?.line).toBe(UNKNOWN_ROUTE_LINE);
  });

  it('says nothing when no alias is set', () => {
    expect(routeForAlias('', rows, '', PEERS)).toBeNull();
    expect(routeForAlias(undefined, rows, '', PEERS)).toBeNull();
  });
});

describe('compactionOffLine', () => {
  it('says compaction is off and where its summaries would go', () => {
    const route = modelRoute({ alias: 'c', type: 'anthropic', route: 'local', provider_kind: 'vendor' }, '', PEERS);
    expect(compactionOffLine(route)).toBe(
      'Compaction is off. If it is turned on, its summaries go to: Cloud: vendor model, your prompt goes to Anthropic and is not kept on this machine.'
    );
  });

  it('carries the unknown line for an unknown model', () => {
    expect(compactionOffLine(routeForAlias('gone', [], '', PEERS))).toBe(
      `Compaction is off. If it is turned on, its summaries go to: ${UNKNOWN_ROUTE_LINE}`
    );
  });

  it('is absent when no model is configured', () => {
    expect(compactionOffLine(null)).toBeNull();
  });
});

describe('optionWithRoute', () => {
  it('appends the tag, and nothing when the place is not known', () => {
    expect(optionWithRoute('a (m)', { where: 'vendor', tag: 'cloud', line: '' })).toBe('a (m) · cloud');
    expect(optionWithRoute('a (m)', { where: 'lan', tag: 'your network', line: '' })).toBe('a (m) · your network');
    expect(optionWithRoute('a (m)', { where: 'unknown', tag: '', line: '' })).toBe('a (m)');
    expect(optionWithRoute('a (m)', null)).toBe('a (m)');
  });
});

describe('computeHostNote', () => {
  it('is absent for an agent that runs here', () => {
    expect(computeHostNote('', PEERS)).toBeNull();
    expect(computeHostNote('local', PEERS)).toBeNull();
    expect(computeHostNote(undefined, PEERS)).toBeNull();
  });

  it('names the node every call goes to', () => {
    expect(computeHostNote(PEER, PEERS)).toBe(
      'This agent runs on Bob: every model call below goes to that node, whichever model is chosen, and its prompts are sent there.'
    );
  });
});
