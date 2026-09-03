/**
 * Which URL an <img> gets for an image attachment.
 *
 * A record that arrived from another node carries THAT node's absolute path.
 * Tauri's asset scope covers only this machine's ~/.dpc, so the request 403s
 * and the <img> shows a broken icon — while the same record holds a thumbnail
 * data-URL that would render (2026-09-03: a /home/... path on Windows).
 */

export interface ImageSrcAttachment {
    file_path?: string;
    thumbnail?: string;
}

export interface ImageSrcOptions {
    /** Whether this app runs on Windows; defaults to reading `navigator`. */
    isWindows?: boolean;
}

/** Windows from the browser globals; false where there is no `navigator`. */
export function detectWindows(): boolean {
    if (typeof navigator === 'undefined') return false;
    return /^win/i.test(navigator.platform || '') || /Windows/.test(navigator.userAgent || '');
}

// Rule: a POSIX home path (/home/, /Users/) on Windows, or a drive path (C:\) elsewhere, is another machine's.
export function isForeignPath(path: string, isWindows: boolean): boolean {
    return isWindows ? /^\/(home|Users)\//.test(path) : /^[A-Za-z]:\\/.test(path);
}

/**
 * Local `file_path` → `convert(file_path)`; otherwise the thumbnail data-URL.
 * A foreign path with no thumbnail yields '' rather than the converted path,
 * so the <img> is not requested at all.
 */
export function pickImageSrc(
    attachment: ImageSrcAttachment,
    convert: (path: string) => string,
    opts: ImageSrcOptions = {},
): string {
    const isWindows = opts.isWindows ?? detectWindows();
    const { file_path, thumbnail } = attachment;
    if (file_path && !isForeignPath(file_path, isWindows)) return convert(file_path);
    return thumbnail || '';
}
