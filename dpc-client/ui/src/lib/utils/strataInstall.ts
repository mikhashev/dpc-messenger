// Add Provider for type `strata`: choosing a Strata run config from the model files
// folder, installing a Strata model (list_strata_models / install_strata_model), and the
// conversation-cache and resident-budget suggestions that follow the choice. UI-local helpers, pure.

import type { FreeCommitReading } from './strataFields';

/** One size of a family in list_strata_models: Strata's own model_table row, field for
 *  field. `on_this_pc` is Strata's words for this PC and is advice only. */
export interface StrataModelSize {
    model: string;
    about?: string;
    download_gb?: number | null;
    ram_needed_gb?: number | null;
    /** Strata's `arena_gb`: all of this size's experts, in decimal GB. A budget is in GiB. */
    experts_gb?: number | null;
    images?: boolean;
    on_this_pc?: string | null;
    installed?: boolean;
    id?: string;
}

export interface StrataModelFamily {
    family: string;
    title?: string;
    about?: string;
    images?: boolean;
    sizes: StrataModelSize[];
}

/** The label that says where the numbers beside a size come from. */
export const STRATA_ESTIMATE_LABEL = "Strata's estimate";

export const CACHE_SUGGESTION_MIN_MIB = 1024;
export const CACHE_SUGGESTION_MAX_MIB = 16384;
/** Left for the rest of the machine before the cache is sized. */
export const CACHE_SUGGESTION_RESERVE_MIB = 8192;

/**
 * The conversation-cache budget (MiB) suggested for a model that needs `ramNeededGb`, from
 * the free commit memory read now: half of what is left after the model and a reserve of
 * 8192 MiB, rounded down to a multiple of 1024 and held to [1024, 16384]. Null when either
 * figure is unknown (off Windows free commit is not measured). A suggestion, not a measurement.
 * The one place the rule lives.
 */
export function suggestedConversationCacheMib(
    freeCommitMib: number | null | undefined,
    ramNeededGb: number | null | undefined,
): number | null {
    if (typeof freeCommitMib !== 'number' || !Number.isFinite(freeCommitMib)) return null;
    if (typeof ramNeededGb !== 'number' || !Number.isFinite(ramNeededGb)) return null;
    const floored = Math.floor(unclampedCacheMib(freeCommitMib, ramNeededGb) / 1024) * 1024;
    return Math.min(CACHE_SUGGESTION_MAX_MIB, Math.max(CACHE_SUGGESTION_MIN_MIB, floored));
}

function unclampedCacheMib(freeCommitMib: number, ramNeededGb: number): number {
    return (freeCommitMib - ramNeededGb * 1024 - CACHE_SUGGESTION_RESERVE_MIB) / 2;
}

/** True when the rule, before its floor of 1024, comes out below 1024: the model and the
 *  reserve leave little or no free commit, so the suggested minimum may not fit. Said beside
 *  the field rather than hidden behind the clamp. */
export function cacheSuggestionMayNotFit(
    freeCommitMib: number | null | undefined,
    ramNeededGb: number | null | undefined,
): boolean {
    if (typeof freeCommitMib !== 'number' || !Number.isFinite(freeCommitMib)) return false;
    if (typeof ramNeededGb !== 'number' || !Number.isFinite(ramNeededGb)) return false;
    return unclampedCacheMib(freeCommitMib, ramNeededGb) < CACHE_SUGGESTION_MIN_MIB;
}

