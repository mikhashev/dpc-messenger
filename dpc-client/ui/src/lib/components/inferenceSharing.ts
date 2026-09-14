// inferenceSharing.ts — the pure half of the Inference Sharing tab.
//
// Everything here mirrors a rule the backend already enforces in
// dpc_client_core/firewall.py, so the tab emits exactly the `compute` block
// the validator accepts and refuses on the client what the server would
// refuse anyway — with the same words where practical. The backend stays the
// authority: a save still goes through `save_firewall_rules`, and its reasons
// are shown beside these. No DOM in this file; it is what the tests exercise.

import type { ProviderInfo } from '$lib/types';
import type { MenuRow } from './peerMenu';

// --- The block as written to privacy_rules.json --------------------------

/** One dated line of an alias's tariff (firewall.py `TariffEntry`, :145-150):
 *  rates per 1M tokens, in the node currency, from `from` (YYYY-MM-DD) on. */
export interface TariffEntry {
  from: string;
  in: number;
  out: number;
}

/** `compute` as firewall.py reads it (`_parse_compute_settings`, :231-305).
 *  Keys the tab never touches (`_comment`, the `_…` explanations) travel
 *  through untouched. */
export interface ComputeRules {
  _comment?: string;
  enabled: boolean;
  allow_nodes: string[];
  allow_groups: string[];
  /** A filter on what a caller may ask for; **empty accepts every model**
   *  (firewall.py :1928-1938, :2016-2020), the opposite of the serving lists. */
  allowed_models: string[];
  /** Deprecated single form; folded into `serving_local` (firewall.py :248-256). */
  serving_alias?: string | null;
  serving_local?: string[] | null;
  serving_vendor?: string[] | null;
  /** alias -> USD per day per caller; required for every vendor alias (:357-362). */
  vendor_quotas?: Record<string, number> | null;
  /** ISO 4217 code or null = no tariff declared (:394-399). */
  currency?: string | null;
  serving_tariff?: Record<string, TariffEntry[]> | null;
  free_nodes?: string[] | null;
  free_groups?: string[] | null;
  [key: string]: unknown;
}

// --- Provider types and their place (firewall.py :88-95) -----------------

/** A local type spends the card. firewall.py's LOCAL_PROVIDER_TYPES also
 *  holds `local_whisper`; the board (2026-09-10) and the tab before this one
 *  keep Whisper out of inference sharing — it is reached through Transcription
 *  Sharing, which has a gate of its own — so it is classified apart. */
export const LOCAL_SERVING_TYPES: ReadonlySet<string> = new Set(['ollama', 'llamacpp_server']);
/** A vendor type spends money and needs a ceiling (VENDOR_PROVIDER_TYPES). */
export const VENDOR_SERVING_TYPES: ReadonlySet<string> = new Set([
  'openai_compatible', 'anthropic', 'zai', 'deepseek', 'gemini', 'github_models', 'gigachat',
]);
/** Somebody else's model, shared onward by nobody (UNSERVABLE_PROVIDER_TYPES, ADR-041 D7). */
export const NEVER_OFFERED_TYPES: ReadonlySet<string> = new Set(['dpc_agent', 'remote_peer']);
export const TRANSCRIPTION_TYPES: ReadonlySet<string> = new Set(['local_whisper']);

export type ServingClass = 'local' | 'vendor' | 'never' | 'transcription' | 'unknown';

export function classifyProviderType(type: string | undefined | null): ServingClass {
  if (!type) return 'unknown';
  if (NEVER_OFFERED_TYPES.has(type)) return 'never';
  if (LOCAL_SERVING_TYPES.has(type)) return 'local';
  if (VENDOR_SERVING_TYPES.has(type)) return 'vendor';
  if (TRANSCRIPTION_TYPES.has(type)) return 'transcription';
  return 'unknown';
}

/** The registry split into what each list may be offered. Anything that is
 *  not local or vendor is offered nowhere: `never` by D7, `transcription`
 *  by the tab's own rule, `unknown` because firewall.py's classifier refuses
 *  a type it does not know rather than guessing (:518-521). */
export function offeredProviders(providers: readonly ProviderInfo[] | null | undefined): {
  local: ProviderInfo[];
  vendor: ProviderInfo[];
} {
  const local: ProviderInfo[] = [];
  const vendor: ProviderInfo[] = [];
  for (const entry of providers ?? []) {
    const kind = classifyProviderType(entry.type);
    if (kind === 'local') local.push(entry);
    else if (kind === 'vendor') vendor.push(entry);
  }
  return { local, vendor };
}

