// inferenceUsage.ts — the pure half of the Usage block of the Inference Sharing tab.
//
// The backend already reads the node ledger by role: `node_ledger.usage_by_role`
// folds the same rows three ways and `CoreService.get_inference_usage` answers
// with them (ADR-041 D3; board entry THE-LEDGER-COUNTS-EVERY-SHARED-CALL-AND-
// NEITHER-SIDE-CAN-SEE-IT-IN-THE-UI). Nothing here re-derives money: a group's
// `cost_usd` and its per-currency `tariff` amounts are shown exactly as the rows
// carry them, and the two states that are not an amount — a tariff over counts
// nobody could price, and a call with no tariff at all — are counted, never
// summed in as zero. No DOM in this file; it is what the tests exercise.

// --- The response as `usage_by_role` writes it ---------------------------

/** What one currency comes to in a group, and over how many rows. */
export interface WireTariff {
  amount: number;
  rows: number;
}

/** One group of `_new_role_entry`, plus the fields its role sets on it. */
export interface WireGroup {
  row_count?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  thinking_tokens?: number;
  duration_s?: number;
  counts_source?: { ours?: number; engine?: number };
  peer_proved?: { true?: number; false?: number; none?: number };
  cost_usd?: number;
  unpriced?: number;
  tariff?: Record<string, WireTariff>;
  tariff_unpriceable?: number;
  untariffed?: number;
  /** `consumed.by_source` only: the host that served the call, when the row named one. */
  node_id?: string | null;
  /** `consumed.by_source` only: the alias asked for, as the row carries it. */
  alias?: string | null;
  /** `served.by_caller` only: the same fold, one group per alias that answered. */
  by_alias?: Record<string, WireGroup>;
}

export interface UsageResponse {
  status?: string;
  message?: string;
  since?: string | null;
  until?: string | null;
  served?: { by_caller?: Record<string, WireGroup>; by_alias?: Record<string, WireGroup> };
  consumed?: { by_source?: Record<string, WireGroup> };
  own?: { by_alias?: Record<string, WireGroup> };
}

// --- The view model ------------------------------------------------------

export type UsageRole = 'served' | 'consumed' | 'own';

/** A node the application already holds a name for — `knownNodes` in
 *  inferenceSharing.ts, which is the tab's own source for peer names. */
export interface NamedNode {
  node_id: string;
  label: string;
}

export interface Badge {
  /** The `.badge-*` modifier this reuses from the dialog's own idiom. */
  kind: 'missing' | 'gift' | 'first' | 'quota';
  text: string;
}

export interface Owed {
  currency: string;
  amount: number;
  rows: number;
}

export interface UsageRow {
  /** The key the backend grouped by; unique inside its list. */
  key: string;
  /** What a person reads: a peer's name, its bare id where no name is known,
   *  or an alias. Never blank. */
  label: string;
  /** The node at the other end, where the row has one. */
  id: string | null;
  /** The model name that answered, where the row is about one alias. */
  alias: string | null;
  calls: number;
  promptTokens: number;
  completionTokens: number;
  thinkingTokens: number;
  durationS: number;
  /** This node's own dollars on these rows — zero on a consumed row by
   *  construction: the money stayed with the node that ran the call. */
  costUsd: number;
  unpriced: number;
  owed: Owed[];
  unpriceable: number;
  badges: Badge[];
  /** For what: on a served row, the aliases that answered this caller. */
  parts: UsageRow[];
}

export interface UsageList {
  role: UsageRole;
  title: string;
  /** The line beside the title: what this list counts, and where it was measured. */
  note: string;
  /** What the list says about itself when it holds nothing. */
  empty: string;
  rows: UsageRow[];
  /** True when any row on this list carries a tariff nobody could price. */
  hasUnpriceable: boolean;
}

export interface UsageView {
  month: string;
  lists: UsageList[];
  /** True when any of the three lists holds an unpriceable row, which is what
   *  the "counted, not summed" note stands on. */
  hasUnpriceable: boolean;
}

const LIST_TITLES: Record<UsageRole, string> = {
  served: 'Served to peers',
  consumed: 'Consumed',
  /** `route=local` minus the calls a peer asked for, while the summary's burn
   *  folds every local row: two questions, so two names. */
  own: 'Own calls (peers excluded)',
};

/** The line beside a list's title: what it counts and on which side it was
 *  measured. Here rather than in the markup so a test can read it. */
const LIST_NOTES: Record<UsageRole, string> = {
  served: 'to whom, how much, for what',
  consumed: 'from whom, how much, for what',
  own: "this node's own vendor and local burn; a call served to a peer is on the Served list, "
    + "not here — the summary's burn figure counts every local call, those included",
};

const LIST_EMPTY: Record<UsageRole, string> = {
  served: 'No peer has called this node yet.',
  consumed: "This node has called no peer's or vendor's model yet.",
  own: 'No own usage this month.',
};

