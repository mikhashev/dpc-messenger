<!-- NetworkSettingsEditor.svelte -->
<!-- Edit the network, hub and gateway keys of config.ini, grouped by WHEN a value -->
<!-- applies (THE-NETWORK-HUB-AND-GATEWAY-SETTINGS-HAVE-NO-CONTROL-IN-THE-UI, -->
<!-- audit/config_ui_parity_2026-11-27.md). The first group holds the live keys — -->
<!-- read per dial, per request, per punch attempt, per reconnect check or per new -->
<!-- WebRTC peer (p2p_manager.py, gateway.py, service.py, udp_hole_punch.py, -->
<!-- webrtc_peer.py) — and every field there says "applies at once / to the next -->
<!-- use" or its own truer wording. The second group holds the start-only keys, -->
<!-- captured into the managers, the HubClient and the GatewayServer when the -->
<!-- backend starts, and every field there says "applies after the next backend -->
<!-- start". The P2P listen port is shown read-only: it is bound when the backend -->
<!-- starts and is set in config.ini before that, never from here. A stored TURN -->
<!-- credential is never sent back to this form: the field opens empty, empty keeps -->
<!-- the stored credential, and only the Remove control clears it. -->

<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { sendCommand } from '$lib/coreService';
  import {
    allowedValue, boolValue, credentialPlaceholder, LISTEN_HOSTS, OAUTH_PROVIDERS,
    parseFigure, parseFigureAbove, parseHostPortList, parseIceList, parsePort, parseWholeNumber,
  } from '$lib/utils/networkSettings';

  export let open: boolean = false;

  const dispatch = createEventDispatcher();

  // The number fields of each group: where each lands in the save payload, the
  // words a refusal says, and the client-side parse that runs first (a number
  // input binds a string until it holds a number, so every entry is text).
  type NumSpec = { id: string; section: string; key: string; words: string; parse: (t: string) => number | null };
  const secondsMoreThanZero = (t: string) => parseFigureAbove(t, 0);
  const zeroOrMore = (t: string) => parseFigure(t, 0);
  const oneOrMore = (t: string) => parseWholeNumber(t, 1);
  const aPort = (t: string) => parsePort(t, 1);

  const LIVE_NUMBERS: NumSpec[] = [
    { id: 'connectionTimeout', section: 'p2p', key: 'connection_timeout',
      words: 'P2P connection timeout must be a number of seconds, more than 0.', parse: secondsMoreThanZero },
    { id: 'remoteInferenceTimeout', section: 'connection', key: 'remote_inference_timeout',
      words: 'Remote inference timeout must be a number of seconds, more than 0.', parse: secondsMoreThanZero },
    { id: 'reconnectCeiling', section: 'connection', key: 'reconnect_slow_interval_max_seconds',
      words: 'The slow reconnect ceiling must be a number of seconds, more than 0.', parse: secondsMoreThanZero },
    { id: 'dtlsTimeout', section: 'hole_punch', key: 'dtls_handshake_timeout',
      words: 'DTLS handshake timeout must be a number of seconds, more than 0.', parse: secondsMoreThanZero },
  ];
  const START_NUMBERS: NumSpec[] = [
    { id: 'autoConnectDelay', section: 'p2p', key: 'auto_connect_delay',
      words: 'Auto-connect delay must be a number of seconds, 0 or more (0 = connect at once).', parse: zeroOrMore },
    { id: 'dhtPort', section: 'dht', key: 'port',
      words: 'DHT port must be a whole number from 1 to 65535.', parse: aPort },
    { id: 'dhtK', section: 'dht', key: 'k',
      words: 'DHT k must be a whole number of 1 or more.', parse: oneOrMore },
    { id: 'dhtAlpha', section: 'dht', key: 'alpha',
      words: 'DHT alpha must be a whole number of 1 or more.', parse: oneOrMore },
    { id: 'dhtBootstrapTimeout', section: 'dht', key: 'bootstrap_timeout',
      words: 'DHT bootstrap timeout must be a number of seconds, more than 0.', parse: secondsMoreThanZero },
    { id: 'dhtLookupTimeout', section: 'dht', key: 'lookup_timeout',
      words: 'DHT lookup timeout must be a number of seconds, more than 0.', parse: secondsMoreThanZero },
    { id: 'dhtBucketRefresh', section: 'dht', key: 'bucket_refresh_interval',
      words: 'DHT bucket refresh must be a number of seconds, more than 0.', parse: secondsMoreThanZero },
    { id: 'dhtAnnounce', section: 'dht', key: 'announce_interval',
      words: 'DHT announce interval must be a number of seconds, more than 0.', parse: secondsMoreThanZero },
    { id: 'relayMaxPeers', section: 'relay', key: 'max_peers',
      words: 'Relay max peers must be a whole number of 1 or more.', parse: oneOrMore },
    { id: 'relayBandwidth', section: 'relay', key: 'bandwidth_limit_mbps',
      words: 'Relay bandwidth limit must be a number of Mbps, 0 or more.', parse: zeroOrMore },
    { id: 'gossipMaxHops', section: 'gossip', key: 'max_hops',
      words: 'Gossip max hops must be a whole number of 1 or more.', parse: oneOrMore },
    { id: 'gossipFanout', section: 'gossip', key: 'fanout',
      words: 'Gossip fanout must be a whole number of 1 or more.', parse: oneOrMore },
    { id: 'gossipTtl', section: 'gossip', key: 'ttl_seconds',
      words: 'Gossip TTL must be a whole number of seconds, 1 or more.', parse: oneOrMore },
    { id: 'gossipSync', section: 'gossip', key: 'sync_interval',
      words: 'Gossip sync interval must be a whole number of seconds, 1 or more.', parse: oneOrMore },
    { id: 'holePunchTimeout', section: 'connection', key: 'hole_punch_timeout',
      words: 'Hole punch timeout must be a number of seconds, more than 0.', parse: secondsMoreThanZero },
    { id: 'helloTimeout', section: 'connection', key: 'hello_timeout',
      words: 'HELLO timeout must be a number of seconds, more than 0.', parse: secondsMoreThanZero },
    { id: 'linkSilence', section: 'connection', key: 'link_silence_timeout',
      words: 'Link silence timeout must be a number of seconds, more than 0.', parse: secondsMoreThanZero },
    { id: 'maxPendingHellos', section: 'connection', key: 'max_pending_hellos_per_ip',
      words: 'Pending HELLOs per address must be a whole number of 1 or more.', parse: oneOrMore },
    { id: 'maxProvedPerIp', section: 'connection', key: 'max_proved_connections_per_ip',
      words: 'Proved connections per address must be a whole number of 1 or more.', parse: oneOrMore },
    { id: 'udpPunchPort', section: 'hole_punch', key: 'udp_punch_port',
      words: 'UDP punch port must be a whole number from 1 to 65535.', parse: aPort },
    { id: 'oauthCallbackPort', section: 'oauth', key: 'callback_port',
      words: 'OAuth callback port must be a whole number from 1 to 65535.', parse: aPort },
    { id: 'gatewayPort', section: 'gateway', key: 'port',
      words: 'Gateway port must be a whole number from 1 to 65535.', parse: aPort },
    { id: 'ftChunkDelay', section: 'file_transfer', key: 'chunk_delay',
      words: 'Chunk delay must be a number of seconds, 0 or more (0 = none).', parse: zeroOrMore },
    { id: 'ftMaxConcurrent', section: 'file_transfer', key: 'max_concurrent_transfers',
      words: 'Max concurrent transfers must be a whole number of 1 or more.', parse: oneOrMore },
  ];

  // The drafts hold numbers and lists as text the save parses; the two TURN
  // credential fields are always empty on load — a stored credential is never
  // sent to the form, only credential_set, which the placeholder words.
  let draft: Record<string, string> = {};
  let flags: Record<string, boolean> = {};
  let cred = '';
  let credSet = false;
  let fallbackCred = '';
  let fallbackCredSet = false;

  let loading = false;
  let loadError = '';
  let liveBusy = false, startBusy = false;
  let liveError = '', startError = '';
  let liveNotice = '', startNotice = '';

  // Loads every section each time the editor opens (the only dependency named
  // is `open`, so the statement runs exactly when it flips).
  $: if (open) loadAll();

  function field(sections: any, section: string, key: string, fallback: string): string {
    const value = sections?.[section]?.[key];
    return value === undefined || value === null ? fallback : String(value);
  }

  async function loadAll() {
    loading = true;
    loadError = '';
    try {
      const r = await sendCommand('get_network_config', {});
      if (r?.status === 'success') {
        draft = {
          connectionTimeout: field(r, 'p2p', 'connection_timeout', '30'),
          remoteInferenceTimeout: field(r, 'connection', 'remote_inference_timeout', '1200'),
          reconnectCeiling: field(r, 'connection', 'reconnect_slow_interval_max_seconds', '900'),
          dtlsTimeout: field(r, 'hole_punch', 'dtls_handshake_timeout', '3'),
          listenPort: field(r, 'p2p', 'listen_port', '8888'),
          listenHost: allowedValue(r?.p2p?.listen_host, LISTEN_HOSTS, 'dual'),
          autoConnectDelay: field(r, 'p2p', 'auto_connect_delay', '5'),
          turnUsername: field(r, 'turn', 'username', ''),
          turnServers: field(r, 'turn', 'servers', ''),
          turnFallbackServers: field(r, 'turn', 'fallback_servers', ''),
          turnFallbackUsername: field(r, 'turn', 'fallback_username', ''),
          stunServers: field(r, 'webrtc', 'stun_servers', ''),
          dhtPort: field(r, 'dht', 'port', '8889'),
          dhtK: field(r, 'dht', 'k', '20'),
          dhtAlpha: field(r, 'dht', 'alpha', '3'),
          dhtBootstrapTimeout: field(r, 'dht', 'bootstrap_timeout', '30'),
          dhtLookupTimeout: field(r, 'dht', 'lookup_timeout', '10'),
          dhtBucketRefresh: field(r, 'dht', 'bucket_refresh_interval', '3600'),
          dhtAnnounce: field(r, 'dht', 'announce_interval', '3600'),
          dhtSeeds: field(r, 'dht', 'seed_nodes', ''),
          relayMaxPeers: field(r, 'relay', 'max_peers', '10'),
          relayBandwidth: field(r, 'relay', 'bandwidth_limit_mbps', '10'),
          gossipMaxHops: field(r, 'gossip', 'max_hops', '5'),
          gossipFanout: field(r, 'gossip', 'fanout', '3'),
          gossipTtl: field(r, 'gossip', 'ttl_seconds', '86400'),
          gossipSync: field(r, 'gossip', 'sync_interval', '300'),
          holePunchTimeout: field(r, 'connection', 'hole_punch_timeout', '15'),
          helloTimeout: field(r, 'connection', 'hello_timeout', '10'),
          linkSilence: field(r, 'connection', 'link_silence_timeout', '90'),
          maxPendingHellos: field(r, 'connection', 'max_pending_hellos_per_ip', '8'),
          maxProvedPerIp: field(r, 'connection', 'max_proved_connections_per_ip', '8'),
          udpPunchPort: field(r, 'hole_punch', 'udp_punch_port', '8890'),
          hubUrl: field(r, 'hub', 'url', 'http://localhost:8000'),
          oauthCallbackHost: field(r, 'oauth', 'callback_host', '127.0.0.1'),
          oauthCallbackPort: field(r, 'oauth', 'callback_port', '8080'),
          oauthProvider: allowedValue(r?.oauth?.default_provider, OAUTH_PROVIDERS, 'google'),
          gatewayPort: field(r, 'gateway', 'port', '9997'),
          ftChunkSize: field(r, 'file_transfer', 'chunk_size', 'auto'),
          ftChunkDelay: field(r, 'file_transfer', 'chunk_delay', '0'),
          ftMaxConcurrent: field(r, 'file_transfer', 'max_concurrent_transfers', '3'),
        };
        flags = {
          autoConnectGroups: boolValue(r?.p2p?.auto_connect_node_groups, true),
          dhtEnabled: boolValue(r?.dht?.enabled, true),
          relayVolunteer: boolValue(r?.relay?.volunteer, false),
          enableHolePunching: boolValue(r?.connection?.enable_hole_punching, false),
          hubAutoConnect: boolValue(r?.hub?.auto_connect, false),
          gatewayEnabled: boolValue(r?.gateway?.enabled, false),
          ftVerifyHash: boolValue(r?.file_transfer?.verify_hash, true),
        };
        cred = '';
        fallbackCred = '';
        credSet = boolValue(r?.turn?.credential_set, false);
        fallbackCredSet = boolValue(r?.turn?.fallback_credential_set, false);
      } else if (r?.status === 'error') {
        loadError = r.message || 'Could not read the network settings';
      }
      liveNotice = startNotice = '';
      liveError = startError = '';
    } catch (error) {
      loadError = `Error: ${error}`;
    } finally {
      loading = false;
    }
  }

  function close() {
    dispatch('close');
  }

  function handleKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape' && !liveBusy && !startBusy) close();
  }

  // Each save sends only its group, as one payload of config.ini sections. A
  // typed credential goes along only when non-empty — an empty field keeps the
  // stored credential — and the Remove control is the one explicit clear.
  async function saveLive() {
    liveBusy = true;
    liveError = '';
    try {
      const config: Record<string, Record<string, unknown>> = {};
      for (const f of LIVE_NUMBERS) {
        const n = f.parse(draft[f.id] ?? '');
        if (n === null) {
          liveError = f.words;
          return;
        }
        (config[f.section] ??= {})[f.key] = n;
      }
      const stun = parseIceList(draft.stunServers);
      if (stun === null) {
        liveError = 'STUN servers must be a comma-separated list of stun: addresses.';
        return;
      }
      const servers = parseIceList(draft.turnServers);
      if (servers === null) {
        liveError = 'TURN servers must be a comma-separated list of turn: addresses.';
        return;
      }
      const fallbackServers = parseIceList(draft.turnFallbackServers);
      if (fallbackServers === null) {
        liveError = 'Fallback TURN servers must be a comma-separated list of turn: addresses.';
        return;
      }
      config.p2p.auto_connect_node_groups = flags.autoConnectGroups;
      config.turn = {
        username: draft.turnUsername.trim(),
        servers: servers.join(', '),
        fallback_servers: fallbackServers.join(', '),
        fallback_username: draft.turnFallbackUsername.trim(),
      };
      const credential = cred.trim();
      if (credential) config.turn.credential = credential;
      const fallback = fallbackCred.trim();
      if (fallback) config.turn.fallback_credential = fallback;
      config.webrtc = { stun_servers: stun.join(', ') };
      const r = await sendCommand('save_network_config', { config });
      if (r?.status === 'success') {
        cred = '';
        fallbackCred = '';
        if (credential) credSet = true;
        if (fallback) fallbackCredSet = true;
        liveNotice = 'Saved.';
      } else {
        liveError = r?.message || 'Could not save the live network settings';
      }
    } catch (error) {
      liveError = `Error: ${error}`;
    } finally {
      liveBusy = false;
    }
  }

  async function removeCredential() {
    liveBusy = true;
    liveError = '';
    try {
      const r = await sendCommand('save_network_config', {
        config: { turn: { clear_credential: true } },
      });
      if (r?.status === 'success') {
        cred = '';
        credSet = false;
        liveNotice = 'Stored TURN credential removed.';
      } else {
        liveError = r?.message || 'Could not remove the stored TURN credential';
      }
    } catch (error) {
      liveError = `Error: ${error}`;
    } finally {
      liveBusy = false;
    }
  }

  async function removeFallbackCredential() {
    liveBusy = true;
    liveError = '';
    try {
      const r = await sendCommand('save_network_config', {
        config: { turn: { clear_fallback_credential: true } },
      });
      if (r?.status === 'success') {
        fallbackCred = '';
        fallbackCredSet = false;
        liveNotice = 'Stored fallback TURN credential removed.';
      } else {
        liveError = r?.message || 'Could not remove the stored fallback TURN credential';
      }
    } catch (error) {
      liveError = `Error: ${error}`;
    } finally {
      liveBusy = false;
    }
  }

  async function saveStartOnly() {
    startBusy = true;
    startError = '';
    try {
      const config: Record<string, Record<string, unknown>> = {};
      for (const f of START_NUMBERS) {
        const n = f.parse(draft[f.id] ?? '');
        if (n === null) {
          startError = f.words;
          return;
        }
        (config[f.section] ??= {})[f.key] = n;
      }
      const seeds = parseHostPortList(draft.dhtSeeds);
      if (seeds === null) {
        startError = 'DHT seed nodes must be a comma-separated list of host:port entries.';
        return;
      }
      const hubUrl = draft.hubUrl.trim();
      let hubParsed: URL | null = null;
      try {
        hubParsed = new URL(hubUrl);
      } catch {
        hubParsed = null;
      }
      if (!hubParsed || (hubParsed.protocol !== 'http:' && hubParsed.protocol !== 'https:')) {
        startError = 'Hub URL must be an http:// or https:// address.';
        return;
      }
      const chunkSize = draft.ftChunkSize.trim();
      if (chunkSize !== 'auto' && !/^\d+\s*(kib|mib|k|m)?$/i.test(chunkSize)) {
        startError = 'Chunk size must be auto or a size in bytes (64KiB, 1MiB, 1048576).';
        return;
      }
      const callbackHost = draft.oauthCallbackHost.trim();
      if (!callbackHost || callbackHost.split(/\s+/).length > 1) {
        startError = 'OAuth callback host must be a host name or address without spaces.';
        return;
      }
      config.p2p.listen_host = draft.listenHost;
      config.dht.enabled = flags.dhtEnabled;
      config.dht.seed_nodes = seeds.join(', ');
      config.relay.volunteer = flags.relayVolunteer;
      config.connection.enable_hole_punching = flags.enableHolePunching;
      config.hub = { url: hubUrl.replace(/\/+$/, ''), auto_connect: flags.hubAutoConnect };
      config.oauth.callback_host = callbackHost;
      config.oauth.default_provider = draft.oauthProvider;
      config.gateway.enabled = flags.gatewayEnabled;
      config.file_transfer.chunk_size = chunkSize;
      config.file_transfer.verify_hash = flags.ftVerifyHash;
      const r = await sendCommand('save_network_config', { config });
      if (r?.status === 'success') {
        startNotice = 'Saved.';
      } else {
        startError = r?.message || 'Could not save the start-only network settings';
      }
    } catch (error) {
      startError = `Error: ${error}`;
    } finally {
      startBusy = false;
    }
  }
