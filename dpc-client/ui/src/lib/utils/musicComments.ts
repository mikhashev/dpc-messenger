// The pure parts of the music player's timecoded comments (`mp.comment/1`, SPEC §4): the
// shapes, where a marker sits on the position slider, the form's validation (the core
// checks the same rules again), and which key presses the "C" shortcut may take.

export type CommentAction = 'note' | 'regenerate_region' | 'extend_ending' | 'fix_seam' | 'remove_region';

/** `mp.comment/1` as the core stores it (times are `t_start` / `t_end`; `t_end` is null for a point). */
export interface MusicComment {
    schema?: string;
    id: string;
    track_id: string;
    version_id?: string;
    kind: 'point' | 'region';
    t_start: number;
    t_end: number | null;
    text: string;
    action: CommentAction | string;
    author: { kind: 'human' | 'agent'; name: string };
    created_at: string;
    updated_at?: string;
}

/** The actions the form lists (SPEC §4). `supported` says whether DPC has the action built; whether it
 *  runs now is the capability report's (`utils/musicEdit.ts` `editActionState`). Extend ending is planned. */
export const COMMENT_ACTIONS: { value: CommentAction; label: string; supported: boolean }[] = [
    { value: 'note', label: 'Note', supported: true },
    { value: 'regenerate_region', label: 'Regenerate region', supported: true },
    { value: 'extend_ending', label: 'Extend ending', supported: false },
    { value: 'fix_seam', label: 'Fix seam', supported: true },
    { value: 'remove_region', label: 'Remove region', supported: true },
];

/** The core's text limit (`comments.MAX_TEXT_CHARS`). */
export const COMMENT_MAX_CHARS = 2000;

/** A position in seconds as the form shows it: at most two decimals, no trailing zeros. */
export function secondsText(t: number | null | undefined): string {
    if (t == null || !Number.isFinite(t) || t < 0) return '';
    return String(Math.round(t * 100) / 100);
}

/** Where a marker sits on the slider: `left` is a percentage, clamped to 0..100; `width`
 *  is the region's percentage (clamped so the bar stays inside the slider) or null for a
 *  point. Null when the duration is not known (nothing to place a marker on). */
export function markerGeometry(
    c: Pick<MusicComment, 't_start' | 't_end'>,
    duration: number | null | undefined,
): { left: number; width: number | null } | null {
    if (duration == null || !Number.isFinite(duration) || duration <= 0) return null;
    const pct = (t: number) => Math.max(0, Math.min(100, (t / duration) * 100));
    const left = pct(c.t_start);
    if (c.t_end == null) return { left, width: null };
    return { left, width: Math.max(0, pct(c.t_end) - left) };
}

/** "0:12" for a point, "0:12-0:18" for a region. */
export function commentTimeLabel(c: Pick<MusicComment, 't_start' | 't_end'>, format: (t: number) => string): string {
    return c.t_end == null ? format(c.t_start) : `${format(c.t_start)}–${format(c.t_end)}`;
}

export interface CommentFormValues {
    start: string;
    end: string;
    text: string;
}

export interface CommentFormResult {
    /** Null when the form is valid. */
    error: string | null;
    /** The core payload parts when it is valid. */
    value?: { start_s: number; end_s: number | null; text: string };
}

function parseSeconds(raw: string): number | null {
    const t = raw.trim().replace(',', '.');
    if (!t || !/^\d+(\.\d+)?$/.test(t)) return null;
    const n = Number(t);
    return Number.isFinite(n) ? n : null;
}

/** The form's own checks, the core's rules: Start a number >= 0 within the duration when it
 *  is known; End empty (a point) or a number greater than Start (and within the duration);
 *  Text required and within the limit. */
export function validateCommentForm(v: CommentFormValues, duration: number | null | undefined): CommentFormResult {
    const known = duration != null && Number.isFinite(duration) && duration > 0;
    const start = parseSeconds(v.start);
    if (start == null) return { error: 'Start must be a number of seconds, 0 or more.' };
    if (known && start > (duration as number)) return { error: `Start is past the end of the track (${secondsText(duration)} s).` };
    let end: number | null = null;
    if (v.end.trim()) {
        end = parseSeconds(v.end);
        if (end == null) return { error: 'End must be a number of seconds, or empty for a point.' };
        if (end <= start) return { error: 'End must be greater than Start (leave it empty for a point).' };
        if (known && end > (duration as number)) return { error: `End is past the end of the track (${secondsText(duration)} s).` };
    }
    const text = v.text.trim();
    if (!text) return { error: 'Write the note: the text is required.' };
    if (text.length > COMMENT_MAX_CHARS) return { error: `The note is ${text.length} characters; the limit is ${COMMENT_MAX_CHARS}.` };
    return { error: null, value: { start_s: start, end_s: end, text } };
}

/** The element a key press came from, as far as typing goes. */
export interface KeyTarget {
    tagName?: string;
    isContentEditable?: boolean;
}

export function isTypingTarget(t: KeyTarget | null | undefined): boolean {
    if (!t) return false;
    return !!t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/i.test(t.tagName ?? '');
}

/** Whether a key press is the "C" (new comment) shortcut: a bare C, not typing in a field. */
export function isCommentShortcut(e: { key: string; ctrlKey?: boolean; metaKey?: boolean; altKey?: boolean; shiftKey?: boolean },
                                  target: KeyTarget | null | undefined): boolean {
    if (e.key.toLowerCase() !== 'c') return false;
    if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return false;
    return !isTypingTarget(target);
}
