import { describe, it, expect } from 'vitest';
import src from './ProvidersEditor.svelte?raw';

// No mount harness: the remote_peer form is checked in the source, the way the other
// ProvidersEditor guards do. The logic the form calls is tested in
// utils/remotePeerProvider.test.ts.
const snippet = (name: string) => {
  const start = src.indexOf(`{#snippet ${name}(`);
  expect(start, `${name} snippet is in the markup`).toBeGreaterThan(0);
  return src.slice(start, src.indexOf('{/snippet}', start));
};

const fn = (name: string) => {
  const m = src.match(new RegExp(`(?:async )?function ${name}\\([^)]*\\) \\{([\\s\\S]*?)\\n  \\}\\n`));
  expect(m, `${name} is in the script`).not.toBeNull();
  return m![1];
};

describe('the remote_peer type is on offer', () => {
  it('is in the ProviderType union', () => {
    expect(src).toMatch(/type ProviderType = [^;]*'remote_peer'/);
  });

  it('is in the Type dropdown of the Add form and of an alias being edited', () => {
    const options = src.match(/<option value="remote_peer">\{REMOTE_PEER_TYPE_LABEL\}<\/option>/g) ?? [];
    expect(options).toHaveLength(2);
  });
});

describe('the pickers', () => {
  it('list peers from the status peer_info, not from a typed node id', () => {
    const s = snippet('remotePeerPickers');
    expect(s).toContain('peerChoices($nodeStatus?.peer_info, peerId)');
    expect(s).toContain('peerModelChoices($peerProviders.get(peerId))');
    expect(s).not.toContain('type="text"');
  });

  it('say what happens instead of a list: states and a refresh that asks the peer again', () => {
    const s = snippet('remotePeerPickers');
    expect(s).toContain('remotePeerModelState(peers, peerId, fetchState, choices)');
    expect(s).toContain('{picker.text}');
    expect(s).toContain('loadRemotePeerModels(peerId)');
  });

  it('keep a stored model that the peer no longer offers on the list instead of blanking it', () => {
    expect(snippet('remotePeerPickers')).toContain('NOT_AVAILABLE_NOW');
  });

  it('are rendered by the Add form and by the edit form of a remote_peer alias', () => {
    expect(src).toContain("{#if newProvider.type === 'remote_peer'}\n              {@render remotePeerPickers(");
    expect(src).toContain("{#if editedConfig.providers[i].type === 'remote_peer'}\n                      {@render remotePeerPickers(");
  });
});

describe('the peer is asked what it offers', () => {
  it('through query_remote_providers, writing the answer to the shared peerProviders store', () => {
    const body = fn('loadRemotePeerModels');
    expect(body).toContain("sendCommand('query_remote_providers', { peer_id: peerId })");
    expect(body).toContain('peerProviders.update');
    expect(body).toContain("result?.message || 'Failed to fetch providers'");
  });

  it('on a pick in either form, and once for each stored peer when an alias opens for editing', () => {
    expect(fn('pickNewRemotePeer')).toContain('loadRemotePeerModels(peerId)');
    expect(fn('pickEditedRemotePeer')).toContain('loadRemotePeerModels(peerId)');
    expect(src).toContain("p.type === 'remote_peer' && p.peer_id && !autoLoadedModelLists.has(key)");
  });

  it('a new peer clears the model picked from the old one', () => {
    for (const name of ['pickNewRemotePeer', 'pickEditedRemotePeer']) {
      const body = fn(name);
      expect(body).toContain('.provider = undefined');
    }
  });
});

describe('what Add writes', () => {
  const add = () => src.slice(src.indexOf("} else if (newProvider.type === 'remote_peer') {"), src.indexOf('// Carry over user-entered optional settings'));

  it('peer_id, provider (the alias on the peer) and model, and no host, key or timeout', () => {
    const b = add().split(/\r?\n/).filter((l) => !l.trim().startsWith('//')).join('\n');
    expect(b).toContain("provider.peer_id = newProvider.peer_id ?? ''");
    expect(b).toContain('provider.provider = newProvider.provider || undefined');
    expect(b).toContain('if (newProvider.model) provider.model = newProvider.model');
    expect(b).not.toContain('host');
    expect(b).not.toContain('api_key');
    expect(b).not.toContain('timeout');
  });

  it('the picked row is what fills the draft', () => {
    const body = fn('pickNewRemoteModel');
    expect(body).toContain('remotePeerFields(newProvider.peer_id');
    expect(body).toContain('newProvider.provider = f.provider');
    expect(body).toContain("newProvider.model = f.model ?? ''");
  });
});

describe('fields that mean nothing for a remote_peer alias are not drawn', () => {
  it('no temperature in the Add form or the edit form: the host\'s settings apply', () => {
    expect(src).toContain("newProvider.type !== 'local_whisper' && newProvider.type !== 'remote_peer'}\n              <div class=\"form-group\">\n                <label for=\"new-temperature\">");
    expect(src).toContain("{#if editedConfig.providers[i].type !== 'remote_peer'}\n                    <div class=\"form-group\">\n                      <label for=\"temperature-{i}\">");
  });

  it('the typed Model input of the other types is replaced, not shown beside the pickers', () => {
    expect(src).toContain("{:else if newProvider.type !== 'dpc_agent'}\n              <div class=\"form-group\">\n                <label for=\"new-model\">");
    expect(src).toContain("{:else if editedConfig.providers[i].type !== 'dpc_agent'}\n                      <div class=\"form-group\">\n                        <label for=\"model-{i}\">");
  });
});

describe('the display card', () => {
  it('names the peer and the alias on it', () => {
    expect(src).toContain("{#if provider.type === 'remote_peer'}");
    expect(src).toContain('<strong>Provider on the peer:</strong>');
  });
});