// --- Normalising a block read from disk ----------------------------------

function cleanList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string' && v.length > 0) : [];
}

/** The block as the tab edits it: every list present, and the deprecated
 *  `serving_alias` folded into `serving_local` the way the load path folds
 *  it (firewall.py :248-256 — only when `serving_local` is absent), then
 *  dropped, which is what the validator's own message asks for when the two
 *  disagree (:370-377: «put the alias first in the list and drop
 *  serving_alias»). Returns a new object; the input is not touched. */
export function foldServingAlias(compute: ComputeRules): ComputeRules {
  const legacy = typeof compute.serving_alias === 'string' && compute.serving_alias.length > 0
    ? compute.serving_alias : null;
  const hadLocal = compute.serving_local !== undefined && compute.serving_local !== null;
  const local = hadLocal ? cleanList(compute.serving_local) : (legacy ? [legacy] : []);
  const quotas: Record<string, number> = {};
  for (const [alias, quota] of Object.entries(compute.vendor_quotas ?? {})) {
    if (!alias.startsWith('_') && typeof quota === 'number') quotas[alias] = quota;
  }
  const tariff: Record<string, TariffEntry[]> = {};
  for (const [alias, entries] of Object.entries(compute.serving_tariff ?? {})) {
    if (alias.startsWith('_') || !Array.isArray(entries)) continue;
    tariff[alias] = entries.map((e) => ({ from: e.from, in: e.in, out: e.out }));
  }
  return {
    ...compute,
    enabled: !!compute.enabled,
    allow_nodes: cleanList(compute.allow_nodes),
    allow_groups: cleanList(compute.allow_groups),
    allowed_models: cleanList(compute.allowed_models),
    serving_alias: null,
    serving_local: local,
    serving_vendor: cleanList(compute.serving_vendor),
    vendor_quotas: quotas,
    currency: typeof compute.currency === 'string' && compute.currency.length > 0 ? compute.currency : null,
    serving_tariff: tariff,
    free_nodes: cleanList(compute.free_nodes),
    free_groups: cleanList(compute.free_groups),
  };
}

// --- Who may call, and who calls for free --------------------------------

export type CallerKind = 'nodes' | 'groups';

function allowKey(kind: CallerKind): 'allow_nodes' | 'allow_groups' {
  return kind === 'nodes' ? 'allow_nodes' : 'allow_groups';
}
function freeKey(kind: CallerKind): 'free_nodes' | 'free_groups' {
  return kind === 'nodes' ? 'free_nodes' : 'free_groups';
}

/** What a pasted list of callers splits on: whitespace of any kind (a
 *  newline-per-id paste included), commas and semicolons. */
const CALLER_SEPARATORS = /[\s,;]+/;

/** The ids in a typed or pasted text, in order, each once, none empty. The
 *  old textarea took one id per line; the chip input takes the same paste,
 *  and a comma- or semicolon-separated one besides. No format check: the
 *  validator accepts any string in `compute.allow_nodes` / `allow_groups`
 *  (firewall.py :2177-2181 — a list, nothing more), unlike `node_groups`,
 *  `nodes` and `file_transfer.allow_nodes`, which do require the `dpc-node-`
 *  prefix (:2154, :2224, :2297). Mirroring means not inventing one here. */
export function splitCallerIds(text: string): string[] {
  const out: string[] = [];
  for (const part of text.split(CALLER_SEPARATORS)) {
    const id = part.trim();
    if (id && !out.includes(id)) out.push(id);
  }
  return out;
}

/** Adds every id in `text` (see `splitCallerIds`) that the list does not
 *  already hold, in paste order. Returns the same object when nothing joins. */
export function addAllowed(compute: ComputeRules, kind: CallerKind, text: string): ComputeRules {
  const key = allowKey(kind);
  const current = cleanList(compute[key]);
  const joining = splitCallerIds(text).filter((id) => !current.includes(id));
  if (joining.length === 0) return compute;
  return { ...compute, [key]: [...current, ...joining] };
}

/** Removing a caller from the allow list removes it from the free list too,
 *  so `free ⊆ allow` (firewall.py :435-448) holds by construction and the
 *  save is never refused for a straggler. */