/** Free commit in MiB from local_engine_status, or null when it was not measured. */
export function freeCommitMibOf(reading: FreeCommitReading | null | undefined): number | null {
    const v = reading?.avail_page_file_mib;
    return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

function clockText(at: Date): string {
    const two = (n: number) => String(n).padStart(2, '0');
    return `${two(at.getHours())}:${two(at.getMinutes())}`;
}

/** The text beside the field while its value is the suggestion; `at` is when it was made. */
export function suggestionMarkText(at: Date): string {
    return `suggested from free commit memory at ${clockText(at)}, not measured`;
}

/** Left for the rest of the machine before the resident budget is sized. It comes from one
 *  worked example (about 33 GiB free, --resident-budget-gib 24 ran, on IQ3_XXS), not from a
 *  measured rule. */
export const RESIDENT_SUGGESTION_RESERVE_MIB = 9216;

/** No budget is suggested below this: Strata's own setup never writes one (`resident_budget_gib`,
 *  setup.py: `max(8, ...)`). */
export const RESIDENT_SUGGESTION_MIN_GIB = 8;

/** Decimal GB in a GiB: setup.py divides `arena_gb` by this to compare it with a budget. */
const GB_PER_GIB = 1.073741824;

export interface ResidentSuggestion {
    /** Whole GiB to prefill; null when free memory leaves less than the least after the reserve. */
    gib: number | null;
    freeMib: number;
    /** The reading that set `freeMib`. */
    reading: 'free physical' | 'free commit';
    /** False off Windows, where free commit is not measured and physical stands alone. */
    commitMeasured: boolean;
    ramNeededGb: number;
    /** All of this size's experts in whole GiB; null when the table gives no figure, and no cap then. */
    arenaGib: number | null;
    /** True when the arena, not free memory, decided `gib`. */
    cappedByArena: boolean;
    /** Why freeing memory cannot make this size fit this PC, in words; null when it may. */
    neverFits: string | null;
}

const measured = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);

/**
 * The resident budget (--resident-budget-gib) for a size that needs more RAM than is free now:
 * the lower of free physical and free commit, less the reserve, rounded down to a whole GiB and
 * held to the size's arena as setup.py holds its own, with no value below the least.
 * Null when the size fits or a figure is unknown. A suggestion, not a measurement.
 * `ram_needed_gb` is compared as GiB: Strata holds its `ram_gb` against total RAM / 2^30.
 */
export function suggestedResidentBudget(
    reading: FreeCommitReading | null | undefined,
    size: Pick<StrataModelSize, 'ram_needed_gb' | 'experts_gb' | 'on_this_pc'> | null | undefined,
): ResidentSuggestion | null {
    const physical = measured(reading?.avail_phys_mib);
    const commit = measured(reading?.avail_page_file_mib);
    const needed = measured(size?.ram_needed_gb);
    if (needed === null || (physical === null && commit === null)) return null;
    const byCommit = physical === null || (commit !== null && commit < physical);
    const freeMib = (byCommit ? commit : physical) as number;
    if (needed * 1024 <= freeMib) return null;
    const afterReserve = Math.floor((freeMib - RESIDENT_SUGGESTION_RESERVE_MIB) / 1024);
    const experts = measured(size?.experts_gb);
    const arenaGib = experts !== null ? Math.floor(experts / GB_PER_GIB) : null;
    const enough = afterReserve >= RESIDENT_SUGGESTION_MIN_GIB;
    const cappedByArena = enough && arenaGib !== null && arenaGib < afterReserve;
    const total = measured(reading?.total_phys_mib);
    const verdict = size?.on_this_pc ?? '';
    return {
        gib: !enough ? null : cappedByArena ? arenaGib : afterReserve,
        freeMib,
        reading: byCommit ? 'free commit' : 'free physical',
        commitMeasured: commit !== null,
        ramNeededGb: needed,
        arenaGib,
        cappedByArena,
        neverFits: total !== null && needed * 1024 > total
            ? `this PC has ${(total / 1024).toFixed(1)} GiB of RAM in all`
            : verdict.startsWith('does not fit') ? `${STRATA_ESTIMATE_LABEL} for this PC: ${verdict}` : null,
    };
}

/** The text beside the resident budget while its value is the suggestion. */
export function residentSuggestionMarkText(at: Date): string {
    return `suggested from free memory at ${clockText(at)}, not measured`;
}

