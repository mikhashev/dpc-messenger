/**
 * The "Memory limit per process" line of the Agent map's D-PC backend card: what
 * `list_processes` says (`shell_memory_limit_mb`, `shell_memory_limit_source`),
 * how it is worded, what a typed value means, and when to warn. The backend owns
 * the rules (`set_shell_memory_limit` refuses a negative or non-integer value and
 * any change while an environment variable decides); this module only keeps the
 * screen from sending what it would refuse and from turning a 0 into a default.
 *
 * The unit is MiB on screen and `mb` in the command: one unit, 2^20 bytes (GLOSSARY,
 * "resource peaks").
 */

/** Below this (and not 0) every agent command that grows past it is stopped. */
export const MEMORY_LIMIT_WARN_BELOW_MIB = 1024;

/** The backend's built-in limit (`_DEFAULT_MEMORY_CEILING_MB` in dpc_agent/tools/process.py):
 *  what an emptied field puts back. Not "no limit": only 0 is that. */
export const MEMORY_LIMIT_DEFAULT_MIB = 8192;

/** The two variables the backend reads, the older name first. */
export const MEMORY_LIMIT_ENV_NAMES = ['DPC_SHELL_MEMORY_LIMIT_MB', 'DPC_SYSTEM_SHELL_MEMORY_LIMIT_MB'] as const;

export type MemoryLimitSource = 'env' | 'config' | 'default';

/** The two fields of the `list_processes` answer the line reads; an older backend sends neither. */
export interface MemoryLimitFields {
    shell_memory_limit_mb?: number | null;
    shell_memory_limit_source?: string | null;
}

export interface MemoryLimitView {
    /** The limit in MiB; 0 (or less) is no limit. */
    mb: number;
    source: MemoryLimitSource;
    /** "Memory limit per process: 32,768 MiB", or "... no limit". */
    line: string;
    /** "(default)" when the number is the built-in one, else empty. */
    suffix: string;
    /** Why the line cannot be edited here; empty when it can. */
    envNote: string;
    editable: boolean;
    /** The warning for the value in force; empty when there is none. */
    warning: string;
}

/** "no limit" for 0 (a hand-edited negative runs as no limit too), else "32,768 MiB". */
export function formatMemoryLimit(mb: number): string {
    return mb > 0 ? `${mb.toLocaleString('en-US')} MiB` : 'no limit';
}

/** The warning for a limit, or '' : only a limit below 1,024 MiB that is not 0. */
export function memoryLimitWarning(mb: number): string {
    if (mb > 0 && mb < MEMORY_LIMIT_WARN_BELOW_MIB) {
        return `Below ${MEMORY_LIMIT_WARN_BELOW_MIB.toLocaleString('en-US')} MiB: every agent command that grows past it is stopped.`;
    }
    return '';
}

/** What the card shows, or null when the backend sent no limit (an older backend, or nothing read yet). */
export function memoryLimitView(listing: MemoryLimitFields | null | undefined): MemoryLimitView | null {
    const mb = listing?.shell_memory_limit_mb;
    if (typeof mb !== 'number' || !Number.isFinite(mb)) return null;
    const source: MemoryLimitSource =
        listing?.shell_memory_limit_source === 'env' || listing?.shell_memory_limit_source === 'config'
            ? listing.shell_memory_limit_source
            : 'default';
    const fromEnvironment = source === 'env';
    return {
        mb,
        source,
        line: `Memory limit per process: ${formatMemoryLimit(mb)}`,
        suffix: source === 'default' ? '(default)' : '',
        // The backend says "env" for either name and the screen cannot tell which, so both are named.
        envNote: fromEnvironment ? `set by ${MEMORY_LIMIT_ENV_NAMES.join(' or ')} in the environment` : '',
        editable: !fromEnvironment,
        warning: memoryLimitWarning(mb),
    };
}

/** What the number input holds when it opens: the number, 0 for no limit. */
export function memoryLimitInputText(mb: number): string {
    return mb > 0 ? String(mb) : '0';
}

/**
 * What the field holds, ready to send as `mb`. 0 is no limit; empty (or only spaces) puts the
 * default 8,192 MiB back, not no limit; digits may carry thousands commas ("32,768");
 * anything else is an error the card can show.
 */
export function parseMemoryLimitInput(typed: string | number | null | undefined): { mb: number } | { error: string } {
    const text = typed === null || typed === undefined ? '' : String(typed).trim();
    if (text === '') return { mb: MEMORY_LIMIT_DEFAULT_MIB };
    const digits = text.replace(/,/g, '');
    if (!/^\d+$/.test(digits) || !Number.isSafeInteger(Number(digits))) {
        return { error: 'Enter a whole number of MiB (for example 8192), 0 for no limit, or empty for the default 8,192.' };
    }
    return { mb: Number(digits) };
}
