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
 * Strata's serve/runconfig.py EDITABLE (Strata v0.2.11, compared 2026-11-06), in
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
    help: 'Shorten a max_tokens that does not fit the context (on by default); off: answer 400 like llama.cpp' },
  { key: 'anthropic_thinking', kind: 'enum', choices: ['model', 'on_request'],
    help: 'Anthropic requests that do not ask for thinking: think as the model does (model) or not (on_request)' },
  { key: 'effort_position', kind: 'enum', choices: ['start', 'end'],
    help: 'Where a non-default reasoning effort goes: start (the default) or end (keeps the cache when it changes)' },
  { key: 'aliases', kind: 'names',
    help: 'Other model names the server lists and answers to (comma-separated)' },
  { key: 'idle_unload_s', kind: 'number', min: 0, step: 60,
    help: 'Unload the model after this many seconds without requests (0 or empty: never)' },
  { key: 'mtp', kind: 'bool',
    help: 'Draft layer (MTP): off starts the engine without it (about 1 GB less graphics memory, slower words per second); empty or on: as installed' },
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
 * The conversation cache's admission rules (read in src/program/generate.cpp at Strata
 * 0.1.45, 2026-10-19; the defaults re-checked in the `--help` of engine 0.1.57, Strata
 * v0.2.11, 2026-11-06). Every start config carries all three, the default included,
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

/**
 * The drafter and the picture reader: two typed alias fields, each a dropdown whose first
 * option is an explicit "auto" (Mike's call, 2026-11-06). "auto" stores nothing: the key is
 * left out of the alias and of the start config, and Strata decides. The backend's
 * `DRAFTING_CHOICES` (strata_supervisor.py) is the same list; a backend test keeps them equal.
 * `off` is not a drafter choice: the Draft layer (MTP) field above turns the layer off.
 */
export const STRATA_DRAFTING_CHOICES = ['auto', 'mtp', 'dflash2'] as const;
export type StrataDraftingChoice = (typeof STRATA_DRAFTING_CHOICES)[number];

/** The dropdown value for a stored `drafting`: anything but mtp / dflash2 reads as auto. */
export function draftingSelectValue(stored: unknown): StrataDraftingChoice {
  return stored === 'mtp' || stored === 'dflash2' ? stored : 'auto';
}

/** The stored `drafting` for a dropdown value: auto stores nothing (undefined). */
export function draftingFromSelect(raw: string): 'mtp' | 'dflash2' | undefined {
  return raw === 'mtp' || raw === 'dflash2' ? raw : undefined;
}

export const DRAFTING_AUTO_LABEL = 'auto (Strata decides)';

/** The help under the drafter dropdown; the base config's own `drafting` stays when this field is auto. */
export function draftingHelp(baseValue: unknown): string {
  const base = typeof baseValue === 'string' && baseValue !== ''
    ? ` The base config says "${baseValue}" and stays.` : '';
  return 'auto: on one NVIDIA card DFlash2 when its pack is in the data folder, else MTP. '
    + 'To turn the draft layer off use the Draft layer (MTP) field.' + base;
}

export type PictureReaderChoice = 'auto' | 'false' | 'true';

/** The dropdown value for a stored `vision_elastic`: only a boolean is a choice. */
export function pictureReaderSelectValue(stored: unknown): PictureReaderChoice {
  return stored === true ? 'true' : stored === false ? 'false' : 'auto';
}

export const PICTURE_READER_OPTIONS: { value: PictureReaderChoice; label: string }[] = [
  { value: 'auto', label: 'auto (Strata decides)' },
  { value: 'false', label: 'start with the engine' },
  { value: 'true', label: 'start with the first picture' },
];

/** The help under the picture reader dropdown. `baseValue` is the vision block's own `elastic`. */
export function pictureReaderHelp(baseValue: unknown): string {
  const base = typeof baseValue === 'boolean'
    ? ` The base config says ${baseValue ? 'first picture' : 'engine'} and stays.` : '';
  return 'auto: on one NVIDIA card the reader starts with the first picture. '
    + 'Starting with the first picture frees VRAM only on an engine that has --vram-elastic; '
    + 'otherwise Strata starts the reader anyway. Needs a vision block.' + base;
}

