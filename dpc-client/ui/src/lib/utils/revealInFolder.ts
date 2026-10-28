/**
 * "Show in folder" for a file the chat holds: the OS file manager opens the
 * folder with the file selected (Explorer, Finder, or the Linux file manager).
 *
 * The reveal itself is passed in, so this stays testable without Tauri; the
 * caller hands it `revealItemInDir` from @tauri-apps/plugin-opener.
 */

export type RevealItem = (path: string) => Promise<void>;

/** What the bubble shows beside the button when the reveal fails. */
export function revealFailureText(error: unknown): string {
    const raw = error instanceof Error ? error.message : typeof error === 'string' ? error : '';
    const detail = raw.trim();
    // A plugin message can be a long OS error; the bubble has room for a line.
    const short = detail.length > 120 ? `${detail.slice(0, 117)}...` : detail;
    return short ? `Could not show the file: ${short}` : 'Could not show the file';
}

/** Reveal `path`; null on success, the line to show on failure. */
export async function revealInFolder(path: string | null | undefined, reveal: RevealItem): Promise<string | null> {
    if (!path) return 'The file has no saved location';
    try {
        await reveal(path);
        return null;
    } catch (e) {
        console.error('Failed to show the file in its folder:', e);
        return revealFailureText(e);
    }
}
