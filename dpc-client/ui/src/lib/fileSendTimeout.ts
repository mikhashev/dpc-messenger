/**
 * How long the UI waits for a command that sends a file.
 *
 * The backend hashes the file before it answers, so the wait grows with the
 * size: base 60 s + 40 s per GB + a 20 s margin (1 GB = 120 s, 10 GB = 480 s).
 * `send_file` had this alone; `send_group_file` and `send_group_image` ran on
 * the 60 s default and timed out on a large file that was still being hashed.
 */

const BASE_MS = 60_000;
const PER_GB_MS = 40_000;
const SAFETY_MARGIN_MS = 20_000;
const GB = 1024 * 1024 * 1024;

/** Commands whose answer waits for the file to be hashed. */
const FILE_SEND_COMMANDS = new Set(['send_file', 'send_group_file', 'send_group_image']);

export function fileSendTimeoutMs(sizeBytes: number): number {
    const size = Number.isFinite(sizeBytes) && sizeBytes > 0 ? sizeBytes : 0;
    return BASE_MS + (size / GB) * PER_GB_MS + SAFETY_MARGIN_MS;
}

/** Bytes behind a `data:...;base64,` URL (or bare base64): 3 bytes per 4 characters, less padding. */
export function dataUrlBytes(dataUrl: unknown): number {
    if (typeof dataUrl !== 'string') return 0;
    const comma = dataUrl.indexOf(',');
    const b64 = dataUrl.startsWith('data:') && comma >= 0 ? dataUrl.slice(comma + 1) : dataUrl;
    const padding = b64.endsWith('==') ? 2 : b64.endsWith('=') ? 1 : 0;
    return Math.max(0, Math.floor((b64.length * 3) / 4) - padding);
}

/**
 * The size-scaled timeout for a command, or null when the command is not a file
 * send (the caller keeps its own default). A file path is sized by the UI and
 * passed as `file_size_bytes`; an image travels in the payload, so its size is read from it.
 */
export function fileSendTimeoutFor(command: string, payload: any): number | null {
    if (!FILE_SEND_COMMANDS.has(command)) return null;
    const size = command === 'send_group_image'
        ? dataUrlBytes(payload?.image_base64)
        : Number(payload?.file_size_bytes) || 0;
    return fileSendTimeoutMs(size);
}

/** Size of a local file in bytes via the Tauri shell; 0 when it cannot be read (the default timeout then applies). */
export async function getLocalFileSize(filePath: string): Promise<number> {
    try {
        const { invoke } = await import('@tauri-apps/api/core');
        const metadata = await invoke('get_file_metadata', { path: filePath });
        return Number((metadata as any).size) || 0;
    } catch (e) {
        console.warn('Could not get file size for timeout calculation:', e);
        return 0;
    }
}