/** Why the form suggests a resident budget, or why it suggests none. */
export function residentSuggestionWhy(s: ResidentSuggestion): string {
    const reserveGib = RESIDENT_SUGGESTION_RESERVE_MIB / 1024;
    const short = `${STRATA_ESTIMATE_LABEL}: this size needs ${s.ramNeededGb} GB of RAM and `
        + `${(s.freeMib / 1024).toFixed(1)} GiB is free now (${s.reading})`;
    if (s.gib === null) {
        const none = `${short}. After the reserve of ${reserveGib} GiB that is less than `
            + `${RESIDENT_SUGGESTION_MIN_GIB} GiB, the least Strata's own setup writes, so no budget is suggested: `;
        return s.neverFits
            ? `${none}closing other programs will not make this size fit (${s.neverFits}). Type a budget: the engine `
                + 'keeps that many GiB of experts in RAM and reads the others from the model files, which can be slower.'
            : `${none}close other programs and choose the run config again, or type a budget.`;
    }
    const basis = s.commitMeasured
        ? 'the lower of free physical and free commit'
        : 'free physical (free commit is not measured on this OS)';
    const cap = s.cappedByArena ? `, held to the ${s.arenaGib} GiB of experts this size has` : '';
    return `${short}, so the engine may not start without a budget. The suggestion is ${basis}, less a reserve of `
        + `${reserveGib} GiB${cap}. That reserve comes from one worked example (about 33 GiB free, a budget of 24 `
        + 'ran on the smaller IQ3_XXS), not from a rule. The engine keeps this many GiB of experts in RAM and reads any others from the '
        + 'model files, which can be slower.';
}

/** Why `value` cannot be a resident budget, or null when it can or the field is empty. The words
 *  of the backend's `budget_error`, which refuses the same values at the save; `min` on the input
 *  is only a hint to the browser. */
export function residentBudgetError(value: number | null | undefined): string | null {
    if (value === null || value === undefined) return null;
    return Number.isFinite(value) && value > 0
        ? null
        : "Resident budget: a number more than 0, or empty to keep the base config's value.";
}

export interface RunConfigOption {
    /** Absolute path: the value of the select. */
    path: string;
    /** The file name: the text of the option. */
    label: string;
    /** `strata-<id>.json` without the frame, lower case; matches a model_table size `id`. */
    id: string;
}

function baseName(path: string): string {
    return path.split(/[\\/]/).pop() ?? path;
}

/** The size id a run config file is named for: `strata-<id>.json` -> `<id>`, lower case. */
export function runConfigId(path: string): string {
    return baseName(path).replace(/^strata-/i, '').replace(/\.json$/i, '').toLowerCase();
}

/** The run configs of list_model_files as select options, in the order given. */
export function runConfigOptions(runConfigs: readonly string[] | null | undefined): RunConfigOption[] {
    return (runConfigs ?? []).map((path) => ({ path, label: baseName(path), id: runConfigId(path) }));
}

/** The size whose id names this run config, or null. */
export function sizeForRunConfig(
    families: readonly StrataModelFamily[] | null | undefined,
    path: string,
): StrataModelSize | null {
    const id = runConfigId(path);
    if (!id) return null;
    for (const family of families ?? []) {
        for (const size of family.sizes ?? []) {
            if (typeof size.id === 'string' && size.id.toLowerCase() === id) return size;
        }
    }
    return null;
}

/** One line for a size: download, RAM needed, and Strata's words for this PC under its label. */
export function strataSizeLine(size: StrataModelSize): string {
    const parts: string[] = [];
    if (typeof size.download_gb === 'number') parts.push(`download ${size.download_gb} GB`);
    if (typeof size.ram_needed_gb === 'number') parts.push(`needs ${size.ram_needed_gb} GB RAM`);
    if (size.on_this_pc) parts.push(`${STRATA_ESTIMATE_LABEL}: ${size.on_this_pc}`);
    return parts.join(' · ');
}
