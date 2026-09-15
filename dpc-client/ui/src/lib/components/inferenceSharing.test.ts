import { describe, it, expect } from 'vitest';
import type { ProviderInfo } from '$lib/types';
import {
  addAllowed,
  addAllowedModel,
  addServing,
  addTariffEntry,
  callerPriceBadge,
  classifyProviderType,
  computeBlockErrors,
  computeErrorsOf,
  doorAddress,
  foldServingAlias,
  gatewayVerdict,
  groupGatewayMenu,
  isFree,
  maskedHeader,
  menuVerdict,
  MENU_IS_LIVE_NOTE,
  knownGroups,
  knownNodes,
  offeredProviders,
  removeAllowed,
  removeAllowedModel,
  removeServing,
  removeTariffEntry,
  SERVES_NO_LOCAL_ALIAS,
  setCurrency,
  setFree,
  setVendorQuota,
  shortenPeerId,
  soleMenuChoiceLine,
  splitCallerIds,
  tariffAliases,
  tariffEntryErrors,
  tariffHistory,
  tariffState,
  THIS_MACHINE_GROUP,
  unmatchedModels,
  validationDraft,
  type ComputeRules,
  type GatewayMenuEntry,
  type GatewayState,
} from './inferenceSharing';
import type { MenuRow } from './peerMenu';

const provider = (alias: string, type: string): ProviderInfo => ({ alias, model: `${alias}-model`, type, supports_vision: false });

const emptyBlock = (): ComputeRules => ({
  enabled: true,
  allow_nodes: [],
  allow_groups: [],
  allowed_models: [],
  serving_local: [],
  serving_vendor: [],
  vendor_quotas: {},
  currency: null,
  serving_tariff: {},
  free_nodes: [],
  free_groups: [],
});

describe('a provider is offered to one list by its type, or to none', () => {
  it('sends the card types to local and the paying types to vendor', () => {
    expect(classifyProviderType('ollama')).toBe('local');
    expect(classifyProviderType('llamacpp_server')).toBe('local');
    for (const type of ['openai_compatible', 'anthropic', 'zai', 'deepseek', 'gemini', 'github_models', 'gigachat']) {
      expect(classifyProviderType(type)).toBe('vendor');
    }
  });

  it('never offers somebody else\'s model (ADR-041 D7)', () => {
    expect(classifyProviderType('remote_peer')).toBe('never');
    expect(classifyProviderType('dpc_agent')).toBe('never');
  });

  it('keeps Whisper for the transcription gate and refuses to guess an unknown type', () => {
    expect(classifyProviderType('local_whisper')).toBe('transcription');
    expect(classifyProviderType('something_new')).toBe('unknown');
    expect(classifyProviderType(undefined)).toBe('unknown');
  });

  it('splits a registry so that only local and vendor aliases are pickable', () => {
    const { local, vendor } = offeredProviders([
      provider('llama', 'ollama'),
      provider('ds', 'deepseek'),
      provider('peer', 'remote_peer'),
      provider('agent', 'dpc_agent'),
      provider('whisper', 'local_whisper'),
      provider('odd', 'not_a_type'),
    ]);
    expect(local.map((p) => p.alias)).toEqual(['llama']);
    expect(vendor.map((p) => p.alias)).toEqual(['ds']);
  });
});

