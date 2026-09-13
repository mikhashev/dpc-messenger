import { describe, it, expect } from 'vitest';
import type { ProviderInfo } from '$lib/types';
import {
  addAllowed,
  addServing,
  addTariffEntry,
  classifyProviderType,
  computeBlockErrors,
  computeErrorsOf,
  foldServingAlias,
  isFree,
  knownGroups,
  knownNodes,
  offeredProviders,
  removeAllowed,
  removeServing,
  removeTariffEntry,
  setCurrency,
  setFree,
  setVendorQuota,
  splitCallerIds,
  tariffAliases,
  tariffEntryErrors,
  tariffHistory,
  tariffState,
  type ComputeRules,
} from './computeSharing';

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
