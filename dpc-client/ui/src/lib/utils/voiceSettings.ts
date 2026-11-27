/**
 * The voice settings form's fields (config-parity card, audit/config_ui_parity_2026-11-27.md):
 * the numbers and comma lists of the three sections parse before a command is sent, the
 * way `parseSwapWait` does for the idle swap. The backend refuses anything else; these
 * say so first, with the field's own words.
 */

/** A whole number `atLeast` or more, or null when the text is not one. */
export function parseWholeNumber(text: string | number | undefined | null, atLeast: number): number | null {
    if (typeof text === 'number') {
        return Number.isSafeInteger(text) && text >= atLeast ? text : null;
    }
    const v = (text ?? '').trim();
    if (!/^-?\d+$/.test(v)) return null;
    const n = Number(v);
    return Number.isSafeInteger(n) && n >= atLeast ? n : null;
}

/** A figure (decimals allowed) `atLeast` or more, or null when the text is not one. */
export function parseFigure(text: string | number | undefined | null, atLeast: number): number | null {
    const v = typeof text === 'number' ? String(text) : (text ?? '').trim();
    if (!/^-?\d+(\.\d+)?$/.test(v)) return null;
    const n = Number(v);
    return Number.isFinite(n) && n >= atLeast ? n : null;
}

/** A figure (decimals allowed) strictly above `floor`, or null when the text is not one. */
export function parseFigureAbove(text: string | number | undefined | null, floor: number): number | null {
    const n = parseFigure(text, floor);
    return n !== null && n > floor ? n : null;
}

/** A comma list as the form sends it: entries trimmed, empties dropped. */
export function parseNameList(text: string | undefined | null): string[] {
    return (text ?? '')
        .split(',')
        .map((entry) => entry.trim())
        .filter((entry) => entry.length > 0);
}