/** One line for the status panel: the drafter the running engine chose, and why not the one asked for. */
export function engineDraftingLine(drafter: string | null | undefined, note: string | null | undefined): string {
  const word = (drafter ?? '').trim();
  const why = (note ?? '').trim();
  if (!word && !why) return 'not reported';
  const label = word === 'mtp' ? 'MTP' : word === 'dflash2' ? 'DFlash2' : word === 'off' ? 'off' : word || '?';
  // The launcher's note already begins "Drafting: <word> (reason)"; show it whole, it is its own words.
  return why ? why : label;
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
export function versionDiffers(engine: string | null | undefined, pinned: string | null | undefined): boolean | null {
  if (!engine || !pinned) return null;
  return engine.trim() !== pinned.trim();
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

/** GlobalMemoryStatusEx as local_engine_status and list_processes report it
 *  (commit_memory.py); MiB. Both send the same keys on every OS: off Windows `source`
 *  names the physical reading (psutil) and the two page-file (commit) fields are null. */
export interface FreeCommitReading {
  avail_page_file_mib: number | null;
  total_page_file_mib: number | null;
  avail_phys_mib: number;
  total_phys_mib: number;
  source?: string;
}

/** The cache hint's measurement: free commit (ullAvailPageFile) first, the
 *  reference by Mike's call of 2026-10-19; free physical (ullAvailPhys) after it.
 *  Another source carries physical memory only, printed with its name.
 *  `undefined` is a status not read yet, `null` a reading that failed: every OS has one. */
export function freeCommitLine(r: FreeCommitReading | null | undefined): string {
  if (r === undefined) return 'free commit (GlobalMemoryStatusEx): not read yet — Refresh the engine status';
  if (!r) return 'free commit and free physical memory unread (the reading failed)';
  const n = (v: number | null) => (v ?? 0).toLocaleString('en-US');
  if (r.source && r.source !== 'GlobalMemoryStatusEx') {
    return `free physical (${r.source}) ${n(r.avail_phys_mib)} of ${n(r.total_phys_mib)} MiB; `
      + 'free commit not measured on this OS';
  }
  return `free commit (GlobalMemoryStatusEx ullAvailPageFile) ${n(r.avail_page_file_mib)} of `
    + `${n(r.total_page_file_mib)} MiB; free physical (ullAvailPhys) ${n(r.avail_phys_mib)} of `
    + `${n(r.total_phys_mib)} MiB`;
}

/** The engine backend select (`strata_engine_backend`): its one-line help. */
export const ENGINE_BACKEND_HELP = 'Only AMD builds can be chosen; CUDA 12 or 13 is picked by Strata from the driver.';
/** In its place when a Strata folder is set: the choice applies only to DPC's own install. */
export const ENGINE_BACKEND_OWN_FOLDER = "Applies only to DPC's own install: your Strata folder installs its own engine.";

/** The select's options: Strata's own pick first (empty), then the values this
 *  machine may take (local_engine_status `engine_backends`, the backend's one
 *  list). A saved value not among them stays listed, so the save can name why
 *  it is refused instead of the form dropping it. */
export function engineBackendOptions(
  allowed: string[] | null | undefined,
  current?: string | null
): { value: string; label: string }[] {
  const values = [...(allowed ?? [])];
  const saved = (current ?? '').trim();
  if (saved && !values.includes(saved)) values.push(saved);
  return [{ value: '', label: 'Strata picks for this card (default)' }, ...values.map((v) => ({ value: v, label: v }))];
}

/**
 * The note under Context Window on a strata alias left unset. The backend then takes the
 * running engine's report, and until one runs the start config's --max-context.
 *
 * @param maxContext the alias's max_context or the run config's value; undefined until read
 */
export function strataWindowHint(maxContext: unknown): string {
  if (maxContext === undefined) return '';
  const tokens = typeof maxContext === 'number' ? maxContext : parseInt(String(maxContext), 10);
  if (!Number.isInteger(tokens) || tokens <= 0 || String(tokens) !== String(maxContext)) {
    return (
      'This run config names no --max-context, so the window is not known until the engine has started: ' +
      'an agent on this alias refuses its messages until then. Set one here, or start the engine once in an AI chat.'
    );
  }
  return (
    `Left on Auto-detected, an agent's window is ${tokens.toLocaleString()} tokens from --max-context, ` +
    'and once the engine runs, what the engine reports.'
  );
}