describe('the deprecated serving_alias is folded into serving_local the way the load path folds it', () => {
  it('becomes the one local entry when serving_local is absent', () => {
    const folded = foldServingAlias({ enabled: true, allow_nodes: [], allow_groups: [], allowed_models: [], serving_alias: 'llama' });
    expect(folded.serving_local).toEqual(['llama']);
    expect(folded.serving_alias).toBeNull();
  });

  it('yields to an existing serving_local and is dropped, as the validator message asks', () => {
    const folded = foldServingAlias({ ...emptyBlock(), serving_local: ['other'], serving_alias: 'llama' });
    expect(folded.serving_local).toEqual(['other']);
    expect(folded.serving_alias).toBeNull();
  });

  it('does not touch the object it was given and keeps keys it does not know', () => {
    const original: ComputeRules = { enabled: false, allow_nodes: ['n1'], allow_groups: [], allowed_models: ['m'], serving_alias: 'llama', _comment: 'kept' };
    const folded = foldServingAlias(original);
    expect(original.serving_alias).toBe('llama');
    expect(original.serving_local).toBeUndefined();
    expect(folded._comment).toBe('kept');
    expect(folded.allowed_models).toEqual(['m']);
    expect(folded.free_nodes).toEqual([]);
    expect(folded.vendor_quotas).toEqual({});
    expect(folded.currency).toBeNull();
  });

  it('drops the _explanation keys from the quota and tariff maps but not from the block', () => {
    const folded = foldServingAlias({
      ...emptyBlock(),
      vendor_quotas: { _comment: 'x' as unknown as number, ds: 5 },
      serving_tariff: { _comment: 'x' as unknown as never, llama: [{ from: '2026-09-01', in: 1, out: 2 }] },
      _serving_tariff: 'explanation',
    });
    expect(folded.vendor_quotas).toEqual({ ds: 5 });
    expect(Object.keys(folded.serving_tariff ?? {})).toEqual(['llama']);
    expect(folded._serving_tariff).toBe('explanation');
  });
});

describe('free is a subset of allow by construction', () => {
  it('removing a node from allow removes it from free as well', () => {
    let block = addAllowed(emptyBlock(), 'nodes', 'dpc-node-a');
    block = setFree(block, 'nodes', 'dpc-node-a', true);
    expect(isFree(block, 'nodes', 'dpc-node-a')).toBe(true);

    block = removeAllowed(block, 'nodes', 'dpc-node-a');
    expect(block.allow_nodes).toEqual([]);
    expect(block.free_nodes).toEqual([]);
  });

  it('refuses to mark free a group that is not allowed', () => {
    const block = setFree(emptyBlock(), 'groups', 'friends', true);
    expect(block.free_groups).toEqual([]);
  });

  it('does not add the same caller twice and ignores blank ids', () => {
    let block = addAllowed(emptyBlock(), 'nodes', ' dpc-node-a ');
    block = addAllowed(block, 'nodes', 'dpc-node-a');
    block = addAllowed(block, 'nodes', '   ');
    expect(block.allow_nodes).toEqual(['dpc-node-a']);
  });

  it('a pasted list is split on whitespace, commas and semicolons, trimmed, and de-duplicated in order', () => {
    expect(splitCallerIds(' dpc-node-a, dpc-node-b\n\tdpc-node-c;;dpc-node-a  ,\r\n')).toEqual(['dpc-node-a', 'dpc-node-b', 'dpc-node-c']);
    expect(splitCallerIds('')).toEqual([]);
    expect(splitCallerIds(' , ;\n')).toEqual([]);
  });

  it('adding several pasted node ids yields one element each, none twice, nothing malformed', () => {
    let block = addAllowed(emptyBlock(), 'nodes', 'dpc-node-b');
    block = addAllowed(block, 'nodes', 'dpc-node-a,dpc-node-b\n dpc-node-c ; dpc-node-a');
    expect(block.allow_nodes).toEqual(['dpc-node-b', 'dpc-node-a', 'dpc-node-c']);
    expect(block.allow_nodes.some((id) => /[\s,;]/.test(id))).toBe(false);
  });

  it('group names get the same split, and a paste that adds nothing returns the same block', () => {
    let block = addAllowed(emptyBlock(), 'groups', 'friends, trusted\nfriends');
    expect(block.allow_groups).toEqual(['friends', 'trusted']);
    const again = addAllowed(block, 'groups', ' trusted ;friends ');
    expect(again).toBe(block);
  });

  it('the pre-check names a straggler the same way the validator does', () => {
    const errors = computeBlockErrors({ ...emptyBlock(), free_groups: ['friends'] });
    expect(errors).toHaveLength(1);
    expect(errors[0]).toContain("'friends' is in compute.free_groups but not in compute.allow_groups");
  });
});

