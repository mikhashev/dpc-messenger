/**
 * The providers editor's "API Key Environment Variable" field holds a variable's
 * NAME. A key pasted there by mistake must not travel as a name: the backend
 * would look it up, fail, and the value would land in an error and a log line.
 */

const ENV_VAR_NAME = /^[A-Za-z_][A-Za-z0-9_]*$/;

/** A valid environment variable name (letters, digits, underscores; no leading digit). */
export function isEnvVarName(value: string | undefined | null): boolean {
    return ENV_VAR_NAME.test((value ?? '').trim());
}

/** Something typed in the variable field that cannot be a variable's name — most likely a key. */
export function looksLikeKey(value: string | undefined | null): boolean {
    const v = (value ?? '').trim();
    return v !== '' && !ENV_VAR_NAME.test(v);
}

/** The field's value when it may be sent as a variable name, else undefined. */
export function envNameOrUndefined(value: string | undefined | null): string | undefined {
    const v = (value ?? '').trim();
    return v && ENV_VAR_NAME.test(v) ? v : undefined;
}
