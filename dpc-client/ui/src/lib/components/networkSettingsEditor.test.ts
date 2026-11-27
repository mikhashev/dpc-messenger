import { describe, it, expect } from 'vitest';
import editor from './NetworkSettingsEditor.svelte?raw';
import core from '../coreService.ts?raw';
import sidebar from './Sidebar.svelte?raw';
import page from '../../routes/+page.svelte?raw';

// No mount harness: the form is checked in the source, the way the other settings-form
// guards do (providersEditorIdleSwap.test.ts). The card is
// THE-NETWORK-HUB-AND-GATEWAY-SETTINGS-HAVE-NO-CONTROL-IN-THE-UI.

describe('the Network and Gateway form groups its keys by when they apply', () => {
  it('has the two groups, each with its own save button', () => {
    expect(editor).toContain('<h3>Applies at once — the live keys</h3>');
    expect(editor).toContain('<h3>Applies after the next backend start</h3>');
    const saves = editor.match(/class="btn-save" on:click=\{save(Live|StartOnly)\}/g) ?? [];
    expect(saves.length).toBe(2);
  });

  it('carries the live keys as fields', () => {
    for (const id of ['nw-connection-timeout', 'nw-remote-inference-timeout',
      'nw-reconnect-ceiling', 'nw-dtls-timeout', 'nw-auto-connect-groups',
      'nw-turn-username', 'nw-turn-servers', 'nw-turn-fallback-servers',
      'nw-turn-fallback-username', 'nw-stun-servers']) {
      expect(editor).toContain(`id="${id}"`);
    }
  });

  it('carries the start-only keys as fields, and no api or gateway host field', () => {
    for (const id of ['nw-listen-host', 'nw-auto-connect-delay',
      'nw-dht-enabled', 'nw-dht-port', 'nw-dht-k', 'nw-dht-alpha',
      'nw-dht-bootstrap-timeout', 'nw-dht-lookup-timeout', 'nw-dht-bucket-refresh',
      'nw-dht-announce', 'nw-dht-seeds',
      'nw-relay-volunteer', 'nw-relay-max-peers', 'nw-relay-bandwidth',
      'nw-gossip-max-hops', 'nw-gossip-fanout', 'nw-gossip-ttl', 'nw-gossip-sync',
      'nw-hole-punch-timeout', 'nw-hello-timeout', 'nw-link-silence',
      'nw-max-pending-hellos', 'nw-max-proved-per-ip', 'nw-enable-hole-punching',
      'nw-udp-punch-port', 'nw-hub-url', 'nw-hub-auto-connect',
      'nw-oauth-callback-host', 'nw-oauth-callback-port', 'nw-oauth-provider',
      'nw-gateway-enabled', 'nw-gateway-port',
      'nw-ft-chunk-size', 'nw-ft-chunk-delay', 'nw-ft-background-mb',
      'nw-ft-direct-tls-mb', 'nw-ft-max-concurrent', 'nw-ft-verify-hash']) {
      expect(editor).toContain(`id="${id}"`);
    }
    // [api] host/port belong to the API-port card; gateway.host is refused by
    // the gateway itself (ADR-041 D1), so neither is a field here.
    expect(editor).not.toContain('id="nw-api-');
    expect(editor).not.toContain('id="nw-gateway-host"');
  });
});

describe('the P2P listen port is shown read-only', () => {
  it('displays the configured value in a disabled field with the config.ini note', () => {
    expect(editor).toContain('id="nw-listen-port"');
    expect(editor).toContain('value={draft.listenPort} disabled');
    const flat = editor.replace(/\s+/g, ' ');
    expect(flat).toContain('in config.ini before the backend starts');
    expect(flat).toContain('not changed from the UI');
  });

  it('sends no listen_port in any save payload', () => {
    expect(editor).not.toContain('listen_port:');
  });
});