describe('what I share', () => {
  it('a vendor alias without a ceiling is refused before the round trip', () => {
    const block = addServing(emptyBlock(), 'vendor', 'ds');
    expect(computeBlockErrors(block)).toEqual([
      "'ds' is in compute.serving_vendor with no ceiling in compute.vendor_quotas: a vendor alias spends money and is refused without one (ADR-041 D5)",
    ]);
    expect(computeBlockErrors(setVendorQuota(block, 'ds', 2.5))).toEqual([]);
  });

  it('a vendor alias leaves with its ceiling and keeps its tariff', () => {
    let block = setVendorQuota(addServing(emptyBlock(), 'vendor', 'ds'), 'ds', 2.5);
    block = addTariffEntry(block, 'ds', { from: '2026-09-01', in: 1, out: 2 });
    block = removeServing(block, 'vendor', 'ds');
    expect(block.serving_vendor).toEqual([]);
    expect(block.vendor_quotas).toEqual({});
    expect(block.serving_tariff?.ds).toHaveLength(1);
    expect(tariffAliases(block)).toEqual([{ alias: 'ds', served: false }]);
  });

  it('an alias in both lists is named', () => {
    const block = addServing(addServing(emptyBlock(), 'local', 'x'), 'vendor', 'x');
    expect(computeBlockErrors(setVendorQuota(block, 'x', 1)).join('\n')).toContain('is in both compute.serving_local and compute.serving_vendor');
  });

  it('an alias in both serving lists is priced once, so the keyed rows cannot collide', () => {
    let block = addServing(addServing(emptyBlock(), 'local', 'x'), 'vendor', 'x');
    block = addTariffEntry(block, 'x', { from: '2026-09-01', in: 1, out: 2 });
    block = addTariffEntry(block, 'y', { from: '2026-09-01', in: 1, out: 2 });
    const rows = tariffAliases(block);
    expect(rows).toEqual([{ alias: 'x', served: true }, { alias: 'y', served: false }]);
    expect(new Set(rows.map((r) => r.alias)).size).toBe(rows.length);
  });
});

describe('the models this door accepts', () => {
  it('names models, splitting a paste the way the caller lists split one, each once', () => {
    let block = addAllowedModel(emptyBlock(), 'llama3.1:8b');
    block = addAllowedModel(block, 'qwen3:14b, llama3.1:8b\n gpt-4o ; qwen3:14b');
    expect(block.allowed_models).toEqual(['llama3.1:8b', 'qwen3:14b', 'gpt-4o']);
    expect(addAllowedModel(block, ' gpt-4o ')).toBe(block);
    expect(addAllowedModel(block, '  ')).toBe(block);
  });

  it('removing the last named model widens the door: empty accepts every model', () => {
    const block = addAllowedModel(emptyBlock(), 'llama3.1:8b');
    expect(removeAllowedModel(block, 'llama3.1:8b').allowed_models).toEqual([]);
    expect(removeAllowedModel(block, 'never-listed')).toBe(block);
  });

  it('badges a model no configured provider carries, by model and not by alias', () => {
    const providers = [provider('llama', 'ollama'), provider('ds', 'deepseek')];
    const block = addAllowedModel(emptyBlock(), 'llama-model, ds, gone:70b');
    expect(unmatchedModels(block, providers)).toEqual(['ds', 'gone:70b']);
    expect(unmatchedModels(emptyBlock(), providers)).toEqual([]);
    expect(unmatchedModels(block, [])).toEqual(['llama-model', 'ds', 'gone:70b']);
    expect(unmatchedModels(null, null)).toEqual([]);
  });

  it('a list emptied of models is still carried through the pre-check unchanged', () => {
    expect(computeBlockErrors(addAllowedModel(emptyBlock(), 'gone:70b'))).toEqual([]);
  });

  // The tab has to say the rule out loud, because it is the opposite of the
  // serving lists two headings above: there, empty means nobody is served.
  // Vitest runs in `environment: 'node'` here and the repo carries no DOM
  // harness, so this reads the component source rather than rendering it
  // (the same bargain approvalCardContrast.test.ts strikes).
  it('says in the tab itself that an empty list accepts every model', () => {
    const sources = import.meta.glob('./InferenceSharingEditor.svelte', {
      query: '?raw',
      import: 'default',
      eager: true,
    }) as Record<string, string>;
    const tab = Object.values(sources)[0];
    expect(tab).toBeTruthy();
    expect(tab).toContain('<strong>An empty list accepts every model</strong>');
    expect(tab).toContain('No model named &mdash; every model this node serves is accepted.');
    expect(tab).toContain('matches no configured provider');
  });
});