/** The duration column is not one quantity: a host times its own provider call,
 *  a guest times its wait, and the same calls read longer on the guest. */
export function durationLabel(role: UsageRole): string {
  return role === 'consumed' ? 'round trip' : 'engine time';
}

/** The same distinction at length, for the column's tooltip. */
export function durationTitle(role: UsageRole): string {
  return role === 'consumed'
    ? "Round trip: this node's wait for the answer, the wire and the host's queue included."
    : "Engine time: this node's own provider call, the engine's warm-up included.";
}

// --- The month, which is the default period ------------------------------

const MONTH_NAMES = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
];

/** `YYYY-MM` of a moment, in UTC — a ledger partition is a UTC month of
 *  `started_at`, so a local-time month would ask for a file that is not there. */
export function monthKey(at: Date = new Date()): string {
  return at.toISOString().slice(0, 7);
}

/** The month `delta` months from `key`, as `YYYY-MM`. */
export function shiftMonth(key: string, delta: number): string {
  const match = /^(\d{4})-(\d{2})$/.exec(key);
  if (!match) return key;
  const months = Number(match[1]) * 12 + (Number(match[2]) - 1) + delta;
  const year = Math.floor(months / 12);
  const month = months - year * 12;
  return `${String(year).padStart(4, '0')}-${String(month + 1).padStart(2, '0')}`;
}

/** `2026-09` as `September 2026`; an unparsable key reads as itself. */
export function monthLabel(key: string): string {
  const match = /^(\d{4})-(\d{2})$/.exec(key);
  if (!match) return key;
  const name = MONTH_NAMES[Number(match[2]) - 1];
  return name ? `${name} ${match[1]}` : key;
}

// --- Formatting ----------------------------------------------------------

/** Thousands separated by a space, written here rather than taken from
 *  the runtime's locale so the same number reads the same on every machine. */
export function formatTokens(count: number): string {
  const whole = Math.round(count || 0);
  return String(whole).replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
}

/** An amount of money as its own size asks: cents where there are cents, four
 *  places where a call costs less than one, and never a 0.00 that hides a
 *  charge — a tariff of $0.6 per 1M tokens makes amounts this small. */
export function formatAmount(amount: number): string {
  const value = Number(amount) || 0;
  if (value === 0) return '0.00';
  const magnitude = Math.abs(value);
  if (magnitude >= 0.01) return value.toFixed(2);
  if (magnitude >= 0.0001) return value.toFixed(4);
  return value.toExponential(1);
}

/** What is owed, per currency, in the order the currencies sort. Two
 *  currencies do not add, so they are two strings and never one sum. */
export function formatOwed(owed: readonly Owed[]): string {
  if (owed.length === 0) return '—';
  return owed.map((o) => `${formatAmount(o.amount)} ${o.currency}`).join(' + ');
}

export function formatDuration(seconds: number): string {
  const value = Number(seconds) || 0;
  if (value < 60) return `${value.toFixed(1)} s`;
  const minutes = Math.floor(value / 60);
  if (minutes < 60) return `${minutes} m ${Math.round(value - minutes * 60)} s`;
  const hours = Math.floor(minutes / 60);
  return `${hours} h ${minutes - hours * 60} m`;
}

// --- Shaping -------------------------------------------------------------

function number(value: unknown): number {
  return typeof value === 'number' && isFinite(value) ? value : 0;
}

function owedOf(group: WireGroup): Owed[] {
  return Object.entries(group.tariff ?? {})
    .map(([currency, value]) => ({
      currency,
      amount: number(value?.amount),
      rows: number(value?.rows),
    }))
    .sort((a, b) => a.currency.localeCompare(b.currency));
}

/** The badges a group's own counters earn.
 *
 *  Nothing about a far end or a tariff is said on an own row: there is no node
 *  to prove (`usage_row` writes `none`), and no tariff applies to a call a node
 *  makes for itself — an own row is priced by `cost_usd`, so 'gift' there would
 *  deny real spend. 'recounted' says who counted the tokens, true of any row.
 */
export function badgesOf(group: WireGroup, role: UsageRole): Badge[] {
  const badges: Badge[] = [];
  const rows = number(group.row_count);
  const recounted = number(group.counts_source?.ours);
  if (recounted > 0) badges.push({ kind: 'quota', text: `${recounted} recounted` });
  if (role !== 'own') {
    const unpriceable = number(group.tariff_unpriceable);
    if (unpriceable > 0) badges.push({ kind: 'missing', text: `${unpriceable} unpriceable` });
    if (rows > 0 && number(group.untariffed) === rows) badges.push({ kind: 'gift', text: 'gift' });
    const unproved = number(group.peer_proved?.false);
    if (unproved > 0) badges.push({ kind: 'missing', text: `${unproved} unproved` });
    const unknown = number(group.peer_proved?.none);
    if (unknown > 0) badges.push({ kind: 'first', text: `${unknown} unknown` });
  }
  return badges;
}

