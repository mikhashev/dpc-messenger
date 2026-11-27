/**
 * The privacy and logging form's option lists and its guards
 * (THE-PRIVACY-COLLECTION-AND-LOGGING-SETTINGS-HAVE-NO-CONTROL-IN-THE-UI): a select
 * must always hold one of its options whatever a command answered, and the lists are
 * the backend reader's own set — the five levels run_service's own config comment
 * names, which its getters upper-case before getattr(logging, ...).
 */

/** The log levels the reader knows ([logging] level, console_level and every
 * [logging.modules] override). NOTSET and WARN are not in the set: the reader
 * documents exactly these five. */
export const LOG_LEVELS: readonly string[] = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'];

/** `text` when it names one of `allowed` (uppercased), else `fallback` — a select's
 * value must always be one of its options, whatever a command answered. */
export function allowedValue(text: unknown, allowed: readonly string[], fallback: string): string {
    const value = typeof text === 'string' ? text.trim().toUpperCase() : '';
    return allowed.includes(value) ? value : fallback;
}

/** A checkbox's bool, whatever a command answered. */
export function boolValue(text: unknown, fallback: boolean): boolean {
    return typeof text === 'boolean' ? text : fallback;
}

/** A dotted logger name the writer can store ([logging.modules]): non-empty parts
 * of ASCII letters, digits and underscores, none starting with a digit. The
 * client half of the backend's own check — a name it refuses never reaches the
 * command. */
export function isModuleName(text: string): boolean {
    return /^[A-Za-z_$][A-Za-z0-9_$]*(\.[A-Za-z_$][A-Za-z0-9_$]*)*$/.test(text.trim());
}
