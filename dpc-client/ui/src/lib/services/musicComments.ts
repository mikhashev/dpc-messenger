// The comments of the tracks the Music Player shows, one list per library track id, kept
// in a store so the now-playing slider (markers), the comment list and the A/B review read
// the same thing. The core stores them (music/comments.py) and broadcasts
// `music_comments_updated {track_id}` on every change, from any window, so a cached list
// is read again when that arrives.

import { writable, get } from 'svelte/store';
import { addMusicComment, deleteMusicComment, listMusicComments, updateMusicComment } from '$lib/coreService';
import type { MusicComment } from '$lib/utils/musicComments';

export const musicComments = writable<Record<string, MusicComment[]>>({});
export const musicCommentsError = writable<Record<string, string>>({});

const loading = new Set<string>();

function setError(trackId: string, message: string): void {
    musicCommentsError.update(m => {
        if (!message) {
            if (!(trackId in m)) return m;
            const { [trackId]: _drop, ...rest } = m;
            return rest;
        }
        return { ...m, [trackId]: message };
    });
}

/** Read a track's comments from the core and keep them. */
export async function loadComments(trackId: string): Promise<void> {
    if (!trackId || loading.has(trackId)) return;
    loading.add(trackId);
    try {
        const a = await listMusicComments(trackId);
        if (a.ok) {
            musicComments.update(m => ({ ...m, [trackId]: a.data.comments }));
            setError(trackId, '');
        } else {
            setError(trackId, a.unavailable ? 'Comments are not available on this backend yet.' : a.message);
        }
    } finally {
        loading.delete(trackId);
    }
}

/** Read the list once per track (the player and the review both ask on a track change). */
export function ensureComments(trackId: string | null | undefined): void {
    if (!trackId) return;
    if (trackId in get(musicComments)) return;
    void loadComments(trackId);
}

/** A `music_comments_updated` event from the core: read that track again if it is cached. */
export function noteCommentsUpdated(payload: { track_id?: string } | null | undefined): void {
    const id = payload?.track_id;
    if (id && id in get(musicComments)) void loadComments(id);
}

/** Each returns null on success, or the line to show. The list is read again after a change. */
export async function saveComment(
    trackId: string,
    value: { start_s: number; end_s: number | null; text: string },
    commentId?: string,
    action = 'note',
): Promise<string | null> {
    const a = commentId
        ? await updateMusicComment(trackId, commentId, { ...value, action })
        : await addMusicComment(trackId, { ...value, action });
    if (!a.ok) return a.message;
    await loadComments(trackId);
    return null;
}

export async function removeComment(trackId: string, commentId: string): Promise<string | null> {
    const a = await deleteMusicComment(trackId, commentId);
    if (!a.ok) return a.message;
    await loadComments(trackId);
    return null;
}
