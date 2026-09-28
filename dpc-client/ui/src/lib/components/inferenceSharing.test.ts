import { describe, it, expect } from 'vitest';
import type { ProviderInfo } from '$lib/types';
import {
  addAllowed,
  addAllowedModel,
  addServing,
  addTariffEntry,
  blockedModelLine,
  callerPriceBadge,
  classifyProviderType,
  clientLabel,
  computeBlockErrors,
  computeErrorsOf,
  contextWindowLine,
  doorAddress,
  formatContextWindow,
  formatQuotaBlockers,
  formatQuotaDuration,
  formatQuotaLine,
  formatQuotaLineTitle,
  formatQuotaWindow,
  formatQuotaWindowTitle,
  foldServingAlias,
  quotaHasWarning,
  quotaWindowIsWarning,
  quotaWindowPercent,
  QUOTA_WARN_PERCENT,
  gatewayVerdict,
  groupGatewayMenu,
  isFree,
  maskedHeader,
  menuVerdict,
  MENU_IS_LIVE_NOTE,
  knownGroups,
  knownNodes,
  offeredProviders,
  PEER_GROUP_PREFIX,
  peerGroupTitle,
  removeAllowed,
  removeAllowedModel,
  removeServing,
  removeTariffEntry,
  selectedMenuEntry,
  SERVES_NO_LOCAL_ALIAS,
  setAliasCurrency,
  setCurrency,
  setFree,
  setVendorQuota,
  shortenPeerId,
  soleMenuChoiceLine,
  splitCallerIds,
  tariffAliases,
  tariffCurrencyOf,
  tariffCurrencySourceLabel,
  tariffEntryErrors,
  tariffHistory,
  tariffState,
  THIS_MACHINE_GROUP,
  unmatchedModels,
  validationDraft,
  vendorQuotaBadge,
  vendorQuotaLabel,
  accountRowsFromBalances,
  isSubscriptionAccount,
  walletLevel,
  quotaLevel,
  accountLevel,
  balanceErrorText,
  providerTypeLabel,
  localResetTime,
  BALANCE_LEVEL_THRESHOLDS,
  type AccountRow,
  type BalanceResult,
  type ComputeRules,
  type GatewayMenuEntry,
  type GatewayState,
  type ProviderQuota,
  type QuotaWindow,
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
    for (const type of ['openai_compatible', 'anthropic', 'zai', 'deepseek', 'neuraldeep', 'gemini', 'github_models', 'gigachat']) {
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
  function tabSource(): string {
    const sources = import.meta.glob('./InferenceSharingEditor.svelte', {
      query: '?raw',
      import: 'default',
      eager: true,
    }) as Record<string, string>;
    return Object.values(sources)[0];
  }

  it('says in the tab itself that an empty list accepts every model', () => {
    const tab = tabSource();
    expect(tab).toBeTruthy();
    expect(tab).toContain('<strong>An empty list accepts every model</strong>');
    expect(tab).toContain('No model named &mdash; every model this node serves is accepted.');
    expect(tab).toContain('matches no configured provider');
  });

  // Mike's call, 2026-09-18: every alias marked served over P2P is served, so
  // the badge is on every row — the index===0 special case is the defect.
  it('badges every served local alias, not the first one only', () => {
    const tab = tabSource();
    expect(tab).not.toContain('{#if index === 0}<span class="badge badge-first">served over P2P</span>{/if}');
    expect(tab).toContain('<span class="badge badge-first">served over P2P</span>');
    expect(tab).not.toContain('the first one is what peers are served from over P2P');
  });

  // Mike's call, 2026-09-18: the tab read like the ADR that designed it. The
  // rationale is folded away, not deleted, and the sentences that change an
  // action stay in the open line.
  it('keeps each block to one line of copy with the rationale folded behind details', () => {
    const tab = tabSource();
    expect(tab.match(/<details class="why">/g)?.length ?? 0).toBeGreaterThanOrEqual(5);
    expect(tab).toContain('ADR-041 D7, amendment 2026-09-14');
    expect(tab).toContain('a vendor alias needs a daily ceiling');
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

  it('names the settings JSON as its own block, and keeps its note out of what Copy hands over', () => {
    // Two blocks for one client: the exports, and the file `claude --settings`
    // reads. The note carries the menu note and the launch line, which JSON
    // cannot hold inside itself, so it is rendered beside the body and never
    // copied with it.
    expect(clientLabel('claude_code_settings')).toBe('Claude Code — settings JSON');
    const sources = import.meta.glob('./InferenceSharingEditor.svelte', {
      query: '?raw',
      import: 'default',
      eager: true,
    }) as Record<string, string>;
    const tab = Object.values(sources)[0];
    expect(tab).toBeTruthy();
    expect(tab).toContain('{#if line.note}');
    expect(tab).toMatch(/copy\(line\.text, clientLabel\(line\.client\)\)/);
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
    expect(groups[0].key).toBe(THIS_MACHINE_GROUP);
    expect(groups[0].entries.map((e) => e.id)).toEqual(['llama', 'ds']);
  });

  it('a menu with only peer rows — the guest-node shape, the case that was broken — has no "this machine" group, one group per peer, titled with the Peer — prefix', () => {
    const alice = peerRow('dpc-node-alice000000000000000000000000000', 'Alice');
    const bob = peerRow('dpc-node-bob0000000000000000000000000000', 'Bob', 'mistral');
    const groups = groupGatewayMenu([alice, bob]);
    expect(groups.map((g) => g.title)).toEqual([`${PEER_GROUP_PREFIX}Alice`, `${PEER_GROUP_PREFIX}Bob`]);
    expect(groups.every((g) => g.title !== THIS_MACHINE_GROUP)).toBe(true);
    expect(groups[0].entries).toEqual([alice]);
    expect(groups[1].entries).toEqual([bob]);
  });

  it('a mixed menu puts this machine first, then one group per peer, a peer\'s rows staying together', () => {
    const alice1 = peerRow('dpc-node-alice000000000000000000000000000', 'Alice', 'llama');
    const alice2 = peerRow('dpc-node-alice000000000000000000000000000', 'Alice', 'qwen');
    const groups = groupGatewayMenu([local(), alice1, vendor(), alice2]);
    expect(groups.map((g) => g.title)).toEqual([THIS_MACHINE_GROUP, `${PEER_GROUP_PREFIX}Alice`]);
    expect(groups[0].entries.map((e) => e.id)).toEqual(['llama', 'ds']);
    expect(groups[1].entries).toEqual([alice1, alice2]);
  });

  it('a peer with no name gets a shortened id, never the raw node_id, inside its group title', () => {
    const longId = 'dpc-node-' + 'f'.repeat(64);
    const row = peerRow(longId, null);
    const groups = groupGatewayMenu([row]);
    expect(groups[0].title).not.toBe(longId);
    expect(groups[0].title.length).toBeLessThan(longId.length);
    expect(groups[0].title).toBe(peerGroupTitle(null, longId));
    expect(groups[0].title).toBe(`${PEER_GROUP_PREFIX}${shortenPeerId(longId)}`);
  });

  it('a malformed row with no id is dropped rather than shown with nothing to select', () => {
    const bad = { owner: 'local', alias: 'x', label: 'x' } as unknown as GatewayMenuEntry;
    expect(groupGatewayMenu([bad, local()])).toEqual([{ key: THIS_MACHINE_GROUP, title: THIS_MACHINE_GROUP, entries: [local()] }]);
  });

  // The defect found in review, 2026-09-16: a peer's title is free text
  // (chosen in its HELLO), so it could forge the "this machine" heading, key
  // two distinct groups under one `{#each}` key, or merge into another
  // peer's group. Each case is falsified below by breaking the guard it
  // proves and watching the assertion turn red before restoring it.

  it('a peer named exactly "this machine" gets a title distinct from the this-machine group, not equal to it', () => {
    const impostor = peerRow('dpc-node-impostor00000000000000000000000', 'this machine');
    const bob = peerRow('dpc-node-bob0000000000000000000000000000', 'Bob');
    const groups = groupGatewayMenu([local(), impostor, bob]);
    const titles = groups.map((g) => g.title);
    expect(titles).toEqual([THIS_MACHINE_GROUP, `${PEER_GROUP_PREFIX}this machine`, `${PEER_GROUP_PREFIX}Bob`]);
    expect(new Set(titles).size).toBe(titles.length);
    // The own group and the impostor's group must read as different strings,
    // not merely be different objects — the human picks by the title text.
    expect(groups[1].title).not.toBe(THIS_MACHINE_GROUP);
  });

  it('two peers who share a display name get two distinct groups, not one merged group', () => {
    const bobA = peerRow('dpc-node-bob0000000000000000000000000000', 'Bob');
    const bobB = peerRow('dpc-node-bob2222222222222222222222222222', 'Bob', 'mistral');
    const groups = groupGatewayMenu([bobA, bobB]);
    expect(groups).toHaveLength(2);
    expect(groups[0].title).toBe(groups[1].title);
    expect(groups[0].key).not.toBe(groups[1].key);
    expect(groups[0].entries).toEqual([bobA]);
    expect(groups[1].entries).toEqual([bobB]);
  });

  it('a peer row with no peer_id does not merge into another peer\'s group, even by a coincidence of label text', () => {
    const anon1: GatewayMenuEntry = { id: 'remote:unknown-1:llama', owner: 'peer', alias: 'llama', label: 'llama (an unnamed peer)', peer_id: null, peer_name: null };
    const anon2: GatewayMenuEntry = { id: 'remote:unknown-2:llama', owner: 'peer', alias: 'llama', label: 'llama (an unnamed peer)', peer_id: null, peer_name: null };
    const groups = groupGatewayMenu([anon1, anon2]);
    expect(groups).toHaveLength(2);
    expect(groups[0].entries).toEqual([anon1]);
    expect(groups[1].entries).toEqual([anon2]);
    expect(groups[0].key).not.toBe(groups[1].key);
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

describe('the context-window line under the client-menu selector', () => {
  const entry = (id: string, context_window?: number | null): GatewayMenuEntry => ({
    id, owner: 'local', alias: id, label: id, context_window,
  });

  it('formats a known window for a human at a glance, thousands separated', () => {
    expect(formatContextWindow(128000, 'en-US')).toBe('128,000');
    expect(formatContextWindow(128000)).toBe((128000).toLocaleString());
  });

  it('says "unknown" in words for null or a missing key, never a blank or a zero', () => {
    expect(formatContextWindow(null)).toBe('unknown');
    expect(formatContextWindow(undefined)).toBe('unknown');
    expect(formatContextWindow(null)).not.toBe('0');
    expect(formatContextWindow(null)).not.toBe('');
  });

  it('a known window is named in the host\'s own voice, and carries no "unknown" wording anywhere', () => {
    const line = contextWindowLine(entry('llama', 128000), 'en-US');
    expect(line).toBe('Context window: 128,000 tokens — the window one conversation may take, as the host states it.');
    expect(line.toLowerCase()).not.toContain('unknown');
  });

  it('a null window says unknown outright, not a blank or a zero', () => {
    const line = contextWindowLine(entry('ds', null));
    expect(line).toBe('Context window: unknown — the host did not state one for this model.');
    expect(line).not.toBe('');
    expect(line).not.toContain('0 tokens');
  });

  it('a missing key reads exactly like null — the two are never told apart', () => {
    const withKeyMissing: GatewayMenuEntry = { id: 'ds', owner: 'local', alias: 'ds', label: 'ds' };
    expect(contextWindowLine(withKeyMissing)).toBe(contextWindowLine(entry('ds', null)));
  });

  it('no entry selected renders no line at all', () => {
    expect(contextWindowLine(null)).toBe('');
    expect(contextWindowLine(undefined)).toBe('');
  });

  it('the entry the line describes is the one the selection names, and moves with the selection', () => {
    const menu = [entry('llama', 128000), entry('ds', null), entry('mistral', 32000)];
    expect(selectedMenuEntry(menu, 'llama')?.context_window).toBe(128000);
    expect(selectedMenuEntry(menu, 'ds')?.context_window).toBeNull();
    expect(selectedMenuEntry(menu, 'mistral')?.context_window).toBe(32000);
    expect(contextWindowLine(selectedMenuEntry(menu, 'llama'), 'en-US')).toContain('128,000');
    expect(contextWindowLine(selectedMenuEntry(menu, 'mistral'), 'en-US')).toContain('32,000');
  });

  it('a stale id that names no row in the current menu selects nothing, rather than showing a different model\'s window under the old id', () => {
    const menu = [entry('llama', 128000)];
    expect(selectedMenuEntry(menu, 'gone')).toBeNull();
    expect(selectedMenuEntry(menu, '')).toBeNull();
    expect(selectedMenuEntry([], 'llama')).toBeNull();
    expect(selectedMenuEntry(null, 'llama')).toBeNull();
  });
});

describe('the tab wires the context-window line to the live selection', () => {
  it('reads selectedEntry off selectedMenuId and renders contextWindowLine(selectedEntry) beside the selector', () => {
    const sources = import.meta.glob('./InferenceSharingEditor.svelte', {
      query: '?raw',
      import: 'default',
      eager: true,
    }) as Record<string, string>;
    const tab = Object.values(sources)[0];
    expect(tab).toBeTruthy();
    expect(tab).toMatch(/selectedEntry = selectedMenuEntry\(clientLines\?\.menu, selectedMenuId\)/);
    expect(tab).toContain('{contextWindowLine(selectedEntry)}');
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

// Since 2026-09-28 both doors count a vendor ceiling in the currency the
// serving provider bills in; the backend names it per row (`ceiling_currency`,
// from `pricing.vendor_ceiling_currency`) and the tab only reads it.
describe('a vendor ceiling is labelled in the currency its alias bills in', () => {
  const nd: ProviderInfo = { alias: 'qwen 3.8 27b ND', model: 'qwen3.8-27b', type: 'neuraldeep', supports_vision: false, ceiling_currency: 'RUB' };
  const ds: ProviderInfo = { alias: 'ds', model: 'deepseek-v4-flash', type: 'deepseek', supports_vision: false, ceiling_currency: 'USD' };
  const unrated: ProviderInfo = { alias: 'odd', model: 'x', type: 'openai_compatible', supports_vision: false, ceiling_currency: null };

  it('says RUB/day for a NeuralDeep alias and never USD', () => {
    expect(vendorQuotaLabel(nd)).toContain('RUB/day');
    expect(vendorQuotaLabel(nd)).not.toContain('USD');
    expect(vendorQuotaBadge(3000, nd)).toBe('3000 RUB/day per caller');
    expect(vendorQuotaBadge(3000, nd)).not.toContain('USD');
  });

  it('says USD/day for a DeepSeek alias', () => {
    expect(vendorQuotaLabel(ds)).toBe('USD/day');
    expect(vendorQuotaBadge(2.5, ds)).toBe('2.5 USD/day per caller');
  });

  it('names an unrated alias as refused rather than a currency', () => {
    expect(vendorQuotaLabel(unrated)).toContain('unrated');
    expect(vendorQuotaBadge(5, unrated)).toContain('refused');
    expect(vendorQuotaBadge(5, unrated)).not.toMatch(/USD|RUB/);
  });

  it('names no currency where the row did not say one', () => {
    expect(vendorQuotaLabel(undefined)).toBe('per day');
    expect(vendorQuotaLabel({ ...ds, ceiling_currency: undefined })).toBe('per day');
    expect(vendorQuotaBadge(5, undefined)).toBe('5 per day per caller');
  });

  it('refuses a bad quota without naming a fixed currency', () => {
    const block = { ...addServing(emptyBlock(), 'vendor', 'nd'), vendor_quotas: { nd: -1 } };
    const [error] = computeBlockErrors(block);
    expect(error).toContain('in the currency the alias bills in');
    expect(error).not.toContain('USD');
  });

  it('the tab source carries no fixed dollar on the ceiling', () => {
    // Read as source, not rendered: see tabSource() above for why.
    const tab = Object.values(import.meta.glob('./InferenceSharingEditor.svelte', {
      query: '?raw',
      import: 'default',
      eager: true,
    }) as Record<string, string>)[0];
    expect(tab).not.toContain('USD/day');
    expect(tab).not.toContain('USD per day');
  });
});

// Since 2026-09-28 the tariff currency is per served alias (ADR-041 D3,
// amendment): an explicit `compute.tariff_currency.<alias>`, else for a vendor
// alias the currency its provider bills in — the backend's `ceiling_currency`,
// from the same `pricing.vendor_ceiling_currency` — else the node default.
describe('each alias in the Tariff section names its own currency and where it came from', () => {
  const nd: ProviderInfo = { alias: 'qwen 3.8 27b ND', model: 'qwen3.8-27b', type: 'neuraldeep', supports_vision: false, ceiling_currency: 'RUB' };
  const local: ProviderInfo = { alias: 'llama', model: 'qwen3:8b', type: 'llamacpp_server', supports_vision: false };
  const block = (): ComputeRules => ({
    ...emptyBlock(),
    currency: 'USD',
    serving_local: ['llama'],
    serving_vendor: [nd.alias],
    vendor_quotas: { [nd.alias]: 1000 },
    serving_tariff: {
      llama: [{ from: '2026-09-01', in: 0.1, out: 0.3 }],
      [nd.alias]: [{ from: '2026-09-01', in: 20, out: 60 }],
    },
  });

  it('shows RUB for the NeuralDeep alias and USD for a local alias under a USD default', () => {
    expect(tariffCurrencyOf(block(), nd.alias, nd)).toEqual({ currency: 'RUB', source: 'provider' });
    expect(tariffCurrencyOf(block(), 'llama', local)).toEqual({ currency: 'USD', source: 'node' });
    expect(tariffCurrencySourceLabel('provider')).toContain('provider');
    expect(tariffCurrencySourceLabel('node')).toContain('default');
  });

  it('an explicit currency on the alias wins, and clearing it falls back', () => {
    const set = setAliasCurrency(block(), 'llama', 'rub');
    expect(set.tariff_currency).toEqual({ llama: 'RUB' });
    expect(tariffCurrencyOf(set, 'llama', local)).toEqual({ currency: 'RUB', source: 'explicit' });
    const ndEur = setAliasCurrency(block(), nd.alias, 'EUR');
    expect(tariffCurrencyOf(ndEur, nd.alias, nd)).toEqual({ currency: 'EUR', source: 'explicit' });
    const cleared = setAliasCurrency(set, 'llama', '');
    expect(cleared.tariff_currency).toEqual({});
    expect(tariffCurrencyOf(cleared, 'llama', local)).toEqual({ currency: 'USD', source: 'node' });
  });

  it('a vendor alias whose provider names no currency reads the node default, and nothing reads nothing', () => {
    expect(tariffCurrencyOf(block(), nd.alias, { ...nd, ceiling_currency: null })).toEqual({ currency: 'USD', source: 'node' });
    expect(tariffCurrencyOf({ ...block(), currency: null }, 'llama', local)).toEqual({ currency: null, source: null });
    // An alias not in serving_vendor never takes a provider's unit.
    expect(tariffCurrencyOf({ ...block(), serving_vendor: [] }, nd.alias, nd)).toEqual({ currency: 'USD', source: 'node' });
  });

  it('an invalid per-alias code is refused naming the alias, as the backend does', () => {
    const [error] = computeBlockErrors({ ...block(), tariff_currency: { llama: 'XYZ' } });
    expect(error).toContain("'compute.tariff_currency.llama' must be an ISO 4217 code");
  });

  it('the fold keeps the per-alias table and drops its comment keys', () => {
    const folded = foldServingAlias({ ...block(), tariff_currency: { _comment: 'x', llama: 'RUB' } });
    expect(folded.tariff_currency).toEqual({ llama: 'RUB' });
  });

  it('the tab reads each alias through the resolver and labels the node field as the default', () => {
    const tab = Object.values(import.meta.glob('./InferenceSharingEditor.svelte', {
      query: '?raw',
      import: 'default',
      eager: true,
    }) as Record<string, string>)[0];
    expect(tab).toContain('tariffCurrencyOf(');
    expect(tab).toContain('Default currency');
    expect(tab).not.toMatch(/tariffState\([^)]*view\.currency/);
  });
});

// --- A vendor key's request quota (A-VENDOR-KEYS-QUOTA-WINDOWS-ARE-READ-AND-
// NEVER-SHOWN) ---------------------------------------------------------------

describe('formatQuotaWindow', () => {
  const win = (over: Partial<QuotaWindow> = {}): QuotaWindow => ({
    name: '3h', unit: 'requests', used: 14, limit: 400, remaining: 386,
    resets_at: '2026-09-28T11:59:59Z', ...over,
  });

  it('reads coddy-style: label, rounded percent, local reset time', () => {
    const line = formatQuotaWindow(win());
    expect(line).toBe(`3h ${Math.round((14 / 400) * 100)}% (resets ${new Date('2026-09-28T11:59:59Z').toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })})`);
  });

  it('the week window is labelled "this week", not the wire word "iso-week"', () => {
    const line = formatQuotaWindow(win({ name: 'iso-week', used: 14, limit: 2000, remaining: 1986 }));
    expect(line).toContain('this week');
    expect(line).not.toContain('iso-week');
  });

  it('a window with no used/limit renders nothing, never "NaN%"', () => {
    expect(formatQuotaWindow(win({ used: undefined }))).toBe('');
    expect(formatQuotaWindow(win({ limit: undefined }))).toBe('');
    expect(formatQuotaWindow(win({ limit: 0 }))).toBe('');
  });

  it('a bad resets_at drops just the reset clause, not the whole line', () => {
    const line = formatQuotaWindow(win({ resets_at: 'not-a-date' }));
    expect(line).not.toContain('Invalid Date');
    expect(line).not.toContain('resets');
    expect(line).toContain('3h');
  });

  it('the percent is clamped to [0, 100] and rounded', () => {
    expect(quotaWindowPercent({ name: '3h', unit: 'requests', used: 400, limit: 400 })).toBe(100);
    expect(quotaWindowPercent({ name: '3h', unit: 'requests', used: 0, limit: 400 })).toBe(0);
    expect(quotaWindowPercent({ name: '3h', unit: 'requests', used: -5, limit: 400 })).toBe(0);
  });
});

describe('formatQuotaWindowTitle', () => {
  it('carries the counts the compact line elides', () => {
    expect(formatQuotaWindowTitle({ name: '3h', unit: 'requests', used: 14, limit: 400, remaining: 386 }))
      .toBe('386 / 400 requests left');
  });

  it('is empty without remaining/limit', () => {
    expect(formatQuotaWindowTitle({ name: '3h', unit: 'requests' })).toBe('');
  });
});

describe('quotaWindowIsWarning and quotaHasWarning', () => {
  it('is a warning at or past 80% used', () => {
    expect(quotaWindowIsWarning({ name: '3h', unit: 'requests', used: 79, limit: 100 })).toBe(false);
    expect(quotaWindowIsWarning({ name: '3h', unit: 'requests', used: 80, limit: 100 })).toBe(true);
    expect(QUOTA_WARN_PERCENT).toBe(80);
  });

  it('an exhausted window warns even if a rounded percent would read under the threshold', () => {
    expect(quotaWindowIsWarning({ name: '3h', unit: 'requests', used: 3, limit: 3 })).toBe(true);
  });

  it('quotaHasWarning reads any window in the quota, and is false with none', () => {
    const balance: NonNullable<BalanceResult['balance']> = {
      quota: {
        windows: [
          { name: '3h', unit: 'requests', used: 10, limit: 400 },
          { name: 'iso-week', unit: 'requests', used: 1900, limit: 2000 },
        ],
      },
    };
    expect(quotaHasWarning(balance)).toBe(true);
    expect(quotaHasWarning({ quota: { windows: [] } })).toBe(false);
    expect(quotaHasWarning(null)).toBe(false);
  });
});

describe('formatQuotaDuration', () => {
  it('renders 42s, 12m 05s, 2h 10m like coddy\'s formatDuration', () => {
    expect(formatQuotaDuration(42)).toBe('42s');
    expect(formatQuotaDuration(725)).toBe('12m 05s');
    expect(formatQuotaDuration(7800)).toBe('2h 10m');
    expect(formatQuotaDuration(0)).toBe('0s');
    expect(formatQuotaDuration(-5)).toBe('0s');
  });
});

describe('formatQuotaLine', () => {
  const subscriptionBalance = (): NonNullable<BalanceResult['balance']> => ({
    is_available: true,
    billing_mode: 'subscription',
    balance_infos: [{ currency: 'RUB', total_balance: '500.00', spent_30d: '0.00' }],
    quota: {
      tier: 'free', billing_mode: 'subscription', can_request: true, blockers: [],
      parallel_limit: 3,
      windows: [
        { name: '3h', unit: 'requests', used: 14, limit: 400, remaining: 386, resets_at: '2026-09-28T11:59:59Z' },
        { name: 'iso-week', unit: 'requests', used: 14, limit: 2000, remaining: 1986, resets_at: '2026-10-05T00:00:00Z' },
        { name: 'minute', unit: 'requests', used: 0, limit: 20, remaining: 20, resets_at: null },
      ],
    },
  });

  it('a subscription key shows the wallet labelled as not debited', () => {
    const line = formatQuotaLine(subscriptionBalance());
    expect(line).toContain('free tier');
    expect(line).toContain('3h');
    expect(line).toContain('this week');
    expect(line).toContain('3 parallel');
    expect(line).toContain('wallet 500.00 RUB');
    expect(line).toContain('not debited on subscription');
  });

  it('the tooltip carries the counts the compact line elided', () => {
    const title = formatQuotaLineTitle(subscriptionBalance());
    expect(title).toContain('386 / 400 requests left');
    expect(title).toContain('1986 / 2000 requests left');
  });

  it('a wallet (charged) key shows a plain balance, no "not debited" claim', () => {
    const balance = subscriptionBalance();
    balance.billing_mode = 'wallet';
    balance.quota!.billing_mode = 'wallet';
    const line = formatQuotaLine(balance);
    expect(line).toContain('balance 500.00 RUB');
    expect(line).not.toContain('not debited');
  });

  it('no quota block (e.g. DeepSeek) and no balance_infos yields an empty line', () => {
    expect(formatQuotaLine({ is_available: true })).toBe('');
    expect(formatQuotaLineTitle({ is_available: true })).toBe('');
  });

  it('null/undefined balance yields an empty line rather than throwing', () => {
    expect(formatQuotaLine(null)).toBe('');
    expect(formatQuotaLine(undefined)).toBe('');
  });
});

describe('formatQuotaBlockers', () => {
  it('says nothing when the key can request', () => {
    expect(formatQuotaBlockers({ quota: { can_request: true, blockers: [] } })).toBe('');
  });

  it('an unclassified blocker id is shown verbatim, coddy\'s fallback', () => {
    const line = formatQuotaBlockers({ quota: { can_request: false, blockers: ['out_of_quota', 'cooldown'] } });
    expect(line).toBe('blocked: out_of_quota');
  });

  it('a false can_request with no blockers still says the key is blocked', () => {
    expect(formatQuotaBlockers({ quota: { can_request: false, blockers: [] } }))
      .toBe('Cannot request right now.');
  });

  it('no quota block, or can_request omitted, says nothing (never guesses blocked)', () => {
    expect(formatQuotaBlockers({})).toBe('');
    expect(formatQuotaBlockers({ quota: {} })).toBe('');
  });

  it('maps window/rate/key/wallet/account blockers the way coddy\'s blockedSegment does', () => {
    expect(formatQuotaBlockers({ quota: { can_request: false, blockers: ['key_blocked'] } })).toBe('key blocked');
    expect(formatQuotaBlockers({ quota: { can_request: false, blockers: ['wallet_empty'] } })).toBe('wallet empty');
    expect(formatQuotaBlockers({ quota: { can_request: false, blockers: ['user_blocked'] } })).toBe('account blocked');
    expect(formatQuotaBlockers({
      quota: { can_request: false, blockers: ['rpm_exhausted'], retry_after_sec: 42 },
    })).toBe('rate limited (retry in 42s)');
    expect(formatQuotaBlockers({
      quota: {
        can_request: false, blockers: ['session_exhausted'],
        windows: [{ name: '3h', unit: 'requests', resets_at: '2026-09-28T11:59:59Z' }],
      },
    })).toBe(`limit reached (resets ${new Date('2026-09-28T11:59:59Z').toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })})`);
  });

  it('a short window block (under a minute) reads as a rate-limit countdown instead of a reset time', () => {
    const line = formatQuotaBlockers({
      quota: { can_request: false, blockers: ['session_cooldown'], retry_after_sec: 30 },
    });
    expect(line).toBe('rate limited (retry in 30s)');
  });
});

// blocked_models: a model-level gate that leaves can_request true (coddy's
// own scar, external/cli/usage.go:136-138).
describe('blockedModelLine', () => {
  const resetsAt = '2026-09-28T12:00:00Z';
  const localReset = new Date(resetsAt).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  const quotaWith = (model: string): NonNullable<BalanceResult['balance']>['quota'] => ({
    blocked_models: [{ model, blocker: 'model_cap_blocked', resets_at: resetsAt, reset_in_sec: 900 }],
  });

  it('names the alias\'s own model, blocked, with a local reset time — coddy\'s wording', () => {
    expect(blockedModelLine(quotaWith('qwen3.8-27b'), 'qwen3.8-27b'))
      .toBe(`qwen3.8-27b blocked (resets ${localReset})`);
  });

  it('a block on the -noreason twin is shown for the alias configured as the base', () => {
    expect(blockedModelLine(quotaWith('qwen3.8-27b-noreason'), 'qwen3.8-27b'))
      .toBe(`qwen3.8-27b-noreason blocked (resets ${localReset})`);
  });

  it('a block on the base does not match an alias configured as the -noreason twin', () => {
    expect(blockedModelLine(quotaWith('qwen3.8-27b'), 'qwen3.8-27b-noreason')).toBe('');
  });

  it('a block on an unrelated model names nothing for this alias', () => {
    expect(blockedModelLine(quotaWith('gpt-oss-120b'), 'qwen3.8-27b')).toBe('');
  });

  it('the match is case-insensitive', () => {
    expect(blockedModelLine(quotaWith('QWEN3.8-27B'), 'qwen3.8-27b')).toContain('blocked');
  });

  it('no quota, no blocked_models, or no provider model each yield an empty line', () => {
    expect(blockedModelLine(null, 'qwen3.8-27b')).toBe('');
    expect(blockedModelLine({}, 'qwen3.8-27b')).toBe('');
    expect(blockedModelLine(quotaWith('qwen3.8-27b'), null)).toBe('');
    expect(blockedModelLine(quotaWith('qwen3.8-27b'), '')).toBe('');
  });

  it('a missing resets_at drops the reset clause but still names the model blocked', () => {
    const quota: NonNullable<BalanceResult['balance']>['quota'] = {
      blocked_models: [{ model: 'qwen3.8-27b', blocker: 'x' }],
    };
    expect(blockedModelLine(quota, 'qwen3.8-27b')).toBe('qwen3.8-27b blocked');
  });
});

// --- Multi-account balances (2026-09-28: DeepSeek + NeuralDeep in the same UI) ---

describe('accountRowsFromBalances', () => {
  const deepseekResult: BalanceResult = {
    status: 'success',
    alias: 'ds_flash',
    balance: { is_available: true, balance_infos: [{ currency: 'USD', total_balance: '10.53' }] },
  };
  const neuraldeepResult: BalanceResult = {
    status: 'success',
    alias: 'qwen38',
    balance: {
      is_available: true,
      billing_mode: 'subscription',
      balance_infos: [{ currency: 'RUB', total_balance: '500.00' }],
      quota: { tier: 'free', billing_mode: 'subscription', can_request: true, blockers: [] },
    },
  };

  it("uses the backend's own accounts array when present, even with one alias each", () => {
    const accounts: AccountRow[] = [
      { account: 'acct-1', provider_type: 'deepseek', label: 'DeepSeek', aliases: ['ds_flash'], result: deepseekResult },
      { account: 'acct-2', provider_type: 'neuraldeep', label: 'NeuralDeep', aliases: ['qwen38', 'deepseek-v4-pro'], result: neuraldeepResult },
    ];
    expect(accountRowsFromBalances({ status: 'success', accounts })).toBe(accounts);
  });

  it('an empty accounts array is a real answer, not a signal to fall back to balances', () => {
    const rows = accountRowsFromBalances({ status: 'success', balances: { ds_flash: deepseekResult }, accounts: [] });
    expect(rows).toEqual([]);
  });

  it('falls back to one row per alias when the backend sent no accounts (older backend)', () => {
    const rows = accountRowsFromBalances({
      status: 'success',
      balances: { ds_flash: deepseekResult, qwen38: neuraldeepResult },
    });
    expect(rows).toEqual([
      { account: 'ds_flash', label: 'ds_flash', aliases: ['ds_flash'], result: deepseekResult },
      { account: 'qwen38', label: 'qwen38', aliases: ['qwen38'], result: neuraldeepResult },
    ]);
  });

  it('no balances and no accounts yields no rows', () => {
    expect(accountRowsFromBalances(null)).toEqual([]);
    expect(accountRowsFromBalances({ status: 'error', message: 'x' })).toEqual([]);
  });
});

describe('providerTypeLabel', () => {
  it('names the two providers this request is about', () => {
    expect(providerTypeLabel('deepseek')).toBe('DeepSeek');
    expect(providerTypeLabel('neuraldeep')).toBe('NeuralDeep');
  });

  it('title-cases a type this build has no dedicated word for, rather than dropping it', () => {
    expect(providerTypeLabel('some_new_vendor')).toBe('Some_new_vendor');
  });

  it('an absent type reads as a generic word, never blank', () => {
    expect(providerTypeLabel(null)).toBe('Provider');
    expect(providerTypeLabel(undefined)).toBe('Provider');
  });
});

describe('isSubscriptionAccount', () => {
  it('reads billing_mode off the quota first, then the balance', () => {
    expect(isSubscriptionAccount({ quota: { billing_mode: 'subscription' } })).toBe(true);
    expect(isSubscriptionAccount({ billing_mode: 'subscription' })).toBe(true);
    expect(isSubscriptionAccount({ billing_mode: 'wallet', quota: { billing_mode: 'wallet' } })).toBe(false);
    expect(isSubscriptionAccount(null)).toBe(false);
  });
});

describe('walletLevel — per-currency thresholds', () => {
  it('USD keeps the existing $3 / $1 thresholds', () => {
    expect(walletLevel('USD', 10.53, true)).toBe('ok');
    expect(walletLevel('USD', 2.99, true)).toBe('low');
    expect(walletLevel('USD', 0.99, true)).toBe('critical');
  });

  it("RUB uses its own two-orders-of-magnitude thresholds (Mike's call, 2026-09-28)", () => {
    expect(BALANCE_LEVEL_THRESHOLDS.RUB).toEqual({ low: 300, critical: 100 });
    expect(walletLevel('RUB', 500.0, true)).toBe('ok');
    expect(walletLevel('RUB', 299.99, true)).toBe('low');
    expect(walletLevel('RUB', 99.99, true)).toBe('critical');
  });

  it('an unknown currency has no threshold to judge by: neutral, not a guessed color', () => {
    expect(walletLevel('EUR', 0.01, true)).toBe('neutral');
    expect(walletLevel(null, 10, true)).toBe('neutral');
  });

  it('is_available: false is always critical, whatever the number says', () => {
    expect(walletLevel('USD', 999, false)).toBe('critical');
  });

  it('an unparseable amount is neutral, not a false "ok"', () => {
    expect(walletLevel('USD', NaN, true)).toBe('neutral');
  });

  it('is exact at the boundary — critical is strictly less-than, not less-or-equal', () => {
    expect(walletLevel('USD', 1, true)).toBe('low');
    expect(walletLevel('USD', 3, true)).toBe('ok');
  });
});

describe('quotaLevel — subscription wallets are judged by the quota, not the wallet', () => {
  it('no quota at all is neutral', () => {
    expect(quotaLevel(null)).toBe('neutral');
  });

  it('can_request: false is critical', () => {
    expect(quotaLevel({ can_request: false })).toBe('critical');
  });

  it('a named blocker is critical even when can_request was not stated', () => {
    expect(quotaLevel({ blockers: ['wallet_empty'] })).toBe('critical');
  });

  it('a window at or past the warning threshold is low', () => {
    const quota: ProviderQuota = {
      can_request: true, blockers: [],
      windows: [{ name: '3h', unit: 'requests', used: 320, limit: 400 }],
    };
    expect(quotaWindowIsWarning(quota.windows![0])).toBe(true);
    expect(quotaLevel(quota)).toBe('low');
  });

  it('plenty of headroom is neutral, not a green "ok" this UI has no grounds to claim', () => {
    const quota: ProviderQuota = {
      can_request: true, blockers: [],
      windows: [{ name: '3h', unit: 'requests', used: 14, limit: 400 }],
    };
    expect(quotaLevel(quota)).toBe('neutral');
  });
});

describe('accountLevel — the level a Sidebar/ProvidersEditor row is coloured by', () => {
  it('a subscription account is judged by its quota, ignoring the wallet number entirely', () => {
    const balance: NonNullable<BalanceResult['balance']> = {
      is_available: true,
      billing_mode: 'subscription',
      balance_infos: [{ currency: 'RUB', total_balance: '0.00' }],
      quota: { billing_mode: 'subscription', can_request: true, blockers: [], windows: [] },
    };
    expect(accountLevel(balance)).toBe('neutral');
  });

  it('a wallet (pay-per-use) account is judged by its balance and currency', () => {
    const balance: NonNullable<BalanceResult['balance']> = {
      is_available: true,
      balance_infos: [{ currency: 'USD', total_balance: '10.53' }],
    };
    expect(accountLevel(balance)).toBe('ok');
  });

  it('no balance at all is neutral (not yet checked, or an error shown separately)', () => {
    expect(accountLevel(null)).toBe('neutral');
    expect(accountLevel(undefined)).toBe('neutral');
  });
});

describe('balanceErrorText — coddy wording for a provider-side failure', () => {
  it('translates each named balance.error into its sentence', () => {
    expect(balanceErrorText({ balance: { error: 'key_rejected' } })).toBe('key rejected — update the API key');
    expect(balanceErrorText({ balance: { error: 'key_blocked' } })).toBe('key blocked');
    expect(balanceErrorText({ balance: { error: 'unavailable' } })).toBe('unavailable, retry later');
    expect(balanceErrorText({ balance: { error: 'invalid' } })).toBe('invalid response from the vendor');
  });

  it('an unrecognised balance.error still shows something, with its detail if given', () => {
    expect(balanceErrorText({ balance: { error: 'weird_code' } })).toBe('error: weird_code');
    expect(balanceErrorText({ balance: { error: 'weird_code', error_detail: 'vendor said huh' } }))
      .toBe('vendor said huh');
  });

  it('falls back to a top-level status: "error" when balance.error is absent', () => {
    expect(balanceErrorText({ status: 'error', message: 'timed out' })).toBe('timed out');
    expect(balanceErrorText({ status: 'error' })).toBe('unavailable, retry later');
  });

  it('a clean success result has no error text', () => {
    expect(balanceErrorText({ status: 'success', balance: { is_available: true } })).toBe('');
    expect(balanceErrorText(null)).toBe('');
  });
});

// --- The rpm (per-minute) window renders as counts, not a stale percent ---

describe('formatQuotaWindow — the rpm window (Do §6)', () => {
  it('renders "rpm used/limit", not a percent, for the minute window', () => {
    const win: QuotaWindow = { name: 'minute', unit: 'requests', used: 0, limit: 20, remaining: 20 };
    expect(formatQuotaWindow(win)).toBe('rpm 0/20');
  });

  it('carries a reset clause when the vendor sent one, same as any other window', () => {
    const win: QuotaWindow = {
      name: 'minute', unit: 'requests', used: 14, limit: 20, resets_at: '2026-09-28T11:59:59Z',
    };
    expect(formatQuotaWindow(win)).toBe(
      `rpm 14/20 (resets ${localResetTime('2026-09-28T11:59:59Z')})`,
    );
  });

  it('a non-minute window is unaffected: still a percent', () => {
    const win: QuotaWindow = { name: '3h', unit: 'requests', used: 14, limit: 400 };
    expect(formatQuotaWindow(win)).toBe('3h 4%');
  });

  it('classifies by window name, not by unit', () => {
    const win: QuotaWindow = { name: 'minute', unit: 'tokens', used: 5, limit: 100 };
    expect(formatQuotaWindow(win)).toBe('rpm 5/100');
  });
});

describe("localResetTime — the one formatter both editors' quota lines share", () => {
  it("formats an ISO timestamp in the reader's own clock, hour:minute", () => {
    const expected = new Date('2026-09-28T11:59:59Z').toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
    expect(localResetTime('2026-09-28T11:59:59Z')).toBe(expected);
  });

  it('an absent or unparseable timestamp is empty, not "Invalid Date"', () => {
    expect(localResetTime(null)).toBe('');
    expect(localResetTime(undefined)).toBe('');
    expect(localResetTime('not-a-date')).toBe('');
  });
});
