// The song lineage graph's commands (core: music/lineage.py, `get_music_lineage` and
// `write_music_lineage_files`). Read-only except the second, which writes `lineage.json` and a static
// `lineage.html` beside the job's generated folder when the person asks.

import { sendCommand } from '$lib/coreService';
import { callMusic, type MusicAnswer } from '$lib/utils/musicPlayer';
import { isLineage, type MusicLineage } from '$lib/utils/musicLineage';

const STALE_TEXT = 'The backend answered a lineage this window cannot read. Update both together.';

/** The lineage of the job chain holding a library track. Never throws; a foreign answer is an error. */
export async function fetchMusicLineage(trackId: string): Promise<MusicAnswer<{ lineage: MusicLineage }>> {
    const answer = await callMusic<{ lineage: unknown }>(sendCommand, 'get_music_lineage', { track_id: trackId });
    if (!answer.ok) return answer;
    if (!isLineage(answer.data.lineage)) return { ok: false, unavailable: false, code: 'stale_lineage', message: STALE_TEXT };
    return { ok: true, data: { lineage: answer.data.lineage } };
}

/** Write `lineage.json` and `lineage.html` for a track's chain; answers the two paths. */
export function writeMusicLineageFiles(trackId: string): Promise<MusicAnswer<{ files: { json: string; html: string } }>> {
    return callMusic(sendCommand, 'write_music_lineage_files', { track_id: trackId });
}
