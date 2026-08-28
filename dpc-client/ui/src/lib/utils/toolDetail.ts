/**
 * What an expanded tool row shows: the arguments the tool received, and the
 * output it produced, both readable.
 *
 * Two decisions live here rather than in the component:
 *
 * 1. **A single string argument reads as itself.** `{"command": "python -c …"}`
 *    is a shell command, not a JSON object, and the reason this row exists at
 *    all is that a person wants to read the command. Several arguments read as
 *    one line each; anything that is not JSON is shown as it came.
 * 2. **A cap that names what it hid.** History holds single tool outputs of
 *    38 000 characters, so a cap stays — but `…` alone reads as "this is where
 *    it ended", which is exactly the misreading the row was meant to prevent.
 */

/** Characters kept in an expanded block before the tail is summarised. */
export const EXPANDED_MAX = 4000;

export function clampDetail(text: string, maxLen: number = EXPANDED_MAX): string {
    if (!text) return '';
    if (text.length <= maxLen) return text;
    const hidden = text.length - maxLen;
    return text.slice(0, maxLen) + `\n… ${hidden} more characters (full text in the agent log)`;
}

export function formatToolInput(input: string): string {
    if (!input) return '';
    try {
        const obj = JSON.parse(input);
        if (obj && typeof obj === 'object' && !Array.isArray(obj)) {
            const entries = Object.entries(obj);
            if (entries.length === 0) return '';
            if (entries.length === 1 && typeof entries[0][1] === 'string') {
                return entries[0][1] as string;
            }
            return entries
                .map(([k, v]) => `${k}: ${typeof v === 'string' ? v : JSON.stringify(v)}`)
                .join('\n');
        }
    } catch {
        // Not JSON — the raw string is the best we have, and showing it beats
        // showing nothing, which is what the row did before.
    }
    return input;
}
