/**
 * What the live row of a running tool shows beside its label: the same preview a
 * finished call gets (`getToolArgPreview`), read from the progress message the
 * backend sends with the tool name (loop.py: the call's arguments as JSON, or
 * `Executing <tool>...` when there are none).
 *
 * Only a JSON object is arguments. A plain string is a status line, not input,
 * and a finished row never shows one, so the live row shows none either.
 */
import { getToolArgPreview } from './toolDisplay';

export function liveToolArgPreview(tool: string | null | undefined, message: string | null | undefined): string {
    if (!tool || !message) return '';
    const text = message.trim();
    if (!text.startsWith('{')) return '';
    try {
        const parsed = JSON.parse(text);
        if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) return '';
    } catch {
        return '';
    }
    const preview = getToolArgPreview(tool, text);
    return typeof preview === 'string' ? preview : '';
}