export function removeAllowed(compute: ComputeRules, kind: CallerKind, id: string): ComputeRules {
  return {
    ...compute,
    [allowKey(kind)]: cleanList(compute[allowKey(kind)]).filter((v) => v !== id),
    [freeKey(kind)]: cleanList(compute[freeKey(kind)]).filter((v) => v !== id),
  };
}

/** Free is a mark on an allowed row, never an admission: an id not in the
 *  allow list cannot be made free. */
export function setFree(compute: ComputeRules, kind: CallerKind, id: string, free: boolean): ComputeRules {
  const allowed = cleanList(compute[allowKey(kind)]);
  const current = cleanList(compute[freeKey(kind)]);
  if (!allowed.includes(id)) return compute;
  const next = free ? (current.includes(id) ? current : [...current, id]) : current.filter((v) => v !== id);
  return { ...compute, [freeKey(kind)]: next };
}

export function isFree(compute: ComputeRules, kind: CallerKind, id: string): boolean {
  return cleanList(compute[freeKey(kind)]).includes(id);
}

// --- What I share ----------------------------------------------------------

export type ServingList = 'local' | 'vendor';

function servingKey(list: ServingList): 'serving_local' | 'serving_vendor' {
  return list === 'local' ? 'serving_local' : 'serving_vendor';
}

export function addServing(compute: ComputeRules, list: ServingList, alias: string): ComputeRules {
  const key = servingKey(list);
  const current = cleanList(compute[key]);
  if (!alias || current.includes(alias)) return compute;
  return { ...compute, [key]: [...current, alias] };
}

/** A vendor alias leaves with its ceiling: the quota was the alias's, and a
 *  ceiling with nothing under it would be read back as a half-configured
 *  alias later. Tariff entries stay — the load path only warns about a priced
 *  alias that is not served (:287-294), and a price is a declaration, not a
 *  setting to lose with a click. */
export function removeServing(compute: ComputeRules, list: ServingList, alias: string): ComputeRules {
  const key = servingKey(list);
  const next: ComputeRules = { ...compute, [key]: cleanList(compute[key]).filter((v) => v !== alias) };
  if (list === 'vendor') {
    const quotas = { ...(compute.vendor_quotas ?? {}) };
    delete quotas[alias];
    next.vendor_quotas = quotas;
  }
  return next;
}

/** `null` removes the ceiling (the save is then refused with the reason). */
export function setVendorQuota(compute: ComputeRules, alias: string, usdPerDay: number | null): ComputeRules {
  const quotas = { ...(compute.vendor_quotas ?? {}) };
  if (usdPerDay === null || Number.isNaN(usdPerDay)) delete quotas[alias];
  else quotas[alias] = usdPerDay;
  return { ...compute, vendor_quotas: quotas };
}

// --- Which models the door accepts -----------------------------------------

/** A model joins `compute.allowed_models`, the door's model filter. The list
 *  is read at firewall.py :256 and applied in `can_request_inference`
 *  (:1928-1938) and `get_available_models_for_peer` (:2016-2020) — both skip
 *  the check when the list is empty, so **an empty list accepts every model**,
 *  the opposite of the serving lists, where empty serves nobody. The validator
 *  asks only that it be a list (:2216), so a free-typed id is as good as a
 *  picked one: a peer may name a model this node has not configured yet, and
 *  the fossils of a provider since removed stay listed until someone removes
 *  them. Pasting follows `splitCallerIds` — the same paste rule the caller
 *  lists take. */
export function addAllowedModel(compute: ComputeRules, text: string): ComputeRules {
  const current = cleanList(compute.allowed_models);
  const joining = splitCallerIds(text).filter((model) => !current.includes(model));
  if (joining.length === 0) return compute;
  return { ...compute, allowed_models: [...current, ...joining] };
}

/** Removing the last named model widens the door rather than closing it —
 *  empty accepts every model. Returns the same object when nothing leaves. */
export function removeAllowedModel(compute: ComputeRules, model: string): ComputeRules {
  const current = cleanList(compute.allowed_models);
  if (!current.includes(model)) return compute;
  return { ...compute, allowed_models: current.filter((m) => m !== model) };
}

/** The listed models that equal no configured provider's `model`, in list
 *  order: a name kept from a provider since removed (the Ollama-era entries
 *  found on the Linux node, 2026-09-14) filters callers down to a model this
 *  node cannot answer with. Not an error — the backend accepts any string —
 *  so the tab badges it and leaves the removing to the owner. */
