/**
 * Pure helpers for the `strata` provider form (ADR-044 point 7): the model
 * parameters Strata's own page edits, the second-shard suggestion, and the
 * small formatters the status panel uses. The backend owns the rules; this
 * module only mirrors what the form must show before a save.
 */

export type StrataParamKind = 'number' | 'int' | 'bool' | 'enum' | 'names';

export interface StrataParamSpec {
  key: string;
  kind: StrataParamKind;
  min?: number;
  max?: number;
  /** The lower bound is exclusive (top_p: more than 0). */
  minExclusive?: boolean;
  step?: number;
  choices?: string[];
  help: string;
}

/**
 * Strata's serve/runconfig.py EDITABLE (Strata 477ac13c, read 2026-10-18), in
 * its order, with its help texts. `open_browser` is left out: DPC's start
 * config always writes it false. The backend's STRATA_MODEL_PARAMS
 * (strata_supervisor.py) is the same list; a backend test keeps them equal.
 */
export const STRATA_MODEL_PARAMS: StrataParamSpec[] = [
  { key: 'sampling.temperature', kind: 'number', min: 0, step: 0.05,
    help: "Default temperature for requests that send none (0 = greedy; without it the model card's: 1.0 thinking, 0.7 not)" },
  { key: 'sampling.top_p', kind: 'number', min: 0, minExclusive: true, max: 1, step: 0.01,
    help: 'Default top_p for requests that send none' },
  { key: 'sampling.top_k', kind: 'int', min: 1, max: 64, step: 1,
    help: 'Default top_k for requests that send none (1-64)' },
  { key: 'sampling.min_p', kind: 'number', min: 0, max: 1, step: 0.01,
    help: 'Default min_p for requests that send none' },
  { key: 'reasoning_budget_tokens', kind: 'int', min: 0, step: 256,
    help: 'Cap the thinking of every request at this many tokens (0 or empty: no cap)' },
  { key: 'fit_max_tokens', kind: 'bool',
    help: 'Shorten a max_tokens that does not fit the context instead of answering 400' },
  { key: 'anthropic_thinking', kind: 'enum', choices: ['model', 'on_request'],
    help: 'Anthropic requests that do not ask for thinking: think as the model does (model) or not (on_request)' },
  { key: 'effort_position', kind: 'enum', choices: ['start', 'end'],
    help: 'Where a non-default reasoning effort goes: start (the default) or end (keeps the cache when it changes)' },
  { key: 'aliases', kind: 'names',
    help: 'Other model names the server lists and answers to (comma-separated)' },
  { key: 'idle_unload_s', kind: 'number', min: 0, step: 60,
    help: 'Unload the model after this many seconds without requests (0 or empty: never)' },
  { key: 'lazy_load', kind: 'bool',
    help: 'Start without loading the model; the first request loads it (text only)' },
  { key: 'engine_silence_s', kind: 'number', min: 0, step: 30,
    help: 'End a request when the engine says nothing for this long (default 300 s, 0 = wait)' },
  { key: 'api_monitor', kind: 'bool',
    help: "Keep the last 100 requests' prompts and answers in memory for /api-monitor" },
  { key: 'vram_reserve_mib', kind: 'int', min: 0, step: 100,
    help: 'VRAM in MiB the engine leaves free for other programs (engine default 700); the VRAM reserve field above wins when set' },
];

export interface StrataCacheAdmissionSpec {
  key: 'conversation_cache_min_free_mib' | 'conversation_cache_min_tokens' | 'conversation_cache_park_gain';
  flag: string;
  /** Strata's own default, which DPC writes when the alias sets none. */
  strataDefault: number;
  max: number;
  label: string;
  help: string;
}

/**
 * The conversation cache's admission rules (Strata 0.1.45, src/program/generate.cpp,
 * read 2026-10-19). Every start config carries all three, the default included,
 * so the value is the alias's and visible. The backend's CACHE_ADMISSION_FLAGS
 * (strata_supervisor.py) is the same list; a backend test keeps them equal.
 */
