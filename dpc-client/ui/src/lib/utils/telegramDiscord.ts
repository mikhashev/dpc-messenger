/**
 * The Telegram and Discord form's guards
 * (THE-TELEGRAM-AND-DISCORD-SETTINGS-CAN-ONLY-BE-TYPED-INTO-CONFIG-INI,
 * audit/config_ui_parity_2026-11-27.md): the Discord limit numbers parse
 * before a command is sent, the way the voice form's do, and the token
 * field's placeholder words all the form knows about a stored token — that it
 * is stored (dots, never the value: the backend answers `token_set` only).
 */

/** A whole number `atLeast` or more, or null when the text is not one. */
export function parseWholeNumber(text: string | number | undefined | null, atLeast: number): number | null {
    if (typeof text === 'number') {
        return Number.isSafeInteger(text) && text >= atLeast ? text : null;
    }
    if (typeof text !== 'string') return null;
    const v = text.trim();
    if (!/^-?\d+$/.test(v)) return null;
    const n = Number(v);
    return Number.isSafeInteger(n) && n >= atLeast ? n : null;
}

/** A checkbox's bool, whatever a command answered. */
export function boolValue(text: unknown, fallback: boolean): boolean {
    return typeof text === 'boolean' ? text : fallback;
}

/** The password field's placeholder. A stored token is never sent to the form,
 * so dots stand in for it and the words say what an empty field does (keeps
 * it — only the Remove control clears it). */
export function tokenPlaceholder(tokenSet: boolean): string {
    return tokenSet
        ? '•••••••• (set — leave empty to keep it)'
        : 'Paste the bot token';
}
