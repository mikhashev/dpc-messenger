// The waveform peaks of the tracks the player shows, one request per library track id.
// The core decodes (ffmpeg) and caches them on disk by content hash (music/peaks.py); this
// keeps the answer in memory so the card and the A/B review do not ask twice. A failure is
// not kept: the next ask tries again.

import { getMusicTrackPeaks } from '$lib/coreService';
import type { Peak } from '$lib/utils/musicWaveform';

export interface TrackPeaks {
    peaks: Peak[];
    duration: number;
}

const MAX_KEPT = 24;
const kept = new Map<string, Promise<TrackPeaks>>();

export class PeaksUnavailable extends Error {}

/** The peaks of a track; rejects with `PeaksUnavailable` and the reason to show. */
export function loadPeaks(trackId: string): Promise<TrackPeaks> {
    const hit = kept.get(trackId);
    if (hit) return hit;
    const pending = (async (): Promise<TrackPeaks> => {
        const a = await getMusicTrackPeaks(trackId);
        if (!a.ok) throw new PeaksUnavailable(a.unavailable ? 'the backend does not have it yet' : a.message);
        if (!a.data.peaks.length) throw new PeaksUnavailable('the track has no audio to draw');
        return { peaks: a.data.peaks, duration: a.data.duration_s };
    })();
    kept.set(trackId, pending);
    if (kept.size > MAX_KEPT) kept.delete(kept.keys().next().value as string);
    pending.catch(() => {
        if (kept.get(trackId) === pending) kept.delete(trackId);
    });
    return pending;
}