export const STRATA_CACHE_ADMISSION: StrataCacheAdmissionSpec[] = [
  { key: 'conversation_cache_min_free_mib', flag: '--conversation-cache-min-free-mib', strataDefault: 2560,
    max: 8796093022207, label: 'RAM floor, MiB',
    help: 'Physical RAM (MemAvailable; GlobalMemoryStatusEx ullAvailPhys on Windows, under the cgroup limits and '
      + '--memory-limit-mib) that must stay free when a conversation is parked, saved or restored. It is not free '
      + 'commit: a park can pass this floor and still meet the commit limit.' },
  { key: 'conversation_cache_min_tokens', flag: '--conversation-cache-min-tokens', strataDefault: 0,
    max: 2147483647, label: 'Shortest parked conversation, tokens',
    help: 'Only conversations of at least this many tokens are parked, so short side requests do not take the '
      + 'slots a long one needs (0 = park every conversation).' },
  { key: 'conversation_cache_park_gain', flag: '--conversation-cache-park-gain', strataDefault: 0,
    max: 2147483647, label: 'Park gain, tokens',
    help: 'A conversation is parked again only after it grew this many tokens past the copy it would replace; a '
      + 'resume from the older copy then re-reads up to that many tokens (0 = park every turn).' },
];

/** The placeholder of an admission field: the default DPC writes, and a base value it overrides. */
export function admissionPlaceholder(spec: StrataCacheAdmissionSpec, baseValue: unknown): string {
  const written = `Strata default ${spec.strataDefault}, written`;
  if (baseValue === undefined || baseValue === null || baseValue === '' || baseValue === spec.strataDefault) return written;
  return `${written} — the base config's ${modelParamText(baseValue)} is not used`;
}

/** The five cache flags as the start config the last launch used writes them (`start_cache_flags`). */
export interface StartCacheFlags {
  conversation_cache_mib?: number | null;
  conversation_cache_slots?: number | null;
  conversation_cache_min_free_mib?: number | null;
  conversation_cache_min_tokens?: number | null;
  conversation_cache_park_gain?: number | null;
}

/** One line for the status panel: every cache flag the launch was started with. */
export function cacheFlagsLine(f: StartCacheFlags | null | undefined): string {
  if (!f) return 'not read — no start config used yet';
  const n = (v: number | null | undefined) => (typeof v === 'number' ? v.toLocaleString('en-US') : '?');
  const slots = typeof f.conversation_cache_slots === 'number' ? n(f.conversation_cache_slots) : 'engine default 4';
  return `budget ${n(f.conversation_cache_mib)} MiB, slots ${slots}, RAM floor ${n(f.conversation_cache_min_free_mib)} MiB `
    + `(physical RAM), shortest parked ${n(f.conversation_cache_min_tokens)} tokens, `
    + `park gain ${n(f.conversation_cache_park_gain)} tokens`;
}

/** The admission rules as the engine's INFO line reports them (`metrics.cache_admission`). */
export interface EngineCacheAdmission {
  min_free_mib?: number | null;
  min_tokens?: number | null;
  park_gain?: number | null;
}

export function engineAdmissionLine(a: EngineCacheAdmission | null | undefined): string {
  const n = (v: number | null | undefined, unit: string) =>
    (typeof v === 'number' ? `${v.toLocaleString('en-US')} ${unit}` : 'not reported');
  if (!a) return 'not reported';
  return `RAM floor ${n(a.min_free_mib, 'MiB')}, shortest parked ${n(a.min_tokens, 'tokens')}, `
    + `park gain ${n(a.park_gain, 'tokens')}`;
}

const SHARD_RE = /^(.*)-(\d{5})-of-(\d{5})(\.gguf)$/i;

/** The second shard beside a first shard `...-00001-of-0000N.gguf`, or null. */
export function suggestPleGguf(native: string | undefined | null): string | null {
  const m = SHARD_RE.exec((native ?? '').trim());
  if (!m || parseInt(m[2], 10) !== 1 || parseInt(m[3], 10) < 2) return null;
  return `${m[1]}-00002-of-${m[3]}${m[4]}`;
}

/**
 * The `ple_gguf` to show after the first shard changed: the new suggestion
 * when the field is empty or still holds the suggestion made for the previous
 * shard; otherwise what the user typed stays.
 */
export function pleAfterNativeChange(
  currentPle: string | undefined | null,
  previousNative: string | undefined | null,
  newNative: string | undefined | null,
): string | undefined {
  const current = (currentPle ?? '').trim();
  const suggested = suggestPleGguf(newNative);
  const stale = suggestPleGguf(previousNative);
  if (current === '' || (stale !== null && current === stale)) return suggested ?? (current || undefined);
  return current;
}

export type ParamParse = { value: unknown } | { error: string };

/**
 * A form value for one parameter: `{ value: undefined }` for an empty field
 * (the key is left out and Strata's own default applies), an error otherwise
 * when the value breaks Strata's rule.
 */