describe('the tariff has three states', () => {
  const today = '2026-09-13';

  it('no currency is a gift whatever the entries say', () => {
    const state = tariffState([{ from: '2026-01-01', in: 5, out: 10 }], null, today);
    expect(state).toMatchObject({ kind: 'gift', reason: 'no-currency' });
  });

  it('no entry is a gift, an entry only in the future is a gift for now', () => {
    expect(tariffState([], 'USD', today)).toMatchObject({ kind: 'gift', reason: 'no-entry' });
    const state = tariffState([{ from: '2026-10-01', in: 5, out: 10 }], 'USD', today);
    expect(state).toMatchObject({ kind: 'gift', reason: 'not-yet' });
    expect(state.upcoming).toHaveLength(1);
  });

  it('a declared zero is free by decision, a rate above zero is paid, and the newest applicable line wins', () => {
    expect(tariffState([{ from: '2026-01-01', in: 0, out: 0 }], 'RUB', today)).toMatchObject({ kind: 'free' });
    const state = tariffState(
      [
        { from: '2026-01-01', in: 0, out: 0 },
        { from: '2026-09-01', in: 20, out: 60 },
        { from: '2026-12-01', in: 1, out: 1 },
      ],
      'RUB',
      today,
    );
    expect(state.kind).toBe('paid');
    if (state.kind === 'paid') expect(state.entry.from).toBe('2026-09-01');
    expect(state.upcoming.map((e) => e.from)).toEqual(['2026-12-01']);
  });

  it('history reads newest first and is not the array it was given', () => {
    const entries = [{ from: '2026-01-01', in: 0, out: 0 }, { from: '2026-09-01', in: 1, out: 1 }];
    expect(tariffHistory(entries).map((e) => e.from)).toEqual(['2026-09-01', '2026-01-01']);
    expect(entries[0].from).toBe('2026-01-01');
  });

  it('a new line is appended, never rewrites an older one, and one day has one rate', () => {
    let block = addServing(emptyBlock(), 'local', 'llama');
    block = addTariffEntry(block, 'llama', { from: '2026-09-01', in: 20, out: 60 });
    block = addTariffEntry(block, 'llama', { from: '2026-09-13', in: 10, out: 30 });
    expect(block.serving_tariff?.llama).toEqual([
      { from: '2026-09-01', in: 20, out: 60 },
      { from: '2026-09-13', in: 10, out: 30 },
    ]);
    expect(tariffEntryErrors(block.serving_tariff?.llama, { from: '2026-09-13', in: 1, out: 1 })).toEqual([
      'there is already an entry from 2026-09-13; one day has one rate',
    ]);
    expect(addTariffEntry(block, 'llama', { from: '2026-09-13', in: 1, out: 1 })).toBe(block);
    expect(tariffEntryErrors([], { from: '20260913', in: -1, out: 1 })).toEqual([
      "'from' must be an ISO date YYYY-MM-DD, got \"20260913\"",
      "'in' must be a non-negative number per 1M tokens",
    ]);
    block = removeTariffEntry(block, 'llama', '2026-09-13');
    expect(block.serving_tariff?.llama).toHaveLength(1);
  });

  it('the currency is upper-cased, cleared by an empty string, and checked against the ISO table', () => {
    expect(setCurrency(emptyBlock(), ' rub ').currency).toBe('RUB');
    expect(setCurrency(emptyBlock(), '').currency).toBeNull();
    expect(computeBlockErrors(setCurrency(emptyBlock(), 'XYZ'))[0]).toContain("'compute.currency' must be an ISO 4217 code");
    expect(computeBlockErrors(setCurrency(emptyBlock(), 'USD'))).toEqual([]);
  });
});