export function unmatchedModels(
  compute: ComputeRules | null | undefined,
  providers: readonly ProviderInfo[] | null | undefined,
): string[] {
  const configured = new Set<string>();
  for (const entry of providers ?? []) {
    if (typeof entry?.model === 'string' && entry.model.length > 0) configured.add(entry.model);
  }
  return cleanList(compute?.allowed_models).filter((model) => !configured.has(model));
}

/** Every alias with a place in the tariff table, each once: served ones
 *  first, in list order, then aliases priced but served nowhere (kept
 *  visible, so a price does not vanish because its alias was unlisted). An
 *  alias sitting in both serving lists is a refusal the pre-check names; it
 *  still gets one row, not two, because the rows are keyed by alias. */
export function tariffAliases(compute: ComputeRules): Array<{ alias: string; served: boolean }> {
  const seen = new Set<string>();
  const out: Array<{ alias: string; served: boolean }> = [];
  for (const alias of [...cleanList(compute.serving_local), ...cleanList(compute.serving_vendor)]) {
    if (!seen.has(alias)) { seen.add(alias); out.push({ alias, served: true }); }
  }
  for (const alias of Object.keys(compute.serving_tariff ?? {})) {
    if (!alias.startsWith('_') && !seen.has(alias)) { seen.add(alias); out.push({ alias, served: false }); }
  }
  return out;
}

// --- Tariff ----------------------------------------------------------------

export type TariffState =
  | { kind: 'gift'; reason: 'no-currency' | 'no-entry' | 'not-yet'; upcoming: TariffEntry[] }
  | { kind: 'free'; entry: TariffEntry; upcoming: TariffEntry[] }
  | { kind: 'paid'; entry: TariffEntry; upcoming: TariffEntry[] };

/** Newest first — the order the history is read in. */
export function tariffHistory(entries: readonly TariffEntry[] | null | undefined): TariffEntry[] {
  return [...(entries ?? [])].sort((a, b) => (a.from < b.from ? 1 : a.from > b.from ? -1 : 0));
}

/** The three states an alias's tariff can be in on `today` (a UTC
 *  YYYY-MM-DD), mirroring `tariff_for` (firewall.py :459-484): no currency or
 *  no entry on or before today is «not declared» — a gift; the newest
 *  applicable entry at 0/0 is free by decision; anything else is paid. */
export function tariffState(
  entries: readonly TariffEntry[] | null | undefined,
  currency: string | null | undefined,
  today: string,
): TariffState {
  const history = tariffHistory(entries);
  const upcoming = history.filter((e) => e.from > today);
  if (!currency) return { kind: 'gift', reason: 'no-currency', upcoming };
  if (history.length === 0) return { kind: 'gift', reason: 'no-entry', upcoming };
  const entry = history.find((e) => e.from <= today);
  if (!entry) return { kind: 'gift', reason: 'not-yet', upcoming };
  if (entry.in === 0 && entry.out === 0) return { kind: 'free', entry, upcoming };
  return { kind: 'paid', entry, upcoming };
}