function rowOf(
  key: string,
  group: WireGroup,
  role: UsageRole,
  identity: { label: string; id: string | null; alias: string | null },
  parts: UsageRow[] = [],
): UsageRow {
  return {
    key,
    label: identity.label,
    id: identity.id,
    alias: identity.alias,
    calls: number(group.row_count),
    promptTokens: number(group.prompt_tokens),
    completionTokens: number(group.completion_tokens),
    thinkingTokens: number(group.thinking_tokens),
    durationS: number(group.duration_s),
    costUsd: number(group.cost_usd),
    unpriced: number(group.unpriced),
    owed: owedOf(group),
    unpriceable: number(group.tariff_unpriceable),
    badges: badgesOf(group, role),
    parts,
  };
}

/** Busiest first, then by label, so two runs of the same data read alike. */
function ordered(rows: UsageRow[]): UsageRow[] {
  return rows.sort((a, b) => (b.calls - a.calls) || a.label.localeCompare(b.label));
}

/** The name this tab already shows for a node, its bare id where none is
 *  known, and a caller that is not a node id read as itself. Never blank. */
export function nodeLabel(id: string | null | undefined, names: ReadonlyMap<string, string>): string {
  if (!id) return '';
  return names.get(id) || id;
}

function namesOf(nodes: readonly NamedNode[] | null | undefined): Map<string, string> {
  const names = new Map<string, string>();
  for (const node of nodes ?? []) {
    if (node?.node_id && node.label) names.set(node.node_id, node.label);
  }
  return names;
}

/**
 * A `consumed.by_source` key as `node_ledger.consumed_key` writes it:
 * `remote:<host>:<alias>`, with `?` for a host the row did not name and an
 * alias that may itself hold colons. A key with no `remote:` prefix is the bare
 * alias an older row was grouped under, and is read as that alias.
 */
export function parseConsumedKey(key: string): { host: string | null; alias: string | null } {
  const bare = key ?? '';
  if (!bare.startsWith('remote:')) return { host: null, alias: bare || null };
  const rest = bare.slice('remote:'.length);
  const cut = rest.indexOf(':');
  if (cut < 0) return { host: null, alias: rest || null };
  const host = rest.slice(0, cut);
  const alias = rest.slice(cut + 1);
  return { host: host && host !== '?' ? host : null, alias: alias || null };
}

function listOf(role: UsageRole, rows: UsageRow[]): UsageList {
  return {
    role,
    title: LIST_TITLES[role],
    note: LIST_NOTES[role],
    empty: LIST_EMPTY[role],
    rows: ordered(rows),
    hasUnpriceable: rows.some((row) => row.unpriceable > 0),
  };
}

/**
 * The response as three lists: to whom this node served and on what, from whom
 * it consumed and on what, and what it spent on itself.
 *
 * `consumed.by_source` is keyed `remote:<host>:<alias>`, with `?` for the host
 * of a row that named none, so a peer's alias can never key the same bucket as
 * a local alias of that name. The group's own `node_id` and `alias` are read
 * first and the key answers where they are absent; an unnamed host is shown as
 * unrecorded rather than misnamed.
 */
export function shapeUsage(
  response: UsageResponse | null | undefined,
  nodes: readonly NamedNode[] | null | undefined,
  month: string,
): UsageView {
  const names = namesOf(nodes);

  const served = Object.entries(response?.served?.by_caller ?? {}).map(([caller, group]) => {
    const parts = Object.entries(group.by_alias ?? {}).map(([alias, part]) =>
      rowOf(`${caller}:${alias}`, part, 'served', { label: alias, id: null, alias }),
    );
    return rowOf(caller, group, 'served', {
      label: nodeLabel(caller, names) || caller,
      id: caller,
      alias: null,
    }, ordered(parts));
  });

  const consumed = Object.entries(response?.consumed?.by_source ?? {}).map(([key, group]) => {
    const fromKey = parseConsumedKey(key);
    const host = group.node_id ?? fromKey.host;
    const alias = group.alias ?? fromKey.alias;
    const row = rowOf(key, group, 'consumed', {
      label: host ? nodeLabel(host, names) : alias || key,
      id: host,
      alias,
    });
    if (!host) row.badges = [{ kind: 'missing', text: 'host not recorded' }, ...row.badges];
    return row;
  });

  const own = Object.entries(response?.own?.by_alias ?? {}).map(([alias, group]) =>
    rowOf(alias, group, 'own', { label: alias, id: null, alias }),
  );

  const lists = [listOf('served', served), listOf('consumed', consumed), listOf('own', own)];
  return { month, lists, hasUnpriceable: lists.some((list) => list.hasUnpriceable) };
}