describe('the callers the tab can offer', () => {
  it('collects peers, group members, per-node rules and the block itself, connected first', () => {
    const nodes = knownNodes(
      [{ node_id: 'dpc-node-b', display_name: 'Bob', is_connected: true }],
      { _comment: 'x', friends: ['dpc-node-a', 'dpc-node-b'] },
      ['dpc-node-c'],
      { ...emptyBlock(), allow_nodes: ['dpc-node-gone'] },
    );
    expect(nodes.map((n) => n.node_id)).toEqual(['dpc-node-b', 'dpc-node-a', 'dpc-node-c', 'dpc-node-gone']);
    expect(nodes[0]).toEqual({ node_id: 'dpc-node-b', label: 'Bob', connected: true });
    expect(knownGroups({ _comment: 'x', friends: [], trusted: [] }, { ...emptyBlock(), allow_groups: ['old'] })).toEqual(['friends', 'old', 'trusted']);
  });

  it('keeps only the save refusals that concern compute', () => {
    expect(computeErrorsOf(["'compute.currency' must be …", "'transcription.enabled' must be a boolean"])).toEqual(["'compute.currency' must be …"]);
  });
});

describe('the verdict at the top of the tab reads the two doors apart', () => {
  const door = (over: Partial<GatewayState> = {}): GatewayState => ({
    enabled: true,
    running: true,
    port: 8899,
    bind: '127.0.0.1',
    key_masked: 'wtHZ…opY4',
    key_file: '/home/u/.dpc/.gateway_key',
    serving_local: ['llama'],
    serving_vendor: [],
    serving_error: null,
    compute_enabled: true,
    ...over,
  });

  it('names both doors when both are open, at the address the backend reported', () => {
    const verdict = gatewayVerdict(door({ bind: '::1', port: 9100 }), true);
    expect(verdict.tone).toBe('shared');
    expect(verdict.text).toBe('Peers on the allow lists and IDE clients on ::1:9100 can call the serving lists.');
  });

  it('says which door is shut when only one is', () => {
    expect(gatewayVerdict(door({ enabled: false, running: false }), true)).toEqual({
      tone: 'partial',
      text: 'Peers can call; the IDE door is off ([gateway] enabled = false).',
    });
    expect(gatewayVerdict(door(), false)).toEqual({
      tone: 'partial',
      text: 'The IDE door is open for this machine only; no peer is served (compute.enabled = false).',
    });
  });

  it('says nothing is shared when neither is on', () => {
    expect(gatewayVerdict(door({ enabled: false, running: false }), false)).toEqual({ tone: 'off', text: 'Nothing is shared.' });
  });

  it('quotes a serving-list refusal and does not call it a listener that failed to open', () => {
    const verdict = gatewayVerdict(door({ serving_error: "compute.serving_local names 'ghost', whose provider is not loaded" }), true);
    expect(verdict.tone).toBe('error');
    expect(verdict.text).toContain('every call on them is refused');
    expect(verdict.text).toContain("names 'ghost'");
    expect(verdict.text).not.toContain('listening');
  });

  it('separates configured from listening in both directions', () => {
    expect(gatewayVerdict(door({ running: false }), true)).toEqual({
      tone: 'error',
      text: 'The IDE door is configured but not running — see the log; nothing is listening on 127.0.0.1:8899.',
    });
    expect(gatewayVerdict(door({ enabled: false }), true).text).toContain('stays open until the next restart');
  });

  it('says so rather than guessing before the state has been read', () => {
    expect(gatewayVerdict(null, true)).toEqual({ tone: 'off', text: 'The IDE door has not been read yet.' });
    expect(doorAddress(undefined)).toBe('127.0.0.1:?');
  });
});