/** Today as the backend sees it: the UTC calendar day (firewall.py :470-474). */
export function utcToday(now: Date = new Date()): string {
  return now.toISOString().slice(0, 10);
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

export function isIsoDate(text: unknown): text is string {
  if (typeof text !== 'string' || !ISO_DATE.test(text)) return false;
  const parsed = new Date(text + 'T00:00:00Z');
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === text;
}

function isRate(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0;
}

/** Why a new dated entry cannot join `existing`, in the validator's words
 *  (firewall.py `_tariff_errors`, :383-449), or nothing. */
export function tariffEntryErrors(existing: readonly TariffEntry[] | null | undefined, entry: TariffEntry): string[] {
  const errors: string[] = [];
  if (!isIsoDate(entry.from)) errors.push(`'from' must be an ISO date YYYY-MM-DD, got ${JSON.stringify(entry.from)}`);
  else if ((existing ?? []).some((e) => e.from === entry.from)) {
    errors.push(`there is already an entry from ${entry.from}; one day has one rate`);
  }
  for (const field of ['in', 'out'] as const) {
    if (!isRate(entry[field])) errors.push(`'${field}' must be a non-negative number per 1M tokens`);
  }
  return errors;
}

/** Appends; never rewrites an older line. The caller checks
 *  `tariffEntryErrors` first — this function only refuses a duplicate day. */
export function addTariffEntry(compute: ComputeRules, alias: string, entry: TariffEntry): ComputeRules {
  const tariff = { ...(compute.serving_tariff ?? {}) };
  const current = tariff[alias] ?? [];
  if (current.some((e) => e.from === entry.from)) return compute;
  tariff[alias] = [...current, { from: entry.from, in: entry.in, out: entry.out }];
  return { ...compute, serving_tariff: tariff };
}

/** Only an entry added in this edit session may be taken back; that is what
 *  `isNew` decides, from the component's own memory of what it appended. */
export function removeTariffEntry(compute: ComputeRules, alias: string, from: string): ComputeRules {
  const tariff = { ...(compute.serving_tariff ?? {}) };
  tariff[alias] = (tariff[alias] ?? []).filter((e) => e.from !== from);
  if (tariff[alias].length === 0) delete tariff[alias];
  return { ...compute, serving_tariff: tariff };
}

/** '' or null clears the currency — no tariff declared. */
export function setCurrency(compute: ComputeRules, code: string | null): ComputeRules {
  const trimmed = (code ?? '').trim().toUpperCase();
  return { ...compute, currency: trimmed.length > 0 ? trimmed : null };
}

// ISO 4217 List One as firewall.py carries it (ISO_4217_CODES, :106-128 —
// SIX list-one.xml Pblshd 2026-01-01, 178 codes minus XXX and XTS). Copied
// rather than fetched so the picker and the validator agree offline.
export const ISO_4217_CODES: readonly string[] = [
  'AED', 'AFN', 'ALL', 'AMD', 'AOA', 'ARS', 'AUD', 'AWG', 'AZN', 'BAM', 'BBD', 'BDT',
  'BHD', 'BIF', 'BMD', 'BND', 'BOB', 'BOV', 'BRL', 'BSD', 'BTN', 'BWP', 'BYN', 'BZD',
  'CAD', 'CDF', 'CHE', 'CHF', 'CHW', 'CLF', 'CLP', 'CNY', 'COP', 'COU', 'CRC', 'CUP',
  'CVE', 'CZK', 'DJF', 'DKK', 'DOP', 'DZD', 'EGP', 'ERN', 'ETB', 'EUR', 'FJD', 'FKP',
  'GBP', 'GEL', 'GHS', 'GIP', 'GMD', 'GNF', 'GTQ', 'GYD', 'HKD', 'HNL', 'HTG', 'HUF',
  'IDR', 'ILS', 'INR', 'IQD', 'IRR', 'ISK', 'JMD', 'JOD', 'JPY', 'KES', 'KGS', 'KHR',
  'KMF', 'KPW', 'KRW', 'KWD', 'KYD', 'KZT', 'LAK', 'LBP', 'LKR', 'LRD', 'LSL', 'LYD',
  'MAD', 'MDL', 'MGA', 'MKD', 'MMK', 'MNT', 'MOP', 'MRU', 'MUR', 'MVR', 'MWK', 'MXN',
  'MXV', 'MYR', 'MZN', 'NAD', 'NGN', 'NIO', 'NOK', 'NPR', 'NZD', 'OMR', 'PAB', 'PEN',
  'PGK', 'PHP', 'PKR', 'PLN', 'PYG', 'QAR', 'RON', 'RSD', 'RUB', 'RWF', 'SAR', 'SBD',
  'SCR', 'SDG', 'SEK', 'SGD', 'SHP', 'SLE', 'SOS', 'SRD', 'SSP', 'STN', 'SVC', 'SYP',
  'SZL', 'THB', 'TJS', 'TMT', 'TND', 'TOP', 'TRY', 'TTD', 'TWD', 'TZS', 'UAH', 'UGX',
  'USD', 'USN', 'UYI', 'UYU', 'UYW', 'UZS', 'VED', 'VES', 'VND', 'VUV', 'WST', 'XAD',
  'XAF', 'XAG', 'XAU', 'XBA', 'XBB', 'XBC', 'XBD', 'XCD', 'XCG', 'XDR', 'XOF', 'XPD',
  'XPF', 'XPT', 'XSU', 'XUA', 'YER', 'ZAR', 'ZMW', 'ZWG',
];
const ISO_4217_SET: ReadonlySet<string> = new Set(ISO_4217_CODES);

export function isIso4217(code: unknown): code is string {
  return typeof code === 'string' && ISO_4217_SET.has(code);
}

// --- What the validator would say ------------------------------------------

/** The reasons `save_firewall_rules` would refuse this block, said before
 *  the round trip. A subset of `_compute_list_errors` + `_tariff_errors`
 *  (firewall.py :317-449): the rules that need no provider registry. The
 *  backend's own answer is still shown when it arrives — this list only
 *  saves a trip for the obvious cases. */
export function computeBlockErrors(compute: ComputeRules): string[] {
  const errors: string[] = [];
  const local = cleanList(compute.serving_local);
  const vendor = cleanList(compute.serving_vendor);
  const quotas = compute.vendor_quotas ?? {};

  for (const [alias, quota] of Object.entries(quotas)) {
    if (alias.startsWith('_')) continue;
    if (typeof quota !== 'number' || !Number.isFinite(quota) || quota < 0) {
      errors.push(`'compute.vendor_quotas.${alias}' must be a non-negative number of USD per day`);
    }
  }
  for (const alias of vendor) {
    if (!(alias in quotas) || typeof quotas[alias] !== 'number') {
      errors.push(
        `'${alias}' is in compute.serving_vendor with no ceiling in compute.vendor_quotas: ` +
        'a vendor alias spends money and is refused without one (ADR-041 D5)',
      );
    }
  }
  for (const alias of local) {
    if (vendor.includes(alias)) {
      errors.push(`'${alias}' is in both compute.serving_local and compute.serving_vendor; an alias is bounded by the card or by money, not both`);
    }
  }

  const currency = compute.currency ?? null;
  if (currency !== null && !isIso4217(currency)) {
    errors.push(
      `'compute.currency' must be an ISO 4217 code — three upper-case letters from the standard's list, ` +
      `such as 'USD' or 'RUB' — got ${JSON.stringify(currency)}`,
    );
  }
  for (const [alias, entries] of Object.entries(compute.serving_tariff ?? {})) {
    if (alias.startsWith('_')) continue;
    const seen = new Set<string>();
    (entries ?? []).forEach((entry, index) => {
      const where = `compute.serving_tariff.${alias}[${index}]`;
      if (!isIsoDate(entry.from)) errors.push(`'${where}.from' must be an ISO date YYYY-MM-DD, got ${JSON.stringify(entry.from)}`);
      else if (seen.has(entry.from)) errors.push(`'compute.serving_tariff.${alias}' has two entries from ${entry.from}; one day has one rate`);
      else seen.add(entry.from);
      for (const field of ['in', 'out'] as const) {
        if (!isRate(entry[field])) errors.push(`'${where}.${field}' must be a non-negative number per 1M tokens`);
      }
    });
  }

  for (const kind of ['nodes', 'groups'] as const) {
    const allowed = cleanList(compute[allowKey(kind)]);
    for (const id of cleanList(compute[freeKey(kind)])) {
      if (!allowed.includes(id)) {
        errors.push(`'${id}' is in compute.${freeKey(kind)} but not in compute.${allowKey(kind)}: a free list distinguishes among the peers already allowed, it does not admit (ADR-041 D3)`);
      }
    }
  }
  return errors;
}

/** The lines of a save refusal that concern this tab. */
export function computeErrorsOf(errors: readonly string[] | null | undefined): string[] {
  return (errors ?? []).filter((line) => /\bcompute\b/.test(line));
}

// --- Who the tab can offer as a caller --------------------------------------

export interface KnownNode {
  node_id: string;
  label: string;
  connected: boolean;
}

/** Every node id the application already holds a name for: the peers in the
 *  status payload, the members of the Node Groups tab, the nodes with a
 *  per-node rule, and whatever the block already allows (so an id the
 *  registry has forgotten still renders as itself). Sorted, connected first. */
export function knownNodes(
  peerInfo: ReadonlyArray<{ node_id: string; display_name?: string; name?: string; is_connected?: boolean }> | null | undefined,
  nodeGroups: Record<string, unknown> | null | undefined,
  nodeRuleIds: readonly string[] | null | undefined,
  compute: ComputeRules | null | undefined,
): KnownNode[] {
  const byId = new Map<string, KnownNode>();
  const put = (node_id: string, label?: string, connected = false) => {
    if (!node_id || node_id.startsWith('_')) return;
    const existing = byId.get(node_id);
    if (existing) {
      if (label && existing.label === node_id) existing.label = label;
      existing.connected = existing.connected || connected;
    } else {
      byId.set(node_id, { node_id, label: label || node_id, connected });
    }
  };
  for (const peer of peerInfo ?? []) put(peer.node_id, peer.display_name || peer.name, !!peer.is_connected);
  for (const [group, members] of Object.entries(nodeGroups ?? {})) {
    if (group.startsWith('_') || !Array.isArray(members)) continue;
    for (const id of members) if (typeof id === 'string') put(id);
  }
  for (const id of nodeRuleIds ?? []) put(id);
  for (const id of cleanList(compute?.allow_nodes)) put(id);
  return [...byId.values()].sort((a, b) =>
    a.connected !== b.connected ? (a.connected ? -1 : 1) : a.label.localeCompare(b.label));
}

export function knownGroups(nodeGroups: Record<string, unknown> | null | undefined, compute: ComputeRules | null | undefined): string[] {
  const names = new Set<string>();
  for (const name of Object.keys(nodeGroups ?? {})) if (!name.startsWith('_')) names.add(name);
  for (const name of cleanList(compute?.allow_groups)) names.add(name);
  return [...names].sort((a, b) => a.localeCompare(b));
}

// --- (5) The IDE door -------------------------------------------------------

/** `get_gateway_state`. Every field optional and read fail-closed: a state
 *  that arrived without one is not guessed at. */
export interface GatewayState {
  /** `[gateway] enabled` in config.ini, which only a restart re-reads. */
  enabled?: boolean;
  /** Whether a listener holds the port right now. */
  running?: boolean;
  port?: number | null;
  bind?: string | null;
  /** `sk-…abcd`, or null where no key has been written yet. */
  key_masked?: string | null;
  key_file?: string | null;
  serving_local?: string[] | null;
  serving_vendor?: string[] | null;
  /** Why `classify_serving_lists` refused the lists, or null. */
  serving_error?: string | null;
  compute_enabled?: boolean;
}

export type VerdictTone = 'shared' | 'partial' | 'off' | 'error';

export interface Verdict {
  tone: VerdictTone;
  text: string;
}

/** What the backend reported, not what the defaults say, so a door bound
 *  elsewhere is not described as loopback. */
export function doorAddress(state: GatewayState | null | undefined): string {
  const bind = typeof state?.bind === 'string' && state.bind.length > 0 ? state.bind : '127.0.0.1';
  const port = typeof state?.port === 'number' && Number.isFinite(state.port) ? String(state.port) : '?';
  return `${bind}:${port}`;
}

/**
 * What is shared right now, from the two independent switches that decide it:
 * `[gateway] enabled` (the IDE door on this machine) and `compute.enabled`
 * (the P2P door for peers). Four cells, four sentences.
 *
 * Two states are answered before the table, because in them the table would
 * say something untrue: `serving_error` shuts both doors on the listed
 * aliases while the socket may well be up, so it is not «the listener did not
 * open»; and `enabled` disagreeing with `running` is two facts, the setting
 * being read once at start.
 */
export function gatewayVerdict(
  state: GatewayState | null | undefined,
  computeEnabled: boolean,
): Verdict {
  if (!state) return { tone: 'off', text: 'The IDE door has not been read yet.' };
  const where = doorAddress(state);

  if (typeof state.serving_error === 'string' && state.serving_error.length > 0) {
    return {
      tone: 'error',
      text: `The serving lists cannot be read, so every call on them is refused — at the IDE door and over P2P alike: ${state.serving_error}`,
    };
  }
  if (state.enabled && !state.running) {
    return {
      tone: 'error',
      text: `The IDE door is configured but not running — see the log; nothing is listening on ${where}.`,
    };
  }
  if (!state.enabled && state.running) {
    return {
      tone: 'error',
      text: `A listener still holds ${where} while [gateway] enabled = false: that setting is read once at start, so this door stays open until the next restart.`,
    };
  }

  const doorOpen = !!state.enabled && !!state.running;
  if (computeEnabled && doorOpen) {
    return {
      tone: 'shared',
      text: `Peers on the allow lists and IDE clients on ${where} can call the serving lists.`,
    };
  }
  if (computeEnabled) {
    return { tone: 'partial', text: 'Peers can call; the IDE door is off ([gateway] enabled = false).' };
  }
  if (doorOpen) {
    return {
      tone: 'partial',
      text: 'The IDE door is open for this machine only; no peer is served (compute.enabled = false).',
    };
  }
  return { tone: 'off', text: 'Nothing is shared.' };
}

/** One paste-ready block, as `gateway.client_config_lines` renders it. */
export interface ClientLine {
  client: string;
  text: string;
}

/** `get_gateway_client_lines`: the blocks, and the masked form of the key that
 *  stands in clear inside them. */
export interface ClientLinesResult {
  lines?: ClientLine[] | null;
  key_masked?: string | null;
}

const CLIENT_LABELS: Record<string, string> = {
  continue: 'Continue',
  cursor: 'Cursor',
  claude_code: 'Claude Code',
  curl: 'curl',
};

/** A client this build has no word for is printed as it arrived, not dropped. */
export function clientLabel(client: string | null | undefined): string {
  const name = (client ?? '').trim();
  if (!name) return 'unnamed client';
  return CLIENT_LABELS[name] ?? name;
}

/**
 * The line on the collapsed «Client configuration» block. The blocks inside
 * hold the key in clear; what stands on the outside — readable over a
 * shoulder, or in a screenshot — is the masked form.
 */
export function maskedHeader(result: ClientLinesResult | null | undefined): string {
  const lines = (result?.lines ?? []).filter((line) => line && typeof line.client === 'string');
  const names = lines.map((line) => clientLabel(line.client)).join(', ');
  const masked = typeof result?.key_masked === 'string' && result.key_masked.length > 0
    ? result.key_masked
    : null;
  if (lines.length === 0) {
    return masked
      ? `nothing to paste yet; the key is ${masked}`
      : 'nothing to paste yet, and no key has been written';
  }
  return masked
    ? `${names} — each block carries the key ${masked} in clear`
    : `${names} — no key has been written yet; start the gateway once`;
}

// --- (6) What a peer sees ---------------------------------------------------

/** `get_peer_provider_menu(peer_id)`. `allowed` is read off the firewall and
 *  not off the row count: allowed and served nothing is a serving list, not a
 *  permission, and `reason` says which. */
export interface PeerMenuResult {
  peer_id?: string;
  known?: boolean;
  connected?: boolean;
  allowed?: boolean;
  reason?: string | null;
  rows?: MenuRow[] | null;
}

export type MenuVerdictKind = 'none' | 'refused' | 'empty' | 'served';

export interface MenuVerdict {
  kind: MenuVerdictKind;
  /** The tab's sentence about this peer. */
  text: string;
  /** The backend's own reason, where it gave one, shown beneath. */
  detail: string | null;
  rows: MenuRow[];
}

/** The three states kept apart because they ask for different repairs:
 *  refused is a permission, empty is a serving list, served is the wire. */
export function menuVerdict(result: PeerMenuResult | null | undefined): MenuVerdict {
  if (!result) return { kind: 'none', text: 'No peer picked yet.', detail: null, rows: [] };
  const rows = (result.rows ?? []).filter((row): row is MenuRow => !!row && typeof row.alias === 'string');
  const detail = typeof result.reason === 'string' && result.reason.length > 0 ? result.reason : null;

  if (!result.allowed) {
    return {
      kind: 'refused',
      text: 'This peer gets an empty menu: it is on no list that admits it.',
      detail,
      rows: [],
    };
  }
  if (rows.length === 0) {
    return {
      kind: 'empty',
      text: 'Allowed, but nothing is designated for it — check the serving lists.',
      detail,
      rows: [],
    };
  }
  return {
    kind: 'served',
    text: `${rows.length} ${rows.length === 1 ? 'row' : 'rows'} would be sent to this peer.`,
    detail,
    rows,
  };
}

// --- Validate without saving ------------------------------------------------

/**
 * The rules object `validate_firewall_rules` is asked about.
 *
 * `whole`, when given, is the same object Save would post (`editedRules`):
 * every tab mutates it in place, so it is returned as-is, no overlay. Absent
 * a `whole` — the component used standalone — this falls back to `saved`
 * (the file on disk) with `compute` and `node_groups` laid over it.
 */
export function validationDraft(
  whole: Record<string, unknown> | null | undefined,
  saved: Record<string, unknown> | null | undefined,
  compute: ComputeRules | null | undefined,
  nodeGroups?: Record<string, unknown> | null,
): Record<string, unknown> {
  if (whole) return whole;
  const draft: Record<string, unknown> = { ...(saved ?? {}) };
  if (compute) draft.compute = compute;
  if (nodeGroups) draft.node_groups = nodeGroups;
  return draft;
}
