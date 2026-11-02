// Add Provider for type `strata`: choosing a Strata run config from the model files
// folder, installing a Strata model (list_strata_models / install_strata_model), and the
// conversation-cache suggestion that follows the choice. UI-local helpers, pure.

import type { FreeCommitReading } from './strataFields';

/** One size of a family in list_strata_models: Strata's own model_table row, field for
 *  field. `on_this_pc` is Strata's words for this PC and is advice only. */
export interface StrataModelSize {
    model: string;
    about?: string;
    download_gb?: number | null;
    ram_needed_gb?: number | null;
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

/** The text beside the field while its value is the suggestion; `at` is when it was made. */
export function suggestionMarkText(at: Date): string {
    const two = (n: number) => String(n).padStart(2, '0');
    return `suggested from free commit memory at ${two(at.getHours())}:${two(at.getMinutes())}, not measured`;
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