describe('the header of the client blocks carries the masked key only', () => {
  it('names the clients and the masked key, never the key itself', () => {
    const header = maskedHeader({
      lines: [
        { client: 'continue', text: '{"apiKey": "sk-secret-value"}' },
        { client: 'cursor', text: 'API key: sk-secret-value' },
        { client: 'claude_code', text: 'export ANTHROPIC_API_KEY=sk-secret-value' },
        { client: 'curl', text: 'curl -H "Authorization: Bearer sk-secret-value"' },
      ],
      key_masked: 'abcd…wxyz',
    });
    expect(header).toBe('Continue, Cursor, Claude Code, curl — each block carries the key abcd…wxyz in clear');
    expect(header).not.toContain('sk-secret-value');
  });

  it('says there is no key rather than showing an empty one, and prints an unknown client as it arrived', () => {
    expect(maskedHeader({ lines: [{ client: 'zed', text: '…' }], key_masked: null }))
      .toBe('zed — no key has been written yet; start the gateway once');
    expect(maskedHeader({ lines: [], key_masked: 'wtHZ…opY4' })).toBe('nothing to paste yet; the key is wtHZ…opY4');
    expect(maskedHeader(null)).toBe('nothing to paste yet, and no key has been written');
  });
});

describe('the client-lines selector groups menu rows by owner', () => {
  const local = (): GatewayMenuEntry => ({ id: 'llama', owner: 'local', alias: 'llama', label: 'llama' });
  const vendor = (): GatewayMenuEntry => ({ id: 'ds', owner: 'vendor', alias: 'ds', label: 'ds' });
  const peerRow = (peerId: string, peerName: string | null, alias = 'llama'): GatewayMenuEntry => ({
    id: `remote:${peerId}:${alias}`,
    owner: 'peer',
    alias,
    label: peerName ? `${alias} (${peerName})` : `${alias} (${peerId})`,
    peer_id: peerId,
    peer_name: peerName,
  });

  it('an empty menu yields no groups — the placeholder-block path is untouched', () => {
    expect(groupGatewayMenu([])).toEqual([]);
    expect(groupGatewayMenu(null)).toEqual([]);
    expect(groupGatewayMenu(undefined)).toEqual([]);
  });

  it('a menu with only this node\'s own rows is one group, titled "this machine"', () => {
    const groups = groupGatewayMenu([local(), vendor()]);
    expect(groups).toHaveLength(1);
    expect(groups[0].title).toBe(THIS_MACHINE_GROUP);
    expect(groups[0].entries.map((e) => e.id)).toEqual(['llama', 'ds']);
  });

  it('a menu with only peer rows — the guest-node shape, the case that was broken — has no "this machine" group, one group per peer', () => {
    const alice = peerRow('dpc-node-alice000000000000000000000000000', 'Alice');
    const bob = peerRow('dpc-node-bob0000000000000000000000000000', 'Bob', 'mistral');
    const groups = groupGatewayMenu([alice, bob]);
    expect(groups.map((g) => g.title)).toEqual(['Alice', 'Bob']);
    expect(groups.every((g) => g.title !== THIS_MACHINE_GROUP)).toBe(true);
    expect(groups[0].entries).toEqual([alice]);
    expect(groups[1].entries).toEqual([bob]);
  });

  it('a mixed menu puts this machine first, then one group per peer, a peer\'s rows staying together', () => {
    const alice1 = peerRow('dpc-node-alice000000000000000000000000000', 'Alice', 'llama');
    const alice2 = peerRow('dpc-node-alice000000000000000000000000000', 'Alice', 'qwen');
    const groups = groupGatewayMenu([local(), alice1, vendor(), alice2]);
    expect(groups.map((g) => g.title)).toEqual([THIS_MACHINE_GROUP, 'Alice']);
    expect(groups[0].entries.map((e) => e.id)).toEqual(['llama', 'ds']);
    expect(groups[1].entries).toEqual([alice1, alice2]);
  });

  it('a peer with no name gets a shortened id, never the raw node_id, as its group title', () => {
    const longId = 'dpc-node-' + 'f'.repeat(64);
    const row = peerRow(longId, null);
    const groups = groupGatewayMenu([row]);
    expect(groups[0].title).not.toBe(longId);
    expect(groups[0].title.length).toBeLessThan(longId.length);
    expect(shortenPeerId(longId)).toBe(groups[0].title);
  });

  it('a malformed row with no id is dropped rather than shown with nothing to select', () => {
    const bad = { owner: 'local', alias: 'x', label: 'x' } as unknown as GatewayMenuEntry;
    expect(groupGatewayMenu([bad, local()])).toEqual([{ title: THIS_MACHINE_GROUP, entries: [local()] }]);
  });
});