export function parseModelParam(spec: StrataParamSpec, raw: string): ParamParse {
  const text = raw.trim();
  if (text === '') return { value: undefined };
  if (spec.kind === 'bool') {
    if (text === 'true') return { value: true };
    if (text === 'false') return { value: false };
    return { error: `${spec.key}: expected true or false` };
  }
  if (spec.kind === 'enum') {
    return spec.choices?.includes(text) ? { value: text } : { error: `${spec.key}: expected one of ${spec.choices?.join(', ')}` };
  }
  if (spec.kind === 'names') {
    const names = text.split(',').map((s) => s.trim()).filter((s) => s.length > 0);
    return names.length ? { value: names } : { value: undefined };
  }
  const n = Number(text);
  if (!Number.isFinite(n)) return { error: `${spec.key}: expected a number` };
  if (spec.kind === 'int' && !Number.isInteger(n)) return { error: `${spec.key}: expected a whole number` };
  if (spec.min !== undefined && (spec.minExclusive ? n <= spec.min : n < spec.min)) {
    return { error: `${spec.key}: must be ${spec.minExclusive ? 'more than' : 'at least'} ${spec.min}` };
  }
  if (spec.max !== undefined && n > spec.max) return { error: `${spec.key}: must be at most ${spec.max}` };
  return { value: n };
}

/** How a stored parameter value reads in its form control. */
export function modelParamText(value: unknown): string {
  if (value === undefined || value === null) return '';
  if (Array.isArray(value)) return value.join(', ');
  return String(value);
}

/** A placeholder from the base config's value, or the fallback when it names none. */
export function basePlaceholder(value: unknown, fallback: string): string {
  if (value === undefined || value === null || value === '') return fallback;
  return `base config: ${modelParamText(value)}`;
}

/** `true` mismatch, `false` match, `null` when either side is unknown. */
export function versionDiffers(engine: string | null | undefined, tested: string | null | undefined): boolean | null {
  if (!engine || !tested) return null;
  return engine.trim() !== tested.trim();
}

export function formatMib(bytes: number | null | undefined): string {
  if (typeof bytes !== 'number' || !Number.isFinite(bytes)) return '—';
  return `${Math.round(bytes / (1 << 20)).toLocaleString('en-US')} MiB`;
}

export interface ConversationCacheSummary {
  enabled?: boolean | null;
  budget_mib?: number | null;
  slots?: number | null;
  parked?: number | null;
  bytes?: number | null;
  parks?: number | null;
  restores?: number | null;
  evictions?: number | null;
}

/** One line for the status panel: budget, what is parked, and the counters. */
export function cacheSummaryLine(c: ConversationCacheSummary | null | undefined): string {
  if (!c) return 'not reported';
  if (!c.enabled) return 'off';
  const budget = typeof c.budget_mib === 'number' ? `${c.budget_mib.toLocaleString('en-US')} MiB` : '?';
  const slots = typeof c.slots === 'number' ? ` / ${c.slots} slots` : '';
  return `budget ${budget}${slots}; parked ${c.parked ?? 0} (${formatMib(c.bytes)}); `
    + `parks ${c.parks ?? 0}, restores ${c.restores ?? 0}, evictions ${c.evictions ?? 0}`;
}

/** GlobalMemoryStatusEx as local_engine_status reports it (commit_memory.py); MiB. */
export interface FreeCommitReading {
  avail_page_file_mib: number;
  total_page_file_mib: number;
  avail_phys_mib: number;
  total_phys_mib: number;
  source?: string;
}

/** The cache hint's measurement: free commit (ullAvailPageFile) first, the
 *  reference by Mike's call of 2026-10-19; free physical (ullAvailPhys) after it.
 *  `undefined` is a status not read yet, `null` a backend that measured nothing. */
export function freeCommitLine(r: FreeCommitReading | null | undefined): string {
  if (r === undefined) return 'free commit (GlobalMemoryStatusEx): not read yet — Refresh the engine status';
  if (!r) return 'free commit (GlobalMemoryStatusEx): not measured on this OS';
  const n = (v: number) => v.toLocaleString('en-US');
  return `free commit (GlobalMemoryStatusEx ullAvailPageFile) ${n(r.avail_page_file_mib)} of `
    + `${n(r.total_page_file_mib)} MiB; free physical (ullAvailPhys) ${n(r.avail_phys_mib)} of `
    + `${n(r.total_phys_mib)} MiB`;
}