describe('a stored TURN credential is never sent back to the form as text', () => {
  it('shows password fields that open empty, with dots and a keep rule when one is stored', () => {
    expect((editor.match(/type="password"/g) ?? []).length).toBe(2);
    expect(editor).toContain('placeholder={credentialPlaceholder(credSet)}');
    expect(editor).toContain('placeholder={credentialPlaceholder(fallbackCredSet)}');
    expect(editor).toMatch(/let cred = '';/);
    expect(editor).toMatch(/let fallbackCred = '';/);
    // The load path binds presence only: the credential fields are set to the
    // empty string, and no reply field is ever bound into them.
    expect(editor).toContain("cred = '';");
    expect(editor).toContain("fallbackCred = '';");
    expect(editor).not.toContain('cred = r.');
    expect(editor).not.toContain('fallbackCred: r.');
  });

  it('has an explicit Remove control per credential, and only that clears', () => {
    expect(editor).toContain('config: { turn: { clear_credential: true } }');
    expect(editor).toContain('config: { turn: { clear_fallback_credential: true } }');
    expect(editor).toContain('Stored TURN credential removed.');
    // A typed credential rides along only when non-empty — an empty field keeps it.
    expect(editor).toContain('if (credential) config.turn.credential = credential;');
    expect(editor).toContain('if (fallback) config.turn.fallback_credential = fallback;');
  });

  it('says the never-sent-back rule beside both credential fields', () => {
    expect((editor.match(/A stored credential is never sent back to this form/g) ?? []).length)
      .toBeGreaterThanOrEqual(2);
  });
});

describe('the form is honest about when each key takes effect', () => {
  // The hints wrap at whatever column the source breaks at, so the wording is
  // checked against the source with its whitespace collapsed to single spaces.
  const flat = editor.replace(/\s+/g, ' ');
  const count = (needle: string) => flat.split(needle).length - 1;

  it('the four live timeouts say the at-once wording the card fixed', () => {
    expect(count('applies at once / to the next use.')).toBe(4);
  });

  it('the auto-connect switch says the reconnect-check wording', () => {
    expect(flat).toContain('applies to the next reconnect check.');
  });

  it('the TURN keys say the next-connection rule and the environment override', () => {
    expect((flat.match(/applies to the next connection; connections already open keep their current ice configuration/gi) ?? []).length)
      .toBeGreaterThanOrEqual(3);
    expect(flat).toContain('DPC_TURN_USERNAME overrides this value');
    expect(flat).toContain('DPC_TURN_CREDENTIAL overrides this value');
    expect(flat).toContain('DPC_TURN_FALLBACK_USERNAME overrides this value');
    expect(flat).toContain('DPC_TURN_FALLBACK_CREDENTIAL overrides this value');
  });

  it('the STUN servers say both halves: next connection, and the discovery copy', () => {
    expect(flat).toContain('applies to the next connection. The STUN discovery task keeps its own ' +
      'copy for the life of the process, so the public-address discovery follows the new value ' +
      'only after a restart.');
  });

  it('every start-only field says the restart wording', () => {
    expect((flat.match(/applies after the next backend start/gi) ?? []).length)
      .toBeGreaterThanOrEqual(30);
  });

  it('the gateway switch says the panel-now-listener-later truth', () => {
    expect(flat).toContain('Inference Sharing panel reads this value at once while the ' +
      'gateway itself is built at start, so after a save the panel can show it enabled with ' +
      'no listener until the next backend start.');
  });

  it('the OAuth provider says the manual login hardcodes google', () => {
    expect(flat).toContain('the manual login hardcodes "google"');
  });
});

describe('each save sends only its group payload to the one command', () => {
  it('both saves and both removes go to save_network_config', () => {
    expect((editor.match(/sendCommand\('save_network_config'/g) ?? []).length).toBe(4);
    expect(editor).not.toContain("save_telegram_discord_config'");
  });

  it('the start-only payload carries the selects and text keys under their sections', () => {
    for (const key of ['config.p2p.listen_host', 'config.dht.seed_nodes',
      'url: hubUrl.replace', 'config.oauth.callback_host',
      'config.oauth.default_provider', 'config.file_transfer.chunk_size']) {
      expect(editor).toContain(key);
    }
  });

  it('a refused or failed save shows the backend reason beside its group', () => {
    expect((editor.match(/r\?\.message \|\| 'Could not (save|remove)/g) ?? []).length).toBe(4);
  });
});

describe('the form loads its keys and opens from the sidebar', () => {
  it('loads the get when it opens', () => {
    expect(editor).toContain('$: if (open) loadAll();');
    expect(editor).toContain("'get_network_config'");
  });

  it('coreService carries the two new command helpers', () => {
    for (const fn of ['getNetworkConfig', 'saveNetworkConfig']) {
      expect(core).toContain(`export async function ${fn}(`);
    }
  });

  it('the sidebar has the button and the page mounts the editor', () => {
    expect(sidebar).toContain('onOpenNetwork');
    expect(sidebar).toContain('Network and Gateway');
    expect(page).toContain('import NetworkSettingsEditor from "$lib/components/NetworkSettingsEditor.svelte"');
    expect(page).toContain('bind:open={showNetworkSettingsEditor}');
  });
});