describe('the sole-entry line and the live-list note', () => {
  it('names the one entry outright rather than leaving a one-item dropdown to speak for itself', () => {
    const entry: GatewayMenuEntry = { id: 'llama', owner: 'local', alias: 'llama', label: 'llama' };
    expect(soleMenuChoiceLine(entry)).toContain('llama');
    expect(soleMenuChoiceLine(entry)).toContain('Only one model');
    expect(soleMenuChoiceLine(null)).toBe('');
  });

  it('says the list is proved-and-connected-now, from GET /v1/models, and that a drop makes a paste start failing', () => {
    expect(MENU_IS_LIVE_NOTE).toContain('GET /v1/models');
    expect(MENU_IS_LIVE_NOTE).toContain('proved and connected right now');
    expect(MENU_IS_LIVE_NOTE.toLowerCase()).toContain('drop');
  });
});

describe('the client-lines selector re-requests the blocks for the newly picked id', () => {
  // No DOM harness (see the "empty list" test above for the same bargain):
  // this reads the component source to prove the dropdown's change handler
  // asks the backend again with the new id, rather than relabeling old blocks.
  it('is bound to selectedMenuId and re-asks get_gateway_client_lines with { selected_id: id } on change', () => {
    const sources = import.meta.glob('./InferenceSharingEditor.svelte', {
      query: '?raw',
      import: 'default',
      eager: true,
    }) as Record<string, string>;
    const tab = Object.values(sources)[0];
    expect(tab).toBeTruthy();
    expect(tab).toMatch(/bind:value=\{selectedMenuId\}/);
    expect(tab).toMatch(/on:change=\{\(\) => selectMenu\(selectedMenuId\)\}/);
    expect(tab).toMatch(/get_gateway_client_lines',\s*\{\s*selected_id:\s*id\s*\}/);
    // Grouped: this node's own rows first, then one group per peer — never
    // the raw node_id shown where a label belongs.
    expect(tab).toContain('<optgroup label={group.title}>');
    expect(tab).toContain('<option value={entry.id}>{entry.label}</option>');
  });
});

describe('what a peer sees has three states, and they ask for different repairs', () => {
  const row = (alias: string): MenuRow => ({ alias, model: `${alias}-model`, type: 'ollama' });

  it('a peer on no list is refused, and the backend reason stands beneath', () => {
    const verdict = menuVerdict({ peer_id: 'dpc-node-a', known: true, connected: false, allowed: false, reason: 'dpc-node-a may ask this node for neither inference nor transcription', rows: [] });
    expect(verdict.kind).toBe('refused');
    expect(verdict.text).toBe('This peer gets an empty menu: it is on no list that admits it.');
    expect(verdict.detail).toContain('neither inference nor transcription');
    expect(verdict.rows).toEqual([]);
  });

  it('an allowed peer with nothing designated is sent to the serving lists, not to the permissions', () => {
    const verdict = menuVerdict({ allowed: true, reason: 'no alias is designated in compute.serving_local', rows: [] });
    expect(verdict.kind).toBe('empty');
    expect(verdict.text).toBe('Allowed, but nothing is designated for it — check the serving lists.');
    expect(verdict.detail).toBe('no alias is designated in compute.serving_local');
  });

  it('counts the menu rows that would go out, and drops a row with no alias', () => {
    const served = menuVerdict({ allowed: true, reason: null, rows: [row('llama'), row('whisper')] });
    expect(served.kind).toBe('served');
    expect(served.text).toBe('2 menu rows would be sent to this peer.');
    expect(served.detail).toBeNull();
    expect(menuVerdict({ allowed: true, rows: [row('llama'), {} as MenuRow] }).text).toBe('1 menu row would be sent to this peer.');
    expect(menuVerdict(null).kind).toBe('none');
  });
});

describe('validate sends exactly what save would post', () => {
  it('returns the whole draft as-is, so an edit on another block (transcription here) reaches the validator untouched', () => {
    const whole = {
      compute: { enabled: false },
      transcription: { enabled: true, allow_nodes: ['dpc-node-a'] },
      node_groups: { friends: [] },
    };
    // The saved/compute/nodeGroups fallback arguments are ignored once a
    // whole draft is given — passing conflicting values proves they are not
    // consulted, not merely that they happen to agree.
    const conflictingCompute = { ...emptyBlock(), enabled: true };
    const draft = validationDraft(whole, { compute: { enabled: true } }, conflictingCompute, { friends: ['ignored'] });
    expect(draft).toBe(whole);
    expect(draft.transcription).toEqual({ enabled: true, allow_nodes: ['dpc-node-a'] });
    expect(draft.compute).toEqual({ enabled: false });
  });

  it('falls back to the file on disk with this tab\'s compute and node_groups laid over it when no whole draft is handed down', () => {
    const saved = { compute: { enabled: false }, transcription: { enabled: true }, node_groups: { friends: [] } };
    const compute = { ...emptyBlock(), allow_nodes: ['dpc-node-a'] };
    const draft = validationDraft(null, saved, compute, { friends: ['dpc-node-a'] });
    expect(draft.compute).toBe(compute);
    expect(draft.node_groups).toEqual({ friends: ['dpc-node-a'] });
    expect(draft.transcription).toEqual({ enabled: true });
    expect(saved.compute).toEqual({ enabled: false });
  });

  it('keeps the saved blocks when the tab has nothing of its own to lay over, and answers {} for no input at all', () => {
    const saved = { compute: { enabled: false }, node_groups: { friends: [] } };
    expect(validationDraft(null, saved, null, null)).toEqual(saved);
    expect(validationDraft(null, null, null, null)).toEqual({});
  });

  // The pure function above proves the merge logic; this proves the wiring
  // actually uses it — that FirewallEditor.svelte hands the tab the same
  // object it saves, and the tab prefers it over a fresh disk read. Read as
  // raw source (Vitest runs in `environment: 'node'`, no DOM harness here),
  // the same bargain the "empty list" test above strikes.
  it('the parent hands the tab the exact draft it would save, and the tab checks that draft when given one', () => {
    const firewallSources = import.meta.glob('./FirewallEditor.svelte', {
      query: '?raw',
      import: 'default',
      eager: true,
    }) as Record<string, string>;
    const firewallEditor = Object.values(firewallSources)[0];
    expect(firewallEditor).toBeTruthy();
    expect(firewallEditor).toMatch(/draftRules=\{editMode && editedRules \?/);

    const tabSources = import.meta.glob('./InferenceSharingEditor.svelte', {
      query: '?raw',
      import: 'default',
      eager: true,
    }) as Record<string, string>;
    const tab = Object.values(tabSources)[0];
    expect(tab).toBeTruthy();
    expect(tab).toContain('export let draftRules');
    expect(tab).toMatch(/if \(draftRules\)\s*\{\s*rules = validationDraft\(draftRules, null, null, null\);/);
  });
});

describe('the Serves row of the IDE door, with both lists empty', () => {
  it("says the local aliases are refused and the peers' models are served", () => {
    expect(SERVES_NO_LOCAL_ALIAS).toContain("peers' models are served");
    expect(SERVES_NO_LOCAL_ALIAS).toContain("no alias of this node's own");
    expect(SERVES_NO_LOCAL_ALIAS).toContain('calls to local aliases are refused');
  });

  it('never says every call is refused, which the door disproves with a 200', () => {
    expect(SERVES_NO_LOCAL_ALIAS).not.toContain('every call is refused');
  });
});

describe('what an admitted caller is marked with', () => {
  it('names no price while no currency is declared', () => {
    expect(callerPriceBadge(false, null)).toBe('at tariff (none declared — gift)');
    expect(callerPriceBadge(false, undefined)).toBe('at tariff (none declared — gift)');
    expect(callerPriceBadge(false, '')).toBe('at tariff (none declared — gift)');
  });

  it('names the tariff once a currency stands behind it', () => {
    expect(callerPriceBadge(false, 'EUR')).toBe('at tariff');
  });

  it('marks a free caller free whether or not a tariff exists', () => {
    expect(callerPriceBadge(true, null)).toBe('free');
    expect(callerPriceBadge(true, 'EUR')).toBe('free');
  });
});