</script>

{#if open}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="modal-overlay" on:keydown={handleKeydown} role="presentation">
    <div class="modal" role="dialog" aria-labelledby="network-title" tabindex="-1">
      <div class="modal-header">
        <h2 id="network-title">Network and Gateway</h2>
        <button class="close-btn" on:click={close}>&times;</button>
      </div>

      <div class="modal-body">
        {#if loadError}
          <p class="section-error">{loadError}</p>
        {/if}

        <section class="settings-section">
          <h3>Applies at once — the live keys</h3>
          <p class="section-hint">
            These are re-read as they are used — per dial, per request, per punch
            attempt, per reconnect check, per new WebRTC peer — so a save takes
            effect without a restart.
          </p>
          {#if liveError}
            <p class="section-error">{liveError}</p>
          {:else if liveNotice}
            <p class="section-saved">{liveNotice}</p>
          {/if}
          <div class="form-row">
            <label for="nw-connection-timeout">P2P connection timeout (seconds)</label>
            <input id="nw-connection-timeout" type="number" min="0.1" step="any"
              bind:value={draft.connectionTimeout} disabled={liveBusy || loading} />
            <span class="field-hint">
              Each direct dial and each WebRTC finalize/timeout watch
              (p2p_manager.py); applies at once / to the next use.
            </span>
          </div>
          <div class="form-row">
            <label for="nw-remote-inference-timeout">Remote inference timeout (seconds)</label>
            <input id="nw-remote-inference-timeout" type="number" min="0.1" step="any"
              bind:value={draft.remoteInferenceTimeout} disabled={liveBusy || loading} />
            <span class="field-hint">
              Per gateway request, per peer send, per slot-wait (gateway.py,
              service.py, llama_server_supervisor.py); applies at once / to the
              next use.
            </span>
          </div>
          <div class="form-row">
            <label for="nw-reconnect-ceiling">Slow reconnect ceiling (seconds)</label>
            <input id="nw-reconnect-ceiling" type="number" min="0.1" step="any"
              bind:value={draft.reconnectCeiling} disabled={liveBusy || loading} />
            <span class="field-hint">
              Re-read each time a peer's slow reconnect starts (service.py);
              applies at once / to the next use.
            </span>
          </div>
          <div class="form-row">
            <label for="nw-dtls-timeout">DTLS handshake timeout (seconds)</label>
            <input id="nw-dtls-timeout" type="number" min="0.1" step="any"
              bind:value={draft.dtlsTimeout} disabled={liveBusy || loading} />
            <span class="field-hint">
              Per punch attempt (udp_hole_punch.py); applies at once / to the
              next use.
            </span>
          </div>
          <div class="form-group">
            <label for="nw-auto-connect-groups">
              <input id="nw-auto-connect-groups" type="checkbox" bind:checked={flags.autoConnectGroups}
                disabled={liveBusy || loading} />
              Auto-connect to firewall node-group members
            </label>
            <span class="field-hint wide">
              Read at start and at each per-peer reconnect give-up check;
              applies to the next reconnect check.
            </span>
          </div>

          <h4 class="sub-group">TURN relay (WebRTC)</h4>
          <p class="section-hint">
            All six TURN keys are read per new WebRTC peer through a fresh
            Settings parse (webrtc_peer.py), so each applies to the next
            connection; connections already open keep their current ICE
            configuration.
          </p>
          <div class="form-row">
            <label for="nw-turn-username">TURN username</label>
            <input id="nw-turn-username" type="text" bind:value={draft.turnUsername}
              disabled={liveBusy || loading} />
            <span class="field-hint">
              Applies to the next connection; connections already open keep
              their current ICE configuration. An environment variable
              DPC_TURN_USERNAME overrides this value.
            </span>
          </div>
          <div class="form-row">
            <label for="nw-turn-credential">TURN credential</label>
            <div class="token-row">
              <input id="nw-turn-credential" type="password" autocomplete="off" bind:value={cred}
                disabled={liveBusy || loading} placeholder={credentialPlaceholder(credSet)} />
              <button class="btn-remove" on:click={removeCredential}
                disabled={liveBusy || loading || !credSet} title="Clear the stored credential">
                Remove
              </button>
            </div>
            <span class="field-hint">
              A stored credential is never sent back to this form: the field
              opens empty, leaving it empty keeps the stored one, and only
              Remove clears it. Applies to the next connection. An environment
              variable DPC_TURN_CREDENTIAL overrides this value.
            </span>
          </div>
          <div class="form-row">
            <label for="nw-turn-servers">TURN servers</label>
            <input id="nw-turn-servers" type="text" bind:value={draft.turnServers}
              disabled={liveBusy || loading} placeholder="turn:global.relay.example.com:80, ..." />
            <span class="field-hint">
              Comma-separated stun:/turn:/turns: addresses, used only when the
              username and credential are set. Applies to the next connection;
              connections already open keep their current ICE configuration.
            </span>
          </div>
          <div class="form-row">
            <label for="nw-turn-fallback-servers">Fallback TURN servers</label>
            <input id="nw-turn-fallback-servers" type="text" bind:value={draft.turnFallbackServers}
              disabled={liveBusy || loading} placeholder="turn:openrelay.example.com:80, ..." />
            <span class="field-hint">
              A public relay, if you accept that your traffic passes through
              it; used only when no TURN username/credential is set. Applies to
              the next connection.
            </span>
          </div>
          <div class="form-row">
            <label for="nw-turn-fallback-username">Fallback TURN username</label>
            <input id="nw-turn-fallback-username" type="text" bind:value={draft.turnFallbackUsername}
              disabled={liveBusy || loading} />
            <span class="field-hint">
              Applies to the next connection. An environment variable
              DPC_TURN_FALLBACK_USERNAME overrides this value.
            </span>
          </div>
          <div class="form-row">
            <label for="nw-turn-fallback-credential">Fallback TURN credential</label>
            <div class="token-row">
              <input id="nw-turn-fallback-credential" type="password" autocomplete="off" bind:value={fallbackCred}
                disabled={liveBusy || loading} placeholder={credentialPlaceholder(fallbackCredSet)} />
              <button class="btn-remove" on:click={removeFallbackCredential}
                disabled={liveBusy || loading || !fallbackCredSet} title="Clear the stored credential">
                Remove
              </button>
            </div>
            <span class="field-hint">
              A stored credential is never sent back to this form: the field
              opens empty, leaving it empty keeps the stored one, and only
              Remove clears it. Applies to the next connection. An environment
              variable DPC_TURN_FALLBACK_CREDENTIAL overrides this value.
            </span>
          </div>

          <h4 class="sub-group">WebRTC</h4>
          <div class="form-row">
            <label for="nw-stun-servers">STUN servers</label>
            <input id="nw-stun-servers" type="text" bind:value={draft.stunServers}
              disabled={liveBusy || loading} placeholder="stun:stun.l.google.com:19302, ..." />
            <span class="field-hint">
              Read per WebRTC peer (webrtc_peer.py); applies to the next
              connection. The STUN discovery task keeps its own copy for the
              life of the process, so the public-address discovery follows the
              new value only after a restart.
            </span>
          </div>
          <button class="btn-save" on:click={saveLive} disabled={liveBusy || loading}>
            {liveBusy ? 'Saving...' : 'Save live settings'}
          </button>
        </section>

        <section class="settings-section">
          <h3>Applies after the next backend start</h3>
          <p class="section-hint">
            Each of these is captured once while the backend starts — into the
            DHT, relay, gossip, hole-punch and file-transfer managers, the P2P
            listener, the Hub client and the gateway server — so a save lands
            in config.ini and applies after the next backend start.
          </p>
          {#if startError}
            <p class="section-error">{startError}</p>
          {:else if startNotice}
            <p class="section-saved">{startNotice}</p>
          {/if}

          <h4 class="sub-group">P2P listener</h4>
          <div class="form-row">
            <label for="nw-listen-port">P2P listen port</label>
            <input id="nw-listen-port" type="text" value={draft.listenPort} disabled
              title="Set in config.ini before the backend starts" />
            <span class="field-hint">
              Read-only: this port is bound when the backend starts and is not
              changed from the UI. To change it, edit p2p.listen_port in
              config.ini before the backend starts.
            </span>
          </div>
          <div class="form-row">
            <label for="nw-listen-host">P2P listen host</label>
            <select id="nw-listen-host" bind:value={draft.listenHost} disabled={startBusy || loading}>
              {#each LISTEN_HOSTS as host (host)}
                <option value={host}>{host}</option>
              {/each}
            </select>
            <span class="field-hint">
              dual = IPv4 and IPv6, 0.0.0.0 = IPv4 only, :: = IPv6 only
              (p2p_manager.py start_server); applies after the next backend
              start.
            </span>
          </div>
          <div class="form-row">
            <label for="nw-auto-connect-delay">Auto-connect delay (seconds)</label>
            <input id="nw-auto-connect-delay" type="number" min="0" step="any"
              bind:value={draft.autoConnectDelay} disabled={startBusy || loading} />
            <span class="field-hint">
              How long startup waits before auto-connecting to node-group
              peers, so the DHT can bootstrap; 0 = connect at once. Read once
              in CoreService.start; applies after the next backend start.
            </span>
          </div>

          <h4 class="sub-group">DHT peer discovery</h4>
          <div class="form-group">
            <label for="nw-dht-enabled">
              <input id="nw-dht-enabled" type="checkbox" bind:checked={flags.dhtEnabled}
                disabled={startBusy || loading} />
              Enable DHT peer discovery
            </label>
            <span class="field-hint wide">Applies after the next backend start.</span>
          </div>
          <div class="form-row">
            <label for="nw-dht-port">DHT port</label>
            <input id="nw-dht-port" type="number" min="1" max="65535" step="1"
              bind:value={draft.dhtPort} disabled={startBusy || loading} />
            <span class="field-hint">UDP port for DHT RPCs. Applies after the next backend start.</span>
          </div>
          <div class="form-row">
            <label for="nw-dht-k">DHT k (bucket size)</label>
            <input id="nw-dht-k" type="number" min="1" step="1"
              bind:value={draft.dhtK} disabled={startBusy || loading} />
            <span class="field-hint">Kademlia k parameter (nodes per bucket). Applies after the next backend start.</span>
          </div>
          <div class="form-row">
            <label for="nw-dht-alpha">DHT alpha (parallelism)</label>
            <input id="nw-dht-alpha" type="number" min="1" step="1"
              bind:value={draft.dhtAlpha} disabled={startBusy || loading} />
            <span class="field-hint">Parallelism factor for iterative lookups. Applies after the next backend start.</span>
          </div>
          <div class="form-row">
            <label for="nw-dht-bootstrap-timeout">Bootstrap timeout (seconds)</label>
            <input id="nw-dht-bootstrap-timeout" type="number" min="0.1" step="any"
              bind:value={draft.dhtBootstrapTimeout} disabled={startBusy || loading} />
            <span class="field-hint">Applies after the next backend start.</span>
          </div>
          <div class="form-row">
            <label for="nw-dht-lookup-timeout">Lookup timeout (seconds)</label>
            <input id="nw-dht-lookup-timeout" type="number" min="0.1" step="any"
              bind:value={draft.dhtLookupTimeout} disabled={startBusy || loading} />
            <span class="field-hint">Applies after the next backend start.</span>
          </div>
          <div class="form-row">
            <label for="nw-dht-bucket-refresh">Bucket refresh (seconds)</label>
            <input id="nw-dht-bucket-refresh" type="number" min="0.1" step="any"
              bind:value={draft.dhtBucketRefresh} disabled={startBusy || loading} />
            <span class="field-hint">Applies after the next backend start.</span>
          </div>
          <div class="form-row">
            <label for="nw-dht-announce">Announce interval (seconds)</label>
            <input id="nw-dht-announce" type="number" min="0.1" step="any"
              bind:value={draft.dhtAnnounce} disabled={startBusy || loading} />
            <span class="field-hint">Applies after the next backend start.</span>
          </div>
          <div class="form-row">
            <label for="nw-dht-seeds">Seed nodes</label>
            <input id="nw-dht-seeds" type="text" bind:value={draft.dhtSeeds}
              disabled={startBusy || loading} placeholder="203.0.113.7:8889, seed.example.org:8889" />
            <span class="field-hint">
              Comma-separated host:port entries to bootstrap from. Applies
              after the next backend start.
            </span>
          </div>

          <h4 class="sub-group">Relay</h4>
          <div class="form-group">
            <label for="nw-relay-volunteer">
              <input id="nw-relay-volunteer" type="checkbox" bind:checked={flags.relayVolunteer}
                disabled={startBusy || loading} />
              Volunteer this node as a relay
            </label>
            <span class="field-hint wide">Applies after the next backend start.</span>
          </div>
          <div class="form-row">
            <label for="nw-relay-max-peers">Max relay peers</label>
            <input id="nw-relay-max-peers" type="number" min="1" step="1"
              bind:value={draft.relayMaxPeers} disabled={startBusy || loading} />
            <span class="field-hint">Max concurrent relay sessions in server mode. Applies after the next backend start.</span>
          </div>
          <div class="form-row">
            <label for="nw-relay-bandwidth">Bandwidth limit (Mbps)</label>
            <input id="nw-relay-bandwidth" type="number" min="0" step="any"
              bind:value={draft.relayBandwidth} disabled={startBusy || loading} />
            <span class="field-hint">Applies after the next backend start.</span>
          </div>

          <h4 class="sub-group">Gossip</h4>
          <div class="form-row">
            <label for="nw-gossip-max-hops">Max hops</label>
            <input id="nw-gossip-max-hops" type="number" min="1" step="1"
              bind:value={draft.gossipMaxHops} disabled={startBusy || loading} />
            <span class="field-hint">Applies after the next backend start.</span>
          </div>
          <div class="form-row">
            <label for="nw-gossip-fanout">Fanout</label>
            <input id="nw-gossip-fanout" type="number" min="1" step="1"
              bind:value={draft.gossipFanout} disabled={startBusy || loading} />
            <span class="field-hint">Random peers each gossip message is forwarded to. Applies after the next backend start.</span>
          </div>
          <div class="form-row">
            <label for="nw-gossip-ttl">TTL (seconds)</label>
            <input id="nw-gossip-ttl" type="number" min="1" step="1"
              bind:value={draft.gossipTtl} disabled={startBusy || loading} />
            <span class="field-hint">How long a gossip message lives. Applies after the next backend start.</span>
          </div>
          <div class="form-row">
            <label for="nw-gossip-sync">Sync interval (seconds)</label>
            <input id="nw-gossip-sync" type="number" min="1" step="1"
              bind:value={draft.gossipSync} disabled={startBusy || loading} />
            <span class="field-hint">Anti-entropy sync interval. Applies after the next backend start.</span>
          </div>

          <h4 class="sub-group">Connection strategies</h4>
          <div class="form-row">
            <label for="nw-hole-punch-timeout">Hole punch timeout (seconds)</label>
            <input id="nw-hole-punch-timeout" type="number" min="0.1" step="any"
              bind:value={draft.holePunchTimeout} disabled={startBusy || loading} />
            <span class="field-hint">Applies after the next backend start.</span>
          </div>
          <div class="form-row">
            <label for="nw-hello-timeout">HELLO timeout (seconds)</label>
            <input id="nw-hello-timeout" type="number" min="0.1" step="any"
              bind:value={draft.helloTimeout} disabled={startBusy || loading} />
            <span class="field-hint">Applies after the next backend start.</span>
          </div>
          <div class="form-row">
            <label for="nw-link-silence">Link silence timeout (seconds)</label>
            <input id="nw-link-silence" type="number" min="0.1" step="any"
              bind:value={draft.linkSilence} disabled={startBusy || loading} />
            <span class="field-hint">Applies after the next backend start.</span>
          </div>
          <div class="form-row">
            <label for="nw-max-pending-hellos">Pending HELLOs per address</label>
            <input id="nw-max-pending-hellos" type="number" min="1" step="1"
              bind:value={draft.maxPendingHellos} disabled={startBusy || loading} />
            <span class="field-hint">Applies after the next backend start.</span>
          </div>
          <div class="form-row">
            <label for="nw-max-proved-per-ip">Proved connections per address</label>
            <input id="nw-max-proved-per-ip" type="number" min="1" step="1"
              bind:value={draft.maxProvedPerIp} disabled={startBusy || loading} />
            <span class="field-hint">Applies after the next backend start.</span>
          </div>
          <div class="form-group">
            <label for="nw-enable-hole-punching">
              <input id="nw-enable-hole-punching" type="checkbox" bind:checked={flags.enableHolePunching}
                disabled={startBusy || loading} />
              Enable UDP hole punching
            </label>
            <span class="field-hint wide">
              Off by default: hole punching lacks DTLS encryption (v0.10.0).
              Read once at boot; applies after the next backend start.
            </span>
          </div>
          <div class="form-row">
            <label for="nw-udp-punch-port">UDP punch port</label>
            <input id="nw-udp-punch-port" type="number" min="1" max="65535" step="1"
              bind:value={draft.udpPunchPort} disabled={startBusy || loading} />
            <span class="field-hint">The UDP socket is bound at manager start. Applies after the next backend start.</span>
          </div>

          <h4 class="sub-group">Hub</h4>
          <div class="form-row">
            <label for="nw-hub-url">Hub URL</label>
            <input id="nw-hub-url" type="text" bind:value={draft.hubUrl}
              disabled={startBusy || loading} placeholder="http://localhost:8000" />
            <span class="field-hint">
              Captured in the Hub client as its base URL when the backend
              starts; applies after the next backend start.
            </span>
          </div>
          <div class="form-group">
            <label for="nw-hub-auto-connect">
              <input id="nw-hub-auto-connect" type="checkbox" bind:checked={flags.hubAutoConnect}
                disabled={startBusy || loading} />
              Connect to the Hub at startup
            </label>
            <span class="field-hint wide">Read once in CoreService.start; applies after the next backend start.</span>
          </div>

          <h4 class="sub-group">OAuth</h4>
          <div class="form-row">
            <label for="nw-oauth-callback-host">Callback host</label>
            <input id="nw-oauth-callback-host" type="text" bind:value={draft.oauthCallbackHost}
              disabled={startBusy || loading} />
            <span class="field-hint">
              The interface the login callback server binds. A HubClient
              field, bound at each login; applies after the next backend start.
            </span>
          </div>
          <div class="form-row">
            <label for="nw-oauth-callback-port">Callback port</label>
            <input id="nw-oauth-callback-port" type="number" min="1" max="65535" step="1"
              bind:value={draft.oauthCallbackPort} disabled={startBusy || loading} />
            <span class="field-hint">Applies after the next backend start.</span>
          </div>
          <div class="form-row">
            <label for="nw-oauth-provider">Default provider</label>
            <select id="nw-oauth-provider" bind:value={draft.oauthProvider} disabled={startBusy || loading}>
              {#each OAUTH_PROVIDERS as provider (provider)}
                <option value={provider}>{provider}</option>
              {/each}
            </select>
            <span class="field-hint">
              Read only by the start-up auto-connect; the manual login
              hardcodes "google", so this value does not change it. Applies
              after the next backend start.
            </span>
          </div>

          <h4 class="sub-group">Gateway (OpenAI-compatible)</h4>
          <div class="form-group">
            <label for="nw-gateway-enabled">
              <input id="nw-gateway-enabled" type="checkbox" bind:checked={flags.gatewayEnabled}
                disabled={startBusy || loading} />
              Enable the gateway
            </label>
            <span class="field-hint wide">
              The Inference Sharing panel reads this value at once while the
              gateway itself is built at start, so after a save the panel can
              show it enabled with no listener until the next backend start.
            </span>
          </div>
          <div class="form-row">
            <label for="nw-gateway-port">Gateway port</label>
            <input id="nw-gateway-port" type="number" min="1" max="65535" step="1"
              bind:value={draft.gatewayPort} disabled={startBusy || loading} />
            <span class="field-hint">
              The gateway server is built once when the backend starts
              (9998 is the file server, 9999 the local API); applies after the
              next backend start. The bind host is not a setting: the gateway
              refuses any value but 127.0.0.1.
            </span>
          </div>

          <h4 class="sub-group">File transfer</h4>
          <div class="form-row">
            <label for="nw-ft-chunk-size">Chunk size</label>
            <input id="nw-ft-chunk-size" type="text" bind:value={draft.ftChunkSize}
              disabled={startBusy || loading} placeholder="auto" />
            <span class="field-hint">
              auto = per file, by size, tier and measured rate; a size (65537,
              64KiB, 1MiB) fixes every file's. Applies after the next backend
              start.
            </span>
          </div>
          <div class="form-row">
            <label for="nw-ft-chunk-delay">Chunk delay (seconds)</label>
            <input id="nw-ft-chunk-delay" type="number" min="0" step="any"
              bind:value={draft.ftChunkDelay} disabled={startBusy || loading} />
            <span class="field-hint">Pause after each window of chunks; 0 = none. Applies after the next backend start.</span>
          </div>
          <div class="form-row">
            <label for="nw-ft-max-concurrent">Max concurrent transfers</label>
            <input id="nw-ft-max-concurrent" type="number" min="1" step="1"
              bind:value={draft.ftMaxConcurrent} disabled={startBusy || loading} />
            <span class="field-hint">Applies after the next backend start.</span>
          </div>
          <div class="form-group">
            <label for="nw-ft-verify-hash">
              <input id="nw-ft-verify-hash" type="checkbox" bind:checked={flags.ftVerifyHash}
                disabled={startBusy || loading} />
              Verify file hash after transfer (SHA256)
            </label>
            <span class="field-hint wide">Applies after the next backend start.</span>
          </div>
          <button class="btn-save" on:click={saveStartOnly} disabled={startBusy || loading}>
            {startBusy ? 'Saving...' : 'Save start-only settings'}
          </button>
        </section>
      </div>
    </div>
  </div>
{/if}

<style>
  .modal-overlay {
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    background: rgba(0, 0, 0, 0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }

  .modal {
    background: white;
    border-radius: 8px;
    width: 95%;
    max-width: 640px;
    max-height: 90vh;
    display: flex;
    flex-direction: column;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
  }

  .modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 1rem 1.5rem;
    border-bottom: 1px solid #e0e0e0;
  }

  .modal-header h2 {
    margin: 0;
    font-size: 1.3rem;
    color: #333;
  }

  .close-btn {
    background: none;
    border: none;
    font-size: 2rem;
    cursor: pointer;
    color: #999;
    line-height: 1;
  }

  .close-btn:hover {
    color: #333;
  }

  .modal-body {
    padding: 1rem 1.5rem;
    overflow-y: auto;
  }

  .settings-section {
    padding: 0.75rem 0;
    border-bottom: 1px solid #e0e0e0;
  }

  .settings-section:last-child {
    border-bottom: none;
  }

  .settings-section h3 {
    margin: 0 0 0.25rem 0;
    font-size: 1.05rem;
    color: #333;
  }

  .sub-group {
    margin: 0.75rem 0 0.25rem 0;
    font-size: 0.9rem;
    color: #444;
  }

  .section-hint {
    margin: 0 0 0.5rem 0;
    color: #666;
    font-size: 0.85rem;
  }

  .field-hint {
    display: block;
    color: #888;
    font-size: 0.8rem;
  }

  .field-hint.wide {
    margin-bottom: 0.5rem;
  }

  .section-error {
    padding: 0.5rem;
    background: #f8d7da;
    color: #721c24;
    border: 1px solid #f5c6cb;
    border-radius: 4px;
    font-size: 0.85rem;
    margin: 0 0 0.5rem 0;
  }

  .section-saved {
    padding: 0.5rem;
    background: #d4edda;
    color: #155724;
    border: 1px solid #c3e6cb;
    border-radius: 4px;
    font-size: 0.85rem;
    margin: 0 0 0.5rem 0;
  }

  .form-group {
    margin-bottom: 0.75rem;
  }

  .form-group label {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.4rem;
    font-size: 0.9rem;
    color: #444;
    cursor: pointer;
  }

  .form-row {
    display: grid;
    grid-template-columns: 190px 1fr;
    align-items: center;
    column-gap: 0.75rem;
    row-gap: 0.15rem;
    margin-bottom: 0.75rem;
  }

  .form-row label {
    font-size: 0.9rem;
    font-weight: 500;
    color: #555;
  }

  .form-row input,
  .form-row select,
  .token-row input {
    padding: 0.45rem;
    border: 1px solid #ddd;
    border-radius: 4px;
    font-size: 0.95rem;
  }

  .form-row input:disabled,
  .form-row select:disabled {
    background: #f2f2f2;
    color: #666;
  }

  .form-row input:focus,
  .form-row select:focus,
  .token-row input:focus {
    outline: none;
    border-color: #1976d2;
    box-shadow: 0 0 0 3px rgba(25, 118, 210, 0.1);
  }

  .form-row .field-hint {
    grid-column: 2;
    margin-bottom: 0.25rem;
  }

  .token-row {
    display: flex;
    gap: 0.5rem;
    align-items: center;
  }

  .token-row input {
    flex: 1;
  }

  .btn-remove {
    padding: 0.45rem 0.9rem;
    background: #fff;
    color: #a94442;
    border: 1px solid #a94442;
    border-radius: 4px;
    font-size: 0.9rem;
    cursor: pointer;
  }

  .btn-remove:hover:not(:disabled) {
    background: #f8d7da;
  }

  .btn-remove:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .btn-save {
    padding: 0.5rem 1rem;
    background: #1976d2;
    color: white;
    border: none;
    border-radius: 4px;
    font-size: 0.9rem;
    cursor: pointer;
    transition: background 0.2s;
  }

  .btn-save:hover:not(:disabled) {
    background: #1258a0;
  }

  .btn-save:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  @media (max-width: 560px) {
    .form-row {
      grid-template-columns: 1fr;
    }

    .form-row input,
    .form-row select,
    .form-row .field-hint {
      grid-column: 1;
    }
  }
</style>
